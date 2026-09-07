#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T201 §8 Broker —— ``Broker`` 协议 + ``BacktestBroker``。

四环境同构的**唯一差异收敛点**（SDD-1）：撮合规则（``matching``）、账本
（``ledger``）、状态机（``order_fsm``）三份代码全环境共用，回测 / 模拟盘 / 实盘
的区别只是换一个 Broker 实现 —— 回测用 ``MatchEngine`` 自己撮合，实盘把
``submit`` 转成券商 API 调用、``on_bars`` 变成回报回调。

``BacktestBroker`` 的日内时序（⛔ 顺序即契约，见 ``settle.py`` 顺序约束）：

```
submit(order)            # PENDING_SUBMIT → SUBMITTED，进 _pending
on_bars(date, bars)      # 撮合 _pending 里的 SUBMITTED 单 → FILLED / REJECTED
                         #   FILLED   → ledger.process_trade + 出 _pending
                         #   REJECTED → 填 reject_reason + 出 _pending
settle(date, exdiv)      # ① 未成交残单 → EXPIRED
                         # ② 除权除息 → ledger.process_exdiv
                         # ③ settle_day 刷市值 + NAV（⛔ 必在除权之后）
                         # ④ 写 SETTLE 流水
                         # ⑤ advance_sellable(date+1) → T+1 解禁
```

两条关键口径：

  ① **DAY 单**：v1 所有委托都是当日有效。当日撮合没成（含停牌被拒之外的情形）
     一律 ``EXPIRED``，⛔ 不隔夜挂单 —— 隔夜挂单在 A 股要重新报，回测里留着会
     产生"昨天的价格今天成交"的前视。
  ② **T+1 在日终推进**：``settle`` 末尾调 ``advance_sellable(date + 1 天)``，语义
     是"截至下一交易日开盘，这些批次已可卖"。⛔ 不能在 ``on_bars`` 之后、当日内
     解禁（那就成了当日买当日卖）；也不能等到下一日 ``settle``（那就成了 T+2）。
