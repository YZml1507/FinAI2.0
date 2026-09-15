#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""方案 D 宽度择时归因诊断 —— 驱动脚本。

与 breadth-v1-default 验证实验同口径（默认阈值：进攻 0.40 / 防守 0.20 /
警戒上限 0.50 / 确认 2 日），用观测探针替换策略类跑同区间回测，
导出逐日明细并回答三个归因问题：
  Q1 警戒区（宽度 20%~40%）50% 仓位上限是否只在调仓日生效、日常满仓扛跌？
  Q2 冰点确认期 2 天内的回撤暴露有多大？
  Q3 各档位的收益贡献与净值回撤段落分布？

产物隔离到 ``experiments/lab/breadth-v1-diagnosis/<tag>/``，⛔ 不污染 runs。

用法：
  .venv/bin/python scripts/lab/run_breadth_diagnosis.py \
      [--start 2015-01-05] [--end 2024-12-31] [--tag v1-default]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import timezone as _timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from strategy.candidates import DividendConfig                # noqa: E402
from scripts import run_dividend_backtest as rdb              # noqa: E402
from scripts.lab.breadth_probe import DividendBreadthProbe    # noqa: E402

OUT_DIR = ROOT / "experiments" / "lab" / "breadth-v1-diagnosis"
BREADTH_FILE = ROOT / "experiments" / "lab" / "market-breadth-a" / "breadth20_daily.parquet"

# 与 breadth-v1-default 实验一致的默认阈值
BREADTH_OVERRIDES = dict(
    use_breadth_timing=True,
    use_ma200_timing=False,
    breadth_attack_threshold=Decimal("0.40"),
    breadth_defense_threshold=Decimal("0.20"),
    breadth_mid_cap=Decimal("0.50"),
    breadth_ice_confirm_days=2,
)


def _parse_date(s: str) -> _date:
    y, m, d = s.split("-")
    return _date(int(y), int(m), int(d))


def _load_breadth_series(path: Path) -> dict:
    import pandas as pd
    if not path.exists():
        raise SystemExit(f"宽度序列文件缺失: {path}（⛔ Fail-Closed）")
    df = pd.read_parquet(path)
    return {str(d)[:10]: Decimal(str(b)) for d, b in zip(df["date"], df["breadth20"])}


def _provenance() -> dict:
    """出处三件套：Git SHA + Data Hash + Timestamp。"""
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True,
                                      stderr=subprocess.DEVNULL, cwd=ROOT).strip()
    except Exception:
        sha = "unknown"
    try:
        status = subprocess.check_output(["git", "status", "--porcelain"], text=True,
                                         stderr=subprocess.DEVNULL, cwd=ROOT).strip()
    except Exception:
        status = ""
    import hashlib
    data_hash = hashlib.sha256()
    if BREADTH_FILE.exists():
        data_hash.update(BREADTH_FILE.read_bytes())
    return {
        "git_sha": sha,
        "git_dirty": bool(status),
        "breadth_file": str(BREADTH_FILE.relative_to(ROOT)),
        "breadth_sha256": data_hash.hexdigest()[:16],
        "timestamp": _datetime.now(_timezone.utc).isoformat(),
        "script": "scripts/lab/run_breadth_diagnosis.py",
    }


