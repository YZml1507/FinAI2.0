#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全 A 个股资金流采集：efinance get_history_bill 历史资金流逐股落盘。

数据源 = finai.sources.money_flow_source.MoneyFlowCollector（东财 push2his 主源；
备份腿因 FINDING-554 禁用，backup_enabled 保持 False，本脚本不改适配层口径）。

清单：data/daily_basic_alla/*.parquet 的 ts_code 并集（全 A 覆盖，含已退市）。
落盘：data/money_flow/{ts_code}.parquet（幂等：已存在且非空跳过，原子写）。
台账：data/money_flow/_manifest.json 记 fails / 汇总。

限速 pace_s>=0.15；熔断沿用适配层参数（连续连接级失败每 5 次冷却 45s）。
网络层连续失败 >30 次：暂停 10min 重试一次；仍失败则如实报告并停止。

用法：.venv/bin/python scripts/collect_money_flow_alla.py [--limit N] [--pace 0.15] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from data.collector import _atomic_write_parquet  # noqa: E402
from finai.sources.money_flow_source import (  # noqa: E402
    CONTRACT_COLUMNS, MoneyFlowCollector)

DV_DIR = _root / 'data/daily_basic_alla'
OUT_DIR = _root / 'data/money_flow'

# 与适配层一致的连接级失败签名（FINDING-175/555：断连/超时/代理/空体或
# 非 JSON 限流响应）；命中才计入网络连续失败 streak。
_NET_SIGNS = (
    "remote disconnected", "connection aborted", "max retries",
    "timed out", "timeout", "proxy", "unreachable", "ssl",
    "expecting value", "json decode", "empty response",
)

CIRCUIT_BREAK = 5            # 适配层 circuit_break
CIRCUIT_COOLDOWN_S = 45.0    # 适配层 circuit_cooldown_s
HARD_STREAK = 30             # 网络层连续失败 >30 → 暂停 10min
LONG_PAUSE_S = 600.0


def _load_codes() -> list[str]:
    codes: set[str] = set()
    for f in sorted(DV_DIR.glob('*.parquet')):
        codes.update(pq.read_table(f, columns=['ts_code'])['ts_code'].to_pylist())
    return sorted(codes)


def _is_net_fail(err: str) -> bool:
    msg = err.lower()
    return any(k in msg for k in _NET_SIGNS)


def _existing_rows(path: Path) -> int:
    """已落盘文件的行数；不存在/损坏/空文件视为未完成。"""
    try:
        return pq.read_metadata(path).num_rows if path.exists() else 0
    except Exception:  # noqa: BLE001
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--pace', type=float, default=0.15)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    pace = max(args.pace, 0.15)  # 限速下限，不许更快

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT_DIR / '_manifest.json'

    codes = _load_codes()
    todo = [c for c in codes
            if _existing_rows(OUT_DIR / f'{c}.parquet') == 0]
    if args.limit:
        todo = todo[:args.limit]
    print(f"[money_flow] 全A清单 {len(codes)}，已完成 {len(codes)-len(todo)}，待采 {len(todo)}",
          flush=True)
    if args.dry_run:
        for c in todo[:5]:
            print("  would fetch", c)
        return 0

    collector = MoneyFlowCollector(backup_enabled=False)
    fails: list[dict[str, str]] = []
    rows_new = 0
    net_streak = 0
    paused_once = False
    stopped_reason = None
    t0 = time.time()

    for i, ts_code in enumerate(todo):
        bare = ts_code.split('.')[0]
        try:
            df = collector.fetch(bare)
            if df is None or len(df) == 0:
                raise RuntimeError("0 rows")
            df = df.copy()
            df['ts_code'] = ts_code  # 统一 .SH/.SZ/.BJ 后缀口径（适配层输出为裸码）
            df = df[CONTRACT_COLUMNS]
            _atomic_write_parquet(df, OUT_DIR / f'{ts_code}.parquet')
            rows_new += len(df)
            net_streak = 0
            paused_once = False
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            fails.append({'code': ts_code, 'err': err[:300]})
            if _is_net_fail(err):
                net_streak += 1
                if net_streak > HARD_STREAK:
                    if paused_once:
                        stopped_reason = (
                            f"网络连续失败 {net_streak} 次且 10min 暂停后重试仍失败，停止于 {ts_code}")
                        break
                    print(f"[money_flow] 网络连续失败 {net_streak}>{HARD_STREAK}，"
                          f"暂停 {LONG_PAUSE_S/60:.0f}min 后重试一次", flush=True)
                    time.sleep(LONG_PAUSE_S)
                    paused_once = True
                elif net_streak % CIRCUIT_BREAK == 0:
                    print(f"[money_flow] circuit break: {net_streak} 连续连接失败，"
                          f"冷却 {CIRCUIT_COOLDOWN_S:.0f}s（于 {ts_code}）", flush=True)
                    time.sleep(CIRCUIT_COOLDOWN_S)
            else:
                net_streak = 0  # 服务端有响应（如空数据/退市票），网络正常
        if pace > 0:
            time.sleep(pace)
        if (i + 1) % 100 == 0:
            rate = (i + 1) / max(time.time() - t0, 1)
            print(f"[money_flow] {i+1}/{len(todo)} ok={i+1-len(fails)} fail={len(fails)} "
                  f"rate={rate:.2f}/s eta={(len(todo)-i-1)/max(rate,1e-9)/60:.0f}min",
                  flush=True)
            _write_manifest(manifest_path, codes, todo, fails, rows_new, stopped_reason)

    summary, readback_errs = _final_stats()
    _write_manifest(manifest_path, codes, todo, fails + readback_errs,
                    rows_new, stopped_reason, summary)
    print(f"[money_flow] 结束：股票数={summary['files']} 总行数={summary['rows_total']} "
          f"失败数={len(fails)} 日期范围={summary['date_min']}~{summary['date_max']}",
          flush=True)
    if stopped_reason:
        print(f"[money_flow] 中止原因：{stopped_reason}", flush=True)
        return 3
    return 2 if fails else 0


def _write_manifest(path: Path, codes: list[str], todo: list[str],
                    fails: list[dict[str, str]], rows_new: int,
                    stopped_reason: str | None,
                    summary: dict | None = None) -> None:
    manifest = {
        'axis': 'money_flow',
        'source': 'efinance_eastmoney',
        'codes_total': len(codes),
        'todo': len(todo),
        'attempted': len(todo),
        'stocks_failed': len(fails),
        'rows_written_this_run': rows_new,
        'fails': fails,
        'stopped_reason': stopped_reason,
        'finished_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    if summary:
        manifest['final'] = summary
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    tmp.replace(path)


def _final_stats() -> tuple[dict, list[dict[str, str]]]:
    """扫 data/money_flow 全量文件：文件数/总行数/日期范围（如实口径）。"""
    rows_total = 0
    date_min = date_max = None
    files = 0
    readback_errs: list[dict[str, str]] = []
    for p in sorted(OUT_DIR.glob('*.parquet')):
        try:
            meta = pq.read_metadata(p)
            if meta.num_rows == 0:
                continue
            dates = pq.read_table(p, columns=['trade_date'])['trade_date']
            dmin = pc.min(dates).as_py()
            dmax = pc.max(dates).as_py()
            files += 1
            rows_total += meta.num_rows
            date_min = dmin if date_min is None or dmin < date_min else date_min
            date_max = dmax if date_max is None or dmax > date_max else date_max
        except Exception as e:  # noqa: BLE001
            readback_errs.append({'code': p.stem, 'err': f"readback: {type(e).__name__}: {e}"})
    return ({'files': files, 'rows_total': rows_total,
             'date_min': date_min, 'date_max': date_max}, readback_errs)


if __name__ == '__main__':
    sys.exit(main())
