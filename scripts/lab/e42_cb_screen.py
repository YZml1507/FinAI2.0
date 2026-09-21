#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e42 可转债因子族影子筛选（docs/E42_CB_FACTOR_PREREG.md 冻结口径）。

宇宙 = 当月已上市未摘牌转债（cb_meta listing/delist 判定），
上市 <10 交易日剔除。月末信号 → 次月债券收益。
基线 = 转债宇宙等权月收益（含摘牌券）。
E 时代分段门：2022+ 子窗均值须与全期同号（双低衰减专检）。

用法：.venv/bin/python -m scripts.lab.e42_cb_screen --run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
CB_DIR = ROOT / 'data' / 'cb'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e42'

COST_PER_TURN = 0.0010       # 单边 0.1%（转债零印花保守档）
MIN_LIST_TD = 10             # 上市 <10td 剔除
MIN_DEFINED_N = 5
G4_ABS_FLOOR = 30            # 转债月均宇宙下限（券少，绝对下限 30）
T_STRONG = 2.6
T_WEAK = 2.0
MIN_MONTHS = 48
MOM_WIN = 20
ERA_SPLIT = pd.Timestamp('2022-01-31')

HYP_DEFS = {
    'C1': 'double_low',      # rank(price)+rank(prem) 低好
    'C2': 'prem_conv',       # 转股溢价率 低好
    'C3': 'prem_pure',       # 纯债溢价率 低好
    'C4': 'mom20',           # 20td 动量 高好
}


def _note(m: str) -> None:
    print(f"[note] {m}")


def load_meta() -> pd.DataFrame:
    m = pd.read_parquet(CB_DIR / 'cb_meta.parquet')
    m['list_d'] = pd.to_datetime(m['listing_date'], errors='coerce')
    m['delist_d'] = pd.to_datetime(m['delist_date'], errors='coerce')
    return m


