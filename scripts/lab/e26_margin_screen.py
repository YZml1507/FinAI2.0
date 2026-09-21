#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e26 两融杠杆资金因子族影子筛选（docs/E26_MARGIN_FACTOR_PREREG.md 冻结）。

宇宙 = 当日两融标的 ∩ 全 A 有效（margin_detail 当日出现即资格，PIT 天然）。
6 假设同一多重比较家族（Bonferroni t≥2.6 为「强」）。

关键口径：
- ⛔ 两融明细次日披露 → 信号日 T 仅用 ≤T−1 交易日的 margin 截面
  （margin 日网格上取 last margin day < T，滞后一交易日）。
- 单位归一：daily_basic circ_mv/free_share 为万元/万股，fin_balance/fin_buy
  为元 → circ_mv×1e4 后作分母（本项目 688 单位坑教训，显式登记）。
- 方向先验全冻结为「多低」（融资活跃度低=杠杆未拥挤流入）。
- G4：有定义月(n≥5)中 n < max(300, 当日两融标的数×50%) 的月份占比 ≤30%。
- 「强」t≥2.6+G2/G3/G4；「弱」2.0≤t<2.6+G2/G3/G4；M<60 → INCONCLUSIVE。
- 合成臂：≥2 弱及以上 → z-score 等权 1 臂（披露，不计入基数）。

输出 experiments/lab/e26/: panel_{close,circ_mv,free_share,fin_balance,
fin_buy,short_qty}.parquet + e26_results.json + e26_monthly.parquet。
不写 leaderboard/registry。

用法：.venv/bin/python -m scripts.lab.e26_margin_screen
        --build-panel [--limit-days N] | --run [--hyp M1 ...] [--no-composite]
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
from scripts.lab import e25_factor_screen as e25

ROOT = Path(__file__).resolve().parents[2]
DV_DIR = ROOT / 'data/daily_basic_alla'
MARGIN_DIR = ROOT / 'data/margin_detail'
OUT_DIR = ROOT / 'experiments/lab/e26'

COST_PER_TURN = 0.0030
MIN_DEFINED_N = 5
G4_ABS_FLOOR = 300          # 绝对下限（五分位篮 ≥60 只）
G4_COV_FRAC = 0.50          # 当日两融标的覆盖率下限
T_STRONG = 2.6              # Bonferroni 6 假设（同 e23 口径）
T_WEAK = 2.0
MIN_MONTHS = 60
WIN_LONG = 20               # M1/M2/M5/M6 回看
WIN_SHORT = 5               # M4
MIN_OBS = 10                # 滚动均值最少有效日

# hyp -> (构造名, 低好?)  e26 全部低好（反向指标先验）
HYP_DEFS = {
    'M1': ('fin_chg20', True),   # 融资余额 20 日环比
    'M2': ('buy_int20', True),   # 融资买入/流通市值 20 日均
    'M3': ('fin_cmv', True),     # 融资余额/流通市值
    'M4': ('fin_chg5', True),    # 融资余额 5 日环比
    'M5': ('short_chg20', True), # 融券余量 20 日环比
    'M6': ('buy_turn20', True),  # 融资买入/融资余额 20 日均
}

NOTES: list[str] = []


def _note(msg: str) -> None:
    NOTES.append(msg)
    print(f"[note] {msg}")


# ---------- 面板构建 ----------

def build_panel(limit_days: int | None = None) -> dict:
    """三段：daily_basic(close/circ_mv/free_share) + margin 三字段宽表。"""
    dv_files = sorted(DV_DIR.glob('*.parquet'))
    if limit_days:
        dv_files = dv_files[:limit_days]
        _note(f"--limit-days={limit_days} 开发截断（{len(dv_files)} 截面）")
    t0 = time.time()
    acc = {k: {} for k in ('close', 'circ_mv', 'free_share')}
    for i, f in enumerate(dv_files):
        df = pd.read_parquet(
            f, columns=['ts_code', 'close', 'circ_mv', 'free_share'])
        d = pd.to_datetime(f.stem, format='%Y%m%d')
        for k in acc:
            acc[k][d] = df.set_index('ts_code')[k]
        if (i + 1) % 500 == 0:
            print(f"[panel-dv] {i+1}/{len(dv_files)} ({time.time()-t0:.0f}s)")
    m_files = sorted(MARGIN_DIR.glob('2*.parquet'))
    macc = {k: {} for k in ('fin_balance', 'fin_buy', 'short_qty')}
    for f in m_files:
        df = pd.read_parquet(
            f, columns=['ts_code', 'fin_balance', 'fin_buy', 'short_qty'])
        d = pd.to_datetime(f.stem, format='%Y%m%d')
        for k in macc:
            macc[k][d] = df.set_index('ts_code')[k]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_lim{limit_days}" if limit_days else ""
    for k, ser in {**acc, **macc}.items():
        w = pd.DataFrame(ser).T.astype(np.float64)
        w.to_parquet(OUT_DIR / f'panel_{k}{suffix}.parquet')
        print(f"[panel] {k} {w.shape}")
    print(f"[panel] done {time.time()-t0:.0f}s margin_days={len(m_files)}")
    return {'days': len(dv_files), 'margin_days': len(m_files),
            'elapsed': time.time() - t0}


def load_panels(limit_days: int | None):
    suffix = f"_lim{limit_days}" if limit_days else ""
    names = ('close', 'circ_mv', 'free_share', 'fin_balance', 'fin_buy',
             'short_qty')
    return {k: pd.read_parquet(OUT_DIR / f'panel_{k}{suffix}.parquet')
            for k in names}


