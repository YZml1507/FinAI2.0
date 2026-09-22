"""e64 fund-level holdings signals (tushare fund_portfolio snapshot via Kaggle).

Input: data/fund_holdings_fundlvl/fund_portfolio_tushare_20230808.csv
  cols: ts_code(fund), ann_date, end_date(period), symbol(stock), mkv, amount,
        stk_mkv_ratio, stk_float_ratio

PIT: una fila es visible a partir de su ann_date. Para cada stock y periodo
(end_date) se computan agregados sobre filas cuyo ann_date <= fecha de corte;
para simplificar y ser conservadores se usa disponibilidad = max(ann_date)
del par (symbol, end_date) (periodo "completamente divulgado").

Signals (monthly, last fully-disclosed period):
- F1 fhold_n       : # fondos que reportan el stock
- F2 fhold_float   : suma stk_float_ratio (% del flotante en manos de fondos)
- F3 fhold_n_chg   : F1 periodo actual - anterior (Δ cobertura)
- F4 fhold_float_chg: Δ F2
- F5 fhold_new     : # fondos que aparecen por primera vez en el stock en el
                     periodo (entrantes)
- F6 fhold_exit    : # fondos presentes en periodo previo ausentes en el actual

Output: data/fund_holdings_fundlvl/sig_monthly.parquet (date=月末, ts_code, F1..F6)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / 'data' / 'fund_holdings_fundlvl' / 'fund_portfolio_tushare_20230808.csv'
OUT = ROOT / 'data' / 'fund_holdings_fundlvl' / 'sig_monthly.parquet'


def main() -> int:
    d = pd.read_csv(SRC, usecols=['ts_code', 'ann_date', 'end_date',
                                  'symbol', 'mkv', 'stk_float_ratio'])
    d = d.dropna(subset=['ann_date', 'end_date', 'symbol'])
    d['ann_date'] = pd.to_datetime(d['ann_date'], format='%Y%m%d')
    d['end_date'] = pd.to_datetime(d['end_date'], format='%Y%m%d')
    d = d.rename(columns={'ts_code': 'fund'})

    # agregado por stock x periodo
    agg = (d.groupby(['symbol', 'end_date'])
             .agg(n_funds=('fund', 'nunique'),
                  float_sum=('stk_float_ratio', 'sum'),
                  mkv_sum=('mkv', 'sum'),
                  avail=('ann_date', 'max'))
             .reset_index())

    # nuevos fondos por stock: primera vez que un fondo reporta el stock
    first = (d.groupby(['fund', 'symbol'])['end_date'].min().reset_index()
               .groupby(['symbol', 'end_date']).size().rename('n_new'))
    agg = agg.merge(first, on=['symbol', 'end_date'], how='left')
    agg['n_new'] = agg['n_new'].fillna(0)

    # salidas: fondos del periodo previo que no aparecen en el actual
    periods = sorted(d['end_date'].unique())
    pmap = {p: i for i, p in enumerate(periods)}
    sets = {}
    for (sym, p), sub in d.groupby(['symbol', 'end_date']):
        sets[(sym, p)] = set(sub['fund'])
    exits = {}
    for (sym, p), fs in sets.items():
        i = pmap[p]
        prev = periods[i - 1] if i > 0 else None
        exits[(sym, p)] = len(sets.get((sym, prev), set()) - fs) if prev else 0
    agg['n_exit'] = [exits.get((s, p), 0)
                     for s, p in zip(agg['symbol'], agg['end_date'])]

    agg = agg.sort_values(['symbol', 'end_date'])
    g = agg.groupby('symbol')
    agg['n_funds_chg'] = g['n_funds'].diff()
    agg['float_chg'] = g['float_sum'].diff()

    # mensual: ultimo periodo con avail <= fin de mes (merge_asof por symbol)
    month_ends = pd.date_range('2015-01-01', '2023-08-31', freq='ME')
    grid = pd.DataFrame(
        [(s, m) for s in agg['symbol'].unique() for m in month_ends],
        columns=['symbol', 'date'])
    agg_sorted = agg.sort_values('avail')
    sig = pd.merge_asof(
        grid.sort_values('date'), agg_sorted,
        left_on='date', right_on='avail', by='symbol', direction='backward')
    sig = sig.rename(columns={'symbol': 'ts_code'})
    sig = sig.rename(columns={'n_funds': 'F1', 'float_sum': 'F2',
                              'n_funds_chg': 'F3', 'float_chg': 'F4',
                              'n_new': 'F5', 'n_exit': 'F6'})
    sig = sig[['date', 'ts_code', 'F1', 'F2', 'F3', 'F4', 'F5', 'F6']]
    sig = sig.dropna(subset=['F1'])
    sig.to_parquet(OUT)
    print(f'[e64] -> {OUT} rows={len(sig)} stocks={sig.ts_code.nunique()} '
          f'months={sig.date.nunique()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
