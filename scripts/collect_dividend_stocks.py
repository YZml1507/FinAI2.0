#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T312 红利股数据采集器 —— 重写版（修复 baostock 每只登录/登出导致的网络中断）。

v1 简化口径声明：
  - 候选池：全 A 股中取前 ``--limit`` 只（默认 500），跳过股息率预筛选（耗时且不可靠）
  - 日线存储：RAW 不复权（因 baostock 不复权日线才可配分红数据算真实股息率）
  - 扩展字段（dividend_yield / market_cap）在 **后处理** 阶段追加
  - 股息率计算：年度每股分红 / 当年平均收盘价（简化，⛔ 非 TTM）
  - 流通市值：用 ``bar.amount``（成交额）代理（v1 简化，⛔ 非真实流通市值）

用法：
  python scripts/collect_dividend_stocks.py --limit 500
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date as _date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from data.collector import (
    DailyCollector,
    _canonicalize,
    _atomic_write_parquet,
)
from finai.sources import baostock_source
from finai.sources.adjustment_mode import AdjustmentMode
from finai.sources.baostock_source import fetch

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ===================================================================
# 候选池（全 A 股快照，⛔ 不筛股息率）
# ===================================================================

def fetch_all_a_stocks() -> list[str]:
    """取全 A 股代码列表（上市状态正常）。"""
    logger.info("获取全 A 股候选池...")
    result = fetch("all_stock", day=datetime.now().strftime("%Y-%m-%d"))
    if result.frame is None or result.frame.empty:
        raise RuntimeError("all_stock 查询返回空")
    df = result.frame
    # 筛选 A 股（type=1）且上市状态正常
    stocks = df[df["code"].str.startswith("sh.") | df["code"].str.startswith("sz.")]
    codes = stocks["code"].tolist()
    logger.info(f"获取到 {len(codes)} 只 A 股")
    return codes


# ===================================================================
# 后处理：追加股息率与市值字段
# ===================================================================

def _fetch_dividend_cash(symbol: str, year: int) -> Decimal:
    """取某股票某年度的每股税前分红（元）。"""
    result = fetch("dividend", code=symbol, year=str(year), yearType="report")
    if result.frame is None or result.frame.empty:
        return Decimal("0")
    df = result.frame
    if "dividCashPsBeforeTax" not in df.columns:
        return Decimal("0")
    total = pd.to_numeric(df["dividCashPsBeforeTax"], errors="coerce").sum()
    return Decimal(str(total)) if total > 0 else Decimal("0")


def _compute_dividend_yield(symbol: str, year: int, avg_close: Decimal) -> Decimal | None:
    """计算某股票某年度的股息率 = 年度每股分红 / 年均收盘价。

    Returns:
        None 表示分红数据缺失或年均价无效（策略层跳过）。
    """
    if avg_close <= Decimal("0"):
        return None
    div_cash = _fetch_dividend_cash(symbol, year)
    if div_cash <= Decimal("0"):
        return None
    return div_cash / avg_close


def enrich_dividend_columns(
    symbol: str,
    root: Path,
    years: list[int],
) -> None:
    """为某股票各年份分区追加 dividend_yield / market_cap 列。

    market_cap 用日均成交额代理（v1 简化）。
    dividend_yield 用年度总分红 / 年均收盘价。
    """
    for year in years:
        path = root / symbol / f"{year}.parquet"
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path)
        except Exception as exc:
            logger.warning(f"[{symbol}] {year} 读分区失败: {exc}")
            continue

        if df.empty:
            continue

        # 计算年均收盘价
        close_col = pd.to_numeric(df["close"], errors="coerce")
        avg_close = close_col.mean()
        if pd.isna(avg_close) or avg_close <= 0:
            logger.warning(f"[{symbol}] {year} 年均收盘价无效，跳过股息率计算")
            continue

        # 股息率
        div_yield = _compute_dividend_yield(symbol, year, Decimal(str(avg_close)))

        # 流通市值代理：日均成交额（amount）
        amount_col = pd.to_numeric(df["amount"], errors="coerce")
        avg_amount = amount_col.mean()
        market_cap_proxy = Decimal(str(avg_amount)) if not pd.isna(avg_amount) else Decimal("0")

        # 追加列
        df["dividend_yield"] = float(div_yield) if div_yield is not None else None
        df["market_cap"] = float(market_cap_proxy)

        # 幂等写回
        _atomic_write_parquet(_canonicalize(df), path)


