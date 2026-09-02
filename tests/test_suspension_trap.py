#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""停牌陷阱指标验收测试（suspension_trapped_days）。

场景覆盖：
  1. 无停牌 → trapped_days = 0（基线）
  2. 停牌但复牌正常（非跌停）→ 0（停牌≠陷阱，能卖即不算）
  3. 停牌 N 日 + 复牌跌停 → trapped_days = N（陷阱成立）
  4. 多标的独立停牌陷阱 → 累加
  5. 同标的多次停牌陷阱 → 累加
  6. 停牌后直接清仓卖出（非跌停）→ 0（有行情记录说明非跌停陷阱）

口径：停牌陷阱 = 持仓停牌 **且** 复牌当日跌停（无法卖出）的累计天数。
判定依据：SETTLE 流水 frozen → refreshed + 跌停拒单（reject_reason 含"跌停"）。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd

from backtest.broker import BacktestBroker
from backtest.constants import OrderSide, OrderStatus, OrderType
from backtest.engine import BacktestEngine
from backtest.feed import ParquetDailyFeed
from backtest.ledger import Ledger
from backtest.matching import MatchEngine
from backtest.metrics import compute_metrics
from backtest.types import Order

D = Decimal

# ---------------------------------------------------------------------- 造数工具

_COLS = (
    "date", "open", "high", "low", "close", "preclose",
    "volume", "amount", "turn", "pctChg",
    "tradestatus", "isST", "code", "adjust_mode", "source",
    "limit_up", "limit_down",
)


def _row(d: date, *, open_: str, close: str, preclose: str, code: str) -> dict:
    """一行日线记录（价格用 Decimal 算准，落帧转 float）。"""
    o, c, p = D(open_), D(close), D(preclose)
    pct_chg = (c - p) / p * D("100") if p != 0 else D("0")
    # 涨跌停判定：主板 ±10%，ST ±5%（这里简化为主板）
    limit_up = (pct_chg >= D("9.9"))
    limit_down = (pct_chg <= D("-9.9"))
    return {
        "date": d,
        "open": float(o),
        "high": float(max(o, c)),
        "low": float(min(o, c)),
        "close": float(c),
        "preclose": float(p),
        "volume": 1_000_000.0,
        "amount": float(c * D("1000000")),
        "turn": 1.0,
        "pctChg": float(pct_chg),
        "tradestatus": "1",
        "isST": "0",
        "code": code,
        "adjust_mode": "hfq",
        "source": "baostock",
        "limit_up": limit_up,
        "limit_down": limit_down,
    }


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(_COLS))


def _calendar_fn(dates: list[date]):
    """mock 交易日历。"""
    return lambda start, end: [d for d in dates if start <= d <= end]


def _make(
    preloaded: dict[str, pd.DataFrame],
    calendar: list[date],
    cash: Decimal = D("120000"),
) -> tuple[BacktestEngine, BacktestBroker]:
    """装一套 feed + ledger + broker + engine（全离线）。"""
    feed = ParquetDailyFeed(
        preloaded=preloaded,
        trade_calendar=_calendar_fn(calendar),
    )
    ledger = Ledger(cash, date=calendar[0])
    broker = BacktestBroker(MatchEngine(), ledger, feed)
    return BacktestEngine(broker, feed), broker


class ScriptStrategy:
    """脚本化策略（鸭子类型，⛔ 不继承基类）。"""

    def __init__(self, script: dict[date, list[tuple]]):
        """
        Args:
            script: ``{date: [(side, volume, order_id), ...]}``。
        """
        self.script = script
        self.watchlist = ["sh.600000"]

    def on_bar(self, day, bars, book, broker):
        actions = self.script.get(day, [])
        for side, volume, oid in actions:
            # Extract symbol from order_id suffix (e.g., "SELL-600000-1" → sh.600000)
            # or default to sh.600000 for simple IDs like "BUY-1", "SELL-1"
            if "600001" in oid:
                symbol = "sh.600001"
            else:
                symbol = "sh.600000"
            order = Order(
                client_order_id=oid,
                symbol=symbol,
                side=side,
                order_type=OrderType.MARKET,
                volume=volume,
                price=None,
                created_date=day,
            )
            broker.submit(order)


# ======================================================================
# 场景 1：无停牌（基线）
# ======================================================================

