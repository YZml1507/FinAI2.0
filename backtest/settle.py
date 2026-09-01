#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T201 §9 日终结算 —— ``settle_day``（四环境共用）。

**只做一件事**：把 ``BookView`` 的持仓市值刷到当日口径并重算 NAV，返回本日
**受影响**的 symbol 集合。

⭐ **顺序约束（唯一正确调用顺序，⛔ 颠倒即算错净值）**：

```
1. broker.on_bars(date, bars)          # 撮合，产生 Trade → ledger.process_trade
2. ledger.process_exdiv(symbol, ...)   # 除权除息：股数 × factor、现金 += 分红/股 × 老股数
3. settle_day(book, date, bars, ...)   # ← 本函数：刷市值 + NAV
4. ledger.advance_sellable(next_day)   # T+1 解禁推进
```

即 **除权必须先于本函数**。理由：``BookView.process_exdiv`` 会把 ``last_close``
按同一 factor 折算以保证净值曲线无跳变（T202 必挂用例 #4），随后本函数用当日
**真实除权后**的 ``bar.close`` 覆盖它 —— 顺序反了就会拿除权前的 close 乘除权后的
股数，市值凭空翻倍。

本函数**不调** ``Ledger.settle`` / ``BookView.settle``，也**不写 Journal**：
避免"双重结算"（同一日市值刷两遍、SETTLE 流水记两条）。写 SETTLE 流水是
``BacktestBroker.settle`` 的活。

两条市值口径（与 ``ledger`` 一致）：

  · bar 存在 → ``last_close = bar.close``、``market_value = volume × close``；
  · **bar 缺失（停牌）→ 市值冻结不动**（沿用上一 ``last_close`` 与
    ``market_value``）—— T202 必挂用例 #3 的"停牌三日 NAV 水平线"锚点。
    ⛔ 不清零、⛔ 不前向捏造价格。

返回集合语义：**受影响 = 刷新的 ∪ 冻结的**（两类都是"本日结算过问的持仓"）。
需要区分时用 ``settle_day_detail``。注意这与 ``BookView.settle`` 的返回值
（**只含刷新的**）不同 —— 本函数多返回停牌冻结的那部分，供上层做停牌告警。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from decimal import Decimal
from typing import Any, Mapping

from backtest.ledger import BookView
from backtest.types import Bar

__all__ = [
    "ExdivEvent",
    "SettleReport",
    "normalize_exdiv_event",
    "settle_day",
    "settle_day_detail",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True)
class ExdivEvent:
    """一次除权除息事件（契约 §8/§9 的 ``ExdivEvent``）。

    Attributes:
        symbol: 标的代码。
        factor: 送股 / 拆股因子（10 送 10 → ``Decimal("2")``；纯现金分红 → ``1``）。
        cash_dividend: **每股**税前现金分红。
        date: 除权日（可选，仅作血缘记录）。
    """

    symbol: str
    factor: Decimal = _ONE
    cash_dividend: Decimal = _ZERO
    date: _date | None = None


@dataclass(frozen=True)
class SettleReport:
    """结算明细（``settle_day_detail`` 返回）。"""

    date: _date
    refreshed: frozenset[str]      # bar 存在，市值已刷新
    frozen: frozenset[str]         # bar 缺失（停牌），市值冻结
    nav: Decimal

    @property
    def affected(self) -> frozenset[str]:
        """受影响 symbol = 刷新的 ∪ 冻结的。"""
        return self.refreshed | self.frozen


def normalize_exdiv_event(symbol: str, raw: Any) -> ExdivEvent:
    """把多种写法的除权事件规整成 ``ExdivEvent``。

    接受：``ExdivEvent`` 原样 / ``{"factor":…, "cash_dividend":…}`` /
    ``(factor, cash_dividend)`` 二元组 / 单个数值（视为 factor）。
    ⛔ 不接受 ``None``（调用方该先过滤）—— 会 raise ``TypeError``。
    """
    if isinstance(raw, ExdivEvent):
        return raw
    if isinstance(raw, Mapping):
        return ExdivEvent(
            symbol=str(raw.get("symbol", symbol)),
            factor=Decimal(str(raw.get("factor", "1"))),
            cash_dividend=Decimal(str(raw.get("cash_dividend", "0"))),
            date=raw.get("date"),
        )
    if isinstance(raw, (tuple, list)):
        if len(raw) == 1:
            return ExdivEvent(symbol=symbol, factor=Decimal(str(raw[0])))
        if len(raw) >= 2:
            return ExdivEvent(
                symbol=symbol,
                factor=Decimal(str(raw[0])),
                cash_dividend=Decimal(str(raw[1])),
            )
        raise TypeError(f"{symbol} 的除权事件是空序列，无法解析")
    if isinstance(raw, (Decimal, int, str)):
        return ExdivEvent(symbol=symbol, factor=Decimal(str(raw)))
    raise TypeError(
        f"无法解析 {symbol} 的除权事件：{raw!r}（支持 ExdivEvent / dict / "
        f"(factor, cash_dividend) / factor 数值）")


def settle_day(
    book: BookView,
    date: _date,
    bars: Mapping[str, Bar],
    exdiv_events: Mapping[str, Any] | None = None,
) -> set[str]:
    """日终刷市值 + NAV，返回受影响 symbol 集合（刷新的 ∪ 停牌冻结的）。

    Args:
        book: 待刷新的推导视图（原地更新 ``last_close`` / ``market_value`` /
            ``nav`` / ``date``）。
        date: 结算日。
        bars: 当日行情（**停牌 = 键缺席**，见 ``backtest.feed`` §6-1）。
        exdiv_events: ⚠️ **本函数不处理除权** —— 除权必须在调用本函数**之前**
            由 ``Ledger.process_exdiv`` 完成（见模块 docstring 顺序约束）。
            这里只用它把除权标的一并计入"受影响"集合，便于上层记账/告警。

    Returns:
        受影响 symbol 集合。⛔ 与 ``BookView.settle`` 的返回口径不同（那个只含
        刷新的），需要区分请用 :func:`settle_day_detail`。
    """
    return set(settle_day_detail(book, date, bars, exdiv_events).affected)


def settle_day_detail(
    book: BookView,
    date: _date,
    bars: Mapping[str, Bar],
    exdiv_events: Mapping[str, Any] | None = None,
) -> SettleReport:
    """:func:`settle_day` 的明细版：区分"已刷新"与"停牌冻结"。"""
    book.date = date
    refreshed: set[str] = set()
    frozen: set[str] = set()
    for symbol, pos in book.positions.items():
        bar = bars.get(symbol)
        if bar is None:
            # 停牌：市值冻结不动（⛔ 不清零、不前向填充价格）。
            frozen.add(symbol)
            continue
        pos.last_close = bar.close
        pos.market_value = bar.close * Decimal(pos.volume)
        refreshed.add(symbol)
    nav = book.recompute_nav()
    if exdiv_events:
        # 除权标的即使已清仓 / 无持仓，也算"本日被过问"。
        for symbol in exdiv_events:
            if symbol in refreshed:
                continue
            if symbol in bars:
                refreshed.add(symbol)
            else:
                frozen.add(symbol)
    return SettleReport(
        date=date,
        refreshed=frozenset(refreshed),
        frozen=frozenset(frozen),
        nav=nav,
    )
