#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反事实探针：C3 年度池「满仓买入持有」等权日调仓全收益净值（研究用，非实验）。

回答一个量化问题：如果池子全程满仓拿着不动（仅年度重选），能赚多少？
对比择时策略 isst-e8b，直接量化「权益 beta 漏接」的机会成本。

口径：
- 每年初用 pool_yearly 该年名单；等权、日度再平衡近似（每日均值收益）
- 全收益：除息日收益 = (close×factor + cash_dividend)/preclose − 1（raw 价+分红）
- 死亡票：bar 中断后自然退出当日均值（不补生存偏差——名单在年初定死，
  年中退市按实际 bar 贡献到最后一天，退市日收益含在内）
- 无费用/税/流动性约束（这是「池子本身值多少」的裸上限，不是可交易策略）
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
POOL = ROOT / 'data/c3_pool/pool_yearly.parquet'
UNI = ROOT / 'data/c3_universe'


def load_sym_year(sym, year):
    f = UNI / sym / f'{year}.parquet'
    if not f.exists():
        return None
    b = pd.read_parquet(f, columns=['date', 'close', 'preclose'])
    b = b[b['close'] > 0]
    return b


def exdiv_map(sym, year):
    f = UNI / 'exdiv' / f'{sym}.parquet'
    if not f.exists():
        return {}
    e = pd.read_parquet(f)
    e['y'] = pd.to_datetime(e['date']).dt.year
    e = e[e['y'] == year]
    return dict(zip(pd.to_datetime(e['date']).dt.date,
                    zip(e['factor'], e['cash_dividend'])))


def sym_ret_series(sym, year):
    b = load_sym_year(sym, year)
    if b is None or b.empty:
        return None
    ex = exdiv_map(sym, year)
    rets = {}
    prev = None
    for _, r in b.sort_values('date').iterrows():
        d = r['date']
        d = d.date() if hasattr(d, 'date') else pd.to_datetime(d).date()
        if prev is None:
            prev = (d, r['close'])
            continue
        fac, cash = ex.get(d, (1.0, 0.0))
        rets[d] = (r['close'] * fac + cash) / prev[1] - 1.0
        prev = (d, r['close'])
    return pd.Series(rets)


def main():
    pool = pd.read_parquet(POOL)
    nav = 1.0
    curve = []
    year_stats = []
    for _, prow in pool.sort_values('year').iterrows():
        year = int(prow['year'])
        syms = list(prow['symbols'])
        mats = []
        for s in syms:
            sr = sym_ret_series(s, year)
            if sr is not None and len(sr) > 5:
                mats.append(sr)
        if not mats:
            continue
        df = pd.concat(mats, axis=1).sort_index()
        port = df.mean(axis=1, skipna=True)
        y0 = nav
        for d, r in port.items():
            nav *= 1.0 + r
            curve.append((d, nav))
        year_stats.append((year, len(mats), nav / y0 - 1.0,
                           float(df.notna().mean(axis=1).mean())))
    s = pd.Series({d: v for d, v in curve})
    days = (curve[-1][0] - curve[0][0]).days
    cagr = nav ** (365.25 / days) - 1
    dd = (s / s.cummax() - 1).min()
    print(f'C3池满仓等权日调仓（全收益, 裸上限无费用）')
    print(f'  区间 {curve[0][0]} ~ {curve[-1][0]}  ({days}d)')
    print(f'  终值NAV {nav:.4f}  CAGR {cagr:.4%}  MDD {-dd:.4%}')
    print(f'  对照 isst-e8b: CAGR 8.58% MDD 17.40%')
    print('  分年:')
    for y, n, r, cov in year_stats:
        print(f'    {y}: 池{n:3d}只 年收益 {r:+.2%} 日均覆盖 {cov:.0%}')


if __name__ == '__main__':
    sys.exit(main())
