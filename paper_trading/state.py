#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T401 §2 状态持久化 —— 持仓/资金/订单队列/日期水位。

``PaperTradingState`` 封装模拟盘运行状态，支持：
  - JSON 原子写（.tmp → os.replace）
  - 从 BookView + pending_orders 构建快照
  - 恢复到 Ledger + Broker
  - 日期水位记录（防止重复执行同一交易日）

幂等保护：
  - ``last_trading_date`` 记录最后执行日期
  - ``state_hash`` 记录状态摘要（防止脏数据恢复）

与回测契约对齐：
  - Position volume/sellable 格式与 ledger.Position 一致
  - Order 状态只持久化 SUBMITTED（终态订单进历史，不重复恢复）
  - cash/frozen_cash/nav 直接映射 BookView
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from typing import Any

from backtest.broker import BacktestBroker
from backtest.constants import OrderSide, OrderStatus
from backtest.ledger import Position
from backtest.types import Order, Trade

logger = logging.getLogger(__name__)

__all__ = ["PaperTradingState", "StateError"]


class StateError(RuntimeError):
    """状态错误（文件损坏/版本不兼容/恢复失败）。"""


@dataclass
class PaperTradingState:
    """模拟盘状态快照（可持久化到 JSON）。"""

    #: 最后执行的交易日（防止重复执行）
    last_trading_date: str = ""  # ISO 8601 "YYYY-MM-DD"

    #: 现金（可用+冻结）
    cash: str = "0"  # Decimal → str
    frozen_cash: str = "0"

    #: 持仓快照 {symbol: {"volume": int, "sellable": int, "cost": str, ...}}
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)

    #: 活动委托快照（仅 SUBMITTED，终态不保存）
    pending_orders: list[dict[str, Any]] = field(default_factory=list)

    #: 成交史（⛔ 红利税 FIFO 重放必需：``broker._trades`` 不落盘 ⇒
    #:    重启后首个除权日「队列空 vs 持仓>0」撞 ``compute_dividend_tax``
    #:    边界校验而崩）。字段只留计税所需五项 + 幂等键。
    trade_history: list[dict[str, Any]] = field(default_factory=list)

    #: 送转事件史（``broker._split_events``，红利税 SPLIT 摊批数据源）
    split_events: list[dict[str, Any]] = field(default_factory=list)

    #: 净值
    nav: str = "0"

    #: 状态版本（兼容性标记）
    version: str = "1.0"

    #: 状态哈希（校验完整性）
    state_hash: str = ""

    def compute_hash(self) -> str:
        """计算状态哈希（排除 state_hash 字段本身）。"""
        data = asdict(self)
        data.pop("state_hash", None)
        canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def save(self, path: Path) -> None:
        """原子写到文件（.tmp → os.replace）。"""
        self.state_hash = self.compute_hash()
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        logger.info("状态已保存: %s（哈希 %s）", path, self.state_hash)

    @classmethod
    def load(cls, path: Path) -> PaperTradingState:
        """从文件加载（校验哈希，损坏即 raise）。"""
        if not path.exists():
            raise StateError(f"状态文件不存在: {path}")
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        state = cls(**data)
        expected_hash = state.compute_hash()
        if state.state_hash != expected_hash:
            raise StateError(
                f"状态哈希不匹配（文件可能损坏）: "
                f"期望 {expected_hash}，实际 {state.state_hash}"
            )
        logger.info("状态已加载: %s（日期 %s）", path, state.last_trading_date)
        return state

    @classmethod
    def from_broker(cls, broker: BacktestBroker, last_date: _date) -> PaperTradingState:
        """从 BacktestBroker 构建快照（用于保存当前状态）。"""
        book = broker.book
        positions_dict: dict[str, dict[str, Any]] = {}
        for symbol, pos in book.positions.items():
            if pos.volume > 0:  # 只保存非零持仓
                positions_dict[symbol] = {
                    "volume": pos.volume,
                    "sellable": pos.sellable,
                    "cost_basis": str(pos.cost_basis),
                    "last_close": str(pos.last_close),
                    "market_value": str(pos.market_value),
                }

        pending_orders_list: list[dict[str, Any]] = []
        for order in broker.pending_orders:
            if order.status == OrderStatus.SUBMITTED:
                pending_orders_list.append({
                    "client_order_id": order.client_order_id,
                    "symbol": order.symbol,
                    "side": order.side.name,
                    "volume": order.volume,
                    "price": str(order.price) if order.price else None,
                    "created_date": order.created_date.isoformat(),
                })

        trade_history_list: list[dict[str, Any]] = [
            {
                "trade_id": t.trade_id,
                "client_order_id": t.client_order_id,
                "date": t.date.isoformat(),
                "symbol": t.symbol,
                "side": t.side.name,
                "volume": int(t.volume),
                "price": str(t.price),
            }
            for t in broker.trades
        ]
        split_events_list: list[dict[str, Any]] = [
            {"date": d.isoformat(), "symbol": s, "factor": str(f)}
            for d, s, f in broker._split_events
        ]

        return cls(
            last_trading_date=last_date.isoformat(),
            cash=str(book.cash),
            frozen_cash=str(book.frozen_cash),
            positions=positions_dict,
            pending_orders=pending_orders_list,
            nav=str(book.total_nav),
            trade_history=trade_history_list,
            split_events=split_events_list,
        )

    def restore_to_broker(self, broker: BacktestBroker) -> None:
        """恢复状态到 Broker（账本+活动委托）。

        ⚠️ 调用前 broker 必须已初始化（入金完成），本方法只恢复持仓与挂单。
        """
        ledger = broker.ledger
        book = ledger.book

        # 恢复现金（覆盖初始入金后的状态）
        book.cash = Decimal(self.cash)
        book.frozen_cash = Decimal(self.frozen_cash)

        # 恢复持仓
        for symbol, pos_data in self.positions.items():
            pos = Position(symbol=symbol)
            pos.volume = pos_data["volume"]
            pos.sellable = pos_data["sellable"]
            pos.cost_basis = Decimal(pos_data["cost_basis"])
            pos.last_close = Decimal(pos_data["last_close"])
            pos.market_value = Decimal(pos_data["market_value"])
            book.positions[symbol] = pos

        # 重算净值（持仓市值+现金）
        book.recompute_nav()

        # 恢复活动委托（仅 SUBMITTED）
        for order_data in self.pending_orders:
            order = Order(
                client_order_id=order_data["client_order_id"],
                symbol=order_data["symbol"],
                side=OrderSide[order_data["side"]],
                order_type=broker.fsm._default_order_type,  # v1 全 MARKET
                volume=order_data["volume"],
                price=Decimal(order_data["price"]) if order_data["price"] else None,
                created_date=_date.fromisoformat(order_data["created_date"]),
                status=OrderStatus.SUBMITTED,
            )
            broker._pending[order.client_order_id] = order
            broker._all_orders[order.client_order_id] = order

        # 恢复成交史 + 送转史（红利税 FIFO 重放数据源；⛔ 必须在除权事件
        #    到来前补回，否则队列空撞边界校验 → 日任务崩）
        for td in self.trade_history:
            broker._trades.append(Trade(
                trade_id=str(td.get("trade_id") or f"restored-{len(broker._trades)}"),
                client_order_id=str(td.get("client_order_id") or ""),
                symbol=str(td["symbol"]),
                side=OrderSide[str(td["side"])],
                volume=int(td["volume"]),
                price=Decimal(str(td["price"])),
                date=_date.fromisoformat(str(td["date"])),
                fees={},
                sellable_date=None,
            ))
        for sd in self.split_events:
            broker._split_events.append(
                (_date.fromisoformat(str(sd["date"])), str(sd["symbol"]), Decimal(str(sd["factor"])))
            )

        logger.info(
            "状态已恢复: 现金 %s, 持仓 %d 只, 挂单 %d 笔",
            self.cash, len(self.positions), len(self.pending_orders)
        )
