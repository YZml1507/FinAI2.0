"""e40 指数定期调样事件族影子筛选（事件研究法）。

预登记：docs/E40_INDEXREBAL_PREREG.md（已冻结 2026-09-21）。
框架复用 e29/e30/e39：T0=review_date 公告日，T1=次一交易日，
CAR(h)=个股窗收益−同日全 A 有效股窗收益截面均值，h∈{1,5,10,20}
+ 专项窗 T+1→生效日（ann→eff 文献主效应段）。
筛选窗：review_date ≤ 2024-12-31（2025+ 期次登记为 OOS 不入筛选）。

臂：R1 调入沪深300 / R2 调入中证500 / R3 调出300+500 /
    R4 调入中证红利（小样本登记）/ R5 备选名单弱安慰剂 /
    R6 随机(日,股)安慰剂 3 重复核。
方向：R1/R2/R4 正向；R3 负向；R5 |t|<2.0 过门（≥2.0→随机复核预案）；
    R6 基线无偏复核。
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

IR_DIR = ROOT / 'data' / 'index_rebal'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e40'
SCREEN_END = pd.Timestamp('2024-12-31')   # 筛选窗硬上界
PLACEBO_SEED = 20260921


def _note(m: str) -> None:
    print(f"[note] {m}")


def _bare2ts(code: str) -> str:
    c = str(code).zfill(6)
    if c.startswith(('60', '68', '90')):
        return f'{c}.SH'
    if c.startswith(('00', '30', '20')):
        return f'{c}.SZ'
    if c.startswith(('43', '83', '87', '88', '92')):
        return f'{c}.BJ'
    return f'{c}.SH'


def load_events() -> pd.DataFrame:
    df = pd.read_parquet(IR_DIR / '_all.parquet')
    df['T0'] = pd.to_datetime(df['review_date'])
    df['eff'] = pd.to_datetime(df['effective_date'])
    df['ts_code'] = df['stock_code'].map(_bare2ts)
    n_all = len(df)
    df = df.dropna(subset=['T0'])
    _note(f"index_rebal {n_all} 行 → review_date 非空 {len(df)}")
    oos = df[df['T0'] > SCREEN_END]
    _note(f"OOS(2025+) 期次 {oos['T0'].nunique()} 期 {len(oos)} 行"
          f"——登记不入筛选")
    df = df[df['T0'] <= SCREEN_END]
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


def ann_to_eff_car(df, r, valid, days_idx):
    """专项窗：T+1 收盘 → 生效日收盘 的 CAR（逐事件窗长不同）。"""
    iloc = {d: i for i, d in enumerate(days_idx)}
    cars = []
    for t in df.itertuples():
        i0 = iloc.get(t.T0)
        if i0 is None or i0 + 1 >= len(days_idx):
            continue
        # 生效日映射到 panel 中 ≥eff 的最近交易日
        j = np.searchsorted(days_idx.values, np.datetime64(t.eff))
        if j >= len(days_idx) or j <= i0:
            continue
        sym = t.ts_code
        if sym not in r.columns or not valid.iloc[i0 + 1][sym]:
            continue
        seg = r[sym].iloc[i0 + 1: j + 1]
        base_seg = r.iloc[i0 + 1: j + 1][valid.iloc[i0 + 1: j + 1]].mean(axis=1)
        if len(seg) < 3 or seg.isna().all():
            continue
        cars.append(float((seg.fillna(0) - base_seg.fillna(0)).sum()))
    a = np.array(cars)
    return {'n': len(a), 'car_mean': float(a.mean()) if len(a) else None,
            't_naive': float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a))))
            if len(a) > 2 and a.std(ddof=1) > 0 else None}


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
    pfile = e27.OUT_DIR / f'panel_close{suf}.parquet'
    if not pfile.exists():
        _note("panel build (close/circ_mv)")
        e27.build_panel(args.limit_days)
    close_w = pd.read_parquet(pfile)
    r = daily_returns(close_w)
    valid = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    days_idx = r.index
    _note(f"panel {close_w.shape}")
    fwd = fwd_panels(r)
    base = baseline_mean(fwd, valid)

    ev = load_events()
    col_set = set(r.columns)
    ev = ev[ev['T0'].isin(set(days_idx)) & ev['ts_code'].isin(col_set)]
    _note(f"events in panel(screen window): {len(ev)}")

    a300 = ev[(ev.index_code == '000300') & (ev.action == 'add')]
    a500 = ev[(ev.index_code == '000905') & (ev.action == 'add')]
    rem = ev[(ev.index_code.isin(['000300', '000905']))
             & (ev.action == 'remove')]
    adv = ev[(ev.index_code == '000922') & (ev.action == 'add')]
    stb = ev[ev.action == 'standby']
    _note(f"counts: 300调入={len(a300)} 500调入={len(a500)} "
          f"调出={len(rem)} 红利调入={len(adv)} 备选={len(stb)}")

    results: dict = {'meta': {'panel_days': len(days_idx),
                              'horizons': list(HORIZONS) + ['ann2eff'],
                              'min_events': MIN_EVENTS,
                              'screen_end': str(SCREEN_END.date())}}

    # R5 备选名单弱安慰剂先行（存疑门）
    st5 = run_arm(stb, 'R5_standby_placebo', +1, results, r, valid,
                  days_idx, fwd, base)
    t5 = st5.get('h20', {}).get('t_cluster', np.nan)
    st5['verdict_gate'] = ('通过(|t|<2.0)' if (not np.isnan(t5) and abs(t5) < 2.0)
                           else '存疑(|t|>=2.0)→需随机复核')

    run_arm(a300, 'R1_add_000300', +1, results, r, valid, days_idx, fwd, base)
    run_arm(a500, 'R2_add_000905', +1, results, r, valid, days_idx, fwd, base)
    run_arm(rem, 'R3_remove_300500', -1, results, r, valid, days_idx, fwd, base)
    run_arm(adv, 'R4_add_000922', +1, results, r, valid, days_idx, fwd, base)

    # 专项窗 ann→eff
    for nm, sub in (('R1', a300), ('R2', a500), ('R3', rem)):
        results[f'{nm}_ann2eff'] = ann_to_eff_car(sub, r, valid, days_idx)
        _note(f"{nm} ann→eff: {results[f'{nm}_ann2eff']}")

    # RD 诊断：同指数同期 add − standby CAR 差（h20，报告量）
    for idx_code, arm_nm in (('000300', 'R1'), ('000905', 'R2')):
        ad = ev[(ev.index_code == idx_code) & (ev.action == 'add')]
        sb = ev[(ev.index_code == idx_code) & (ev.action == 'standby')]
        both = []
        for per, g in ad.groupby(ad.T0.dt.to_period('M')):
            gs = sb[sb.T0.dt.to_period('M') == per]
            if len(gs) == 0:
                continue
            ca = car_table(g[['T0', 'ts_code']].rename(columns={'T0': 'trade_date'}),
                           r, valid, days_idx, fwd, base)
            cs = car_table(gs[['T0', 'ts_code']].rename(columns={'T0': 'trade_date'}),
                           r, valid, days_idx, fwd, base)
            if 'car20' in ca.columns and 'car20' in cs.columns:
                both.append(ca['car20'].mean() - cs['car20'].mean())
        if both:
            b = np.array(both)
            results[f'{arm_nm}_rd_diff'] = {
                'n_periods': len(b), 'diff_mean': float(b.mean()),
                't_naive': float(b.mean() / (b.std(ddof=1) / np.sqrt(len(b))))
                if len(b) > 2 and b.std(ddof=1) > 0 else None}
            _note(f"{arm_nm} RD diff(add-standby h20): "
                  f"{results[f'{arm_nm}_rd_diff']}")

    # R6 随机(日,股)安慰剂 3 rep
    rng = np.random.default_rng(PLACEBO_SEED)
    reps = []
    days_arr = np.array(days_idx)
    cols = np.array(r.columns)
    for rep in range(3):
        n = len(a300)
        dd = pd.DataFrame({'trade_date': rng.choice(days_arr, n),
                           'ts_code': rng.choice(cols, n)})
        car = car_table(dd, r, valid, days_idx, fwd, base)
        st = arm_stats(car, f'R6_rep{rep}')
        reps.append({'n': st['n_events'],
                     'h20': st.get('h20', {}).get('car_mean'),
                     't': st.get('h20', {}).get('t_cluster')})
    results['R6_random_placebo'] = reps
    _note(f"R6 random placebo: {reps}")

    (OUT_DIR / 'e40_results.json').write_text(
        json.dumps(results, ensure_ascii=False, indent=1, default=str))
    _note(f"done {time.time()-t0:.0f}s -> {OUT_DIR/'e40_results.json'}")
    for k in ('R1_add_000300', 'R2_add_000905', 'R3_remove_300500',
              'R4_add_000922', 'R5_standby_placebo'):
        print(f"{k:20s} verdict={results.get(k, {}).get('verdict')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
