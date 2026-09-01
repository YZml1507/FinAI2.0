#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T201 §7 撮合引擎 —— A 股语义（停牌 / 涨跌停 / T+1 / 整手 / 资金）。

``MatchEngine`` 是**纯函数、无状态**的：一次只吃一根 ``Bar`` + 一笔 ``Order``，
不读账本、不写账本、不发起任何 IO。账本副作用全部由 ``backtest.broker`` 负责
—— 这样同一份撮合规则能被回测 / 模拟盘 / 实盘的"下单前置校验"复用（SDD-1）。

**规则顺序即契约**（T201 §7 表格 1-8，⛔ 不许重排 —— 拒绝理由是 T202 五必挂
用例的断言对象，顺序变了理由就变了）：

  | # | 检查 | 拒绝理由 |
  |---|---|---|
  | 1 | bar 缺席（``None`` = 停牌 / 无行情） | ``停牌不可下单`` |
  | 2 | BUY + ``bar.limit_up``               | ``涨停买入不可成交`` |
  | 3 | SELL + ``bar.limit_down``            | ``跌停卖出不可成交`` |
  | 4 | SELL + ``volume > sellable``         | ``T+1 可卖不足`` |
  | 5 | BUY + ``volume % 100 != 0``          | ``买入须整手100股`` |
  | 6 | SELL + ``volume < 100`` 且 ``volume != position_volume`` | ``零股卖出须清仓`` |
  | 7 | BUY + 需款 > ``cash_available``      | ``资金不足`` |
  | 8 | 全过 → ``FILLED``，成交价 = ``bar.open``（FR-BT-6 次一开盘） | — |

红线落点：

  ① **全有或全无**（T201 §12）：v1 不做部分成交。``MatchResult`` 保留
     ``PARTIAL`` / ``NO_FILL`` 成员（契约 §7 枚举形状 + 冲击成本模型留口），
     但 ``match()`` **只会**返回 ``FILLED`` / ``REJECTED``。
  ② **金额全 ``Decimal``**：⛔ 无 float 参与任何比较或乘除。
  ③ **成交幂等**：``trade_id = f"{client_order_id}:{bar.date}"`` —— 同一笔委托在
     同一交易日重复撮合产出同一个 ``trade_id``，下游 Journal 的 ``tx_hash``
     命中即跳过，不会二次记账。
  ④ **T+1 fail-closed**：BUY 成交的 ``sellable_date`` 由 broker 经
     ``MatchContext.sellable_date`` 注入（下一个**交易日**）。未注入时本层
     **不留成 ``None``**（``None`` 在 ledger 里语义是"立即可卖" = T+1 失守），
     而是保守取 ``bar.date + 1 天`` —— 至少锁住当日。
  ⑤ **费用**：真实费用模型是 T203 的活。本层默认给四个必填科目
     （``COMMISSION`` / ``STAMP_TAX`` / ``TRANSFER_FEE`` / ``HANDLING_FEE``）
     置 0，并在规则 7 用 ``PREFLIGHT_FEE_RATE``（万三）做**保守资金垫**；
     T203 落地后通过 ``MatchEngine(fee_model=...)`` 注入即可，⛔ 不要在这里
     内联真实费率。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from datetime import timedelta
from decimal import Decimal
from enum import Enum
from typing import Callable, Mapping

from backtest.constants import FeeItem, OrderSide
from backtest.types import Bar, Order, Trade

__all__ = [
    "MatchResult",
    "MatchContext",
    "MatchEngine",
    "PREFLIGHT_FEE_RATE",
    "REQUIRED_FEE_ITEMS",
    "REJECT_SUSPENDED",
    "REJECT_LIMIT_UP_BUY",
    "REJECT_LIMIT_DOWN_SELL",
    "REJECT_T1_INSUFFICIENT",
    "REJECT_BUY_LOT_SIZE",
    "REJECT_ODD_LOT_SELL",
    "REJECT_CASH_INSUFFICIENT",
    "REJECT_NON_POSITIVE_VOLUME",
    "LOT_SIZE",
]

_ZERO = Decimal("0")

#: A 股一手 = 100 股（买入必须整手；卖出零股只允许清仓）。
LOT_SIZE = 100

#: 规则 7 的**撮合前置**费用垫（万三）。⛔ 不是真实费率 —— 真实费用由 T203 接管。
PREFLIGHT_FEE_RATE = Decimal("0.0003")

