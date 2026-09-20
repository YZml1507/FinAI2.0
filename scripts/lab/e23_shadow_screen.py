#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e23 影子筛选：全 A 截面六假设五分位价差（docs/E23_SHADOW_SCREEN_PREREG.md 冻结）。

实现口径完全按 /home/ubuntu/e23_research/e23_shadow_spec.md（主窗口规格）：
- 收益：日收盘价比值；除权日跳空（|r|>0.11 且当日为 ex_date）置 NaN；
  其余 |r|>0.21 置 NaN；复合时 NaN 视为 0 收益并计数（低估含派现股票，登记）。
- 上市日 = stock_basic_cache.ipoDate（spec 的 list_date 字段不存在；
  ipoDate 为同义字段，无需启用「首次出现日」兜底——notes 登记）。
- 面板缓存 experiments/lab/e23/panel_{close,turnover}.parquet（宽表，float64）。
- 输出 e23_results.json + e23_monthly.parquet + 控制台表格。
  不写 leaderboard、不写 registry。

用法：.venv/bin/python scripts/lab/e23_shadow_screen.py --build-panel [--limit-days N] | --run [--hyp H1]
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
DV_DIR = ROOT / 'data/daily_basic_alla'
Fina_DIR = ROOT / 'data/financial_pit_alla'
DIV_DIR = ROOT / 'data/dividend_events_alla'
STOCK_BASIC = ROOT / 'data/stock_basic_cache.parquet'
OUT_DIR = ROOT / 'experiments/lab/e23'

MIN_LISTED_DAYS = 120
EX_JUMP = 0.11
RET_CAP = 0.21
HORIZON = 21
SKIP = 21          # H1 剔近 21 交易日
LOOK_MOM = 252     # H1 长窗
MIN_MOM_DAYS = 200
VOL_WIN = 60
MIN_VOL_DAYS = 40
TURN_WIN = 20
MIN_TURN_DAYS = 10
PEAD_FRESH_DAYS = 60
COST_PER_TURN = 0.0030
G4_MIN_N = 1000

NOTES = []


def _note(msg: str) -> None:
    NOTES.append(msg)
    print(f"[note] {msg}")


def build_panel(limit_days: int | None = None) -> dict:
    files = sorted(DV_DIR.glob('*.parquet'))
    if limit_days:
        files = files[:limit_days]
        _note(f"--limit-days={limit_days} 开发用截断（{len(files)} 截面），结果不可用于判定")
    t0 = time.time()
    closes, turns = {}, {}
    for i, f in enumerate(files):
        df = pd.read_parquet(f, columns=['ts_code', 'close', 'turnover_rate'])
        d = pd.to_datetime(f.stem, format='%Y%m%d')
        closes[d] = df.set_index('ts_code')['close']
        turns[d] = df.set_index('ts_code')['turnover_rate']
        if (i + 1) % 500 == 0:
            print(f"[panel] {i+1}/{len(files)} ({time.time()-t0:.0f}s)")
    close_w = pd.DataFrame(closes).T.astype(np.float64)
    turn_w = pd.DataFrame(turns).T.astype(np.float64)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_lim{limit_days}" if limit_days else ""
    close_w.to_parquet(OUT_DIR / f'panel_close{suffix}.parquet')
    turn_w.to_parquet(OUT_DIR / f'panel_turnover{suffix}.parquet')
    print(f"[panel] close {close_w.shape} turnover {turn_w.shape} "
          f"in {time.time()-t0:.0f}s -> {OUT_DIR}")
    return {'days': len(files), 'elapsed': time.time() - t0}


