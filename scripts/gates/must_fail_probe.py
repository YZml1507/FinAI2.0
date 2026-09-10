#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""E-1 五必挂极限用例探针（Must-Fail Probe）。

在**真实回测路径**里真跑一遍 5 条撮合极限用例，把逐用例结果交给
``MustFailCasesGate``（E-1）——取代旧 runner 里"无证据即预设 5 个用例全通过"
的硬编码兜底（审计 §6.2 / roadmap §4 配套治理）。

用例锚点（与 ``tests/test_t202_must_fail.py`` 同源，⛔ 只驱动引擎公开接口）：

  1. ``LIMIT_UP_BUY_REJECT``     一字涨停买单必须在撮合日被拒（rule#2）
  2. ``LIMIT_DOWN_SELL_REJECT``  一字跌停卖单必须被拒（rule#3）
  3. ``SUSPENSION_REJECT``       停牌日（bar 键缺席）委托必须被拒（rule#1）+ 净值冻结
  4. ``EXDIV_CONTINUOUS_NAV``    除权日（10 送 10 + 派现）后持仓翻倍、净值无跳变
  5. ``T1_SAME_DAY_SELL_REJECT`` 同一撮合日买卖同时到场 ⇒ 卖单撞 T+1 可卖不足被拒

返回 ``{用例名: bool}``（True = 引擎行为符合预期）。任一异常 ⇒ 该用例记 False。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Callable

import pandas as pd

from backtest.broker import BacktestBroker
from backtest.constants import OrderSide, OrderStatus, OrderType
from backtest.engine import BacktestEngine
from backtest.feed import ParquetDailyFeed
from backtest.ledger import Ledger
from backtest.matching import (
    REJECT_LIMIT_DOWN_SELL,
    REJECT_LIMIT_UP_BUY,
    REJECT_SUSPENDED,
    REJECT_T1_INSUFFICIENT,
    MatchEngine,
)
from backtest.settle import ExdivEvent
from backtest.types import Order

D = Decimal
SYMBOL = "sh.600000"
INITIAL_CASH = D("120000")

_D1 = date(2024, 3, 1)
_D2 = date(2024, 3, 4)
_D3 = date(2024, 3, 5)
_D4 = date(2024, 3, 6)
_CALENDAR = [_D1, _D2, _D3, _D4]

_COLS = (
    "date", "open", "high", "low", "close", "preclose",
    "volume", "amount", "turn", "pctChg",
    "tradestatus", "isST", "code", "adjust_mode", "source",
)


def _row(d: date, *, open_: str, close: str, preclose: str) -> dict[str, Any]:
    o, c, p = D(open_), D(close), D(preclose)
    return {
        "date": d, "open": float(o), "high": float(max(o, c)), "low": float(min(o, c)),
        "close": float(c), "preclose": float(p),
        "volume": 1_000_000.0, "amount": float(c * D("1000000")), "turn": 1.0,
        "pctChg": float((c - p) / p * D("100")),
        "tradestatus": "1", "isST": "0", "code": SYMBOL,
        "adjust_mode": "hfq", "source": "baostock",
    }


def _frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(_COLS))


class _ScriptStrategy:
    """脚本化策略（鸭子类型）；``script = {下单日: [(side, volume, oid), ...]}``。"""

    def __init__(self, script: dict[date, list[tuple]], *, exdiv: dict[date, dict] | None = None) -> None:
        self.script = script
        self.exdiv = exdiv or {}
        self.watchlist = [SYMBOL]

    def on_bar(self, day, bars, book, broker) -> None:
        for side, volume, oid in self.script.get(day, []):
            broker.submit(Order(
                client_order_id=oid, symbol=SYMBOL, side=side,
                order_type=OrderType.MARKET, volume=volume, price=None, created_date=day,
            ))

    def exdiv_events_for(self, day):
        return self.exdiv.get(day)


