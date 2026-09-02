#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T201 §10 事件循环 —— ``BacktestEngine`` + ``BacktestResult``。

全事件驱动单引擎（SDD-3）：⛔ 不做向量化双轨，⛔ 不做多线程撮合。一天一根
循环体，顺序**不可颠倒**：

```
for date in feed.get_trading_dates(start, end):
    bars = feed.get_bars(symbols, date)            # ① 取数
    broker.on_bars(date, bars)                     # ② 先撮合（吃的是昨天的委托）
    strategy.on_bar(date, bars, book, broker)      # ③ 后信号（今天下的单明天成交）
    broker.settle(date, exdiv_events)              # ④ 日终：过期 / 除权 / 市值 / T+1
    nav_curve[date] = book.total_nav               # ⑤ 记净值
```

⭐ **先撮合后信号**（13 号 L370）是**零前视**的结构性保证：策略在 ③ 里下的单
进 ``_pending``，要到**下一个交易日**的 ② 才被撮合，成交价取那天的 ``bar.open``
—— 结构上不可能出现"当日信号当日成交"。⛔ 把 ② ③ 调换即引入前视偏差，回测收益
会凭空虚高，这是最贵的一类 bug。

**取数范围**（① 的 ``symbols``）= 活动委托标的 ∪ 非零持仓标的 ∪
``strategy.watchlist``。前两者是**必须**的（漏了挂单 ⇒ 该单当日无行情被判停牌
拒单；漏了持仓 ⇒ 该持仓市值被冻结，净值算错）；``watchlist`` 是策略选股域。

策略是**鸭子类型**，不要求继承任何基类：

  · ``on_bar(date, bars, book, broker)`` —— 必需。
  · ``before_run(broker)`` —— 可选。做期初入金（``broker.deposit`` 写 ``CASH_IN``
    流水）、预热等。
  · ``after_run(broker)`` —— 可选。
  · ``watchlist`` —— 可选，``Iterable[str]`` 或 ``() -> Iterable[str]``。
  · ``exdiv_events_for(date)`` —— 可选，返回当日除权事件 ``{symbol: ...}``；
    也可以在构造 Engine 时用 ``exdiv_provider`` 注入（二者都给以 provider 为准）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as _date
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence

from backtest.broker import BacktestBroker
from backtest.feed import DataFeed
from backtest.ledger import BookView, JournalEntry
from backtest.types import Order, Trade

logger = logging.getLogger(__name__)

__all__ = [
    "EngineError",
    "BacktestResult",
    "BacktestEngine",
]

_ZERO = Decimal("0")

#: 除权事件注入点：``(date) -> {symbol: ExdivEvent | dict | (factor, dividend)}``。
ExdivProviderFn = Callable[[_date], Mapping[str, Any] | None]


class EngineError(RuntimeError):
    """引擎编排错误（策略缺 ``on_bar``、区间倒置等）。"""


@dataclass
class BacktestResult:
    """回测产物（⛔ 不依赖 pandas —— 契约 §10 写的是 ``pd.Series``，这里退化成
    ``dict[isoformat, Decimal]``：Decimal 进 pandas 会退化成 object 列且易被
    静默转 float，净值就不可复算了。需要 Series 的下游自行
    ``pd.Series(result.nav_curve)``）。"""

    nav_curve: dict[str, Decimal] = field(default_factory=dict)
    orders: list[Order] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    journal_entries: list[JournalEntry] = field(default_factory=list)
    final_nav: Decimal = _ZERO
    final_book: BookView | None = None
    trading_dates: list[_date] = field(default_factory=list)
    bars_by_date: dict[_date, dict[str, Any]] = field(default_factory=dict)  # date → {symbol → Bar}

    @property
    def journal(self) -> list[JournalEntry]:
        """契约 §10 的 ``journal`` 别名（= ``journal_entries``）。"""
        return self.journal_entries

    def nav_at(self, date: _date) -> Decimal:
        """取某日净值（⛔ 缺日期 raise，不返回 0 —— 0 会被误当成爆仓）。"""
        key = date.isoformat()
        if key not in self.nav_curve:
            raise KeyError(f"{key} 不在净值曲线内（可能不是交易日）")
        return self.nav_curve[key]


