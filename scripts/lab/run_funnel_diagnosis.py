#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A-1 建不满仓根因诊断 —— 驱动脚本。

流程：
  ① 猴子补丁：把回测模块命名空间里的 ``DividendStrategy`` 替换为探针类
     （⛔ 不改 ``scripts/run_dividend_backtest.py`` 本体，进程内临时生效）；
  ② 调回测主函数跑指定区间（产物隔离到 ``experiments/lab/funnel-a1/``）；
  ③ 从探针实例注册表取回实例，导出漏斗明细 JSON；
  ④ 聚合统计并生成 Markdown 诊断报告。

用法：
  .venv/bin/python scripts/lab/run_funnel_diagnosis.py \
      [--start 2023-01-01] [--end 2024-12-31] [--tag smoke]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date as _date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import run_dividend_backtest as rdb           # noqa: E402
from scripts.lab.funnel_probe import DividendFunnelProbe   # noqa: E402

OUT_DIR = ROOT / "experiments" / "lab" / "funnel-a1"


def _parse_date(s: str) -> _date:
    y, m, d = s.split("-")
    return _date(int(y), int(m), int(d))


def _aggregate(probe: DividendFunnelProbe) -> dict:
    daily = probe.daily_log
    funnel = probe.funnel_log

    trading_days = [r for r in daily if r["warmup_done"]]
    zero_pos_days = [r for r in trading_days if r["positions"] == 0]
    pos_counts = [r["positions"] for r in trading_days]

    zone_days = Counter(r["zone"] for r in trading_days)
    avoid_days = sum(1 for r in trading_days if r["avoid_post"])
    clear_events = sum(1 for r in trading_days if r["breach_cleared_today"])
    rebuild_events = sum(1 for r in trading_days if r["rebuild_cleared_today"])

    due_days = [r for r in trading_days if r["due"]]
    executed = [r for r in trading_days if r["executed"]]

    # 调仓日漏斗聚合
    n_funnel = len(funnel)
    sum_f = Counter()
    for row in funnel:
        for k in ("f0_universe", "f2_data_missing", "f3_below_yield",
                  "f4_pool_overflow", "f5_over_positions",
                  "f6_portfolio_dropped", "signals_out"):
            sum_f[k] += row.get(k, 0)
    f6_reasons = Counter()
    for row in funnel:
        for reason, cnt in (row.get("f6_reasons") or {}).items():
            f6_reasons[reason] += cnt

    # 按年拆分零持仓日（与择时区域交叉）
    zero_by_year = Counter()
    total_by_year = Counter()
    zero_zone_above_by_year = Counter()
    for r in trading_days:
        y = r["date"][:4]
        total_by_year[y] += 1
        if r["positions"] == 0:
            zero_by_year[y] += 1
            if r["zone"] == "above":
                zero_zone_above_by_year[y] += 1

    return {
        "trading_days": len(trading_days),
        "zero_pos_days": len(zero_pos_days),
        "zero_pos_ratio": round(len(zero_pos_days) / max(1, len(trading_days)), 4),
        "avg_positions": round(sum(pos_counts) / max(1, len(pos_counts)), 2),
        "zone_days": dict(zone_days),
        "avoid_days": avoid_days,
        "clear_events": clear_events,
        "rebuild_events": rebuild_events,
        "due_days": len(due_days),
        "executed_days": len(executed),
        "funnel_rows": n_funnel,
        "funnel_sums": dict(sum_f),
        "f6_reasons": dict(f6_reasons),
        "year_table": {
            y: {
                "total": total_by_year[y],
                "zero": zero_by_year[y],
                "zero_above_ma200": zero_zone_above_by_year[y],
            } for y in sorted(total_by_year)
        },
    }


