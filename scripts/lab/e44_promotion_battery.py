#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e44 带宽化（暴露配平斜坡）晋升电池跑批器。

预登记：docs/E44_NEUTRAL_BAND_PREREG.md（冻结 2026-09-21）。
臂阵 mirror e37promo：锚点 verify + ln-off/veto + G-1 留出 +
G-2 阈值扰动（a' 随 attack 重标定）+ G-3 费率×2。
对照基线复用 e37promo 已跑 hard 臂读数。

用法：.venv/bin/python -m scripts.lab.e44_promotion_battery --arm <arm|all>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date as _date
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.lab.run_experiment import run_experiment  # noqa: E402
from scripts.lab.e36_e37_overlay_ab import CHAMPION  # noqa: E402
from scripts.lab.e37_promotion_battery import load_veto  # noqa: E402

DATA_PATH = ROOT / "data" / "dividend_stocks"
OUT_DIR = ROOT / "experiments" / "lab" / "e44"

# 冻结标定解（e44_neutral_calibration.py，calib_end=2020-12-31）
NA_BY_ATTACK = {
    "0.315": Decimal("0.3766914"),
    "0.35": Decimal("0.4620962"),
    "0.385": Decimal("0.5362748"),
}

EXPECTED_VERIFY = {   # champion hard 锚点（leaderboard 权威值）
    "cagr": "0.085814",
    "max_drawdown": "0.1739899329267204508628679309",
    "round_trips": 156,
    "annual_turnover": "4.607400",
}


def _neutral(attack: str = "0.35") -> dict:
    return {"breadth_weight_mode": "linear_neutral",
            "breadth_neutral_attack": NA_BY_ATTACK[attack]}


ARMS = {
    "verify-hard": {"veto": False},
    "ln":     {"veto": False, "extra": _neutral()},
    "ln-veto": {"veto": True, "extra": _neutral()},
    "g1-ln-off":  {"veto": False, "extra": {**_neutral(),
                   "backtest_start": _date(2021, 1, 1)}},
    "g1-ln-veto": {"veto": True, "extra": {**_neutral(),
                   "backtest_start": _date(2021, 1, 1)}},
    "g2-a315-ln-off":  {"veto": False, "extra": {
        **_neutral("0.315"), "breadth_attack_threshold": Decimal("0.315")}},
    "g2-a315-ln-veto": {"veto": True, "extra": {
        **_neutral("0.315"), "breadth_attack_threshold": Decimal("0.315")}},
    "g2-a385-ln-off":  {"veto": False, "extra": {
        **_neutral("0.385"), "breadth_attack_threshold": Decimal("0.385")}},
    "g2-a385-ln-veto": {"veto": True, "extra": {
        **_neutral("0.385"), "breadth_attack_threshold": Decimal("0.385")}},
    "g3-fee2-ln-off":  {"veto": False, "extra": {
        **_neutral(), "fee_multiplier": Decimal("2")}},
    "g3-fee2-ln-veto": {"veto": True, "extra": {
        **_neutral(), "fee_multiplier": Decimal("2")}},
}


def _note(m: str) -> None:
    print(f"[note] {m}", flush=True)


def run_arm(arm: str, veto: dict | None = None) -> dict:
    spec = ARMS[arm]
    ov = dict(CHAMPION)
    ov.update(spec.get("extra", {}))
    if spec["veto"]:
        if veto is None:
            veto = load_veto()
        ov["event_veto_series"] = veto

    name = f"e44-{arm}"
    t0 = time.time()
    summary = run_experiment(name, ov, DATA_PATH)
    res = {"arm": arm,
           "elapsed_min": (time.time() - t0) / 60,
           "summary": {k: str(v) for k, v in summary.items()}}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{arm}_result.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2))
    _note(f"{arm} done: cagr={summary.get('cagr')} "
          f"mdd={summary.get('max_drawdown')} "
          f"rt={summary.get('round_trips')} ({res['elapsed_min']:.1f}min)")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True,
                    choices=list(ARMS) + ["all"])
    args = ap.parse_args()
    arms = list(ARMS) if args.arm == "all" else [args.arm]
    veto = load_veto() if any(ARMS[a]["veto"] for a in arms) else None
    for a in arms:
        run_arm(a, veto)
    return 0


if __name__ == "__main__":
    sys.exit(main())
