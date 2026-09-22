#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e67 e65 残差归因：T+1 开盘价成交漂移成本（向量化分解）。

模型篮（e58_basket_bt 口径，close→close）与引擎成交口径（open→open，T+1 进出场）
的差值 = 可解释模型 48.5% → 引擎 12.87% 残差的第一大分量。

口径：
- 每个 sig_date 取 score top100（等权），持有 reb 个交易日；
- 理想臂：入场 close(sig) → 出场 close(sig+reb)；
- 现实臂：入场 open(sig+1) → 出场 open(sig+reb+1)（T+1 次日开盘，无 bar 顺延/剔除）；
- 两臂均为等权持有组合（不重叠复用样本：sig 间隔恰为 reb 天，天然无重叠）。
输出：experiments/lab/e67/fill_attribution.json + 每臂逐年收益表打印。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SCORES = ROOT / 'experiments/lab/e63/scores_label150x.parquet'
BARS = ROOT / 'data/daily_bars'
OUT = ROOT / 'experiments/lab' / 'e67'
TOPN, REB = 100, 20


def _sym(bs_code: str) -> str:
    return bs_code.split('.')[1]          # 'sz.000063' → '000063'


def load_bars_index() -> dict[str, pd.DataFrame]:
    """每日 bars 索引：sym → DataFrame(date, open, close, tradestatus, volume)。"""
    S = pd.read_parquet(SCORES)
    want = {s for s in S.ts_code.unique()}
    idx: dict[str, pd.DataFrame] = {}
    for d in BARS.iterdir():
        if not d.is_dir() or not d.name.startswith(('sh.', 'sz.')):
            continue                       # exdiv/ 等旁支目录非个股分区
        parts = sorted(d.glob('*.parquet'))
        if not parts:
            continue
        df = pd.concat(
            [pd.read_parquet(p, columns=['date', 'open', 'close',
                                         'tradestatus', 'volume'])
             for p in parts],
            ignore_index=True)
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        ts = _sym(d.name) + ('.SH' if d.name.startswith('sh.') else '.SZ')
        if ts in want:
            idx[ts] = df
    return idx


def arm_returns(idx: dict[str, pd.DataFrame], top: pd.DataFrame,
                use_open: bool) -> pd.Series:
    """等权持有组合逐段收益序列。use_open=True → T+1 开盘进出场。"""
    px = 'open' if use_open else 'close'
    legs: list[tuple[pd.Timestamp, float]] = []
    for sig, g in top.groupby('sig_date'):
        rs = []
        for _, row in g.iterrows():
            df = idx.get(row.ts_code)
            if df is None:
                continue
            pos = df['date'].searchsorted(pd.Timestamp(sig))
            if use_open:
                pos += 1                     # T+1 开盘成交
            if pos >= len(df) - 1:
                continue
            i0 = pos
            i1 = min(pos + REB, len(df) - 1)
            sub = df.iloc[i0:i1 + 1]
            # 停牌/无量 bar 跳过（引擎撮合同口径：无成交）
            sub = sub[(sub['tradestatus'] == '1') & (sub['volume'] > 0)]
            if len(sub) < 2:
                continue
            e, x = float(sub[px].iloc[0]), float(sub[px].iloc[-1])
            if e > 0:
                rs.append(x / e - 1)
        if rs:
            legs.append((pd.Timestamp(sig), float(np.mean(rs))))
    s = pd.Series(dict(legs)).sort_index()
    return s


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    S = pd.read_parquet(SCORES)
    top = (S.sort_values('score', ascending=False)
             .groupby('sig_date').head(TOPN))
    print(f'sig_dates={S.sig_date.nunique()} top rows={len(top)}')
    idx = load_bars_index()
    print(f'bars idx: {len(idx)} syms')
    close_arm = arm_returns(idx, top, use_open=False)
    open_arm = arm_returns(idx, top, use_open=True)
    yrs = (close_arm.index[-1] - close_arm.index[0]).days / 365.25
    res = {
        'n_legs': int(len(close_arm)),
        'ideal_close_to_close': {
            'total_return': float((1 + close_arm).prod() - 1),
            'cagr': float((1 + close_arm).prod() ** (1 / yrs) - 1),
            'mean_leg': float(close_arm.mean()),
        },
        'real_open_to_open_t1': {
            'total_return': float((1 + open_arm).prod() - 1),
            'cagr': float((1 + open_arm).prod() ** (1 / yrs) - 1),
            'mean_leg': float(open_arm.mean()),
        },
        'drift_cost_per_leg_pp': float((close_arm.mean() - open_arm.mean()) * 100),
    }
    (OUT / 'fill_attribution.json').write_text(
        json.dumps(res, ensure_ascii=False, indent=1))
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
