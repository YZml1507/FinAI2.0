#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反事实探针 v2：C3 年度池满仓持有 vs 择时叠加（研究用影子测算，非实验）。

三条净值线回答「钱亏在哪」：
  A) 纯买入持有（年初等权、权重随价漂移、年调仓）——裸池 beta 上限
  B) A + 宽度仓位系数（defense×d / mid×m / attack×1.0，空仓部分吃 GC001）
     ——P-1「满仓核心+择时对冲尾部」倒置架构的影子预估
  C) A + 引擎现行档位（defense=0/mid=0/attack=1，demote 即清）——对照

口径：
- 全收益：除息日 (close×factor+cash_div)/preclose−1；退市票贡献到最后一个 bar
- 年调仓费：卖出侧 印花税0.05%+佣金0.025% ≈ 0.075%×100% 换手/年（双边 ~0.13%）
- 红利税：持仓<1 年部分粗按股息 10% 税档 ≈ 股息率 4%×0.1 ≈ 0.4%/年 固定拖累
- 宽度系数切换费：每次档位变动按 |Δscalar|×0.1% 双边摩擦粗估
- B/C 中的空仓部分按 GC001 日收益率计息（data/rates/gc001_daily.parquet）
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
POOL = ROOT / 'data/c3_pool/pool_yearly.parquet'
UNI = ROOT / 'data/c3_universe'
BREADTH = ROOT / 'experiments/lab/market-breadth-a/breadth20_daily.parquet'
GC001 = ROOT / 'data/rates/gc001_daily.parquet'

DEF, MID, ATK = 0.25, 0.35, 1.0   # 引擎现行阈值（e8b）
DIV_TAX_DRAG = 0.004              # 红利税年化拖累粗估
REBAL_COST = 0.0013               # 年调仓双边摩擦
SHIFT_COST = 0.001                # 宽度档位切换 |Δ|×双边摩擦


def exdiv_map(sym, year):
    f = UNI / 'exdiv' / f'{sym}.parquet'
    if not f.exists():
        return {}
    e = pd.read_parquet(f)
    e['y'] = pd.to_datetime(e['date']).dt.year
    e = e[e['y'] == year]
    return dict(zip(pd.to_datetime(e['date']).dt.date,
                    zip(e['factor'], e['cash_dividend'])))


def sym_ret(sym, year):
    f = UNI / sym / f'{year}.parquet'
    if not f.exists():
        return None
    b = pd.read_parquet(f, columns=['date', 'close'])
    b = b[b['close'] > 0].sort_values('date')
    if len(b) < 6:
        return None
    ex = exdiv_map(sym, year)
    out = {}
    prev = None
    for _, r in b.iterrows():
        d = r['date']
        d = d.date() if hasattr(d, 'date') else pd.to_datetime(d).date()
        if prev is not None:
            fac, cash = ex.get(d, (1.0, 0.0))
            out[d] = (r['close'] * fac + cash) / prev[1] - 1.0
        prev = (d, r['close'])
    return pd.Series(out)


def load_breadth():
    b = pd.read_parquet(BREADTH)
    b.columns = [c.lower() for c in b.columns]
    val = 'breadth20' if 'breadth20' in b.columns else \
        [c for c in b.columns if c != 'date'][0]
    s = pd.Series(b[val].values,
                  index=pd.to_datetime(b['date']).dt.date)
    return s.astype(float)


_BDAYS = None


def _prev_breadth_day(d):
    """信号滞后：返回 d 前一交易日的宽度值（T-1 信号 → T 日仓位）。"""
    global _BDAYS
    if _BDAYS is None:
        _BDAYS = _BREADTH_IDX
    import bisect
    i = bisect.bisect_left(_BDAYS, d)
    return _BDAYS[i - 1] if i > 0 else None


_BREADTH_IDX = []


def load_gc001():
    g = pd.read_parquet(GC001)
    g.columns = [c.lower() for c in g.columns]
    dc = 'date' if 'date' in g.columns else g.columns[0]
    vc = [c for c in g.columns if c != dc][0]
    s = pd.Series(g[vc].values, index=pd.to_datetime(g[dc]).dt.date).astype(float)
    if s.median() > 1:            # 存的是百分数（如 2.05）→ 化日利率
        s = s / 100 / 365.0
    else:                          # 存的是小数年化 → 化日利率
        s = s / 365.0
    return s


