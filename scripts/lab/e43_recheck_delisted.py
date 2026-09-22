#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e43 退市股缺口复检臂（docs/E43_FORECAST_PREREG.md §六登记随访）。

动机：EM yjyg 库剔除大部分退市股（抽查 16/21 NULL）→ 负向臂
F2/F3 的退市段截面缺失 = 偏低估。cninfo 逐股补采的退市股预告
公告落地后（data/delisted_forecast/{code}.parquet），把缺失事件
合并进 EM 事件集，重跑 F2/F3（含安慰剂门与可成交性切片复算），
判断缺口修正是否翻转既有裁决。

标题→预告类型归并（冻结口径，不扩臂）：
 预增/扭亏/略增 → POS；预减/首亏/续亏/略减/增亏 → NEG；
 减亏/续盈/不确定/其他 → NEU（沿用冻结稿：不分类不投）。
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

DL_DIR = ROOT / 'data' / 'delisted_forecast'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e43'

_TYPE_KW = [
    ('预增', '预增'), ('扭亏', '扭亏'), ('略增', '略增'),
    ('预减', '预减'), ('首亏', '首亏'), ('续亏', '续亏'),
    ('略减', '略减'), ('增亏', '增亏'), ('减亏', '减亏'),
    ('续盈', '续盈'), ('不确定', '不确定'),
]


def _note(m): print(f"[note] {m}", flush=True)


def classify_title(title: str) -> str | None:
    t = str(title)
    for kw, typ in _TYPE_KW:
        if kw in t:
            return typ
    return None


def load_delisted() -> pd.DataFrame | None:
    """cninfo 退市股预告补采：{ann_date, ts_code(裸码), title, ...}。"""
    if not DL_DIR.exists():
        return None
    frames = []
    for f in sorted(DL_DIR.glob('*.parquet')):
        d = pd.read_parquet(f)
        d.columns = [str(c).strip() for c in d.columns]
        frames.append(d)
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df['ann_date'] = pd.to_datetime(df['ann_date'], errors='coerce')
    df = df.dropna(subset=['ann_date'])
    df['ts_code'] = df['ts_code'].map(e43._bare2ts)
    df['ftype'] = df['title'].map(classify_title)
    n_none = int(df['ftype'].isna().sum())
    _note(f"delisted raw={len(df)} 归并可分类={len(df)-n_none} "
          f"未分类标题={n_none}")
    df = df.dropna(subset=['ftype'])
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    args = ap.parse_args()
    if not args.run:
        print("⛔ --run required（复检臂，沿用 e43 冻结口径）")
        return 2
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    em = e43.load_em()
    if em is None:
        _note("⛔ EM 源缺失")
        return 3
    dl = load_delisted()
    if dl is None or dl.empty:
        _note("⛔ 退市股补采未落地")
        return 3

    # 合并：退市补采行补 end_date=NaT（dedup 按 ann_date 维度即可——
    # 退市股与 EM 有交集时按同股同公告日去重）
    dl = dl[['ann_date', 'ts_code', 'ftype']].copy()
    dl['end_date'] = pd.NaT
    em_cols = ['ann_date', 'ts_code', 'ftype', 'end_date']
    merged = pd.concat([em[em_cols], dl], ignore_index=True)
    merged = (merged.sort_values('ann_date')
                    .drop_duplicates(['ts_code', 'ann_date'], keep='first'))
    n_new = len(merged) - len(em.drop_duplicates(['ts_code', 'ann_date']))
    _note(f"merged events={len(merged)}（EM {len(em)} + 退市增量 {n_new}）")

    close_w = pd.read_parquet(e27.OUT_DIR / 'panel_close.parquet')
    r = daily_returns(close_w)
    valid = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    days_idx = r.index

    merged = merged[merged['ann_date'] <= e43.SCREEN_END]
    merged = e43.dedup_events(merged, set(r.columns))
    _note(f"screen-window dedup events={len(merged)}")

    fwd = e29.fwd_panels(r)
    base = e29.baseline_mean(fwd, valid)
    plc = e29.placebo_gate(r, valid, days_idx, fwd, base)
    _note(f"placebo: {plc}")
    if not plc['pass']:
        (OUT_DIR / 'e43_recheck_results.json').write_text(json.dumps(
            {'placebo': plc, 'aborted': True}, ensure_ascii=False, indent=1))
        return 3

    merged['event_date'] = merged['ann_date']
    ft = merged['ftype'].astype(str)
    arms = {
        'F2_neg_recheck': ft.isin(e43.NEG_TYPES),
        'F3_first_loss_recheck': ft.eq('首亏'),
        'F1_pos_recheck': ft.isin(e43.POS_TYPES),   # 对照：正向臂应近似不变
    }
    res = {'meta': {'em_events': len(em), 'delisted_added': int(n_new),
                    'merged_events': len(merged)},
           'placebo': plc}
    for nm, m in arms.items():
        ev = (merged[m][['ann_date', 'ts_code']]
              .rename(columns={'ann_date': 'trade_date'}))
        car = e29.car_table(ev, r, valid, days_idx, fwd, base)
        st = e29.arm_stats(car, nm)
        st['verdict'] = e29.verdict(st) if st['n_events'] >= e43.ALLA_MIN_N \
            else f"INCONCLUSIVE(n<{e43.ALLA_MIN_N})"
        res[nm] = st
        _note(f"{nm}: n={st['n_events']} h20={st.get('h20')} "
              f"t={st.get('t_clu')} verdict={st['verdict']}")

    res['elapsed_min'] = (time.time() - t0) / 60
    (OUT_DIR / 'e43_recheck_results.json').write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=str))
    _note(f"done {res['elapsed_min']:.1f}min")
    return 0


if __name__ == '__main__':
    sys.exit(main())
