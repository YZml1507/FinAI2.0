#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e43 业绩预告事件族影子筛选（docs/E43_FORECAST_PREREG.md 冻结）。

数据源二择一：
 ① EM 全 A：data/forecast_em/{yjyg|yjkb}_<period>.parquet（公告日期列
    为 PIT 锚；退市股覆盖缺口如存在，按冻结稿如实登记偏置方向：
    负向臂低估→veto 判定偏保守）；
 ② 池内降级：data/forecast_pit/{code}.parquet（487 池，
    pub_date/first_ann_date 锚，判强上限弱 unless t≥4.0）。

T0=公告日，T+1 收盘入场，h∈{1,5,10,20}；同股同报告期多次预告取
first_ann_date；同股 20 交易日内重复公告 DEDUP_DAYS=20 去重。
筛选窗 ≤2024-12-31；2025+ 登记 OOS。
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

EM_DIR = ROOT / 'data' / 'forecast_em'
PIT_DIR = ROOT / 'data' / 'forecast_pit'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e43'
SCREEN_END = pd.Timestamp('2024-12-31')
DEDUP_DAYS = 20
POOL_MIN_N = 150
ALLA_MIN_N = 300

POS_TYPES = ('预增', '扭亏', '略增')
NEG_TYPES = ('预减', '首亏', '续亏', '略减', '增亏')
NEU_TYPES = ('不确定', '其他')


def _note(m): print(f"[note] {m}", flush=True)


def _bare2ts(code: str) -> str:
    c = str(code).zfill(6)
    if c.startswith('6'):
        return c + '.SH'
    if c[0] in '489':
        return c + '.BJ'
    return c + '.SZ'


def load_em() -> pd.DataFrame | None:
    """EM 全 A 预告：预期列名（采集后如实适配）。
    东财 yjyg 列通常：股票代码/股票简称/预测指标/业绩变动/
    预测数值/业绩变动幅度(下限/上限)/预告类型/公告日期。"""
    if not EM_DIR.exists():
        return None
    frames = []
    for f in sorted(EM_DIR.glob('yjyg_*.parquet')):
        d = pd.read_parquet(f)
        d.columns = [str(c).strip() for c in d.columns]
        frames.append(d)
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    # 列名适配（容忍东财列名差异）
    ren = {}
    for c in df.columns:
        if '公告日期' in c or c == 'ann_date':
            ren[c] = 'ann_date'
        elif c in ('股票代码', '代码', 'secCode', 'SECURITY_CODE'):
            ren[c] = 'code'
        elif '预告类型' in c or c == 'type' or '业绩变动' in c and '幅度' not in c:
            ren[c] = 'ftype'
        elif '报告期' in c or c == 'end_date':
            ren[c] = 'end_date'
        elif '幅度' in c and ('下' in c or 'min' in c.lower()):
            ren[c] = 'p_min'
        elif '幅度' in c and ('上' in c or 'max' in c.lower()):
            ren[c] = 'p_max'
    df = df.rename(columns=ren)
    req = {'ann_date', 'code', 'ftype'}
    if not req.issubset(df.columns):
        _note(f"EM cols unresolved: {sorted(df.columns)}")
        return None
    df['ann_date'] = pd.to_datetime(df['ann_date'], errors='coerce')
    df = df.dropna(subset=['ann_date'])
    df['ts_code'] = df['code'].map(_bare2ts)
    if 'end_date' in df.columns:
        df['end_date'] = pd.to_datetime(df['end_date'], errors='coerce')
    return df


def load_pit() -> pd.DataFrame:
    frames = []
    for f in sorted(PIT_DIR.glob('*.parquet')):
        d = pd.read_parquet(f)
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    df.columns = [str(c).strip() for c in df.columns]
    df['ann_date'] = pd.to_datetime(
        df.get('first_ann_date', df.get('pub_date')), errors='coerce')
    df = df.dropna(subset=['ann_date'])
    if 'code' not in df.columns and 'ts_code' in df.columns:
        df['code'] = df['ts_code']
    df['ts_code'] = df['code'].map(_bare2ts) \
        if not df['code'].astype(str).str.contains('.').all() \
        else df['code']
    df['ftype'] = df.get('type', df.get('ftype'))
    return df


