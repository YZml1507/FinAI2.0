#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e58 ML 截面排序 spike（docs/E58_ML_XSEC_PREREG.md 冻结）。

LightGBM 月频截面排序：特征全 ≤T PIT，标签=fwd20 超额 rank。
walk-forward 年度重训（标签窗 20td embargo）；判负即关 ML 方向。
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.lab.e23_shadow_screen import MIN_LISTED_DAYS, daily_returns  # noqa: E402
from scripts.lab import e27_insider_screen as e27  # noqa: E402
from scripts.lab import e49_analyst_screen as e49  # noqa: E402

OUT_DIR = ROOT / 'experiments' / 'lab' / 'e58'
SCREEN_END = pd.Timestamp('2024-12-31')
MIN_TRAIN_MONTHS = 24
EMBARGO_TD = 20
LGBM_PARAMS = dict(num_leaves=31, learning_rate=0.05, n_estimators=500,
                   min_data_in_leaf=100, feature_fraction=0.8,
                   seed=42, verbose=-1)

PRICE_FEATS = ['ret5', 'ret20', 'ret60', 'ret120', 'vol20', 'max20',
               'amihud20', 'turnover20', 'pe', 'pb', 'dv_ttm',
               'log_circ_mv', 'fin_bal_chg20', 'short_qty_chg20']
EVENT_FEATS = ['ev_letter', 'ev_resumption', 'ev_fc_pos', 'ev_fc_neg',
               'ev_incentive', 'ev_lhb', 'ev_insider_sell',
               'ev_bt_inst_sell', 'ev_reduce', 'ev_frozen',
               'an_rating_dir20', 'an_epsrev20']


def _note(m): print(f"[note] {m}", flush=True)


# ---------- 事件集构造 ----------
def _day_codes(df, date_col='ann_date', code_col='ts_code'):
    """→ dict[date] -> set(codes)。"""
    out: dict[pd.Timestamp, set] = {}
    for d, g in df.groupby(df[date_col]):
        out[pd.Timestamp(d)] = set(g[code_col])
    return out


def build_event_sets(r_index) -> dict[str, dict]:
    """各事件 → {trade_date: set(ts_code)}。"""
    sets: dict[str, dict] = {}
    # 函件（e50 V4 集 = 全部 letter 事件）
    fs = glob.glob(str(ROOT / 'data/letters/*.parquet'))
    if fs:
        l = pd.concat([pd.read_parquet(f, columns=['ann_date', 'ts_code'])
                       for f in fs])
        l['ann_date'] = pd.to_datetime(l['ann_date'])
        l['ts_code'] = l['ts_code'].map(e49._norm_code)
        sets['ev_letter'] = _day_codes(l)
    # 复牌（NaN run 结束日）
    sets['ev_resumption'] = _resumption_sets()
    # 业绩预告方向
    from scripts.lab import e43_forecast_screen as e43
    yg = e43.load_em()
    if yg is not None:
        pos = yg[yg.ftype.isin(e43.POS_TYPES)]
        neg = yg[yg.ftype.isin(e43.NEG_TYPES)]
        sets['ev_fc_pos'] = _day_codes(pos)
        sets['ev_fc_neg'] = _day_codes(neg)
    # 股权激励（S2）
    fs = glob.glob(str(ROOT / 'data/esop_events/股权激励_*.parquet'))
    if fs:
        s = pd.concat([pd.read_parquet(f) for f in fs])
        s = s[~s.title.str.contains(
            '进展公告|实施完成|届满|结果公告|终止|失效|停止实施',
            na=False)]
        s['ann_date'] = pd.to_datetime(s['ann_date'])
        s['ts_code'] = s['ts_code'].map(e49._norm_code)
        sets['ev_incentive'] = _day_codes(s)
    # 龙虎榜任一
    fs = glob.glob(str(ROOT / 'data/lhb/*.parquet'))
    if fs:
        h = pd.concat([pd.read_parquet(f, columns=['trade_date', 'ts_code'])
                       for f in fs])
        h['trade_date'] = pd.to_datetime(h['trade_date'])
        sets['ev_lhb'] = _day_codes(h, 'trade_date')
    # 内部人净卖出（e27 源表）
    f = ROOT / 'data/insider_trade/detail_2015_2024.parquet'
    if f.exists():
        i = pd.read_parquet(f, columns=['DERIVE_SECURITY_CODE',
                                        'CHANGE_DATE', 'CHANGE_SHARES'])
        i = i[pd.to_numeric(i['CHANGE_SHARES'], errors='coerce') < 0]
        i['ann_date'] = pd.to_datetime(i['CHANGE_DATE'], errors='coerce')
        i['ts_code'] = i['DERIVE_SECURITY_CODE'].map(e49._norm_code)
        sets['ev_insider_sell'] = _day_codes(i.dropna(subset=['ann_date']))
    # 大宗卖方机构
    fs = glob.glob(str(ROOT / 'data/block_trade/*.parquet'))
    if fs:
        b = pd.concat([pd.read_parquet(
            f, columns=['trade_date', 'ts_code', 'seller_broker'])
            for f in fs])
        b = b[b['seller_broker'].astype(str).str.contains('机构专用',
                                                         na=False)]
        b['trade_date'] = pd.to_datetime(b['trade_date'])
        sets['ev_bt_inst_sell'] = _day_codes(b, 'trade_date')
    # cninfo 减持计划/司法冻结
    for kw, tag in (('减持计划', 'ev_reduce'), ('司法冻结', 'ev_frozen')):
        fs = glob.glob(str(ROOT / f'data/cninfo_events/{kw}_*.parquet'))
        if fs:
            c = pd.concat([pd.read_parquet(f) for f in fs])
            c['ann_date'] = pd.to_datetime(c['ann_date'])
            c['ts_code'] = c['ts_code'].map(e49._norm_code)
            sets[tag] = _day_codes(c)
    return sets


