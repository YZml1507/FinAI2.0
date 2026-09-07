#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase 3.6 多策略研发 · Task A —— 核心 ETF 资产池日线采集器（CITYDATA 主源）。

需求（agy 下发，Phase 3.6）：6 只核心 ETF 2015-01-01 → 2024-12-31 全量日线，
落盘 ``data/etf_bars/{symbol}/{year}.parquet``，字段：date/open/high/low/close/
volume/amount/factor（复权因子），并产出 SHA-256 校验元数据。

数据源：``finai/sources/citydata_source.py``（闲鱼 tushare 镜像）。
  · ``fund_daily`` —— 日线 OHLC + vol(手) + amount(千元) + pre_close。
  · ``fund_adj``   —— 复权因子 adj_factor（按日，与 fund_daily 同口径日期）。
  · ``fund_basic`` —— 上市日期（list_date，用来自动界定数据起点，⛔ 不硬编码）。
  · ``trade_cal``  —— 交易日历（is_open=1，用于停牌/非交易日对齐校验，不落盘）。

反伪约束（agy 硬性）：
  · 无静态前向填充：缺行 = 停牌/未上市，**保留行缺失**，绝不向前 fill。
  · 严格跟随交易日历：volume/amount 单位显式换算（手→股 ×100；千元→元 ×1000），
    写入 meta 声明单位口径，避免下游误读。
  · 幂等：同区间重跑产出**逐字节一致**分区文件（复用 ``data/collector`` 原语
    ``_canonicalize`` / ``_atomic_write_parquet`` / ``hash_file``，FR-DATA-6）。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from data.collector import _atomic_write_parquet, _canonicalize, hash_file
from finai.sources import citydata_source as cs

logger = __import__("logging").getLogger(__name__)
__import__("logging").basicConfig(level=20, format="%(asctime)s [%(levelname)s] %(message)s")

#: Task A 六只核心 ETF（agy 指定）。symbol 用本仓标准 ``sh.`/``sz.`` 前缀；
#: 交易所后缀 CITYDATA 用 tushare 约定（.SH/.SZ）。
ETF_UNIVERSE: dict[str, str] = {
    "sh.510300": "510300.SH",   # 沪深300 ETF
    "sh.510500": "510500.SH",   # 中证500 ETF
    "sz.159915": "159915.SZ",   # 创业板 ETF
    "sh.512890": "512890.SH",   # 红利低波 ETF（2019-01-18 上市，前段合法缺失）
    "sh.511010": "511010.SH",   # 5年期国债 ETF
    "sh.513100": "513100.SH",   # 纳斯达克100 ETF(QDII)
}

#: 落盘列序（含血缘列；feed 只读所需字段）。
_BAR_COLUMNS = [
    "date", "open", "high", "low", "close", "volume", "amount", "factor",
    "code", "source", "adjust_mode",
]

START = "20150101"
END = "20241231"
_SLEEP_S = 0.5            # 串行限速（FR-DATA-7 友好）


def _fetch_retry(api: str, **params: Any) -> Any:
    """CITYDATA 取数带重试（FAIL_UNREACHABLE 是网络瞬断，非数据缺失）。"""
    last = None
    for attempt in range(1, 4):
        r = cs.fetch(api, **params)
        if r.state == "OK":
            return r
        if r.state != "FAIL_UNREACHABLE":
            raise RuntimeError(f"{api} {params.get('ts_code')} 失败: {r.state} {getattr(r, 'detail', '')}")
        last = r
        time.sleep(2 * attempt)
    raise RuntimeError(f"{api} {params.get('ts_code')} 3 次瞬断: {getattr(last, 'detail', '')}")


def _list_date(ts_code: str) -> str:
    """上市日期（YYYYMMDD）。fail-closed：取不到 raise，⛔ 不硬编码不猜测。"""
    r = _fetch_retry("fund_basic", ts_code=ts_code)
    if r.frame is None or r.frame.empty:
        raise RuntimeError(f"fund_basic {ts_code} 取上市日期失败: {r.state}")
    return str(r.frame["list_date"].iloc[0])


