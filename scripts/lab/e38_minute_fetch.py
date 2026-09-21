#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e38 分钟线采样采集器（幂等：已有文件跳过）。

样本：487 池（data/dividend_stocks/ 488 标的 − sh.000300 指数）等距抽 30 只，
窗口 = 指数日线最近 60 个交易日（以 sh.000300 parquet 日历为准）。
源：baostock ``query_history_k_data_plus`` frequency='5', adjustflag='3'（不复权，
与仓内腾讯 RAW 日线同口径）。

实测注意（本窗口已验证）：baostock 5m 在**停牌日吐全零行**（open=volume=0），
落盘前滤掉 ``volume<=0`` 的行；字段 date,time,open..amount,adjustflag，
time 为 bar **结束**时间戳（YYYYmmddHHMMSSmmm），48 根/日。

落盘：data/minute_probe/{symbol}.parquet（data/ 已 gitignore）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DSTOCKS = ROOT / "data" / "dividend_stocks"
OUT = ROOT / "data" / "minute_probe"
N_SAMPLE = 30
N_DAYS = 60
FIELDS = "date,time,code,open,high,low,close,volume,amount,adjustflag"


def pool_symbols() -> list[str]:
    syms = sorted(
        p.name for p in DSTOCKS.iterdir()
        if p.is_dir() and p.name[:3] in ("sh.", "sz.") and p.name != "sh.000300"
    )
    assert len(syms) == 487, f"pool size {len(syms)} != 487"
    return syms


def pick_sample(syms: list[str], n: int) -> list[str]:
    stride = len(syms) / n
    return [syms[int(i * stride)] for i in range(n)]


def last_trading_days(n: int) -> list[str]:
    dfs = []
    for y in ("2026", "2025"):
        f = DSTOCKS / "sh.000300" / f"{y}.parquet"
        if f.exists():
            dfs.append(pd.read_parquet(f, columns=["date"]))
    cal = sorted(set(pd.concat(dfs)["date"].astype(str)))
    return cal[-n:]


def fetch(bs, sym: str, start: str, end: str) -> pd.DataFrame:
    rs = bs.query_history_k_data_plus(
        sym, FIELDS, start_date=start, end_date=end,
        frequency="5", adjustflag="3")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    if rs.error_code != "0":
        raise RuntimeError(f"{sym} baostock {rs.error_code} {rs.error_msg}")
    df = pd.DataFrame(rows, columns=rs.fields)
    df["hhmm"] = df["time"].str[8:12]
    for c in ("open", "high", "low", "close", "amount"):
        df[c] = df[c].astype(float)
    df["volume"] = df["volume"].astype(float)
    df = df[df["volume"] > 0].drop(columns=["adjustflag"])  # 停牌全零行剔除
    df["symbol"] = sym
    return df[["symbol", "date", "hhmm", "open", "high", "low", "close",
               "volume", "amount"]]


def main() -> int:
    import baostock as bs
    OUT.mkdir(parents=True, exist_ok=True)
    syms = pick_sample(pool_symbols(), N_SAMPLE)
    days = last_trading_days(N_DAYS)
    start, end = days[0], days[-1]
    print(f"sample={len(syms)} window={start}..{end} ({len(days)}d)")
    lg = bs.login()
    if lg.error_code != "0":
        print(f"login fail {lg.error_code} {lg.error_msg}")
        return 1
    stats = {}
    for i, s in enumerate(syms):
        fp = OUT / f"{s}.parquet"
        if fp.exists():
            print(f"[{i+1}/{len(syms)}] {s} exists, skip")
            continue
        t0 = time.time()
        try:
            df = fetch(bs, s, start, end)
            df.to_parquet(fp, index=False)
            stats[s] = {"rows": len(df), "days": int(df["date"].nunique()),
                        "seconds": round(time.time() - t0, 1)}
            print(f"[{i+1}/{len(syms)}] {s} rows={len(df)} "
                  f"days={df['date'].nunique()} ({stats[s]['seconds']}s)")
        except Exception as e:  # noqa: BLE001 —— 如实登记失败票
            stats[s] = {"error": str(e)[:150]}
            print(f"[{i+1}/{len(syms)}] {s} ERROR {e}")
    bs.logout()
    (OUT / "_fetch_meta.json").write_text(json.dumps(
        {"window": [start, end], "n_days": len(days), "sample": syms,
         "stats": stats}, ensure_ascii=False, indent=1))
    fails = [s for s, v in stats.items() if "error" in v]
    print(f"done: {len(stats)-len(fails)} ok, {len(fails)} fail -> {fails}")
    return 0 if not fails else 2


if __name__ == "__main__":
    sys.exit(main())
