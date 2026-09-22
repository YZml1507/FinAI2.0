#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OOS 2025 事件输入续采（东财系）：lhb / block_trade / margin_detail。

各自从现有水位次日续到 2026-09-22，日粒度分片落盘，幂等可重跑。
串行 sleep 防频控。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import akshare as ak
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
TODAY = '2026-09-22'


def _watermark(d: Path, col: str) -> str:
    """目录内最新 parquet 的最大日期（YYYYMMDD）。"""
    mx = None
    for p in sorted(d.glob('*.parquet'))[-3:]:
        df = pd.read_parquet(p)
        for c in df.columns:
            if c != col:
                continue
            v = pd.to_datetime(df[c], errors='coerce').max()
            if pd.notna(v) and (mx is None or v > mx):
                mx = v
    if mx is None:
        for p in sorted(d.glob('*.parquet'))[-3:]:
            stem = p.stem
            if len(stem) == 8 and stem.isdigit():
                mx = max(mx or pd.Timestamp('1970-01-01'),
                         pd.Timestamp(stem))
    return (mx or pd.Timestamp('2022-12-31')).strftime('%Y%m%d')


def _days(start: str, end: str):
    d0, d1 = pd.Timestamp(start), pd.Timestamp(end)
    for d in pd.date_range(d0 + pd.Timedelta(days=1), d1):
        if d.weekday() < 5:
            yield d


def extend_lhb() -> None:
    out = ROOT / 'data/lhb'
    start = _watermark(out, 'trade_date')
    print(f'lhb watermark {start}')
    # 东财按区间返回：按周切片稳一点
    cur = pd.Timestamp(start)
    end = pd.Timestamp(TODAY)
    while cur < end:
        w_end = min(cur + pd.Timedelta(days=6), end)
        try:
            df = ak.stock_lhb_detail_em(
                start_date=cur.strftime('%Y%m%d'),
                end_date=w_end.strftime('%Y%m%d'))
            if df is not None and len(df):
                df.columns = [str(c) for c in df.columns]
                for day, g in df.groupby(
                        pd.to_datetime(df['交易日期']).dt.strftime('%Y%m%d')):
                    g.to_parquet(out / f'{day}.parquet')
                print('lhb', cur.date(), w_end.date(), len(df))
        except Exception as e:
            print('lhb FAIL', cur.date(), str(e)[:80])
        cur = w_end + pd.Timedelta(days=1)
        time.sleep(0.3)


def extend_block() -> None:
    out = ROOT / 'data/block_trade'
    start = _watermark(out, 'trade_date')
    print(f'block watermark {start}')
    for d in _days(start, TODAY):
        try:
            df = ak.stock_dzjy_mrtj(start_date=d.strftime('%Y%m%d'),
                                    end_date=d.strftime('%Y%m%d'))
            if df is not None and len(df):
                df.to_parquet(out / f'{d:%Y%m%d}.parquet')
        except Exception as e:
            print('block FAIL', d.date(), str(e)[:80])
        time.sleep(0.3)


def extend_margin() -> None:
    out = ROOT / 'data/margin_detail'
    start = _watermark(out, None)
    print(f'margin watermark {start}')
    for d in _days(start, TODAY):
        rows = []
        for fn, tag in ((ak.stock_margin_detail_sse, 'sse'),
                        (getattr(ak, 'stock_margin_detail_szse', None), 'szse')):
            if fn is None:
                continue
            try:
                df = fn(date=d.strftime('%Y%m%d'))
                if df is not None and len(df):
                    df['_src'] = tag
                    rows.append(df)
            except Exception:
                pass
        if rows:
            pd.concat(rows, ignore_index=True).to_parquet(
                out / f'{d:%Y%m%d}.parquet')
        time.sleep(0.3)


if __name__ == '__main__':
    which = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if which in ('lhb', 'all'):
        extend_lhb()
    if which in ('block', 'all'):
        extend_block()
    if which in ('margin', 'all'):
        extend_margin()
    print('DONE')
