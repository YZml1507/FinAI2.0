#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e76 目标价事件族筛选（docs/E76_TARGET_PRICE_PREREG.md 冻结）。

数据源：data/analyst/research_report_em_*.parquet（同 e49 原料，
indvAimPriceT/L 原始列）。aim_mid=(T+L)/2，仅单边取非空端。
T0=publishDate，T+1 收盘入场，h∈{1,5,10,20}，基线=同日截面均值。
P2/P3 负先验走 direction='neg'（veto 语义），P1/P4 正。
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

A_DIR = ROOT / 'data' / 'analyst'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e76'
SCREEN_END = pd.Timestamp('2024-12-31')


def _note(m): print(f"[note] {m}", flush=True)


def load_reports() -> pd.DataFrame:
    fs = sorted(A_DIR.glob('research_report_em_*.parquet'))
    cols = ['stockCode', 'publishDate', 'indvAimPriceT', 'indvAimPriceL',
            'emRatingName', 'orgSName']
    df = pd.concat([pd.read_parquet(f, columns=cols) for f in fs])
    df['publishDate'] = pd.to_datetime(df['publishDate'])
    for c in ('indvAimPriceT', 'indvAimPriceL'):
        df[c] = pd.to_numeric(df[c].replace('', np.nan), errors='coerce')
    df['aim_mid'] = df[['indvAimPriceT', 'indvAimPriceL']].mean(axis=1)
    df = df[df['aim_mid'] > 0]
    df['ts_code'] = df['stockCode'].map(e49._norm_code)
    return df.sort_values(['ts_code', 'publishDate'])


def build_arms(df: pd.DataFrame, close_w: pd.DataFrame) -> dict:
    g = df.groupby(['ts_code', 'orgSName'])['aim_mid']
    chg = g.pct_change()
    df = df.assign(aim_chg=chg)
    ev = {}
    p1 = df[df['aim_chg'] >= 0.10]
    p2 = df[df['aim_chg'] <= -0.10]
    ev['P1_up'] = p1[['publishDate', 'ts_code']].rename(
        columns={'publishDate': 'trade_date'})
    ev['P2_down'] = p2[['publishDate', 'ts_code']].rename(
        columns={'publishDate': 'trade_date'})
    # P3 隐含空间 top20%（截面年内分位）
    cl = close_w
    days = cl.index
    idx = {d: i for i, d in enumerate(days)}
    px = []
    for r_ in df.itertuples():
        i = idx.get(r_.publishDate.normalize())
        j = i if i is not None else None
        if j is None or r_.ts_code not in cl.columns:
            px.append(np.nan)
            continue
        px.append(cl.iloc[max(j - 1, 0)][r_.ts_code])
    df['px_t0'] = px
    df['implied'] = df['aim_mid'] / df['px_t0'] - 1
    d3 = df.dropna(subset=['implied'])
    d3 = d3[(d3['implied'] <= 4.0) & (d3['implied'] >= -1.0)]  # 脏值剔除
    rk = d3.groupby(d3['publishDate'].dt.year)['implied'].rank(pct=True)
    p3 = d3[rk >= 0.8]
    ev['P3_upside'] = p3[['publishDate', 'ts_code']].rename(
        columns={'publishDate': 'trade_date'})
    # P4 首次目标价覆盖：该股 180 日内首条 aim 行
    df = df.sort_values(['ts_code', 'publishDate'])
    last = {}
    keep = []
    for t in df.itertuples():
        prev = last.get(t.ts_code)
        if prev is None or (t.publishDate - prev).days > 180:
            keep.append(True)
        else:
            keep.append(False)
        last[t.ts_code] = t.publishDate
    p4 = df[pd.Series(keep, index=df.index)]
    ev['P4_first'] = p4[['publishDate', 'ts_code']].rename(
        columns={'publishDate': 'trade_date'})
    for k in ev:
        ev[k] = ev[k][ev[k]['trade_date'] <= SCREEN_END]
    return ev, int((d3['implied'].isna()).sum())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    args = ap.parse_args()
    if not args.run:
        print("⛔ --run required (E76 prereg frozen 2026-09-23)")
        return 2
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    df = load_reports()
    _note(f"aim reports={len(df)} codes={df['ts_code'].nunique()}")

    close_w = pd.read_parquet(e27.OUT_DIR / 'panel_close_2025.parquet')
    r = daily_returns(close_w)
    valid = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    days_idx = r.index

    ev, dirty = build_arms(df, close_w)
    _note(f"dirty dropped={dirty} arms=" +
          {k: len(v) for k, v in ev.items()}.__str__())

    fwd = e29.fwd_panels(r)
    base = e29.baseline_mean(fwd, valid)
    plc = e29.placebo_gate(r, valid, days_idx, fwd, base)
    _note(f"placebo: {plc}")
    res = {'meta': {'n_reports': len(df), 'dirty': dirty},
           'placebo': plc}
    if not plc['pass']:
        res['aborted'] = True
    else:
        for name, direc in (('P1_up', 'pos'), ('P2_down', 'neg'),
                            ('P3_upside', 'neg'), ('P4_first', 'pos')):
            e49.run_arm(name, ev[name], r, valid, days_idx, fwd, base,
                        res, direction=direc)
    (OUT_DIR / 'e76_results.json').write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=str))
    _note(f"done {(time.time() - t0) / 60:.1f}min")
    return 0


if __name__ == '__main__':
    sys.exit(main())
