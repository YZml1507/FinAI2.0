#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e28 股东户数因子族影子筛选（docs/E28_GDHS_FACTOR_PREREG.md 冻结口径）。

宇宙 = 全 A 有效（剔除/上市<120 日同 e23 口径）。
6 假设同一多重比较家族（Bonferroni t≥2.6 为「强」）。

关键口径：
- ⛔ PIT 锚 = 逐股真实公告日：信号日 T 仅用 公告日期 ≤ T 的最新一期户数记录；
  同一统计截止日多次披露取最早公告日（首次可见为准）。
- 剔除规则（冻结）：户数-本次 <1000 或 公告日缺失 → 该期该股缺席（次新/pre-IPO 口径）。
- S6 行业相对排名：证监会行业分类 baostock 季末时点快照（data/industry/），
  T 时点用 ≤T 最近一期快照映射行业。
- G4：有定义月(n≥5)中 n < max(200, 全A有效数×5%) 的月份占比 ≤30%。
- 「强」t≥2.6+G2/G3/G4；「弱」2.0≤t<2.6+G2/G3/G4；M<60 → INCONCLUSIVE。
- 合成臂：≥2 弱及以上 → z-score 等权 1 臂（披露，不计入基数）。

输出 experiments/lab/e28/: panel_close.parquet + e28_results.json +
e28_monthly.parquet。不写 leaderboard/registry。

用法：.venv/bin/python -m scripts.lab.e28_gdhs_screen
        --build-panel [--limit-days N] | --run [--hyp S1 ...] [--no-composite]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.lab import e23_shadow_screen as e23

ROOT = Path(__file__).resolve().parents[2]
DV_DIR = ROOT / 'data/daily_basic_alla'
GDHS_DIR = ROOT / 'data/gdhs'
IND_DIR = ROOT / 'data/industry'
OUT_DIR = ROOT / 'experiments/lab/e28'

COST_PER_TURN = 0.0030
MIN_DEFINED_N = 5
G4_ABS_FLOOR = 200          # 冻结：季频稀疏族绝对下限
G4_COV_FRAC = 0.05          # 全A有效×5% 下限
T_STRONG = 2.6
T_WEAK = 2.0
MIN_MONTHS = 60
RET60_WIN = 60              # S3 价格强势回看
ZSCORE_WIN = 4              # S5 历史期数
MIN_HOLDERS = 1000          # 剔除规则（冻结）

# hyp -> (构造名, 低好?)
HYP_DEFS = {
    'S1': ('qoq_chg', True),        # 户数环比变化率——户数减=筹码集中→低好
    'S2': ('consec_down', False),   # 连续收缩档 0-2
    'S3': ('qoq_x_ret60', False),   # S1 分位×近60日收益分位
    'S4': ('avgsh_chg', False),     # 户均持股数量环比
    'S5': ('holders_z4', True),     # 户数自身4期 z-score→低好
    'S6': ('qoq_ind_rank', True),   # S1 行业内相对分位→低好
}

NOTES: list[str] = []


def _note(msg: str) -> None:
    NOTES.append(msg)
    print(f"[note] {msg}")


# ---------- 面板构建 ----------

def build_panel(limit_days: int | None = None) -> dict:
    dv_files = sorted(DV_DIR.glob('*.parquet'))
    if limit_days:
        dv_files = dv_files[:limit_days]
        _note(f"--limit-days={limit_days} 开发截断（{len(dv_files)} 截面）")
    t0 = time.time()
    acc = {}
    for i, f in enumerate(dv_files):
        df = pd.read_parquet(f, columns=['ts_code', 'close'])
        d = pd.to_datetime(f.stem, format='%Y%m%d')
        acc[d] = df.set_index('ts_code')['close']
        if (i + 1) % 500 == 0:
            print(f"[panel-dv] {i+1}/{len(dv_files)} ({time.time()-t0:.0f}s)")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_lim{limit_days}" if limit_days else ""
    w = pd.DataFrame(acc).T.astype(np.float64)
    w.to_parquet(OUT_DIR / f'panel_close{suffix}.parquet')
    print(f"[panel] close {w.shape} ({time.time()-t0:.0f}s)")
    return {'days': len(dv_files), 'elapsed': time.time() - t0}


