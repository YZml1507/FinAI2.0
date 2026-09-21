#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""事件轴数据采集器：龙虎榜 / 大宗交易 / 涨停池情绪（逐日落盘，幂等断点）。

数据源 = finai/sources/ 已契约化适配层（PIT 口径：T 日数据 T+1 才可见）：
- lhb        → finai.sources.lhb_source.LhbCollector（东财→新浪降级）
- block_trade→ finai.sources.block_trade_source（东财 stock_dzjy_mrmx）
- sentiment  → finai.sources.sentiment_source（东财涨停池 fetch_zt_pool）

落盘：data/<axis>/{YYYYMMDD}.parquet；已有非空文件跳过。
清单：data/<axis>/_manifest.json（fails/日期列表，append 式重算）。

限速：默认 0.4s/次（东财端点；与 collect_margin_detail 同级）。
⛔ 不静默改口径：adapter normalize 失败即记 fail，不手写兜底映射。

用法：.venv/bin/python scripts/collect_event_axes.py --axis lhb
          [--start 20150101] [--end 20241231] [--limit N] [--dry-run]
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

DV_DIR = _root / 'data/daily_basic_alla'
SLEEP = 0.4


def _fetch(axis: str, date_dash: str):
    """单日单轴 → DataFrame（契约列）；失败抛异常由上层记 manifest。"""
    if axis == 'lhb':
        from finai.sources.lhb_source import LhbCollector
        return LhbCollector(backup_enabled=True).fetch_date(date_dash)
    if axis == 'block_trade':
        from finai.sources.block_trade_source import (
            fetch_eastmoney, normalize_frame)
        return normalize_frame(fetch_eastmoney(date_dash, date_dash), date_dash)
    if axis == 'sentiment':
        from finai.sources.sentiment_source import (
            fetch_zt_pool, normalize_zt_pool_frame)
        return normalize_zt_pool_frame(fetch_zt_pool(date_dash), date_dash)
    raise ValueError(f"unknown axis {axis}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--axis', required=True, choices=['lhb', 'block_trade', 'sentiment'])
    ap.add_argument('--start', default='20150101')
    ap.add_argument('--end', default='20241231')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--sleep', type=float, default=SLEEP)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--dates-file', default=None,
                    help='交易日清单文件（逐行 YYYYMMDD）；提供后取代 DV_DIR 文件名日历'
                         '（用于 DV_DIR 覆盖范围之外的区间，如 2025+）')
    args = ap.parse_args()

    out_dir = _root / 'data' / args.axis
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / '_manifest.json'

    if args.dates_file:
        dates = sorted({ln.strip()
                        for ln in Path(args.dates_file).read_text().splitlines()
                        if ln.strip() and args.start <= ln.strip() <= args.end})
    else:
        dates = [p.stem for p in sorted(DV_DIR.glob('*.parquet'))
                 if args.start <= p.stem <= args.end]
    todo = [d for d in dates
            if not ((out_dir / f'{d}.parquet').exists()
                    and (out_dir / f'{d}.parquet').stat().st_size > 100)]
    if args.limit:
        todo = todo[:args.limit]
    print(f"[{args.axis}] 交易日 {len(dates)}，待采 {len(todo)}", flush=True)
    if args.dry_run:
        for d in todo[:5]:
            print("  would fetch", d)
        return 0

    fails = []
    t0 = time.time()
    for i, d8 in enumerate(todo):
        dash = f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"
        try:
            df = _fetch(args.axis, dash)
            if df is None or len(df) == 0:
                raise RuntimeError("0 rows (EMPTY_OK 不视为成功)")
            _atomic_write_parquet(df, out_dir / f'{d8}.parquet')
        except Exception as e:  # noqa: BLE001
            fails.append({'date': d8, 'err': f"{type(e).__name__}: {e}"[:200]})
        if args.sleep:
            time.sleep(args.sleep)
        if (i + 1) % 100 == 0:
            done = i + 1 - len(fails)
            rate = (i + 1) / max(time.time() - t0, 1)
            print(f"[{args.axis}] {i+1}/{len(todo)} ok={done} fail={len(fails)} "
                  f"rate={rate:.1f}/s eta={(len(todo)-i-1)/max(rate,1e-9)/60:.0f}min",
                  flush=True)

    manifest = {
        'axis': args.axis, 'window': [args.start, args.end],
        'dates_total': len(dates), 'dates_done': len(dates) - len(todo) + len(todo) - len(fails),
        'fails': fails, 'finished_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    print(f"[{args.axis}] done: fails={len(fails)} manifest={manifest_path}", flush=True)
    return 2 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
