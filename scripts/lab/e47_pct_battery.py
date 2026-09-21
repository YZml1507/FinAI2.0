#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e47 宽度百分位域晋升电池（docs/E47_BREADTH_PCT_PREREG.md）。

实现：pct 域改造 = runner 侧序列变换（零引擎改动）——
BREADTH_FILE 指向按 (W) 滚动秩变换的 pct parquet，
defense/attack 阈值按 (q_d,q_a) 重解释。

冻结标定（calibration.json, calib_end=2020-12-31）：
    W=200, q_d=0.27, q_a=0.40（score=0.021 双配平）

臂阵：verify-hard / pct-cal / g1-pct-off(2021+) / g2a-W160 /
g2b-q±0.05 / g3-pct-fee2。

用法：.venv/bin/python -m scripts.lab.e47_pct_battery --arm <arm|all>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date as _date
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.lab.run_experiment import run_experiment, _load_breadth_series  # noqa: E402
from scripts.lab.e36_e37_overlay_ab import CHAMPION  # noqa: E402
from scripts.lab.e47_pct_calibration import rolling_pct  # noqa: E402

DATA_PATH = ROOT / "data" / "dividend_stocks"
OUT_DIR = ROOT / "experiments" / "lab" / "e47"
BREADTH_SRC = ROOT / 'experiments' / 'lab' / 'market-breadth-a' / 'breadth20_daily.parquet'

# 冻结标定解
SEL = {"W": 200, "q_d": Decimal("0.27"), "q_a": Decimal("0.40")}

EXPECTED_VERIFY = {
    "cagr": "0.085814",
    "max_drawdown": "0.1739899329267204508628679309",
    "round_trips": 156,
    "fees_sum": "22328.60",
    "annual_turnover": "4.607400",
}


def _pct_file(w: int) -> Path:
    p = OUT_DIR / f'breadth_pct_w{w}.parquet'
    if not p.exists():
        ser = _load_breadth_series(BREADTH_SRC)
        b = pd.Series({pd.Timestamp(k): float(v) for k, v in ser.items()})
        b = b.sort_index()
        pct = rolling_pct(b, w)
        pd.DataFrame({'date': b.index, 'breadth20': pct.values}
                     ).to_parquet(p, index=False)
    return p


def _pct_ov(w: int, q_d: Decimal, q_a: Decimal) -> dict:
    return {"breadth_defense_threshold": q_d,
            "breadth_attack_threshold": q_a,
            "_breadth_file": str(_pct_file(w))}


ARMS = {
    "verify-hard": {},
    "pct-cal": _pct_ov(SEL['W'], SEL['q_d'], SEL['q_a']),
    "g1-pct": {**_pct_ov(SEL['W'], SEL['q_d'], SEL['q_a']),
               "backtest_start": _date(2021, 1, 1)},
    "g2a-w160": _pct_ov(160, SEL['q_d'], SEL['q_a']),
    "g2b-qpm": _pct_ov(SEL['W'], Decimal("0.22"), Decimal("0.45")),
    "g3-pct-fee2": {**_pct_ov(SEL['W'], SEL['q_d'], SEL['q_a']),
                    "fee_multiplier": "2"},
}


def _note(m): print(f"[note] {m}", flush=True)


def run_arm(name: str) -> dict:
    ov = dict(CHAMPION)
    spec = ARMS[name]
    bf = spec.pop("_breadth_file", None)
    for k in ("backtest_start", "backtest_end", "fee_multiplier"):
        if k in spec:
            ov[k] = spec.pop(k)
    ov.update(spec)
    if bf:
        os.environ["BREADTH_FILE"] = bf
    else:
        os.environ.pop("BREADTH_FILE", None)
    _note(f"arm={name} bf={bf or 'default'} start={time.strftime('%H:%M')}")
    t0 = time.time()
    res = run_experiment(f"e47-{name}", ov, DATA_PATH)
    _note(f"arm={name} done {time.time()-t0:.0f}s "
          f"cagr={res.get('cagr')} mdd={res.get('max_drawdown')}")
    (OUT_DIR / f'{name}_result.json').write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=str))
    if name == "verify-hard":
        for k, v in EXPECTED_VERIFY.items():
            actual = str(res.get(k))
            if actual != str(v):
                _note(f"⛔ verify-hard 不复现 {k}: {actual} != {v}")
                return {"_verify_failed": True, "res": res}
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--arm', required=True)
    args = ap.parse_args()
    names = list(ARMS) if args.arm == 'all' else [args.arm]
    out = {}
    for nm in names:
        r = run_arm(nm)
        out[nm] = r
        if r.get("_verify_failed"):
            break
    (OUT_DIR / 'battery_results.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
