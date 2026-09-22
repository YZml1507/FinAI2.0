#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e41 指数调样事件篮子袖珍仓评估（docs/E41_EVENT_SLEEVE_PREREG.md）。

逐期篮子：T+1 收等权买入调入股 → 生效日收卖出。
剔除：T+1 涨停（|ret|≥9.8%/19.8%）或无 bar 停牌股。
净收益 = 篮子 raw 收益 − 0.25% 往返成本。
闲时 GC001（袖珍仓口径只评占用期，闲时收益另列）。
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

from scripts.lab import e40_indexrebal_screen as e40  # noqa: E402
from scripts.lab import e27_insider_screen as e27  # noqa: E402
from scripts.lab.e23_shadow_screen import daily_returns  # noqa: E402

OUT_DIR = ROOT / 'experiments' / 'lab' / 'e41'
RT_COST = 0.0025          # 往返净扣 0.25%
LIMIT_PCT = {'sh': 0.098, 'sz': 0.098, 'bj': 0.19}  # 简化：主板9.8%
GC001 = ROOT / 'data' / 'rates' / 'gc001_daily.parquet'


def _note(m): print(f"[note] {m}", flush=True)


def sleeve_events(idx_code: str | tuple, r, close_w, days_idx):
    ev = e40.load_events()
    codes = (idx_code,) if isinstance(idx_code, str) else idx_code
    adds = ev[(ev.index_code.isin(list(codes))) & (ev.action == 'add')]
    iloc = {d: i for i, d in enumerate(days_idx)}
    daysv = days_idx.values
    per = []
    for t0, g in adds.groupby('T0'):
        i0 = iloc.get(t0)
        if i0 is None or i0 + 1 >= len(days_idx):
            continue
        eff = g.eff.iloc[0]
        j = np.searchsorted(daysv, np.datetime64(eff))
        if j >= len(daysv) or j <= i0:
            continue
        e_day = days_idx[i0 + 1]           # T+1 入场日
        x_day = days_idx[j]                # 生效日（卖出日）
        kept, dropped = [], []
        for t in g.itertuples():
            sym = t.ts_code
            if sym not in r.columns:
                dropped.append((sym, 'no_bar'))
                continue
            e_ret = r.iloc[i0 + 1][sym]
            if pd.isna(e_ret):
                dropped.append((sym, 'suspended'))
                continue
            lim = LIMIT_PCT['sh'] if sym.endswith('.SH') else \
                (LIMIT_PCT['bj'] if sym.endswith('.BJ') else LIMIT_PCT['sz'])
            if e_ret >= lim:
                dropped.append((sym, 'limit_up'))
                continue
            seg = r[sym].iloc[i0 + 1: j + 1]
            if seg.isna().all():
                dropped.append((sym, 'no_fwd'))
                continue
            kept.append({'sym': sym,
                         'ret': float((1 + seg.fillna(0)).prod() - 1)})
        if len(kept) < 5:
            continue
        gross = float(np.mean([k['ret'] for k in kept]))
        per.append({'idx': idx_code, 'T0': str(t0.date()),
                    'eff': str(x_day.date()), 'win_td': int(j - i0),
                    'n_list': len(g), 'n_kept': len(kept),
                    'n_drop': len(dropped),
                    'gross': gross, 'net': gross - RT_COST})
    return per


def stats(per: list[dict]) -> dict:
    if not per:
        return {}
    d = pd.DataFrame(per)
    d['T0'] = pd.to_datetime(d['T0'])
    net = d['net']
    n = len(net)
    t = float(net.mean() / (net.std(ddof=1) / np.sqrt(n))) \
        if n > 1 and net.std(ddof=1) > 0 else np.nan
    pos_frac = float((net > 0).mean())
    post = net[d['T0'] >= pd.Timestamp('2019-01-01')]
    post_ok = len(post) > 0 and float(post.mean()) > 0
    # 占用期年化：sum(net)/占用交易日 ×252 + 闲时 GC001 近似
    occ_td = int(d['win_td'].sum())
    occ_ann = float(net.sum() / occ_td * 252) if occ_td else np.nan
    # 年化全仓口径：每年 ~2 期 × 净
    per_year = d.groupby(d['T0'].dt.year)['net'].sum()
    return {'n_events': n, 'mean_net': float(net.mean()),
            't_cluster': t, 'pos_frac': pos_frac,
            'post2019_mean': float(post.mean()) if len(post) else None,
            'occ_td_total': occ_td,
            'occ_annualized': occ_ann,
            'sum_by_year': {int(k): float(v) for k, v in per_year.items()},
            'n_kept_med': float(d['n_kept'].median())}


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
    days_idx = r.index
    _note(f"panel {close_w.shape}")

    res = {}
    for code, nm in (('000905', 'S1_add_500'), ('000300', 'S2_add_300')):
        per = sleeve_events(code, r, close_w, days_idx)
        res[nm] = {'events': per, 'stats': stats(per)}
        s = res[nm]['stats']
        _note(f"{nm}: n={s.get('n_events')} net={s.get('mean_net')} "
              f"t={s.get('t_cluster')} pos={s.get('pos_frac')} "
              f"post19={s.get('post2019_mean')}")

    # S3 合并：同日 300+500 名单并为单篮子（同 T0 簇，不重复计期次）
    s3 = sleeve_events(('000300', '000905'), r, close_w, days_idx)
    res['S3_merged'] = {'events': s3, 'stats': stats(s3)}
    s = res['S3_merged']['stats']
    _note(f"S3: n={s.get('n_events')} net={s.get('mean_net')} "
          f"t={s.get('t_cluster')}")

    # 判读（冻结门）：net_mean>0 & t>=2.6 & pos_frac>=0.6 & post2019>0
    for nm in ('S1_add_500', 'S2_add_300', 'S3_merged'):
        s = res[nm]['stats']
        ok = (s.get('t_cluster', 0) or 0) >= 2.6 and \
            (s.get('pos_frac', 0) or 0) >= 0.6 and \
            (s.get('post2019_mean') or 0) > 0
        s['verdict'] = '成立' if ok else '负'
        _note(f"{nm} verdict={s['verdict']}")

    (OUT_DIR / 'e41_results.json').write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=str))
    _note(f"done {time.time()-t0:.0f}s")
    return 0


if __name__ == '__main__':
    sys.exit(main())