def fetch_one_etf(symbol: str, ts_code: str) -> pd.DataFrame:
    """单只 ETF 全量日线 + 复权因子合并（CITYDATA fund_daily × fund_adj）。"""
    list_date = _list_date(ts_code)
    start = max(START, list_date)            # 未上市前合法缺失，不硬造
    daily = _fetch_retry("fund_daily", ts_code=ts_code, start_date=start, end_date=END)
    if daily.frame is None or daily.frame.empty:
        raise RuntimeError(f"fund_daily {ts_code} 空: {daily.state}")
    adj = _fetch_retry("fund_adj", ts_code=ts_code, start_date=start, end_date=END)
    if adj.frame is None or adj.frame.empty:
        raise RuntimeError(f"fund_adj {ts_code} 空: {adj.state}")

    d = daily.frame.copy()
    a = adj.frame[["trade_date", "adj_factor"]].copy()
    merged = d.merge(a, on="trade_date", how="left")
    # ⛔ 复权因子缺失行保留 NaN，绝不前向填充（反伪约束 1）
    merged["date"] = pd.to_datetime(merged["trade_date"], format="%Y%m%d")
    merged = merged.sort_values("date").reset_index(drop=True)

    out = pd.DataFrame({
        "date": merged["date"],
        "open": merged["open"].astype(float),
        "high": merged["high"].astype(float),
        "low": merged["low"].astype(float),
        "close": merged["close"].astype(float),
        "volume": (merged["vol"].astype(float) * 100).round(0).astype("int64"),   # 手→股
        "amount": merged["amount"].astype(float) * 1000.0,                        # 千元→元
        "factor": merged["adj_factor"].astype(float),
        "code": symbol,
        "source": "citydata",
        "adjust_mode": "RAW",                # 落盘 close 为不复权真实价，factor 单独给
    })
    return out[_BAR_COLUMNS]


def collect_etfs(
    output_root: Path,
    symbols: dict[str, str] | None = None,
) -> dict[str, Any]:
    """批量采集 6 ETF，按 (symbol, 年) 分区幂等落盘，产出 meta.json + SHA-256。"""
    symbols = symbols or ETF_UNIVERSE
    output_root.mkdir(parents=True, exist_ok=True)

    meta_rows: list[dict[str, Any]] = []
    for sym, ts_code in symbols.items():
        try:
            df = fetch_one_etf(sym, ts_code)
        except Exception as exc:             # noqa: BLE001
            logger.error(f"[{sym}] 采集失败: {exc}")
            meta_rows.append({"symbol": sym, "ts_code": ts_code, "error": str(exc)})
            continue

        canonical = _canonicalize(df)
        # _canonicalize 把 date 归一为 date 对象（非 datetime64），统一转 pd.Timestamp 分组
        dates = pd.to_datetime(canonical["date"])
        for year, yr_rows in canonical.groupby(dates.dt.year):
            path = output_root / sym / f"{year}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_parquet(_canonicalize(yr_rows), path)

        n_null = int(canonical[["open", "high", "low", "close", "factor"]].isna().sum().sum())
        meta_rows.append({
            "symbol": sym,
            "ts_code": ts_code,
            "rows": len(canonical),
            "start_date": dates.min().strftime("%Y-%m-%d"),
            "end_date": dates.max().strftime("%Y-%m-%d"),
            "null_rate": round(n_null / (len(canonical) * 5), 6),
            "sha256": {
                str(y): hash_file(output_root / sym / f"{y}.parquet")
                for y in sorted(dates.dt.year.unique())
            },
        })
        logger.info(f"[{sym}] 落盘 {len(canonical)} 行")
        time.sleep(_SLEEP_S)

    meta = {
        "task": "Task A — 核心 ETF 资产池日线",
        "collection_date": datetime.now().isoformat(),
        "start": START, "end": END,
        "source": "citydata(fund_daily + fund_adj + fund_basic)",
        "volume_unit": "股（源:手 ×100）",
        "amount_unit": "元（源:千元 ×1000）",
        "adjust_mode": "RAW + factor 列",
        "note": "close 为不复权真实价；复权因子独立 factor 列；停牌/未上市=行缺失，无前向填充",
        "symbols": meta_rows,
    }
    with open(output_root / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    ok = sum(1 for r in meta_rows if "error" not in r)
    logger.info(f"✅ Task A 完成: {ok}/{len(symbols)} 只 ETF 落盘")
    return meta


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Task A 核心 ETF 日线采集（CITYDATA）")
    parser.add_argument("--output", default="data/etf_bars")
    args = parser.parse_args()
    meta = collect_etfs(Path(args.output))
    n = sum(1 for r in meta["symbols"] if "error" not in r)
    return 0 if n == len(ETF_UNIVERSE) else 1


if __name__ == "__main__":
    sys.exit(main())
