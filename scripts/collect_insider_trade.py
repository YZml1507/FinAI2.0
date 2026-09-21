#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""内部人（董监高及关联人）增减持明细采集器。

数据源：东财 datacenter-web ``reportName=RPT_EXECUTIVE_HOLD_DETAILS``
（akshare ``stock_hold_management_detail_em`` 背后同一端点），全 A 全历史
持股变动明细。服务端 ``filter=`` 按 ``CHANGE_DATE`` 截段，本脚本默认采
[2015-01-01, 2024-12-31]（约 228 页 × 5000 ≈ 114 万行）。

⚠ 该端点**没有公告日期列**：PIT 锚只能取 CHANGE_DATE+保守滞后（另案处理）。

落盘：
- ``data/insider_trade/detail_2015_2024.parquet``（单文件，``_atomic_write_parquet``）
- ``data/insider_trade/_manifest.json``（总行数/失败页/时间戳/success 标志）

幂等：parquet 已存在且 manifest ``success=true`` → 跳过（``--force`` 强刷）。
限速：每次请求间隔 ``--sleep``（默认 0.5s，≥0.4s）；单页失败重试 ≤3 次，
重试耗尽后记入 ``failed_pages`` 继续采下一页，manifest ``success=false``
（下次运行会重采整段，不跳过）。

用法：.venv/bin/python scripts/collect_insider_trade.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from data.collector import _atomic_write_parquet  # noqa: E402

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT = "RPT_EXECUTIVE_HOLD_DETAILS"
PAGE_SIZE = 5000
OUT_DIR = _root / "data" / "insider_trade"
OUT_FILE = OUT_DIR / "detail_2015_2024.parquet"
MANIFEST = OUT_DIR / "_manifest.json"
RETRIES = 3
TIMEOUT = 30
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fetch_page(session: requests.Session, date_filter: str, page: int) -> dict:
    """单页请求 → 响应 ``result`` dict；任何异常/坏响应抛给上层记重试。"""
    params = {
        "reportName": REPORT,
        "columns": "ALL",
        "quoteColumns": "",
        "filter": date_filter,
        "pageNumber": str(page),
        "pageSize": str(PAGE_SIZE),
        "sortTypes": "-1,1,1",
        "sortColumns": "CHANGE_DATE,SECURITY_CODE,PERSON_NAME",
        "source": "WEB",
        "client": "WEB",
        "p": str(page),
        "pageNo": str(page),
        "pageNum": str(page),
    }
    r = session.get(URL, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    result = r.json().get("result")
    if not isinstance(result, dict) or "data" not in result:
        raise ValueError(f"unexpected payload shape: {r.text[:200]}")
    return result


def _fetch_page_retry(session: requests.Session, date_filter: str,
                      page: int, sleep: float) -> dict:
    """重试 ≤ RETRIES 次；耗尽后抛最后一次异常。"""
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            return _fetch_page(session, date_filter, page)
        except Exception as exc:  # noqa: BLE001 — 网络/解析失败统一重试
            last = exc
            print(f"    page {page} attempt {attempt}/{RETRIES} "
                  f"failed: {type(exc).__name__} {str(exc)[:120]}",
                  flush=True)
            time.sleep(max(sleep, 1.0) * attempt)
    raise last  # type: ignore[misc]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--sleep", type=float, default=0.5)
    ap.add_argument("--limit", type=int, default=0,
                    help="只采前 N 页（冒烟用）")
    ap.add_argument("--force", action="store_true",
                    help="忽略幂等跳过，强制重采")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.sleep < 0.4:
        print("⛔ --sleep 必须 ≥0.4s")
        return 2

    date_filter = (f"(CHANGE_DATE>='{args.start}')"
                   f"(CHANGE_DATE<='{args.end}')")

    # 幂等：成功产物已存在 → 直接跳过。
    if not args.force and OUT_FILE.exists() and MANIFEST.exists():
        try:
            m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — manifest 坏了就重采
            m = {}
        if m.get("success") is True:
            print(f"skip: {OUT_FILE} 已存在且 manifest success "
                  f"(rows={m.get('total_rows')}, "
                  f"{m.get('date_min')}→{m.get('date_max')})")
            return 0

    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Referer": "https://data.eastmoney.com/executive/list.html",
    })

    t0 = time.time()
    started = _utc_iso()
    first = _fetch_page_retry(session, date_filter, 1, args.sleep)
    total_pages = int(first.get("pages") or 0)
    expected = int(first.get("count") or 0)
    print(f"filter={date_filter} pages={total_pages} "
          f"count={expected} pageSize={PAGE_SIZE}", flush=True)
    if args.dry_run:
        print(f"dry-run: 首页 rows={len(first['data'])}")
        return 0

    n_pages = min(total_pages, args.limit) if args.limit else total_pages
    frames: list[pd.DataFrame] = [pd.DataFrame(first["data"])]
    failed_pages: list[int] = []
    fetched = len(first["data"])

    for page in range(2, n_pages + 1):
        time.sleep(args.sleep)
        try:
            result = _fetch_page_retry(session, date_filter, page, args.sleep)
        except Exception as exc:  # noqa: BLE001 — 记失败页继续采
            failed_pages.append(page)
            print(f"  page {page}/{total_pages} GIVE-UP: "
                  f"{type(exc).__name__} {str(exc)[:120]}", flush=True)
            continue
        rows = len(result["data"])
        fetched += rows
        frames.append(pd.DataFrame(result["data"]))
        if page % 20 == 0 or page == n_pages:
            print(f"  page {page}/{total_pages} rows={rows} "
                  f"cum={fetched}", flush=True)

    big = (pd.concat(frames, ignore_index=True)
           if frames else pd.DataFrame())
    if not big.empty:
        big["CHANGE_DATE"] = pd.to_datetime(
            big["CHANGE_DATE"], errors="coerce").dt.date
        big = (big.sort_values(["CHANGE_DATE", "SECURITY_CODE", "PERSON_NAME"],
                               kind="stable")
                  .reset_index(drop=True))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_parquet(big, OUT_FILE)

    manifest = {
        "reportName": REPORT,
        "source_url": URL,
        "filter": date_filter,
        "range": [args.start, args.end],
        "page_size": PAGE_SIZE,
        "total_pages": total_pages,
        "pages_fetched": n_pages - len(failed_pages),
        "expected_count": expected,
        "total_rows": int(len(big)),
        "unique_securities": (int(big["SECURITY_CODE"].nunique())
                              if not big.empty else 0),
        "date_min": (str(big["CHANGE_DATE"].min())
                     if not big.empty else None),
        "date_max": (str(big["CHANGE_DATE"].max())
                     if not big.empty else None),
        "failed_pages": failed_pages,
        "success": not failed_pages and len(big) == expected,
        "started_at": started,
        "finished_at": _utc_iso(),
        "duration_sec": round(time.time() - t0, 1),
        "note": ("无公告日期列；PIT 锚需 CHANGE_DATE+保守滞后（另案）。"
                 "success=true 要求零失败页且实采行数=服务端 count"),
    }
    tmp = MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(MANIFEST)

    print(f"done: rows={len(big)} expected={expected} "
          f"date {manifest['date_min']}→{manifest['date_max']} "
          f"unique={manifest['unique_securities']} "
          f"failed_pages={len(failed_pages)} "
          f"→ {OUT_FILE}", flush=True)
    return 0 if manifest["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