class TestNoSuspension:
    """无停牌 → trapped_days = 0（基线）。"""

    @staticmethod
    def _run():
        d1 = date(2024, 3, 1)
        d2 = date(2024, 3, 4)
        d3 = date(2024, 3, 5)
        calendar = [d1, d2, d3]
        frame = _frame([
            _row(d1, open_="10.00", close="10.00", preclose="10.00", code="sh.600000"),
            _row(d2, open_="10.00", close="10.50", preclose="10.00", code="sh.600000"),
            _row(d3, open_="10.50", close="11.00", preclose="10.50", code="sh.600000"),
        ])
        engine, broker = _make({"sh.600000": frame}, calendar)
        strategy = ScriptStrategy({d1: [(OrderSide.BUY, 100, "BUY-1")]})
        result = engine.run(strategy, d1, d3)
        return result, broker

    def test_trapped_days_zero(self):
        """无停牌 → trapped_days = 0。"""
        result, _ = self._run()
        report = compute_metrics(result, risk_free_annual=D("0.02"))
        assert report.suspension_trapped_days == 0, (
            f"无停牌应为 0，实际 {report.suspension_trapped_days}")


# ======================================================================
# 场景 2：停牌但复牌正常（非跌停）
# ======================================================================

class TestSuspensionButResumeNormal:
    """停牌 2 日，复牌正常（可卖）→ trapped_days = 0。"""

    @staticmethod
    def _run():
        d1 = date(2024, 3, 1)
        d2 = date(2024, 3, 4)
        d3 = date(2024, 3, 5)  # 停牌
        d4 = date(2024, 3, 6)  # 停牌
        d5 = date(2024, 3, 7)  # 复牌正常
        d6 = date(2024, 3, 8)
        calendar = [d1, d2, d3, d4, d5, d6]
        frame = _frame([
            _row(d1, open_="10.00", close="10.00", preclose="10.00", code="sh.600000"),
            _row(d2, open_="10.00", close="10.50", preclose="10.00", code="sh.600000"),
            # d3/d4 无行 = 停牌
            _row(d5, open_="10.50", close="11.00", preclose="10.50", code="sh.600000"),
            _row(d6, open_="11.00", close="11.50", preclose="11.00", code="sh.600000"),
        ])
        engine, broker = _make({"sh.600000": frame}, calendar)
        strategy = ScriptStrategy({
            d1: [(OrderSide.BUY, 100, "BUY-1")],
            d2: [(OrderSide.SELL, 100, "SELL-1")],  # d3 停牌被拒（停牌期间尝试卖出）
            d5: [(OrderSide.SELL, 100, "SELL-2")],  # d6 撮合成功（复牌正常非跌停）
        })
        result = engine.run(strategy, d1, d6)
        return result, broker

    def test_trapped_days_zero_when_resume_normal(self):
        """停牌但复牌正常 → trapped_days = 0。"""
        result, _ = self._run()
        # 验证停牌期间卖单被拒（停牌）
        sell1_order = [o for o in result.orders if o.client_order_id == "SELL-1"][0]
        assert sell1_order.status == OrderStatus.REJECTED
        assert "停牌" in sell1_order.reject_reason
        # 验证复牌后卖单成交（说明非跌停）
        sell2_order = [o for o in result.orders if o.client_order_id == "SELL-2"][0]
        assert sell2_order.status == OrderStatus.FILLED, (
            f"复牌正常应成交，实际 {sell2_order.status}")
        report = compute_metrics(result, risk_free_annual=D("0.02"))
        assert report.suspension_trapped_days == 0, (
            f"停牌但能卖 → trapped_days = 0，实际 {report.suspension_trapped_days}")


# ======================================================================
# 场景 3：停牌 N 日 + 复牌跌停（陷阱成立）
# ======================================================================

class TestSuspensionThenLimitDown:
    """停牌 3 日，复牌跌停（卖单被拒）→ trapped_days = 3。"""

    @staticmethod
    def _run():
        d1 = date(2024, 3, 1)
        d2 = date(2024, 3, 4)
        d3 = date(2024, 3, 5)  # 停牌
        d4 = date(2024, 3, 6)  # 停牌
        d5 = date(2024, 3, 7)  # 停牌
        d6 = date(2024, 3, 8)  # 复牌跌停
        d7 = date(2024, 3, 11)
        calendar = [d1, d2, d3, d4, d5, d6, d7]
        frame = _frame([
            _row(d1, open_="10.00", close="10.00", preclose="10.00", code="sh.600000"),
            _row(d2, open_="10.00", close="10.00", preclose="10.00", code="sh.600000"),
            # d3/d4/d5 无行 = 停牌 3 日
            # d6 复牌跌停：preclose=10.00, close=9.00 → 跌幅 -10%
            _row(d6, open_="9.00", close="9.00", preclose="10.00", code="sh.600000"),
            _row(d7, open_="9.00", close="9.50", preclose="9.00", code="sh.600000"),
        ])
        engine, broker = _make({"sh.600000": frame}, calendar)
        strategy = ScriptStrategy({
            d1: [(OrderSide.BUY, 100, "BUY-1")],
            d2: [(OrderSide.SELL, 100, "SELL-1")],  # d3 停牌被拒（停牌期间尝试卖出）
            d5: [(OrderSide.SELL, 100, "SELL-2")],  # d6 跌停撮合被拒（复牌跌停卖不掉）
        })
        result = engine.run(strategy, d1, d7)
        return result, broker

    def test_trapped_days_equals_suspension_days(self):
        """停牌 3 日 + 复牌跌停 → trapped_days = 3。"""
        result, _ = self._run()
        # 验证停牌期间卖单被拒（停牌）
        sell1_order = [o for o in result.orders if o.client_order_id == "SELL-1"][0]
        assert sell1_order.status == OrderStatus.REJECTED
        assert "停牌" in sell1_order.reject_reason
        # 验证复牌日跌停卖单被拒（d5下单→d6跌停撮合被拒）
        sell2_order = [o for o in result.orders if o.client_order_id == "SELL-2"][0]
        assert sell2_order.status == OrderStatus.REJECTED
        assert "跌停" in sell2_order.reject_reason
        report = compute_metrics(result, risk_free_annual=D("0.02"))
        assert report.suspension_trapped_days == 3, (
            f"停牌 3 日 + 复牌跌停 + 期间尝试卖出 → trapped_days = 3，实际 "
            f"{report.suspension_trapped_days}")


