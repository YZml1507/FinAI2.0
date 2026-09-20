#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""防御期资产（空仓现金替代）ETF 日线采集——Tushare 双代理并行。

用途：利用率归因实锤 ~45% 交易日近空仓、现金零收益 → 空仓期拟停靠
货基/短债/国债 ETF。本脚本拉取候选池 2015-2024 全量日线，落盘与
``data/etf_bars`` 同格式（RAW + factor 列，缺行保留不填充）。

数据源：datahubco 主 + promax 备（同一 Tushare 镜像，双端并行提速）。
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import urllib3

urllib3.disable_warnings()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data.collector import _atomic_write_parquet, _canonicalize, hash_file  # noqa
from scripts._secrets import require_env  # noqa: E402

ENDPOINTS = [
    ("datahubco", "http://datahubco.com/app-api/openapi/v1/tushare",
     "DATAHUBCO_API_KEY", False),
    ("promax", "https://pcd.mobcvb.cn/tushare/pro",
     "PROMAX_TUSHARE_KEY", True),
]

#: 防御期候选 ETF（symbol: ts_code）
UNIVERSE = {
    "sh.511880": "511880.SH",   # 银华日利（货基 ETF，净值累进）
    "sh.511990": "511990.SH",   # 华宝添益（货基 ETF）
    "sh.511360": "511360.SH",   # 海富通短融 ETF
    "sh.511260": "511260.SH",   # 十年国债 ETF
    "sh.511220": "511220.SH",   # 城投债 ETF
}
YEARS = list(range(2015, 2025))

_FIELDS = ["ts_code", "trade_date", "open", "high", "low", "close",
           "pre_close", "change", "pct_chg", "vol", "amount"]


def fetch(api: str, ep_idx: int, **params):
    name, url, key_env, noverify = ENDPOINTS[ep_idx % len(ENDPOINTS)]
    key = require_env(key_env)   # ⛔ 不再硬编码；惰性取秘密
    last = None
    for attempt in range(5):
        try:
            r = requests.get(f"{url}/{api}", params=params,
                             headers={"X-API-Key": key},
                             verify=not noverify, timeout=30)
            d = r.json()
            if d.get("code") == 0:
                items = d["data"].get("items") or []
                return pd.DataFrame(items, columns=d["data"].get("fields", _FIELDS))
            last = d.get("msg")
        except Exception as e:  # noqa
            last = str(e)
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{api} {params} via {name} failed: {last}")


def pull_one(sym: str, ts_code: str, year: int, ep_idx: int) -> tuple:
    df = fetch("fund_daily", ep_idx, ts_code=ts_code,
               start_date=f"{year}0101", end_date=f"{year}1231")
    if df.empty:
        return sym, year, None
    out = pd.DataFrame({
        "date": pd.to_datetime(df["trade_date"], format="%Y%m%d"),
        "open": df["open"].astype(float), "high": df["high"].astype(float),
        "low": df["low"].astype(float), "close": df["close"].astype(float),
        "volume": (df["vol"].astype(float) * 100).round(0).astype("int64"),
        "amount": df["amount"].astype(float) * 1000.0,
        "factor": float("nan"),          # 货基/债基几乎无分红拆分，因子留 NaN
        "code": sym, "source": "tushare-dual-proxy", "adjust_mode": "RAW",
    }).sort_values("date").reset_index(drop=True)
    return sym, year, out


def main() -> int:
    out_root = ROOT / "data" / "etf_bars"
    tasks = [(s, t, y, i) for i, (s, t) in enumerate(UNIVERSE.items())
             for y in YEARS]
    results: dict = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for fut in [ex.submit(pull_one, s, t, y, i + j)
                    for j, (s, t, y, i) in enumerate(tasks)]:
            sym, year, df = fut.result()
            results.setdefault(sym, {})[year] = df
            print(f"done {sym} {year} rows={0 if df is None else len(df)}",
                  flush=True)

    meta_rows = []
    for sym, ts_code in UNIVERSE.items():
        frames = {y: d for y, d in results.get(sym, {}).items()
                  if d is not None and not d.empty}
        if not frames:
            meta_rows.append({"symbol": sym, "ts_code": ts_code,
                              "error": "no data"})
            continue
        all_df = _canonicalize(pd.concat(frames.values(), ignore_index=True))
        dates = pd.to_datetime(all_df["date"])
        sha = {}
        for year, yr in all_df.groupby(dates.dt.year):
            p = out_root / sym / f"{year}.parquet"
            p.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_parquet(_canonicalize(yr), p)
            sha[str(year)] = hash_file(p)
        meta_rows.append({
            "symbol": sym, "ts_code": ts_code, "rows": len(all_df),
            "start_date": dates.min().strftime("%Y-%m-%d"),
            "end_date": dates.max().strftime("%Y-%m-%d"),
            "sha256": sha})
        print(f"[{sym}] 落盘 {len(all_df)} 行", flush=True)

    meta = {"task": "防御期资产 ETF 日线（空仓现金替代候选池）",
            "collection_date": datetime.now().isoformat(),
            "start": "20150101", "end": "20241231",
            "source": "tushare:datahubco+promax dual-proxy fund_daily",
            "volume_unit": "股（源:手 ×100）", "amount_unit": "元（源:千元 ×1000）",
            "adjust_mode": "RAW（货基/债基因子缺失留 NaN）",
            "symbols": meta_rows}
    mp = out_root / "meta_defensive.json"
    mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    ok = sum(1 for r in meta_rows if "error" not in r)
    print(f"✅ {ok}/{len(UNIVERSE)} 落盘 → {mp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
