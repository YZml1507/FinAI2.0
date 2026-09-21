#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e25 双面板质量/价值影子筛选（docs/E25_FACTOR_SCREEN_PREREG.md 冻结）。

16 假设同一多重比较家族：
- Panel P：c3_pool 年度 PIT 池内（pool_yearly.parquet，T.year 当年名单，并集 692）
- Panel F：daily_basic_alla 全 A（与 e23 同宇宙同窗）

与 e23 共享冻结口径（import 复用，不重写）：日收益/除权跳空剔除/上市日/
月末信号日/T+1→T+21 前瞻收益/换手成本/年度一致性统计口径。

e25 新增口径：
- 财报 PIT：financial_pit_alla，pub_date ≤ T 最新一条，⛔ 无新鲜度截断；
  staleness>180 自然日仅作诊断计数。
- 多观测窗因子：近 8 条已公告（<4 条 → NaN）。
- 分红史：dividend_events_alla，ex_date 归属自然年；
  P6=近 5 已完成自然年分红年数；P7=相邻两年现金分红和之比−1。
- F1/F2：daily_basic pe/pb，仅 >0 入排。
- G4：信号有定义月(n≥5)中低样本月占比 ≤30%（P<300 / F<1000）。
- 分级：「强」t≥2.85+G2/G3/G4；「弱」2.0≤t<2.85+G2/G3/G4；其余负；
  有效月 M<60 → INCONCLUSIVE。
- 合成臂：同面板 ≥2 个弱及以上 → z-score 截面标准化等权合成 1 臂
  （披露性追加，不计入首轮 16 假设多重比较基数）。

输出 experiments/lab/e25/: panel_{close,turnover,pe,pb}.parquet 缓存 +
e25_results.json + e25_monthly.parquet + 控制台表。不写 leaderboard/registry。

用法：.venv/bin/python -m scripts.lab.e25_factor_screen
        --build-panel [--limit-days N] | --run [--hyp P1 F1 ...] [--no-composite]
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
FINA_DIR = ROOT / 'data/financial_pit_alla'
DIV_DIR = ROOT / 'data/dividend_events_alla'
POOL_PATH = ROOT / 'data/c3_pool/pool_yearly.parquet'
OUT_DIR = ROOT / 'experiments/lab/e25'

COST_PER_TURN = 0.0030
MIN_DEFINED_N = 5          # 信号有定义的下限（n≥5 计入 G4 分母与有效月）
MIN_N = {'P': 300, 'F': 1000}
T_STRONG = 2.85            # Bonferroni 16 假设
T_WEAK = 2.0
MIN_MONTHS = 60            # 有效月 <60 → INCONCLUSIVE
STALE_DIAG_DAYS = 180      # 财报陈旧度诊断阈值（非闸门）

# PIT 财报字段在 vals 矩阵中的列序
F_ROE, F_NPY, F_DNPY, F_DTA, F_CFPS = 0, 1, 2, 3, 4
PIT_FIELDS = ['roe', 'net_profit_yoy', 'deducted_net_profit_yoy',
              'debt_to_assets', 'cash_flow_per_share']

# hyp -> (panel, 构造名, 低好?)
HYP_DEFS = {
    'P1': ('P', 'roe', False),       'P2': ('P', 'roe_std8', True),
    'P3': ('P', 'eq_gap', False),    'P4': ('P', 'dta', True),
    'P5': ('P', 'cfps', False),      'P6': ('P', 'div_cont5', False),
    'P7': ('P', 'div_growth', False),'P8': ('P', 'npyoy_pos8', False),
    'F1': ('F', 'pe', True),         'F2': ('F', 'pb', True),
    'F3': ('F', 'roe', False),       'F4': ('F', 'dta', True),
    'F5': ('F', 'eq_gap', False),    'F6': ('F', 'roe_std8', True),
    'F7': ('F', 'cfps', False),      'F8': ('F', 'npyoy_pos8', False),
}

NOTES: list[str] = []


def _note(msg: str) -> None:
    NOTES.append(msg)
    print(f"[note] {msg}")


def bs_to_ts(code: str) -> str:
    """sh.600000 → 600000.SH（面板列名口径，同 e23.first_seen_listed）。"""
    m, c = code.split('.')
    return f"{c}.{m.upper()}"


# ---------- 面板构建 ----------