def dedup_events(df: pd.DataFrame, panel_cols: set) -> pd.DataFrame:
    """同股同报告期去重（取最早公告）+ 同股20日内重复公告去重。"""
    df = df.sort_values('ann_date')
    if 'end_date' in df.columns:
        df = (df.sort_values('ann_date')
                .drop_duplicates(['ts_code', 'end_date'], keep='first'))
    keep_idx, last_seen = [], {}
    for i, t in enumerate(df.itertuples()):
        if t.ts_code not in panel_cols:
            continue
        prev = last_seen.get(t.ts_code)
        if prev is not None and (t.ann_date - prev).days <= DEDUP_DAYS:
            continue
        keep_idx.append(i)
        last_seen[t.ts_code] = t.ann_date
    return df.iloc[keep_idx]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    ap.add_argument('--source', choices=['auto', 'em', 'pit'],
                    default='auto')
    args = ap.parse_args()
    if not args.run:
        print("⛔ --run required (prereg frozen 2026-09-21)")
        return 2
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    df = None
    src_used = None
    if args.source in ('auto', 'em'):
        df = load_em()
        src_used = 'em' if df is not None else None
    if df is None and args.source in ('auto', 'pit'):
        df = load_pit()
        src_used = 'pit'
    if df is None:
        _note("⛔ 无可用数据源")
        return 3
    _note(f"source={src_used} raw={len(df)}")
    min_n = POOL_MIN_N if src_used == 'pit' else ALLA_MIN_N

    close_w = pd.read_parquet(e27.OUT_DIR / 'panel_close.parquet')
    r = daily_returns(close_w)
    valid = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    days_idx = r.index
    _note(f"panel {close_w.shape}")

    df = df[df['ann_date'] <= SCREEN_END]
    df = dedup_events(df, set(r.columns))
    _note(f"screen-window dedup events={len(df)}")

    fwd = e29.fwd_panels(r)
    base = e29.baseline_mean(fwd, valid)
    plc = e29.placebo_gate(r, valid, days_idx, fwd, base)
    _note(f"placebo: {plc}")
    if not plc['pass']:
        (OUT_DIR / 'e43_results.json').write_text(json.dumps(
            {'placebo': plc, 'aborted': True}, ensure_ascii=False, indent=1))
        return 3

    df['event_date'] = df['ann_date']
    def arm_ev(mask):
        return (df[mask][['ann_date', 'ts_code']]
                .rename(columns={'ann_date': 'trade_date'}))

    ft = df['ftype'].astype(str)
    arms = {
        'F1_pos': ft.isin(POS_TYPES),
        'F2_neg': ft.isin(NEG_TYPES),
        'F3_first_loss': ft.eq('首亏'),
        'F4_neutral': ft.isin(NEU_TYPES),
    }
    res = {'meta': {'source': src_used, 'min_n': min_n},
           'placebo': plc,
           'type_dist': df['ftype'].astype(str).value_counts().to_dict()}
    for nm, m in arms.items():
        ev = arm_ev(m)
        car = e29.car_table(ev, r, valid, days_idx, fwd, base)
        st = e29.arm_stats(car, nm)
        n_ok = st['n_events'] >= min_n
        st['verdict'] = e29.verdict(st) if n_ok else \
            f'INCONCLUSIVE(n<{min_n})'
        if src_used == 'pit' and st['verdict'] == '强':
            t20 = abs(st.get('h20', {}).get('t_cluster', 0) or 0)
            if t20 < 4.0:
                st['verdict'] = '弱(池内口径封顶)'
        res[nm] = st
        _note(f"{nm}: n={st['n_events']} v={st['verdict']} "
              f"t20={st.get('h20', {}).get('t_cluster')}")

    # F5 幅度分层（诊断臂）
    if 'p_min' in df.columns:
        f1 = df[ft.isin(POS_TYPES)].copy()
        f1['p_min'] = pd.to_numeric(f1['p_min'], errors='coerce')
        for nm, m in (('big', f1['p_min'] >= 100), ('mid', f1['p_min'].between(30, 100))):
            ev = f1[m][['ann_date', 'ts_code']].rename(
                columns={'ann_date': 'trade_date'})
            res[f'F5_{nm}'] = e29.arm_stats(e29.car_table(ev, r, valid,
                                          days_idx, fwd, base), f'F5_{nm}')
            _note(f"F5_{nm}: {res[f'F5_{nm}'].get('h20', {})}")

    (OUT_DIR / 'e43_results.json').write_text(json.dumps(
        res, ensure_ascii=False, indent=1, default=str))
    _note(f"done {time.time()-t0:.0f}s")
    return 0


if __name__ == '__main__':
    sys.exit(main())
