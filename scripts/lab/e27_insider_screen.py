#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e27 内部人增减持因子族影子筛选（docs/E27_INSIDER_FACTOR_PREREG.md 冻结）。

宇宙 = 全 A 有效（剔除/上市<120 日同 e23 口径）。
6 假设同一多重比较家族（Bonferroni t≥2.6 为「强」）。

关键口径（冻结）：
- ⛔ PIT 锚：事件可见日 = CHANGE_DATE + 15 交易日（SSE 申报通道校准
  n=1447 p95≈23 自然日）；信号日 T 仅用 vis_date ≤ T 的事件。
- 剔除 REASON 含『继承/赠与/过户/质押/申购/行权/授予/要约/协议/资管/转让/
  股转/基金/约定购回/不详/其他/其它』的行；方向仅以 CHANGE_SHARES 符号定。
- I1/I2/I5/I6 用 PERSON_DSE_RELATION=='本人' 行；I3 用全部行减持侧。
- 金额=CHANGE_AMOUNT（元）；circ_mv 万元×1e4 作分母（688 单位坑登记）。
- G4：有定义月(n≥5)中 n < max(200, 全A有效数×5%) 的月份占比 ≤30%。

输出 experiments/lab/e27/: panel_close/panel_circ_mv + e27_results.json +
e27_monthly.parquet。不写 leaderboard/registry。

用法：.venv/bin/python -m scripts.lab.e27_insider_screen
        --build-panel [--limit-days N] | --run [--hyp I1 ...] [--no-composite]
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
INSIDER_FILE = ROOT / 'data/insider_trade/detail_2015_2024.parquet'
OUT_DIR = ROOT / 'experiments/lab/e27'

COST_PER_TURN = 0.0030
MIN_DEFINED_N = 5
G4_ABS_FLOOR = 200
G4_COV_FRAC = 0.05
T_STRONG = 2.6
T_WEAK = 2.0
MIN_MONTHS = 60
LAG_TD = 15                 # 冻结：CHANGE_DATE + 15 交易日可见
WIN60 = 60
WIN120 = 120

# 冻结：非信息性/非市场原因剔除子串
EXCLUDE_REASON = ('继承', '赠与', '过户', '质押', '申购', '行权', '授予',
                  '要约', '协议', '资管', '转让', '股转', '基金', '约定购回',
                  '不详', '其他', '其它')

