#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase 3.6 多策略研发 · Task C —— 宏观无风险基准（10 年期国债收益率）。

需求（agy 下发，Phase 3.6）：2015~2024 每日中国 10 年期国债收益率，落盘
``data/macro/treasury_yield_10y.parquet``，字段：date / yield_10y。

数据源：``akshare bond_zh_us_rate(start_date='20150101')``（东财中国国债收益率，
列 ``中国国债收益率10年``，实测覆盖 2015-01-02 → 2026-09-07，单位 %）。

反伪约束（agy 硬性）：
  · **无静态前向填充**：非交易日/缺失日保留 NaN，绝不 fill。
  · 严格交易日历对齐：本帧只提供**自然日**收益序列（含周末/节假日 NaN），
    下游按 CITYDATA ``trade_cal`` 或行情日期做左对齐，不在本层硬造交易日。
  · 幂等：同区间重跑逐字节一致。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from data.collector import _atomic_write_parquet, hash_file

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

START = "2015-01-01"
END = "2024-12-31"


def fetch_treasury_yield() -> pd.DataFrame:
    """10 年期国债收益率日频帧（date / yield_10y，NaN=非交易日/缺失）。"""
    import akshare as ak  # noqa: PLC0415

    raw = ak.bond_zh_us_rate(start_date="20150101")
    df = raw[["日期", "中国国债收益率10年"]].rename(
        columns={"日期": "date", "中国国债收益率10年": "yield_10y"})
    df["date"] = pd.to_datetime(df["date"])
    df["yield_10y"] = pd.to_numeric(df["yield_10y"], errors="coerce")
    df = df[(df["date"] >= START) & (df["date"] <= END)].reset_index(drop=True)
    df = df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    return df


def collect_treasury(output_root: Path) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    df = fetch_treasury_yield()
    path = output_root / "treasury_yield_10y.parquet"
    _atomic_write_parquet(df, path)

    n = len(df)
    n_null = int(df["yield_10y"].isna().sum())
    meta = {
        "task": "Task C — 宏观无风险基准（10 年期国债收益率）",
        "collection_date": datetime.now().isoformat(),
        "start": START, "end": END,
        "source": "akshare(bond_zh_us_rate) 东财中国国债收益率",
        "unit": "百分比（%）",
        "rows": n,
        "null_count": n_null,
        "null_rate": round(n_null / n, 6) if n else None,
        "date_min": df["date"].min().strftime("%Y-%m-%d"),
        "date_max": df["date"].max().strftime("%Y-%m-%d"),
        "sha256": hash_file(path),
        "note": "自然日序列，非交易日/缺失=NaN，不填充；下游按 trade_cal 对齐",
    }
    with open(output_root / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    logger.info(f"✅ Task C 完成: {n} 行，null {n_null}")
    return meta


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Task C 10 年期国债收益率采集")
    parser.add_argument("--output", default="data/macro")
    args = parser.parse_args()
    collect_treasury(Path(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
