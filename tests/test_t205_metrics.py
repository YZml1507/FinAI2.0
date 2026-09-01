#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T205 绩效指标单测（离线，⛔ 无网络 / 无磁盘依赖）—— FR-REP-1 口径断言。

黄金数字全部手算复核（见各用例注释）。核心手工序列：

  NAV: [100, 120, 90, 110, 125, 100]（1970-01-01 ~ 1970-01-06，6 天）
  · 总收益 = 0；日收益 = [.2, -.25, .2222, .1364, -.2]
  · MDD = (120-90)/120 = 0.25，peak=01-02, trough=01-03, recovery=01-05(110<120? 不) → None
    ... 再核：peak_nav=120；trough 后 nav ≥120 的第一个日期：01-05 nav=125 ≥120 ✓ → recovery=01-05
  · CAGR（5 天历期）= (1+0)^(365.25/5) − 1 = 0

胜率 FIFO：BUY 100@10（费 5）→ SELL 100@11（费 5.05...口径内用手工值）配对盈利 1 次 ⇒ win_rate=1。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtest.constants import FeeItem, OrderSide
from backtest.metrics import MetricsError, compute_metrics
from backtest.types import Trade

D = Decimal
D0 = Decimal("0")


class _Result:
    """最小 BacktestResult 鸭子（只喂 metrics 需要的三字段）。"""

    def __init__(self, nav_curve, trades=(), final_nav=None):
        self.nav_curve = nav_curve
        self.trades = list(trades)
        self.final_nav = final_nav


def _nav(values, start: date = date(1970, 1, 1)) -> dict[str, Decimal]:
    return {
        (start + timedelta(days=i)).isoformat(): D(str(v))
        for i, v in enumerate(values)
    }


_GOLD = [100, 120, 90, 110, 125, 100]     # 见模块 docstring 手算


def _trade(symbol, side, volume, price, d, oid, fees=None) -> Trade:
    fees = fees or {FeeItem.COMMISSION: D("5"), FeeItem.STAMP_TAX: D0,
                    FeeItem.TRANSFER_FEE: D("0.10"), FeeItem.HANDLING_FEE: D("0.34"),
                    FeeItem.MANAGEMENT_FEE: D("0.20"), FeeItem.SLIPPAGE: D0}
    return Trade(trade_id=f"{oid}:{d.isoformat()}", client_order_id=oid,
                 symbol=symbol, side=side, volume=volume, price=D(str(price)),
                 date=d, fees=fees, sellable_date=None)


# ======================================================================
# A. 收益
# ======================================================================

class TestReturns:
    def test_total_return_and_cagr_flat(self) -> None:
        r = compute_metrics(_Result(_nav(_GOLD)), risk_free_annual=D("0.02"))
        assert r.total_return == D0                        # 100 → 100
        assert r.cagr == D0                                # (1+0)^x − 1 = 0
        assert r.trading_days == 6 and r.calendar_days == 5

    def test_total_return_and_cagr_up(self) -> None:
        # 100 → 110，恰 365 天跨度（2023-01-01 → 2024-01-01）：total=0.1，
        # CAGR = (1.1)^(365.25/365) − 1 = 0.100072
        nav = {date(2023, 1, 1).isoformat(): D("100"),
               date(2024, 1, 1).isoformat(): D("110")}
        r = compute_metrics(_Result(nav), risk_free_annual=D("0.02"))
        assert r.total_return == D("0.1")
        assert r.cagr == D(str(round((1.1 ** (365.25 / 365) - 1), 6)))


# ======================================================================
# B. 风险（波动 + 回撤）
# ======================================================================