def _resumption_sets() -> dict:
    """e52 复牌事件：NaN run ≥5td 结束日。"""
    from scripts.lab import e52_resumption_screen as e52
    close_w = pd.read_parquet(e27.OUT_DIR / 'panel_close.parquet')
    ev = e52.resumption_events(close_w, 5, 10000)
    return _day_codes(ev, 'trade_date')


def analyst_feats() -> pd.DataFrame:
    """每股每日 rating_dir / epsrev 事件行 → 20td 窗内聚合于信号日处取。"""
    a = e49.load_analyst()
    if a is None:
        return pd.DataFrame(columns=['ann_date', 'ts_code',
                                     'an_rating_dir20', 'an_epsrev20'])
    return a


# ---------- 特征面板 ----------
def build_features() -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    close_w = pd.read_parquet(e27.OUT_DIR / 'panel_close.parquet')
    r = daily_returns(close_w)
    days = r.index
    month_ends = pd.Series(days).groupby([days.year, days.month]).max()
    sig_days = pd.DatetimeIndex(sorted(month_ends))
    sig_days = sig_days[sig_days <= SCREEN_END]
    _note(f"signal days: {len(sig_days)}")

    # 价量特征
    feats = {}
    for h in (5, 20, 60, 120):
        feats[f'ret{h}'] = close_w.shift(0) / close_w.shift(h) - 1
    feats['vol20'] = r.rolling(20).std()
    feats['max20'] = r.rolling(20).max()
    # turnover/amihud 需要 daily_basic 面板
    db = _load_daily_basic()
    tov = db['turnover_rate'] / 100.0
    feats['turnover20'] = tov.rolling(20).mean().reindex(
        index=days, columns=close_w.columns)
    amt = tov * db['circ_mv'] * 1e4          # 估算成交额(元)
    amih = (r.abs() / amt.replace(0, np.nan)) * 1e8
    feats['amihud20'] = amih.rolling(20).mean().reindex(
        index=days, columns=close_w.columns)
    for c in ('pe', 'pb', 'dv_ttm', 'circ_mv'):
        feats[c] = db[c].reindex(index=days, columns=close_w.columns)
    feats['log_circ_mv'] = np.log(feats['circ_mv'].clip(lower=1e6))
    feats = {k: v.reindex(index=days) for k, v in feats.items()}

    # 两融
    mg = _load_margin(days, close_w.columns)
    feats['fin_bal_chg20'] = mg['fin_balance'] / \
        mg['fin_balance'].shift(20) - 1
    feats['short_qty_chg20'] = mg['short_qty'] / \
        mg['short_qty'].replace(0, np.nan).shift(20) - 1

    # 标签：T+1 收盘入场 → T+20 收盘退出（= cp[T+20]/cp[T+1]−1）
    cp = (1 + r).cumprod()
    fwd20 = cp.shift(-20) / cp.shift(-1) - 1
    lbl_ex = fwd20.sub(fwd20.mean(axis=1), axis=0)

    esets = build_event_sets(days)
    ana = analyst_feats()

    rows = []
    valid20 = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    for T in sig_days:
        i = days.get_loc(T)
        if i + 21 >= len(days):
            continue
        col = pd.DataFrame(index=close_w.columns)
        for k, p in feats.items():
            col[k] = p.loc[T]
        col['label'] = lbl_ex.loc[T]
        col['valid'] = valid20.loc[T]
        col = col[col['valid'] & col['label'].notna()]
        # 事件旗标：trailing 20td
        w0, w1 = i - 19, i
        win_days = days[w0:w1 + 1]
        for tag, d2c in esets.items():
            hot: set = set()
            for d in win_days:
                hot |= d2c.get(d, set())
            col[tag] = col.index.isin(hot).astype(int)
        # 分析师 trailing 窗聚合（≈20td，日历 30d 近似）
        if not ana.empty:
            a20 = ana[(ana.ann_date > T - pd.Timedelta(days=30))
                      & (ana.ann_date <= T)]
            col['an_rating_dir20'] = col.index.map(
                a20[a20.kind == 'rating'].groupby('ts_code')
                .rating_dir.sum()).fillna(0)
            col['an_epsrev20'] = col.index.map(
                a20[a20.kind == 'forecast'].groupby('ts_code')
                .fy_np_chg.count()).fillna(0)
        col['sig_date'] = T
        rows.append(col.reset_index().rename(columns={'index': 'ts_code'}))
    X = pd.concat(rows, ignore_index=True)
    return X, sig_days


