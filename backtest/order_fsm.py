#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T201 §4 七态订单状态机（SDD-2）—— 四环境共用同一份迁移表。

```
PENDING_SUBMIT → SUBMITTED          # broker 接收
PENDING_SUBMIT → REJECTED           # broker 校验拒绝
SUBMITTED    → PARTIALLY_FILLED     # 部分成交
SUBMITTED    → FILLED               # 全部成交
SUBMITTED    → CANCELLED            # 撤单成功
SUBMITTED    → EXPIRED              # 日终未成交自动过期
SUBMITTED    → REJECTED             # 撮合阶段发现违规（停牌 / 涨跌停）
PARTIALLY_FILLED → FILLED           # 补齐
PARTIALLY_FILLED → CANCELLED        # 撤剩余
PARTIALLY_FILLED → EXPIRED          # 日终剩余过期
```

红线落点：

  ① **非法迁移 raise ``OrderStateError``（继承 ``ValueError``）**，⛔ 绝不静默忽略
     或"尽力而为"地改状态 —— 状态错乱会直接污染账本与净值。
  ② **失败原子性**：``transition`` 先判定后写入，raise 时订单状态**一字不改**
     （单测 ``test_illegal_transition_does_not_mutate`` 守这条）。
  ③ **终态无出边**：``FILLED`` / ``CANCELLED`` / ``EXPIRED`` / ``REJECTED`` 的
     出边集合为空 —— 包括"同态自迁移"（``FILLED → FILLED``）也是非法，避免
     重复回报被当成新事件二次记账。
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from backtest.constants import OrderStatus
from backtest.types import Order

__all__ = [
    "OrderStateError",
    "LEGAL_TRANSITIONS",
    "TERMINAL_STATUSES",
    "can_transition",
    "OrderStateMachine",
]


class OrderStateError(ValueError):
    """非法状态迁移（继承 ``ValueError``，调用方可按值错误统一兜）。"""


# 合法迁移表：唯一登记点。⛔ 改这里必须同步改 T201_design.md §4。
_LEGAL: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_SUBMIT: frozenset(
        {OrderStatus.SUBMITTED, OrderStatus.REJECTED}
    ),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED}
    ),
    # 终态：无出边
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}

#: 只读视图（防止调用方运行时篡改迁移表）
LEGAL_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = MappingProxyType(_LEGAL)

#: 终态集合（无出边）
TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset(
    status for status, outs in _LEGAL.items() if not outs
)


def can_transition(from_status: OrderStatus, to_status: OrderStatus) -> bool:
    """``from_status → to_status`` 是否为合法迁移。

    未知状态（不在表内）一律返回 ``False``，⛔ 不 raise —— 纯查询语义。
    """
    return to_status in _LEGAL.get(from_status, frozenset())


class OrderStateMachine:
    """七态状态机。无状态（迁移表是类级常量），可安全共享单例。"""

    #: 类级暴露，便于测试/巡检引用
    legal_transitions: Mapping[OrderStatus, frozenset[OrderStatus]] = LEGAL_TRANSITIONS

    @staticmethod
    def can_transition(from_status: OrderStatus, to_status: OrderStatus) -> bool:
        """见模块级 :func:`can_transition`。"""
        return can_transition(from_status, to_status)

    @staticmethod
    def allowed_targets(from_status: OrderStatus) -> frozenset[OrderStatus]:
        """``from_status`` 的全部合法目标态（终态返回空集）。"""
        return _LEGAL.get(from_status, frozenset())

    @staticmethod
    def is_terminal(status: OrderStatus) -> bool:
        """是否终态（无出边）。"""
        return status in TERMINAL_STATUSES

    def transition(
        self,
        order: Order,
        target: OrderStatus,
        *,
        reason: str = "",
    ) -> Order:
        """把 ``order`` 迁移到 ``target``，返回**同一个** Order 实例（原地更新）。

        Args:
            order: 待迁移订单（``Order`` 是可变推导态）。
            target: 目标状态。
            reason: 可选原因；``target == REJECTED`` 时写入 ``order.reject_reason``。

        Raises:
            OrderStateError: 目标态非法（含终态出边、同态自迁移、未知状态）。
                raise 时订单**未被修改**。
        """
        current = order.status
        if not can_transition(current, target):
            raise OrderStateError(
                f"非法状态迁移: {getattr(current, 'value', current)} → "
                f"{getattr(target, 'value', target)}"
                f"（订单 {order.client_order_id}）"
            )
        order.status = target
        if target is OrderStatus.REJECTED and reason:
            order.reject_reason = reason
        return order
