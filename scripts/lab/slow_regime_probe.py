#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""慢变量体制对冲影子：日频宽度失败的是「择时」还是「粒度」？（研究用）

对照（同池同口径，PIT T-1 信号）：
  A  满仓裸持（基准）
  B  日频宽度系数（def.15/mid.5/atk1.0）——已判负
  D1 慢体制全清：沪深300 收盘<MA200 → 空仓+GC001，≥MA200 → 满仓
  D2 慢体制半仓：同上但 defense 档保留 0.4（尾部对冲而非清仓）
信号均为 T-1 日指数收盘与 MA200 比较（MA 本身慢变量，天然降噪）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from pool_bh_probe import sym_ret, load_gc001, POOL, UNI

IDX = 'sh.000300'
MA_N = 200
DEF_KEEP = 0.4


def load_index_ma():
    frames = []
    for f in sorted((UNI / IDX).glob('*.parquet')):
        frames.append(pd.read_parquet(f, columns=['date', 'close']))
    px = pd.concat(frames).sort_values('date')
    px['d'] = pd.to_datetime(px['date']).dt.date
    px['ma'] = px['close'].rolling(MA_N).mean()
    return pd.Series((px['close'] > px['ma']).values,
                     index=px['d'])


def main():
    pool = pd.read_parquet(POOL)
    regime = load_index_ma()
    cash = load_gc001()
    rdays = sorted(regime.index)

    import bisect
    def prev_regime(d):
        i = bisect.bisect_left(rdays, d)
        return bool(regime.iloc[i - 1]) if i > 0 else False

    navA = navD1 = navD2 = 1.0
    curA, curD1, curD2 = [], [], []
    for _, prow in pool.sort_values('year').iterrows():
        year = int(prow['year'])
        mats = {s: sym_ret(s, year) for s in prow['symbols']}
        df = pd.DataFrame(mats).sort_index()
        w = pd.Series(1.0 / df.shape[1], index=df.columns)
        for d, r in df.iterrows():
            port_r = float((w * r.fillna(0)).sum())   # 先算收益（漂移前权重）
            wv = w * (1 + np.nan_to_num(r.values))
            w = wv / wv.sum()                          # 再漂移归一
            cr = float(cash.get(d, 0.0))
            up = prev_regime(d)          # T-1 信号
            navA *= 1 + port_r
            navD1 *= 1 + (port_r if up else cr)
            navD2 *= 1 + (port_r if up else DEF_KEEP * port_r + (1 - DEF_KEEP) * cr)
            curA.append(navA); curD1.append(navD1); curD2.append(navD2)

    def stats(cur):
        s = pd.Series(cur)
        yrs = len(s) / 242
        return ((s.iloc[-1]) ** (1 / yrs) - 1, (1 - s / s.cummax()).max())

    for n, c in [('A 满仓裸持', curA),
                 (f'D1 沪深300<MA{MA_N}清仓+GC001', curD1),
                 (f'D2 同上但保留{DEF_KEEP}', curD2)]:
        c_, m_ = stats(c)
        print(f'{n:32s}: CAGR {c_:.2%}  MDD {m_:.2%}')
    print('参照：B日频宽度系数 6.38%/20.3% | e8b 实测 8.58%/17.4%')


if __name__ == '__main__':
    sys.exit(main())
