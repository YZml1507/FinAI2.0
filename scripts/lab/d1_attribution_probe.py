#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D1 归因拆解探针：C3 池收益对因子组合的回归（研究用，非实验）。

问题（Income Illusions 之问）：我们的股息率池子收益是独立 alpha，
还是只是 value/低波/规模/动量等已知因子的载体？

方法：月末截面打分形成因子组合（全A，daily_basic_alla）：
  MKT = 全A等权日收益
  SMB = 市值最小 20% − 最大 20%
  HML = PB 最低 20% − 最高 20%（仅 pb>0）
  DIV = dv_ttm 最高 20% − 最低 20%（dv_ttm>0 者排序）
  VOL = 60 日收益波动最低 20% − 最高 20%（低波−高波）
  UMD = 60 日累计收益最高 20% − 最低 20%
持有期=次月全月，日频收益=组内等权均值。除息日用现金分红+送转因子修正
（raw close 上分红会假摔）。
回归：C3 池日收益 ~ MKT+SMB+HML+DIV+VOL+UMD，报 beta/t/R²/年化 alpha。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / 'data/daily_basic_alla'
EV = ROOT / 'data/dividend_events_alla'
POOL = ROOT / 'data/c3_pool/pool_yearly.parquet'
UNI = ROOT / 'data/c3_universe'


def build_panel():
    """全A日面板：close/total_mv/pb/dv_ttm。"""
    frames = []
    for f in sorted(DB.glob('*.parquet')):
        d = pd.read_parquet(
            f, columns=['ts_code', 'trade_date', 'close', 'total_mv',
                        'pb', 'dv_ttm'])
        frames.append(d)
    p = pd.concat(frames)
    p['dt'] = pd.to_datetime(p['trade_date']).dt.date
    p = p.drop_duplicates(subset=['dt', 'ts_code'], keep='last')
    return p


def build_exdiv():
    rows = []
    for f in EV.glob('*.parquet'):
        e = pd.read_parquet(f, columns=['ts_code', 'ex_date', 'cash_div',
                                        'stk_bo_rate', 'stk_co_rate'])
        e = e.dropna(subset=['ex_date'])
        rows.append(e)
    e = pd.concat(rows)
    e['ex'] = pd.to_datetime(e['ex_date']).dt.date
    e['fac'] = 1 + e['stk_bo_rate'].fillna(0) + e['stk_co_rate'].fillna(0)
    e = e.drop_duplicates(subset=['ts_code', 'ex'], keep='last')
    return e.set_index(['ts_code', 'ex'])


def c3_pool_daily():
    """C3 池年度成员等权日收益（全收益口径，复用 pool_bh_probe 逻辑）。"""
    pool = pd.read_parquet(POOL)
    out = {}
    for _, prow in pool.iterrows():
        year = int(prow['year'])
        for s in prow['symbols']:
            f = UNI / s / f'{year}.parquet'
            if not f.exists():
                continue
            b = pd.read_parquet(f, columns=['date', 'close'])
            b = b[b['close'] > 0].sort_values('date')
            pc = b['close'].shift(1)
            r = (b['close'] / pc - 1)
            ser = pd.Series(r.values[1:],
                            index=pd.to_datetime(b['date']).dt.date.values[1:])
            out.setdefault(s, []).append(ser)
    df = pd.DataFrame({s: pd.concat(v) for s, v in out.items()})
    return df.mean(axis=1)  # 注意：未调除息（池探针口径修正见备注）


def c3_pool_daily_tr(ex):
    """C3 池日收益（含除息修正=全收益）。"""
    pool = pd.read_parquet(POOL)
    exmap = {}
    for (tc, d), r in ex.iterrows():
        exmap[(tc.split('.')[1] + '.' + ('sh' if tc.endswith('SH') else 'sz'), d)] = \
            (float(r['fac']) if pd.notna(r['fac']) else 1.0,
             float(r['cash_div']) if pd.notna(r['cash_div']) else 0.0)
    out = {}
    for _, prow in pool.iterrows():
        year = int(prow['year'])
        for s in prow['symbols']:
            f = UNI / s / f'{year}.parquet'
            if not f.exists():
                continue
            b = pd.read_parquet(f, columns=['date', 'close'])
            b = b[b['close'] > 0].sort_values('date')
            prev = None
            rets = {}
            for _, r in b.iterrows():
                d = r['date']
                d = d.date() if hasattr(d, 'date') else pd.to_datetime(d).date()
                if prev is not None:
                    fac, cdv = exmap.get((s, d), (1.0, 0.0))
                    rets[d] = (r['close'] * fac + cdv) / prev[1] - 1.0
                prev = (d, r['close'])
            out[s] = pd.Series(rets)
    df = pd.DataFrame(out)
    return df.mean(axis=1)


