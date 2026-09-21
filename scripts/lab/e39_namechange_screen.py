"""e39 名称变更事件族影子筛选（事件研究法）。

预登记：docs/E39_NAMECHANGE_PREREG.md（已冻结 2026-09-21）。
框架复用 e29/e30：T0=ann_date 公告日，T1=次一交易日，CAR(h)=个股窗
收益−同日全 A 有效股窗收益截面均值，h∈{1,5,10,20}。

臂：N1 摘帽全事件 / N2 戴帽全事件 / N3 摘帽严重度分层（撤销*ST vs
    撤销ST）/ N4 「其他」更名安慰剂 / N5 摘帽延迟入场（T0+5td）/
    N6 退市整理期（探索）。
方向：N1/N3/N5 正向先验；N2/N6 负向先验（判强=t_clu≤−2.6，风控剔除
候选形态）；N4 安慰剂 |t|<2.0 为过基线门。
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
from scripts.lab.e29_lhb_screen import (  # noqa: E402
    HORIZONS, car_table, arm_stats, fwd_panels, baseline_mean,
)
from scripts.lab.e30_event_screen import (  # noqa: E402
    verdict_signed, MIN_EVENTS, STRONG_T, YEAR_CONS_MIN,
)

NC_PATH = ROOT / 'data' / 'namechange' / 'namechange.parquet'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e39'

REMOVE_WARN = {'撤销*ST', '撤销ST', '撤销高风险警示'}          # 摘帽
IMPOSE_WARN = {'*ST', 'ST', '撤消*ST并实行ST', '高风险警示'}      # 戴帽
DELIST_PERIOD = {'退市整理期'}
PLACEBO = {'其他'}
SEVERE = '撤销*ST'
MILD = '撤销ST'
DEDUP_DAYS = 30          # 同股 30 日内多起事件只计第一起
DELAY_TD = 5             # N5 延迟入场（交易日）
PLACEBO_SEED = 20260921


def _note(m: str) -> None:
    print(f"[note] {m}")


def load_events() -> pd.DataFrame:
    df = pd.read_parquet(NC_PATH)
    df['T0'] = pd.to_datetime(df['ann_date'], format='%Y%m%d',
                            errors='coerce')
    n_all = len(df)
    df = df.dropna(subset=['T0'])
    _note(f"namechange {n_all} 行 → ann_date 非空 {len(df)} "
          f"（剔除 {n_all - len(df)}）")
    df['reason'] = df['change_reason'].astype(str)
    # 同股 30 日去重（防簇内重复）
    df = df.sort_values(['ts_code', 'T0'])
    keep, last = [], {}
    for i, r in enumerate(df.itertuples()):
        prev = last.get(r.ts_code)
        if prev is None or (r.T0 - prev).days > DEDUP_DAYS:
            keep.append(i)
            last[r.ts_code] = r.T0
    df = df.iloc[keep]
    _note(f"30 日去重后 {len(df)} 起事件")
    return df


def run_arm(df, name, expect, results, r, valid, days_idx, fwd, base):
    car = car_table(df[['T0', 'ts_code']].rename(columns={'T0': 'trade_date'}),
                    r, valid, days_idx, fwd, base)
    st = arm_stats(car, name)
    st['verdict'] = verdict_signed(st, expect)
    results[name] = st
    _note(f"{name}: n={st['n_events']} verdict={st['verdict']} "
          f"h20_car={st.get('h20', {}).get('car_mean')} "
          f"t_clu={st.get('h20', {}).get('t_cluster')}")
    return st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    ap.add_argument('--limit-days', type=int, default=None)
    args = ap.parse_args()
    if not args.run:
        print("⛔ --run required (prereg frozen 2026-09-21)")
        return 2

    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suf = f"_lim{args.limit_days}" if args.limit_days else ""
    if not (e27.OUT_DIR / f'panel_close{suf}.parquet').exists():
        _note("panel build (close/circ_mv)")
        e27.build_panel(args.limit_days)
    close_w = pd.read_parquet(e27.OUT_DIR / f'panel_close{suf}.parquet')
    r = daily_returns(close_w)
    valid = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    days_idx = r.index
    _note(f"panel {close_w.shape}")
    fwd = fwd_panels(r)
    base = baseline_mean(fwd, valid)

    ev = load_events()
    day_set, col_set = set(days_idx), set(r.columns)
    ev = ev[ev['T0'].isin(day_set) & ev['ts_code'].isin(col_set)]
    _note(f"events in panel: {len(ev)}")

    rm = ev[ev['reason'].isin(REMOVE_WARN)]
    im = ev[ev['reason'].isin(IMPOSE_WARN)]
    dp = ev[ev['reason'].isin(DELIST_PERIOD)]
    pb_src = ev[ev['reason'].isin(PLACEBO)]
    _note(f"counts: 摘帽={len(rm)} 戴帽={len(im)} 退市整理={len(dp)} "
          f"安慰剂源={len(pb_src)}")

    results: dict = {'meta': {'panel_days': len(days_idx),
                              'horizons': HORIZONS,
                              'min_events': MIN_EVENTS,
                              'dedup_days': DEDUP_DAYS}}

    # N4 安慰剂先行（基线证伪门）：从「其他」更名随机抽与摘帽等量
    rng = np.random.default_rng(PLACEBO_SEED)
    pb = pb_src.sample(n=min(len(rm), len(pb_src)), random_state=rng)
    car_pb = car_table(pb[['T0', 'ts_code']].rename(columns={'T0': 'trade_date'}),
                       r, valid, days_idx, fwd, base)
    st_pb = arm_stats(car_pb, 'N4_placebo_other')
    t_pb = st_pb.get('h20', {}).get('t_cluster', np.nan)
    st_pb['verdict'] = ('通过(|t|<2.0)' if (not np.isnan(t_pb) and abs(t_pb) < 2.0)
                        else '基线存疑(|t|>=2.0)')
    st_pb['n_pool'] = int(len(pb_src))
    results['N4_placebo_other'] = st_pb
    _note(f"N4 placebo: n={st_pb['n_events']} t_clu={t_pb} "
          f"verdict={st_pb['verdict']}")

    run_arm(rm, 'N1_remove_warn', +1, results, r, valid, days_idx, fwd, base)
    run_arm(im, 'N2_impose_warn', -1, results, r, valid, days_idx, fwd, base)

    # N3 摘帽严重度分层：撤销*ST vs 撤销ST
    n3 = {}
    for tp, sub in ((SEVERE, rm[rm['reason'] == SEVERE]),
                    (MILD, rm[rm['reason'] == MILD])):
        car = car_table(sub[['T0', 'ts_code']].rename(columns={'T0': 'trade_date'}),
                        r, valid, days_idx, fwd, base)
        st = arm_stats(car, f'N3_{tp}')
        st['verdict'] = verdict_signed(st, +1)
        n3[tp] = st
        _note(f"N3/{tp}: n={st['n_events']} v={st['verdict']} "
              f"h20={st.get('h20', {}).get('car_mean')}")
    sev_h20 = n3[SEVERE].get('h20', {}).get('car_mean', np.nan)
    mild_h20 = n3[MILD].get('h20', {}).get('car_mean', np.nan)
    hier = (np.isfinite(sev_h20) and np.isfinite(mild_h20)
            and sev_h20 > mild_h20)
    results['N3_severity'] = {'types': n3, 'severe_gt_mild': bool(hier),
                              'verdict': ('强' if hier and n3[SEVERE]['verdict'] == '强'
                                          else ('弱' if any(n3[t]['verdict'] != '负' for t in n3)
                                                else '负'))}

    # N5 摘帽延迟入场（T0+5td）：把 T0 平移 5 个 panel 交易日
    iloc = {d: i for i, d in enumerate(days_idx)}
    rm5 = rm.copy()
    rm5['T0'] = [days_idx[i + DELAY_TD] if (i := iloc.get(d)) is not None
                 and i + DELAY_TD < len(days_idx) else pd.NaT
                 for d in rm['T0']]
    rm5 = rm5.dropna(subset=['T0'])
    run_arm(rm5, 'N5_remove_warn_d5', +1, results, r, valid, days_idx,
            fwd, base)

    # N6 退市整理期（探索臂）
    run_arm(dp, 'N6_delist_period', -1, results, r, valid, days_idx,
            fwd, base)

    (OUT_DIR / 'e39_results.json').write_text(
        json.dumps(results, ensure_ascii=False, indent=1, default=str))
    _note(f"done {time.time()-t0:.0f}s -> {OUT_DIR/'e39_results.json'}")
    for k in ('N1_remove_warn', 'N2_impose_warn', 'N3_severity',
              'N4_placebo_other', 'N5_remove_warn_d5', 'N6_delist_period'):
        v = results.get(k, {})
        print(f"{k:20s} verdict={v.get('verdict')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