"""
from __future__ import annotations

import logging
from datetime import date as _date
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from backtest.constants import FeeItem, OrderSide, OrderStatus
from backtest.feed import DataFeed
from backtest.ledger import JournalEntry, JournalType, Ledger
from backtest.matching import MatchContext, MatchEngine, MatchResult
from backtest.order_fsm import OrderStateMachine
from backtest.settle import ExdivEvent, normalize_exdiv_event, settle_day_detail
from backtest.types import Bar, Order, Trade

logger = logging.getLogger(__name__)

__all__ = [
    "BrokerError",
    "Broker",
    "BacktestBroker",
    "EXPIRE_REASON",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")

#: 日终过期的说明（写入 ``order.reject_reason``？⛔ 不写 —— EXPIRED 不是 REJECTED，
#: 只记 log / meta，避免下游把过期当拒绝统计）。
EXPIRE_REASON = "日终未成交自动过期"


class BrokerError(RuntimeError):
    """Broker 域错误（重复 client_order_id、账本状态非法等）。"""


@runtime_checkable
class Broker(Protocol):
    """经纪商协议（契约 §8 逐字）。"""

    def submit(self, order: Order) -> Order:
        """接收委托：``PENDING_SUBMIT → SUBMITTED``，返回同一个 Order 实例。"""
        ...

    def cancel(self, client_order_id: str) -> Order | None:
        """撤单。不存在 / 非 ``SUBMITTED`` → 返回 ``None``（⛔ 不 raise）。"""
        ...

    def on_bars(self, date: _date, bars: dict[str, Bar]) -> None:
        """用当日行情撮合全部待成交委托。"""
        ...

    def settle(self, date: _date, exdiv_events: Mapping[str, Any] | None = None) -> None:
        """日终结算：残单过期 → 除权 → 刷市值 → T+1 推进。"""
        ...

    @property
    def book(self):
        """当前推导视图（``BookView``）。"""
        ...


class BacktestBroker:
    """回测经纪商：本地撮合 + 本地账本，无 IO。"""

    def __init__(
        self,
        matcher: MatchEngine,
        ledger: Ledger,
        feed: DataFeed | None = None,
        *,
        fsm: OrderStateMachine | None = None,
        enable_dividend_tax: bool = False,
    ) -> None:
        """
        Args:
            matcher: 撮合引擎（``MatchEngine``，无状态可共享）。
            ledger: 双账本（Journal + BookView）。
            feed: 行情源。回测撮合只吃 ``on_bars`` 传进来的 bars，本参数仅用于
                日终补取停牌判定所需的行情（可为 ``None``）。
            fsm: 状态机（默认新建；无状态，可共享单例）。
            enable_dividend_tax: 是否开启 T309 红利税（默认 False 保证向后兼容，
                红利策略回测开启）。
        """
        self.matcher = matcher
        self.ledger = ledger
        self.feed = feed
        self.fsm = fsm or OrderStateMachine()
        self.enable_dividend_tax = bool(enable_dividend_tax)
        #: client_order_id → Order（只放**未终态**的活动委托）
        self._pending: dict[str, Order] = {}
        #: 全生命周期订单登记（终态也留着，供 BacktestResult 汇总）
        self._all_orders: dict[str, Order] = {}
        #: 全部成交（按发生顺序）
        self._trades: list[Trade] = []
        #: 历史送转股事件 [(date, symbol, factor)]
        self._split_events: list[tuple[_date, str, Decimal]] = []
        #: 最近一次 on_bars 的日期与行情（settle 复用，避免重复取数）
        self._last_bars_date: _date | None = None
        self._last_bars: dict[str, Bar] = {}

    # ------------------------------------------------------------------ 只读

    @property
    def book(self):
        """当前 ``BookView``（委托给 ledger，⛔ 不另存一份账本）。"""
        return self.ledger.book

    @property
    def pending_orders(self) -> list[Order]:
        """活动委托快照（``SUBMITTED`` / ``PARTIALLY_FILLED``）。"""
        return list(self._pending.values())

    @property
    def orders(self) -> list[Order]:
        """全部订单（含终态），按提交顺序。"""
        return list(self._all_orders.values())

    @property
    def trades(self) -> list[Trade]:
        """全部成交快照。"""
        return list(self._trades)

    def pending_symbols(self) -> set[str]:
        """活动委托涉及的标的集合（引擎组装取数 symbol 用）。"""
        return {o.symbol for o in self._pending.values()}

    def position_symbols(self) -> set[str]:
        """当前**非零**持仓标的集合。"""
        return {s for s, p in self.book.positions.items() if p.volume > 0}

    # ------------------------------------------------------------------ 下单 / 撤单

    def submit(self, order: Order) -> Order:
        """接收委托：``PENDING_SUBMIT → SUBMITTED``。

        Raises:
            BrokerError: ``client_order_id`` 重复（幂等键冲突，⛔ 不静默覆盖 ——
                覆盖会让两笔不同委托共用一个成交幂等键）。
            OrderStateError: 订单不在 ``PENDING_SUBMIT``（重复提交同一实例）。
        """
        if order.client_order_id in self._all_orders and (
            self._all_orders[order.client_order_id] is not order
        ):
            raise BrokerError(
                f"client_order_id 重复: {order.client_order_id}"
                f"（幂等键冲突，⛔ 不覆盖）")
        self.fsm.transition(order, OrderStatus.SUBMITTED)
        self._all_orders[order.client_order_id] = order
        self._pending[order.client_order_id] = order
        return order

    def cancel(self, client_order_id: str) -> Order | None:
        """撤单：``SUBMITTED``（或 ``PARTIALLY_FILLED``）→ ``CANCELLED``。

        不存在 / 已终态 ⇒ 返回 ``None``（⛔ 不 raise：撤已成交的单是**正常竞态**，
        不是编程错误）。
        """
        order = self._pending.get(client_order_id)
        if order is None:
            return None
        if order.status not in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
            return None
        self.fsm.transition(order, OrderStatus.CANCELLED)
        self._pending.pop(client_order_id, None)
        return order

    # ------------------------------------------------------------------ 撮合

    def on_bars(self, date: _date, bars: dict[str, Bar]) -> None:
        """用当日行情撮合全部活动委托（先撮合、后信号 —— 见 ``engine``）。

        每笔委托独立组装 ``MatchContext``（可卖 / 可用资金 / 总持仓都取**当下**
        账本状态）—— 所以同日多笔买单会按顺序消耗现金，第 N 笔可能因"资金不足"
        被拒，这是刻意的（贴近真实资金约束）。
        """
        self._last_bars_date = date
        self._last_bars = dict(bars)
        if not self._pending:
            return
        # 快照 key 列表：循环体里会改 _pending。
        for client_order_id in list(self._pending):
            order = self._pending.get(client_order_id)
            if order is None:
                continue
            if order.status not in (
                OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED,
            ):
                continue
            self._match_one(order, date, bars.get(order.symbol))

    def _seed_last_close(self, symbol: str, bar: Bar | None) -> None:
        """新建仓位的 ``last_close`` 播种（⛔ 只在它还是 0 时播，不覆盖已有口径）。

        为什么需要：``BookView.apply_trade_effect`` 只在 ``pos.last_close > 0`` 时
        刷 ``market_value``。首次建仓时 ``last_close`` 还是 0 ⇒ 市值留 0，而现金
        已经扣掉了 —— 在**当日 settle 之前**读 ``book.total_nav``（引擎里策略的
        ``on_bar`` 正好在这个窗口）会看到净值凭空少了一整笔买入金额，按净值做
        仓位控制的策略会误判。用当日 bar 的 ``close`` 播种即可闭合这个窗口；
        日终 ``settle_day`` 会用同一个 close 再覆盖一次（幂等）。
        """
        if bar is None:
            return
        pos = self.book.positions.get(symbol)
        if pos is None or pos.volume <= 0 or pos.last_close > 0:
            return
        pos.last_close = bar.close
        pos.market_value = bar.close * Decimal(pos.volume)
        self.book.recompute_nav()

    def _match_one(self, order: Order, date: _date, bar: Bar | None) -> None:
        """撮合单笔委托并落地结果（FILLED → 记账；REJECTED → 状态迁移）。"""
        pos = self.book.positions.get(order.symbol)
        ctx = MatchContext(
            bar=bar,
            order=order,
            sellable=int(pos.sellable) if pos else 0,
            cash_available=self.book.cash - self.book.frozen_cash,
            position_volume=int(pos.volume) if pos else 0,
            sellable_date=self._next_sellable_date(date),
        )
        result, trade, reason = self.matcher.match(ctx)
        if result is MatchResult.FILLED and trade is not None:
            self._apply_fill(order, trade)
            self._seed_last_close(order.symbol, bar)
            return
        if result is MatchResult.REJECTED:
            self.fsm.transition(order, OrderStatus.REJECTED, reason=reason)
            order.reject_reason = reason
            self._pending.pop(order.client_order_id, None)
            return
        # v1 撮合只产出 FILLED / REJECTED（契约 §7）；其余留口结论视为未成交，
        # 留在 _pending 等日终 EXPIRED。⛔ 不静默当成成交。
        logger.warning(
            "撮合返回未支持的结论 %s（订单 %s），按未成交处理",
            result, order.client_order_id)

    def _apply_fill(self, order: Order, trade: Trade) -> None:
        """成交落地：账本记账 + 订单 FILLED + 出 _pending。

        ``ledger.process_trade`` 自带幂等（``trade_id`` → ``tx_hash`` 命中即跳过），
        所以同日重复撮合同一笔委托不会二次扣钱。
        """
        self.ledger.process_trade(trade)
        order.fills.append(trade)
        order.filled_volume += int(trade.volume)
        # 成交量加权均价（v1 全有或全无 ⇒ 只会有一笔 fill；公式仍按加权写，
        # 部分成交模型接上后无需改这里）。
        total_volume = sum(f.volume for f in order.fills)
        order.avg_fill_price = (
            sum((f.price * Decimal(f.volume) for f in order.fills), _ZERO)
            / Decimal(total_volume)
        )
        self.fsm.transition(order, OrderStatus.FILLED)
        self._trades.append(trade)
        self._pending.pop(order.client_order_id, None)

    @staticmethod
    def _next_sellable_date(date: _date) -> _date:
        """买入成交的 T+1 解禁日基准（``date + 1`` 自然日）。

        用自然日 +1 而不是"下一个交易日"是安全的：解禁由 ``advance_sellable``
        在**交易日**推进时比较 ``sellable_date <= today`` 完成，周末不开盘 ⇒
        不会提前解禁；且不需要在撮合热路径上依赖交易日历（⛔ 不静默打网）。
        """
        return date + timedelta(days=1)

    # ------------------------------------------------------------------ 日终

    def settle(
        self,
        date: _date,
        exdiv_events: Mapping[str, Any] | None = None,
        *,
        bars: Mapping[str, Bar] | None = None,
    ) -> set[str]:
        """日终结算（顺序见模块 docstring）。返回受影响 symbol 集合。

        Args:
            date: 结算日。
            exdiv_events: ``{symbol: ExdivEvent | dict | (factor, dividend) | factor}``。
            bars: 当日行情；``None`` ⇒ 复用同日 ``on_bars`` 的 bars（引擎正常路径）。
                同日没调过 ``on_bars`` 且未显式给 bars ⇒ 视为**全部停牌**
                （市值冻结），⛔ 不偷偷去 feed 取数。
        """
        day_bars: Mapping[str, Bar]
        if bars is not None:
            day_bars = bars
        elif self._last_bars_date == date:
            day_bars = self._last_bars
        else:
            logger.warning(
                "settle(%s) 没有当日 bars（未调 on_bars 也未显式传入）⇒ "
                "全部持仓市值冻结", date)
            day_bars = {}

        # ① 残单过期（DAY 单）。
        self._expire_pending(date)

        # ② 除权除息（⛔ 必须先于 settle_day 刷市值）。
        events = self._normalize_events(exdiv_events)
        for symbol, event in events.items():
            pos = self.book.positions.get(symbol)
            old_vol = pos.volume if pos else 0
            if event.factor != _ONE and event.factor > _ZERO:
                self._split_events.append((date, symbol, event.factor))
            self.ledger.process_exdiv(
                symbol,
                event.factor,
                event.cash_dividend,
                date=date,
                ref_id=f"EXDIV:{symbol}:{date.isoformat()}",
            )
            if self.enable_dividend_tax and old_vol > 0 and event.cash_dividend > _ZERO:
                self._apply_dividend_tax(symbol, event, old_vol, date)

        # ③ 刷市值 + NAV（settle.py：停牌市值冻结）。
        report = settle_day_detail(self.book, date, day_bars, events or None)

        # ④ 写 SETTLE 快照流水。⛔ 不调 ledger.settle —— 那会二次刷市值。
        self.ledger.journal.append(
            JournalEntry.create(
                date=date,
                entry_type=JournalType.SETTLE,
                amount=_ZERO,
                ref_id=f"SETTLE:{date.isoformat()}",
                meta={
                    "nav": self.book.nav,
                    "cash": self.book.cash,
                    "market_value": self.book.total_market_value(),
                    "refreshed": sorted(report.refreshed),
                    "frozen": sorted(report.frozen),
                    "limit_down": sorted(report.limit_down),
                },
            )
        )

        # ⑤ T+1 推进：截至下一交易日开盘可卖。
        self.ledger.advance_sellable(self._next_sellable_date(date))
        return set(report.affected)

    def _expire_pending(self, date: _date) -> None:
        """未成交残单 → ``EXPIRED``（DAY 单纪律）。

        ⭐ **只过期"已经有过撮合机会"的委托**：``order.created_date < date``。

        引擎的日内顺序是"先撮合（②）后信号（③）"—— 策略在 ③ 里下的单
        ``created_date == date``，它的撮合机会在**下一个交易日**的 ②。若在当日
        ④ 就把它过期掉，任何委托都永远不可能成交（曾经的实现 bug，单测
        ``test_engine_simple_buy_position_and_nav`` 守这条）。

        反过来，``created_date < date`` 的单说明它今天 ② 已经过过撮合了还留在
        ``_pending``（正常路径不会发生：``on_bars`` 必把每笔委托判成 FILLED 或
        REJECTED；只有当日**根本没调** ``on_bars`` 才会留下）—— 那就按 DAY 单
        纪律过期，⛔ 不隔夜挂单（隔夜挂单会产生"昨天的价格今天成交"的前视）。
        """
        for client_order_id in list(self._pending):
            order = self._pending[client_order_id]
            if order.created_date >= date:
                continue                      # 今天刚下的单，机会在明天
            self._pending.pop(client_order_id, None)
            if self.fsm.is_terminal(order.status):
                continue
            self.fsm.transition(order, OrderStatus.EXPIRED)
            logger.debug("%s %s：%s", date, order.client_order_id, EXPIRE_REASON)

    def expire_all(self, date: _date) -> list[Order]:
        """把**全部**活动委托过期掉（回测收尾用：区间最后一天下的单没有下一日）。

        与 ``_expire_pending`` 的区别：不看 ``created_date`` —— 收尾时"明天"不存在。
        """
        expired: list[Order] = []
        for client_order_id in list(self._pending):
            order = self._pending.pop(client_order_id)
            if self.fsm.is_terminal(order.status):
                continue
            self.fsm.transition(order, OrderStatus.EXPIRED)
            expired.append(order)
            logger.debug("%s %s：%s（回测收尾）", date, client_order_id, EXPIRE_REASON)
        return expired

    @staticmethod
    def _normalize_events(
        exdiv_events: Mapping[str, Any] | None,
    ) -> dict[str, ExdivEvent]:
        """把各种写法的除权事件规整成 ``{symbol: ExdivEvent}``（``None`` 值跳过）。"""
        if not exdiv_events:
            return {}
        out: dict[str, ExdivEvent] = {}
        for symbol, raw in exdiv_events.items():
            if raw is None:
                continue
            out[symbol] = normalize_exdiv_event(symbol, raw)
        return out

    def _apply_dividend_tax(
        self, symbol: str, event: ExdivEvent, old_volume: int, date: _date
    ) -> None:
        """T309 红利税：FIFO 配对追溯持股期并扣税。"""
        from backtest.dividend_tax import DividendEvent as DivTaxEvent, compute_dividend_tax

        div_ev = DivTaxEvent(
            ex_date=date,
            symbol=symbol,
            dividend_per_share=event.cash_dividend,
            shares_held=old_volume,
        )
        buys = [
            (t.date, t.symbol, int(t.volume))
            for t in self._trades
            if t.symbol == symbol and t.side is OrderSide.BUY
        ]
        sells = [
            (t.date, t.symbol, int(t.volume))
            for t in self._trades
            if t.symbol == symbol and t.side is OrderSide.SELL
        ]
        splits = [
            (d, s, f) for d, s, f in self._split_events if s == symbol
        ]
        tax = compute_dividend_tax([div_ev], buys, sells, split_events=splits)
        if tax > _ZERO:
            self.book.cash -= tax
            self.book.recompute_nav()
            self.ledger.journal.append(
                JournalEntry.create(
                    date=date,
                    entry_type=JournalType.DIVIDEND_TAX,
                    symbol=symbol,
                    amount=-tax,
                    fees={FeeItem.DIVIDEND_TAX: tax},
                    ref_id=f"DIVTAX:{symbol}:{date.isoformat()}",
                    meta={
                        "shares_held": old_volume,
                        "cash_dividend": str(event.cash_dividend),
                        "tax": str(tax),
                    },
                )
            )

    # ------------------------------------------------------------------ 辅助

    def deposit(self, amount: Decimal, *, date: _date, ref_id: str = "") -> bool:
        """入金（走 ledger 的 ``CASH_IN`` 流水）。策略 ``before_run`` 钩子常用。"""
        return self.ledger.deposit(Decimal(amount), date=date, ref_id=ref_id)

    def register_trades(self, trades: Iterable[Trade]) -> None:
        """从历史成交重建 T+1 待解禁索引（热启动 / 实盘对账用）。"""
        self.ledger.register_trades(trades)
