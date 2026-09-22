#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e58 宽篮调仓频率实算诊断：top500 等权篮，月调 vs 季调，含成本。

用 e58_horizon/scores.parquet + e27 面板日收益直接算组合收益，
成本=单边换手×（买 0.1%+卖 0.13%≈0.23%)/2 近似——量级诊断非精确撮合。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.lab.e23_shadow_screen import daily_returns  # noqa: E402

OUT = ROOT / 'experiments/lab' / 'e58_horizon'
COST_SIDE = 0.00115          # 单边综合成本（佣金+印花+滑点）约 11.5bp


def basket_bt(S: pd.DataFrame, rets: pd.DataFrame,
              freq_months: int, topn: int = 500) -> dict:
    days = sorted(rets.index)
    sig = sorted(S.sig_date.unique())
    holds: dict[pd.Timestamp, set] = {}
    for d in sig[::freq_months]:
        g = S[S.sig_date == d]
        holds[d] = set(g.nlargest(min(topn, len(g)), 'score').ts_code)
    hold_days = sorted(holds)
    nav, turn, prev = [1.0], [], set()
    series = []
    cur = None
    for i, d in enumerate(days):
        # 信号日次日换仓（与 e58 标签 T+1 起算同口径）
        while hold_days and hold_days[0] < d:
            new = holds[hold_days.pop(0)]
            if cur is not None:
                ch = len(new - cur) / max(1, len(new))
                turn.append(ch * 2)          # 双边换手近似=换名比例*2
                nav.append(nav[-1] * (1 - ch * 2 * COST_SIDE))
            cur = new
        if cur is None:
            continue
        r = rets.loc[d].reindex(list(cur)).dropna()
        if len(r) < 50:
            continue
        nav.append(nav[-1] * (1 + r.mean()))
        series.append((d, nav[-1]))
    s = pd.Series(dict(series)).sort_index()
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    dd = (s / s.cummax() - 1).min()
    return {'cagr': float(s.iloc[-1] ** (1 / yrs) - 1), 'mdd': float(-dd),
            'ann_turnover': float(np.mean(turn) * 12 / freq_months)
            if turn else None, 'n_names': len(cur or []),
            'end': float(s.iloc[-1])}


def main() -> int:
    S = pd.read_parquet(OUT / 'scores.parquet')
    close_w = pd.read_parquet(ROOT / 'experiments/lab/e27/panel_close.parquet')
    rets = daily_returns(close_w)
    res = {}
    for f, tag in ((1, 'monthly'), (3, 'quarterly'), (6, 'semiannual')):
        res[tag] = basket_bt(S, rets, f)
        print(tag, {k: round(v, 4) if isinstance(v, float) else v
                    for k, v in res[tag].items()})
    (OUT / 'basket_bt.json').write_text(
        json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
