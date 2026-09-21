"""e30 回购×解禁事件族影子筛选（事件研究法）。

预登记：docs/E30_BUYBACK_UNLOCK_PREREG.md（已冻结 2026-09-21）。
复用 e29 事件研究框架：T0=事件日（回购预案 DIM_DATE / 解禁 FREE_DATE），
T1=次一交易日收盘起算，CAR(h)=个股窗收益−同日全A中位，h∈{1,5,10,20}。

臂：R1 回购预案全事件 / R2 预案规模(JEXX/cmv)三分层 / R3 完成实施(FINISHDATE)
    R4 解禁全事件 / R5 解禁规模(TOTAL_RATIO)三分层 / R6 解禁类型分化。
方向：回购臂先验正漂移；解禁臂先验负漂移（判「强」= t_clu≤−2.6，
落地形态=风控剔除过滤器候选）。
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
    HORIZONS, car_table, arm_stats,
)

REP_PATH = ROOT / 'data' / 'e30_repurchase' / 'e30_repurchase.parquet'
UNL_PATH = ROOT / 'data' / 'e30_restricted' / 'e30_restricted.parquet'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e30'

MIN_EVENTS = 300
STRONG_T = 2.6
YEAR_CONS_MIN = 0.60
UNLOCK_TYPES = ('定向增发机构配售股份', '首发原股东限售股份', '股权激励限售股份')


def _note(m: str) -> None:
    print(f"[note] {m}")


def verdict_signed(arm: dict, expect: int) -> str:
    """expect=+1 正向假设 / −1 负向假设（解禁）。负向判强 t_clu≤−2.6。"""
    h20 = arm.get('h20', {})
    n = arm.get('n_events', 0)
    if n < MIN_EVENTS:
        return 'INCONCLUSIVE(样本不足)'
    t = h20.get('t_cluster', np.nan)
    if np.isnan(t):
        return 'INCONCLUSIVE'
    dirs = [arm.get(f'h{h}', {}).get('car_mean', np.nan) for h in HORIZONS]
    dirs = [d for d in dirs if not np.isnan(d)]
    same_dir = len(dirs) > 0 and (all(d > 0 for d in dirs) or all(d < 0 for d in dirs))
    if expect > 0:
        strong = (t >= STRONG_T and h20.get('car_mean', 0) > 0 and same_dir
                  and h20.get('year_cons', 0) >= YEAR_CONS_MIN)
    else:
        strong = (t <= -STRONG_T and h20.get('car_mean', 0) < 0 and same_dir)
    if strong:
        return '强'
    if abs(t) >= 2.0:
        return '弱'
    return '负'


def norm_code(raw: pd.Series) -> pd.Series:
    cr = raw.astype(str).str.extract(r'(\d{6})')[0]
    exch = np.where(cr.str.startswith('6'), 'SH',
                    np.where(cr.str[0].isin(['4', '8', '9']), 'BJ', 'SZ'))
    return cr + '.' + exch


def load_repurchase() -> pd.DataFrame:
    r = pd.read_parquet(REP_PATH)
    r['T0'] = pd.to_datetime(r['DIM_DATE'], errors='coerce')
    r['ts_code'] = norm_code(r['DIM_SCODE'])
    r['jexx'] = pd.to_numeric(r['JEXX'], errors='coerce')
    r['finish'] = pd.to_datetime(r['FINISHDATE'], errors='coerce')
    r['done'] = r['REPURPROGRESS_TEXT'].astype(str).str.contains('完成实施')
    return r.dropna(subset=['T0', 'ts_code'])


def load_restricted() -> pd.DataFrame:
    u = pd.read_parquet(UNL_PATH)
    u['T0'] = pd.to_datetime(u['FREE_DATE'], errors='coerce')
    u['ts_code'] = norm_code(u['SECURITY_CODE'])
    u['total_ratio'] = pd.to_numeric(u['TOTAL_RATIO'], errors='coerce')
    u['share_type'] = u['FREE_SHARES_TYPE'].astype(str)
    return u.dropna(subset=['T0', 'ts_code'])


def tercile_arms(df: pd.DataFrame, intensity: pd.Series,
                 r, valid, days_idx) -> dict:
    out = {}
    df = df.assign(intensity=intensity).dropna(subset=['intensity'])
    if len(df) < 3:
        return {'verdict': 'INCONCLUSIVE(样本不足)', 'n_events': int(len(df))}
    try:
        lab = pd.qcut(df['intensity'], 3, labels=['lo', 'mid', 'hi'])
    except ValueError:
        return {'verdict': 'INCONCLUSIVE(分层失败)', 'n_events': int(len(df))}
    df = df.assign(terc=lab)
    stats = {}
    for g in ('lo', 'mid', 'hi'):
        car = car_table(df[df['terc'] == g][['T0', 'ts_code']]
                        .rename(columns={'T0': 'trade_date'}),
                        r, valid, days_idx)
        stats[g] = arm_stats(car, f'terc_{g}')
    means = [stats[g].get('h20', {}).get('car_mean', np.nan) for g in ('lo', 'mid', 'hi')]
    mono = all(not np.isnan(m) for m in means)
    mono_up = mono and means[0] <= means[1] <= means[2]
    mono_dn = mono and means[0] >= means[1] >= means[2]
    return {'terciles': stats, 'means_h20': means,
            'monotone_up': mono_up, 'monotone_dn': mono_dn,
            'n_events': int(len(df))}


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
    _note("panel (close/circ_mv)")
    e27.build_panel(args.limit_days)
    suf = f"_lim{args.limit_days}" if args.limit_days else ""
    close_w = pd.read_parquet(e27.OUT_DIR / f'panel_close{suf}.parquet')
    cmv_w = pd.read_parquet(e27.OUT_DIR / f'panel_circ_mv{suf}.parquet')
    r = daily_returns(close_w)
    valid = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    days_idx = r.index
    _note(f"panel {close_w.shape}")

    rep = load_repurchase()
    unl = load_restricted()
    day_set, col_set = set(days_idx), set(r.columns)
    rep = rep[rep['T0'].isin(day_set) & rep['ts_code'].isin(col_set)]
    unl = unl[unl['T0'].isin(day_set) & unl['ts_code'].isin(col_set)]
    _note(f"events: rep={len(rep)} unl={len(unl)}")

    results: dict = {'meta': {'panel_days': len(days_idx), 'horizons': HORIZONS,
                              'min_events': MIN_EVENTS}}

    def run_arm(df, name, expect):
        car = car_table(df[['T0', 'ts_code']].rename(columns={'T0': 'trade_date'}),
                        r, valid, days_idx)
        st = arm_stats(car, name)
        st['verdict'] = verdict_signed(st, expect)
        results[name] = st
        _note(f"{name}: n={st['n_events']} verdict={st['verdict']} "
              f"h20_car={st.get('h20', {}).get('car_mean')} "
              f"t_clu={st.get('h20', {}).get('t_cluster')}")
        return st

    # R1 回购预案
    run_arm(rep, 'R1_rep_announce', +1)
    # R2 规模分层（预案金额下限/流通市值T0）
    cmv_by = {d: cmv_w.loc[d] for d in rep['T0'].unique() if d in cmv_w.index}
    inten = []
    for ev in rep.itertuples():
        s = cmv_by.get(ev.T0)
        c = np.nan if s is None else s.get(ev.ts_code, np.nan)
        inten.append(ev.jexx / (c * 1e4) if c and c > 0 else np.nan)
    res2 = tercile_arms(rep, pd.Series(inten, index=rep.index), r, valid, days_idx)
    top = res2.get('terciles', {}).get('hi', {})
    res2['verdict'] = ('强' if res2.get('monotone_up') and
                       verdict_signed(top, +1) == '强'
                       else ('弱' if verdict_signed(top, +1) != '负' else '负'))
    results['R2_rep_size'] = res2
    _note(f"R2: monotone_up={res2.get('monotone_up')} verdict={res2['verdict']}")

    # R3 完成实施（FINISHDATE 事件日）
    rep_done = rep[rep['done']].dropna(subset=['finish']).copy()
    rep_done['T0'] = rep_done['finish']
    rep_done = rep_done[rep_done['T0'].isin(day_set)]
    run_arm(rep_done, 'R3_rep_complete', +1)

    # R4 解禁全事件
    run_arm(unl, 'R4_unlock_all', -1)
    # R5 解禁规模分层（TOTAL_RATIO 三分位）
    res5 = tercile_arms(unl, unl['total_ratio'], r, valid, days_idx)
    top5 = res5.get('terciles', {}).get('hi', {})
    res5['verdict'] = ('强' if res5.get('monotone_dn') and
                       verdict_signed(top5, -1) == '强'
                       else ('弱' if verdict_signed(top5, -1) != '负' else '负'))
    results['R5_unlock_size'] = res5
    _note(f"R5: monotone_dn={res5.get('monotone_dn')} verdict={res5['verdict']}")

    # R6 类型分化
    r6 = {}
    for tp in UNLOCK_TYPES:
        sub = unl[unl['share_type'] == tp]
        car = car_table(sub[['T0', 'ts_code']].rename(columns={'T0': 'trade_date'}),
                        r, valid, days_idx)
        st = arm_stats(car, f'R6_{tp}')
        st['verdict'] = verdict_signed(st, -1)
        r6[tp] = st
        _note(f"R6/{tp}: n={st['n_events']} v={st['verdict']} "
              f"h20={st.get('h20', {}).get('car_mean')}")
    best_neg = min((r6[t].get('h20', {}).get('car_mean', np.inf) for t in UNLOCK_TYPES))
    results['R6_unlock_type'] = {'types': r6,
                                 'verdict': '强' if any(r6[t]['verdict'] == '强' for t in UNLOCK_TYPES) else
                                 ('弱' if any(r6[t]['verdict'] == '弱' for t in UNLOCK_TYPES) else '负'),
                                 'most_negative_h20': float(best_neg) if np.isfinite(best_neg) else None}

    (OUT_DIR / 'e30_results.json').write_text(
        json.dumps(results, ensure_ascii=False, indent=1, default=str))
    _note(f"done {time.time()-t0:.0f}s -> {OUT_DIR/'e30_results.json'}")
    for k in ('R1_rep_announce', 'R2_rep_size', 'R3_rep_complete',
              'R4_unlock_all', 'R5_unlock_size', 'R6_unlock_type'):
        v = results.get(k, {})
        print(f"{k:16s} n={v.get('n_events', 0):6d} verdict={v.get('verdict')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
