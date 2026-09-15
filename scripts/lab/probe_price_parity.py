#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""口径交叉验证：本地权威红利池 vs 东财 vs 腾讯，抽样三只票的不同交易日，
验证不同数据源的 RAW 不复权收盘价是否一致。仅打印，不落盘。
"""
from __future__ import annotations

import pandas as pd

LOCAL = "/home/ubuntu/FinAI2.0/data/dividend_stocks"
SAMPLES = [("sh.600000", "2020-06-15"), ("sh.600000", "2023-03-10"),
           ("sz.000001", "2021-09-01")]


def local_close(symbol: str, day: str) -> float | None:
    year = day[:4]
    df = pd.read_parquet(LOCAL + "/" + symbol + "/" + year + ".parquet")
    row = df[df["date"] == day]
    return float(row["close"].iloc[0]) if len(row) else None


def em_close(symbol: str, day: str) -> float | None:
    import akshare as ak
    code = symbol.split(".")[1]
    df = ak.stock_zh_a_hist(symbol=code, period="daily",
                            start_date=day.replace("-", ""),
                            end_date=day.replace("-", ""), adjust="")
    if df.empty:
        return None
    return float(df["收盘"].iloc[0])


def tx_close(symbol: str, day: str) -> float | None:
    import requests
    plain = symbol.replace(".", "")
    r = requests.get("https://ifzq.gtimg.cn/appstock/app/fqkline/get",
                     params={"param": plain + ",day," + day + "," + day + ",1,qfq"},
                     timeout=10)
    dd = r.json().get("data", {}).get(plain, {})
    k = dd.get("qfqday") or dd.get("day") or []
    return float(k[0][2]) if k else None


for sym, day in SAMPLES:
    try:
        lc = local_close(sym, day)
    except Exception as e:
        lc = "ERR:" + str(e)[:60]
    try:
        ec = em_close(sym, day)
    except Exception as e:
        ec = "ERR:" + str(e)[:60]
    try:
        tc = tx_close(sym, day)
    except Exception as e:
        tc = "ERR:" + str(e)[:60]
    print(sym, day, "local=", lc, " em=", ec, " tx=", tc)
