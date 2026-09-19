#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R11：宽度阈值的经济有效性与稳定性检验（G-2d 本仓落地，只读）。

回答的问题（Hermes R9/R10 evidence/B §1.2 明确要求分开评估的两件事）：
  「G-2 判负只说明**映射**不稳健，不说明**信号**无效」
  ⇒ 本脚本检验**信号层**：宽度阈值是否承载真实前瞻收益差异，
     且该差异是否在不同子样本上稳定复现（PBO 简化版）。

输出（产物隔离）：experiments/lab/r11-breadth-threshold/r11_result.json

⚠ 口径声明：池内 487 只等权前瞻收益，未扣成本、未含择时、非基线、
   不得用于准入/报备。回测层（路径依赖）的阈值敏感性不在此脚本范围内。

用法：.venv/bin/python scripts/lab/r11_breadth_threshold_pbo_20260919.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path('/home/ubuntu/FinAI2.0')
DST = ROOT / 'data/dividend_stocks'
BREADTH = ROOT / 'experiments/lab/market-breadth-a/breadth20_daily.parquet'
OUT_DIR = ROOT / 'experiments/lab/r11-breadth-threshold'
THRESHOLDS = [0.20, 0.225, 0.25, 0.275, 0.30, 0.35]
BINS = [0, 0.15, 0.20, 0.225, 0.25, 0.275, 0.30, 0.35, 0.40, 0.50, 1.01]
LABELS = ['<15%', '15-20%', '20-22.5%', '22.5-25%', '25-27.5%', '27.5-30%',
          '30-35%', '35-40%', '40-50%', '>=50%']


def load_pool_daily() -> pd.DataFrame:
    pool = sorted([p.name for p in DST.iterdir()
                   if p.is_dir() and '.' in p.name
                   and p.name.split('.')[0] in ('sh', 'sz')])
    frames = []
    for code in pool:
        for f in sorted((DST / code).glob('20*.parquet')):
            try:
                frames.append(pd.read_parquet(f, columns=['date', 'close', 'code']))
            except Exception:
                pass
    big = pd.concat(frames, ignore_index=True)
    big['date'] = pd.to_datetime(big['date'])
    big = big[(big['date'] >= '2015-01-05') & (big['date'] <= '2024-12-31')]
    big['close'] = pd.to_numeric(big['close'], errors='coerce')
    big = big.dropna(subset=['close']).sort_values(['code', 'date'])
    big['ret'] = big.groupby('code')['close'].pct_change()
    daily = (big.groupby('date')['ret'].mean().rename('pool_ret')
             .reset_index().sort_values('date'))
    daily['fwd5'] = ((1 + daily['pool_ret']).rolling(5)
                     .apply(np.prod, raw=True).shift(-4) - 1)
    daily['fwd20'] = ((1 + daily['pool_ret']).rolling(20)
                      .apply(np.prod, raw=True).shift(-19) - 1)
    return daily