# hyp -> (低好?)
HYP_DEFS = {
    'I1': False,   # 本人净增持额/流通市值 60d → 多高
    'I2': False,   # 本人增持广度 60d → 多高
    'I3': True,    # 全部减持额/流通市值 60d → 多低
    'I4': False,   # 净增持笔数 120d → 多高
    'I5': False,   # 增持均价/现价 60d → 多高
    'I6': False,   # 增持月份数 120d → 多高
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
    acc = {k: {} for k in ('close', 'circ_mv')}
    for i, f in enumerate(dv_files):
        df = pd.read_parquet(f, columns=['ts_code', 'close', 'circ_mv'])
        d = pd.to_datetime(f.stem, format='%Y%m%d')
        for k in acc:
            acc[k][d] = df.set_index('ts_code')[k]
        if (i + 1) % 500 == 0:
            print(f"[panel-dv] {i+1}/{len(dv_files)} ({time.time()-t0:.0f}s)")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_lim{limit_days}" if limit_days else ""
    for k, ser in acc.items():
        w = pd.DataFrame(ser).T.astype(np.float64)
        w.to_parquet(OUT_DIR / f'panel_{k}{suffix}.parquet')
        print(f"[panel] {k} {w.shape}")
    return {'days': len(dv_files), 'elapsed': time.time() - t0}


def load_panels(limit_days: int | None):
    suffix = f"_lim{limit_days}" if limit_days else ""
    return {k: pd.read_parquet(OUT_DIR / f'panel_{k}{suffix}.parquet')
            for k in ('close', 'circ_mv')}


# ---------- 事件表（冻结过滤） ----------

def load_events(idx: pd.DatetimeIndex) -> pd.DataFrame:
    df = pd.read_parquet(INSIDER_FILE)
    n0 = len(df)
    reason = df['CHANGE_REASON'].astype(str)
    df = df[~reason.str.contains('|'.join(EXCLUDE_REASON), na=True)]
    df['chg_date'] = pd.to_datetime(df['CHANGE_DATE'])
    df['code6'] = df['SECURITY_CODE'].astype(str).str.zfill(6)
    # 可见日 = 变动日 + 15 交易日（按交易日索引位移）
    pos = pd.Series(np.arange(len(idx)), index=idx)
    cpos = df['chg_date'].map(pos)
    df = df[cpos.notna()]
    df['vis_pos'] = (cpos.dropna().astype(int) + LAG_TD).clip(upper=len(idx) - 1)
    df['vis_date'] = idx[df['vis_pos'].astype(int)]
    df['is_self'] = df['PERSON_DSE_RELATION'] == '本人'
    df['is_buy'] = df['CHANGE_SHARES'] > 0
    df['amt'] = pd.to_numeric(df['CHANGE_AMOUNT'], errors='coerce')
    df['price'] = pd.to_numeric(df['AVERAGE_PRICE'], errors='coerce')
    _note(f"事件 {n0}→{len(df)}（剔除非信息性原因/不可索引日期）")
    return df[['code6', 'chg_date', 'vis_date', 'is_self', 'is_buy',
               'amt', 'price', 'PERSON_NAME']].reset_index(drop=True)


def features_at_T(events: pd.DataFrame, T: pd.Timestamp,
                  ts2six: dict) -> pd.DataFrame:
    """T 时点可见事件窗口聚合 → code6 索引特征表。"""
    vis = events[events['vis_date'] <= T]
    f = {}
    for win, days in (('w60', WIN60), ('w120', WIN120)):
        lo = T - pd.Timedelta(days=days)
        w = vis[vis['chg_date'] >= lo]   # 窗口按变动日计
        if len(w) == 0:
            f[win] = pd.DataFrame()
            continue
        g = w.groupby('code6')
        self_w = w[w['is_self']]
        buy_amt = w['amt'].where(w['is_buy'], 0)
        sell_amt = -w['amt'].where(~w['is_buy'], 0)
        agg = pd.DataFrame({
            'net_amt_self': self_w['amt'].where(
                self_w['is_buy'], -self_w['amt']).groupby(
                    self_w['code6']).sum(),
            'buyers_self': self_w[self_w['is_buy']].groupby(
                'code6')['PERSON_NAME'].nunique(),
            'sell_amt_all': sell_amt.groupby(w['code6']).sum(),
            'n_buy': w['is_buy'].groupby(w['code6']).sum(),
            'n_sell': (~w['is_buy']).groupby(w['code6']).sum(),
            'buy_px_mean': self_w[self_w['is_buy']].groupby(
                'code6')['price'].mean(),
            'buy_months': self_w[self_w['is_buy']].assign(
                m=self_w[self_w['is_buy']]['chg_date'].dt.to_period('M')
            ).groupby('code6')['m'].nunique(),
        })
        f[win] = agg
    return f


def run_screen(limit_days: int | None, hyps: list[str],
               composite: bool = True) -> tuple[list[dict], dict]:
    P = load_panels(limit_days)
    close_w, cmv_w = P['close'], P['circ_mv']
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

    events = load_events(idx)
    six2ts = {t.split('.')[0]: t for t in r.columns}
    ts2six = {v: k for k, v in six2ts.items()}

    monthly_rows: list[dict] = []
    sig_cache: dict[tuple[str, str], pd.Series] = {}
    ctx: dict[str, tuple] = {}
    for T in Ts:
        base_ok = listed_ok.loc[T] & close_w.loc[T].notna()
        fwd_row, nan_row = fwd.loc[T], nanfrac.loc[T]
        ctx[str(T.date())] = (base_ok, fwd_row, nan_row)
        f = features_at_T(events, T, ts2six)
        w60, w120 = f['w60'], f['w120']
        cmv = (cmv_w.loc[T] * 1e4)
        cmv.index = cmv.index.map(lambda t: t.split('.')[0])
        cmv = cmv[~cmv.index.duplicated()]
        close_t = close_w.loc[T]
        close_t.index = close_t.index.map(lambda t: t.split('.')[0])
        close_t = close_t[~close_t.index.duplicated()]
        sigs = {}
        if len(w60):
            sigs['I1'] = (w60['net_amt_self'] / cmv).dropna()
            sigs['I2'] = w60['buyers_self'].dropna()
            sigs['I3'] = (w60['sell_amt_all'] / cmv).dropna()
            sigs['I5'] = (w60['buy_px_mean'] / close_t - 1).dropna()
        if len(w120):
            sigs['I4'] = (w120['n_buy'] - w120['n_sell']).dropna()
            sigs['I6'] = w120['buy_months'].dropna()
        for h in hyps:
            low = HYP_DEFS[h]
            sig = sigs.get(h, pd.Series(dtype=float))
            sig = sig.reindex([t.split('.')[0] for t in r.columns])
            sig.index = r.columns
            sig_cache[(h, str(T.date()))] = sig
            row = eval_month(sig, low, base_ok, fwd_row, nan_row)
            monthly_rows.append(dict(hyp=h, T=str(T.date()),
                                     base_n=int(base_ok.sum()),
                                     evt_n=int(len(events[
                                         events['vis_date'] <= T])), **row))
    res = stats(monthly_rows)
    if composite:
        monthly_rows += composite_rows(res, sig_cache, ctx)
        res = stats(monthly_rows)
    return monthly_rows, res


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
            zs.append(-z if HYP_DEFS[h] else z)
        if len(zs) < 2:
            row = dict(n=0, spread=np.nan, excess=np.nan, nan_ratio=np.nan,
                       _top=None)
        else:
            row = eval_month(pd.concat(zs, axis=1).mean(axis=1), False,
                             base_ok, fwd_row, nan_row)
        rows.append(dict(hyp='CM', T=ts, base_n=int(base_ok.sum()),
                         evt_n=np.nan, **row))
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
            thresh = np.maximum(G4_ABS_FLOOR, defined['base_n'] * G4_COV_FRAC)
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
                      n_nan_ratio=float(g['nan_ratio'].mean()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description='e27 内部人增减持因子影子筛选（预登记冻结口径）')
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
        (OUT_DIR / 'e27_results.json').write_text(
            json.dumps(res, ensure_ascii=False, indent=1))
        pd.DataFrame(rows).drop(columns=['_top'], errors='ignore').to_parquet(
            OUT_DIR / 'e27_monthly.parquet')
        tbl = {k: v for k, v in res.items() if not k.startswith('_')}
        print(pd.DataFrame(tbl).T.to_string())
        return 0
    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
