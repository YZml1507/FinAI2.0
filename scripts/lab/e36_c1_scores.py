"""e36 C1 合成 z 日度序列预计算（e8b 叠加实验数据源）。

逐交易日 T 复用 e31.build_signals 产四族信号（S5/M5/F6/I3），按 e33
口径做全 A 截面 z 标准化后等权合成（缺失族以剩余族等权，家族
全缺则当日该股无分）。输出长表 parquet：

    data/c1_overlay/c1_scores_daily.parquet   (date, symbol, z)

PIT 正确性：全部信号 builder 内部已按「T 时点可见」口径实现
（gdhs ann_date≤T / margin ≤T-1 / PIT 财报披露 ≤T / 行业快照≤T）。
与 e33 月度点同管线，日频只是加密采样——口径零新增。

用法：.venv/bin/python -m scripts.lab.e36_c1_scores --run [--limit-days N]
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
from scripts.lab import e25_factor_screen as e25  # noqa: E402
from scripts.lab import e26_margin_screen as e26  # noqa: E402
from scripts.lab import e27_insider_screen as e27  # noqa: E402
from scripts.lab import e28_gdhs_screen as e28  # noqa: E402
from scripts.lab import e31_candidate_crosscorr as e31  # noqa: E402
from scripts.lab.e34_industry_screen import (  # noqa: E402
    IND_REV_WIN, load_industry_snapshots)

OUT_PATH = ROOT / 'data' / 'c1_overlay' / 'c1_scores_daily.parquet'
MARGIN_DIR = ROOT / 'data' / 'margin_detail'


def _note(m: str) -> None:
    print(f"[note] {m}", flush=True)


def _z(s: pd.Series) -> pd.Series:
    v = s.dropna()
    if len(v) < 30 or v.std(ddof=0) == 0:
        return pd.Series(np.nan, index=s.index)
    return (s - v.mean()) / v.std(ddof=0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    ap.add_argument('--limit-days', type=int, default=None)
    ap.add_argument('--start', default='2015-01-01')
    args = ap.parse_args()
    if not args.run:
        print("dry-run"); return 0

    t0 = time.time()
    suf = f"_lim{args.limit_days}" if args.limit_days else ""
    close_w = pd.read_parquet(e27.OUT_DIR / f'panel_close{suf}.parquet')
    r = e23.daily_returns(close_w)
    idx = r.index
    _note(f"panel {close_w.shape}")

    Ts = [T for T in idx if pd.Timestamp(T) >= pd.Timestamp(args.start)]
    _note(f"目标日数 {len(Ts)}")

    long = e28.load_gdhs_records()
    gdhs_vis = e28.visible_signal(long, list(Ts))          # {T: snap}
    pit_cache = e25.load_pit()
    pit = {T: e25.pit_snapshot(T, pit_cache) for T in Ts}  # {T: snap}
    cs = np.log1p(r.fillna(0.0)).cumsum()
    ret21 = np.expm1(cs - cs.shift(IND_REV_WIN))
    snaps = load_industry_snapshots()

    P26 = e26.load_panels(args.limit_days)
    m5 = e26.margin_signal_frames(P26)['short_chg20']
    margin_dates = P26['fin_balance'].index
    _note(f"margin_days={len(margin_dates)}")

    ctx31 = {'gdhs_vis': gdhs_vis, 'pit': pit, 'h2': ret21, 'm5': m5,
             'margin_dates': margin_dates, 'snaps': snaps,
             'ret21': ret21, 'cols': r.columns,
             'six2ts': {t.split('.')[0]: t for t in r.columns}}

    rows = []
    for i, T in enumerate(Ts):
        sigs = e31.build_signals(T, ctx31)
        zcols = {fam: _z(s) for fam in ('S5', 'M5', 'F6', 'I3')
                 if (s := sigs.get(fam)) is not None}
        if not zcols:
            continue
        comp = pd.concat(zcols, axis=1).mean(axis=1, skipna=True).dropna()
        rows.extend((T.date().isoformat(), sym, float(z))
                    for sym, z in comp.items())
        if i % 250 == 0:
            _note(f"{T.date()} {i}/{len(Ts)}")

    df = pd.DataFrame(rows, columns=['date', 'symbol', 'z'])
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT_PATH, index=False)
    _note(f"done {len(df)} 行 / {df['date'].nunique()} 日 "
          f"-> {OUT_PATH} ({time.time()-t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