#: ``Trade.fees`` 必填科目（T201 §3）。
REQUIRED_FEE_ITEMS: tuple[FeeItem, ...] = (
    FeeItem.COMMISSION,
    FeeItem.STAMP_TAX,
    FeeItem.TRANSFER_FEE,
    FeeItem.HANDLING_FEE,
)

# —— 拒绝理由（中文串，T202 断言对象；⛔ 改字面量即改契约） ——
REJECT_SUSPENDED = "停牌不可下单"
REJECT_LIMIT_UP_BUY = "涨停买入不可成交"
REJECT_LIMIT_DOWN_SELL = "跌停卖出不可成交"
REJECT_T1_INSUFFICIENT = "T+1 可卖不足"
REJECT_BUY_LOT_SIZE = "买入须整手100股"
REJECT_ODD_LOT_SELL = "零股卖出须清仓"
REJECT_CASH_INSUFFICIENT = "资金不足"
REJECT_NON_POSITIVE_VOLUME = "委托数量须为正"


class MatchResult(str, Enum):
    """撮合结论。

    v1 只产出 ``FILLED`` / ``REJECTED``；``PARTIAL`` / ``NO_FILL`` 是契约 §7 声明
    的留口（冲击成本 / 排队模型），⛔ 现在不要产出它们。
    """

    FILLED = "FILLED"
    PARTIAL = "PARTIAL"        # 留口：部分成交（v1 不产出）
    NO_FILL = "NO_FILL"        # 留口：挂着没成（v1 用 EXPIRED 表达）
    REJECTED = "REJECTED"


@dataclass
class MatchContext:
    """撮合输入快照（由 broker 按当日账本状态组装）。

    Attributes:
        bar: 当日行情；``None`` = 停牌 / 无行情（feed 的"停牌 = 键缺席"语义）。
        order: 待撮合委托。
        sellable: 该 symbol 当前**可卖**数量（T+1 已解禁部分）。
        cash_available: 可用现金（``book.cash - book.frozen_cash``）。
        position_volume: 该 symbol **总**持仓（规则 6 判零股清仓用）。
        sellable_date: BUY 成交的 T+1 解禁日（下一个交易日）；见模块 docstring ④。
    """

    bar: Bar | None
    order: Order
    sellable: int = 0
    cash_available: Decimal = _ZERO
    position_volume: int = 0
    sellable_date: _date | None = field(default=None)


#: 费用模型注入点（T203）：``(order, fill_price, volume, bar) -> {FeeItem: Decimal}``。
FeeModelFn = Callable[[Order, Decimal, int, Bar], Mapping[FeeItem, Decimal]]

#: 成交价模型注入点（T204）：``(order, bar) -> 实际成交价``。
#: ``None`` ⇒ 默认 ``bar.open``（次一开盘，FR-BT-6）。语义声明 + 敏感度对比
#: 见 ``docs/t204_price_model_sensitivity.md``。
PriceModelFn = Callable[[Order, Bar], Decimal]


