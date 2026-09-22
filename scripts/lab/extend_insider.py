"""续采内部人增减持明细到 2026（OOS-2025 insider_sell 特征输入）。

复用 scripts/collect_insider_trade.py 的分页实现（东财
RPT_EXECUTIVE_HOLD_DETAILS），写 detail_2025_2026.parquet + 独立 manifest。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "scripts"))

from collect_insider_trade import (  # noqa: E402
    PAGE_SIZE, UA, _fetch_page_retry)
from data.collector import _atomic_write_parquet  # noqa: E402

OUT_DIR = _root / "data" / "insider_trade"
OUT_FILE = OUT_DIR / "detail_2025_2026.parquet"
MANIFEST = OUT_DIR / "_manifest_2025_2026.json"


def main() -> int:
    start, end, sleep = "2025-01-01", "2026-09-22", 0.5
    date_filter = f"(CHANGE_DATE>='{start}')(CHANGE_DATE<='{end}')"
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA,
                         "Referer": "https://data.eastmoney.com/executive/list.html"})
    t0 = time.time()
    first = _fetch_page_retry(sess, date_filter, 1, sleep)
    total_pages = int(first.get("pages") or 0)
    expected = int(first.get("count") or 0)
    print(f"pages={total_pages} count={expected}", flush=True)
    frames = [pd.DataFrame(first["data"])]
    failed = []
    for page in range(2, total_pages + 1):
        time.sleep(sleep)
        try:
            r = _fetch_page_retry(sess, date_filter, page, sleep)
        except Exception:
            failed.append(page)
            continue
        frames.append(pd.DataFrame(r["data"]))
        if page % 10 == 0 or page == total_pages:
            print(f"  page {page}/{total_pages}", flush=True)
    big = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not big.empty:
        big["CHANGE_DATE"] = pd.to_datetime(
            big["CHANGE_DATE"], errors="coerce").dt.date
        big = (big.sort_values(["CHANGE_DATE", "SECURITY_CODE", "PERSON_NAME"],
                               kind="stable").reset_index(drop=True))
    _atomic_write_parquet(big, OUT_FILE)
    MANIFEST.write_text(json.dumps({
        "reportName": "RPT_EXECUTIVE_HOLD_DETAILS", "filter": date_filter,
        "range": [start, end], "total_pages": total_pages,
        "expected_count": expected, "total_rows": len(big),
        "failed_pages": failed,
        "success": not failed and len(big) == expected,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_sec": round(time.time() - t0, 1)},
        ensure_ascii=False, indent=1))
    print(f"DONE rows={len(big)} failed={len(failed)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
