#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e49 分析师评级/盈利预测事件族影子筛选（docs/E49_ANALYST_PREREG.md 冻结）。

数据源：data/analyst/（东财研报库，列名以采集 manifest 为准适配到
规范列：ts_code/ann_date/kind('rating'|'forecast')/rating_score
（上调=+1 下调=-1）/fy_np_chg（预测净利环比变化率）/org）。
T0=披露日，T+1 收盘入场，h∈{1,5,10,20}，基线=同日截面均值。
同股同日多条取变动最大者；同股 20td 去重。筛选窗 ≤2024-12-31。

判强门（冻结）：|t_clu|≥2.6 + 方向合先验 + 年一致性≥60% + n≥300。
判强者必跑可成交性切片（剔除 T+1 涨停/停牌事件）：残余 <50%
原效应 → 降「判强不晋级」。
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

A_DIR = ROOT / 'data' / 'analyst'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e49'
SCREEN_END = pd.Timestamp('2024-12-31')
MIN_N = 300
DEDUP_TD = 20
RESIDUAL_GATE = 0.5   # 可成交切片残余 <50% 原效应 → 判强不晋级

# 评级文本→方向（东财口径为主，采集映射如不同按 manifest 改此处）
RATING_UP = {'买入', '增持', '强烈推荐', '推荐', '强于大市', '跑赢行业'}
RATING_DN = {'中性', '减持', '卖出', '回避', '弱于大市', '跑输行业'}


def _note(m): print(f"[note] {m}", flush=True)


def _norm_code(code: str) -> str:
    c = str(code).split('.')[0].zfill(6)
    if c.startswith('6'):
        return c + '.SH'
    if c[0] in '489':
        return c + '.BJ'
    return c + '.SZ'


def load_analyst() -> pd.DataFrame | None:
    """归一化到 {ts_code, ann_date, kind, rating_dir, fy_np_chg}。
    kind='rating'：rating_dir=+1 上调 / -1 下调（同股同日取 |dir| 最大）；
    kind='forecast'：fy_np_chg=预测净利环比变化率(%, 对上一期同机构预测)。
    列名按采集 manifest 适配——未落地返回 None。"""
    if not A_DIR.exists():
        return None
    files = sorted(A_DIR.glob('*.parquet'))
    if not files:
        return None
    raise NotImplementedError(
        "data/analyst/ 已存在——按实际 schema 实现列映射后重跑")


def fillability_filter(ev: pd.DataFrame, r: pd.DataFrame,
                       valid: pd.DataFrame,
                       days_idx: pd.DatetimeIndex) -> pd.Series:
    """True=可成交（T+1 未涨停且未停牌）。r 为日收益面板。"""
    idx = {d: i for i, d in enumerate(days_idx)}
    flags = []
    for e in ev.itertuples():
        i = idx.get(e.trade_date)
        if i is None or i + 1 >= len(days_idx):
            flags.append(False)
            continue
        t1 = days_idx[i + 1]
        if e.ts_code not in r.columns:
            flags.append(False)
            continue
        ret1 = r.at[t1, e.ts_code]
        lim = 0.19 if e.ts_code.endswith('.BJ') else 0.095
        fillable = pd.notna(ret1) and ret1 < lim and abs(ret1) > 1e-9
        flags.append(bool(fillable))
    return pd.Series(flags, index=ev.index)


def dedup_td(ev: pd.DataFrame) -> pd.DataFrame:
    """同股 20td 去重。"""
    ev = ev.sort_values(['ts_code', 'trade_date'])
    keep, last = [], {}
    for i, t in enumerate(ev.itertuples()):
        prev = last.get(t.ts_code)
        if prev is not None and (t.trade_date - prev).days <= DEDUP_TD:
            continue
        keep.append(i)
        last[t.ts_code] = t.trade_date
    return ev.iloc[keep]