def build_panel(limit_days: int | None = None) -> dict:
    files = sorted(DV_DIR.glob('*.parquet'))
    if limit_days:
        files = files[:limit_days]
        _note(f"--limit-days={limit_days} 开发用截断（{len(files)} 截面），结果不可用于判定")
    t0 = time.time()
    cols = ['ts_code', 'close', 'turnover_rate', 'pe', 'pb']
    acc = {k: {} for k in ('close', 'turnover_rate', 'pe', 'pb')}
    for i, f in enumerate(files):
        df = pd.read_parquet(f, columns=cols).set_index('ts_code')
        d = pd.to_datetime(f.stem, format='%Y%m%d')
        for k in acc:
            acc[k][d] = df[k]
        if (i + 1) % 500 == 0:
            print(f"[panel] {i+1}/{len(files)} ({time.time()-t0:.0f}s)")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_lim{limit_days}" if limit_days else ""
    for k, ser in acc.items():
        w = pd.DataFrame(ser).T.astype(np.float64)
        w.to_parquet(OUT_DIR / f'panel_{k}{suffix}.parquet')
        print(f"[panel] {k} {w.shape}")
    print(f"[panel] done in {time.time()-t0:.0f}s -> {OUT_DIR}")
    return {'days': len(files), 'elapsed': time.time() - t0}


def load_panels(limit_days: int | None):
    suffix = f"_lim{limit_days}" if limit_days else ""
    return tuple(pd.read_parquet(OUT_DIR / f'panel_{k}{suffix}.parquet')
                 for k in ('close', 'turnover_rate', 'pe', 'pb'))


# ---------- 数据缓存 ----------

def load_pit() -> dict:
    """ts_code -> (pub datetime64[ns] sorted, vals n×5 float64)。"""
    cache = {}
    for f in FINA_DIR.glob('*.parquet'):
        df = pd.read_parquet(f).dropna(subset=['pub_date'])
        if df.empty:
            continue
        df = df.sort_values('pub_date')
        pub = pd.to_datetime(df['pub_date']).to_numpy(dtype='datetime64[ns]')
        vals = df[PIT_FIELDS].to_numpy(dtype=np.float64)
        cache[bs_to_ts(f.stem)] = (pub, vals)
    _note(f"PIT 财报缓存 {len(cache)} 只（financial_pit_alla，Panel P/F 共用）")
    return cache


def load_div() -> dict:
    """ts_code -> {'years': set[int], 'cash': dict[year]=cash_div 和}。"""
    cache: dict[str, dict] = {}
    for f in DIV_DIR.glob('*.parquet'):
        df = pd.read_parquet(f).dropna(subset=['ex_date'])
        if df.empty:
            continue
        for row in df.itertuples():
            ent = cache.setdefault(row.ts_code, {'years': set(), 'cash': {}})
            y = int(str(row.ex_date)[:4])
            cash = row.cash_div if pd.notna(row.cash_div) else 0.0
            stk = row.stk_div if pd.notna(row.stk_div) else 0.0
            if cash > 0 or stk > 0:
                ent['years'].add(y)
            if cash > 0:
                ent['cash'][y] = ent['cash'].get(y, 0.0) + float(cash)
    _note(f"分红史缓存 {len(cache)} 只（dividend_events_alla，ex_date 归年）")
    return cache


def load_pool() -> dict[int, set[str]]:
    """year -> {ts_code}（pool_yearly baostock 码 → ts 码）。"""
    p = pd.read_parquet(POOL_PATH)
    return {int(r.year): {bs_to_ts(s) for s in r.symbols}
            for r in p.itertuples()}


# ---------- 信号构造（全部按冻结口径） ----------

