"""e29 龙虎榜事件族影子筛选（事件研究法）。

预登记：docs/E29_LHB_MONEYFLOW_PREREG.md（冻结后方可 --run）。
形态：稀疏事件 → 事件窗 CAR 评价（不套月频五分位）。

事件：stock_lhb_detail_em 逐日上榜记录（data/lhb/{YYYYMMDD}.parquet）。
机构臂：stock_lhb_jgmmtj_em（data/lhb_jgmmtj/{YYYYMM}.parquet）机构买入净额>0。
交易日口径：上榜日 T0 收盘后披露 → 首个可交易日 T1=次一交易日收盘起算；
CAR(h) = r_stock(T1→T1+h) − median_allA(T1→T1+h)，h∈{1,5,10,20}。
对照组 L6=同日未上榜全 A 有效股中位（构造上≈0，披露用）。

判定：簇稳健 t（按事件日聚合再 t）≥2.6 且 +20d CAR>0 且各窗方向一致
且 n≥300 → 「强」；pooled t 一并披露。
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
sys.path.insert(0, str(ROOT))

from scripts.lab.e23_shadow_screen import (  # noqa: E402
    MIN_LISTED_DAYS, daily_returns,
)
from scripts.lab import e27_insider_screen as e27  # noqa: E402

LHB_DIR = ROOT / 'data' / 'lhb'
JG_DIR = ROOT / 'data' / 'lhb_jgmmtj'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e29'

HORIZONS = (1, 5, 10, 20)
REPEAT_WIN = 10          # L4 连续上榜回看窗（交易日）
MIN_EVENTS = 300         # 样本量下限
STRONG_T = 2.6           # Bonferroni 簇稳健 t
YEAR_CONS_MIN = 0.60     # 年份同号一致性

REASON_UP = ('涨幅', '涨幅偏离', '连续三个交易日内，涨幅')
REASON_DOWN = ('跌幅',)


def _note(m: str) -> None:
    print(f"[note] {m}")


def load_lhb() -> pd.DataFrame:
    frames = []
    for f in sorted(LHB_DIR.glob('*.parquet')):
        if f.stem.startswith('_'):
            continue
        d = pd.read_parquet(f)
        d['trade_date'] = pd.to_datetime(d['trade_date'], format='%Y-%m-%d')
        frames.append(d[['trade_date', 'ts_code', 'net_buy', 'buy_amount',
                         'sell_amount', 'reason', 'pct_change']])
    df = pd.concat(frames, ignore_index=True)
    # 同股同公告日多条原因 → 合并为单事件（net_buy 求和、reason 拼接）
    df = (df.groupby(['trade_date', 'ts_code'])
            .agg(net_buy=('net_buy', 'sum'), reason=('reason', '|'.join))
            .reset_index())
    return df


def load_jgmmtj() -> pd.DataFrame:
    frames = []
    for f in sorted(JG_DIR.glob('*.parquet')):
        if f.stem.startswith('_'):
            continue
        d = pd.read_parquet(f)
        d.columns = [str(c) for c in d.columns]
        d = d.rename(columns={'代码': 'code_raw', '上榜日期': 'trade_date',
                              '机构买入净额': 'jg_net', '买方机构数': 'jg_buyers',
                              '卖方机构数': 'jg_sellers',
                              '机构净买额占总成交额比': 'jg_ratio',
                              '流通市值': 'cmv_yi'})
        d['trade_date'] = pd.to_datetime(d['trade_date'].astype(str))
        cr = d['code_raw'].astype(str).str.extract(r'(\d{6})')[0]
        d = d[cr.notna()].copy()
        cr = cr.dropna()
        exch = np.where(cr.str.startswith('6'), 'SH',
                        np.where(cr.str[0].isin(['4', '8', '9']), 'BJ', 'SZ'))
        d['ts_code'] = cr + '.' + exch
        frames.append(d[['trade_date', 'ts_code', 'jg_net', 'jg_buyers',
                         'jg_sellers', 'jg_ratio', 'cmv_yi']])
    df = pd.concat(frames, ignore_index=True)
    df = (df.groupby(['trade_date', 'ts_code'])
            .agg(jg_net=('jg_net', 'sum'), jg_buyers=('jg_buyers', 'sum'),
                 jg_sellers=('jg_sellers', 'sum'), jg_ratio=('jg_ratio', 'sum'))
            .reset_index())
    return df


def daily_median_logret(r: pd.DataFrame, valid: pd.DataFrame) -> pd.Series:
    """逐日全 A 有效股中位日收益的 log1p 序列（对照基线，预算一次）。"""
    return np.log1p(r.where(valid).median(axis=1).fillna(0.0))


def windowed_excess(r: pd.DataFrame, med_log: pd.Series,
                    T1: pd.Timestamp, code: str, h: int) -> float:
    """CAR(h) = Π(1+r_stock)−1 − Π(1+r_med)−1 over (T1, T1+h]。个股缺窗→NaN。"""
    idx = r.index
    i = idx.get_loc(T1)
    if i + h >= len(idx):
        return np.nan
    wnd = r.iloc[i + 1:i + h + 1]
    if code not in wnd.columns or wnd[code].isna().all():
        return np.nan
    stock = np.expm1(np.log1p(wnd[code].fillna(0.0)).sum()) - 1.0
    med = np.expm1(med_log.iloc[i + 1:i + h + 1].sum()) - 1.0
    return stock - med


def car_table(events: pd.DataFrame, r: pd.DataFrame, valid: pd.DataFrame,
              days_idx: pd.DatetimeIndex,
              med_log: pd.Series | None = None) -> pd.DataFrame:
    """逐事件 CAR(h)。events 需含 trade_date/ts_code。无后续窗的事件剔除。"""
    if med_log is None:
        med_log = daily_median_logret(r, valid)
    nxt = {d: days_idx[i + 1] for i, d in enumerate(days_idx[:-1])}
    rows = []
    for ev in events.itertuples():
        T0 = ev.trade_date
        T1 = nxt.get(T0)
        if T1 is None:
            continue
        rec = {'T0': T0, 'T1': T1, 'ts_code': ev.ts_code}
        ok = False
        for h in HORIZONS:
            v = windowed_excess(r, med_log, T1, ev.ts_code, h)
            rec[f'car{h}'] = v
            if not np.isnan(v):
                ok = True
        if ok:
            rows.append(rec)
    return pd.DataFrame(rows)


def arm_stats(car: pd.DataFrame, label: str) -> dict:
    """簇稳健：按 T0 聚合均值 → t over dates；pooled t 并列。"""
    out = {'arm': label, 'n_events': int(len(car))}
    for h in HORIZONS:
        s = car.dropna(subset=[f'car{h}'])
        if len(s) == 0:
            out[f'h{h}'] = {'n': 0}
            continue
        per_day = s.groupby('T0')[f'car{h}'].mean()
        n_day = len(per_day)
        t_clu = float(per_day.mean() / (per_day.std(ddof=1) / np.sqrt(n_day))) \
            if n_day > 1 and per_day.std(ddof=1) > 0 else np.nan
        x = s[f'car{h}']
        t_pool = float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) \
            if len(x) > 1 and x.std(ddof=1) > 0 else np.nan
        yr = s.groupby(s['T0'].dt.year)[f'car{h}'].mean()
        cons = float((yr > 0).mean()) if len(yr) else np.nan
        med_sign = float((x > 0).mean())
        out[f'h{h}'] = {'n': int(len(s)), 'n_days': n_day,
                        'car_mean': float(x.mean()), 'car_med': float(x.median()),
                        't_cluster': t_clu, 't_pool': t_pool,
                        'year_cons': cons, 'sign_ratio': med_sign}
    return out


def verdict(arm: dict) -> str:
    h20 = arm.get('h20', {})
    n = arm.get('n_events', 0)
    if n < MIN_EVENTS:
        return 'INCONCLUSIVE(样本不足)'
    t = h20.get('t_cluster', np.nan)
    if np.isnan(t):
        return 'INCONCLUSIVE'
    dirs = [arm.get(f'h{h}', {}).get('car_mean', np.nan) for h in HORIZONS]
    dirs = [d for d in dirs if not np.isnan(d)]
    same_dir = len(dirs) > 0 and all(d > 0 for d in dirs) or all(d < 0 for d in dirs)
    if t >= STRONG_T and h20.get('car_mean', 0) > 0 and same_dir and \
            h20.get('year_cons', 0) >= YEAR_CONS_MIN:
        return '强'
    if abs(t) >= 2.0:
        return '弱'
    return '负'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true', help='冻结后才允许运行')
    ap.add_argument('--limit-days', type=int, default=None)
    args = ap.parse_args()
    if not args.run:
        print("⛔ 预登记未冻结；--run 前须 docs/E29_LHB_MONEYFLOW_PREREG.md 冻结")
        return 2

    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suf = f"_lim{args.limit_days}" if args.limit_days else ""
    if not (e27.OUT_DIR / f'panel_close{suf}.parquet').exists():
        _note("build panel (close/circ_mv via e27 loader)")
        e27.build_panel(args.limit_days)
    P = {'close': pd.read_parquet(e27.OUT_DIR / f'panel_close{suf}.parquet'),
         'circ_mv': pd.read_parquet(e27.OUT_DIR / f'panel_circ_mv{suf}.parquet')}
    close_w = P['close']
    r = daily_returns(close_w)
    listed_days = (~close_w.isna()).cumsum()
    valid = listed_days.ge(MIN_LISTED_DAYS)
    _note(f"panel close {close_w.shape}; trading days={len(close_w)}")

    lhb = load_lhb()
    jg = load_jgmmtj()
    _note(f"lhb events={len(lhb)}  jgmmtj rows={len(jg)}")
    lhb = lhb.merge(jg[['trade_date', 'ts_code', 'jg_net']],
                    on=['trade_date', 'ts_code'], how='left')

    # 只保留面板内交易日 & 面板内股票
    day_set = set(r.index)
    col_set = set(r.columns)
    lhb = lhb[lhb['trade_date'].isin(day_set) & lhb['ts_code'].isin(col_set)]
    _note(f"panel-matched lhb events={len(lhb)}")

    days_idx = r.index
    cmv_w = P['circ_mv']

    # ---------- arms ----------
    arms: dict[str, pd.DataFrame] = {}
    arms['L1'] = lhb[lhb['jg_net'] > 0]                       # 机构席位净买
    arms['L5_up'] = lhb[lhb['reason'].str.contains('|'.join(REASON_UP), na=False)]
    arms['L5_dn'] = lhb[lhb['reason'].str.contains('|'.join(REASON_DOWN), na=False)]
    # L4: 近10日≥2次上榜 vs 首次
    lhb_s = lhb.sort_values('trade_date')
    dates = list(days_idx)
    dpos = {d: i for i, d in enumerate(dates)}
    codes_by_day: dict = {}
    for ev in lhb_s.itertuples():
        codes_by_day.setdefault(ev.trade_date, []).append(ev.ts_code)
    seen: dict[str, list[int]] = {}
    rep_flag = []
    for ev in lhb_s.itertuples():
        p = dpos[ev.trade_date]
        hist = seen.get(ev.ts_code, [])
        cnt = sum(1 for q in hist if p - q <= REPEAT_WIN)
        rep_flag.append(cnt >= 1)
        hist.append(p)
        seen[ev.ts_code] = hist
    lhb_s = lhb_s.assign(repeat=rep_flag)
    arms['L4_rep'] = lhb_s[lhb_s['repeat']]
    arms['L4_first'] = lhb_s[~lhb_s['repeat']]
    # L3 游资臂：上榜但非机构主导（jg_net<=0 或缺失）且净买>0
    arms['L3_desk'] = lhb[(lhb['jg_net'].fillna(0) <= 0) & (lhb['net_buy'] > 0)]

    results: dict = {'meta': {'panel_days': len(days_idx), 'horizons': HORIZONS,
                              'min_events': MIN_EVENTS, 'strong_t': STRONG_T}}

    for name, ev in arms.items():
        car = car_table(ev[['trade_date', 'ts_code']], r, valid, days_idx)
        st = arm_stats(car, name)
        st['verdict'] = verdict(st)
        results[name] = st
        _note(f"{name}: n={st['n_events']} verdict={st['verdict']} "
              f"h20 t_clu={st.get('h20', {}).get('t_cluster')}")

    # L2 强度分层：net_buy / circ_mv(T0) 三分位
    l2 = lhb.copy()
    cmv_by = {d: cmv_w.loc[d] for d in l2['trade_date'].unique() if d in cmv_w.index}
    def _cmv(ev):
        s = cmv_by.get(ev.trade_date)
        return np.nan if s is None else s.get(ev.ts_code, np.nan)
    cmv_vals = [_cmv(ev) for ev in l2.itertuples()]
    l2['intensity'] = [ev.net_buy / (c * 1e4) if c and c > 0 else np.nan
                       for ev, c in zip(l2.itertuples(), cmv_vals)]
    l2 = l2.dropna(subset=['intensity'])
    if len(l2) >= 3 * MIN_EVENTS:
        try:
            l2['terc'] = pd.qcut(l2['intensity'], 3, labels=['lo', 'mid', 'hi'])
        except ValueError:
            l2['terc'] = None
        l2_stats = {}
        for g in ('lo', 'mid', 'hi'):
            sub = l2[l2['terc'] == g]
            car = car_table(sub[['trade_date', 'ts_code']], r, valid, days_idx)
            l2_stats[g] = arm_stats(car, f'L2_{g}')
            l2_stats[g]['verdict'] = verdict(l2_stats[g])
        means = [l2_stats[g].get('h20', {}).get('car_mean', np.nan) for g in ('lo', 'mid', 'hi')]
        mono = all(not np.isnan(m) for m in means) and means[0] <= means[1] <= means[2]
        top = l2_stats['hi']
        strong = (top['verdict'] == '强' and mono)
        results['L2'] = {'terciles': l2_stats, 'monotone': mono,
                         'verdict': '强' if strong else ('弱' if top['verdict'] != '负' else '负'),
                         'n_events': int(len(l2))}
    else:
        results['L2'] = {'verdict': 'INCONCLUSIVE(样本不足)', 'n_events': int(len(l2))}

    # L6 对照（披露）
    ev_days = set(lhb['trade_date'])
    ctrl_days = sorted(d for d in days_idx if d in ev_days)
    results['L6_control'] = {'note': '同日全A中位基线构造上≈0，仅披露事件天数',
                             'n_days': len(ctrl_days)}

    (OUT_DIR / 'e29_results.json').write_text(
        json.dumps(results, ensure_ascii=False, indent=1, default=str))
    _note(f"done in {time.time()-t0:.0f}s -> {OUT_DIR/'e29_results.json'}")
    for k in ('L1', 'L2', 'L3_desk', 'L4_rep', 'L4_first', 'L5_up', 'L5_dn'):
        v = results.get(k, {})
        print(f"{k:10s} n={v.get('n_events', 0):6d} verdict={v.get('verdict')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