class MatchEngine:
    """纯函数撮合，无状态 —— 可安全共享单例、可跨环境复用。"""

    def __init__(
        self,
        fee_model: FeeModelFn | None = None,
        price_model: PriceModelFn | None = None,
    ) -> None:
        """
        Args:
            fee_model: 可选费用模型（T203 落地后注入）。``None`` ⇒ 四个必填科目
                置 0，规则 7 用 ``PREFLIGHT_FEE_RATE`` 做保守垫。
            price_model: 可选成交价模型（T204）。``None`` ⇒ 规则 8 恒取
                ``bar.open``（次一开盘，默认口径不变）；注入后由模型产出实际
                成交价（如 开盘+滑点），规则 7 资金校验与 Trade.price 同步跟随。
        """
        self.fee_model = fee_model
        self.price_model = price_model

    # ------------------------------------------------------------------ 公开 API

    def match(self, ctx: MatchContext) -> tuple[MatchResult, Trade | None, str]:
        """按契约 §7 顺序逐条检查。

        Returns:
            ``(MatchResult.FILLED, Trade, "")`` 或 ``(MatchResult.REJECTED, None, 理由)``。
            ⛔ 不 raise（撮合拒绝是**正常业务结果**，不是异常）—— 唯一例外是
            调用方传了结构性错误的 ctx（如 ``order`` 为 None），那是编程错误。
        """
        order = ctx.order
        bar = ctx.bar
        volume = int(order.volume)

        # 规则 0（防御）：数量非正。broker 在 submit 阶段就该拦下，这里纵深防御。
        if volume <= 0:
            return MatchResult.REJECTED, None, REJECT_NON_POSITIVE_VOLUME

        # 规则 1：停牌 / 无行情。
        if bar is None:
            return MatchResult.REJECTED, None, REJECT_SUSPENDED

        side = order.side

        # 规则 2：涨停不可买入（封板买不到）。
        if side is OrderSide.BUY and bar.limit_up:
            return MatchResult.REJECTED, None, REJECT_LIMIT_UP_BUY

        # 规则 3：跌停不可卖出（封板卖不掉）。
        if side is OrderSide.SELL and bar.limit_down:
            return MatchResult.REJECTED, None, REJECT_LIMIT_DOWN_SELL

        # 规则 4：T+1 可卖不足（当日买入当日不可卖）。
        if side is OrderSide.SELL and volume > int(ctx.sellable):
            return MatchResult.REJECTED, None, REJECT_T1_INSUFFICIENT

        # 规则 5：买入须整手。
        if side is OrderSide.BUY and volume % LOT_SIZE != 0:
            return MatchResult.REJECTED, None, REJECT_BUY_LOT_SIZE

        # 规则 6：零股卖出只允许一次性清仓。
        if (
            side is OrderSide.SELL
            and volume < LOT_SIZE
            and volume != int(ctx.position_volume)
        ):
            return MatchResult.REJECTED, None, REJECT_ODD_LOT_SELL

        # 成交价：默认 = 次一开盘（FR-BT-6）；T204 起可由 price_model 注入
        # （如 开盘+滑点）。规则 2/3 的涨跌停拒绝在价格模型**之前**（一字板语义），
        # 滑点限幅由价格模型自身负责（13 号：滑点后成交价不越涨跌停价）。
        fill_price = (
            self.price_model(order, bar) if self.price_model is not None else bar.open
        )
        if not isinstance(fill_price, Decimal):
            raise TypeError(
                f"price_model 必须返回 Decimal（⛔ 禁 float）: {fill_price!r}")
        fees = self._fees(order, fill_price, volume, bar)
        total_fees = sum(fees.values(), _ZERO)

        # 规则 7：买入资金校验（含费用预估）。
        if side is OrderSide.BUY:
            gross = fill_price * Decimal(volume)
            if self.fee_model is None:
                # 无费用模型 ⇒ 用万三保守垫（撮合前置检查用，非真实费用）。
                needed = gross + (gross * PREFLIGHT_FEE_RATE)
            else:
                needed = gross + total_fees
            if needed > ctx.cash_available:
                return MatchResult.REJECTED, None, REJECT_CASH_INSUFFICIENT

        # 规则 8：成交。
        trade = Trade(
            trade_id=self.make_trade_id(order.client_order_id, bar.date),
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=side,
            volume=volume,
            price=fill_price,
            date=bar.date,
            fees=fees,
            sellable_date=self._sellable_date(ctx, bar, side),
        )
        return MatchResult.FILLED, trade, ""

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def make_trade_id(client_order_id: str, trade_date: _date) -> str:
        """幂等成交键：``{client_order_id}:{YYYY-MM-DD}``（模块 docstring ③）。"""
        return f"{client_order_id}:{trade_date.isoformat()}"

    def _fees(
        self, order: Order, fill_price: Decimal, volume: int, bar: Bar,
    ) -> dict[FeeItem, Decimal]:
        """费用明细：默认四科目置 0（T203 接管）；有 ``fee_model`` 则用它并补齐必填键。"""
        fees: dict[FeeItem, Decimal] = {item: _ZERO for item in REQUIRED_FEE_ITEMS}
        if self.fee_model is None:
            return fees
        produced = self.fee_model(order, fill_price, volume, bar)
        for item, amount in produced.items():
            fees[item] = Decimal(amount)
        return fees

    @staticmethod
    def _sellable_date(
        ctx: MatchContext, bar: Bar, side: OrderSide,
    ) -> _date | None:
        """BUY → T+1 解禁日；SELL → ``None``（卖出不产生锁定批次）。

        ⛔ BUY 绝不返回 ``None``：ledger 把 ``None`` 视为"立即可卖"，那是 T+1
        失守。未注入 ``ctx.sellable_date`` 时保守取 ``bar.date + 1 天``
        （broker 会在下一个交易日开盘前 ``advance_sellable`` 解禁）。
        """
        if side is not OrderSide.BUY:
            return None
        if ctx.sellable_date is not None:
            return ctx.sellable_date
        return bar.date + timedelta(days=1)
