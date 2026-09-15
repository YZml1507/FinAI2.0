#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""口径交叉验证 v2：本地红利池 vs 腾讯 vs 东财 RAW 收盘价。
仅打印，不落盘。修正点：date 列统一 astype(str) 再比较。
"""
from __future__ import annotations

import time

import pandas as pd

LOCAL = "/home/ubuntu/FinAI2.0/data/dividend_stocks"
SAMPLES = [("sh.600000", "2020-06-15"), ("sh.600000", "2023-03-10"),
           ("sh.600016", "2021-09-01")]


def local_close(symbol: str, day: str):
    df = pd.read_parquet(LOCAL + "/" + symbol + "/" + day[:4] + ".parquet")
    print("   [local] date dtype=%s sample=%r" % (df["date"].dtype, df["date"].iloc[0]))
    row = df[df["date"].astype(str) == day]
    return float(row["close"].iloc[0]) if len(row) else None


def tx_close(symbol: str, day: str):
    import requests
    plain = symbol.replace(".", "")
    r = requests.get("https://ifzq.gtimg.cn/appstock/app/fqkline/get",
                     params={"param": plain + ",day," + day + "," + day + ",1,"},
                     timeout=10)
    dd = r.json().get("data", {}).get(plain, {})
    k = dd.get("day") or dd.get("qfqday") or []
    return (float(k[0][2]), list(dd.keys())) if k else (None, list(dd.keys()))


def em_close(symbol: str, day: str):
    import akshare as ak
    code = symbol.split(".")[1]
    df = ak.stock_zh_a_hist(symbol=code, period="daily",
                            start_date=day.replace("-", ""),
                            end_date=day.replace("-", ""), adjust="")
    return float(df["收盘"].iloc[0]) if not df.empty else None


for sym, day in SAMPLES:
    try:
        lc = local_close(sym, day)
    except Exception as e:
        lc = "ERR:" + str(e)[:80]
    try:
        tc, tkeys = tx_close(sym, day)
    except Exception as e:
        tc, tkeys = "ERR:" + str(e)[:80], []
    time.sleep(1.0)
    try:
        ec = em_close(sym, day)
    except Exception as e:
        ec = "ERR:" + str(e)[:80]
    time.sleep(1.0)
    print("%s %s  local=%s  tx(raw)=%s  em=%s  tx_keys=%s" % (sym, day, lc, tc, ec, tkeys))
