#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T201 §3 数据契约 —— Bar / Order / Trade / Position / PortfolioView。

四环境同构（SDD-1）的**唯一**数据形状定义：回测、模拟盘、实盘、实验共用这一份
dataclass，环境差异全部收敛到 Broker 实现里，⛔ 不许各环境自造 Bar/Trade。

红线落点：

  ① **所有金额用 ``Decimal``**（⛔ 禁 float）：A 股价格 2 位小数、金额 4 位，
     float 累加会漂移，净值/费用/成本必须精确可复算。⛔ 不要写
     ``Decimal(0.1)``（二进制噪声），一律 ``Decimal("0.1")``。
  ② **不可变的用 ``frozen=True``**：``Bar``（行情事实）/ ``Trade``（成交事实）
     一经产生不可改 —— 它们是 append-only 流水的输入，可变即幂等失效。
     ``Order`` / ``Position`` / ``PortfolioView`` 是**推导态**，可变。
  ③ **字段顺序照契约逐字保留**。契约里 ``Order.created_date`` /
     ``PortfolioView.date`` 排在有默认值的字段之后 —— Python dataclass 不允许
     "无默认值字段跟在有默认值字段之后"，故这两个字段声明为
     ``field(kw_only=True)``：**仍是必填**（无默认值），只是必须用关键字传入。
     这样字段名与顺序都不改，语义也不放松。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from decimal import Decimal

from backtest.constants import FeeItem, OrderSide, OrderStatus, OrderType

__all__ = [
    "Bar",
    "Order",
    "Trade",
    "Position",
    "PortfolioView",
]

_ZERO = Decimal("0")


@dataclass(frozen=True)
class Bar:
    """一根日线（行情事实，不可变）。

    ``open/high/low/close/preclose`` 均为 ``adjust_mode`` 声明口径下的价格（R4：
    复权口径必须随数据同行，⛔ 不许靠调用方记忆）。
    ``limit_up`` / ``limit_down`` / ``exdiv`` **不是 parquet 原生列**，由 feed 读入
    后调 ``data.cleaner.mark_limit_flags`` + ``combine_exdiv_flag`` 注入。
    """

    date: _date
    symbol: str                       # baostock 风格代码，如 "sh.600000"
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    preclose: Decimal
    volume: Decimal                   # 成交量（股）
    amount: Decimal                   # 成交额（元）
    # —— 清洗派生列（feed 注入，非 parquet 原生） ——
    limit_up: bool = False
    limit_down: bool = False
    exdiv: bool = False
    # —— 元数据 ——
    is_st: bool = False
    adjust_mode: str = "hfq"


@dataclass
class Order:
    """一笔委托（可变推导态；幂等键 ``client_order_id``）。

    状态迁移**只允许**经 ``backtest.order_fsm.OrderStateMachine.transition``，
    ⛔ 不要直接赋值 ``order.status``（绕过迁移表 = 七态契约失守）。
    """

    client_order_id: str              # 幂等键（同 id 重复提交视为同一笔）
    symbol: str
    side: OrderSide
    order_type: OrderType
    volume: int                       # 正整数；买入须 100 的倍数
    price: Decimal | None             # MARKET 单为 None
    status: OrderStatus = OrderStatus.PENDING_SUBMIT
    created_date: _date = field(kw_only=True)   # 必填（见模块 docstring ③）
    filled_volume: int = 0
    avg_fill_price: Decimal = _ZERO
    fills: list["Trade"] = field(default_factory=list)
    reject_reason: str = ""           # 仅 REJECTED 时填充


@dataclass(frozen=True)
class Trade:
    """一笔成交（成交事实，不可变；幂等键 ``trade_id``）。

    ``fees`` 键必须覆盖 ``COMMISSION`` / ``STAMP_TAX`` / ``TRANSFER_FEE``
    / ``HANDLING_FEE``（T201 §3）；``sellable_date`` 由 settle 按 T+1 写入
    （买入成交的下一个**交易日**才可卖，⛔ 不是自然日 +1）。
    """

    trade_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    volume: int
    price: Decimal
    date: _date
    fees: dict[FeeItem, Decimal]
    sellable_date: _date | None = None


@dataclass
class Position:
    """单标的持仓（推导态，可由 Journal 重放重建）。

    ``sellable`` = 可卖数量（T+1：当日买入不可卖）；``volume`` 为总持仓。
    ``market_value`` 在停牌日**冻结**（沿用上一 ``last_close``，见 settle）。
    """

    symbol: str
    volume: int = 0
    sellable: int = 0
    avg_cost: Decimal = _ZERO
    last_close: Decimal = _ZERO
    market_value: Decimal = _ZERO


@dataclass
class PortfolioView:
    """组合快照（可重算视图 —— 从 Journal 推导，⛔ 不落盘）。

    ``nav = cash + frozen_cash 之外的 Σ market_value``？不 —— 契约 §9 明确：
    ``NAV = cash + Σ market_value``（``frozen_cash`` 是 ``cash`` 中被挂单占用的
    部分的**标记**，不重复计入，也不从 cash 里扣掉）。
    """

    cash: Decimal = _ZERO
    frozen_cash: Decimal = _ZERO
    positions: dict[str, Position] = field(default_factory=dict)
    nav: Decimal = _ZERO
    date: _date = field(kw_only=True)   # 必填（见模块 docstring ③）
