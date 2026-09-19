#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C3 池扩容·补采②：全 A 分红事件流（datahubco tushare dividend）。

范围：bar 宇宙 5473 只（daily_bars ∪ delisted_bars），同 collect_fina_pit_alla。
产物：data/dividend_events_alla/{sym}.parquet（每票一文件、原子写、断点续采）。
字段：tushare dividend 14 字段全留（ts_code/end_date/ann_date/div_proc/
stk_div/stk_bo_rate/stk_co_rate/cash_div/cash_div_tax/record_date/ex_date/
pay_date/div_listdate/imp_ann_date）+ 本仓 code 列。
用途：P1「支付率 ∈(0,1)」= Σcash_div（税前口径另列）/ 净利润，须与
fina_indicator 的净利润字段配套（C3 预登记时定口径）。

用法：.venv/bin/python scripts/lab/collect_dividend_events_alla.py [--limit N]
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

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / 'data/dividend_events_alla'
REPORT = OUT_DIR / 'DIVIDEND_ALLA_COLLECTION_REPORT.json'
URL = 'http://datahubco.com/app-api/openapi/v1/tushare/dividend'
KEY = 'dba548a206a453c197f9175189b757374fa6db9554bb29e69efea127'
SLEEP = 0.12
RETRIES = 3
TIMEOUT = 20
BAR_BASES = [ROOT / 'experiments/lab/market-breadth-a/daily_bars',
             ROOT / 'experiments/lab/market-breadth-a/delisted_bars']
FIELDS = ['ts_code', 'end_date', 'ann_date', 'div_proc', 'stk_div',
          'stk_bo_rate', 'stk_co_rate', 'cash_div', 'cash_div_tax',
          'record_date', 'ex_date', 'pay_date', 'div_listdate', 'imp_ann_date']


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    except Exception:
        return 'unknown'


def to_ts(code: str) -> str:
    mkt, num = code.split('.')
    return f'{num}.{mkt.upper()}'


def universe() -> list:
    syms = set()
    for b in BAR_BASES:
        for p in b.glob('*.parquet'):
            if p.stem.startswith(('sh.', 'sz.', 'bj.')):
                syms.add(p.stem)
    return sorted(syms)


def fetch(ts_code: str) -> list:
    for attempt in range(RETRIES):
        try:
            r = requests.get(URL, params={'ts_code': ts_code},
                             headers={'X-API-Key': KEY}, timeout=TIMEOUT)
            if r.status_code == 200:
                return (r.json().get('data') or {}).get('items', []) or []
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f'dividend fetch failed {ts_code}')


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    syms = universe()
    todo = [s for s in syms if not (OUT_DIR / f'{s}.parquet').exists()]
    if args.limit:
        todo = todo[:args.limit]
    print(f'[dividend-alla] universe={len(syms)} todo={len(todo)}')
    fails, done, empties = [], 0, 0
    for i, sym in enumerate(todo, 1):
        try:
            items = fetch(to_ts(sym))
            df = pd.DataFrame(items, columns=FIELDS)
            df['code'] = sym
            if not df.empty:
                df = df.sort_values(['end_date', 'ann_date'],
                                    na_position='last').reset_index(drop=True)
            else:
                empties += 1
            tmp = OUT_DIR / f'{sym}.parquet.tmp'
            df.to_parquet(tmp, index=False)
            tmp.rename(OUT_DIR / f'{sym}.parquet')
            done += 1
        except Exception as e:  # noqa: BLE001
            fails.append({'code': sym, 'reason': str(e)[:200]})
        time.sleep(SLEEP)
        if i % 250 == 0 or i == len(todo):
            print(f'[dividend-alla] {i}/{len(todo)} done={done} '
                  f'empty={empties} fails={len(fails)}', flush=True)
    report = {
        'git_sha': git_sha(),
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'universe': len(syms), 'todo': len(todo), 'done': done,
        'empty_no_dividend_history': empties, 'fails': fails,
        'note': 'empty=源无分红记录（未分红/退市早期票），非错误；'
                '14字段全留供 C3 口径复核',
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                      encoding='utf-8')
    print(f'[dividend-alla] done={done} empty={empties} '
          f'fails={len(fails)} -> {REPORT}')
    return 0 if not fails else 1


if __name__ == '__main__':
    sys.exit(main())
