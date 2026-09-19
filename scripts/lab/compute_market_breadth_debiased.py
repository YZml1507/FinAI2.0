#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""去偏宽度序列计算（delisted_bars 并入版，新产物不覆盖权威序列）。

口径与 compute_market_breadth.py 完全一致（MA20/收盘价上穿/停牌无 K 线剔除），
唯一差异：universe = daily_bars（5220 在市票）∪ delisted_bars（255 只窗内退市票）。

产物（⛔ 新文件名，权威 breadth20_daily.parquet 不动）：
  experiments/lab/market-breadth-a/breadth20_daily_debiased.parquet
  experiments/lab/market-breadth-a/BREADTH_DEBIASED_META.json

用法：.venv/bin/python scripts/lab/compute_market_breadth_debiased.py
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path('/home/ubuntu/FinAI2.0')
LAB = ROOT / 'experiments/lab/market-breadth-a'
BAR_DIRS = [LAB / 'daily_bars', LAB / 'delisted_bars']
OUT = LAB / 'breadth20_daily_debiased.parquet'
META = LAB / 'BREADTH_DEBIASED_META.json'
MA_WIN = 20
MIN_UNIVERSE = 100


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    except Exception:
        return 'unknown'


def src_hash() -> str:
    h = hashlib.sha256()
    for d in BAR_DIRS:
        for p in sorted(d.glob('*.parquet')):
            h.update(p.name.encode())
            h.update(str(p.stat().st_size).encode())
    return h.hexdigest()[:16]


def load_all() -> pd.DataFrame:
    frames = []
    for d in BAR_DIRS:
        files = sorted(d.glob('*.parquet'))
        print(f'[load] {d.name}: {len(files)} files', flush=True)
        for i, f in enumerate(files):
            try:
                frames.append(pd.read_parquet(f, columns=['date', 'close', 'code']))
            except Exception as e:  # noqa: BLE001
                print(f'  [skip] {f.name}: {str(e)[:60]}', flush=True)
            if i % 1000 == 0:
                print(f'  [load] {d.name} {i}/{len(files)}', flush=True)
    big = pd.concat(frames, ignore_index=True)
    big['date'] = pd.to_datetime(big['date'])
    return big


def compute_breadth(big: pd.DataFrame) -> pd.DataFrame:
    print(f'[compute] rows={len(big)}', flush=True)
    big = big.sort_values(['code', 'date'])
    big['ma20'] = big.groupby('code')['close'].transform(
        lambda s: s.rolling(MA_WIN, min_periods=MA_WIN).mean())
    big['above'] = (big['close'] > big['ma20']).astype('float')
    daily = big.groupby('date').agg(
        universe=('code', 'count'),
        above_cnt=('above', 'sum'),
    ).reset_index()
    daily['breadth20'] = daily['above_cnt'] / daily['universe']
    daily.loc[daily['universe'] < MIN_UNIVERSE, 'breadth20'] = float('nan')
    return daily.sort_values('date').reset_index(drop=True)


def main() -> int:
    big = load_all()
    daily = compute_breadth(big)
    valid = daily.dropna(subset=['breadth20'])
    print(f"[result] days={len(daily)} valid={len(valid)} "
          f"{valid['date'].min().date()} -> {valid['date'].max().date()}")
    # 与权威序列对照（同日日宽度差分布）
    old = pd.read_parquet(LAB / 'breadth20_daily.parquet')
    old['date'] = pd.to_datetime(old['date'])
    m = valid.merge(old[['date', 'breadth20']], on='date', suffixes=('_new', '_old'))
    diff = (m['breadth20_new'] - m['breadth20_old']).abs()
    print(f'[compare] 与权威序列同日日数={len(m)}, |Δb| mean={diff.mean():.5f} '
          f'max={diff.max():.5f}')
    daily.to_parquet(OUT, index=False)
    meta = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'git_sha': git_sha(),
        'source_data_hash': src_hash(),
        'ma_window': MA_WIN, 'min_universe': MIN_UNIVERSE,
        'trading_days': int(len(daily)), 'valid_days': int(len(valid)),
        'universe': 'daily_bars(5220) + delisted_bars(255, 窗内退市票)',
        'compare_vs_authoritative': {
            'common_days': int(len(m)),
            'abs_diff_mean': float(diff.mean()),
            'abs_diff_max': float(diff.max()),
        },
        'caliber_note': '口径与 breadth20_daily.parquet 全同，唯一差异=并入窗内退市票（去幸存者偏差）；isST 字段源缺省未用于宽度计算',
        'source': 'sina daily bars + tushare_datahubco delisted bars',
    }
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[done] -> {OUT}')
    return 0


if __name__ == '__main__':
    sys.exit(main())