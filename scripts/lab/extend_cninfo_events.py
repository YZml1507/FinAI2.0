"""续采 cninfo 关键词事件族到 2026（OOS-2025 特征输入）。

落盘与既有产物同构:
  data/cninfo_events/{kw}_{YYYYMM}.parquet  —— reduce/frozen 等特征源
  data/esop_events/{kw}_{YYYYMM}.parquet    —— incentive 特征源
schema: {ann_date, ts_code, kw, title, url}

水位幂等: 月分片已存在即跳过; 末月(202412)→2025-01 起采至 TODAY。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from data.collector import _atomic_write_parquet  # noqa: E402

API = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
BASE = "http://static.cninfo.com.cn/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 30
PAGE_SIZE = 30
TODAY = "2026-09-22"

TARGETS = {
    "cninfo_events": ["减持计划", "司法冻结", "增持计划", "权益变动", "股权质押", "高送转"],
    "esop_events": ["员工持股计划", "非公开发行"],
}


def _months(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[5:7])
    out = []
    while f"{y}{m:02d}" <= end[:7].replace("-", ""):
        out.append(f"{y}{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _month_range(yyyymm: str, end: str) -> tuple[str, str]:
    y, m = int(yyyymm[:4]), int(yyyymm[4:6])
    first = f"{y}-{m:02d}-01"
    if m == 12:
        nxt = date(y + 1, 1, 1)
    else:
        nxt = date(y, m + 1, 1)
    last = min(pd.Timestamp(nxt) - pd.Timedelta(days=1), pd.Timestamp(end))
    return first, last.strftime("%Y-%m-%d")


def _query_kw(sess: requests.Session, kw: str, first: str, last: str,
              sleep: float) -> pd.DataFrame:
    rows: list[dict] = []
    page = 1
    while True:
        payload = {
            "pageNum": str(page), "pageSize": str(PAGE_SIZE),
            "column": "", "tabName": "fulltext", "plate": "",
            "searchkey": kw, "secid": "", "category": "", "trade": "",
            "seDate": f"{first}~{last}", "sortName": "", "sortType": "",
            "isHLtitle": "true",
        }
        for attempt in range(3):
            try:
                r = sess.post(API, data=payload, timeout=TIMEOUT)
                js = r.json()
                break
            except Exception as exc:
                if attempt == 2:
                    raise
                time.sleep(sleep * (attempt + 2))
        anns = js.get("announcements") or []
        for a in anns:
            rows.append({
                "ann_date": datetime.fromtimestamp(
                    a["announcementTime"] / 1000, tz=timezone.utc).date(),
                "ts_code": a.get("secCode", ""),
                "kw": kw,
                "title": (a.get("announcementTitle") or "").replace(
                    "<em>", "").replace("</em>", ""),
                "url": BASE + (a.get("adjunctUrl") or ""),
            })
        total = js.get("totalAnnouncement") or 0
        if page * PAGE_SIZE >= total or not anns:
            break
        page += 1
        time.sleep(sleep)
    df = pd.DataFrame(rows)
    if not df.empty:
        df = (df.drop_duplicates(["ann_date", "ts_code", "title"])
                .sort_values(["ann_date", "ts_code"], kind="stable")
                .reset_index(drop=True))
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01")
    ap.add_argument("--end", default=TODAY)
    ap.add_argument("--sleep", type=float, default=0.35)
    ap.add_argument("--groups", default="cninfo_events,esop_events")
    args = ap.parse_args()

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Referer": "http://www.cninfo.com.cn/new/fulltextSearch",
        "X-Requested-With": "XMLHttpRequest",
    })
    months = _months(args.start, args.end)
    log = []
    for group in args.groups.split(","):
        out_dir = _root / "data" / group
        out_dir.mkdir(parents=True, exist_ok=True)
        for kw in TARGETS[group]:
            for ym in months:
                fp = out_dir / f"{kw}_{ym}.parquet"
                if fp.exists():
                    continue
                first, last = _month_range(ym, args.end)
                try:
                    df = _query_kw(sess, kw, first, last, args.sleep)
                except Exception as exc:
                    print(f"  {kw}_{ym} FAIL {type(exc).__name__}", flush=True)
                    continue
                _atomic_write_parquet(df, fp)
                log.append({"group": group, "kw": kw, "month": ym,
                            "rows": len(df)})
                if log and len(log) % 10 == 0:
                    print(f"  ...{len(log)} files", flush=True)
                time.sleep(args.sleep)
            print(f"{group}/{kw} done", flush=True)
    (_root / "data" / "_cninfo_extend_log.json").write_text(
        json.dumps({"finished": datetime.now(timezone.utc).isoformat(),
                    "files": log}, ensure_ascii=False, indent=1))
    print(f"DONE files={len(log)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