def run_arm(name: str, ev: pd.DataFrame, r, valid, days_idx, fwd, base,
            res: dict) -> None:
    ev = dedup_td(ev)
    car = e29.car_table(ev, r, valid, days_idx, fwd, base)
    st = e29.arm_stats(car, name)
    st['verdict'] = e29.verdict(st) if st['n_events'] >= MIN_N \
        else f'INCONCLUSIVE(n<{MIN_N})'
    if st['verdict'] == '强':
        mask = fillability_filter(ev, r, valid, days_idx)
        n_fill = int(mask.sum())
        st['fillable_n'] = n_fill
        st['fillable_ratio'] = n_fill / max(1, len(ev))
        if n_fill >= 50:
            car_f = e29.car_table(ev[mask], r, valid, days_idx, fwd, base)
            st_f = e29.arm_stats(car_f, name + '_fillable')
            orig = st.get('h20', {}).get('car_mean')
            resid = st_f.get('h20', {}).get('car_mean')
            st['fillable_h20'] = resid
            if orig and resid is not None and abs(orig) > 1e-9:
                st['residual_ratio'] = resid / orig
                if abs(resid / orig) < RESIDUAL_GATE:
                    st['verdict'] = '判强不晋级(可成交性切片死亡)'
    res[name] = st
    h20 = st.get('h20', {})
    _note(f"{name}: n={st['n_events']} h20={h20.get('car_mean')} "
          f"t={h20.get('t_cluster')} fill={st.get('fillable_ratio')} "
          f"v={st['verdict']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    args = ap.parse_args()
    if not args.run:
        print("⛔ --run required (prereg frozen 2026-09-21)")
        return 2
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    df = load_analyst()
    if df is None:
        _note("⛔ analyst 数据未落地")
        return 3
    _note(f"raw={len(df)} kinds={df['kind'].value_counts().to_dict()}")

    close_w = pd.read_parquet(e27.OUT_DIR / 'panel_close.parquet')
    r = daily_returns(close_w)
    valid = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    days_idx = r.index

    df = df[df['ann_date'] <= SCREEN_END]
    df['trade_date'] = df['ann_date']

    fwd = e29.fwd_panels(r)
    base = e29.baseline_mean(fwd, valid)
    plc = e29.placebo_gate(r, valid, days_idx, fwd, base)
    _note(f"placebo: {plc}")
    res = {'meta': {'n_raw': len(df)}, 'placebo': plc}
    if not plc['pass']:
        (OUT_DIR / 'e49_results.json').write_text(json.dumps(
            res | {'aborted': True}, ensure_ascii=False, indent=1))
        return 3

    rating = df[df.kind == 'rating']
    fcst = df[df.kind == 'forecast']

    arms = {
        'A1_rating_up':   rating[rating.rating_dir > 0],
        'A2_rating_down': rating[rating.rating_dir < 0],
        'A3_forecast_up':   fcst[fcst.fy_np_chg > 10],
        'A4_forecast_dn':   fcst[fcst.fy_np_chg < -10],
    }
    # A5 多机构共振：30 日内 ≥3 家上调
    if not rating.empty:
        ru = rating[rating.rating_dir > 0].sort_values('trade_date')
        res_rows = []
        for sym, g in ru.groupby('ts_code'):
            ds = g['trade_date'].values
            for i, d in enumerate(ds):
                cnt = ((ds > d - np.timedelta64(30, 'D')) & (ds <= d)).sum()
                if cnt >= 3:
                    res_rows.append({'trade_date': pd.Timestamp(d),
                                     'ts_code': sym})
        arms['A5_consensus'] = pd.DataFrame(res_rows)

    for nm, ev in arms.items():
        if ev is None or len(ev) == 0:
            res[nm] = {'arm': nm, 'n_events': 0,
                       'verdict': 'INCONCLUSIVE(n=0)'}
            _note(f"{nm}: n=0")
            continue
        run_arm(nm, ev[['trade_date', 'ts_code']], r, valid, days_idx,
                fwd, base, res)

    res['elapsed_min'] = (time.time() - t0) / 60
    (OUT_DIR / 'e49_results.json').write_text(json.dumps(
        res, ensure_ascii=False, indent=1, default=str))
    _note(f"done {res['elapsed_min']:.1f}min")
    return 0


if __name__ == '__main__':
    sys.exit(main())