# ---------- 信号构造（margin 日网格，信号日滞后一交易日） ----------

def margin_signal_frames(P: dict) -> dict[str, pd.DataFrame]:
    """在 margin 日网格上构造 6 个信号帧（行=margin 日，列=ts_code）。"""
    bal, buy, sqty = P['fin_balance'], P['fin_buy'], P['short_qty']
    cmv = P['circ_mv'].reindex(bal.index) * 1e4   # 万元→元
    frames = {
        'fin_chg20': bal / bal.shift(WIN_LONG) - 1,
        'fin_chg5': bal / bal.shift(WIN_SHORT) - 1,
        'fin_cmv': bal / cmv,
        'short_chg20': (sqty / sqty.shift(WIN_LONG) - 1).where(
            sqty.shift(WIN_LONG) > 0),
    }
    ratio_buy_cmv = buy / cmv
    frames['buy_int20'] = ratio_buy_cmv.rolling(
        WIN_LONG, min_periods=MIN_OBS).mean()
    ratio_buy_bal = buy / bal.where(bal > 0)
    frames['buy_turn20'] = ratio_buy_bal.rolling(
        WIN_LONG, min_periods=MIN_OBS).mean()
    return frames


def eval_month(sig: pd.Series, low: bool, base_ok: pd.Series,
               fwd_row: pd.Series, nan_row: pd.Series) -> dict:
    """同 e25：单月五分位评估（n<5 → NaN 价差如实登记）。"""
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
    P = load_panels(limit_days)
    close_w = P['close']
    r = e23.daily_returns(close_w)
    ipo = e23.first_seen_listed(r, close_w)
    idx = r.index
    listed_ok = pd.DataFrame(False, index=idx, columns=r.columns)
    ipo_map = {c: pd.to_datetime(v) for c, v in ipo.items() if v}
    pos = pd.Series(np.arange(len(idx)), index=idx)
    for c in r.columns:
        t0 = ipo_map.get(c)
        if t0 is None:
            continue
        listed_ok[c] = ((idx >= t0).cumsum() - 1) >= e23.MIN_LISTED_DAYS
    fwd = e23.fwd_returns(close_w, r)
    valid_cum = (~r.isna()).astype(float).cumsum()
    nanfrac = 1 - (valid_cum.shift(-e23.HORIZON) - valid_cum.shift(-1)) / e23.HORIZON

    frames = margin_signal_frames(P)
    margin_dates = P['fin_balance'].index
    mpos = pd.Series(np.arange(len(margin_dates)), index=margin_dates)
    mcount = P['fin_balance'].notna().sum(axis=1)   # 每日两融标的数

    Ts = [T for T in e23.month_ends(idx)
          if int(pos[T]) + e23.HORIZON <= len(idx) - 1]
    monthly_rows: list[dict] = []
    sig_cache: dict[tuple[str, str], pd.Series] = {}
    ctx: dict[str, tuple] = {}
    for T in Ts:
        # ⛔ 次日披露口径：last margin day < T
        j = int(margin_dates.searchsorted(T, side='left')) - 1
        if j < 0:
            continue
        mday = margin_dates[j]
        fwd_row, nan_row = fwd.loc[T], nanfrac.loc[T]
        base_ok = listed_ok.loc[T] & close_w.loc[T].notna()
        ctx[str(T.date())] = (base_ok, fwd_row, nan_row)
        for h in hyps:
            builder, low = HYP_DEFS[h]
            sig = frames[builder].iloc[int(mpos[mday])].reindex(r.columns)
            sig_cache[(h, str(T.date()))] = sig
            row = eval_month(sig, low, base_ok, fwd_row, nan_row)
            monthly_rows.append(dict(
                hyp=h, T=str(T.date()), mrg_n=int(mcount.loc[mday]), **row))
    res = stats(monthly_rows)
    if composite:
        monthly_rows += composite_rows(res, sig_cache, ctx)
        res = stats(monthly_rows)
    return monthly_rows, res


def composite_rows(res: dict, sig_cache: dict, ctx: dict) -> list[dict]:
    """≥2 弱及以上 → z-score 等权合成披露臂（hyp='CM'）。"""
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
            zs.append(-(s - v.mean()) / v.std(ddof=0))   # 低好→取负
        if len(zs) < 2:
            row = dict(n=0, spread=np.nan, excess=np.nan,
                       nan_ratio=np.nan, _top=None)
        else:
            row = eval_month(pd.concat(zs, axis=1).mean(axis=1), False,
                             base_ok, fwd_row, nan_row)
        rows.append(dict(hyp='CM', T=ts, mrg_n=np.nan, **row))
    return rows


# ---------- 统计与分级 ----------

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
        # G4：n < max(300, 当日两融标的数×50%) 的有定义月占比 ≤30%
        defined = g[g['n'] >= MIN_DEFINED_N]
        if len(defined):
            thresh = np.maximum(G4_ABS_FLOOR, defined['mrg_n'] * G4_COV_FRAC)
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
    ap = argparse.ArgumentParser(description='e26 两融因子影子筛选（预登记冻结口径）')
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
        res['_notes'] = NOTES + e23.NOTES + e25.NOTES
        res['_limit_days'] = args.limit_days
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / 'e26_results.json').write_text(
            json.dumps(res, ensure_ascii=False, indent=1))
        pd.DataFrame(rows).drop(columns=['_top'], errors='ignore').to_parquet(
            OUT_DIR / 'e26_monthly.parquet')
        tbl = {k: v for k, v in res.items() if not k.startswith('_')}
        print(pd.DataFrame(tbl).T.to_string())
        return 0
    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
