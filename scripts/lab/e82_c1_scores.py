#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e82 C1 合成信号→分数篮源面板（docs/E82_C1_SCORE_BASKET_PREREG.md 冻结）。

复用 e33_synth_screen 的信号链：S5(户数) / M5(融券) / F6(ROE波动) /
I3(行业相对21d) → 截面 z → 等权合成 → 月末 sig_date 长表。
输出: experiments/lab/e82/scores_c1.parquet (sig_date, ts_code, score)
——直通 run_score_basket_backtest --scores。

用法: .venv/bin/python -m scripts.lab.e82_c1_scores [--limit-days N]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.lab import e23_shadow_screen as e23  # noqa: E402
from scripts.lab import e25_factor_screen as e25  # noqa: E402
from scripts.lab import e26_margin_screen as e26  # noqa: E402
from scripts.lab import e27_insider_screen as e27  # noqa: E402
from scripts.lab import e28_gdhs_screen as e28  # noqa: E402
from scripts.lab import e31_candidate_crosscorr as e31  # noqa: E402
from scripts.lab.e33_synth_screen import FAMILIES, MARGIN_DIR, _z  # noqa: E402
from scripts.lab.e34_industry_screen import (  # noqa: E402
    IND_REV_WIN, load_industry_snapshots)

OUT_DIR = ROOT / 'experiments' / 'lab' / 'e82'
MARGIN_MIN_DAYS = 2300


def _note(m: str) -> None:
    print(f"[e82] {m}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit-days', type=int, default=None)
    args = ap.parse_args()
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    suf = f"_lim{args.limit_days}" if args.limit_days else ""
    close_w = pd.read_parquet(e27.OUT_DIR / f'panel_close{suf}.parquet')
    r = e23.daily_returns(close_w)
    idx = r.index
    ipo = e23.first_seen_listed(r, close_w)
    listed_ok = pd.DataFrame(False, index=idx, columns=r.columns)
    ipo_map = {c: pd.to_datetime(v) for c, v in ipo.items() if v}
    for c in r.columns:
        t0i = ipo_map.get(c)
        if t0i is None:
            continue
        listed_ok[c] = ((idx >= t0i).cumsum() - 1) >= e23.MIN_LISTED_DAYS
    _note(f"panel {close_w.shape}")

    pos = pd.Series(np.arange(len(idx)), index=idx)
    Ts = [T for T in e23.month_ends(idx)
          if int(pos[T]) + e23.HORIZON <= len(idx) - 1]

    # ---- 信号构件（与 e33 同源 ctx） ----
    long = e28.load_gdhs_records()
    gdhs_vis = e28.visible_signal(long, list(Ts))
    pit_cache = e25.load_pit()
    pit = {T: e25.pit_snapshot(T, pit_cache) for T in Ts}
    cs = np.log1p(r.fillna(0.0)).cumsum()
    ret21 = np.expm1(cs - cs.shift(IND_REV_WIN))
    snaps = load_industry_snapshots()

    m5 = None
    margin_dates = None
    n_margin = len(list(MARGIN_DIR.glob('*.parquet')))
    if n_margin >= MARGIN_MIN_DAYS:
        P26 = e26.load_panels(args.limit_days)
        m5 = e26.margin_signal_frames(P26)['short_chg20']
        margin_dates = P26['fin_balance'].index
        _note(f"margin_days={len(margin_dates)} M5 入合成")
    else:
        _note(f"margin_detail 仅 {n_margin} 日 → 三族合成")

    ctx31 = {'gdhs_vis': gdhs_vis, 'pit': pit, 'h2': ret21, 'm5': m5,
             'margin_dates': margin_dates, 'snaps': snaps, 'ret21': ret21,
             'cols': r.columns,
             'six2ts': {t.split('.')[0]: t for t in r.columns}}

    active_fams = [f for f in FAMILIES if m5 is not None or f != 'M5']
    out_rows: list[dict] = []
    for T in Ts:
        base_ok = listed_ok.loc[T] & close_w.loc[T].notna()
        sigs = e31.build_signals(T, ctx31)
        zs = {}
        for f in active_fams:
            s = sigs.get(f)
            if s is None:
                continue
            z = _z(s, base_ok)
            if z.notna().sum() >= 30:
                zs[f] = z
        if len(zs) < 2:
            continue
        comp = pd.concat(zs, axis=1).mean(axis=1).dropna()
        comp = comp[base_ok.reindex(comp.index, fill_value=False)]
        for ts, v in comp.items():
            out_rows.append({'sig_date': T.date(), 'ts_code': ts,
                             'score': float(v)})
        _note(f"{T.date()} comp n={len(comp)} fams={len(zs)} "
              f"cum={len(out_rows)} t={time.time()-t0:.0f}s")

    df = pd.DataFrame(out_rows)
    out = OUT_DIR / 'scores_c1.parquet'
    df.to_parquet(out, index=False)
    _note(f"-> {out} rows={len(df)} sigs={df.sig_date.nunique()}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
