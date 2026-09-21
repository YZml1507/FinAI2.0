"""e37 事件否决日度序列生成（V1 户数激增 + V2 重复上榜，e35 口径）。

逐交易日产出 veto 集合：
- V1：T 日最新已公告 gdhs 期 qoq > +0.30（直到新公告覆盖）；
- V2：T 日回看 10 个交易日内上龙虎榜 ≥2 次。
V3/V4 不入选（e35 池内弱，已如实登记）。

输出 data/e37_veto/veto_daily.parquet（date, symbols[list]），
供 cfg.event_veto_series 装载为 {iso_date: frozenset}。

用法：.venv/bin/python -m scripts.lab.e37_veto_series --run
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
from scripts.lab import e27_insider_screen as e27  # noqa: E402
from scripts.lab import e28_gdhs_screen as e28  # noqa: E402
from scripts.lab.e29_lhb_screen import load_lhb  # noqa: E402

OUT_PATH = ROOT / 'data' / 'e37_veto' / 'veto_daily.parquet'
GDHS_SURGE = 0.30
LHB_WIN = 10


def _note(m: str) -> None:
    print(f"[note] {m}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    args = ap.parse_args()
    if not args.run:
        print("dry-run"); return 0

    t0 = time.time()
    close_w = pd.read_parquet(e27.OUT_DIR / 'panel_close.parquet')
    r = e23.daily_returns(close_w)
    idx = r.index                                   # 2431 交易日
    six2ts = {t.split('.')[0]: t for t in r.columns}
    day_ns = idx.values                             # datetime64 数组

    # ---- V1：每股「最新公告 qoq>0.30」的逐日状态（向量化 asof） ----
    long = e28.load_gdhs_records().sort_values('ann_date')
    long = long[long['holders_prev'] > 0].copy()
    long['qoq'] = long['holders'] / long['holders_prev'] - 1
    v1_active: dict[str, np.ndarray] = {}           # ts_code -> bool[2431]
    for c6, g in long.groupby('code'):
        ts = six2ts.get(c6)
        if ts is None:
            continue
        ann = g['ann_date'].values.astype('datetime64[ns]')
        qoq = g['qoq'].values
        j = np.searchsorted(ann, day_ns, side='right') - 1
        ok = j >= 0
        act = np.zeros(len(idx), dtype=bool)
        act[ok] = qoq[j[ok]] > GDHS_SURGE
        if act.any():
            v1_active[ts] = act
    _note(f"V1 激活股数={len(v1_active)}")

    # ---- V2：每股 10 交易日内上榜 ≥2 次（滚动计数） ----
    lhb = load_lhb()
    lhb_days = lhb.groupby('ts_code')['trade_date'].apply(
        lambda s: np.array(sorted(set(s)))).to_dict()
    ts_set = set(six2ts.values())
    v2_active: dict[str, np.ndarray] = {}
    for ts, days in lhb_days.items():
        if ts not in ts_set:
            continue
        hits = np.isin(day_ns, np.asarray(days, dtype='datetime64[ns]'))
        cnt = pd.Series(hits.astype(int)).rolling(LHB_WIN, min_periods=1).sum()
        act = (cnt.values >= 2)
        if act.any():
            v2_active[ts] = act
    _note(f"V2 激活股数={len(v2_active)}")

    # ---- 合成逐日集合 ----
    rows = []
    codes_v1 = {s: i for i, s in enumerate(sorted(v1_active))}
    codes_v2 = {s: i for i, s in enumerate(sorted(v2_active))}
    M1 = np.array([v1_active[s] for s in codes_v1]) if codes_v1 else np.zeros((0, len(idx)), bool)
    M2 = np.array([v2_active[s] for s in codes_v2]) if codes_v2 else np.zeros((0, len(idx)), bool)
    inv1 = list(codes_v1.keys())
    inv2 = list(codes_v2.keys())
    for i, T in enumerate(idx):
        veto = set()
        if M1.shape[0]:
            veto.update(inv1[j] for j in np.nonzero(M1[:, i])[0])
        if M2.shape[0]:
            veto.update(inv2[j] for j in np.nonzero(M2[:, i])[0])
        if veto:
            rows.append((T.date().isoformat(), sorted(veto)))

    df = pd.DataFrame(rows, columns=['date', 'symbols'])
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT_PATH, index=False)
    _note(f"done {len(df)} 日有否决，V1={len(v1_active)} 股 "
          f"V2={len(v2_active)} 股 -> {OUT_PATH} ({time.time()-t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
