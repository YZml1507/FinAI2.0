#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据层去偏补采②：PIT 名称变更史（tushare namechange，datahubco 代理）。

范围：stock_basic 全量 type=1 票（5220 在市 + 337 退市 = 5557 只）。
产物：data/namechange/namechange.parquet（幂等整表重采，原子写）。

字段（源原样）：ts_code/name/start_date/end_date/ann_date/change_reason
口径声明：start_date/end_date 为名称生效区间（end_date=None 表示至今）；
isST 重建（按「名称含 ST 的期间」回写日线 isST 列）是**后续独立工序**，
本脚本只负责把 PIT 名称史落盘，不改任何日线/权威数据。

用法：.venv/bin/python scripts/lab/collect_namechange.py
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
OUT_DIR = ROOT / 'data/namechange'
OUT = OUT_DIR / 'namechange.parquet'
REPORT = OUT_DIR / 'NAMECHANGE_COLLECTION_REPORT.json'
URL = 'http://datahubco.com/app-api/openapi/v1/tushare/namechange'
sys.path.insert(0, str(ROOT))
from scripts._secrets import require_env  # noqa: E402

KEY = None  # ⛔ 不再硬编码；fetch 时经 require_env 惰性取 DATAHUBCO_API_KEY
SLEEP = 0.12
RETRIES = 3
TIMEOUT = 20
FIELDS = ['ts_code', 'name', 'start_date', 'end_date', 'ann_date',
          'change_reason']


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    except Exception:
        return 'unknown'


def to_ts(code: str) -> str:
    mkt, num = code.split('.')
    return f'{num}.{mkt.upper()}'


def fetch(ts_code: str) -> list:
    for attempt in range(RETRIES):
        try:
            r = requests.get(URL, params={'ts_code': ts_code},
                             headers={'X-API-Key': require_env('DATAHUBCO_API_KEY')}, timeout=TIMEOUT)
            if r.status_code == 200:
                return (r.json().get('data') or {}).get('items', []) or []
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f'namechange fetch failed {ts_code}')


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sb = pd.read_parquet(ROOT / 'data/stock_basic_cache.parquet')
    codes = sorted(sb[sb['type'].astype(str).str.strip() == '1']['code'])
    print(f'[namechange] target {len(codes)}')
    rows, fails = [], []
    for i, code in enumerate(codes, 1):
        ts = to_ts(code)
        try:
            items = fetch(ts)
        except Exception as e:  # noqa: BLE001
            fails.append({'code': code, 'reason': str(e)})
            items = []
        for it in items:
            rows.append(dict(zip(FIELDS, it)))
        time.sleep(SLEEP)
        if i % 250 == 0 or i == len(codes):
            print(f'[namechange] {i}/{len(codes)} rows={len(rows)} fails={len(fails)}')
    df = pd.DataFrame(rows, columns=FIELDS)
    # 本仓 code 口径（sh.600423）对齐
    df['code'] = df['ts_code'].str[-2:].str.lower() + '.' + df['ts_code'].str[:6]
    tmp = OUT.with_suffix('.parquet.tmp')
    df.to_parquet(tmp, index=False)
    tmp.rename(OUT)
    report = {
        'git_sha': git_sha(),
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'target': len(codes), 'rows': len(df), 'fails': fails,
        'st_events': int(df['name'].astype(str).str.upper().str.contains('ST').sum()),
        'codes_with_history': int(df['code'].nunique()),
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                      encoding='utf-8')
    print(f"[namechange] done rows={len(df)} st_events={report['st_events']} "
          f"codes_with_history={report['codes_with_history']} fails={len(fails)}")
    return 0 if not fails else 1


if __name__ == '__main__':
    sys.exit(main())