#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""两融明细采集器（akshare 沪深交易所接口，e26 因子族前置数据）。

- 交易日历：data/daily_basic_alla/*.parquet 文件名（与 e23/e25 同窗 2015–2024）。
- 落盘：data/margin_detail/{YYYYMMDD}.parquet，两市合并、列归一化为：
  ts_code / sec_name / fin_balance(融资余额,元) / fin_buy(融资买入额,元) /
  fin_repay(融资偿还额,元,深市缺→NaN) / short_qty(融券余量,股) /
  short_sell(融券卖出量,股) / short_repay(深市缺→NaN) / mkt(SH/SZ)
- 幂等：已有且非空的日期文件跳过；失败登记进 manifest，fail-soft 不中断批次。
- 限速 sleep 0.35s/次两市合计 ~2431 天 × 2 请求 ≈ 4862 次，预计 40–70min。

用法：.venv/bin/python scripts/collect_margin_detail.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from data.collector import _atomic_write_parquet  # noqa: E402

DV_DIR = _root / 'data/daily_basic_alla'
OUT_DIR = _root / 'data/margin_detail'
MANIFEST = OUT_DIR / '_manifest.json'
SLEEP = 0.35
RETRY = 3

SSE_MAP = {'标的证券代码': 'code', '标的证券简称': 'sec_name',
           '融资余额': 'fin_balance', '融资买入额': 'fin_buy',
           '融资偿还额': 'fin_repay', '融券余量': 'short_qty',
           '融券卖出量': 'short_sell', '融券偿还量': 'short_repay'}
SZSE_MAP = {'证券代码': 'code', '证券简称': 'sec_name',
            '融资余额': 'fin_balance', '融资买入额': 'fin_buy',
            '融券余量': 'short_qty', '融券卖出量': 'short_sell'}


def _to_ts(code: str, mkt: str) -> str:
    return f"{code}.{mkt}"


def fetch_day(date8: str) -> pd.DataFrame:
    """单日两市两融明细；两市任一失败单独记列不影响另一市。"""
    import akshare as ak
    frames = []
    df = ak.stock_margin_detail_sse(date=date8)
    if df is not None and len(df):
        d = df.rename(columns=SSE_MAP)
        d['mkt'] = 'SH'
        d['ts_code'] = d['code'].astype(str).map(lambda c: _to_ts(c, 'SH'))
        frames.append(d)
    df = ak.stock_margin_detail_szse(date=date8)
    if df is not None and len(df):
        d = df.rename(columns=SZSE_MAP)
        d['mkt'] = 'SZ'
        d['ts_code'] = d['code'].astype(str).map(lambda c: _to_ts(c, 'SZ'))
        frames.append(d)
    if not frames:
        return pd.DataFrame()
    cols = ['ts_code', 'sec_name', 'mkt', 'fin_balance', 'fin_buy',
            'fin_repay', 'short_qty', 'short_sell', 'short_repay']
    out = pd.concat(frames, ignore_index=True)
    for c in cols:
        if c not in out.columns:
            out[c] = pd.NA
    return out[cols]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    dates = sorted(f.stem for f in DV_DIR.glob('*.parquet'))
    if args.limit:
        dates = dates[:args.limit]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    todo = [d for d in dates
            if not (OUT_DIR / f'{d}.parquet').exists()
            or (OUT_DIR / f'{d}.parquet').stat().st_size == 0]
    print(f"[margin] 日历 {len(dates)} 天，待采 {len(todo)} 天")
    if args.dry_run:
        return 0
    man = {'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'done': 0,
           'failed': {}, 'rows_total': 0}
    t0 = time.time()
    for i, d in enumerate(todo):
        ok = False
        for att in range(RETRY):
            try:
                df = fetch_day(d)
                if df.empty:
                    raise RuntimeError('empty frame')
                _atomic_write_parquet(df, OUT_DIR / f'{d}.parquet')
                man['rows_total'] += len(df)
                ok = True
                break
            except Exception as e:  # noqa: BLE001 采集器 fail-soft 登记
                last = f'{type(e).__name__}: {str(e)[:160]}'
                time.sleep(1.0 * (att + 1))
        if not ok:
            man['failed'][d] = last
            print(f"[margin] {d} FAIL {last}")
        man['done'] += 1
        if (i + 1) % 50 == 0:
            el = time.time() - t0
            eta = el / (i + 1) * (len(todo) - i - 1)
            print(f"[margin] {i+1}/{len(todo)} 累计 {el:.0f}s ETA {eta:.0f}s "
                  f"失败 {len(man['failed'])}")
            MANIFEST.write_text(json.dumps(man, ensure_ascii=False, indent=1))
        time.sleep(SLEEP)
    man['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
    MANIFEST.write_text(json.dumps(man, ensure_ascii=False, indent=1))
    print(f"[margin] done {man['done']}/{len(todo)} 失败 {len(man['failed'])} "
          f"行 {man['rows_total']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