def main():
    print('加载全A面板…', flush=True)
    p = build_panel()
    print('加载除息…', flush=True)
    ex = build_exdiv()

    # ---- 个股日收益（含除息修正） ----
    print('计算个股日收益…', flush=True)
    close = p.pivot(index='dt', columns='ts_code', values='close').sort_index()
    close = close.astype('float64')
    ret = close.pct_change()
    # 除息修正（向量化）：ex 日 r → (c*fac+cash)/prev_close-1
    ex2 = ex.reset_index()
    ex2['dkey'] = ex2['ex']
    ex2 = ex2.drop_duplicates(subset=['dkey', 'ts_code'], keep='last')
    adj = ex2.pivot(index='dkey', columns='ts_code', values='cash_div')         .reindex(index=ret.index, columns=ret.columns)
    facp = ex2.pivot(index='dkey', columns='ts_code', values='fac')         .reindex(index=ret.index, columns=ret.columns)
    has = adj.notna() | facp.notna()
    r_ex = (close * facp.fillna(1.0) + adj.fillna(0.0)) / close.shift(1) - 1.0
    ret = ret.where(~has, r_ex)

    mv = p.pivot(index='dt', columns='ts_code', values='total_mv')
    pb = p.pivot(index='dt', columns='ts_code', values='pb')
    dvt = p.pivot(index='dt', columns='ts_code', values='dv_ttm')
    vol60 = ret.rolling(60).std()
    mom60 = (close / close.shift(60) - 1.0)

    # ---- 月末形成 + 次月持有 ----
    print('构建因子组合…', flush=True)
    month_ends = ret.groupby(pd.PeriodIndex(
        [pd.Period(d, 'M') for d in ret.index])).apply(lambda x: x.index[-1])
    month_ends = list(month_ends)
    fac_ret = {k: [] for k in ['MKT', 'SMB', 'HML', 'DIV', 'VOL', 'UMD']}
    dates = ret.index.tolist()

    for i, me in enumerate(month_ends[:-1]):
        nxt_lo = month_ends[i]
        nxt_hi = month_ends[i + 1]
        hold = [d for d in dates if nxt_lo < d <= nxt_hi]
        if not hold:
            continue
        def quintile(s, top=True, valid=None):
            v = s.dropna()
            if valid is not None:
                v = v.loc[v.index.intersection(valid)]
            if len(v) < 50:
                return []
            q = v.quantile(0.8 if top else 0.2)
            return (v[v >= q] if top else v[v <= q]).index.tolist()

        alive = close.loc[me].dropna().index
        sma = quintile(mv.loc[me], top=False, valid=alive)
        big = quintile(mv.loc[me], top=True, valid=alive)
        lo_pb = quintile(pb.loc[me][pb.loc[me] > 0], top=False, valid=alive)
        hi_pb = quintile(pb.loc[me][pb.loc[me] > 0], top=True, valid=alive)
        payers = dvt.loc[me][dvt.loc[me] > 0].index
        hi_dv = quintile(dvt.loc[me], top=True, valid=payers & alive)
        lo_dv = quintile(dvt.loc[me], top=False, valid=payers & alive)
        lo_vol = quintile(vol60.loc[me], top=False, valid=alive)
        hi_vol = quintile(vol60.loc[me], top=True, valid=alive)
        hi_mom = quintile(mom60.loc[me], top=True, valid=alive)
        lo_mom = quintile(mom60.loc[me], top=False, valid=alive)

        for d in hold:
            row = ret.loc[d]
            fac_ret['MKT'].append((d, row[alive].mean()))
            def diff(a_, b_):
                a_, b_ = set(a_), set(b_)
                ra = row[[c for c in a_ if c in row.index]].mean()
                rb = row[[c for c in b_ if c in row.index]].mean()
                return (ra - rb) if pd.notna(ra) and pd.notna(rb) else np.nan
            fac_ret['SMB'].append((d, diff(sma, big)))
            fac_ret['HML'].append((d, diff(lo_pb, hi_pb)))
            fac_ret['DIV'].append((d, diff(hi_dv, lo_dv)))
            fac_ret['VOL'].append((d, diff(lo_vol, hi_vol)))
            fac_ret['UMD'].append((d, diff(hi_mom, lo_mom)))

    F = pd.DataFrame({k: pd.Series(dict(v)) for k, v in fac_ret.items()})

    # ---- C3 池收益（全收益口径） ----
    print('C3 池日收益…', flush=True)
    pool_r = c3_pool_daily_tr(ex)

    # ---- 回归 ----
    print('OLS 回归…', flush=True)
    y = pool_r
    X = F[['MKT', 'SMB', 'HML', 'DIV', 'VOL', 'UMD']]
    df = pd.concat([y.rename('y'), X], axis=1).dropna()
    Yv = df['y'].values
    Xv = np.column_stack([np.ones(len(df)), df[X.columns].values])
    beta, res_, rk, sv = np.linalg.lstsq(Xv, Yv, rcond=None)
    resid = Yv - Xv @ beta
    dof = len(df) - Xv.shape[1]
    s2 = resid @ resid / dof
    covb = s2 * np.linalg.inv(Xv.T @ Xv)
    t = beta / np.sqrt(np.diag(covb))
    r2 = 1 - (resid @ resid) / ((Yv - Yv.mean()) @ (Yv - Yv.mean()))
    alpha_ann = beta[0] * 242
    names = ['alpha'] + list(X.columns)
    print(f'\nC3池日收益因子归因（{df.index[0]}~{df.index[-1]}, N={len(df)}）')
    print(f'  R²={r2:.3f}  年化alpha={alpha_ann:+.2%}')
    for n_, b_, t_ in zip(names, beta, t):
        print(f'  {n_:5s} beta={b_:+.4f}  t={t_:+.1f}')
    F.to_parquet('/tmp/d1_factor_returns.parquet')
    print('因子日收益已存 /tmp/d1_factor_returns.parquet')


if __name__ == '__main__':
    sys.exit(main())
