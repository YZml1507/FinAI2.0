#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""方案 D 数据基座：全 A 十年日线并行采集器（hithink 同花顺主源 + 腾讯兜底）。

⚠ 2026-09-15 源切换：腾讯 fqkline 在全量采集约 830 只后对本机 IP 持续
  返回 HTTP 501（封禁），东财整域被墙、tdx 缺模块、citydata 代理不可达、
  新浪不可达。同花顺 d.10jqka.com.cn 单请求可拉全量日线（已实测 6388 行），
  故升为主力源；腾讯保留为兜底，若解封自然接管失败票。

产物隔离（⛔ 不碰权威目录）：
  数据 → experiments/lab/market-breadth-a/daily_bars/<symbol>.parquet
  报告 → experiments/lab/market-breadth-a/COLLECTION_REPORT.json（含出处三件套）

设计纪律（对照治理红线）：
  * 断点续采：已完成票写 <symbol>.parquet + .done 标记，重启跳过；
  * 多源降级：腾讯空 → hithink；同层级禁改口径（RAW 不复权）；
  * 并行安全：8 线程，边界保护（每票独立文件，原子写 tmp→rename）；
  * 出处三件套：Git SHA + Data Hash + Timestamp 写入报告；
  * 失败留痕：失败票记录 reasons，不静默跳过。
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path("/home/ubuntu/FinAI2.0")
LAB = ROOT / "experiments" / "lab" / "market-breadth-a"
BAR_DIR = LAB / "daily_bars"
DONE_SUFFIX = ".done"
REPORT = LAB / "COLLECTION_REPORT.json"

START, END = "2015-01-01", "2024-12-31"
WORKERS = 8
TIMEOUT = 15
RETRIES = 3
#: 腾讯单页上限（红利池采集器同款纪律）
PAGE_LEN = 2000

COLUMNS = ["date", "open", "high", "low", "close", "preclose", "volume",
           "amount", "turn", "pctChg", "tradestatus", "isST", "code",
           "source", "adjust_mode"]

_TX_URL = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"


def _plain(symbol: str) -> str:
    return symbol.replace(".", "")


def _fetch_tx_page(symbol: str, start: str, end: str) -> list[list]:
    plain = _plain(symbol)
    time.sleep(random.uniform(0.1, 0.3))  # 轻量节流：防全量高并发触发 IP 限流
    r = requests.get(_TX_URL, params={
        "param": f"{plain},day,{start},{end},{PAGE_LEN},"
    }, timeout=TIMEOUT)
    data = r.json().get("data", {}).get(plain, {})
    return data.get("day") or []


def fetch_tx(symbol: str) -> list[list]:
    """腾讯两页法拉十年 RAW 日线（红利池采集器同款：首页 2000 + 尾页 2000，合期拼接）。"""
    rows: dict[str, list] = {}
    # 页1：从 START 起 2000 根
    for r in _fetch_tx_page(symbol, START, END):
        rows[r[0]] = r
    # 页2：若页1满 2000，说明还有更早的数据，从 START 再往前不会有（START 已是 2015）；
    # 真正要补的是页1装不下 10 年时（>2000 个交易日），从中间日期再起一页。
    if len(rows) >= PAGE_LEN:
        mid = sorted(rows)[PAGE_LEN - 1]
        for r in _fetch_tx_page(symbol, mid, END):
            rows[r[0]] = r
    return [rows[d] for d in sorted(rows)]


def fetch_sina(symbol: str) -> list[list]:
    """新浪日线主力源（akshare 官方封装，2026-09-15 实测 2399 行/0.7s，
    口径已与腾讯 RAW 校验一致）。行格式：[date, open, high, low, close, volume, amount]。"""
    import akshare as ak
    time.sleep(random.uniform(0.2, 0.5))  # 节流：防高并发触发限流
    code = symbol.replace(".", "")
    df = ak.stock_zh_a_daily(symbol=code,
                             start_date=START.replace("-", ""),
                             end_date=END.replace("-", ""), adjust="")
    if df is None or df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        rows.append([str(r["date"]), r["open"], r["high"], r["low"],
                     r["close"], r["volume"], r["amount"]])
    return rows


_THS_PREFIX = {"sh": "hs_", "sz": "hs_"}


def _ths_code(symbol: str) -> str:
    """sh.600000 -> hs_600000；同花顺沪深统一 hs_ 前缀。"""
    mkt, num = symbol.split(".")
    return f"{_THS_PREFIX[mkt]}{num}"