def build_panels() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """close 矩阵 + 转股溢价率 + 纯债溢价率（日期×券）。"""
    closes, prem_c, prem_p = {}, {}, {}
    for f in sorted((CB_DIR / 'cb_daily').glob('*.parquet')):
        sym = f.stem
        d = pd.read_parquet(f, columns=['date', 'close'])
        d['date'] = pd.to_datetime(d['date'])
        closes[sym] = d.set_index('date')['close']
    for f in sorted((CB_DIR / 'cb_premium').glob('*.parquet')):
        sym = f.stem
        d = pd.read_parquet(f)
        d['date'] = pd.to_datetime(d['日期'])
        prem_c[sym] = d.set_index('date')['转股溢价率']
        prem_p[sym] = d.set_index('date')['纯债溢价率']
    close_w = pd.DataFrame(closes).sort_index()
    pc_w = pd.DataFrame(prem_c).reindex(close_w.index)
    pp_w = pd.DataFrame(prem_p).reindex(close_w.index)
    # 券代码统一成 6 位数字（cb_daily 用 sh113011 / sz123xxx）
    close_w.columns = [c[2:] for c in close_w.columns]
    pc_w.columns = [c[2:] for c in pc_w.columns]
    pp_w.columns = [c[2:] for c in pp_w.columns]
    return close_w, pc_w, pp_w


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    ap.add_argument('--hyps', nargs='*', default=list(HYP_DEFS))
    args = ap.parse_args()
    if not args.run:
        print("⛔ --run required (prereg frozen 2026-09-21)")
        return 2

    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    meta = load_meta()
    close_w, pc_w, pp_w = build_panels()
    _note(f"panel close {close_w.shape}, bonds={close_w.shape[1]}")
    close_w.to_parquet(OUT_DIR / 'panel_close.parquet')
    pc_w.to_parquet(OUT_DIR / 'panel_prem_conv.parquet')
    pp_w.to_parquet(OUT_DIR / 'panel_prem_pure.parquet')

    idx = close_w.index
    r = close_w.pct_change()
    # 上市天数累积 + 摘牌剔除
    listed_days = (~close_w.isna()).cumsum()
    list_ok = listed_days.ge(MIN_LIST_TD)
    delist_map = {str(s)[2:]: d for s, d in
                  zip(meta['symbol'], meta['delist_d'])}
    delist_ok = pd.DataFrame(True, index=idx, columns=close_w.columns)
    for c in close_w.columns:
        dd = delist_map.get(c)
        if pd.notna(dd):
            delist_ok[c] = idx < dd
    universe = list_ok & delist_ok & close_w.notna()

    mom20 = close_w / close_w.shift(MOM_WIN) - 1
    rank_pc = pc_w.rank(axis=1, pct=True)
    rank_px = close_w.rank(axis=1, pct=True)
    sig_frames = {
        'C1': rank_px + rank_pc,               # 双低（两 rank 均值等效）
        'C2': pc_w,
        'C3': pp_w,
        'C4': mom20,
    }
    low_good = {'C1': True, 'C2': True, 'C3': True, 'C4': False}

    # 月末再平衡点
    mends = idx.to_series().groupby(idx.to_period('M')).last().tolist()
    mends = [T for T in mends if T >= pd.Timestamp('2017-01-01')
             and T <= idx[-2]]
    _note(f"rebalance months: {len(mends)}")

    # 次月收益：T 收盘 → 下一月末收盘（前视收益）
    nxt_map = dict(zip(mends, mends[1:]))
    monthly = []
    for T in mends[:-1]:
        T2 = nxt_map[T]
        fwd = close_w.loc[T2] / close_w.loc[T] - 1
        u = universe.loc[T]
        rows_T = {'T': str(T.date())}
        base_ret = fwd[u & fwd.notna()]
        if len(base_ret) < G4_ABS_FLOOR:
            rows_T['universe_n'] = len(base_ret)
            monthly.append(rows_T)
            continue
        uni_mean = float(base_ret.mean())
        for h in args.hyps:
            sig = sig_frames[h].loc[T]
            good = u & sig.notna() & fwd.notna()
            n = int(good.sum())
            if n < MIN_DEFINED_N:
                continue
            sg, fg = sig[good], fwd[good]
            ranked = sg.sort_values(ascending=low_good[h])
            q = max(1, n // 5)
            top = ranked.index[:q]
            # RankIC（spearman = rank 相关）
            ic = float(sg.rank().corr(fg.rank()))
            spread = float(fg[top].mean() - uni_mean)
            monthly.append({**rows_T, 'hyp': h, 'n': n,
                            'ic': ic, 'excess': spread,
                            'universe_mean': uni_mean,
                            'top_ret': float(fg[top].mean()),
                            '_top': list(top)})
        # 宇宙行（无 hyp）登记覆盖率
        monthly.append({'T': str(T.date()), 'hyp': '_u',
                        'n': int(len(base_ret)),
                        'universe_mean': uni_mean})

    df = pd.DataFrame(monthly)
    df['T'] = pd.to_datetime(df['T'])
    uni = df[df.hyp == '_u'].set_index('T')['universe_mean']
    res = {}
    for h in args.hyps:
        g = df[df.hyp == h].sort_values('T')
        # 换手：逐月 top 集合变化率 → 单边成本扣减
        tops = [set(r['_top']) for _, r in g.iterrows() if r['_top']]
        turns = [len(b - a) / len(b) for a, b in zip(tops, tops[1:])]
        turn = float(np.mean(turns)) if turns else np.nan
        ex_net = g['excess'] - (COST_PER_TURN * 2 * turn
                                if not np.isnan(turn) else 0)
        sp = ex_net.dropna()
        M = len(sp)
        sd = sp.std(ddof=1)
        t_stat = float(sp.mean() / (sd / np.sqrt(M))) \
            if M > 1 and sd > 0 else (np.inf if M and sp.mean() > 0
                                      else (-np.inf if M else np.nan))
        gy = g.assign(y=g['T'].dt.year)
        year_cons = float((gy.groupby('y').apply(
            lambda x: (x['excess'] - COST_PER_TURN * 2 * (turn or 0)).mean()
            > 0)).mean()) if len(gy) else np.nan
        pre = sp[g['T'] < ERA_SPLIT]
        post = sp[g['T'] >= ERA_SPLIT]
        era_ok = (len(post) == 0 or
                  np.sign(post.mean()) == np.sign(sp.mean()))
        ic_s = g['ic'].dropna()
        ic_t = float(ic_s.mean() / (ic_s.std(ddof=1) / np.sqrt(len(ic_s)))) \
            if len(ic_s) > 1 and ic_s.std(ddof=1) > 0 else np.nan
        if M < MIN_MONTHS:
            grade = 'INCONCLUSIVE'
        elif t_stat >= T_STRONG and year_cons >= 0.60 and era_ok:
            grade = '强'
        elif t_stat >= T_WEAK and year_cons >= 0.60 and era_ok:
            grade = '弱'
        elif t_stat >= T_STRONG and not era_ok:
            grade = '衰减(2022+反号)'
        else:
            grade = '负'
        res[h] = {'M': M, 't_spread_net': t_stat,
                  'mean_excess_gross': float(g['excess'].mean()),
                  'mean_excess_net': float(sp.mean()),
                  'ic_mean': float(ic_s.mean()), 'ic_t': ic_t,
                  'year_cons': year_cons, 'turn_top': turn,
                  'era_pre22': float(pre.mean()) if len(pre) else None,
                  'era_post22': float(post.mean()) if len(post) else None,
                  'era_ok': bool(era_ok), 'grade': grade}
        _note(f"{h}: M={M} t={t_stat:.2f} net={sp.mean():.4f}/月 "
              f"ic_t={ic_t if np.isnan(ic_t) else round(ic_t,2)} "
              f"pre22={res[h]['era_pre22']} post22={res[h]['era_post22']} "
              f"grade={grade}")

    res['_meta'] = {'months': len(mends), 'bonds': int(close_w.shape[1]),
                    'universe_mean_annual': float((1 + uni).prod() **
                                                  (12 / len(uni)) - 1)
                    if len(uni) else None}
    (OUT_DIR / 'e42_results.json').write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=str))
    df.to_parquet(OUT_DIR / 'e42_monthly.parquet')
    _note(f"done {time.time()-t0:.0f}s")
    for h in args.hyps:
        print(f"{h}: grade={res[h]['grade']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
