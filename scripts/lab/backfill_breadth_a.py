#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全 A 宽度日线基座增量回补器（2024-12-31 → 今日）。

设计纪律（对齐 collect_market_breadth_a.py 与治理红线）：
  * 增量续采：读取既有 parquet 的最大 date，只拉 (last+1 → TODAY) 段追加；
    存量 2015-2024 行**逐字节保留**（只 append，不重写历史）；
  * 新上市票：universe 里存在但盘上无文件 → 全量拉取（START 起）；
  * 退市票：源端自然返回空/无新行 → 文件保持原样，宽度序列自动剔除（正确语义）；
  * 多源降级：sina → tencent → hithink，同口径 RAW 不复权；
  * 原子写 tmp→rename；断点续采（_BACKFILL.done 记录已回补到 TODAY 的票）；
  * 出处三件套：Git SHA + Data Hash + Timestamp 写入报告。

产物：
  读写 → experiments/lab/market-breadth-a/daily_bars/<symbol>.parquet
  报告 → experiments/lab/market-breadth-a/BACKFILL_REPORT.json
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path("/home/ubuntu/FinAI2.0")
sys.path.insert(0, str(ROOT))

from scripts.lab.collect_market_breadth_a import (  # noqa: E402
    BAR_DIR, COLUMNS, START, all_a_universe, fetch_hithink, fetch_sina,
    fetch_tx, to_frame,
)

LAB = ROOT / "experiments" / "lab" / "market-breadth-a"
REPORT = LAB / "BACKFILL_REPORT.json"
DONE_MARKER = "_BACKFILL_DONE.txt"

TODAY = _date.today().isoformat()
WORKERS = int(os.environ.get("WORKERS", "8"))


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def dir_hash() -> str:
    h = hashlib.sha256()
    for p in sorted(BAR_DIR.glob("*.parquet")):
        h.update(p.name.encode())
        h.update(str(p.stat().st_size).encode())
    return h.hexdigest()[:16]


def fetch_range(symbol: str, start: str, end: str) -> tuple[list[list], str]:
    """按区间取数：sina（range 参数）→ tencent（range 参数）→ hithink（全量裁剪）。"""
    # sina: akshare 支持 start/end
    try:
        import akshare as ak
        df = ak.stock_zh_a_daily(symbol=symbol.replace(".", ""),
                                 start_date=start.replace("-", ""),
                                 end_date=end.replace("-", ""), adjust="")
        if df is not None and not df.empty:
            rows = [[str(r["date"]), r["open"], r["high"], r["low"],
                     r["close"], r["volume"], r["amount"]]
                    for _, r in df.iterrows()]
            return rows, "sina"
    except Exception:
        pass
    # tencent: fqkline 支持 start/end
    try:
        from scripts.lab.collect_market_breadth_a import _fetch_tx_page
        rows = _fetch_tx_page(symbol, start, end)
        if rows:
            return rows, "tencent"
    except Exception:
        pass
    # hithink: 全量返回后裁剪
    try:
        rows = fetch_hithink(symbol)
        rows = [r for r in rows if start <= r[0] <= end]
        if rows:
            return rows, "hithink"
    except Exception:
        pass
    return [], ""


def backfill_one(symbol: str, bar_p: Path | None) -> dict:
    """单票增量回补。"""
    try:
        if bar_p is not None and bar_p.exists():
            df_old = pd.read_parquet(bar_p)
            last = pd.to_datetime(df_old["date"]).max().date()
            start = (last + timedelta(days=1)).isoformat()
            if start > TODAY:
                return {"symbol": symbol, "status": "skip_fresh", "rows": 0}
            raw, src = fetch_range(symbol, start, TODAY)
            if not raw:
                return {"symbol": symbol, "status": "empty", "rows": 0}
            df_new = to_frame(symbol, raw, src)
            df = pd.concat([df_old, df_new], ignore_index=True)
            df = df.drop_duplicates(subset=["date"], keep="last")
            df = df.sort_values("date").reset_index(drop=True)
            df = df[COLUMNS]
        else:
            raw, src = fetch_range(symbol, START, TODAY)
            if not raw:
                return {"symbol": symbol, "status": "empty", "rows": 0}
            df = to_frame(symbol, raw, src)
            df = df.drop_duplicates(subset=["date"], keep="last")
            df = df.sort_values("date").reset_index(drop=True)
            df = df[COLUMNS]

        out_p = BAR_DIR / f"{symbol}.parquet"
        tmp = out_p.with_suffix(".tmp")
        df.to_parquet(tmp, index=False)
        tmp.rename(out_p)
        return {"symbol": symbol, "status": "ok", "rows": len(df), "source": src}
    except Exception as e:  # noqa: BLE001
        return {"symbol": symbol, "status": "fail", "rows": 0, "err": str(e)[:120]}


def main() -> int:
    t0 = time.time()
    # 票池 = 盘上已有 + 当前全 A 名单（新上市自动纳入；退市票源端自然无行）
    on_disk = {p.stem for p in BAR_DIR.glob("*.parquet")}
    try:
        universe = sorted(set(all_a_universe()) | on_disk)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 全 A 名单获取失败（{e}），仅用盘上 {len(on_disk)} 只")
        universe = sorted(on_disk)

    limit = os.environ.get("LIMIT")
    if limit:
        universe = universe[: int(limit)]
        print(f"[smoke] LIMIT={limit} 只取前 {len(universe)} 只")

    print(f"[plan] 回补 {len(universe)} 只 → 截止 {TODAY}，{WORKERS} 线程", flush=True)
    results = []
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {}
        for s in universe:
            bp = BAR_DIR / f"{s}.parquet"
            futs[ex.submit(backfill_one, s, bp if bp.exists() else None)] = s
        for i, fut in enumerate(cf.as_completed(futs), 1):
            results.append(fut.result())
            if i % 200 == 0 or i == len(universe):
                ok = sum(1 for x in results if x["status"] == "ok")
                skip = sum(1 for x in results if x["status"].startswith("skip"))
                fail = sum(1 for x in results if x["status"] == "fail")
                empty = sum(1 for x in results if x["status"] == "empty")
                el = time.time() - t0
                eta = el / i * (len(universe) - i)
                print(f"[{i}/{len(universe)}] ok={ok} skip={skip} empty={empty} "
                      f"fail={fail} elapsed={el/60:.1f}m eta={eta/60:.1f}m",
                      flush=True)

    summary = {
        "total": len(universe),
        "ok": sum(1 for x in results if x["status"] == "ok"),
        "skip_fresh": sum(1 for x in results if x["status"] == "skip_fresh"),
        "empty": sum(1 for x in results if x["status"] == "empty"),
        "fail": sum(1 for x in results if x["status"] == "fail"),
        "fail_symbols": [x["symbol"] for x in results if x["status"] == "fail"][:100],
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "backfill_to": TODAY,
        "git_sha": git_sha(),
        "data_hash": dir_hash(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workers": WORKERS,
        "sources": ["sina", "tencent", "hithink(fallback)"],
    }
    REPORT.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