def _aggregate(daily: list[dict]) -> dict:
    days = [r for r in daily if r["warmup_done"] and r["nav"] is not None]
    if len(days) < 2:
        return {"error": "有效交易日不足"}

    # 逐日收益（昨收净值→今收净值）与前高回撤
    prev_nav = days[0]["nav"]
    peak = prev_nav
    zone_days = Counter()
    zone_ret_sum = Counter()
    zone_dd_days = Counter()
    worst_zone_day: dict[str, dict] = {}
    dd_rows = []
    for r in days[1:]:
        nav = r["nav"]
        ret = nav / prev_nav - 1.0 if prev_nav else 0.0
        peak = max(peak, nav)
        dd = nav / peak - 1.0
        z = r["zone"]
        zone_days[z] += 1
        zone_ret_sum[z] += ret
        if dd < -0.001:
            zone_dd_days[z] += 1
            dd_rows.append({"date": r["date"], "zone": z, "dd": round(dd, 6),
                            "breadth": r["breadth"], "positions": r["positions"],
                            "pos_value_est": r["pos_value_est"], "cash": r["cash"],
                            "nav": nav})
        w = worst_zone_day.get(z)
        if w is None or ret < w["ret"]:
            worst_zone_day[z] = {"date": r["date"], "ret": round(ret, 6),
                                 "breadth": r["breadth"], "positions": r["positions"]}
        prev_nav = nav

    # Q1 证据：警戒区（mid）日仍持有 ≥3 仓的天数 vs mid 总天数
    mid_holding = [r for r in days[1:] if r["zone"] == "mid" and r["positions"] >= 3]
    # Q2 证据：冰点确认期（ice_pending）持有仓位的回撤日
    pend_rows = [r for r in dd_rows if r["zone"] == "ice_pending"]

    return {
        "trading_days": len(days),
        "zone_days": dict(zone_days),
        "zone_avg_daily_ret": {z: round(zone_ret_sum[z] / max(1, zone_days[z]), 8)
                               for z in zone_days},
        "zone_dd_days": dict(zone_dd_days),
        "worst_zone_day": worst_zone_day,
        "q1_mid_holding_ge3_days": len(mid_holding),
        "q1_mid_total_days": zone_days.get("mid", 0),
        "q2_ice_pending_dd_events": len(pend_rows),
        "q2_ice_pending_dd_top": sorted(pend_rows, key=lambda x: x["dd"])[:10],
        "dd_top20": sorted(dd_rows, key=lambda x: x["dd"])[:20],
    }


