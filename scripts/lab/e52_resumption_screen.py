#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e52 停牌复牌事件族影子筛选（docs/E52_RESUMPTION_PREREG.md 冻结）。

停牌=panel_close 连续 NaN 段（上市首交易日之前的不算）。
复牌 T0=连续 NaN≥L 后首个非 NaN 交易日；同股事件间隔 ≥20td。
h∈{1,5,10,20}，基线=同日截面均值，安慰剂门同源 e29。
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

OUT_DIR = ROOT / 'experiments' / 'lab' / 'e52'
MIN_N = 300
DEDUP_TD = 20


def _note(m): print(f"[note] {m}", flush=True)


def resumption_events(close_w: pd.DataFrame,
                      lo: int, hi: int) -> pd.DataFrame:
    """NaN 连续段∈[lo,hi] 后首个非 NaN 日=复牌 T0。
    附 gap_ret=T0 收盘/停牌前末收 −1（复牌跳空幅度，R4 分层用）。"""
    days = close_w.index
    events = []
    for sym in close_w.columns:
        s = close_w[sym].values
        nan = np.isnan(s)
        fv = np.argmax(~nan)           # 首个非 NaN（上市日）
        if not (~nan).any():
            continue
        run, prev_close = 0, np.nan
        for i in range(fv, len(s)):
            if nan[i]:
                run += 1
            else:
                if lo <= run <= hi:
                    gap = (s[i] / prev_close - 1) \
                        if pd.notna(prev_close) and prev_close > 0 else np.nan
                    events.append((days[i], sym, gap))
                prev_close = s[i]
                run = 0
    ev = pd.DataFrame(events, columns=['trade_date', 'ts_code', 'gap_ret'])
    return ev


def pool_hits(ev: pd.DataFrame) -> float:
    from scripts.lab.e36_e37_overlay_ab import _universe_symbols, _ts2bs
    uni_ts = set()
    for s in _universe_symbols():
        # bs→ts
        uni_ts.add(s[3:] + ('.SH' if s.startswith('sh')
                            else '.SZ' if s.startswith('sz') else '.BJ'))
    if len(ev) == 0:
        return 0.0
    return float(ev['ts_code'].isin(uni_ts).mean())


def dedup_td(ev: pd.DataFrame) -> pd.DataFrame:
    ev = ev.sort_values(['ts_code', 'trade_date'])
    keep, last = [], {}
    for i, t in enumerate(ev.itertuples()):
        p = last.get(t.ts_code)
        if p is not None and (t.trade_date - p).days <= DEDUP_TD:
            continue
        keep.append(i)
        last[t.ts_code] = t.trade_date
    return ev.iloc[keep]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    args = ap.parse_args()
    if not args.run:
        print("⛔ --run required (prereg frozen 2026-09-21)")
        return 2
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    close_w = pd.read_parquet(e27.OUT_DIR / 'panel_close.parquet')
    r = daily_returns(close_w)
    valid = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    days_idx = r.index
    _note(f"panel {close_w.shape}")

    ev_all = resumption_events(close_w, 5, 10 ** 9)
    _note(f"resumption ≥5td events={len(ev_all)}")

    fwd = e29.fwd_panels(r)
    base = e29.baseline_mean(fwd, valid)
    plc = e29.placebo_gate(r, valid, days_idx, fwd, base)
    _note(f"placebo: {plc}")
    res = {'meta': {'n_resumption_5p': len(ev_all)}, 'placebo': plc}
    if not plc['pass']:
        (OUT_DIR / 'e52_results.json').write_text(json.dumps(
            res | {'aborted': True}, ensure_ascii=False, indent=1))
        return 3

    # R4 复牌跳空（对停牌前末收）≤−5%——停期含除权事件会污染 gap，
    # 属已知噪声如实披露；停牌期间日日收益本身不可定义。
    ev_20p = resumption_events(close_w, 20, 10 ** 9)
    r4 = ev_20p[pd.to_numeric(ev_20p['gap_ret'], errors='coerce') <= -0.05]
    _note(f"R4 gap≤-5% events={len(r4)} (gap 中位 "
          f"{ev_20p['gap_ret'].median():.2%})")
    arms = {
        'R1_susp60p':  resumption_events(close_w, 60, 10 ** 9),
        'R2_susp20_59': resumption_events(close_w, 20, 59),
        'R3_susp5_19': resumption_events(close_w, 5, 19),
        'R4_gap_dn5': r4,
    }
    for nm, ev in arms.items():
        ev = dedup_td(ev)
        hit = pool_hits(ev)
        if len(ev) == 0:
            res[nm] = {'arm': nm, 'n_events': 0, 'pool_hit_rate': 0.0,
                       'verdict': 'INCONCLUSIVE(n=0)'}
            _note(f"{nm}: n=0")
            continue
        car = e29.car_table(ev, r, valid, days_idx, fwd, base)
        st = e29.arm_stats(car, nm)
        st['pool_hit_rate'] = hit
        # 负向判定（e48 同构）：t≤−2.6 + h20<0 + 负年一致性≥0.6
        h20 = st.get('h20', {})
        t20 = h20.get('t_cluster', np.nan)
        neg_cons = 1 - (h20.get('year_cons', np.nan) or 0)
        st['neg_year_cons'] = neg_cons
        if st['n_events'] < MIN_N:
            st['verdict'] = f'INCONCLUSIVE(n<{MIN_N})'
        elif not np.isnan(t20) and t20 <= -2.6 and \
                (h20.get('car_mean') or 0) < 0 and neg_cons >= 0.6:
            st['verdict'] = '强(负→veto候选)'
            # 池内命中率前置门（e50 元教训）
            if hit < 0.08:
                st['verdict'] = '强(宽域待载体-落池率<8%)'
        elif pd.notna(t20) and abs(t20) >= 2.0:
            st['verdict'] = '弱'
        else:
            st['verdict'] = '负'
        res[nm] = st
        _note(f"{nm}: n={st['n_events']} pool_hit={hit:.1%} "
              f"h20={h20.get('car_mean')} t={h20.get('t_cluster')} "
              f"v={st['verdict']}")

    res['elapsed_min'] = (time.time() - t0) / 60
    (OUT_DIR / 'e52_results.json').write_text(json.dumps(
        res, ensure_ascii=False, indent=1, default=str))
    _note(f"done {res['elapsed_min']:.1f}min")
    return 0


if __name__ == '__main__':
    sys.exit(main())
