#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""宽度参数网格（bd* 实验）结果分析脚本。

功能：
  1. 全部 bd* 实验按 MDD 升序的榜单（实验名 / MDD / CAGR / 换手 / 胜率 / 四项宽度参数）；
  2. 达标组筛选（MDD < 0.35 且 CAGR > 0），无达标组时列 MDD 最小的 3 组；
  3. 四参数边际分析（固定其余参数，单参数变化对 MDD / CAGR 的影响方向）；
  4. 输出到 stdout 与 experiments/lab/_logs/grid_analysis.txt；
  5. 带出处三件套（git rev-parse HEAD、leaderboard 行数与 mtime、时间戳）。

设计为**随时可跑**（网格跑完前调用即输出当前完成组数）：
    .venv/bin/python scripts/lab/analyze_breadth_grid.py
    .venv/bin/python scripts/lab/analyze_breadth_grid.py --min-mdd 0.35 --min-cagr 0.0

依赖仅限项目已有：argparse / json / subprocess / datetime / pathlib。
⛔ 不发任何网络请求；不修改任何文件（除输出文件外）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LB_PATH = ROOT / "experiments" / "lab" / "leaderboard.jsonl"
OUT_PATH = ROOT / "experiments" / "lab" / "_logs" / "grid_analysis.txt"

# 宽度参数的规范名（实验名编码 bd{d}a{a}m{m}i{i}）
PARAM_KEYS = ("breadth_defense_threshold", "breadth_attack_threshold",
              "breadth_mid_cap", "breadth_ice_confirm_days")
PARAM_SHORT = {
    "breadth_defense_threshold": "defense",
    "breadth_attack_threshold": "attack",
    "breadth_mid_cap": "mid_cap",
    "breadth_ice_confirm_days": "ice_days",
}
METRIC_KEYS = ("max_drawdown", "cagr", "annual_turnover", "win_rate")

# 网格的完整定义（与 scripts/lab/breadth_grid.sh 的 24 组一致，用于统计完成率）
GRID_NAMES = [
    f"bd{d}a{a}m{m}i{i}"
    for d in ("20", "25")
    for a in ("35", "40", "45")
    for m in ("00", "30")
    for i in ("1", "2")
]
GRID_TOTAL = len(GRID_NAMES)          # 24


# ===================================================================== #
# 数据装载
# ===================================================================== #

def _to_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_leaderboard(path: Path) -> list[dict]:
    """读 leaderboard.jsonl -> 行列表（跳过空行与解析失败行）。"""
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def filter_bd(rows: list[dict]) -> list[dict]:
    """只取 bd* 实验（宽度网格产物）。"""
    return [r for r in rows if str(r.get("experiment", "")).startswith("bd")]


def params_of(row: dict) -> dict:
    """从 overrides 提取四项宽度参数（字符串 -> float；缺失记 None）。"""
    ov = row.get("overrides") or {}
    out = {}
    for k in PARAM_KEYS:
        out[k] = _to_float(ov.get(k))
    return out


def metrics_of(row: dict) -> dict:
    return {k: _to_float(row.get(k)) for k in METRIC_KEYS}


# ===================================================================== #
# 输出构件
# ===================================================================== #

def section_title(buf: list[str], text: str) -> None:
    buf.append("")
    buf.append("=" * 78)
    buf.append(text)
    buf.append("=" * 78)


def provenance_block() -> list[str]:
    """出处三件套：git HEAD / leaderboard 行数与 mtime / 当前时间戳。"""
    lines: list[str] = []
    # ① git rev-parse HEAD（只读命令；失败时显式记录『不可得』，不静默兜底）
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception as exc:                       # noqa: BLE001
        head = ""
        lines.append(f"git HEAD: <不可得: {exc}>")
    if head:
        lines.append(f"git HEAD: {head}")
    elif not lines:
        lines.append("git HEAD: <空输出（不在 git 仓或 git 不可用）>")

    # ② leaderboard.jsonl 行数与 mtime
    if LB_PATH.exists():
        st = LB_PATH.stat()
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        n = sum(1 for _ in open(LB_PATH, "r", encoding="utf-8"))
        lines.append(f"leaderboard: {LB_PATH.relative_to(ROOT)}")
        lines.append(f"  行数: {n}  |  mtime: {mtime}  |  size: {st.st_size} bytes")
    else:
        lines.append(f"leaderboard: <不存在: {LB_PATH}>")

    # ③ 当前时间戳（带时区）
    now = datetime.now(timezone.utc).astimezone()
    lines.append(f"分析时刻: {now.strftime('%Y-%m-%d %H:%M:%S %z')}")
    return lines