def _make(frame: pd.DataFrame, *, calendar: list[date] | None = None):
    days = calendar or _CALENDAR
    feed = ParquetDailyFeed(
        preloaded={SYMBOL: frame},
        trade_calendar=lambda s, e: [d for d in days if s <= d <= e],
    )
    ledger = Ledger(INITIAL_CASH, date=days[0])
    broker = BacktestBroker(MatchEngine(), ledger, feed)
    return BacktestEngine(broker, feed), broker


def _order(result, oid: str):
    for o in result.orders:
        if o.client_order_id == oid:
            return o
    return None


# --------------------------------------------------------------------------- 用例

def _case_limit_up_buy() -> bool:
    frame = _frame([
        _row(_D1, open_="10.00", close="10.00", preclose="10.00"),
        _row(_D2, open_="11.00", close="11.00", preclose="10.00"),   # 一字涨停 +10%
        _row(_D3, open_="11.00", close="11.00", preclose="11.00"),
        _row(_D4, open_="11.00", close="11.00", preclose="11.00"),
    ])
    engine, _ = _make(frame)
    result = engine.run(_ScriptStrategy({_D1: [(OrderSide.BUY, 100, "P1-BUY")]}), _D1, _D4)
    order = _order(result, "P1-BUY")
    return order is not None and order.status is OrderStatus.REJECTED and order.reject_reason == REJECT_LIMIT_UP_BUY


def _case_limit_down_sell() -> bool:
    frame = _frame([
        _row(_D1, open_="10.00", close="10.00", preclose="10.00"),
        _row(_D2, open_="10.00", close="10.00", preclose="10.00"),
        _row(_D3, open_="9.00", close="9.00", preclose="10.00"),     # 一字跌停 -10%
        _row(_D4, open_="9.00", close="9.00", preclose="9.00"),
    ])
    engine, _ = _make(frame)
    result = engine.run(_ScriptStrategy({
        _D1: [(OrderSide.BUY, 100, "P2-BUY")],
        _D2: [(OrderSide.SELL, 100, "P2-SELL")],
    }), _D1, _D4)
    buy, sell = _order(result, "P2-BUY"), _order(result, "P2-SELL")
    return (
        buy is not None and sell is not None
        and buy.status is OrderStatus.FILLED
        and sell.status is OrderStatus.REJECTED
        and sell.reject_reason == REJECT_LIMIT_DOWN_SELL
    )


def _case_suspension() -> bool:
    frame = _frame([
        _row(_D1, open_="10.00", close="10.00", preclose="10.00"),
        _row(_D2, open_="10.00", close="10.50", preclose="10.00"),
        # ⛔ D3 无行 = 停牌
        _row(_D4, open_="11.00", close="11.00", preclose="10.50"),
    ])
    engine, _ = _make(frame)
    result = engine.run(_ScriptStrategy({
        _D1: [(OrderSide.BUY, 100, "P3-BUY")],
        _D2: [(OrderSide.SELL, 100, "P3-SELL")],
    }), _D1, _D4)
    sell = _order(result, "P3-SELL")
    if not (sell is not None and sell.status is OrderStatus.REJECTED and sell.reject_reason == REJECT_SUSPENDED):
        return False
    try:
        return result.nav_at(_D3) == result.nav_at(_D2)     # 停牌日净值冻结
    except Exception:                       # noqa: BLE001
        return False


def _case_exdiv_continuous() -> bool:
    frame = _frame([
        _row(_D1, open_="10.00", close="10.00", preclose="10.00"),
        _row(_D2, open_="10.00", close="10.00", preclose="10.00"),
        _row(_D3, open_="4.75", close="4.75", preclose="4.75"),
    ])
    engine, broker = _make(frame, calendar=[_D1, _D2, _D3])
    strategy = _ScriptStrategy(
        {_D1: [(OrderSide.BUY, 100, "P4-BUY")]},
        exdiv={_D3: {SYMBOL: ExdivEvent(symbol=SYMBOL, factor=D("2"), cash_dividend=D("0.5"), date=_D3)}},
    )
    result = engine.run(strategy, _D1, _D3)
    pos = broker.book.positions.get(SYMBOL)
    if pos is None or pos.volume != 200:
        return False
    try:
        return abs(result.nav_at(_D3) - result.nav_at(_D2)) <= D("0.5")   # 净值无跳变
    except Exception:                       # noqa: BLE001
        return False