def load_close_panel(limit_days: int | None) -> pd.DataFrame:
    suffix = f"_lim{limit_days}" if limit_days else ""
    return pd.read_parquet(OUT_DIR / f'panel_close{suffix}.parquet')


# ---------- gdhs 事件链 ----------

def load_gdhs_records() -> pd.DataFrame:
    """48 期快照 → 长表（按 code+stat_date 去重取最早公告日，冻结剔除规则）。"""
    rows = []
    for f in sorted(GDHS_DIR.glob('*.parquet')):
        df = pd.read_parquet(f)
        df = df.rename(columns={
            '代码': 'code', '股东户数-本次': 'holders',
            '股东户数-上次': 'holders_prev', '股东户数统计截止日-本次': 'stat_date',
            '户均持股数量': 'avg_shares', '公告日期': 'ann_date'})
        rows.append(df[['code', 'holders', 'holders_prev', 'stat_date',
                        'avg_shares', 'ann_date']])
    long = pd.concat(rows, ignore_index=True)
    long['ann_date'] = pd.to_datetime(long['ann_date'], errors='coerce')
    long['stat_date'] = pd.to_datetime(long['stat_date'], errors='coerce')
    for k in ('holders', 'holders_prev', 'avg_shares'):
        long[k] = pd.to_numeric(long[k], errors='coerce')
    n0 = len(long)
    # 冻结剔除：户数<1000 或公告日缺失 → 缺席
    long = long[(long['holders'] >= MIN_HOLDERS) & long['ann_date'].notna()
                & (long['holders_prev'] > 0)]
    _note(f"gdhs 长表 {n0}→{len(long)} 行（剔除户数<{MIN_HOLDERS}/公告缺失/"
          f"上次户数≤0）")
    # 同 code+stat_date 多披露 → 最早公告日
    long = (long.sort_values('ann_date')
                .drop_duplicates(['code', 'stat_date'], keep='first'))
    return long.sort_values(['code', 'stat_date']).reset_index(drop=True)


def load_industry_snaps() -> tuple[list, dict]:
    """全部行业快照 → (日期序列, {date: code6→industry})；T 时点用 ≤T 最近期。"""
    files = sorted(IND_DIR.glob('*.parquet'))
    if not files:
        _note("⚠ data/industry/ 空，S6 将全 NaN（弱项如实登记）")
        return [], {}
    snaps = {}
    for f in files:
        df = pd.read_parquet(f)
        snaps[pd.to_datetime(f.stem)] = df.set_index(
            df['code'].str.split('.').str[-1])['industry']
    dates = sorted(snaps)
    _note(f"行业快照 {len(dates)} 期 {dates[0].date()}→{dates[-1].date()}")
    return dates, snaps


