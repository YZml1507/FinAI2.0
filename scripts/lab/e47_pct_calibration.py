#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e47 宽度择时百分位域标定（docs/E47_BREADTH_PCT_PREREG.md）。

pct_t = rank(b_t, trailing W 窗口含当日)；前 W−1 日用扩张窗
（min_periods=1 口径：rank within all-so-far）。校准域=≤2020-12-31。

选点规则（冻结）：在 (W,q_d,q_a) 网格内选使
    |E[1{pct≥q_a}] − E[1{b≥0.35}]| + |P(pct<q_d) − P(b<0.25)|
最小的组合——暴露与防御触发频率双双配平 hard 基线。选出单点
冻结后写 pct parquet（全史）供 BREADTH_FILE 注入。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.lab.run_experiment import _load_breadth_series  # noqa: E402

BREADTH = ROOT / 'experiments' / 'lab' / 'market-breadth-a' / 'breadth20_daily.parquet'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e47'
CALIB_END = pd.Timestamp('2020-12-31')

# 冻结网格修订（pre-run 修订，登记于 E47 §五）：
# P(pct≥q_a)≈1−q_a 恒等式使原 q_a≥0.70 网格永远达不到
# hard 的 60.1% 攻击暴露——q_a 域扩展至 0.30–0.50 以配平；
# q_d 相应补 0.27 一档贴近 hard P(defense)=0.265。
GRID_W = (200, 250, 300)
GRID_QD = (0.20, 0.25, 0.27)
GRID_QA = (0.30, 0.35, 0.40, 0.45, 0.50, 0.70, 0.75, 0.80)
HARD_DEF, HARD_ATT = 0.25, 0.35


def rolling_pct(b: pd.Series, w: int) -> pd.Series:
    """trailing rank pct；前段用扩张窗（min_periods=1）。"""
    out = np.empty(len(b))
    vals = b.values
    for i in range(len(vals)):
        lo = max(0, i - w + 1)
        win = vals[lo:i + 1]
        out[i] = float((win <= vals[i]).mean())
    return pd.Series(out, index=b.index)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ser = _load_breadth_series(BREADTH)          # {iso: Decimal}
    b = pd.Series({pd.Timestamp(k): float(v) for k, v in ser.items()})
    b = b.sort_index()
    tr = b[b.index <= CALIB_END]
    e_hard_att = float((tr >= HARD_ATT).mean())
    p_hard_def = float((tr < HARD_DEF).mean())
    print(f"[note] train n={len(tr)} E[attack]={e_hard_att:.4f} "
          f"P[defense]={p_hard_def:.4f}")

    rows = []
    pcts = {w: rolling_pct(b, w) for w in GRID_W}
    for w in GRID_W:
        ptr = pcts[w][b.index <= CALIB_END]
        for qd in GRID_QD:
            for qa in GRID_QA:
                e_att = float((ptr >= qa).mean())
                p_def = float((ptr < qd).mean())
                score = abs(e_att - e_hard_att) + abs(p_def - p_hard_def)
                rows.append({'W': w, 'q_d': qd, 'q_a': qa,
                             'e_att': e_att, 'p_def': p_def,
                             'score': score})
    tab = pd.DataFrame(rows).sort_values('score')
    print(tab.to_string(index=False))
    best = tab.iloc[0]
    sel = {'W': int(best.W), 'q_d': float(best.q_d), 'q_a': float(best.q_a),
           'score': float(best.score), 'e_att': float(best.e_att),
           'p_def': float(best.p_def),
           'e_hard_att': e_hard_att, 'p_hard_def': p_hard_def}
    print(f"[note] selected {sel}")

    pct_full = pcts[sel['W']]
    out = pd.DataFrame({'date': b.index, 'breadth20': pct_full.values})
    out.to_parquet(OUT_DIR / 'breadth_pct_daily.parquet', index=False)
    (OUT_DIR / 'calibration.json').write_text(json.dumps(
        sel, ensure_ascii=False, indent=1))
    print("[note] pct parquet written:", OUT_DIR / 'breadth_pct_daily.parquet')
    return 0


if __name__ == '__main__':
    sys.exit(main())
