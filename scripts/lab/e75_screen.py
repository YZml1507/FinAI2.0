#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e75 年报文本因子族筛选（docs/E75_AR_TEXT_PREREG.md 冻结）。

输入：experiments/lab/e75/features.parquet（e75_ar_text.py 产物，
is_first=首次披露事件）。事件=同年截面 rank≥0.8 分位（top quintile）。
T1-T4 负先验：CAR 翻号后统一走 e29 判定约定（car>0=先验兑现）。
h∈{20,60,120}：e29.HORIZONS 运行时改写（fwd_panels/arm_stats 读
模块全局，收单登记口径差异）。
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

from scripts.lab.e23_shadow_screen import MIN_LISTED_DAYS, daily_returns  # noqa: E402
from scripts.lab import e27_insider_screen as e27  # noqa: E402
from scripts.lab import e29_lhb_screen as e29  # noqa: E402
from scripts.lab import e49_analyst_screen as e49  # noqa: E402

# 年报文本信号为低频年频——窗长与冻结 prereg 对齐
e29.HORIZONS = (20, 60, 120)

OUT_DIR = ROOT / 'experiments' / 'lab' / 'e75'
SCREEN_END = pd.Timestamp('2024-12-31')
MIN_N = 300
RESIDUAL_GATE = 0.5
TOP_Q = 0.8  # 截面 top-20%

ARMS = {
    'T1_len':      ('nchars',   'ln', True),
    'T2_delay':    ('delay',    'raw', True),
    'T3_jac':      ('yoy_jac',  'raw', True),
    'T4_risk':     ('risk_per_k', 'raw', True),
    'T5_tone':     ('tone_net', 'raw', False),
}


def _note(m): print(f"[note] {m}", flush=True)


def load_events() -> pd.DataFrame:
    df = pd.read_parquet(OUT_DIR / 'features.parquet')
    df = df[df['is_first'] & (df['pubdate'] <= SCREEN_END)].copy()
    df['ts_code'] = df['code'].map(e49._norm_code)
    df['trade_date'] = df['pubdate']
    df['delay'] = (df['pubdate'] - pd.to_datetime(
        (df['fyear']).astype(str) + '-12-31')).dt.days
    df['nchars'] = np.log1p(df['nchars'])
    return df


def top_quintile_events(df: pd.DataFrame, col: str,
                        drop_revision: bool = False) -> pd.DataFrame:
    x = df.dropna(subset=[col])
    if drop_revision:
        x = x[~x['is_revision']]
    x = x.copy()
    x['rank_pct'] = x.groupby(x['trade_date'].dt.year)[col].rank(pct=True)
    return x[x['rank_pct'] >= TOP_Q][['trade_date', 'ts_code', col]]


def run_arm(name: str, ev: pd.DataFrame, neg: bool,
            r, valid, days_idx, fwd, base) -> dict:
    car = e29.car_table(ev, r, valid, days_idx, fwd, base)
    if neg:
        for c in car.columns:
            if c.startswith('car'):
                car[c] = -car[c]  # 翻号：car>0 即负向先验兑现
    st = e29.arm_stats(car, name)
    st['verdict'] = (e29.verdict(st) if st['n_events'] >= MIN_N
                     else 'INCONCLUSIVE(样本不足)')
    if st['verdict'] == '强':
        mask = e49.fillability_filter(ev, r, valid, days_idx)
        st['fillable_ratio'] = float(mask.mean())
        if mask.sum() >= MIN_N:
            car_f = e29.car_table(ev[mask], r, valid, days_idx, fwd, base)
            if neg:
                for c in car_f.columns:
                    if c.startswith('car'):
                        car_f[c] = -car_f[c]
            st_f = e29.arm_stats(car_f, name + '_fillable')
            orig = st.get('h20', {}).get('car_mean')
            resid = st_f.get('h20', {}).get('car_mean')
            st['fillable_h20'] = resid
            if orig and resid is not None and abs(resid) < RESIDUAL_GATE * abs(orig):
                st['verdict'] = '判强不晋级(可成交切片残余<50%)'
    h20 = st.get('h20', {})
    _note(f"{name}: n={st['n_events']} h20={h20.get('car_mean')} "
          f"t={h20.get('t_cluster')} fill={st.get('fillable_ratio')} "
          f"verdict={st['verdict']}")
    return st


def placebo_shuffled(events: pd.DataFrame, days_idx, r, valid, fwd, base,
                     seed: int = 42) -> dict:
    rng = np.random.default_rng(seed)
    i_idx = {d: i for i, d in enumerate(days_idx)}
    rows = []
    for ev in events.itertuples():
        i = i_idx.get(ev.trade_date)
        if i is None:
            continue
        j = int(np.clip(i + rng.integers(-30, 31), 0, len(days_idx) - 2))
        rows.append({'trade_date': days_idx[j], 'ts_code': ev.ts_code})
    ev2 = pd.DataFrame(rows)
    car = e29.car_table(ev2, r, valid, days_idx, fwd, base)
    return e29.arm_stats(car, 'T6_placebo')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    args = ap.parse_args()
    if not args.run:
        print("⛔ --run required (E75 prereg frozen 2026-09-23)")
        return 2
    t0 = time.time()
    df = load_events()
    _note(f"events={len(df)} codes={df['ts_code'].nunique()} "
          f"win={df['pubdate'].min().date()}→{df['pubdate'].max().date()}")

    panel = e27.OUT_DIR / 'panel_close_2025.parquet'
    if not panel.exists():
        panel = e27.OUT_DIR / 'panel_close.parquet'
    _note(f"panel={panel.name}")
    close_w = pd.read_parquet(panel)
    r = daily_returns(close_w)
    valid = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    days_idx = r.index

    fwd = e29.fwd_panels(r)
    base = e29.baseline_mean(fwd, valid)
    plc = e29.placebo_gate(r, valid, days_idx, fwd, base)
    _note(f"placebo: {plc}")
    res = {'meta': {'n_events': len(df),
                    'codes': int(df['ts_code'].nunique())},
           'placebo': plc}
    if not plc['pass']:
        res['aborted'] = True
    else:
        all_ev = []
        for name, (col, tf, neg) in ARMS.items():
            ev = top_quintile_events(df, col,
                                     drop_revision=(name == 'T2_delay'))
            res[name] = run_arm(name, ev, neg, r, valid, days_idx, fwd, base)
            all_ev.append(ev[['trade_date', 'ts_code']])
        u = pd.concat(all_ev).drop_duplicates()
        res['T6_placebo'] = placebo_shuffled(u, days_idx, r, valid, fwd, base)
        _note(f"T6: n={res['T6_placebo']['n_events']} "
              f"h20={res['T6_placebo'].get('h20', {}).get('car_mean')}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / 'e75_results.json').write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=str))
    _note(f"done {(time.time() - t0) / 60:.1f}min → {OUT_DIR}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
