#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T403 净值计算与时间序列存储 —— FR-ACC-3。

职责：
  1. 计算每日 NAV（复用 settle_day 结算逻辑，持仓市值 + 现金）
  2. 持久化到 Parquet 时间序列（`paper_trading/data/nav_series.parquet`）
  3. 支持增量追加（幂等写入，同日期覆盖）

红线：
  ① 净值计算逻辑**完全复用** `backtest/settle.py::settle_day`（SDD-1 同构）
  ② 持久化格式：Parquet（columns: date[date], nav[Decimal as string], cash, market_value）
  ③ 幂等性：同日期重跑 → 覆盖旧值（读已有 → 合并 → 按日期去重 keep='last' → 原子写）

"""
from __future__ import annotations

import shutil
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from typing import Sequence

import pandas as pd

__all__ = [
    "NAVRecord",
    "append_nav",
    "load_nav_series",
]

_ZERO = Decimal("0")


class NAVRecord:
    """单日净值快照（纯数据容器）。"""

    def __init__(
        self,
        date: _date,
        nav: Decimal,
        cash: Decimal,
        market_value: Decimal,
    ) -> None:
        self.date = date
        self.nav = nav
        self.cash = cash
        self.market_value = market_value

    def to_dict(self) -> dict:
        """转为 DataFrame 行字典。"""
        return {
            "date": self.date,
            "nav": str(self.nav),           # Decimal → str 存储（精确）
            "cash": str(self.cash),
            "market_value": str(self.market_value),
        }


def append_nav(
    nav_file: Path,
    records: Sequence[NAVRecord],
    *,
    idempotent: bool = True,
) -> None:
    """追加净值记录到 Parquet 时间序列（幂等写入）。

    Args:
        nav_file: 净值文件路径（如 `paper_trading/data/nav_series.parquet`）。
        records: 待追加的净值记录列表（可为单条或多条）。
        idempotent: 是否幂等（True = 同日期覆盖旧值，False = 直接追加可能重复）。

    算法（幂等模式）：
      1. 读已有文件（不存在 → 空 DataFrame）
      2. 合并新记录（concat）
      3. 按日期去重（keep='last'，新值覆盖旧值）
      4. 按日期排序
      5. 原子写（.tmp → os.replace）
    """
    nav_file.parent.mkdir(parents=True, exist_ok=True)

    # 1. 读已有数据
    if nav_file.exists():
        df_old = pd.read_parquet(nav_file, engine="pyarrow")
    else:
        df_old = pd.DataFrame(columns=["date", "nav", "cash", "market_value"])

    # 2. 新记录转 DataFrame
    rows = [rec.to_dict() for rec in records]
    df_new = pd.DataFrame(rows)

    # 3. 合并
    df_merged = pd.concat([df_old, df_new], ignore_index=True)

    # 4. 幂等去重（同日期保留最后一次写入）
    if idempotent and not df_merged.empty:
        df_merged["date"] = pd.to_datetime(df_merged["date"]).dt.date
        df_merged = df_merged.drop_duplicates(subset=["date"], keep="last")

    # 5. 按日期排序
    if not df_merged.empty:
        df_merged = df_merged.sort_values("date").reset_index(drop=True)

    # 6. 原子写（.tmp → os.replace）
    tmp_file = nav_file.with_suffix(".parquet.tmp")
    df_merged.to_parquet(tmp_file, engine="pyarrow", index=False)
    shutil.move(str(tmp_file), str(nav_file))


def load_nav_series(nav_file: Path) -> list[tuple[_date, Decimal]]:
    """读取净值时间序列（按日期升序）。

    Returns:
        [(date, nav), ...] 列表（空文件 → 空列表）。
    """
    if not nav_file.exists():
        return []

    df = pd.read_parquet(nav_file, engine="pyarrow", columns=["date", "nav"])
    if df.empty:
        return []

    df["date"] = pd.to_datetime(df["date"]).dt.date
    series = [
        (row["date"], Decimal(str(row["nav"])))
        for _, row in df.iterrows()
    ]
    return sorted(series, key=lambda x: x[0])
