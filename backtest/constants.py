#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T201 §2 枚举常量 —— 回测/模拟盘/实盘四环境同构共用（SDD-1）。

全部枚举继承 ``(str, Enum)``：

  · 值即字符串 → JSON / Parquet / 流水落盘无需额外编解码；
  · ``OrderSide.BUY == "BUY"`` 成立 → 跨环境（回测 ↔ 券商回包）比对不用手工映射；
  · ``compute_tx_hash`` 的 canonical 表示直接取 ``.value``，稳定可复现。

⛔ 红线：枚举成员名 / 值以 ``backtest/T201_design.md`` §2 为唯一契约，
不得增删改名（下游 Journal 的 ``tx_hash`` 依赖枚举值字面量，改名 = 历史流水失配）。
"""
from __future__ import annotations

from enum import Enum

__all__ = [
    "OrderSide",
    "OrderType",
    "OrderStatus",
    "FeeItem",
]


class OrderSide(str, Enum):
    """买卖方向。v1 long-only（SDD-7）：没有开空/平空，只有 BUY / SELL。"""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """委托类型。v1 只支持市价 / 限价两种（T201 §12 非目标：无冰山/条件单）。"""

    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    """七态订单状态（SDD-2 / T201 §4）。

    ``PENDING_SUBMIT → SUBMITTED → (PARTIALLY_FILLED)* → FILLED / CANCELLED
    / EXPIRED / REJECTED``

    ⛔ 恰好 7 态，不多不少：合法迁移表（``order_fsm.LEGAL_TRANSITIONS``）与本枚举
    一一对应，新增状态必须同步改迁移表与 T201 契约。
    """

    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class FeeItem(str, Enum):
    """费用科目（A 股交易成本拆项，FR-BT-5）。

    ``Trade.fees`` 必须至少包含 ``COMMISSION`` / ``STAMP_TAX`` / ``TRANSFER_FEE``
    / ``HANDLING_FEE``（T201 §3）；``MANAGEMENT_FEE`` / ``SLIPPAGE`` 为可选拆项。
    """

    COMMISSION = "COMMISSION"          # 佣金
    STAMP_TAX = "STAMP_TAX"            # 印花税（卖出单边 0.05%）
    TRANSFER_FEE = "TRANSFER_FEE"      # 过户费
    HANDLING_FEE = "HANDLING_FEE"      # 经手费 / 证管费等规费
    MANAGEMENT_FEE = "MANAGEMENT_FEE"  # 管理费（模拟组合层面摊销）
    SLIPPAGE = "SLIPPAGE"              # 滑点（建模为成本项，便于归因）