class BacktestEngine:
    """事件循环编排器。自身**不含**任何交易语义 —— 撮合在 ``matching``、记账在
    ``ledger``、结算在 ``settle``，引擎只管"按日推进、按序调用"。"""

    def __init__(
        self,
        broker: BacktestBroker,
        feed: DataFeed,
        *,
        exdiv_provider: ExdivProviderFn | None = None,
    ) -> None:
        self.broker = broker
        self.feed = feed
        self.exdiv_provider = exdiv_provider

    # ------------------------------------------------------------------ 公开 API

    def run(
        self,
        strategy: Any,
        start: str | _date,
        end: str | _date,
    ) -> BacktestResult:
        """跑一段回测。

        Args:
            strategy: 鸭子类型策略对象（见模块 docstring）。
            start / end: ``'YYYY-MM-DD'`` 或 ``datetime.date``（含端点）。

        Returns:
            ``BacktestResult``。``nav_curve`` 的键集合 **== 交易日历集合**
            （每个交易日必有一条，⛔ 不跳日 —— 跳日会让回撤/年化算错）。
        """
        if not hasattr(strategy, "on_bar"):
            raise EngineError(
                f"策略 {type(strategy).__name__} 缺 on_bar(date, bars, book, broker)"
                f" —— ⛔ 引擎不猜方法名")
        s, e = _as_date(start), _as_date(end)
        if s > e:
            raise EngineError(f"start {s} 晚于 end {e}（区间倒置，拒绝）")

        dates = list(self.feed.get_trading_dates(s, e))
        result = BacktestResult(trading_dates=list(dates))

        before_run = getattr(strategy, "before_run", None)
        if callable(before_run):
            before_run(self.broker)

        for day in dates:
            bars = self.feed.get_bars(sorted(self._symbols_for(strategy)), day)
            # Store bars for metrics calculation
            result.bars_by_date[day] = bars
            # ② 先撮合（⛔ 不可与 ③ 互换：先信号即前视）
            self.broker.on_bars(day, bars)
            # ③ 后信号
            strategy.on_bar(day, bars, self.broker.book, self.broker)
            # ④ 日终
            self.broker.settle(day, self._exdiv_for(strategy, day), bars=bars)
            # ⑤ 记净值
            result.nav_curve[day.isoformat()] = self.broker.book.total_nav

        # 收尾：最后一天下的单没有"下一个交易日"可撮合 ⇒ 全部 EXPIRED，
        # 保证 result.orders 里**没有非终态订单**（⛔ 悬挂的 SUBMITTED 会让
        # 下游统计把它当"还在路上"）。
        if dates:
            self.broker.expire_all(dates[-1])

        after_run = getattr(strategy, "after_run", None)
        if callable(after_run):
            after_run(self.broker)

        result.orders = self.broker.orders
        result.trades = self.broker.trades
        result.journal_entries = list(self.broker.ledger.entries)
        result.final_book = self.broker.book
        result.final_nav = self.broker.book.total_nav
        return result

    # ------------------------------------------------------------------ 内部

    def _symbols_for(self, strategy: Any) -> set[str]:
        """当日取数范围：挂单 ∪ 持仓 ∪ watchlist（见模块 docstring）。"""
        symbols: set[str] = set(self.broker.pending_symbols())
        symbols |= self.broker.position_symbols()
        symbols |= _watchlist(strategy)
        return symbols

    def _exdiv_for(self, strategy: Any, day: _date) -> Mapping[str, Any] | None:
        """当日除权事件：``exdiv_provider`` 优先，其次策略的 ``exdiv_events_for``。"""
        if self.exdiv_provider is not None:
            return self.exdiv_provider(day)
        hook = getattr(strategy, "exdiv_events_for", None)
        if callable(hook):
            return hook(day)
        return None


# ---------------------------------------------------------------------- 工具

def _as_date(value: str | _date) -> _date:
    """``'YYYY-MM-DD'`` / ``date`` → ``datetime.date``（⛔ 不接受模糊格式）。"""
    if isinstance(value, _date):
        return value
    text = str(value).strip()
    parts = text.split("-")
    if len(parts) != 3:
        raise EngineError(f"日期须为 'YYYY-MM-DD' 或 datetime.date，得到 {value!r}")
    try:
        return _date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError as exc:
        raise EngineError(f"无法解析日期 {value!r}: {exc}") from exc


def _watchlist(strategy: Any) -> set[str]:
    """读策略的 ``watchlist``（属性或无参可调用；缺失 → 空集）。"""
    raw = getattr(strategy, "watchlist", None)
    if raw is None:
        return set()
    if callable(raw):
        raw = raw()
    if raw is None:
        return set()
    if isinstance(raw, str):                # 防御：单个代码写成字符串
        return {raw}
    if isinstance(raw, (Sequence, set, frozenset)) or isinstance(raw, Iterable):
        return {str(s) for s in raw}
    raise EngineError(f"strategy.watchlist 无法解析: {raw!r}")
