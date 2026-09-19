#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""510880 红利 ETF 日线采集（sina 通道 —— citydata 代理失效时的本仓替代源）。

用途：e15-etfattack 指数 placebo——同择时下 attack 档持 510880 替代选股栈。
citydata 代理 407 失效（2026-09-19）；baostock 不覆盖 ETF；
akshare `fund_etf_hist_sina`（新浪）实测连通（2007 起全量日线）。

落盘（与 collect_etf_data.py 同 schema，供 run_dividend_backtest 注入消费）：
  * ``data/etf_bars/sh.510880/{year}.parquet`` —— date/open/high/low/close/
    volume/amount/factor/code/source/adjust_mode（RAW 真实价；volume 股、
    amount 元；factor 由分红事件合成——除息日 f_t = f_{t-1}×pc/(pc−div)）
  * ``data/etf_bars/sh.510880/exdiv.parquet`` —— {date, factor=1.0,
    cash_dividend} 除权事件（分红来源：akshare ``fund_etf_dividend_sina``
    累计分红序列差分；日期=除息日口径）
  * meta 合并进 ``data/etf_bars/meta.json`` 的 symbols 列表（保留既有条目）。

纪律：幂等原子写（_canonicalize/_atomic_write_parquet/hash_file）；无行前向
填充；缺数据 raise 不猜值。

用法：.venv/bin/python scripts/collect_etf_510880_sina.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from data.collector import _atomic_write_parquet, _canonicalize, hash_file  # noqa: E402

SYM = "sh.510880"
SINA_CODE = "sh510880"
# 起点须早于回测 data_start(2014-06-01)+warmup(210 交易日)，保证攻击档首日即有 bar
START, END = "2012-01-01", "2024-12-31"
OUT = _root / "data" / "etf_bars"

_BAR_COLUMNS = [
    "date", "open", "high", "low", "close", "volume", "amount", "factor",
    "code", "source", "adjust_mode",
]


def fetch_bars() -> pd.DataFrame:
    """新浪日线 → etf_bars schema（含 factor 合成列）。"""
    import akshare as ak
    df = ak.fund_etf_hist_sina(symbol=SINA_CODE)
    if df is None or df.empty:
        raise RuntimeError(f"fund_etf_hist_sina({SINA_CODE}) 返回空")
    df = df.rename(columns={"prevclose": "preclose_sina"})
    df["date"] = pd.to_datetime(df["date"])
    df = df[(df["date"] >= START) & (df["date"] <= END)].reset_index(drop=True)
    if df.empty:
        raise RuntimeError(f"{SYM} {START}~{END} 窗口无行")
    return df[["date", "open", "high", "low", "close", "volume", "amount"]]


def fetch_dividends() -> pd.DataFrame:
    """累计分红序列差分 → 单次分红事件 {date, factor=1.0, cash_dividend}。"""
    import akshare as ak
    df = ak.fund_etf_dividend_sina(symbol=SINA_CODE)
    if df is None or df.empty:
        raise RuntimeError(f"fund_etf_dividend_sina({SINA_CODE}) 返回空")
    df = df.rename(columns={"日期": "date", "累计分红": "cum"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["cash_dividend"] = df["cum"].diff()
    df = df.dropna(subset=["cash_dividend"])
    df = df[(df["date"] >= START) & (df["date"] <= END)]
    df["factor"] = 1.0
    return df[["date", "factor", "cash_dividend"]].reset_index(drop=True)


def main() -> int:
    bars = fetch_bars()
    divs = fetch_dividends()

    # factor 合成：除息日 f_t = f_{t-1} × prev_close/(prev_close − div)
    # （累计复权因子口径，与 collect_etf_data 的 fund_adj 列同语义）
    bars["factor"] = 1.0
    div_map = {r["date"]: float(r["cash_dividend"]) for r in divs.to_dict("records")}
    prev_close = bars["close"].astype(float).shift(1)
    for i in range(1, len(bars)):
        d = bars.at[i, "date"]
        f_prev = float(bars.at[i - 1, "factor"])
        if d in div_map:
            pc = float(prev_close[i])
            div = div_map[d]
            if pc - div <= 0:
                raise RuntimeError(f"{SYM} {d.date()} 分红 {div} ≥ 前收 {pc}，数据可疑")
            bars.at[i, "factor"] = f_prev * pc / (pc - div)
        else:
            bars.at[i, "factor"] = f_prev

    bars["code"] = SYM
    bars["source"] = "sina"
    bars["adjust_mode"] = "RAW"
    out = bars[_BAR_COLUMNS].copy()
    canonical = _canonicalize(out)

    sym_dir = OUT / SYM
    written = {}
    dates = pd.to_datetime(canonical["date"])
    for year, yr_rows in canonical.groupby(dates.dt.year):
        path = sym_dir / f"{year}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_parquet(_canonicalize(yr_rows), path)
        written[str(year)] = hash_file(path)

    if not divs.empty:
        _atomic_write_parquet(_canonicalize(divs), sym_dir / "exdiv.parquet")

    # meta 合并（保留既有 ETF 条目）
    meta_path = OUT / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    syms = [s for s in meta.get("symbols", []) if s.get("symbol") != SYM]
    syms.append({
        "symbol": SYM, "ts_code": "510880.SH",
        "rows": int(len(canonical)),
        "start_date": dates.min().strftime("%Y-%m-%d"),
        "end_date": dates.max().strftime("%Y-%m-%d"),
        "null_rate": 0.0,
        "sha256": written,
        "source": "sina(fund_etf_hist_sina + fund_etf_dividend_sina)",
        "note": "citydata 代理失效替代源；factor 由分红事件合成；exdiv.parquet 随区落盘",
    })
    meta["symbols"] = syms
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"[{SYM}] 落盘 {len(canonical)} 行、除权事件 {len(divs)} 次 "
          f"（{canonical['date'].min()}~{canonical['date'].max()}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