def pit_snapshot(T: pd.Timestamp, pit: dict) -> dict[str, pd.Series]:
    """T 时点 PIT 快照：最新一条 + 近 8 条聚合（<4 条观测 → NaN）。"""
    t64 = np.datetime64(T)
    cols = {k: {} for k in ('roe', 'npy', 'dnpy', 'dta', 'cfps',
                            'eq_gap', 'roe_std8', 'npyoy_pos8', 'stale')}
    for sym, (pub, vals) in pit.items():
        i = int(np.searchsorted(pub, t64, side='right'))
        if i == 0:
            continue
        last = vals[i - 1]
        cols['roe'][sym] = last[F_ROE]
        cols['npy'][sym] = last[F_NPY]
        cols['dnpy'][sym] = last[F_DNPY]
        cols['dta'][sym] = last[F_DTA]
        cols['cfps'][sym] = last[F_CFPS]
        cols['eq_gap'][sym] = last[F_DNPY] - last[F_NPY]
        cols['stale'][sym] = (t64 - pub[i - 1]).astype('timedelta64[D]').astype(int)
        win = vals[max(0, i - 8):i]
        roe_w = win[:, F_ROE]
        roe_w = roe_w[~np.isnan(roe_w)]
        cols['roe_std8'][sym] = (float(np.std(roe_w, ddof=1))
                                 if len(roe_w) >= 4 else np.nan)
        npy_w = win[:, F_NPY]
        npy_w = npy_w[~np.isnan(npy_w)]
        cols['npyoy_pos8'][sym] = (float((npy_w > 0).mean())
                                   if len(npy_w) >= 4 else np.nan)
    return {k: pd.Series(v, dtype=np.float64) for k, v in cols.items()}


def div_snapshot(T: pd.Timestamp, divc: dict) -> dict[str, pd.Series]:
    """P6 分红连续性 / P7 分红增长（按已完成自然年）。"""
    y = T.year
    cont, growth = {}, {}
    for sym, ent in divc.items():
        cont[sym] = float(sum(1 for yy in range(y - 5, y) if yy in ent['years']))
        c1, c0 = ent['cash'].get(y - 1), ent['cash'].get(y - 2)
        growth[sym] = (c1 / c0 - 1.0) if (c1 and c0) else np.nan
    return {'div_cont5': pd.Series(cont, dtype=np.float64),
            'div_growth': pd.Series(growth, dtype=np.float64)}


def signal_at(builder: str, T: pd.Timestamp, snap: dict, dnap: dict,
              pe_w: pd.DataFrame, pb_w: pd.DataFrame) -> pd.Series:
    """按构造名取 T 时点信号序列。"""
    if builder == 'pe':
        return pe_w.loc[T].where(pe_w.loc[T] > 0)
    if builder == 'pb':
        return pb_w.loc[T].where(pb_w.loc[T] > 0)
    if builder in dnap:
        return dnap[builder]
    return snap[builder]


# ---------- 月度评估 ----------

def eval_month(sig: pd.Series, low: bool, base_ok: pd.Series,
               fwd_row: pd.Series, nan_row: pd.Series) -> dict:
    """单月五分位评估；返回行 dict（n<5 → NaN 价差如实登记）。"""
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
    close_w, turn_w, pe_w, pb_w = load_panels(limit_days)
    r = e23.daily_returns(close_w)
    ipo = e23.first_seen_listed(r, close_w)
    _note("上市日字段：stock_basic_cache.ipoDate（同 e23 口径）")
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
    pit = load_pit()
    divc = load_div()
    pool = load_pool()
    cols_arr = np.array(r.columns)
    Ts = [T for T in e23.month_ends(idx)
          if int(pos[T]) + e23.HORIZON <= len(idx) - 1]
    monthly_rows: list[dict] = []
    sig_cache: dict[tuple[str, str], pd.Series] = {}
    ctx: dict[str, tuple] = {}   # T_str -> (base_P, base_F, fwd_row, nan_row)
    stale_diag: dict[str, float] = {}
    for T in Ts:
        snap = pit_snapshot(T, pit)
        dnap = div_snapshot(T, divc)
        stale_diag[str(T.date())] = float(
            (snap['stale'] > STALE_DIAG_DAYS).mean()) if len(snap['stale']) else np.nan
        fwd_row, nan_row = fwd.loc[T], nanfrac.loc[T]
        base_common = listed_ok.loc[T] & close_w.loc[T].notna()
        pool_mask = pd.Series(cols_arr, index=r.columns).isin(
            pool.get(T.year, set()))
        base_P, base_F = base_common & pool_mask, base_common
        ctx[str(T.date())] = (base_P, base_F, fwd_row, nan_row)
        for h in hyps:
            panel, builder, low = HYP_DEFS[h]
            sig = signal_at(builder, T, snap, dnap, pe_w, pb_w).reindex(r.columns)
            sig_cache[(h, str(T.date()))] = sig
            base_ok = base_P if panel == 'P' else base_F
            row = eval_month(sig, low, base_ok, fwd_row, nan_row)
            monthly_rows.append(dict(hyp=h, panel=panel, T=str(T.date()), **row))
    res = stats(monthly_rows)
    if composite:
        monthly_rows += composite_rows(res, sig_cache, ctx)
        res = stats(monthly_rows)
    res['_stale_frac_mean'] = float(np.nanmean(list(stale_diag.values())))
    return monthly_rows, res