class TestRisk:
    def test_annual_volatility_handcheck(self) -> None:
        # 日收益 [.2, -.25, .2222222, .1363636, -.2]，ddof=1
        import math
        xs = [0.2, -0.25, 90 / 120 * 100 / 90 / 100 - 1, 110 / 90 - 1, 100 / 125 - 1]
        xs = [1.2 - 1, 0.75 - 1, (90 / 120) * (110 / 90) * (1 / 1) - 1, 110 / 90 - 1, 0.8 - 1]
        # 直接用净值打点：90/120=0.75, 110/90≈1.2222, 125/110≈1.1364, 100/125=0.8
        xs = [0.2, -0.25, 110 / 90 - 1, 125 / 110 - 1, 100 / 125 - 1]
        mean = sum(xs) / len(xs)
        var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
        expected = Decimal(str(round(math.sqrt(var) * math.sqrt(252), 6)))
        r = compute_metrics(_Result(_nav(_GOLD)), risk_free_annual=D("0.02"))
        assert r.annual_volatility == expected

    def test_max_drawdown_with_dates(self) -> None:
        r = compute_metrics(_Result(_nav(_GOLD)), risk_free_annual=D("0.02"))
        assert r.max_drawdown == D("0.25")               # (120−90)/120
        assert r.max_dd_peak == date(1970, 1, 2)
        assert r.max_dd_trough == date(1970, 1, 3)
        assert r.max_dd_recovery == date(1970, 1, 5)     # 125 ≥ 120
        # 永不恢复 ⇒ recovery = None
        r2 = compute_metrics(_Result(_nav([100, 120, 90, 100, 110])),
                             risk_free_annual=D("0.02"))
        assert r2.max_dd_recovery is None
        # 单峰单调不降 ⇒ MDD = 0、日期 None
        r3 = compute_metrics(_Result(_nav([100, 110, 120])),
                             risk_free_annual=D("0.02"))
        assert r3.max_drawdown == D0 and r3.max_dd_peak is None

    def test_volatility_none_when_single_point(self) -> None:
        r = compute_metrics(_Result(_nav([100])), risk_free_annual=D("0.02"))
        assert r.annual_volatility is None and r.sharpe_ratio is None


# ======================================================================
# C. 夏普（R_f 显式声明）
# ======================================================================

class TestSharpe:
    def test_rf_mandatory(self) -> None:
        with pytest.raises(MetricsError):
            compute_metrics(_Result(_nav(_GOLD)))                # 缺参
        with pytest.raises(MetricsError):
            compute_metrics(_Result(_nav(_GOLD)), risk_free_annual=0.02)  # float

    def test_sharpe_handcheck(self) -> None:
        import math
        xs = [0.2, -0.25, 110 / 90 - 1, 125 / 110 - 1, 100 / 125 - 1]
        mean = sum(xs) / len(xs)
        var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
        std = math.sqrt(var)
        rf_daily = 0.02 / 252
        expected = Decimal(str(round((mean - rf_daily) / std * math.sqrt(252), 6)))
        r = compute_metrics(_Result(_nav(_GOLD)), risk_free_annual=D("0.02"))
        assert r.sharpe_ratio == expected and r.risk_free_annual == D("0.02")

    def test_calmar_ratio(self) -> None:
        # Calmar = CAGR / MDD（03 号建议项）；MDD=0 ⇒ None（⛔ 不产 inf）
        r = compute_metrics(_Result(_nav(_GOLD)), risk_free_annual=D("0.02"))
        assert r.max_drawdown == D("0.25") and r.cagr == D0       # 5 日归零 ⇒ Calmar 0
        assert r.calmar_ratio == D("0.000000")
        up = compute_metrics(_Result(_nav([100, 110, 90, 130])), risk_free_annual=D("0.02"))
        assert up.max_drawdown > 0 and up.cagr > 0
        assert up.calmar_ratio is not None
        flat_up = compute_metrics(_Result(_nav([100, 110, 120])), risk_free_annual=D("0.02"))
        assert flat_up.max_drawdown == D0 and flat_up.calmar_ratio is None


# ======================================================================
# D. 换手 / 费用 / 胜率
# ======================================================================