def _case_t1_same_day() -> bool:
    frame = _frame([
        _row(_D1, open_="10.00", close="10.00", preclose="10.00"),
        _row(_D2, open_="10.00", close="10.20", preclose="10.00"),
        _row(_D3, open_="10.30", close="10.40", preclose="10.20"),
        _row(_D4, open_="10.40", close="10.50", preclose="10.40"),
    ])
    engine, _ = _make(frame)
    result = engine.run(_ScriptStrategy({_D1: [
        (OrderSide.BUY, 100, "P5-BUY"),
        (OrderSide.SELL, 100, "P5-SELL"),
    ]}), _D1, _D4)
    buy, sell = _order(result, "P5-BUY"), _order(result, "P5-SELL")
    return (
        buy is not None and sell is not None
        and buy.status is OrderStatus.FILLED
        and sell.status is OrderStatus.REJECTED
        and sell.reject_reason == REJECT_T1_INSUFFICIENT
    )


_CASES: tuple[tuple[str, Callable[[], bool]], ...] = (
    ("LIMIT_UP_BUY_REJECT", _case_limit_up_buy),
    ("LIMIT_DOWN_SELL_REJECT", _case_limit_down_sell),
    ("SUSPENSION_REJECT", _case_suspension),
    ("EXDIV_CONTINUOUS_NAV", _case_exdiv_continuous),
    ("T1_SAME_DAY_SELL_REJECT", _case_t1_same_day),
)


def run_split_fifo_probe() -> tuple[list[str], dict[str, int]]:
    """E-2 探针：真实走一遍"10 送 10 后全额卖出"，返回 ``(fifo_errors, final_positions)``。

    ⛔ 取代"无送转证据即 INCONCLUSIVE"：用被检引擎真跑送转→全额卖出，取证 FIFO 是否缺股崩溃。
    """
    frame = _frame([
        _row(_D1, open_="10.00", close="10.00", preclose="10.00"),
        _row(_D2, open_="10.00", close="10.00", preclose="10.00"),
        _row(_D3, open_="4.75", close="4.75", preclose="4.75"),
        _row(_D4, open_="4.75", close="4.75", preclose="4.75"),
    ])
    engine, broker = _make(frame)
    strategy = _ScriptStrategy(
        {_D1: [(OrderSide.BUY, 100, "SPL-BUY")], _D3: [(OrderSide.SELL, 200, "SPL-SELL")]},
        exdiv={_D3: {SYMBOL: ExdivEvent(symbol=SYMBOL, factor=D("2"), cash_dividend=D("0.5"), date=_D3)}},
    )
    try:
        engine.run(strategy, _D1, _D4)
    except Exception as exc:                # noqa: BLE001 —— 缺股击穿等按 FIFO 错误记录
        return ([f"{type(exc).__name__}: {exc}"], {})
    pos = broker.book.positions.get(SYMBOL)
    final = {SYMBOL: int(pos.volume) if pos is not None else 0}
    return ([], final)


def run_must_fail_cases() -> dict[str, bool]:
    """真跑 5 必挂用例，返回 ``{用例名: 是否符合预期}``（异常 ⇒ False）。"""
    results: dict[str, bool] = {}
    for name, fn in _CASES:
        try:
            results[name] = bool(fn())
        except Exception:                   # noqa: BLE001 —— 探针异常按"未通过"记
            results[name] = False
    return results


if __name__ == "__main__":
    import json
    import sys

    outcome = run_must_fail_cases()
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    sys.exit(0 if all(outcome.values()) else 1)