def load_panels(limit_days: int | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    suffix = f"_lim{limit_days}" if limit_days else ""
    return (pd.read_parquet(OUT_DIR / f'panel_close{suffix}.parquet'),
            pd.read_parquet(OUT_DIR / f'panel_turnover{suffix}.parquet'))


def daily_returns(close_w: pd.DataFrame) -> pd.DataFrame:
    """日收益 + spec 跳空/超限剔除。ex_date 匹配自 dividend_events_alla。"""
    r = close_w / close_w.shift(1) - 1.0
    big = r.abs() > RET_CAP
    exmask = pd.DataFrame(False, index=r.index, columns=r.columns)
    for f in DIV_DIR.glob('*.parquet'):
        ev = pd.read_parquet(f, columns=['ts_code', 'ex_date']).dropna(subset=['ex_date'])
        for row in ev.itertuples():
            if row.ts_code not in exmask.columns:
                continue
            d = pd.to_datetime(str(row.ex_date)[:8], format='%Y%m%d', errors='coerce')
            if pd.isna(d) or d not in exmask.index:
                continue
            exmask.at[d, row.ts_code] = True
    exjump = (r.abs() > EX_JUMP) & exmask
    n_ex = int(exjump.sum().sum())
    r = r.mask(exjump | big)
    _note(f"除权日跳空剔除 {n_ex} 格；|r|>0.21 超限剔除 {int(big.sum().sum())} 格")
    return r


def first_seen_listed(r: pd.DataFrame, close_w: pd.DataFrame) -> pd.Series:
    """上市日：ipoDate（stock_basic_cache；spec 的 list_date 不存在，ipoDate 同义）。"""
    sb = pd.read_parquet(STOCK_BASIC)
    ipo = sb.set_index('code')['ipoDate'].astype(str).str[:10]

    def to_repo(ts: str) -> str:
        c, m = ts.split('.')
        return f"{m.lower()}.{c}"
    return ipo.rename(index=to_repo)


def month_ends(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(idx, index=idx)
    return pd.DatetimeIndex(s.groupby([idx.year, idx.month]).max().values)


def signal_frames(r, turn_w, close_w):
    """返回 dict hyp -> DataFrame(date × symbol)；方向高好统一为「越大越好」前的原值。"""
    rf = r.fillna(0.0)
    log1p = np.log1p(rf.where(rf > -1, 0.0))
    # H1 mom_12_1: Π r over (T-252, T-21]
    mom = (log1p.rolling(LOOK_MOM - SKIP).sum()
           .shift(SKIP))  # 窗口终点 T-21
    mom_valid = (r.notna().rolling(LOOK_MOM - SKIP).sum().shift(SKIP) >= MIN_MOM_DAYS)
    h1 = mom.where(mom_valid)
    # H2 rev_1m: (T-21, T]
    h2 = log1p.rolling(SKIP).sum()
    # H3 vol_60
    h3 = r.rolling(VOL_WIN).std(ddof=1).where(
        r.notna().rolling(VOL_WIN).sum() >= MIN_VOL_DAYS)
    # H4 turn_20
    h4 = turn_w.rolling(TURN_WIN).mean().where(
        turn_w.notna().rolling(TURN_WIN).sum() >= MIN_TURN_DAYS)
    return {'H1': h1, 'H2': h2, 'H3': h3, 'H4': h4}


def pit_fina_signal(T: pd.Timestamp, fina_cache: dict, field: str) -> pd.Series:
    """pub_date ≤ T 最新一条（fina_cache: code -> DataFrame sorted by pub_date）。"""
    out = {}
    cutoff = (T - pd.Timedelta(days=PEAD_FRESH_DAYS)).strftime('%Y-%m-%d')
    for code, df in fina_cache.items():
        m = df[df['pub_date'] <= T.strftime('%Y-%m-%d')]
        if m.empty:
            continue
        row = m.iloc[-1]
        if row['pub_date'] < cutoff:
            continue
        out[code] = row[field]
    return pd.Series(out, dtype=np.float64)


def load_fina() -> dict:
    """code(ts 格式 600000.SH) -> df sorted pub_date。"""
    cache = {}
    for f in Fina_DIR.glob('*.parquet'):
        df = pd.read_parquet(f)
        df = df.dropna(subset=['pub_date']).sort_values('pub_date')
        cache[f.stem.split('.')[-1] + '.' + f.stem.split('.')[0].upper()] = df
    return cache


def fwd_returns(close_w: pd.DataFrame, r: pd.DataFrame) -> pd.DataFrame:
    """fwd20(T) = Π r_t over t=D[i+2..i+21]（即 close[i+21]/close[i+1]-1）。"""
    log1p = np.log1p(r.fillna(0.0))
    # fwd at index i = sum_{j=i+2..i+21} log1p[j]
    cs = log1p.cumsum()
    fwd = cs.shift(-HORIZON) - cs.shift(-1)
    return np.expm1(fwd)


def run_screen(limit_days: int | None, hyps: list[str]) -> dict:
    close_w, turn_w = load_panels(limit_days)
    r = daily_returns(close_w)
    ipo = first_seen_listed(r, close_w)
    _note("上市日字段：stock_basic_cache 无 list_date，使用 ipoDate（同义字段，非首次出现日兜底）")
    idx = r.index
    # 上市 <120 交易日剔除：自 ipoDate 起的交易日序号 ≥120 才入池
    listed_ok = pd.DataFrame(False, index=idx, columns=r.columns)
    ipo_map = {c: pd.to_datetime(v) for c, v in ipo.items() if v}
    pos = pd.Series(np.arange(len(idx)), index=idx)
    for c in r.columns:
        t0 = ipo_map.get(c)
        if t0 is None:
            continue
        listed_ok[c] = ((idx >= t0).cumsum() - 1) >= MIN_LISTED_DAYS
    sigs = signal_frames(r, turn_w, close_w)
    fina = load_fina() if any(h in hyps for h in ('H5', 'H6')) else {}
    fwd = fwd_returns(close_w, r)
    Ts = month_ends(idx)
    # 最后 T：i+21 ≤ 最后日
    Ts = [T for T in Ts if pos[T] + HORIZON <= len(idx) - 1]
    n_days = len(idx)
    monthly_rows = []
    for T in Ts:
        i = int(pos[T])
        if i + HORIZON >= n_days:
            continue
        fwd_row = fwd.loc[T]
        base_ok = listed_ok.loc[T] & close_w.loc[T].notna()
        for h in hyps:
            if h in ('H5', 'H6'):
                if h == 'H5':
                    sig = pit_fina_signal(T, fina, 'deducted_net_profit_yoy')
                else:
                    cf = pit_fina_signal(T, fina, 'cash_flow_per_share')
                    sig = cf / close_w.loc[T]
                    sig = sig.where(close_w.loc[T] > 0)
                sig = sig.reindex(r.columns)
            else:
                sig = sigs[h].loc[T]
            good = base_ok & sig.notna() & fwd_row.notna()
            n = int(good.sum())
            if n < 5:
                monthly_rows.append(dict(hyp=h, T=str(T.date()), n=n, spread=np.nan,
                                         excess=np.nan, turn=np.nan))
                continue
            sg, fg = sig[good], fwd_row[good]
            asc = h in ('H2', 'H3', 'H4')  # 低好 → 升序取头
            ranked = sg.sort_values(ascending=asc)
            q = max(1, n // 5)
            top = set(ranked.index[:q])
            bot = set(ranked.index[-q:])
            spread = fg[list(top)].mean() - fg[list(bot)].mean()
            excess = fg[list(top)].mean() - fg.mean()
            monthly_rows.append(dict(hyp=h, T=str(T.date()), n=n, spread=spread,
                                     excess=excess, _top=top))
    return monthly_rows


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
        year_sign = (gy.groupby('y')['spread'].mean() > 0).mean()
        h1 = gy.loc[gy['y'] <= 2019, 'spread'].mean()
        h2 = gy.loc[gy['y'] >= 2020, 'spread'].mean()
        tops = [r.get('_top') for r in monthly_rows if r['hyp'] == h and r.get('_top')]
        turns = []
        for a, b in zip(tops, tops[1:]):
            turns.append(len(b - a) / len(b))
        turn_top = float(np.mean(turns)) if turns else np.nan
        cost = turn_top * COST_PER_TURN if not np.isnan(turn_top) else np.nan
        mean_spread = float(sp.mean()) if M else np.nan
        g4_fail = int((g['n'] < G4_MIN_N).sum())
        g1 = bool(t_stat >= 2.0)
        g2 = bool(year_sign >= 0.60 and h1 > 0 and h2 > 0)
        g3 = bool(not np.isnan(cost) and cost <= 0.5 * mean_spread)
        g4 = g4_fail == 0
        grade = ('强' if (t_stat >= 2.6 and g2 and g3 and g4)
                 else ('弱' if (not np.isnan(t_stat) and t_stat >= 2.0) else '负'))
        out[h] = dict(M=M, t=float(t_stat), mean_spread=mean_spread,
                      mean_excess=float(g['excess'].mean()),
                      year_consistency=float(year_sign),
                      half1=float(h1), half2=float(h2),
                      turn_top=turn_top, cost=cost,
                      G1=g1, G2=g2, G3=g3, G4=g4, grade=grade,
                      n_min=int(g['n'].min()), n_median=float(g['n'].median()))
    return out, df.drop(columns=['_top'], errors='ignore')


def main() -> int:
    ap = argparse.ArgumentParser(description='e23 影子筛选（预登记冻结口径）')
    ap.add_argument('--build-panel', action='store_true')
    ap.add_argument('--run', action='store_true')
    ap.add_argument('--limit-days', type=int, default=None,
                    help='开发用：只用前 N 个截面（登记进 notes，不可用于判定）')
    ap.add_argument('--hyp', nargs='*', default=['H1', 'H2', 'H3', 'H4', 'H5', 'H6'])
    args = ap.parse_args()

    if args.build_panel:
        m = build_panel(args.limit_days)
        (OUT_DIR / 'panel_build_notes.json').write_text(
            json.dumps({'limit_days': args.limit_days, 'days': m['days'],
                        'elapsed_s': m['elapsed'], 'notes': NOTES},
                       ensure_ascii=False, indent=1))
        return 0
    if args.run:
        rows = run_screen(args.limit_days, args.hyp)
        res, mdf = stats(rows)
        res['_notes'] = NOTES
        res['_limit_days'] = args.limit_days
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / 'e23_results.json').write_text(
            json.dumps(res, ensure_ascii=False, indent=1))
        mdf.to_parquet(OUT_DIR / 'e23_monthly.parquet')
        print(pd.DataFrame(res).T.drop(index=['_notes', '_limit_days'],
                                     errors='ignore').to_string())
        return 0
    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