def composite_rows(res: dict, sig_cache: dict, ctx: dict) -> list[dict]:
    """同面板 ≥2 弱及以上 → z-score 等权合成披露臂（panel='C'，G4 按源面板）。"""
    rows: list[dict] = []
    for panel in ('P', 'F'):
        elig = [h for h, d in HYP_DEFS.items()
                if d[0] == panel and res.get(h, {}).get('grade') in ('强', '弱')]
        if len(elig) < 2:
            _note(f"Panel {panel} 弱及以上假设 {len(elig)}<2，合成臂不触发")
            continue
        _note(f"Panel {panel} 合成臂触发，成员={sorted(elig)}")
        name = f'C{panel}'
        for ts, (base_P, base_F, fwd_row, nan_row) in ctx.items():
            base_ok = base_P if panel == 'P' else base_F
            zs = []
            for h in elig:
                s = sig_cache[(h, ts)]
                v = s[base_ok & s.notna()]
                if len(v) < MIN_DEFINED_N or v.std(ddof=0) == 0:
                    continue
                z = (s - v.mean()) / v.std(ddof=0)
                if HYP_DEFS[h][2]:       # 低好 → 取负统一为「越高越好」
                    z = -z
                zs.append(z)
            if len(zs) < 2:
                row = dict(n=0, spread=np.nan, excess=np.nan,
                           nan_ratio=np.nan, _top=None)
            else:
                sig = pd.concat(zs, axis=1).mean(axis=1)
                row = eval_month(sig, False, base_ok, fwd_row, nan_row)
            rows.append(dict(hyp=name, panel=panel, T=ts, **row))
    return rows


# ---------- 统计与分级 ----------

def stats(monthly_rows: list[dict]) -> dict:
    df = pd.DataFrame(monthly_rows)
    out = {}
    for h, g in df.groupby('hyp'):
        panel = g['panel'].iloc[0]
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
        # G4（e23 修正版）：信号有定义月（n≥5）中低样本月占比 ≤30%
        defined = g[g['n'] >= MIN_DEFINED_N]
        low_frac = (float((defined['n'] < MIN_N[panel]).mean())
                    if len(defined) else np.nan)
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
        out[h] = dict(panel=panel, M=M, t=float(t_stat),
                      mean_spread=mean_spread,
                      mean_excess=float(g['excess'].mean()),
                      year_consistency=year_sign, half1=h1, half2=h2,
                      turn_top=turn_top, cost=cost,
                      g4_low_frac=low_frac,
                      G1=g1, G2=g2, G3=g3, G4=g4, grade=grade,
                      n_min=int(g['n'].min()), n_median=float(g['n'].median()),
                      n_nan_ratio=float(g['nan_ratio'].mean())
                      if 'nan_ratio' in g else np.nan)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description='e25 双面板影子筛选（预登记冻结口径）')
    ap.add_argument('--build-panel', action='store_true')
    ap.add_argument('--run', action='store_true')
    ap.add_argument('--no-composite', action='store_true')
    ap.add_argument('--limit-days', type=int, default=None,
                    help='开发用：只用前 N 个截面（登记进 notes，不可用于判定）')
    ap.add_argument('--hyp', nargs='*', default=list(HYP_DEFS))
    args = ap.parse_args()

    if args.build_panel:
        m = build_panel(args.limit_days)
        (OUT_DIR / 'panel_build_notes.json').write_text(
            json.dumps({'limit_days': args.limit_days, 'days': m['days'],
                        'elapsed_s': m['elapsed'], 'notes': NOTES},
                       ensure_ascii=False, indent=1))
        return 0
    if args.run:
        rows, res = run_screen(args.limit_days, args.hyp,
                               composite=not args.no_composite)
        res['_notes'] = NOTES + e23.NOTES   # 含 e23 复用函数的除权/截断登记
        res['_limit_days'] = args.limit_days
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / 'e25_results.json').write_text(
            json.dumps(res, ensure_ascii=False, indent=1))
        pd.DataFrame(rows).drop(columns=['_top'], errors='ignore').to_parquet(
            OUT_DIR / 'e25_monthly.parquet')
        tbl = {k: v for k, v in res.items() if not k.startswith('_')}
        print(pd.DataFrame(tbl).T.to_string())
        return 0
    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
