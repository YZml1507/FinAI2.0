#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e53 ESOP/股权激励/定增公告事件族（docs/E53_ESOP_PREREG.md）。

data/esop_events/{kw}_{YYYYMM}.parquet: {ann_date, ts_code, kw,
title, url}。T0=公告日，T+1 收盘入场，h∈{1,5,10,20}。
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

E_DIR = ROOT / 'data' / 'esop_events'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e53'
SCREEN_END = pd.Timestamp('2024-12-31')
MIN_N = 300

# 过程性披露剔除词（非事件起点）
PROCEDURAL = ['进展公告', '实施完成', '届满', '结果公告',
              '获得批复', '核准批复', '反馈意见', '回复']
TERMINATE = ['终止', '失效', '停止实施']


def _note(m): print(f"[note] {m}", flush=True)


def load_esop() -> pd.DataFrame:
    frames = []
    for f in sorted(E_DIR.glob('*.parquet')):
        if f.name == 'manifest.jsonl':
            continue
        frames.append(pd.read_parquet(f))
    df = pd.concat(frames, ignore_index=True)
    df['ann_date'] = pd.to_datetime(df['ann_date'], errors='coerce')
    df = df.dropna(subset=['ann_date'])
    df['ts_code'] = df['ts_code'].astype(str).map(e49._norm_code)
    df['title'] = df['title'].fillna('')
    df = df.drop_duplicates(['ts_code', 'ann_date', 'kw'])
    df['is_term'] = df['title'].str.contains('|'.join(TERMINATE))
    df['is_proc'] = df['title'].str.contains('|'.join(PROCEDURAL))
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    args = ap.parse_args()
    if not args.run:
        print("⛔ --run required (prereg)")
        return 2
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    df = load_esop()
    _note(f"events={len(df)} kw={df.kw.value_counts().to_dict()} "
          f"term={df.is_term.mean():.1%} proc={df.is_proc.mean():.1%}")
    df = df[df['ann_date'] <= SCREEN_END]

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
        (OUT_DIR / 'e53_results.json').write_text(json.dumps(
            res | {'aborted': True}, ensure_ascii=False, indent=1))
        return 3

    act = df[~df['is_proc'] & ~df['is_term']]       # 事件起点
    arms = {
        'S1_esop':  act[act.kw == '员工持股计划'],
        'S2_incentive': act[act.kw == '股权激励'],
        'S3_pp':    act[act.kw.isin(['定向增发', '非公开发行'])],
        'S4_term':  df[df['is_term']],
    }
    # S5 同股90日内≥2条ESOP/激励
    es = act[act.kw.isin(['员工持股计划', '股权激励'])]
    rows = []
    for sym, g in es.groupby('ts_code'):
        ds = np.sort(g['ann_date'].values)
        for i, d in enumerate(ds):
            if ((ds > d - np.timedelta64(90, 'D')) & (ds < d)).sum() >= 1:
                rows.append({'trade_date': pd.Timestamp(d),
                             'ts_code': sym})
    arms['S5_repeat90'] = pd.DataFrame(rows) if rows else \
        pd.DataFrame(columns=['trade_date', 'ts_code'])

    for nm, sub in arms.items():
        ev = (sub[['ann_date', 'ts_code']]
              .rename(columns={'ann_date': 'trade_date'})
              if 'ann_date' in sub.columns else sub)
        ev = e49.dedup_td(ev)
        if len(ev) == 0:
            res[nm] = {'arm': nm, 'n_events': 0,
                       'verdict': 'INCONCLUSIVE(n=0)'}
            _note(f"{nm}: n=0")
            continue
        car = e29.car_table(ev, r, valid, days_idx, fwd, base)
        st = e29.arm_stats(car, nm)
        h20 = st.get('h20', {})
        t20 = h20.get('t_cluster', np.nan)
        neg_cons = 1 - (h20.get('year_cons', np.nan) or 0)
        st['neg_year_cons'] = neg_cons
        if nm in ('S3_pp', 'S4_term') and st['n_events'] >= MIN_N:
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
                            st['verdict'] = \
                                '判强不晋级(可成交性切片死亡)'
        res[nm] = st
        _note(f"{nm}: n={st['n_events']} h20={h20.get('car_mean')} "
              f"t={t20} v={st['verdict']}")

    res['elapsed_min'] = (time.time() - t0) / 60
    (OUT_DIR / 'e53_results.json').write_text(json.dumps(
        res, ensure_ascii=False, indent=1, default=str))
    _note(f"done {res['elapsed_min']:.1f}min")
    return 0


if __name__ == '__main__':
    sys.exit(main())