def main():
    pool = pd.read_parquet(POOL)
    breadth = load_breadth()
    cash = load_gc001()
    _BREADTH_IDX.clear()
    _BREADTH_IDX.extend(sorted(breadth.index.tolist()))

    navA = navB = navC = 1.0
    curA, curB, curC = [], [], []
    year_rows = []
    prev_sB = prev_sC = None

    for _, prow in pool.sort_values('year').iterrows():
        year = int(prow['year'])
        mats = []
        for s in list(prow['symbols']):
            sr = sym_ret(s, year)
            if sr is not None:
                mats.append(sr)
        if not mats:
            continue
        df = pd.concat(mats, axis=1).sort_index()   # 列=票 行=日 每日收益
        n = df.shape[1]
        w = np.ones(n) / n                          # 年初等权
        y0A, y0B, y0C = navA, navB, navC
        for d, row in df.iterrows():
            r = row.values                          # NaN=当日无bar(停牌/未上市)
            ok = np.isfinite(r)
            # --- A: 买入持有权重漂移（无bar票权重冻结，不参与当日） ---
            tot = w[ok].sum()
            if tot > 0:
                port_r = float((w[ok] * r[ok]).sum() / tot)
            else:
                port_r = 0.0
            navA *= 1.0 + port_r
            w = w * (1.0 + np.nan_to_num(r))        # 权重漂移
            w = w / w.sum()
            # --- 宽度系数（PIT：用 T-1 日信号定 T 日仓位，消除同日共动前视） ---
            b = float(breadth.get(_prev_breadth_day(d), np.nan))
            sB = 1.0 if b >= ATK else (0.5 if b >= DEF else 0.15)  # 倒置:防御≠清零
            sC = 1.0 if b >= ATK else 0.0                        # 现行:mid/def=0
            cr = float(cash.get(d, 0.0))
            navB *= 1.0 + sB * port_r + (1 - sB) * cr
            navC *= 1.0 + sC * port_r + (1 - sC) * cr
            if prev_sB is not None and sB != prev_sB:
                navB *= 1.0 - abs(sB - prev_sB) * SHIFT_COST
            if prev_sC is not None and sC != prev_sC:
                navC *= 1.0 - abs(sC - prev_sC) * SHIFT_COST
            prev_sB, prev_sC = sB, sC
            curA.append((d, navA)); curB.append((d, navB)); curC.append((d, navC))
        # 年调仓费 + 红利税年拖累
        navA *= 1.0 - REBAL_COST - DIV_TAX_DRAG
        navB *= 1.0 - REBAL_COST - DIV_TAX_DRAG
        navC *= 1.0 - REBAL_COST - DIV_TAX_DRAG
        year_rows.append((year, n, navA / y0A - 1, navB / y0B - 1, navC / y0C - 1))

    def stat(cur):
        s = pd.Series({d: v for d, v in cur})
        days = (cur[-1][0] - cur[0][0]).days
        cagr = cur[-1][1] ** (365.25 / days) - 1
        mdd = (s / s.cummax() - 1).min()
        return cagr, -mdd

    cA, mA = stat(curA); cB, mB = stat(curB); cC, mC = stat(curC)
    print('C3池反事实影子测算（全收益口径，含调仓/税/摩擦粗估）')
    print(f'  A 满仓买入持有        : CAGR {cA:.2%}  MDD {mA:.2%}')
    print(f'  B 倒置(defense×0.15/mid×0.5/attack×1.0+GC001): CAGR {cB:.2%}  MDD {mB:.2%}')
    print(f'  C 现行档位(def=0/mid=0/attack=1+GC001)   : CAGR {cC:.2%}  MDD {mC:.2%}')
    print('  对照 isst-e8b 实测   : CAGR 8.58%  MDD 17.40%')
    print('  分年 (A满仓/B倒置/C现行):')
    for y, n, ra, rb, rc in year_rows:
        print(f'    {y}: 池{n:3d}  A {ra:+.1%}  B {rb:+.1%}  C {rc:+.1%}')


if __name__ == '__main__':
    sys.exit(main())
