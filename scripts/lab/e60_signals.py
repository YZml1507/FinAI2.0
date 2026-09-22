#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e60 一致预期修正动量信号构造——CSMAR 日度一致预期面板 → 月频信号。

数据源: data/consensus/YYYY.parquet (ingest_consensus_dta.py 产物)
口径: 对每 (Symbol, ForecastDate) 取 FY1(当年财年)行的一致预期值,
      月末快照, 信号=相对 N 月前的修正幅度 (winsorize 1%/99%)。

信号:
  s1_eps_rev90: FY1 EPS 90日修正幅度
  s2_np_rev90:  FY1 NetProfit 90日修正幅度
  s3_fy_slope:  FY2/FY1 EPS 比值 (期限结构斜率, 预期成长)
  s4_pe_chg:    FY1 一致预期 PE 90日变化 (负向更好=预期上升快于价格)

用法: e60_signals.py -> data/consensus/sig_monthly.parquet + stdout 摘要
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'data' / 'consensus'


def winsor(s: pd.Series, lo=0.01, hi=0.99) -> pd.Series:
    a, b = s.quantile(lo), s.quantile(hi)
    return s.clip(a, b)


def main() -> int:
    fs = sorted(glob.glob(str(OUT / '2*.parquet')))
    frames = []
    for f in fs:
        d = pd.read_parquet(f, columns=[
            'Symbol', 'ForecastDate', 'ForecastYear', 'EPS',
            'NetProfit', 'PE'])
        frames.append(d)
    d = pd.concat(frames, ignore_index=True)
    d['ForecastDate'] = pd.to_datetime(d['ForecastDate'])
    d['ForecastYear'] = pd.to_datetime(d['ForecastYear'])
    d['fy'] = d['ForecastYear'].dt.year
    d['dy'] = d['ForecastDate'].dt.year
    # FY1 = 预测年度 == 预测日历年; FY2 = +1
    fy1 = d[d.fy == d.dy]
    fy2 = d[d.fy == d.dy + 1]
    # 月末最后快照
    fy1['ym'] = fy1['ForecastDate'].dt.to_period('M')
    fy1m = (fy1.sort_values('ForecastDate')
              .groupby(['Symbol', 'ym'])
              .last()[['EPS', 'NetProfit', 'PE', 'ForecastDate']]
              .reset_index())
    fy2['ym'] = fy2['ForecastDate'].dt.to_period('M')
    fy2m = (fy2.sort_values('ForecastDate')
              .groupby(['Symbol', 'ym'])
              .last()[['EPS']].rename(columns={'EPS': 'EPS2'})
              .reset_index())
    m = fy1m.merge(fy2m, on=['Symbol', 'ym'], how='left')
    m = m.sort_values(['Symbol', 'ym'])
    g = m.groupby('Symbol')
    m['s1_eps_rev90'] = g['EPS'].pct_change(3)
    m['s2_np_rev90'] = g['NetProfit'].pct_change(3)
    m['s3_fy_slope'] = m['EPS2'] / m['EPS'].replace(0, np.nan) - 1
    m['s4_pe_chg'] = -g['PE'].pct_change(3)  # PE 下降=预期上修, 取负转正
    for c in ['s1_eps_rev90', 's2_np_rev90', 's3_fy_slope', 's4_pe_chg']:
        m[c] = m.groupby('ym')[c].transform(winsor)
    m['ts_code'] = m['Symbol'].astype(str).str.zfill(6)
    suf = m['ts_code'].str[0].map(
        lambda c: 'SH' if c in '69' else 'BJ' if c in '48' else 'SZ')
    m['ts_code'] = m['ts_code'] + '.' + suf
    m['date'] = m['ForecastDate']
    sig = m[['date', 'ts_code', 's1_eps_rev90', 's2_np_rev90',
             's3_fy_slope', 's4_pe_chg']].dropna(
        subset=['s1_eps_rev90'])
    sig.to_parquet(OUT / 'sig_monthly.parquet', index=False)
    print('rows', len(sig), 'codes', sig.ts_code.nunique(),
          'span', sig.date.min(), '->', sig.date.max())
    for c in ['s1_eps_rev90', 's2_np_rev90', 's3_fy_slope',
              's4_pe_chg']:
        print(c, 'non-null', sig[c].notna().sum(),
              'mean', round(float(sig[c].mean()), 5))
    return 0


if __name__ == '__main__':
    sys.exit(main())