def main() -> int:
    daily = load_pool_daily()
    br = pd.read_parquet(BREADTH)
    br['date'] = pd.to_datetime(br['date'])
    m = (daily.merge(br[['date', 'breadth20']], on='date', how='inner')
         .dropna(subset=['breadth20']))
    m['b'] = m['breadth20'].astype(float)
    m['year'] = m['date'].dt.year

    # A. 分桶前瞻收益
    m['bucket'] = pd.cut(m['b'], bins=BINS, labels=LABELS, right=False)
    buckets = []
    for lab in LABELS:
        g = m[m['bucket'] == lab]
        if len(g) == 0:
            continue
        buckets.append({
            'bucket': lab, 'days': int(len(g)),
            'daily_ret_pct': round(float(g['pool_ret'].mean()) * 100, 4),
            'fwd5_pct': round(float(g['fwd5'].mean()) * 100, 4),
            'fwd20_pct': round(float(g['fwd20'].mean()) * 100, 4),
            'win5_pct': round(float((g['fwd5'] > 0).mean()) * 100, 1),
        })

    # B. 阈值两侧差
    thr_split = []
    for th in THRESHOLDS:
        lo = m[m['b'] < th]['pool_ret']
        hi = m[m['b'] >= th]['pool_ret']
        thr_split.append({
            'threshold': th, 'lo_days': int(len(lo)), 'hi_days': int(len(hi)),
            'lo_daily_pct': round(float(lo.mean()) * 100, 4),
            'hi_daily_pct': round(float(hi.mean()) * 100, 4),
            'diff_pct': round(float(hi.mean() - lo.mean()) * 100, 4),
        })

    # C. 逐年稳定性（前瞻 5 日口径）
    per_year, same_dir, tot = [], 0, 0
    for y in sorted(m['year'].unique()):
        g = m[m['year'] == y]
        lo = g[g['b'] < 0.25]['fwd5']
        hi = g[g['b'] >= 0.25]['fwd5']
        if len(lo) < 5 or len(hi) < 5:
            continue
        diff = float(hi.mean() - lo.mean())
        tot += 1
        same_dir += 1 if diff > 0 else 0
        per_year.append({'year': int(y), 'lo_n': int(len(lo)),
                         'lo_fwd5_pct': round(float(lo.mean()) * 100, 3),
                         'hi_n': int(len(hi)),
                         'hi_fwd5_pct': round(float(hi.mean()) * 100, 3),
                         'diff_pct': round(diff * 100, 3)})

    # D. 两段对照 + 固定阈值 IS/OOS
    seg = {}
    for y0, y1, name in ((2015, 2019, 'first_half'), (2020, 2024, 'second_half')):
        g = m[(m['year'] >= y0) & (m['year'] <= y1)]
        seg[name] = {
            'lo_fwd5_pct': round(float(g[g['b'] < 0.25]['fwd5'].mean()) * 100, 3),
            'hi_fwd5_pct': round(float(g[g['b'] >= 0.25]['fwd5'].mean()) * 100, 3),
        }
    is_oos = []
    tr, te = m[m['year'] <= 2019], m[m['year'] >= 2020]
    for th in THRESHOLDS:
        a = tr[tr["b"] >= th]['fwd5']
        b_ = te[te["b"] >= th]['fwd5']
        if len(a) < 20 or len(b_) < 20:
            continue
        is_oos.append({'threshold': th,
                       'is_15_19_pct': round(float(a.mean()) * 100, 3),
                       'oos_20_24_pct': round(float(b_.mean()) * 100, 3)})

    # E. PBO（随机 50/50 切分，IS 选最优阈值，看 OOS 是否跑输中位阈值）
    vals, fwd, n = m['b'].values, m['fwd5'].values, len(m)
    idx = np.arange(n)
    rng = np.random.default_rng(42)
    worse, used, runs = 0, 0, 200
    is_best_hist = []
    for _ in range(runs):
        rng.shuffle(idx)
        half = n // 2
        is_i, oos_i = idx[:half], idx[half:]
        is_best, is_best_val = None, -9e9
        for th in THRESHOLDS:
            sel = is_i[vals[is_i] >= th]
            if len(sel) < 20:
                continue
            v = fwd[sel].mean()
            if v > is_best_val:
                is_best_val, is_best = v, th
        if is_best is None:
            continue
        oos_vals = [fwd[oos_i[vals[oos_i] >= th]].mean()
                    for th in THRESHOLDS if len(oos_i[vals[oos_i] >= th]) >= 20]
        if not oos_vals:
            continue
        med = float(np.median(oos_vals))
        sel_best = oos_i[vals[oos_i] >= is_best]
        if len(sel_best) < 20:
            continue
        used += 1
        is_best_hist.append(is_best)
        if fwd[sel_best].mean() < med:
            worse += 1

    result = {
        'caliber': 'pool equal-weight forward return, 487 names, 2015-2024, '
                   'no cost, no timing; NOT a baseline',
        'sample_days': int(len(m)),
        'buckets': buckets,
        'threshold_split': thr_split,
        'per_year_stability': {'detail': per_year,
                               'same_direction': f'{same_dir}/{tot}'},
        'two_segments': seg,
        'is_oos_fixed_threshold': is_oos,
        'pbo': {'estimate': round(worse / used, 4) if used else None,
                'n_runs': used, 'thresholds': THRESHOLDS,
                'is_best_freq': {str(t): is_best_hist.count(t) for t in THRESHOLDS}},
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / 'r11_result.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())