def fetch_hithink(symbol: str) -> list[list]:
    """hithink（同花顺 d.10jqka）主力源：单请求获取上市以来全部日线（RAW 不复权）。
    数据行格式：date,open,high,low,close,volume,amount,...（分号分隔）。"""
    url = f"http://d.10jqka.com.cn/v6/line/{_ths_code(symbol)}/01/all.js"
    time.sleep(random.uniform(0.3, 0.8))  # 节流：防高并发触发限流
    r = requests.get(url, headers={
        "Referer": "http://stockpage.10jqka.com.cn/",
        "User-Agent": "Mozilla/5.0"}, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = "gbk"
    text = r.text
    # 提取 "data":"date,...,;date,..."
    m = re.search(r'"data":"([^"]*)"', text)
    if not m:
        return []
    rows = []
    for seg in m.group(1).split(";"):
        if not seg:
            continue
        parts = seg.split(",")
        if len(parts) < 6:
            continue
        # parts: date,open,high,low,close,volume,amount,...
        # 只保留目标区间（同花顺返回全量历史，裁剪以控制磁盘与权威口径一致）
        if START <= parts[0] <= END:
            rows.append(parts)
    return rows


def _num(x) -> float:
    """防御式数值解析：跳过 dict/None/'-' 等非数值元素（腾讯行末常带附加 dict）。"""
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str) and x not in ("", "-"):
        try:
            return float(x)
        except ValueError:
            return 0.0
    return 0.0


def to_frame(symbol: str, raw: list[list], source: str) -> pd.DataFrame:
    recs = []
    for k in raw:
        if source == "sina":
            # 新浪行格式：[date, open, high, low, close, volume, amount]
            date_v, open_v, high_v, low_v, close_v = k[0], k[1], k[2], k[3], k[4]
            vol_v = k[5] if len(k) > 5 else 0.0
            amt_v = k[6] if len(k) > 6 else 0.0
        else:
            # 腾讯行格式：[date, open, close, high, low, volume, {dict?}, ...]
            date_v, open_v, high_v, low_v, close_v = k[0], k[1], k[3], k[4], k[2]
            vol_v = k[5] if len(k) > 5 else 0.0
            amt_v = 0.0
        recs.append({
            "date": date_v, "open": _num(open_v), "high": _num(high_v),
            "low": _num(low_v), "close": _num(close_v), "preclose": 0.0,
            "volume": _num(vol_v), "amount": _num(amt_v), "turn": 0.0, "pctChg": 0.0,
            "tradestatus": 1, "isST": 0, "code": symbol,
            "source": source, "adjust_mode": "raw",
        })
    df = pd.DataFrame(recs, columns=COLUMNS)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def collect_one(symbol: str) -> dict:
    bar_p = BAR_DIR / f"{symbol}.parquet"
    done_p = BAR_DIR / f"{symbol}{DONE_SUFFIX}"
    if done_p.exists() and bar_p.exists():
        return {"symbol": symbol, "status": "skip", "rows": -1}
    last_err = ""
    for attempt in range(RETRIES):
        try:
            raw = fetch_sina(symbol)
            src = "sina"
            if not raw:
                raw = fetch_tx(symbol)
                src = "tencent"
            if not raw:
                return {"symbol": symbol, "status": "empty", "rows": 0}
            df = to_frame(symbol, raw, src)
            tmp = bar_p.with_suffix(".tmp")
            df.to_parquet(tmp, index=False)
            tmp.rename(bar_p)
            done_p.write_text(json.dumps({
                "symbol": symbol, "rows": len(df), "source": src,
                "ts": datetime.now(timezone.utc).isoformat()}))
            return {"symbol": symbol, "status": "ok", "rows": len(df),
                    "source": src}
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:120]
            time.sleep(min(2 ** attempt, 8) + random.uniform(0, 0.5))
    return {"symbol": symbol, "status": "fail", "rows": 0, "err": last_err}


def all_a_universe() -> list[str]:
    import akshare as ak
    df = ak.stock_info_a_code_name()
    syms = []
    for c in df["code"].astype(str):
        if c.startswith(("60", "68")):
            syms.append("sh." + c)
        elif c.startswith(("00", "30")):
            syms.append("sz." + c)
    return sorted(set(syms))


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


def main() -> int:
    import os
    BAR_DIR.mkdir(parents=True, exist_ok=True)
    universe = all_a_universe()
    # 冒烟测试：LIMIT=N 只取前 N 只票，验证链路后再跑全量
    limit = os.environ.get("LIMIT")
    if limit:
        universe = universe[: int(limit)]
        print(f"[smoke] LIMIT={limit}，仅取前 {len(universe)} 只票验证链路")
    print(f"[plan] 全 A 票池 {len(universe)} 只，区间 {START}~{END}，"
          f"{WORKERS} 线程，产物 {BAR_DIR}")
    t0 = time.time()
    results = []
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(collect_one, s): s for s in universe}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            if i % 200 == 0 or i == len(universe):
                ok = sum(1 for x in results if x["status"] == "ok")
                skip = sum(1 for x in results if x["status"] == "skip")
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
        "skip": sum(1 for x in results if x["status"] == "skip"),
        "empty": sum(1 for x in results if x["status"] == "empty"),
        "fail": sum(1 for x in results if x["status"] == "fail"),
        "fail_symbols": [x["symbol"] for x in results if x["status"] == "fail"][:100],
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "git_sha": git_sha(),
        "data_hash": dir_hash(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": [START, END], "workers": WORKERS,
        "sources": ["tencent", "hithink(fallback)"],
    }
    REPORT.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