def visible_signal(long: pd.DataFrame, Ts: list[pd.Timestamp]) -> pd.DataFrame:
    """对每个信号日 T、每个 code，取 公告日≤T 的最新记录，产 (T, code) 特征表。"""
    feats = {}
    for code, g in long.groupby('code'):
        ann = g['ann_date'].values.astype('datetime64[ns]')
        holders = g['holders'].values
        holders_prev = g['holders_prev'].values
        avg_sh = g['avg_shares'].values
        qoq = holders / holders_prev - 1
        # S2: 连续收缩 = 本次下降且上次也降（prev 链：holders[i]<holders_prev[i] 且 holders[i-1]<holders_prev[i-1]）
        down = holders < holders_prev
        consec = np.where(down, 1, 0)
        consec = np.minimum(consec + np.where(
            np.concatenate([[False], down[:-1]]), 1, 0), 2)
        # S4: 户均股数环比（跨记录）
        avgsh_chg = np.full(len(g), np.nan)
        avgsh_chg[1:] = avg_sh[1:] / np.where(avg_sh[:-1] > 0, avg_sh[:-1],
                                            np.nan) - 1
        # S5: 户数 z-score vs 自身近4期
        hz = np.full(len(g), np.nan)
        for i in range(len(g)):
            lo = max(0, i - ZSCORE_WIN)
            hist = holders[lo:i]
            if len(hist) >= 2 and hist.std(ddof=0) > 0:
                hz[i] = (holders[i] - hist.mean()) / hist.std(ddof=0)
        fr = pd.DataFrame({
            'ann': ann, 'qoq_chg': qoq, 'consec_down': consec,
            'avgsh_chg': avgsh_chg, 'holders_z4': hz})
        feats[code] = fr
    # asof: 对每个 T 每个 code 取最后 ann≤T 的行
    out = {}
    for T in Ts:
        snap = {}
        for code, fr in feats.items():
            j = np.searchsorted(fr['ann'].values, np.datetime64(T),
                                side='right') - 1
            if j >= 0:
                snap[code] = fr.iloc[j]
        out[T] = pd.DataFrame(snap).T if snap else pd.DataFrame()
    return out


# ---------- 评估（骨架同 e26） ----------

