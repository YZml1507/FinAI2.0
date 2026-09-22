#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e55 研报标题文本事件族（docs/E55_TITLE_PREREG.md 冻结）。

title 关键词 → 事件集；T0=publishDate，T+1 收盘入场，
h∈{1,5,10,20}，基线=同日截面均值。同股 20td 去重。
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
from scripts.lab import e29_lhb_screen as e29  # noqa: E402
from scripts.lab import e49_analyst_screen as e49  # noqa: E402
from scripts.lab import e52_resumption_screen as e52  # noqa: E402

OUT_DIR = ROOT / 'experiments' / 'lab' / 'e55'
SCREEN_END = pd.Timestamp('2024-12-31')

KW = {
    'T1_low':  ['低于预期', '不及预期', '低于我们预期'],
    'T2_beat': ['超预期', '超出预期', '高于预期'],
    'T3_turn': ['拐点'],
    'T4_first': ['首次覆盖', '首次评级'],
}


def _note(m): print(f"[note] {m}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    args = ap.parse_args()
    if not args.run:
        print("⛔ --run required (prereg frozen 2026-09-22)")
        return 2
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(glob.glob(str(ROOT / 'data/analyst/'
                                 'research_report_em_*.parquet')))
    df = pd.concat([pd.read_parquet(f, columns=['title', 'stockCode',
                                                'publishDate'])
                    for f in files], ignore_index=True)
    df['ann_date'] = pd.to_datetime(df['publishDate'], errors='coerce')
    df = df.dropna(subset=['ann_date'])
    df = df[df['ann_date'] <= SCREEN_END]
    df['ts_code'] = df['stockCode'].map(e49._norm_code)
    df['title'] = df['title'].fillna('')
    # 同股同日多研报取首条
    df = (df.sort_values('ann_date')
          .drop_duplicates(['ts_code', 'ann_date'], keep='first'))
    _note(f"rows={len(df)}")

    close_w = pd.read_parquet(e27.OUT_DIR / 'panel_close.parquet')
    r = daily_returns(close_w)
    valid = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    days_idx = r.index

    fwd = e29.fwd_panels(r)
    base = e29.baseline_mean(fwd, valid)
    plc = e29.placebo_gate(r, valid, days_idx, fwd, base)
    _note(f"placebo: {plc}")
    res = {'meta': {'n_rows': len(df)}, 'placebo': plc}
    if not plc['pass']:
        (OUT_DIR / 'e55_results.json').write_text(json.dumps(
            res | {'aborted': True}, ensure_ascii=False, indent=1))
        return 3

    for nm, kws in KW.items():
        mask = np.zeros(len(df), dtype=bool)
        for kw in kws:
            mask |= df['title'].str.contains(kw, regex=False).values
        sub = df[mask]
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
        direction = 'neg' if nm == 'T1_low' else 'pos'
        h20 = st.get('h20', {})
        t20 = h20.get('t_cluster', np.nan)
        neg_cons = 1 - (h20.get('year_cons', np.nan) or 0)
        st['neg_year_cons'] = neg_cons
        if direction == 'neg' and st['n_events'] >= 300:
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
            st['verdict'] = e29.verdict(st) if st['n_events'] >= 300 \
                else 'INCONCLUSIVE(n<300)'
            if st['verdict'] == '强':
                fmask = e49.fillability_filter(ev, r, valid, days_idx)
                st['fillable_ratio'] = float(fmask.mean())
                if fmask.sum() >= 50:
                    stf = e29.arm_stats(
                        e29.car_table(ev[fmask], r, valid, days_idx,
                                      fwd, base), nm + '_fillable')
                    resid = stf.get('h20', {}).get('car_mean')
                    orig = h20.get('car_mean')
                    st['fillable_h20'] = resid
                    if orig and resid is not None and abs(orig) > 1e-9:
                        st['residual_ratio'] = resid / orig
                        if abs(resid / orig) < 0.5:
                            st['verdict'] = '判强不晋级(可成交性切片死亡)'
        res[nm] = st
        _note(f"{nm}: n={st['n_events']} h20={h20.get('car_mean')} "
              f"t={t20} v={st['verdict']}")

    res['elapsed_min'] = (time.time() - t0) / 60
    (OUT_DIR / 'e55_results.json').write_text(json.dumps(
        res, ensure_ascii=False, indent=1, default=str))
    _note(f"done {res['elapsed_min']:.1f}min")
    return 0


if __name__ == '__main__':
    sys.exit(main())