# ======================================================================
# 场景 4：多标的独立停牌陷阱（累加）
# ======================================================================

class TestMultipleSymbolsTrapped:
    """两标的各停牌 2 日 + 各自复牌跌停 → trapped_days = 2 + 2 = 4。"""

    @staticmethod
    def _run():
        d1 = date(2024, 3, 1)
        d2 = date(2024, 3, 4)
        d3 = date(2024, 3, 5)
        d4 = date(2024, 3, 6)
        d5 = date(2024, 3, 7)
        d6 = date(2024, 3, 8)
        calendar = [d1, d2, d3, d4, d5, d6]
        frame1 = _frame([
            _row(d1, open_="10.00", close="10.00", preclose="10.00", code="sh.600000"),
            _row(d2, open_="10.00", close="10.00", preclose="10.00", code="sh.600000"),
            # d3/d4 停牌 2 日
            _row(d5, open_="9.00", close="9.00", preclose="10.00", code="sh.600000"),  # 跌停
            _row(d6, open_="9.00", close="9.50", preclose="9.00", code="sh.600000"),
        ])
        frame2 = _frame([
            _row(d1, open_="20.00", close="20.00", preclose="20.00", code="sh.600001"),
            _row(d2, open_="20.00", close="20.00", preclose="20.00", code="sh.600001"),
            # d3/d4 停牌 2 日
            _row(d5, open_="18.00", close="18.00", preclose="20.00", code="sh.600001"),  # 跌停
            _row(d6, open_="18.00", close="18.50", preclose="18.00", code="sh.600001"),
        ])
        engine, broker = _make(
            {"sh.600000": frame1, "sh.600001": frame2},
            calendar,
            cash=D("240000"),
        )
        strategy = ScriptStrategy({
            d1: [
                (OrderSide.BUY, 100, "BUY-600000"),
                (OrderSide.BUY, 100, "BUY-600001"),
            ],
            d2: [
                (OrderSide.SELL, 100, "SELL-600000-1"),  # d3 停牌被拒
                (OrderSide.SELL, 100, "SELL-600001-1"),  # d3 停牌被拒
            ],
            d4: [
                (OrderSide.SELL, 100, "SELL-600000-2"),  # d5 跌停撮合被拒
                (OrderSide.SELL, 100, "SELL-600001-2"),  # d5 跌停撮合被拒
            ],
        })
        result = engine.run(strategy, d1, d6)
        return result, broker

    def test_trapped_days_accumulated_across_symbols(self):
        """两标的独立陷阱 → trapped_days = 2 + 2 = 4。"""
        result, _ = self._run()
        # 验证停牌期间卖单被拒（停牌）
        sell1 = [o for o in result.orders if o.client_order_id == "SELL-600000-1"][0]
        sell2 = [o for o in result.orders if o.client_order_id == "SELL-600001-1"][0]
        assert sell1.status == OrderStatus.REJECTED
        assert sell2.status == OrderStatus.REJECTED
        assert "停牌" in sell1.reject_reason
        assert "停牌" in sell2.reject_reason
        # 验证复牌日跌停卖单被拒（d4下单→d5跌停撮合被拒）
        sell3 = [o for o in result.orders if o.client_order_id == "SELL-600000-2"][0]
        sell4 = [o for o in result.orders if o.client_order_id == "SELL-600001-2"][0]
        assert sell3.status == OrderStatus.REJECTED
        assert sell4.status == OrderStatus.REJECTED
        assert "跌停" in sell3.reject_reason
        assert "跌停" in sell4.reject_reason
        report = compute_metrics(result, risk_free_annual=D("0.02"))
        assert report.suspension_trapped_days == 4, (
            f"两标的各停 2 日陷阱 → trapped_days = 4，实际 "
            f"{report.suspension_trapped_days}")