def eval_month(sig: pd.Series, low: bool, base_ok: pd.Series,
               fwd_row: pd.Series, nan_row: pd.Series) -> dict:
    good = base_ok & sig.notna() & fwd_row.notna()
    n = int(good.sum())
    if n < MIN_DEFINED_N:
        return dict(n=n, spread=np.nan, excess=np.nan, nan_ratio=np.nan,
                    _top=None)
    sg, fg = sig[good], fwd_row[good]
    ranked = sg.sort_values(ascending=low)
    q = max(1, n // 5)
    top = set(ranked.index[:q])
    bot = set(ranked.index[-q:])
    return dict(n=n, spread=float(fg[list(top)].mean() - fg[list(bot)].mean()),
                excess=float(fg[list(top)].mean() - fg.mean()),
                nan_ratio=float(nan_row[good].mean()), _top=top)


def run_screen(limit_days: int | None, hyps: list[str],
               composite: bool = True) -> tuple[list[dict], dict]:
    close_w = load_close_panel(limit_days)
    r = e23.daily_returns(close_w)
    ipo = e23.first_seen_listed(r, close_w)
    idx = r.index
    listed_ok = pd.DataFrame(False, index=idx, columns=r.columns)
    ipo_map = {c: pd.to_datetime(v) for c, v in ipo.items() if v}
    for c in r.columns:
        t0 = ipo_map.get(c)
        if t0 is None:
            continue
        listed_ok[c] = ((idx >= t0).cumsum() - 1) >= e23.MIN_LISTED_DAYS
    fwd = e23.fwd_returns(close_w, r)
    valid_cum = (~r.isna()).astype(float).cumsum()
    nanfrac = 1 - (valid_cum.shift(-e23.HORIZON)
                   - valid_cum.shift(-1)) / e23.HORIZON

    pos = pd.Series(np.arange(len(idx)), index=idx)
    Ts = [T for T in e23.month_ends(idx)
          if int(pos[T]) + e23.HORIZON <= len(idx) - 1]

    long = load_gdhs_records()
    # code 归一：gdhs 代码 6 位 ↔ ts_code 'XXXXXX.SH'
    six2ts = {t.split('.')[0]: t for t in r.columns}
    long['code'] = long['code'].astype(str).str.zfill(6)
    long = long[long['code'].isin(six2ts)]
    snap_by_T = visible_signal(long, Ts)
    ind_dates, ind_snaps = load_industry_snaps()
    ret60 = close_w / close_w.shift(RET60_WIN) - 1

    monthly_rows: list[dict] = []
    sig_cache: dict[tuple[str, str], pd.Series] = {}
    ctx: dict[str, tuple] = {}
    for T in Ts:
        snap = snap_by_T.get(T)
        base_ok = listed_ok.loc[T] & close_w.loc[T].notna()
        fwd_row, nan_row = fwd.loc[T], nanfrac.loc[T]
        ctx[str(T.date())] = (base_ok, fwd_row, nan_row)
        if snap is None or snap.empty:
            continue
        snap.index = snap.index.map(lambda c6: six2ts.get(c6, c6))
        snap = snap[~snap.index.duplicated()]
        r60_z = ret60.loc[T].reindex(snap.index)
        for h in hyps:
            builder, low = HYP_DEFS[h]
            if builder == 'qoq_x_ret60':
                s1 = snap['qoq_chg']
                if s1.notna().sum() < MIN_DEFINED_N:
                    sig = pd.Series(np.nan, index=snap.index)
                else:
                    q_s1 = s1.rank(pct=True)
                    q_r60 = r60_z.rank(pct=True)
                    sig = (1 - q_s1) * q_r60  # 户数降(1-qoq pct)×涨势→高好
            elif builder == 'qoq_ind_rank':
                if not ind_dates:
                    sig = pd.Series(np.nan, index=r.columns)
                    sig_cache[(h, str(T.date()))] = sig
                    monthly_rows.append(dict(hyp=h, T=str(T.date()),
                                             gdh_n=int(len(snap)),
                                             **eval_month(sig, True, base_ok,
                                                          fwd_row, nan_row)))
                    continue
                j = np.searchsorted(np.array(ind_dates, dtype='datetime64[ns]'),
                                    np.datetime64(T), side='right') - 1
                if j < 0:
                    sig = pd.Series(np.nan, index=r.columns)
                    sig_cache[(h, str(T.date()))] = sig
                    monthly_rows.append(dict(hyp=h, T=str(T.date()),
                                             gdh_n=int(len(snap)),
                                             **eval_month(sig, True, base_ok,
                                                          fwd_row, nan_row)))
                    continue
                ind_map = ind_snaps[ind_dates[j]]
                ind = snap.index.map(
                    lambda t: ind_map.get(t.split('.')[0], np.nan))
                s1 = snap['qoq_chg']
                sig = s1.groupby(pd.Series(ind, index=snap.index)).rank(
                    pct=True)
            else:
                sig = snap[builder]
            sig = sig.reindex(r.columns)
            sig_cache[(h, str(T.date()))] = sig
            row = eval_month(sig, low, base_ok, fwd_row, nan_row)
            monthly_rows.append(dict(hyp=h, T=str(T.date()),
                                     gdh_n=int(len(snap)), **row))
    res = stats(monthly_rows)
    if composite:
        monthly_rows += composite_rows(res, sig_cache, ctx)
        res = stats(monthly_rows)
    return monthly_rows, res


def composite_rows(res: dict, sig_cache: dict, ctx: dict) -> list[dict]:
    elig = [h for h in HYP_DEFS
            if res.get(h, {}).get('grade') in ('强', '弱')]
    if len(elig) < 2:
        _note(f"弱及以上假设 {len(elig)}<2，合成臂不触发")
        return []
    _note(f"合成臂触发，成员={sorted(elig)}")
    rows = []
    for ts, (base_ok, fwd_row, nan_row) in ctx.items():
        zs = []
        for h in elig:
            s = sig_cache[(h, ts)]
            v = s[base_ok & s.notna()]
            if len(v) < MIN_DEFINED_N or v.std(ddof=0) == 0:
                continue
            z = (s - v.mean()) / v.std(ddof=0)
            zs.append(-z if HYP_DEFS[h][1] else z)
        if len(zs) < 2:
            row = dict(n=0, spread=np.nan, excess=np.nan, nan_ratio=np.nan,
                       _top=None)
        else:
            row = eval_month(pd.concat(zs, axis=1).mean(axis=1), False,
                             base_ok, fwd_row, nan_row)
        rows.append(dict(hyp='CM', T=ts, gdh_n=np.nan, **row))
    return rows


def stats(monthly_rows: list[dict]) -> dict:
    df = pd.DataFrame(monthly_rows)
    out = {}
    for h, g in df.groupby('hyp'):
        g = g.sort_values('T')
        sp = g['spread'].dropna()
        M = len(sp)
        sd = sp.std(ddof=1)
        if M > 1 and sd > 0:
            t_stat = sp.mean() / (sd / np.sqrt(M))
        elif M > 1:
            t_stat = np.inf if sp.mean() > 0 else -np.inf
        else:
            t_stat = np.nan
        gy = g.assign(y=pd.to_datetime(g['T']).dt.year)
        year_sign = float((gy.groupby('y')['spread'].mean() > 0).mean())
        h1 = float(gy.loc[gy['y'] <= 2019, 'spread'].mean())
        h2 = float(gy.loc[gy['y'] >= 2020, 'spread'].mean())
        tops = [r['_top'] for _, r in g.iterrows() if r['_top']]
        turns = [len(b - a) / len(b) for a, b in zip(tops, tops[1:])]
        turn_top = float(np.mean(turns)) if turns else np.nan
        cost = turn_top * COST_PER_TURN if not np.isnan(turn_top) else np.nan
        mean_spread = float(sp.mean()) if M else np.nan
        defined = g[g['n'] >= MIN_DEFINED_N]
        if len(defined):
            thresh = np.maximum(G4_ABS_FLOOR,
                                defined['gdh_n'] * G4_COV_FRAC)
            low_frac = float((defined['n'] < thresh).mean())
        else:
            low_frac = np.nan
        g1 = bool(not np.isnan(t_stat) and t_stat >= T_WEAK)
        g2 = bool(year_sign >= 0.60 and h1 > 0 and h2 > 0)
        g3 = bool(not np.isnan(cost) and cost <= 0.5 * mean_spread)
        g4 = bool(not np.isnan(low_frac) and low_frac <= 0.30)
        if M < MIN_MONTHS:
            grade = 'INCONCLUSIVE'
        elif not np.isnan(t_stat) and t_stat >= T_STRONG and g2 and g3 and g4:
            grade = '强'
        elif not np.isnan(t_stat) and t_stat >= T_WEAK and g2 and g3 and g4:
            grade = '弱'
        else:
            grade = '负'
        out[h] = dict(M=M, t=float(t_stat), mean_spread=mean_spread,
                      mean_excess=float(g['excess'].mean()),
                      year_consistency=year_sign, half1=h1, half2=h2,
                      turn_top=turn_top, cost=cost, g4_low_frac=low_frac,
                      G1=g1, G2=g2, G3=g3, G4=g4, grade=grade,
                      n_min=int(g['n'].min()), n_median=float(g['n'].median()),
                      n_nan_ratio=float(g['nan_ratio'].mean())
                      if 'nan_ratio' in g else np.nan)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description='e28 股东户数因子影子筛选（预登记冻结口径）')
    ap.add_argument('--build-panel', action='store_true')
    ap.add_argument('--run', action='store_true')
    ap.add_argument('--no-composite', action='store_true')
    ap.add_argument('--limit-days', type=int, default=None)
    ap.add_argument('--hyp', nargs='*', default=list(HYP_DEFS))
    args = ap.parse_args()

    if args.build_panel:
        m = build_panel(args.limit_days)
        (OUT_DIR / 'panel_build_notes.json').write_text(
            json.dumps({'limit_days': args.limit_days, **m, 'notes': NOTES},
                       ensure_ascii=False, indent=1))
        return 0
    if args.run:
        rows, res = run_screen(args.limit_days, args.hyp,
                               composite=not args.no_composite)
        res['_notes'] = NOTES + e23.NOTES
        res['_limit_days'] = args.limit_days
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / 'e28_results.json').write_text(
            json.dumps(res, ensure_ascii=False, indent=1))
        pd.DataFrame(rows).drop(columns=['_top'], errors='ignore').to_parquet(
            OUT_DIR / 'e28_monthly.parquet')
        tbl = {k: v for k, v in res.items() if not k.startswith('_')}
        print(pd.DataFrame(tbl).T.to_string())
        return 0
    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