_DB_CACHE: dict = {}


def _load_daily_basic() -> dict:
    if _DB_CACHE:
        return _DB_CACHE
    fs = sorted(glob.glob(str(ROOT / 'data/daily_basic_alla/*.parquet')))
    d = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    d['trade_date'] = pd.to_datetime(d['trade_date'])
    for c in ('turnover_rate', 'pe', 'pb', 'dv_ttm', 'circ_mv'):
        d[c] = pd.to_numeric(d[c], errors='coerce')
    panels = {}
    for c in ('turnover_rate', 'pe', 'pb', 'dv_ttm', 'circ_mv'):
        panels[c] = d.pivot(index='trade_date', columns='ts_code',
                            values=c).sort_index()
    _DB_CACHE.update(panels)
    return panels


_MG_CACHE: dict = {}


def _load_margin(days, cols):
    if _MG_CACHE:
        return _MG_CACHE
    fs = sorted(glob.glob(str(ROOT / 'data/margin_detail/*.parquet')))
    d = pd.concat([pd.read_parquet(f, columns=['ts_code', 'fin_balance',
                                             'short_qty'])
                   .assign(day=pd.to_datetime(Path(f).stem))
                   for f in fs], ignore_index=True)
    out = {}
    for c in ('fin_balance', 'short_qty'):
        out[c] = (d.pivot(index='day', columns='ts_code', values=c)
                  .sort_index().reindex(index=days, columns=cols))
    _MG_CACHE.update(out)
    return out


def _zscore(X: pd.DataFrame) -> pd.DataFrame:
    for c in PRICE_FEATS + ['an_rating_dir20', 'an_epsrev20']:
        v = X[c].clip(X.groupby('sig_date')[c].transform(
            lambda s: s.quantile(0.01)),
            X.groupby('sig_date')[c].transform(lambda s: s.quantile(0.99)))
        mu = X.groupby('sig_date')[c].transform('mean')
        sd = X.groupby('sig_date')[c].transform('std').replace(0, np.nan)
        X[c] = (v - mu) / sd
    return X