def leaderboard_table(bd_rows: list[dict]) -> list[str]:
    """① 全部 bd* 实验按 MDD 升序的榜单。"""
    lines: list[str] = []
    avail = [r for r in bd_rows if metrics_of(r)["max_drawdown"] is not None]
    avail.sort(key=lambda r: metrics_of(r)["max_drawdown"])

    header = (f"{'实验名':16s} {'MDD':>8s} {'CAGR':>8s} {'换手':>7s} "
              f"{'胜率':>7s} {'def':>5s} {'atk':>5s} {'mid':>5s} {'ice':>4s}")
    lines.append(header)
    lines.append("-" * len(header))
    for r in avail:
        m = metrics_of(r)
        p = params_of(r)
        lines.append(
            f"{r['experiment']:16s} "
            f"{(m['max_drawdown'] * 100):7.2f}% "
            f"{(m['cagr'] * 100):7.2f}% "
            f"{(m['annual_turnover'] * 100):6.1f}% "
            f"{(m['win_rate'] * 100):6.2f}% "
            f"{(p['breadth_defense_threshold'] or 0):5.2f} "
            f"{(p['breadth_attack_threshold'] or 0):5.2f} "
            f"{(p['breadth_mid_cap'] or 0):5.2f} "
            f"{int(p['breadth_ice_confirm_days'] or 0):4d}"
        )
    if not avail:
        lines.append("（尚无 bd* 实验结果）")
    return lines


def qualifying_block(bd_rows: list[dict], min_mdd: float, min_cagr: float) -> list[str]:
    """② 达标组筛选（MDD < min_mdd 且 CAGR > min_cagr）。"""
    lines: list[str] = []
    avail = [r for r in bd_rows if metrics_of(r)["max_drawdown"] is not None
             and metrics_of(r)["cagr"] is not None]
    ok = [r for r in avail
          if metrics_of(r)["max_drawdown"] < min_mdd and metrics_of(r)["cagr"] > min_cagr]

    if ok:
        ok.sort(key=lambda r: metrics_of(r)["max_drawdown"])
        lines.append(f"✅ 达标组合（MDD < {min_mdd:.2f} 且 CAGR > {min_cagr:.2f}）：{len(ok)} 组")
        for r in ok:
            m = metrics_of(r)
            lines.append(
                f"  {r['experiment']:16s} MDD {(m['max_drawdown'] * 100):6.2f}%  "
                f"CAGR {(m['cagr'] * 100):+6.2f}%  换手 {(m['annual_turnover'] * 100):5.1f}%  "
                f"胜率 {(m['win_rate'] * 100):5.2f}%")
        return lines

    lines.append(f"⛔ 无达标组合（MDD < {min_mdd:.2f} 且 CAGR > {min_cagr:.2f}）")
    lines.append("")
    lines.append("MDD 最小的 3 组（按 MDD 升序）：")
    avail.sort(key=lambda r: metrics_of(r)["max_drawdown"])
    for r in avail[:3]:
        m = metrics_of(r)
        lines.append(
            f"  {r['experiment']:16s} MDD {(m['max_drawdown'] * 100):6.2f}%  "
            f"CAGR {(m['cagr'] * 100):+6.2f}%  换手 {(m['annual_turnover'] * 100):5.1f}%  "
            f"胜率 {(m['win_rate'] * 100):5.2f}%")
    if not avail:
        lines.append("（尚无 bd* 实验结果）")
    return lines


# --------------------------------------------------------------------- #
# ③ 边际分析
# --------------------------------------------------------------------- #

def _fmt_delta(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v * 100:+.2f}pp"