def _render_md(stats: dict, tag: str, start: str, end: str,
               gate_snippet: dict) -> str:
    lines = [
        f"# A-1 建不满仓漏斗诊断报告（{tag}）",
        "",
        f"> 区间: {start} ~ {end} ｜ 口径: 策略行为零改动纯观测探针",
        "",
        "## 总览",
        "",
        f"- 交易日（冷启动后）: {stats['trading_days']}",
        f"- 零持仓日: {stats['zero_pos_days']}（{stats['zero_pos_ratio'] * 100:.1f}%）",
        f"- 平均持仓: {stats['avg_positions']} 只",
        f"- MA200 区域分布: {stats['zone_days']}",
        f"- 择时避险日: {stats['avoid_days']}、确认清仓事件 {stats['clear_events']} 次、"
        f"解除避险事件 {stats['rebuild_events']} 次",
        f"- 调仓节拍到期日: {stats['due_days']}、实际执行调仓: {stats['executed_days']}",
        "",
        "## 调仓日漏斗（七关聚合）",
        "",
    ]
    s = stats["funnel_sums"]
    if stats["funnel_rows"]:
        n = stats["funnel_rows"]
        lines += [
            f"| 关卡 | 总淘汰数 | 日均/调仓日 |",
            f"|---|---|---|",
            f"| F0 股票池供给 | {s.get('f0_universe', 0)} | {s.get('f0_universe', 0) / n:.1f} |",
            f"| F2 数据残缺 | {s.get('f2_data_missing', 0)} | {s.get('f2_data_missing', 0) / n:.1f} |",
            f"| F3 股息率低于下限 | {s.get('f3_below_yield', 0)} | {s.get('f3_below_yield', 0) / n:.1f} |",
            f"| F4 候选池溢出 | {s.get('f4_pool_overflow', 0)} | {s.get('f4_pool_overflow', 0) / n:.1f} |",
            f"| F5 名额截取 | {s.get('f5_over_positions', 0)} | {s.get('f5_over_positions', 0) / n:.1f} |",
            f"| F6 组合层淘汰 | {s.get('f6_portfolio_dropped', 0)} | {s.get('f6_portfolio_dropped', 0) / n:.1f} |",
            f"| 产出信号 | {s.get('signals_out', 0)} | {s.get('signals_out', 0) / n:.2f} |",
            "",
            f"F6 淘汰原因分布: {stats['f6_reasons']}",
            "",
        ]
    else:
        lines.append("（本区间无调仓日记录）\n")

    lines += [
        "## 按年拆解（含均线上方零持仓日 = 择时不背锅的纯过滤问题）",
        "",
        "| 年 | 交易日 | 零持仓日 | 其中 MA200 上方 |",
        "|---|---|---|---|",
    ]
    for y, row in stats["year_table"].items():
        lines.append(f"| {y} | {row['total']} | {row['zero']} | {row['zero_above_ma200']} |")

    if gate_snippet:
        lines += ["", "## 后置门禁摘要", ""]
        for gid, st in gate_snippet.items():
            lines.append(f"- {gid}: {st}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="A-1 建不满仓漏斗诊断")
    ap.add_argument("--start", default="2015-01-05")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--tag", default="full")
    args = ap.parse_args()

    run_dir = OUT_DIR / args.tag
    run_dir.mkdir(parents=True, exist_ok=True)

    # ① 猴子补丁注入探针（进程内生效，权威脚本零改动）
    rdb.DividendStrategy = DividendFunnelProbe  # type: ignore[misc]
    DividendFunnelProbe.instances.clear()

    result = rdb.run_dividend_backtest_2015_2024(
        data_path=ROOT / "data" / "dividend_stocks",
        enable_gates=True,
        registry_root=run_dir,
        start_date=_parse_date(args.start),
        end_date=_parse_date(args.end),
    )

    if not DividendFunnelProbe.instances:
        print("⛔ 探针未被实例化（注入失败）", file=sys.stderr)
        return 2
    probe = DividendFunnelProbe.instances[-1]

    # ③ 明细落盘
    (run_dir / "funnel_daily.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in probe.daily_log),
        encoding="utf-8")
    (run_dir / "funnel_rebalance.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in probe.funnel_log),
        encoding="utf-8")

    # ④ 聚合 + 报告
    stats = _aggregate(probe)
    gate_map: dict = {}
    try:
        runs = sorted(run_dir.glob("runs/*.json"))
        if runs:
            gate_map = {k: v.get("status") for k, v in
                        (json.loads(runs[-1].read_text()).get("gate_statuses") or {}).items()}
    except Exception:
        pass
    report_md = _render_md(stats, args.tag, args.start, args.end, gate_map)
    (run_dir / "FUNNEL_REPORT.md").write_text(report_md, encoding="utf-8")
    (run_dir / "funnel_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    print(report_md)
    print(f"\n产物目录: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
