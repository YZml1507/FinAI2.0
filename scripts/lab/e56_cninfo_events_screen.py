#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e56 cninfo 六关键词事件族（docs/E56_CNINFO_EVENTS_PREREG.md）。

data/cninfo_events/{kw}_{YYYYMM}.parquet: {ann_date, ts_code, kw,
title, url}。T0=公告日，T+1 收盘入场，h∈{1,5,10,20}。
W4 高送转强制 ≤2017-06 / >2017-06 两期分解（体制断点）。
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
from scripts.lab import e52_resumption_screen as e52  # noqa: E402

E_DIR = ROOT / 'data' / 'cninfo_events'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e56'
SCREEN_END = pd.Timestamp('2024-12-31')
MIN_N = 300
REGIME_CUT = pd.Timestamp('2017-06-30')

# 事件终点/非起点剔除词
DROP = ['进展公告', '结果公告', '届满', '更正', '补充',
        '解除质押', '解除冻结', '完成公告']
NEG_ARMS = {'W2_reduce', 'W5_pledge', 'W6_frozen'}


def _note(m): print(f"[note] {m}", flush=True)


def load_cninfo() -> pd.DataFrame:
    frames = []
    for f in sorted(E_DIR.glob('*.parquet')):
        if f.name.startswith('manifest'):
            continue
        frames.append(pd.read_parquet(f))
    df = pd.concat(frames, ignore_index=True)
    df['ann_date'] = pd.to_datetime(df['ann_date'], errors='coerce')
    df = df.dropna(subset=['ann_date'])
    df['ts_code'] = df['ts_code'].astype(str).map(e49._norm_code)
    df['title'] = df['title'].fillna('')
    df = df.drop_duplicates(['ts_code', 'ann_date', 'kw'])
    df['is_drop'] = df['title'].str.contains('|'.join(DROP))
    return df


def arm_verdict(nm, st, ev, r, valid, days_idx, fwd, base):
    h20 = st.get('h20', {})
    t20 = h20.get('t_cluster', np.nan)
    neg_cons = 1 - (h20.get('year_cons', np.nan) or 0)
    st['neg_year_cons'] = neg_cons
    if nm in NEG_ARMS and st['n_events'] >= MIN_N:
        st['pool_hit_rate'] = e52.pool_hits(ev)
        if pd.notna(t20) and t20 <= -2.6 and \
                (h20.get('car_mean') or 0) < 0 and neg_cons >= 0.6:
            st['verdict'] = '强(负→veto候选)'
            if st['pool_hit_rate'] < 0.08:
                st['verdict'] = '强(宽域待载体-落池率<8%)'
        elif pd.notna(t20) and abs(t20) >= 2.0:
            st['verdict'] = '弱'
        else:
            st['verdict'] = '负'
    else:
        st['verdict'] = e29.verdict(st) if st['n_events'] >= MIN_N \
            else f'INCONCLUSIVE(n<{MIN_N})'
        if st['verdict'] == '强':
            fm = e49.fillability_filter(ev, r, valid, days_idx)
            st['fillable_ratio'] = float(fm.mean())
            if fm.sum() >= 50:
                stf = e29.arm_stats(
                    e29.car_table(ev[fm], r, valid, days_idx, fwd,
                                  base), nm + '_fillable')
                resid = stf.get('h20', {}).get('car_mean')
                orig = h20.get('car_mean')
                st['fillable_h20'] = resid
                if orig and resid is not None and abs(orig) > 1e-9:
                    st['residual_ratio'] = resid / orig
                    if abs(resid / orig) < 0.5:
                        st['verdict'] = '判强不晋级(可成交性切片死亡)'
    return st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    args = ap.parse_args()
    if not args.run:
        print("⛔ --run required (prereg)")
        return 2
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    df = load_cninfo()
    _note(f"events={len(df)} kw={df.kw.value_counts().to_dict()} "
          f"drop={df.is_drop.mean():.1%}")
    df = df[(df['ann_date'] <= SCREEN_END) & ~df['is_drop']]

    close_w = pd.read_parquet(e27.OUT_DIR / 'panel_close.parquet')
    r = daily_returns(close_w)
    valid = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    days_idx = r.index

    fwd = e29.fwd_panels(r)
    base = e29.baseline_mean(fwd, valid)
    plc = e29.placebo_gate(r, valid, days_idx, fwd, base)
    _note(f"placebo: {plc}")
    res = {'meta': {'n_raw': len(df),
                    'kw_dist': df.kw.value_counts().to_dict()},
           'placebo': plc}
    if not plc['pass']:
        (OUT_DIR / 'e56_results.json').write_text(json.dumps(
            res | {'aborted': True}, ensure_ascii=False, indent=1))
        return 3

    jd = df[df.kw == '权益变动']
    arms = {
        'W1_stakeup': jd[jd.title.str.contains('增持|举牌')],
        'W2_reduce':  df[df.kw == '减持计划'],
        'W3_increase': df[df.kw == '增持计划'],
        'W4_bonus':   df[df.kw == '高送转'],
        'W5_pledge':  df[df.kw == '股权质押'],
        'W6_frozen':  df[df.kw == '司法冻结'],
    }
    # W4 体制两期分解
    arms['W4a_pre2017'] = arms['W4_bonus'][
        arms['W4_bonus'].ann_date <= REGIME_CUT]
    arms['W4b_post2017'] = arms['W4_bonus'][
        arms['W4_bonus'].ann_date > REGIME_CUT]
    del arms['W4_bonus']

    for nm, sub in arms.items():
        ev = (sub[['ann_date', 'ts_code']]
              .rename(columns={'ann_date': 'trade_date'}))
        ev = e49.dedup_td(ev)
        if len(ev) == 0:
            res[nm] = {'arm': nm, 'n_events': 0,
                       'verdict': 'INCONCLUSIVE(n=0)'}
            _note(f"{nm}: n=0")
            continue
        car = e29.car_table(ev, r, valid, days_idx, fwd, base)
        st = e29.arm_stats(car, nm)
        st = arm_verdict(nm, st, ev, r, valid, days_idx, fwd, base)
        res[nm] = st
        _note(f"{nm}: n={st['n_events']} "
              f"h20={st.get('h20', {}).get('car_mean')} "
              f"t={st.get('h20', {}).get('t_cluster')} "
              f"v={st['verdict']}")

    res['elapsed_min'] = (time.time() - t0) / 60
    (OUT_DIR / 'e56_results.json').write_text(json.dumps(
        res, ensure_ascii=False, indent=1, default=str))
    _note(f"done {res['elapsed_min']:.1f}min")
    return 0


if __name__ == '__main__':
    sys.exit(main())
