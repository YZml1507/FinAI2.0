#!/usr/bin/env python3
"""最终交付一键复现脚本（e21）——锚点 + e20 两臂复跑并与冻结期望逐值比对。

三臂（同一冻结构型 e8b + fix688 数据修复）：
  anchor = isst-e8b-fix688-v2-repro     2015-01-05 ~ 2024-12-31（默认区间）
  cold   = e20-oos-2025-repro           2025-01-05 ~ 2026-09-16
  warm   = e20-oos-warm-repro           2024-07-01 ~ 2026-09-16

期望取值自 experiments/lab/leaderboard.jsonl 权威记录（isst-e8b-fix688-v2 /
e20-oos-2025 / e20-oos-warm），容差 = 0（值级一致才计 PASS）。

用法：
  .venv/bin/python scripts/repro/reproduce_final_delivery.py           # 三臂并行全跑
  .venv/bin/python scripts/repro/reproduce_final_delivery.py --only anchor
  .venv/bin/python scripts/repro/reproduce_final_delivery.py --dry-run # 只打印命令

退出码：任一臂 FAIL / 前置检查不通过 ⇒ 1；全 PASS ⇒ 0。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = ROOT / ".venv" / "bin" / "python"
RUNNER = ROOT / "scripts" / "lab" / "run_experiment.py"
BREADTH = ROOT / "experiments" / "lab" / "market-breadth-a" / "breadth20_daily.parquet"
BREADTH_REBUILDER = ROOT / "scripts" / "lab" / "rebuild_breadth_from_leaderboard.py"
GC001 = ROOT / "data" / "rates" / "gc001_daily.parquet"
DIVDIR = ROOT / "data" / "dividend_stocks"
LEADERBOARD = ROOT / "experiments" / "lab" / "leaderboard.jsonl"

# e8b 冻结构型 + fix688 数据修复（与 isst-e8b-fix688-v2 / e20 两臂逐字相同）
COMMON_SETS = [
    "--set", "use_breadth_timing=True",
    "--set", "use_ma200_timing=False",
    "--set", "breadth_defense_threshold=0.25",
    "--set", "breadth_attack_threshold=0.35",
    "--set", "breadth_mid_cap=0.0",
    "--set", "breadth_ice_confirm_days=1",
    "--set", "breadth_demote_liquidate=True",
    "--set", "cash_yield_series=data/rates/gc001_daily.parquet",
]

# 期望：逐字取 leaderboard 权威记录（str → Decimal 精确比对）
ARMS = {
    "anchor": {
        "name": "isst-e8b-fix688-v2-repro",
        "extra": [],
        "expect": {
            "cagr": "0.085814",
            "max_drawdown": "0.1739899329267204508628679309",
            "round_trips": 156,
            "fees_sum": "22328.60",
            "annual_turnover": "4.607400",
        },
    },
    "cold": {
        "name": "e20-oos-2025-repro",
        "extra": ["--set", "backtest_start=2025-01-05",
                  "--set", "backtest_end=2026-09-16"],
        "expect": {
            "total_return": "-0.1542629806924931423488610688",
            "max_drawdown": "0.1947481109982804239272227391",
            "round_trips": 19,
        },
    },
    "warm": {
        "name": "e20-oos-warm-repro",
        "extra": ["--set", "backtest_start=2024-07-01",
                  "--set", "backtest_end=2026-09-16"],
        "expect": {
            "total_return": "-0.0954462730284487407261186843",
            "max_drawdown": "0.1269027075538264927627722001",
            "round_trips": 28,
        },
    },
}


def _die(msg: str) -> "SystemExit":
    print(f"[FAIL-CLOSED] {msg}")
    return SystemExit(1)


def precheck() -> None:
    """前置检查（fail-closed）：数据恢复 / 宽度序列 / GC001 覆盖。"""
    if not DIVDIR.is_dir() or not any(DIVDIR.iterdir()):
        raise _die(f"数据未恢复：{DIVDIR} 不存在或为空"
                   "（先恢复 Release data-20260920b 数据包）")
    if not BREADTH.exists():
        print(f"[precheck] 宽度序列缺失：{BREADTH} → 调重建脚本")
        if not BREADTH_REBUILDER.exists():
            raise _die(f"宽度重建脚本缺失：{BREADTH_REBUILDER}")
        subprocess.run([str(PY), str(BREADTH_REBUILDER)],
                       cwd=ROOT, check=True)
        if not BREADTH.exists():
            raise _die("宽度重建执行后仍缺 breadth20_daily.parquet")
    if not GC001.exists():
        raise _die(f"GC001 序列缺失：{GC001}")
    import pandas as pd
    mx = str(pd.read_parquet(GC001)["date"].max())[:10]
    if mx < "2026-09-16":
        raise _die(f"GC001 覆盖止于 {mx} < 2026-09-16，e20 区间会 fail-closed"
                   "（先跑 scripts/extend_gc001_series.py）")
    if not LEADERBOARD.exists():
        raise _die(f"leaderboard 缺失：{LEADERBOARD}（期望/产物出处无源）")
    if not RUNNER.exists():
        raise _die(f"实验入口缺失：{RUNNER}")
    print("[precheck] 数据/宽度/GC001/入口 全部就绪")


def run_arm(key: str) -> dict:
    """跑一臂，返回 experiment.json 摘要字典。"""
    arm = ARMS[key]
    name = arm["name"]
    log = ROOT / "experiments" / "lab" / f"{name}.repro.log"
    cmd = [str(PY), str(RUNNER), "--name", name] + COMMON_SETS + arm["extra"]
    print(f"[run] {key}: {' '.join(cmd)}\n      → {log}")
    with open(log, "w") as fh:
        r = subprocess.run(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        tail = log.read_text()[-1500:]
        raise _die(f"{key} 臂失败（exit {r.returncode}），日志尾：\n{tail}")
    exp_json = ROOT / "experiments" / "lab" / name / "experiment.json"
    return json.loads(exp_json.read_text())


def compare(expect: dict, actual: dict) -> list[str]:
    """逐字段容差-0 比对，返回差异行（空 = 全一致）。

    expect 值取自 leaderboard 记录（str/int）；actual 为 experiment.json
    摘要（同键 str）。Decimal(str) 精确相等才判 PASS。
    """
    diffs = []
    for k, want in expect.items():
        got = actual.get(k)
        if got is None:
            diffs.append(f"{k}: 期望 {want}，实际 <缺失>")
            continue
        try:
            ok = Decimal(str(got)) == Decimal(str(want))
        except Exception:
            ok = str(got) == str(want)
        if not ok:
            diffs.append(f"{k}: 期望 {want}，实际 {got}")
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser(description="最终交付复现：锚点+e20 两臂")
    ap.add_argument("--only", choices=list(ARMS), help="只跑指定臂")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    ap.add_argument("--serial", action="store_true", help="串行跑（默认并行）")
    args = ap.parse_args()

    keys = [args.only] if args.only else list(ARMS)
    if args.dry_run:
        for k in keys:
            a = ARMS[k]
            print(f"{k}: .venv/bin/python scripts/lab/run_experiment.py "
                  f"--name {a['name']} {' '.join(COMMON_SETS)} {' '.join(a['extra'])}")
            print(f"    期望: {a['expect']}")
        return 0

    precheck()
    results = {}
    if args.serial or len(keys) == 1:
        for k in keys:
            results[k] = run_arm(k)
    else:
        with ThreadPoolExecutor(max_workers=len(keys)) as ex:
            results = dict(zip(keys, ex.map(run_arm, keys)))

    print("\n" + "=" * 64)
    print("复现比对（容差 0，期望取自 leaderboard 权威记录）")
    print("=" * 64)
    all_ok = True
    for k in keys:
        diffs = compare(ARMS[k]["expect"], results[k])
        status = "PASS" if not diffs else "FAIL"
        all_ok &= not diffs
        print(f"  {k:6s} ({ARMS[k]['name']}): {status}")
        for d in diffs:
            print(f"      Δ {d}")
    print("=" * 64)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