# ======================================================================
# 场景 5：同标的多次停牌陷阱（累加）
# ======================================================================

class TestSameSymbolMultipleTraps:
    """同标的两次停牌陷阱（1 日 + 2 日）→ trapped_days = 3。"""

    @staticmethod
    def _run():
        d1 = date(2024, 3, 1)
        d2 = date(2024, 3, 4)
        d3 = date(2024, 3, 5)  # 第一次停牌 1 日
        d4 = date(2024, 3, 6)  # 第一次复牌跌停
        d5 = date(2024, 3, 7)
        d6 = date(2024, 3, 8)  # 第二次停牌 2 日
        d7 = date(2024, 3, 11)  # 停牌第 2 日
        d8 = date(2024, 3, 12)  # 第二次复牌跌停
        d9 = date(2024, 3, 13)
        calendar = [d1, d2, d3, d4, d5, d6, d7, d8, d9]
        frame = _frame([
            _row(d1, open_="10.00", close="10.00", preclose="10.00", code="sh.600000"),
            _row(d2, open_="10.00", close="10.00", preclose="10.00", code="sh.600000"),
            # d3 停牌 1 日
            _row(d4, open_="9.00", close="9.00", preclose="10.00", code="sh.600000"),  # 第一次跌停
            _row(d5, open_="9.00", close="9.00", preclose="9.00", code="sh.600000"),
            # d6/d7 停牌 2 日
            _row(d8, open_="8.10", close="8.10", preclose="9.00", code="sh.600000"),  # 第二次跌停
            _row(d9, open_="8.10", close="8.50", preclose="8.10", code="sh.600000"),
        ])
        engine, broker = _make({"sh.600000": frame}, calendar)
        strategy = ScriptStrategy({
            d1: [(OrderSide.BUY, 100, "BUY-1")],
            d2: [(OrderSide.SELL, 100, "SELL-1")],  # d3 停牌被拒
            d5: [(OrderSide.SELL, 100, "SELL-2")],  # d6 停牌被拒，第二次陷阱 d7跌停撮合被拒
        })
        result = engine.run(strategy, d1, d9)
        return result, broker

    def test_trapped_days_accumulated_across_traps(self):
        """同标的两次陷阱（1 日 + 2 日）→ trapped_days = 3。"""
        result, _ = self._run()
        sell1 = [o for o in result.orders if o.client_order_id == "SELL-1"][0]
        sell2 = [o for o in result.orders if o.client_order_id == "SELL-2"][0]
        assert sell1.status == OrderStatus.REJECTED
        assert sell2.status == OrderStatus.REJECTED
        assert "停牌" in sell1.reject_reason
        assert "停牌" in sell2.reject_reason
        report = compute_metrics(result, risk_free_annual=D("0.02"))
        assert report.suspension_trapped_days == 3, (
            f"同标的两次陷阱（1+2 日）→ trapped_days = 3，实际 "
            f"{report.suspension_trapped_days}")


# ======================================================================
# 场景 6：停牌期间无卖单（无陷阱证据）
# ======================================================================

class TestSuspensionWithoutSellOrder:
    """停牌 2 日但期间无卖单尝试 → trapped_days = 0（无证据不计）。"""

    @staticmethod
    def _run():
        d1 = date(2024, 3, 1)
        d2 = date(2024, 3, 4)
        d3 = date(2024, 3, 5)  # 停牌
        d4 = date(2024, 3, 6)  # 停牌
        d5 = date(2024, 3, 7)  # 复牌
        calendar = [d1, d2, d3, d4, d5]
        frame = _frame([
            _row(d1, open_="10.00", close="10.00", preclose="10.00", code="sh.600000"),
            _row(d2, open_="10.00", close="10.00", preclose="10.00", code="sh.600000"),
            # d3/d4 停牌 2 日
            _row(d5, open_="10.00", close="10.00", preclose="10.00", code="sh.600000"),
        ])
        engine, broker = _make({"sh.600000": frame}, calendar)
        strategy = ScriptStrategy({
            d1: [(OrderSide.BUY, 100, "BUY-1")],
            # ⛔ 停牌期间无卖单
        })
        result = engine.run(strategy, d1, d5)
        return result, broker

    def test_no_trapped_days_without_sell_attempt(self):
        """停牌但无卖单 → trapped_days = 0（无跌停证据）。"""
        result, _ = self._run()
        report = compute_metrics(result, risk_free_annual=D("0.02"))
        assert report.suspension_trapped_days == 0, (
            f"停牌但无卖单 → trapped_days = 0，实际 "
            f"{report.suspension_trapped_days}")