def marginal_analysis(bd_rows: list[dict]) -> list[str]:
    """固定其余三项参数，单参数变化对 MDD / CAGR 的影响方向。

    口径：把实验按四项参数完整分组，对每个参数的每个取值，聚合该取值下
    所有实验的 MDD/CAGR 均值；再按参数取值排序，输出相邻取值间的差值
    （固定其他参数的『单变量对照』只能在取值两两都存在的子集上做）。
    """
    lines: list[str] = []
    rows = [r for r in bd_rows if metrics_of(r)["max_drawdown"] is not None
            and metrics_of(r)["cagr"] is not None
            and all(params_of(r)[k] is not None for k in PARAM_KEYS)]
    if len(rows) < 2:
        lines.append("（bd* 实验不足 2 组，无法做边际分析）")
        return lines

    for key in PARAM_KEYS:
        short = PARAM_SHORT[key]
        section_title(lines, f"边际分析 · {short}（{key}）")
        # 该参数的每个取值 -> [实验]
        by_val: dict[float, list[dict]] = {}
        for r in rows:
            by_val.setdefault(params_of(r)[key], []).append(r)
        vals = sorted(by_val)
        lines.append(f"  {'取值':>8s} {'组数':>4s} {'平均MDD':>10s} {'平均CAGR':>10s} "
                     f"{'MDD相邻差':>10s} {'CAGR相邻差':>10s}")
        prev = None
        for v in vals:
            ms = [metrics_of(r)["max_drawdown"] for r in by_val[v]]
            cs = [metrics_of(r)["cagr"] for r in by_val[v]]
            avg_m = sum(ms) / len(ms)
            avg_c = sum(cs) / len(cs)
            d_m = (avg_m - prev[0]) if prev else None
            d_c = (avg_c - prev[1]) if prev else None
            lines.append(
                f"  {v:8.2f} {len(by_val[v]):4d} {avg_m * 100:9.2f}% "
                f"{avg_c * 100:9.2f}% {_fmt_delta(d_m):>10s} {_fmt_delta(d_c):>10s}")
            prev = (avg_m, avg_c)

        # 方向结论（取最大 vs 最小取值）
        if len(vals) >= 2:
            lo = by_val[vals[0]]
            hi = by_val[vals[-1]]
            lo_m = sum(metrics_of(r)["max_drawdown"] for r in lo) / len(lo)
            hi_m = sum(metrics_of(r)["max_drawdown"] for r in hi) / len(hi)
            lo_c = sum(metrics_of(r)["cagr"] for r in lo) / len(lo)
            hi_c = sum(metrics_of(r)["cagr"] for r in hi) / len(hi)
            dm = hi_m - lo_m
            dc = hi_c - lo_c
            lines.append(f"  → {short} 从 {vals[0]:.2f} 升到 {vals[-1]:.2f}："
                         f"MDD {_fmt_delta(dm)}（{'升' if dm > 0 else '降' if dm < 0 else '平'}），"
                         f"CAGR {_fmt_delta(dc)}（{'升' if dc > 0 else '降' if dc < 0 else '平'}）")
        lines.append("")

    # 交叉表：defense × attack 的平均 MDD（固定 mid / ice 后的最常见组合）
    section_title(lines, "交叉表 · defense × attack 平均 MDD")
    by_da: dict[tuple, list[float]] = {}
    for r in rows:
        p = params_of(r)
        by_da.setdefault((p["breadth_defense_threshold"],
                          p["breadth_attack_threshold"]), []).append(
            metrics_of(r)["max_drawdown"])
    d_vals = sorted({k[0] for k in by_da})
    a_vals = sorted({k[1] for k in by_da})
    if d_vals and a_vals:
        lines.append("  " + "attack →".rjust(10) + "".join(f"{v:9.2f}" for v in a_vals))
        for dv in d_vals:
            cells = "".join(
                f"{(sum(by_da[(dv, av)]) / len(by_da[(dv, av)]) * 100):8.2f}%"
                if (dv, av) in by_da else "       --"
                for av in a_vals)
            lines.append(f"  def={dv:.2f}  " + cells)
    return lines


# --------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------- #

def build_report(bd_rows: list[dict], all_rows: list[dict],
                 min_mdd: float, min_cagr: float) -> list[str]:
    buf: list[str] = []
    buf.append("FinAI2.0 宽度参数网格（bd*）结果分析")
    buf.append(f"生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}（CST）")
    buf.append("")

    # 网格进度
    done = {str(r.get("experiment")) for r in bd_rows}
    n_done = len(done & set(GRID_NAMES))
    buf.append(f"网格进度: {n_done}/{GRID_TOTAL} 组完成"
               + ("（仍在运行中，本报表为中间态）" if n_done < GRID_TOTAL else "（全部完成）"))
    missing = [n for n in GRID_NAMES if n not in done]
    if missing:
        buf.append(f"未完成: {' '.join(missing)}")

    # 出处三件套
    section_title(buf, "出处三件套（Provenance）")
    buf.extend(provenance_block())

    # ① 榜单
    section_title(buf, f"① bd* 实验榜单（按 MDD 升序，共 {len(bd_rows)} 组）")
    buf.extend(leaderboard_table(bd_rows))

    # ② 达标组
    section_title(buf, f"② 达标组筛选（MDD < {min_mdd:.2f} 且 CAGR > {min_cagr:.2f}）")
    buf.extend(qualifying_block(bd_rows, min_mdd, min_cagr))

    # ③ 边际分析
    section_title(buf, "③ 边际分析（固定其他参数，单参数变化对 MDD/CAGR 的影响）")
    buf.extend(marginal_analysis(bd_rows))

    buf.append("")
    buf.append("=" * 78)
    buf.append("注：MDD/CAGR/换手/胜率取自 leaderboard.jsonl 的同名字段（字符串小数，"
               "本脚本转 float）。")
    buf.append("    边际分析为**均值口径**（同参数取值下全部实验求平均），"
               "若网格未跑完则结论会随完成组数变化。")
    buf.append("=" * 78)
    return buf


def main() -> int:
    ap = argparse.ArgumentParser(
        description="宽度参数网格（bd*）结果分析（随时可跑，含中间态）")
    ap.add_argument("--min-mdd", type=float, default=0.35,
                    help="达标 MDD 上限（默认 0.35 = 35%%）")
    ap.add_argument("--min-cagr", type=float, default=0.0,
                    help="达标 CAGR 下限（默认 0.0）")
    ap.add_argument("--out", type=str, default=str(OUT_PATH),
                    help="输出文件路径（默认 experiments/lab/_logs/grid_analysis.txt）")
    args = ap.parse_args()

    all_rows = load_leaderboard(LB_PATH)
    bd_rows = filter_bd(all_rows)

    report_lines = build_report(bd_rows, all_rows, args.min_mdd, args.min_cagr)
    report = "\n".join(report_lines)

    # ① stdout
    print(report)

    # ② 落盘
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report + "\n", encoding="utf-8")
    print(f"\n[analyze_breadth_grid] 已写入 {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
