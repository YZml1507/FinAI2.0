#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""股东人数（户数）采集器：东财按期全市场快照。

- `stock_zh_a_gdhs(symbol=YYYYMM期末)` 单期返回全 A 截面（实测 2025Q2=5347 行，
  ~20s/期）。期序列 = 每年 0331/0630/0930/1231（东财另有 0230/0831 等自愿披露期，
  先行只采四大法定报告期，口径稳定）。
- 落盘：data/gdhs/{YYYYMMDD}.parquet；_manifest.json 记 fails。
- PIT 注意：列内「统计截止日」为期末日；**可见日 = 实际公告日**，不在本数据内——
  下游建模须另接披露日历（预案：巨潮 stock_hold_num_cninfo 或按季报法定披露
  截止日近似 = 期末 +2 月内首个交易日，冻结前须定）。

用法：.venv/bin/python scripts/collect_gdhs.py [--start-year 2013] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from data.collector import _atomic_write_parquet  # noqa: E402

OUT_DIR = _root / 'data/gdhs'
PERIODS = ['0331', '0630', '0930', '1231']


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--start-year', type=int, default=2013)
    ap.add_argument('--end-year', type=int, default=2024)
    ap.add_argument('--sleep', type=float, default=2.0)  # 单期 ~20s 返回，间隔 2s
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    periods = [f"{y}{p}" for y in range(args.start_year, args.end_year + 1)
               for p in PERIODS]
    todo = [p for p in periods
            if not ((OUT_DIR / f'{p}.parquet').exists()
                    and (OUT_DIR / f'{p}.parquet').stat().st_size > 1000)]
    print(f"[gdhs] 期数 {len(periods)}，待采 {len(todo)}", flush=True)
    if args.dry_run:
        print(todo[:5])
        return 0

    import akshare as ak
    fails = []
    for i, p in enumerate(todo):
        try:
            df = ak.stock_zh_a_gdhs(symbol=p)
            if df is None or len(df) == 0:
                raise RuntimeError("0 rows")
            _atomic_write_parquet(df, OUT_DIR / f'{p}.parquet')
            print(f"[gdhs] {i+1}/{len(todo)} {p}: {len(df)} rows", flush=True)
        except Exception as e:  # noqa: BLE001
            fails.append({'period': p, 'err': f"{type(e).__name__}: {e}"[:200]})
            print(f"[gdhs] {p} FAIL {e}", flush=True)
        time.sleep(args.sleep)

    manifest = {'axis': 'gdhs', 'periods': periods, 'fails': fails,
                'finished_at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    (OUT_DIR / '_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    print(f"[gdhs] done fails={len(fails)}", flush=True)
    return 2 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
