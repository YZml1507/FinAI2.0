#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e86 东财妙想 F10 分析师预测明细（原子层）全市场日频采集。

接口 RPT_HSF10_RES_PREDICTDETAIL：逐机构×逐分析师×发布日的
EPS/归母净利 4 年预测 + 评级。窗口≈近 6 个月滚动——无法回填历史，
故按日快照向前累积，rolling 90d 特征由下游 e87 因子族消费。

落盘：data/analyst_atomic_em/YYYYMMDD.parquet（日快照，全市场分页）。
幂等：同日重跑覆盖。

用法: python scripts/lab/pull_analyst_atomic_em.py [--date YYYYMMDD]
"""
import argparse
import sys
import time
from datetime import date as _date
from pathlib import Path

import pandas as pd
import requests

URL = ("https://datacenter.eastmoney.com/securities"
       "/api/data/v1/get")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://emweb.eastmoney.com/",
}
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "analyst_atomic_em"
PAGE_SIZE = 500
SLEEP = 0.35          # 实测 26QPS 上限内保守限速


def fetch_page(page: int) -> dict:
    params = {
        "reportName": "RPT_HSF10_RES_PREDICTDETAIL",
        "columns": "ALL",
        "pageNumber": page, "pageSize": PAGE_SIZE,
        "sortColumns": "PUBLISH_DATE", "sortTypes": "-1",
        "source": "HSF10", "client": "PC",
    }
    last = None
    for i in range(4):
        try:
            r = requests.get(URL, params=params, headers=HEADERS,
                             timeout=20)
            d = r.json()
            if d.get("success"):
                return d.get("result") or {}
            last = RuntimeError(str(d)[:200])
        except Exception as e:        # noqa: BLE001
            last = e
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"page {page} 失败: {last}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="快照日期 YYYYMMDD")
    args = ap.parse_args()
    day = args.date or _date.today().strftime("%Y%m%d")
    OUT.mkdir(parents=True, exist_ok=True)

    res = fetch_page(1)
    pages = int(res.get("pages") or 1)
    count = int(res.get("count") or 0)
    rows = list(res.get("data") or [])
    for p in range(2, pages + 1):
        time.sleep(SLEEP)
        rows.extend(fetch_page(p).get("data") or [])
        if p % 10 == 0:
            print(f"  {p}/{pages} pages, {len(rows)} rows", flush=True)

    df = pd.DataFrame(rows)
    df["pull_date"] = day
    dst = OUT / f"{day}.parquet"
    df.to_parquet(dst, index=False)
    assert len(df) >= count * 0.95, f"行数 {len(df)} < count {count}×0.95"
    print(f"-> {dst} rows={len(df)} count={count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
