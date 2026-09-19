#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""选股技巧测量探针：池内「股息率降序取前5」是否有选股 alpha（研究用）。

策略选股规则（_select_stocks）：dv≥3% → dv降序 → 前50候选 → 取前5、市值加权。
问题：这个「股息率最高前5」规则在池内是否跑赢池均值/随机5只？

测量（C3 年度池，2015-2024）：
- 每日截面：池成员的 dividend_yield 排序
- 前瞻收益：fwd_20d = close[t+20]/close[t]-1（raw价；除息修正）
- 指标①：Spearman IC = corr(dv_rank, fwd_ret_rank) 逐日 → 均值/t值
- 指标②：top5-by-dv 组合前瞻20d收益 − 池均值（逐日，等权）
- 指标③：top5 − 随机5只（1000次蒙特卡洛均值）≈ top5−池均值（大样本收敛）
口径登记：用 c3_universe 的 dividend_yield 字段（引擎同源）；
除息日用 (close×fac+cash)/prev−1 修正 fwd 收益；停牌/无bar日跳过。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
POOL = ROOT / 'data/c3_pool/pool_yearly.parquet'
UNI = ROOT / 'data/c3_universe'
FWD = 20


def exdiv_map(sym):
    f = UNI / 'exdiv' / f'{sym}.parquet'
    if not f.exists():
        return {}
    e = pd.read_parquet(f)
    dc = 'ex_date' if 'ex_date' in e.columns else 'date'
    e['d'] = pd.to_datetime(e[dc]).dt.date
    return dict(zip(e['d'], zip(e['factor'], e['cash_dividend'])))


def year_frame(year, symbols):
    """返回 (dv_df, ret_df)：date×symbol 的股息率与全收益日收益矩阵。"""
    dvs, rets = {}, {}
    for s in symbols:
        f = UNI / s / f'{year}.parquet'
        if not f.exists():
            continue
        b = pd.read_parquet(
            f, columns=['date', 'close', 'dividend_yield']).sort_values('date')
        b = b[b['close'] > 0]
        ex = exdiv_map(s)
        rr = {}
        prev = None
        for _, r in b.iterrows():
            d = r['date']
            d = d.date() if hasattr(d, 'date') else pd.to_datetime(d).date()
            if prev is not None:
                fac, cdv = ex.get(d, (1.0, 0.0))
                rr[d] = (r['close'] * fac + cdv) / prev[1] - 1.0
            prev = (d, r['close'])
            dvs.setdefault(d, {})[s] = r['dividend_yield']
        for d, v in rr.items():
            rets.setdefault(d, {})[s] = v
    # dict[date][symbol] → DataFrame 列=date 行=symbol，需转置成 date×symbol
    return pd.DataFrame(dvs).T.sort_index(), pd.DataFrame(rets).T.sort_index()


def main():
    pool = pd.read_parquet(POOL)
    all_ic, all_spread = [], []
    per_year = []
    for _, prow in pool.sort_values('year').iterrows():
        year = int(prow['year'])
        dv, ret = year_frame(year, list(prow['symbols']))
        if dv.empty:
            continue
        # 前瞻20日收益：fwd_t = prod(1+r_{t+1..t+20})-1（用cumprod近似）
        cum = (1 + ret.fillna(0)).cumprod()
        fwd = cum.shift(-FWD) / cum - 1.0
        common = dv.index.intersection(fwd.dropna(how='all').index)
        dv, fwd = dv.loc[common], fwd.loc[common]
        ics, spreads = [], []
        for d in common:
            row_dv = dv.loc[d].dropna()
            row_f = fwd.loc[d]
            both = row_dv.index.intersection(row_f.dropna().index)
            if len(both) < 15:
                continue
            x = row_dv.loc[both]
            y = row_f.loc[both]
            ic = x.rank().corr(y.rank(), method='pearson')
            ics.append(ic)
            rk = x.rank(ascending=False)          # dv 降序名次（1=最高息）
            bands = {'top5': rk <= 5, 'r6_15': (rk > 5) & (rk <= 15),
                     'r16_40': (rk > 15) & (rk <= 40), 'rest': rk > 40}
            spreads.append({k: y[b.index[b]].mean() - y.mean()
                            for k, b in bands.items() if b.any()})
        ics = pd.Series(ics).dropna()
        sdf = pd.DataFrame(spreads).dropna()
        all_ic.append(ics)
        all_spread.append(sdf)
        if len(ics):
            per_year.append((year, len(dv.columns),
                             ics.mean(), ics.mean() / (ics.std() / np.sqrt(len(ics))),
                             sdf.mean().to_dict()))
    ic = pd.concat(all_ic)
    sp = pd.concat(all_spread)
    print(f'池内股息率排序选股技巧（fwd={FWD}d, 2015-2024）')
    print(f'  IC均值 {ic.mean():+.4f}  t={ic.mean()/(ic.std()/np.sqrt(len(ic))):+.1f}'
          f'  ICIR={ic.mean()/ic.std():+.3f}  IC>0占比 {(ic>0).mean():.0%}')
    print('  各dv名次带 spread vs 池均值（20d, bp；年化≈×242/20）：')
    for c in sp.columns:
        m_, t_ = sp[c].mean(), sp[c].mean() / (sp[c].std() / np.sqrt(len(sp[c])))
        print(f'    {c:7s} {m_*10000:+8.1f}bp  t={t_:+.1f}  年化≈{m_*242/FWD:+.2%}')
    print('  分年：')
    for y, n, m, t, s_ in per_year:
        print(f'    {y}: 池{n:3d}  IC{m:+.4f} t={t:+.1f}  top5={s_.get("top5",0)*10000:+.0f}bp r6_15={s_.get("r6_15",0)*10000:+.0f}bp')


if __name__ == '__main__':
    sys.exit(main())