class TestActivityAndCosts:
    def test_turnover_annualized(self) -> None:
        # 买 10,000 + 卖 10,000 → 单边 10,000；平均 NAV=100、跨 365 天 ⇒ 100 倍/年
        d0, d1 = date(2023, 1, 1), date(2024, 1, 1)
        trades = [
            _trade("sh.x", OrderSide.BUY, 1000, "10", d0, "b1"),
            _trade("sh.x", OrderSide.SELL, 1000, "10", d1, "s1"),
        ]
        nav = {d0.isoformat(): D("100"), d1.isoformat(): D("100")}
        r = compute_metrics(_Result(nav, trades), risk_free_annual=D("0.02"))
        assert r.annual_turnover == D(str(round(10000 / 100 / (365 / 365.25), 6)))

    def test_fees_sum_by_item(self) -> None:
        t1 = _trade("a", OrderSide.BUY, 100, "10", date(2024, 1, 2), "b1")
        t2 = _trade("a", OrderSide.SELL, 100, "11", date(2024, 1, 3), "s1")
        r = compute_metrics(_Result(_nav([100, 100, 100]), [t1, t2]),
                            risk_free_annual=D("0.02"))
        assert set(r.fees_total.keys()) == set(FeeItem)           # 六键齐备
        assert r.fees_total[FeeItem.COMMISSION] == D("10")        # 5 + 5
        assert r.fees_sum == sum(r.fees_total.values(), D0)

    def test_win_rate_fifo_one_win_one_loss(self) -> None:
        d = date(2024, 1, 2)
        b1 = _trade("a", OrderSide.BUY, 100, "10", d, "b1")       # 成本 1000+5.64
        b2 = _trade("b", OrderSide.BUY, 100, "10", d, "b2")
        s1 = _trade("a", OrderSide.SELL, 100, "12", d, "s1")      # 赚 ⇒ win
        s2 = _trade("b", OrderSide.SELL, 100, "8", d, "s2")       # 亏 ⇒ loss
        r = compute_metrics(_Result(_nav([100, 100]), [b1, b2, s1, s2]),
                            risk_free_annual=D("0.02"))
        assert r.round_trips == 2 and r.win_rate == D("0.5")

    def test_win_rate_none_without_roundtrip(self) -> None:
        b1 = _trade("a", OrderSide.BUY, 100, "10", date(2024, 1, 2), "b1")
        r = compute_metrics(_Result(_nav([100, 100]), [b1]), risk_free_annual=D("0.02"))
        assert r.win_rate is None and r.round_trips == 0


# ======================================================================
# E. 月度热力图 + fail-closed
# ======================================================================

class TestMonthlyAndGuard:
    def test_monthly_matrix(self) -> None:
        # 1/31=100, 2/27=110, 3/31=99：1 月=(100/100)−1=0；2 月=110/100−1=0.1；3 月=99/110−1=−0.1
        nav = {"2024-01-31": D("100"), "2024-02-27": D("110"), "2024-03-31": D("99")}
        r = compute_metrics(_Result(nav), risk_free_annual=D("0.02"))
        assert r.monthly_returns[(2024, 1)] == D0
        assert r.monthly_returns[(2024, 2)] == D("0.1")
        assert r.monthly_returns[(2024, 3)] == D("-0.1")

    def test_fail_closed(self) -> None:
        with pytest.raises(MetricsError):
            compute_metrics(_Result({}), risk_free_annual=D("0.02"))          # 空曲线
        with pytest.raises(MetricsError):
            compute_metrics(_Result({"not-a-date": D("100")}),
                            risk_free_annual=D("0.02"))                        # 脏键
        with pytest.raises(MetricsError):
            compute_metrics(_Result({"2024-01-01": D("-5")}),
                            risk_free_annual=D("0.02"))                        # 净值 ≤0
        with pytest.raises(MetricsError):
            compute_metrics(_Result({"2024-01-01": 100.0}),
                            risk_free_annual=D("0.02"))                        # float 净值
        with pytest.raises(MetricsError):
            compute_metrics(_Result(_nav([100, 120]), final_nav=D("999")),
                            risk_free_annual=D("0.02"))                        # 对账矛盾