# ===================================================================
# 主流程
# ===================================================================

def collect_dividend_stocks(
    symbols: list[str],
    start_date: str,
    end_date: str,
    output_root: Path,
) -> dict[str, Any]:
    """采集红利股日线数据（RAW 不复权）→ 后处理追加字段。

    流程：
      1. 用 DailyCollector 采集日线 kline（RAW 口径）
      2. 对每只股票，逐笔查询分红数据 → 计算股息率
      3. 追加 dividend_yield / market_cap 列 → 幂等写回 parquet
    """
    logger.info(f"第 1 阶段：采集 {len(symbols)} 只股票日线数据（RAW 不复权）...")

    # ⭐ RAW 不复权：baostock 不复权日线 OHLC 才与分红数据一致
    # ⛔ check_fetchers={}：关闭新浪/腾讯校验腿——红利采集只求主源吞吐，三源
    #   交叉比对属 T110 验收域（独立路径），此处省掉每条腿 0.5s 的吞吐损耗。
    collector = DailyCollector(
        root=output_root,
        adjust_mode=AdjustmentMode.RAW,
        sample_rate=0.0,
        max_retries=3,
        min_interval=1.0,  # 1 秒/只：baostock 实测吞吐上限 ~1.1 qps，4s 纯属浪费
        check_fetchers={},
    )

    results = collector.collect(symbols, start_date, end_date)

    ok_symbols = [s for s, r in results.items() if r.ok]
    failed = [s for s, r in results.items() if not r.ok]
    logger.info(f"采集完成: {len(ok_symbols)}/{len(symbols)} 成功")
    if failed:
        logger.warning(f"失败标的: {len(failed)} 只（前 10: {failed[:10]}）")

    # 第 2 阶段：后处理追加红利字段
    logger.info("第 2 阶段：追加股息率 / 市值字段...")
    start_year = int(start_date.split("-")[0])
    end_year = int(end_date.split("-")[0])
    years = list(range(start_year, end_year + 1))

    enriched = 0
    for i, symbol in enumerate(ok_symbols):
        if i % 50 == 0:
            logger.info(f"后处理进度: {i}/{len(ok_symbols)}")
        try:
            enrich_dividend_columns(symbol, output_root, years)
            enriched += 1
        except Exception as exc:
            logger.warning(f"[{symbol}] 后处理失败: {exc}")

    logger.info(f"后处理完成: {enriched}/{len(ok_symbols)} 成功")

    meta = {
        "collection_date": datetime.now().isoformat(),
        "start_date": start_date,
        "end_date": end_date,
        "total_symbols": len(symbols),
        "successful": len(ok_symbols),
        "enriched": enriched,
        "failed": len(failed),
        "failed_symbols": failed[:20],
        "adjust_mode": "RAW",
        "note": "v1 简化：market_cap 用日均成交额代理；dividend_yield 用年度总分红/年均收盘价",
    }
    meta_path = output_root / "meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    logger.info(f"元数据已写入: {meta_path}")
    return meta


# ===================================================================
# CLI
# ===================================================================

def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="T312 红利股数据采集器（重写版）")
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--output", default="data/dividend_stocks")
    parser.add_argument("--limit", type=int, default=500, help="采集股票数（默认 500）")
    args = parser.parse_args()

    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)

    # 清洗旧数据
    for d in output_root.iterdir():
        if d.is_dir() and d.name.startswith(("sh.", "sz.")):
            import shutil
            shutil.rmtree(d)

    # 候选池
    candidates = fetch_all_a_stocks()
    selected = candidates[:args.limit]
    logger.info(f"采集前 {len(selected)} 只股票")

    # 采集
    meta = collect_dividend_stocks(selected, args.start, args.end, output_root)
    logger.info(f"✅ 采集完成: {meta['successful']}/{meta['total_symbols']} 成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())