def run_eval(X: pd.DataFrame, shuffle: bool) -> dict:
    import lightgbm as lgb
    rng = np.random.default_rng(42)
    years = sorted(pd.DatetimeIndex(X.sig_date).year.unique())
    metrics = {'months': [], 'ic': [], 'spread': [], 'churn': [],
               'top_net': []}
    prev_top: set = set()
    for Y in years:
        tr_mask = (pd.DatetimeIndex(X.sig_date) <
                   pd.Timestamp(f'{Y}-01-01') - pd.Timedelta(days=30))
        te_mask = pd.DatetimeIndex(X.sig_date).year == Y
        tr = X[tr_mask]
        te = X[te_mask]
        if tr.sig_date.nunique() < MIN_TRAIN_MONTHS or len(te) == 0:
            continue
        ytr = tr.groupby('sig_date')['label'].rank(pct=True)
        if shuffle:
            ytr = pd.Series(rng.permutation(ytr.values), index=tr.index)
        ds = lgb.Dataset(tr[PRICE_FEATS + EVENT_FEATS], label=ytr)
        mdl = lgb.train(LGBM_PARAMS, ds)
        te = te.copy()
        te['score'] = mdl.predict(te[PRICE_FEATS + EVENT_FEATS])
        for T, g in te.groupby('sig_date'):
            if len(g) < 200:
                continue
            ic = g['score'].corr(g['label'], method='spearman')
            top = g.nlargest(int(len(g) * 0.1), 'score')
            bot = g.nsmallest(int(len(g) * 0.1), 'score')
            spread = top['label'].mean() - bot['label'].mean()
            top_ex = top['label'].mean()
            churn = 1 - len(set(top.ts_code) & prev_top) / max(
                1, len(top)) if prev_top else 1.0
            prev_top = set(top.ts_code)
            metrics['months'].append(str(T.date()))
            metrics['ic'].append(float(ic))
            metrics['spread'].append(float(spread))
            metrics['churn'].append(float(churn))
            metrics['top_net'].append(
                float(top_ex - churn * 0.003))
    ic = np.array(metrics['ic'])
    yrs = pd.Series(metrics['months']).str[:4].astype(int)
    ic_by_year = pd.Series(ic).groupby(yrs).mean()
    n = len(ic)
    t_spread = float(np.mean(metrics['spread']) /
                     (np.std(metrics['spread'], ddof=1) / np.sqrt(n))
                     ) if n > 5 else np.nan
    return {
        'n_months': n,
        'ic_mean': float(ic.mean()) if n else None,
        'icir': float(ic.mean() / ic.std(ddof=1) * np.sqrt(12))
        if n and ic.std(ddof=1) > 0 else None,
        'ic_pos_year_ratio': float((ic_by_year > 0).mean())
        if n else None,
        'spread_mean': float(np.mean(metrics['spread'])) if n else None,
        'spread_t': t_spread,
        'churn_mean': float(np.mean(metrics['churn'])) if n else None,
        'top_net_mean': float(np.mean(metrics['top_net'])) if n else None,
        'years': {str(k): float(v) for k, v in ic_by_year.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    args = ap.parse_args()
    if not args.run:
        print("⛔ --run required (prereg frozen 2026-09-22)")
        return 2
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    cache = OUT_DIR / 'features.parquet'
    if cache.exists():
        X = pd.read_parquet(cache)
    else:
        X, _ = build_features()
        X.to_parquet(cache)
    _note(f"feature matrix {X.shape}")

    Xz = _zscore(X.copy())
    res = {'meta': {'rows': len(X), 'feats': PRICE_FEATS + EVENT_FEATS}}
    res['main'] = run_eval(Xz, shuffle=False)
    res['placebo'] = run_eval(Xz, shuffle=True)
    _note(f"main: {res['main']}")
    _note(f"placebo: {res['placebo']}")

    m = res['main']
    ok = (m['ic_mean'] and m['ic_mean'] >= 0.03
          and m['icir'] and m['icir'] >= 0.3
          and m['spread_t'] and m['spread_t'] >= 2.6
          and m['ic_pos_year_ratio'] and m['ic_pos_year_ratio'] >= 0.6
          and m['top_net_mean'] and m['top_net_mean'] >= 0.0015
          and abs(res['placebo'].get('ic_mean') or 0) < 0.01)
    res['verdict'] = '强' if ok else '负'
    res['elapsed_min'] = (time.time() - t0) / 60
    (OUT_DIR / 'e58_results.json').write_text(json.dumps(
        res, ensure_ascii=False, indent=1, default=str))
    _note(f"verdict={res['verdict']} done {res['elapsed_min']:.1f}min")
    return 0


if __name__ == '__main__':
    sys.exit(main())
