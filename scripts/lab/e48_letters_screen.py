#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e48 监管函件事件族影子筛选（docs/E48_LETTERS_PREREG.md 冻结）。

数据源：data/letters/{type}_{year}.parquet（cninfo+交易所，采集
子会话产物；字段 ann_date/secCode/letter_type/letter_type_fine/title）。
T0=公告日，T+1 入场，h∈{1,5,10,20}，基线=同日截面均值；
同股同日多函合并取最重类。判定=负向 veto 候选。
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

L_DIR = ROOT / 'data' / 'letters'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e48'
SCREEN_END = pd.Timestamp('2024-12-31')

SEV = {'问询函': 1, '年报问询函': 1, '关注函': 2,
       '警示函': 3, '监管函': 3, '监管措施': 3, '其他': 0}


def _bare2ts(c: str) -> str:
    c = str(c).zfill(6)
    if c.startswith('6'):
        return c + '.SH'
    if c[0] in '489':
        return c + '.BJ'
    return c + '.SZ'


def _note(m): print(f"[note] {m}", flush=True)


def load_letters() -> pd.DataFrame:
    frames = []
    for f in sorted(L_DIR.rglob('*.parquet')):
        if f.stem.startswith('_'):
            continue
        d = pd.read_parquet(f)
        d.columns = [str(c).strip() for c in d.columns]
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    ren = {}
    for c in df.columns:
        if c in ('ann_date', 'announcementTime', '公告日期'):
            ren[c] = 'ann_date'
        elif c in ('secCode', 'code', 'SECURITY_CODE', 'ts_code'):
            ren[c] = 'code'
        elif c in ('letter_type', 'type', '大类'):
            ren[c] = 'ltype'
        elif c in ('letter_type_fine', '细类'):
            ren[c] = 'lfine'
    df = df.rename(columns=ren)
    df['ann_date'] = pd.to_datetime(
        df['ann_date'].astype(str).str[:10], errors='coerce')
    df = df.dropna(subset=['ann_date'])
    if 'ltype' not in df.columns:
        df['ltype'] = df.get('lfine', '其他')
    df['sev'] = df['ltype'].map(SEV).fillna(0)
    df['ts_code'] = df['code'].map(_bare2ts)
    # 同股同日多函 → 取最重类
    df = (df.sort_values('sev', ascending=False)
            .drop_duplicates(['ts_code', 'ann_date'], keep='first'))
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    args = ap.parse_args()
    if not args.run:
        print("⛔ --run required (E48 prereg frozen 2026-09-21)")
        return 2
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    df = load_letters()
    _note(f"letters raw={len(df)} types={df.ltype.value_counts().to_dict()}")
    df = df[df.ann_date <= SCREEN_END]

    close_w = pd.read_parquet(e27.OUT_DIR / 'panel_close.parquet')
    r = daily_returns(close_w)
    valid = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    days_idx = r.index
    df = df[df.ann_date.isin(set(days_idx)) & df.ts_code.isin(set(r.columns))]
    _note(f"panel-matched events={len(df)}")

    fwd = e29.fwd_panels(r)
    base = e29.baseline_mean(fwd, valid)
    plc = e29.placebo_gate(r, valid, days_idx, fwd, base)
    _note(f"placebo: {plc}")
    if not plc['pass']:
        (OUT_DIR / 'e48_results.json').write_text(json.dumps(
            {'placebo': plc, 'aborted': True}, ensure_ascii=False, indent=1))
        return 3

    res = {'meta': {'n': len(df)}, 'placebo': plc,
           'type_dist': df.ltype.value_counts().to_dict()}
    arms = {
        'K1_inquiry': df[df.sev == 1],
        'K2_attention': df[df.sev == 2],
        'K3_warning': df[df.sev == 3],
    }
    # K5 高频收函：30td 内≥2 封（按日近似：30 自然日窗口）
    df_s = df.sort_values('ann_date')
    last = {}
    k5_rows = []
    for t in df_s.itertuples():
        prev = last.get(t.ts_code)
        if prev is not None and (t.ann_date - prev).days <= 30:
            k5_rows.append(t)
        last[t.ts_code] = t.ann_date
    arms['K5_frequent'] = df.loc[[t.Index for t in k5_rows]] \
        if k5_rows else df.iloc[:0]

    for nm, ev in arms.items():
        evt = ev[['ann_date', 'ts_code']].rename(
            columns={'ann_date': 'trade_date'})
        car = e29.car_table(evt, r, valid, days_idx, fwd, base)
        st = e29.arm_stats(car, nm)
        # 负向判定：t≤−2.6 且 h20<0 且负年一致性≥0.6
        h20 = st.get('h20', {})
        t20 = h20.get('t_cluster', np.nan)
        neg_cons = 1 - (h20.get('year_cons', np.nan) or 0)
        if st['n_events'] < 300:
            st['verdict'] = 'INCONCLUSIVE(n<300)'
        elif not np.isnan(t20) and t20 <= -2.6 and \
                h20.get('car_mean', 0) < 0 and neg_cons >= 0.6:
            st['verdict'] = '强(负→veto候选)'
        elif abs(t20) >= 2.0:
            st['verdict'] = '弱'
        else:
            st['verdict'] = '负'
        st['neg_year_cons'] = neg_cons
        res[nm] = st
        _note(f"{nm}: n={st['n_events']} v={st['verdict']} "
              f"t20={t20} car20={h20.get('car_mean')}")

    # K4 严重度单调性（诊断）
    cars = [res[k].get('h20', {}).get('car_mean') for k in
            ('K1_inquiry', 'K2_attention', 'K3_warning')]
    res['K4_monotone'] = {'cars': cars,
                          'monotone': all(c is not None for c in cars) and
                          cars[0] >= cars[1] >= cars[2]}
    _note(f"K4 cars={cars} mono={res['K4_monotone']['monotone']}")

    (OUT_DIR / 'e48_results.json').write_text(json.dumps(
        res, ensure_ascii=False, indent=1, default=str))
    _note(f"done {time.time()-t0:.0f}s")
    return 0


if __name__ == '__main__':
    sys.exit(main())
