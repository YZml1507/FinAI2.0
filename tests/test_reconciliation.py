#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T403 对账模块测试（paper_trading/reconciliation.py）。

覆盖范围：
  - 账本自对账（持仓市值 + 现金 = NAV）
  - 不变式检查（现金非负 / 冻结资金合法 / 持仓非负）
  - 差异容差校验（>0.01 元 raise）
  - NAV 单调性检查（异常下跌告警）
  - 停牌市值冻结处理
"""
from datetime import date as dt
from decimal import Decimal

import pytest

from backtest.constants import OrderSide
from backtest.ledger import BookView, Ledger, Position
from backtest.types import Bar
from paper_trading.reconciliation import (
    ReconciliationError,
    reconcile_account,
)

_ZERO = Decimal("0")


# ===== Fixtures =====
@pytest.fixture
def mock_book():
    """构造测试用 BookView。"""
    ledger = Ledger(initial_cash=Decimal("100000"), date=dt(2026, 9, 2))
    return ledger.book  # .book 是 BookView 实例（非方法）


@pytest.fixture
def mock_bars():
    """构造测试用行情字典。"""
    return {
        "sz.000001": Bar(
            symbol="sz.000001",
            date=dt(2026, 9, 2),
            open=Decimal("10.0"),
            close=Decimal("10.5"),
            high=Decimal("11.0"),
            low=Decimal("10.0"),
            preclose=Decimal("10.0"),
            volume=Decimal("1000000"),
            amount=Decimal("10500000"),
            limit_up=False,
            limit_down=False,
        ),
        "sz.000002": Bar(
            symbol="sz.000002",
            date=dt(2026, 9, 2),
            open=Decimal("20.0"),
            close=Decimal("21.0"),
            high=Decimal("22.0"),
            low=Decimal("20.0"),
            preclose=Decimal("20.0"),
            volume=Decimal("500000"),
            amount=Decimal("10500000"),
            limit_up=False,
            limit_down=False,
        ),
    }


# ===== 测试用例 =====
class TestReconciliationBasic:
    """基础对账逻辑测试。"""

    def test_empty_portfolio_pass(self, mock_book, mock_bars):
        """空持仓：现金 = NAV，对账通过。"""
        report = reconcile_account(mock_book, dt(2026, 9, 2), mock_bars)
        assert report.ok
        assert report.nav == Decimal("100000")
        assert report.positions_market_value == _ZERO
        assert report.checks["nav_match"]

    def test_single_position_pass(self, mock_book, mock_bars):
        """单持仓：市值 + 现金 = NAV，对账通过。"""
        # 模拟买入 1000 股 sz.000001
        mock_book.positions["sz.000001"] = Position(
            symbol="sz.000001",
            volume=1000,
            sellable=0,
            avg_cost=Decimal("10.0"),
            last_close=Decimal("10.5"),
            market_value=Decimal("10500"),  # 1000 × 10.5
        )
        mock_book.cash = Decimal("89500")
        mock_book.nav = Decimal("100000")

        report = reconcile_account(mock_book, dt(2026, 9, 2), mock_bars)
        assert report.ok
        assert report.positions_market_value == Decimal("10500")
        assert report.nav == Decimal("100000")

    def test_multiple_positions_pass(self, mock_book, mock_bars):
        """多持仓：全部市值 + 现金 = NAV，对账通过。"""
        mock_book.positions["sz.000001"] = Position(
            symbol="sz.000001", volume=1000, avg_cost=Decimal("10.0"),
            sellable=0, last_close=Decimal("10.5"),
            market_value=Decimal("10500"))
        mock_book.positions["sz.000002"] = Position(
            symbol="sz.000002", volume=500, avg_cost=Decimal("20.0"),
            sellable=0, last_close=Decimal("21.0"),
            market_value=Decimal("10500"))
        mock_book.cash = Decimal("79000")
        mock_book.nav = Decimal("100000")

        report = reconcile_account(mock_book, dt(2026, 9, 2), mock_bars)
        assert report.ok
        assert report.positions_market_value == Decimal("21000")
        assert report.nav == Decimal("100000")


class TestReconciliationInvariantViolations:
    """不变式违反测试（致命错误，必须 raise）。"""

    def test_negative_cash_raise(self, mock_book, mock_bars):
        """现金为负 → ReconciliationError（爆仓检测）。"""
        mock_book.cash = Decimal("-100")
        with pytest.raises(ReconciliationError, match="现金为负"):
            reconcile_account(mock_book, dt(2026, 9, 2), mock_bars)

    def test_negative_frozen_cash_raise(self, mock_book, mock_bars):
        """冻结资金为负 → ReconciliationError。"""
        mock_book.frozen_cash = Decimal("-100")
        with pytest.raises(ReconciliationError, match="冻结资金为负"):
            reconcile_account(mock_book, dt(2026, 9, 2), mock_bars)

    def test_frozen_exceeds_cash_raise(self, mock_book, mock_bars):
        """冻结资金 > 现金 → ReconciliationError。"""
        mock_book.cash = Decimal("10000")
        mock_book.frozen_cash = Decimal("10001")
        with pytest.raises(ReconciliationError, match="冻结资金.*>.*现金"):
            reconcile_account(mock_book, dt(2026, 9, 2), mock_bars)

    def test_negative_position_raise(self, mock_book, mock_bars):
        """持仓为负 → ReconciliationError（v1 不支持空头）。"""
        mock_book.positions["sz.000001"] = Position(
            symbol="sz.000001", volume=-100,  # 负持仓
            avg_cost=Decimal("10.0"), sellable=0,
            last_close=Decimal("10.0"), market_value=Decimal("-1000"))
        with pytest.raises(ReconciliationError, match="持仓为负"):
            reconcile_account(mock_book, dt(2026, 9, 2), mock_bars)


class TestReconciliationDiscrepancies:
    """差异超容差测试（>0.01 元 raise）。"""

    def test_nav_mismatch_over_tolerance_raise(self, mock_book, mock_bars):
        """NAV 差异 > 1 分 → ReconciliationError。"""
        mock_book.positions["sz.000001"] = Position(
            symbol="sz.000001", volume=1000, avg_cost=Decimal("10.0"),
            sellable=0, last_close=Decimal("10.5"),
            market_value=Decimal("10500"))
        mock_book.cash = Decimal("89500")
        mock_book.nav = Decimal("100000.02")  # 差异 0.02 元 > 容差 0.01

        with pytest.raises(ReconciliationError, match="NAV 差异.*超容差"):
            reconcile_account(mock_book, dt(2026, 9, 2), mock_bars)

    def test_nav_mismatch_within_tolerance_pass(self, mock_book, mock_bars):
        """NAV 差异 <= 1 分 → 通过（容差保护）。"""
        mock_book.positions["sz.000001"] = Position(
            symbol="sz.000001", volume=1000, avg_cost=Decimal("10.0"),
            sellable=0, last_close=Decimal("10.5"),
            market_value=Decimal("10500"))
        mock_book.cash = Decimal("89500")
        mock_book.nav = Decimal("100000.01")  # 差异 0.01 元 = 容差边界

        report = reconcile_account(mock_book, dt(2026, 9, 2), mock_bars)
        assert report.ok
        assert report.checks["nav_match"]


class TestReconciliationSuspension:
    """停牌处理测试（市值冻结 + 告警）。"""

    def test_suspended_stock_use_last_close(self, mock_book):
        """停牌标的：使用账本 last_close 计算市值 + 发告警。"""
        mock_book.positions["sz.000001"] = Position(
            symbol="sz.000001", volume=1000, avg_cost=Decimal("10.0"),
            sellable=0, last_close=Decimal("10.5"),
            market_value=Decimal("10500"))
        mock_book.cash = Decimal("89500")
        mock_book.nav = Decimal("100000")

        # bars 中无 sz.000001（停牌）
        bars = {}

        report = reconcile_account(mock_book, dt(2026, 9, 2), bars)
        assert report.ok
        assert report.positions_market_value == Decimal("10500")
        assert any("停牌" in w for w in report.warnings)


class TestReconciliationNAVMonotonicity:
    """NAV 单调性检查测试（异常下跌告警）。"""

    def test_nav_normal_decline_no_warning(self, mock_book, mock_bars):
        """NAV 正常下跌 (<5%) → 无告警。"""
        mock_book.cash = Decimal("96000")
        mock_book.nav = Decimal("96000")
        prev_nav = Decimal("100000")

        report = reconcile_account(
            mock_book, dt(2026, 9, 2), mock_bars, prev_nav=prev_nav)
        assert report.ok
        assert report.nav_change_pct == Decimal("-0.04")  # -4%
        assert not report.warnings

    def test_nav_abnormal_decline_warning(self, mock_book, mock_bars):
        """NAV 异常下跌 (>5%) → 告警但不 raise。"""
        mock_book.cash = Decimal("94000")
        mock_book.nav = Decimal("94000")
        prev_nav = Decimal("100000")

        report = reconcile_account(
            mock_book, dt(2026, 9, 2), mock_bars, prev_nav=prev_nav)
        assert report.ok  # 告警不影响 ok 状态
        assert report.nav_change_pct == Decimal("-0.06")  # -6%
        assert any("NAV 下跌" in w and "超阈值" in w for w in report.warnings)

    def test_first_day_no_prev_nav(self, mock_book, mock_bars):
        """首日（无 prev_nav）→ 不检查单调性。"""
        report = reconcile_account(
            mock_book, dt(2026, 9, 2), mock_bars, prev_nav=None)
        assert report.ok
        assert report.nav_change_pct is None
