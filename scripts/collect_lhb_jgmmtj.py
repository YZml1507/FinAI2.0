"""Collect 龙虎榜机构席位买卖统计 (stock_lhb_jgmmtj_em) full history.

Per-stock institutional seat net buy on lhb events, 2015-2024.
Output: data/lhb_jgmmtj/{YYYYMM}.parquet (monthly partitions), idempotent.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import akshare as ak
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "lhb_jgmmtj"
MANIFEST = OUT / "_manifest.json"


def month_chunks(start: str, end: str) -> list[tuple[str, str]]:
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    out = []
    cur = s
    while cur <= e:
        m_end = (cur + pd.offsets.MonthEnd(0))
        out.append((cur.strftime("%Y%m%d"), min(m_end, e).strftime("%Y%m%d")))
        cur = m_end + pd.Timedelta(days=1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20150105")
    ap.add_argument("--end", default="20241231")
    ap.add_argument("--pace", type=float, default=0.8)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {}
    if MANIFEST.exists():
        manifest = json.loads(MANIFEST.read_text())

    fail_streak = 0
    for s, e in month_chunks(args.start, args.end):
        key = f"{s[:6]}"
        out_path = OUT / f"{key}.parquet"
        if out_path.exists() and manifest.get(key, {}).get("ok"):
            continue
        try:
            df = ak.stock_lhb_jgmmtj_em(start_date=s, end_date=e)
        except Exception as exc:
            fail_streak += 1
            print(f"[{key}] FAIL {type(exc).__name__}: {exc}", flush=True)
            if fail_streak >= 10:
                print("fail streak >=10, stopping", flush=True)
                return 2
            time.sleep(5 * fail_streak)
            continue
        fail_streak = 0
        df = df.copy()
        df.columns = [str(c) for c in df.columns]
        df.to_parquet(out_path, index=False)
        manifest[key] = {"ok": True, "rows": int(len(df)), "range": [s, e]}
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
        print(f"[{key}] rows={len(df)}", flush=True)
        time.sleep(args.pace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