def _render_md(stats: dict, prov: dict, tag: str, start: str, end: str,
               overrides: dict, metrics: dict, gates: dict) -> str:
    def _ov(k):
        v = overrides[k]
        return str(v) if isinstance(v, Decimal) else v
    lines = [
        f"# 方案 D 宽度择时归因诊断报告（{tag}）",
        "",
        f"> 区间: {start} ~ {end} ｜ 口径: 策略行为零改动纯观测探针，与 breadth-v1-default 实验同参数",
        "",
        "## 出处三件套",
        "",
        f"- Git SHA: `{prov['git_sha']}`（dirty: {prov['git_dirty']}）",
        f"- 宽度数据: `{prov['breadth_file']}` sha256[:16]=`{prov['breadth_sha256']}`",
        f"- 生成时间: {prov['timestamp']}",
        f"- 诊断脚本: `{prov['script']}`",
        "",
        "## 注入参数（与验证实验一致）",
        "",
        f"- 进攻线 breadth_attack_threshold = {_ov('breadth_attack_threshold')}",
        f"- 防守线 breadth_defense_threshold = {_ov('breadth_defense_threshold')}",
        f"- 警戒区仓位上限 breadth_mid_cap = {_ov('breadth_mid_cap')}",
        f"- 冰点确认天数 breadth_ice_confirm_days = {_ov('breadth_ice_confirm_days')}",
        "",
        "## 回测指标复核（探针运行）",
        "",
        f"- CAGR: {metrics.get('cagr')} ｜ MDD: {metrics.get('max_drawdown')} ｜ "
        f"总收益: {metrics.get('total_return')} ｜ 换手率: {metrics.get('annual_turnover')}",
        "",
        "## 总览",
        "",
        f"- 有效交易日: {stats['trading_days']}",
        f"- 档位天数分布: {stats['zone_days']}",
        f"- 档位日均收益: {stats['zone_avg_daily_ret']}",
        f"- 档位回撤天数(回撤>0.1%): {stats['zone_dd_days']}",
        "",
        "## Q1 警戒区扛跌检验（宽度 20%~40% 仓位上限 50%）",
        "",
        f"- 警戒区(mid)总天数: {stats['q1_mid_total_days']}，"
        f"其中持仓≥3 只的天数: {stats['q1_mid_holding_ge3_days']}",
        f"- 判读: 警戒区仓位上限只在调仓日生效；若持仓≥3 天数接近总天数，"
        f"说明日常未主动减仓、满仓/高仓位扛跌成立",
        "",
        "## Q2 冰点确认期回撤暴露（确认期内尚未清仓）",
        "",
        f"- 冰点确认期(ice_pending)发生回撤(>0.1%)的交易日数: {stats['q2_ice_pending_dd_events']}",
        "",
        "| 日期 | 宽度 | 回撤 | 持仓数 |",
        "|---|---|---|---|",
    ]
    for r in stats.get("q2_ice_pending_dd_top", []):
        lines.append(f"| {r['date']} | {r['breadth']} | {r['dd']} | {r['positions']} |")
    lines += [
        "",
        "## 各档位最差单日",
        "",
        "| 档位 | 日期 | 当日收益 | 宽度 | 持仓数 |",
        "|---|---|---|---|---|",
    ]
    for z, w in stats.get("worst_zone_day", {}).items():
        lines.append(f"| {z} | {w['date']} | {w['ret']} | {w['breadth']} | {w['positions']} |")
    lines += [
        "",
        "## 回撤最深 20 个交易日（按回撤排序）",
        "",
        "| 日期 | 档位 | 宽度 | 回撤 | 持仓数 | 持仓市值估算 |",
        "|---|---|---|---|---|---|",
    ]
    for r in stats.get("dd_top20", []):
        pv = r.get("pos_value_est")
        lines.append(f"| {r['date']} | {r['zone']} | {r['breadth']} | {r['dd']} | "
                     f"{r['positions']} | {round(pv, 0) if pv is not None else 'na'} |")
    if gates:
        lines += ["", "## 后置门禁状态（report-only）", ""]
        for gid, st in gates.items():
            lines.append(f"- {gid}: {st}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="方案 D 宽度择时归因诊断")
    ap.add_argument("--start", default="2015-01-05")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--tag", default="v1-default")
    args = ap.parse_args()

    run_dir = OUT_DIR / args.tag
    run_dir.mkdir(parents=True, exist_ok=True)

    breadth_series = _load_breadth_series(BREADTH_FILE)
    overrides = dict(BREADTH_OVERRIDES, breadth_series=breadth_series)

    # ① 猴子补丁：配置类注入宽度参数、策略类替换为探针（进程内生效，权威脚本零改动）
    orig_config = DividendConfig

    def _patched_config(**kwargs):
        return replace(orig_config(**kwargs), **overrides)

    rdb.DividendConfig = _patched_config          # type: ignore[misc]
    rdb.DividendStrategy = DividendBreadthProbe   # type: ignore[misc]
    DividendBreadthProbe.instances.clear()

    result = rdb.run_dividend_backtest_2015_2024(
        data_path=ROOT / "data" / "dividend_stocks",
        enable_gates=True,
        registry_root=run_dir,
        start_date=_parse_date(args.start),
        end_date=_parse_date(args.end),
    )

    if not DividendBreadthProbe.instances:
        print("⛔ 探针未被实例化（注入失败）", file=sys.stderr)
        return 2
    probe = DividendBreadthProbe.instances[-1]

    # ③ 明细落盘
    (run_dir / "breadth_daily.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in probe.daily_log),
        encoding="utf-8")

    # ④ 聚合 + 报告 + 出处
    stats = _aggregate(probe.daily_log)
    prov = _provenance()
    _rep = result.get("report")
    metrics = {
        "cagr": getattr(_rep, "cagr", None),
        "max_drawdown": getattr(_rep, "max_drawdown", None),
        "total_return": getattr(_rep, "total_return", None),
        "annual_turnover": getattr(_rep, "annual_turnover", None),
    }
    gates = result.get("gate_statuses", {}) or {}
    (run_dir / "DIAGNOSIS_STATS.json").write_text(
        json.dumps({"provenance": prov, "metrics": metrics, "stats": stats},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    (run_dir / "DIAGNOSIS_REPORT.md").write_text(
        _render_md(stats, prov, args.tag, args.start, args.end,
                   BREADTH_OVERRIDES, metrics, gates),
        encoding="utf-8")

    print(f"诊断完成 → {run_dir}")
    print(f"探针回测指标: {metrics}")
    print(f"档位天数: {stats.get('zone_days')}")
    print(f"Q1 警戒区持仓≥3 天数/总天数: "
          f"{stats.get('q1_mid_holding_ge3_days')}/{stats.get('q1_mid_total_days')}")
    print(f"Q2 冰点确认期回撤事件: {stats.get('q2_ice_pending_dd_events')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
