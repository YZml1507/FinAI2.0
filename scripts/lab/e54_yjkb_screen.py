#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e54 业绩快报「真超预期」事件族（docs/E54_YJKB_PREREG.md 冻结）。

yjkb=快报（公告日期 PIT 锚，净利润-同比增长）；yjyg=预告（判定
同股同期次是否有在先预告）。T0=公告日，T+1 入场，h∈{1,5,10,20}。
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
from scripts.lab import e43_forecast_screen as e43  # noqa: E402
from scripts.lab import e49_analyst_screen as e49  # noqa: E402
from scripts.lab import e52_resumption_screen as e52  # noqa: E402

KB_DIR = ROOT / 'data' / 'forecast_em' / 'yjkb'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e54'
SCREEN_END = pd.Timestamp('2024-12-31')
MIN_N = 300
DEDUP_TD = 20
HI_GROWTH = 50.0    # 净利同比 ≥+50%
LO_GROWTH = -50.0

_NEG_FORECAST = {'预减', '首亏', '续亏', '略减', '增亏'}


def _note(m): print(f"[note] {m}", flush=True)


def load_yjkb() -> pd.DataFrame:
    frames = []
    for f in sorted(KB_DIR.glob('*.parquet')):
        d = pd.read_parquet(f)
        d.columns = [str(c).strip() for c in d.columns]
        d['end_date'] = pd.to_datetime(f.stem, errors='coerce')
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    df = df.rename(columns={'公告日期': 'ann_date', '股票代码': 'code',
                            '净利润-同比增长': 'np_yoy'})
    df['ann_date'] = pd.to_datetime(df['ann_date'], errors='coerce')
    df = df.dropna(subset=['ann_date'])
    df['ts_code'] = df['code'].map(e43._bare2ts)
    df['np_yoy'] = pd.to_numeric(df['np_yoy'], errors='coerce')
    df = df.dropna(subset=['np_yoy'])
    # 同股同期取最早公告
    df = (df.sort_values('ann_date')
            .drop_duplicates(['ts_code', 'end_date'], keep='first'))
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    args = ap.parse_args()
    if not args.run:
        print("⛔ --run required (prereg frozen 2026-09-22)")
        return 2
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    kb = load_yjkb()
    _note(f"yjkb events={len(kb)} coverage={kb.ann_date.min().date()}→"
          f"{kb.ann_date.max().date()}")

    yg = e43.load_em()
    yg_pair = set(zip(yg.ts_code, yg.end_date)) if yg is not None else set()
    yg_neg_pair = set(zip(yg[yg.ftype.isin(_NEG_FORECAST)].ts_code,
                        yg[yg.ftype.isin(_NEG_FORECAST)].end_date)) \
        if yg is not None else set()
    kb['has_fc'] = [p in yg_pair for p in zip(kb.ts_code, kb.end_date)]
    kb['neg_fc'] = [p in yg_neg_pair for p in zip(kb.ts_code, kb.end_date)]
    _note(f"有在先预告占比: {kb['has_fc'].mean():.1%} "
          f"其中负类预告: {kb['neg_fc'].mean():.2%}")

    close_w = pd.read_parquet(e27.OUT_DIR / 'panel_close.parquet')
    r = daily_returns(close_w)
    valid = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    days_idx = r.index

    kb = kb[kb['ann_date'] <= SCREEN_END]

    fwd = e29.fwd_panels(r)
    base = e29.baseline_mean(fwd, valid)
    plc = e29.placebo_gate(r, valid, days_idx, fwd, base)
    _note(f"placebo: {plc}")
    res = {'meta': {'yjkb_n': len(kb),
                    'has_fc_ratio': float(kb['has_fc'].mean())},
           'placebo': plc}
    if not plc['pass']:
        (OUT_DIR / 'e54_results.json').write_text(json.dumps(
            res | {'aborted': True}, ensure_ascii=False, indent=1))
        return 3

    hi = kb['np_yoy'] >= HI_GROWTH
    arms = {
        'K1_hi_noFC':  kb[hi & ~kb['has_fc']],
        'K2_hi_hasFC': kb[hi & kb['has_fc']],
        'K3_lo':       kb[kb['np_yoy'] <= LO_GROWTH],
        'K4_hi_negFC': kb[hi & kb['neg_fc']],
    }
    for nm, sub in arms.items():
        ev = (sub[['ann_date', 'ts_code']]
              .rename(columns={'ann_date': 'trade_date'}))
        ev = e49.dedup_td(ev)
        if len(ev) == 0:
            res[nm] = {'arm': nm, 'n_events': 0,
                       'verdict': 'INCONCLUSIVE(n=0)'}
            _note(f"{nm}: n=0")
            continue
        hit = e52.pool_hits(ev)
        car = e29.car_table(ev, r, valid, days_idx, fwd, base)
        st = e29.arm_stats(car, nm)
        st['pool_hit_rate'] = hit
        h20 = st.get('h20', {})
        t20 = h20.get('t_cluster', np.nan)
        neg_cons = 1 - (h20.get('year_cons', np.nan) or 0)
        st['neg_year_cons'] = neg_cons
        if st['n_events'] < MIN_N:
            st['verdict'] = f'INCONCLUSIVE(n<{MIN_N})'
        elif pd.notna(t20) and t20 <= -2.6 and \
                (h20.get('car_mean') or 0) < 0 and neg_cons >= 0.6:
            st['verdict'] = '强(负→veto候选)'
            if hit < 0.08:
                st['verdict'] = '强(宽域待载体-落池率<8%)'
        elif pd.notna(t20) and t20 >= 2.6 and \
                (h20.get('car_mean') or 0) > 0 and \
                (h20.get('year_cons') or 0) >= 0.6:
            st['verdict'] = '强'
            # 可成交性切片
            mask = e49.fillability_filter(ev, r, valid, days_idx)
            st['fillable_ratio'] = float(mask.mean())
            if mask.sum() >= 50:
                stf = e29.arm_stats(
                    e29.car_table(ev[mask], r, valid, days_idx, fwd, base),
                    nm + '_fillable')
                resid = stf.get('h20', {}).get('car_mean')
                orig = h20.get('car_mean')
                st['fillable_h20'] = resid
                if orig and resid is not None and abs(orig) > 1e-9:
                    st['residual_ratio'] = resid / orig
                    if abs(resid / orig) < 0.5:
                        st['verdict'] = '判强不晋级(可成交性切片死亡)'
        elif pd.notna(t20) and abs(t20) >= 2.0:
            st['verdict'] = '弱'
        else:
            st['verdict'] = '负'
        res[nm] = st
        _note(f"{nm}: n={st['n_events']} hit={hit:.1%} "
              f"h20={h20.get('car_mean')} t={t20} v={st['verdict']}")

    res['elapsed_min'] = (time.time() - t0) / 60
    (OUT_DIR / 'e54_results.json').write_text(json.dumps(
        res, ensure_ascii=False, indent=1, default=str))
    _note(f"done {res['elapsed_min']:.1f}min")
    return 0


if __name__ == '__main__':
    sys.exit(main())
