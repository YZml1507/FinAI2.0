#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据层去偏补采①：回测窗内存活过的退市票日线（datahubco tushare 代理）。

范围：stock_basic 中 type=1 且 2015-01-05 <= outDate 的退市票（255 只），
采集区间 max(ipoDate, 2015-01-01) ~ min(outDate, 2024-12-31)，按年分段
（datahubco 限制 date range 不可跨年）。

产物隔离（不碰权威目录）：
  数据 -> experiments/lab/market-breadth-a/delisted_bars/<code>.parquet
  报告 -> experiments/lab/market-breadth-a/DELISTED_COLLECTION_REPORT.json

schema 与 daily_bars 对齐；turn/isST 源缺省置 0 并如实标注
（isST 由 namechange 补采另行重建）。
纪律：断点续采（.done 标记）、原子写（tmp->rename）、失败留痕、限频。

用法：.venv/bin/python scripts/lab/collect_delisted_bars.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path('/home/ubuntu/FinAI2.0')
LAB = ROOT / 'experiments/lab/market-breadth-a'
OUT_DIR = LAB / 'delisted_bars'
REPORT = LAB / 'DELISTED_COLLECTION_REPORT.json'
DONE_SUFFIX = '.done'

URL = 'http://datahubco.com/app-api/openapi/v1/tushare/daily'
KEY = 'dba548a206a453c197f9175189b757374fa6db9554bb29e69efea127'
WINDOW_START = '2015-01-01'
WINDOW_END = '2024-12-31'
SLEEP = 0.12
RETRIES = 3
TIMEOUT = 20

COLUMNS = ['date', 'open', 'high', 'low', 'close', 'preclose', 'volume',
           'amount', 'turn', 'pctChg', 'tradestatus', 'isST', 'code',
           'source', 'adjust_mode']


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    except Exception:
        return 'unknown'


def to_ts(code: str) -> str:
    mkt, num = code.split('.')
    return f'{num}.{mkt.upper()}'


def fetch_year(ts_code: str, y0: str, y1: str) -> list:
    params = {'ts_code': ts_code, 'start_date': y0, 'end_date': y1}
    for attempt in range(RETRIES):
        try:
            r = requests.get(URL, params=params,
                             headers={'X-API-Key': KEY}, timeout=TIMEOUT)
            j = r.json()
            if r.status_code == 200:
                return (j.get('data') or {}).get('items', []) or []
            if 'too large' in str(j.get('msg', '')):
                return []
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f'fetch failed {ts_code} {y0}-{y1} after {RETRIES} tries')


def collect_one(code: str, ipo: str, out: str) -> dict:
    ts = to_ts(code)
    start = max(ipo.replace('-', ''), WINDOW_START.replace('-', ''))
    end = min(out.replace('-', '') if out else '99999999',
              WINDOW_END.replace('-', ''))
    if start > end:
        return {'code': code, 'rows': 0, 'status': 'skip_out_of_window'}
    y0, y1 = int(start[:4]), int(end[:4])
    rows: list = []
    for y in range(y0, y1 + 1):
        seg0 = max(start, f'{y}0101')
        seg1 = min(end, f'{y}1231')
        if seg0 > seg1:
            continue
        items = fetch_year(ts, seg0, seg1)
        rows.extend(items)
        time.sleep(SLEEP)
    if not rows:
        return {'code': code, 'rows': 0, 'status': 'empty_no_data'}
    df = pd.DataFrame(rows, columns=['ts_code', 'trade_date', 'open', 'high',
                                     'low', 'close', 'pre_close', 'change',
                                     'pct_chg', 'vol', 'amount'])
    df['date'] = pd.to_datetime(df['trade_date']).dt.strftime('%Y-%m-%d')
    df = df.sort_values('date').drop_duplicates('date', keep='last')
    out_df = pd.DataFrame({
        'date': df['date'],
        'open': df['open'], 'high': df['high'], 'low': df['low'],
        'close': df['close'], 'preclose': df['pre_close'],
        'volume': df['vol'], 'amount': df['amount'],
        'turn': 0.0,
        'pctChg': df['pct_chg'],
        'tradestatus': 1, 'isST': 0,
        'code': code, 'source': 'tushare_datahubco', 'adjust_mode': 'raw',
    })[COLUMNS]
    tmp = OUT_DIR / f'{code}.parquet.tmp'
    final = OUT_DIR / f'{code}.parquet'
    out_df.to_parquet(tmp, index=False)
    tmp.rename(final)
    (OUT_DIR / f'{code}{DONE_SUFFIX}').write_text(
        datetime.now(timezone.utc).isoformat(), encoding='utf-8')
    return {'code': code, 'rows': len(out_df),
            'date_min': str(out_df['date'].iloc[0]),
            'date_max': str(out_df['date'].iloc[-1]), 'status': 'ok'}


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sb = pd.read_parquet(ROOT / 'data/stock_basic_cache.parquet')
    sb['type_s'] = sb['type'].astype(str).str.strip()
    sb['out'] = sb['outDate'].astype(str).str[:10]
    sb['ipo'] = sb['ipoDate'].astype(str).str[:10]
    dl = sb[(sb['type_s'] == '1') & (sb['out'].str.len() >= 8)
            & (sb['out'] >= '2015-01-05') & (sb['ipo'] <= '2024-12-31')]
    todo = []
    for _, r in dl.iterrows():
        if (OUT_DIR / f"{r['code']}{DONE_SUFFIX}").exists():
            continue
        todo.append((r['code'], r['ipo'], r['out']))
    print(f'[collect] target {len(dl)} todo {len(todo)}')
    results, fails = [], []
    for i, (code, ipo, out) in enumerate(todo, 1):
        try:
            res = collect_one(code, ipo, out)
        except Exception as e:  # noqa: BLE001
            res = {'code': code, 'status': 'error', 'reason': str(e)}
            fails.append(res)
        results.append(res)
        if i % 10 == 0 or i == len(todo):
            print(f"[collect] {i}/{len(todo)} last={res['code']} {res['status']} "
                  f"rows={res.get('rows', '-')}")
    report = {
        'git_sha': git_sha(),
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'window': [WINDOW_START, WINDOW_END],
        'universe': 'stock_basic type=1 & outDate>=2015-01-05 & ipoDate<=2024-12-31',
        'target': len(dl), 'collected': len(results),
        'ok': sum(1 for r in results if r['status'] == 'ok'),
        'empty': sum(1 for r in results if r['status'] == 'empty_no_data'),
        'errors': fails,
        'results': results,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                      encoding='utf-8')
    print(f"[collect] done ok={report['ok']} empty={report['empty']} "
          f'errors={len(fails)}')
    return 0 if not fails else 1


if __name__ == '__main__':
    sys.exit(main())