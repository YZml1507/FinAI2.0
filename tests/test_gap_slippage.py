#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Gap slippage stress test — 隔夜跳空缺口额外滑点压测（离线，⛔ 无网络 / 无磁盘依赖）。

**背景**：隔夜跳空 ±3% / ±5% 时，真实成交价会因流动性枯竭、挂单稀疏而额外偏离开盘价。
``make_price_model`` 新增 ``gap_slippage_pct`` 参数，在检测到缺口超阈值时额外叠加滑点
（**叠加**基础 5bps，不是替换）。

**测试策略**：
  1. 无缺口滑点（``gap_slippage_pct=None``）⇒ 仅基础 5bps，向后兼容。
  2. 缺口 < 阈值（如 2.5% < 3%）⇒ 不触发，仍 5bps。
  3. 缺口 = 3% / 5%，``gap_slippage_pct=10bps`` ⇒ 总滑点 ≈ 5bps + 10bps（复利叠加）。
  4. 方向性：BUY 时跳空↑加剧不利（更贵）、跳空↓也保守加滑点；SELL 反之。
  5. tick 取整 + 涨跌停限幅红线不破。

手算锚点（见用例注释）：preclose=10.00，open 分别 10.30（+3%）/ 10.50（+5%）。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from backtest.constants import OrderSide, OrderType
from backtest.fees import (
    FeeError,
    TICK_SIZE,
    default_fee_config,
    make_price_model,
)
from backtest.types import Bar, Order

D = Decimal
D0 = Decimal("0")

_DAY = date(2024, 1, 4)
SH = "sh.600000"


# ----------------------------------------------------------------------
# 工厂函数
# ----------------------------------------------------------------------

def _bar(
    o: str = "10.04", p: str = "10.00", symbol: str = SH, d: date = _DAY,
    **flags: bool,
) -> Bar:
    po, pp = D(o), D(p)
    return Bar(
        date=d, symbol=symbol, open=po, high=po, low=po, close=po, preclose=pp,
        volume=D("1000000"), amount=D("10040000"), **flags,
    )


def _order(side: OrderSide = OrderSide.BUY, volume: int = 10000, d: date = _DAY) -> Order:
    return Order(
        client_order_id=f"gap-{side.value}-{volume}-{d.isoformat()}",
        symbol=SH, side=side, order_type=OrderType.MARKET,
        volume=volume, price=None, created_date=d,
    )


# ======================================================================
# A. 缺口滑点参数校验（fail-closed）
# ======================================================================

class TestGapSlippageParamValidation:
    def test_gap_slippage_must_be_decimal(self) -> None:
        with pytest.raises(FeeError, match="gap_slippage_pct 须为 Decimal"):
            make_price_model(gap_slippage_pct=0.001)  # float

    def test_gap_slippage_cannot_be_negative(self) -> None:
        with pytest.raises(FeeError, match="gap_slippage_pct 不可为负"):
            make_price_model(gap_slippage_pct=D("-0.001"))

    def test_gap_threshold_must_be_decimal(self) -> None:
        with pytest.raises(FeeError, match="gap_threshold_pct 须为 Decimal"):
            make_price_model(gap_slippage_pct=D("0.001"), gap_threshold_pct=0.03)  # float

    def test_gap_threshold_must_be_in_range(self) -> None:
        with pytest.raises(FeeError, match="gap_threshold_pct 须在"):
            make_price_model(gap_slippage_pct=D("0.001"), gap_threshold_pct=D("0"))
        with pytest.raises(FeeError, match="gap_threshold_pct 须在"):
            make_price_model(gap_slippage_pct=D("0.001"), gap_threshold_pct=D("1"))

    def test_zero_gap_slippage_allowed(self) -> None:
        # 0 滑点合法（压测边界 / 无缺口滑点场景）
        pm = make_price_model(gap_slippage_pct=D("0"))
        assert pm(_order(), _bar()) == D("10.05")  # 仅基础 5bps


# ======================================================================
# B. 向后兼容（gap_slippage_pct=None ⇒ 无缺口滑点，恒等原有逻辑）
# ======================================================================

class TestBackwardCompatibility:
    def test_none_disables_gap_slippage(self) -> None:
        pm = make_price_model(gap_slippage_pct=None)  # 显式 None
        # preclose=10.00, open=10.30（+3% 缺口），但未启用缺口滑点 ⇒ 仅基础 5bps
        bar = _bar(o="10.30", p="10.00")
        # BUY：10.30 × 1.0005 = 10.30515 → tick 10.31
        assert pm(_order(OrderSide.BUY), bar) == D("10.31")
        # SELL：10.30 × 0.9995 = 10.29485 → 10.29
        assert pm(_order(OrderSide.SELL), bar) == D("10.29")

    def test_default_arg_backward_compat(self) -> None:
        # 不传 gap_slippage_pct ⇒ 默认 None ⇒ 无缺口滑点
        pm = make_price_model()
        # 10.50 × 1.0005 = 10.50525 → 10.51（仅基础 5bps，无缺口滑点）
        assert pm(_order(), _bar(o="10.50", p="10.00")) == D("10.51")


# ======================================================================
# C. 缺口检测与阈值逻辑
# ======================================================================

class TestGapDetection:
    def test_gap_below_threshold_no_extra_slippage(self) -> None:
        # 缺口 2.5% < 3% 阈值 ⇒ 不触发额外滑点
        pm = make_price_model(gap_slippage_pct=D("0.0010"), gap_threshold_pct=D("0.03"))
        bar = _bar(o="10.25", p="10.00")  # +2.5%
        # BUY：10.25 × 1.0005 = 10.255125 → 10.26（仅基础 5bps）
        assert pm(_order(OrderSide.BUY), bar) == D("10.26")

    def test_gap_at_threshold_triggers(self) -> None:
        # 缺口恰好 3.0% ⇒ 触发（>= 阈值）
        pm = make_price_model(gap_slippage_pct=D("0.0010"), gap_threshold_pct=D("0.03"))
        bar = _bar(o="10.30", p="10.00")  # +3.0%
        # BUY：10.30 × 1.0005（基础）× 1.0010（缺口）= 10.30 × 1.001501 = 10.31546 → 10.32
        assert pm(_order(OrderSide.BUY), bar) == D("10.32")

    def test_gap_5pct_triggers(self) -> None:
        # 缺口 5.0% >> 3% 阈值 ⇒ 触发
        pm = make_price_model(gap_slippage_pct=D("0.0010"), gap_threshold_pct=D("0.03"))
        bar = _bar(o="10.50", p="10.00")  # +5.0%
        # BUY：10.50 × 1.0005 × 1.0010 = 10.50 × 1.001501 = 10.51576 → 10.52
        assert pm(_order(OrderSide.BUY), bar) == D("10.52")

    def test_custom_threshold_5pct(self) -> None:
        # 自定义阈值 5% ⇒ 3% 缺口不触发
        pm = make_price_model(gap_slippage_pct=D("0.0010"), gap_threshold_pct=D("0.05"))
        bar = _bar(o="10.30", p="10.00")  # +3.0% < 5%
        # 不触发 ⇒ 仅基础 5bps：10.30 × 1.0005 = 10.305 → 10.31
        assert pm(_order(OrderSide.BUY), bar) == D("10.31")
        # 5% 时触发
        bar5 = _bar(o="10.50", p="10.00")  # +5.0% >= 5%
        assert pm(_order(OrderSide.BUY), bar5) == D("10.52")


# ======================================================================
# D. 方向性 — BUY 不利 / SELL 对称
# ======================================================================

class TestDirectionality:
    def test_buy_upward_gap_more_expensive(self) -> None:
        # BUY + 跳空↑ ⇒ 买得更贵（基础 5bps + 缺口 10bps）
        pm = make_price_model(gap_slippage_pct=D("0.0010"))
        bar = _bar(o="10.30", p="10.00")  # +3%
        # 10.30 × 1.0005 × 1.0010 = 10.31546 → 10.32
        assert pm(_order(OrderSide.BUY), bar) == D("10.32")

    def test_buy_downward_gap_also_adds_slippage(self) -> None:
        # BUY + 跳空↓ ⇒ 保守估计仍加缺口滑点（流动性枯竭双向）
        pm = make_price_model(gap_slippage_pct=D("0.0010"))
        bar = _bar(o="9.70", p="10.00")  # -3%
        # 9.70 × 1.0005 × 1.0010 = 9.70 × 1.001501 = 9.71456 → 9.71
        assert pm(_order(OrderSide.BUY), bar) == D("9.71")

    def test_sell_upward_gap_adds_slippage(self) -> None:
        # SELL + 跳空↑ ⇒ 保守估计加缺口滑点
        pm = make_price_model(gap_slippage_pct=D("0.0010"))
        bar = _bar(o="10.30", p="10.00")  # +3%
        # 10.30 × 0.9995 × 0.9990 = 10.30 × 0.998501 = 10.28456 → 10.28
        assert pm(_order(OrderSide.SELL), bar) == D("10.28")

    def test_sell_downward_gap_worse(self) -> None:
        # SELL + 跳空↓ ⇒ 卖得更贱（基础 -5bps + 缺口 -10bps）
        pm = make_price_model(gap_slippage_pct=D("0.0010"))
        bar = _bar(o="9.70", p="10.00")  # -3%
        # 9.70 × 0.9995 × 0.9990 = 9.70 × 0.998501 = 9.68546 → 9.69
        assert pm(_order(OrderSide.SELL), bar) == D("9.69")


# ======================================================================
# E. 压测档 — 3% / 5% 缺口 × 10bps / 20bps 缺口滑点
# ======================================================================

class TestStressScenarios:
    """±3% / ±5% 缺口 × 10bps / 20bps 缺口滑点 ⇒ 总滑点单调递增，tick 红线不破。"""

    def test_3pct_gap_10bps_extra_buy(self) -> None:
        pm = make_price_model(gap_slippage_pct=D("0.0010"))
        bar = _bar(o="10.30", p="10.00")  # +3%
        # 10.30 × 1.0005 × 1.0010 = 10.31546 → 10.32
        p = pm(_order(OrderSide.BUY), bar)
        assert p == D("10.32")
        assert p == p.quantize(TICK_SIZE)  # tick 红线

    def test_3pct_gap_20bps_extra_buy(self) -> None:
        pm = make_price_model(gap_slippage_pct=D("0.0020"))
        bar = _bar(o="10.30", p="10.00")  # +3%
        # 10.30 × 1.0005 × 1.0020 = 10.30 × 1.002501 = 10.32576 → 10.33
        p = pm(_order(OrderSide.BUY), bar)
        assert p == D("10.33")
        assert p > D("10.32")  # 20bps > 10bps

    def test_5pct_gap_10bps_extra_buy(self) -> None:
        pm = make_price_model(gap_slippage_pct=D("0.0010"))
        bar = _bar(o="10.50", p="10.00")  # +5%
        # 10.50 × 1.0005 × 1.0010 = 10.51576 → 10.52
        p = pm(_order(OrderSide.BUY), bar)
        assert p == D("10.52")

    def test_5pct_gap_20bps_extra_buy(self) -> None:
        pm = make_price_model(gap_slippage_pct=D("0.0020"))
        bar = _bar(o="10.50", p="10.00")  # +5%
        # 10.50 × 1.0005 × 1.0020 = 10.50 × 1.002501 = 10.52626 → 10.53
        p = pm(_order(OrderSide.BUY), bar)
        assert p == D("10.53")
        assert p > D("10.52")

    def test_5pct_gap_20bps_extra_sell(self) -> None:
        pm = make_price_model(gap_slippage_pct=D("0.0020"))
        bar = _bar(o="9.50", p="10.00")  # -5%
        # 9.50 × 0.9995 × 0.9980 = 9.50 × 0.997501 = 9.47626 → 9.48
        p = pm(_order(OrderSide.SELL), bar)
        assert p == D("9.48")
        assert p == p.quantize(TICK_SIZE)

    def test_monotonicity_gap_increases_drag(self) -> None:
        # 单调性：缺口滑点 0 < 10bps < 20bps ⇒ BUY 价格单调递增
        bar = _bar(o="10.30", p="10.00")
        pm0 = make_price_model(gap_slippage_pct=None)
        pm10 = make_price_model(gap_slippage_pct=D("0.0010"))
        pm20 = make_price_model(gap_slippage_pct=D("0.0020"))
        p0 = pm0(_order(OrderSide.BUY), bar)
        p10 = pm10(_order(OrderSide.BUY), bar)
        p20 = pm20(_order(OrderSide.BUY), bar)
        assert p0 < p10 < p20  # 无缺口滑点 < 10bps < 20bps


# ======================================================================
# F. 边界与红线
# ======================================================================

class TestEdgeCasesAndInvariants:
    def test_zero_preclose_no_crash(self) -> None:
        # preclose=0 ⇒ 缺口计算跳过（防零除），仅基础滑点
        pm = make_price_model(gap_slippage_pct=D("0.0010"))
        bar = _bar(o="10.00", p="0.00")  # preclose=0
        # 10.00 × 1.0005 = 10.005 → 10.01（无缺口滑点，不崩溃）
        assert pm(_order(OrderSide.BUY), bar) == D("10.01")

    def test_no_gap_no_extra_slippage(self) -> None:
        # open = preclose ⇒ 缺口 0% < 阈值 ⇒ 不触发
        pm = make_price_model(gap_slippage_pct=D("0.0010"))
        bar = _bar(o="10.00", p="10.00")
        # 10.00 × 1.0005 = 10.005 → 10.01
        assert pm(_order(OrderSide.BUY), bar) == D("10.01")

    def test_limit_clamp_after_gap_slippage(self) -> None:
        # 涨跌停限幅在缺口滑点**之后**（tick 取整后）生效
        pm = make_price_model(
            gap_slippage_pct=D("0.0020"), limit_pct=D("0.1"))
        # preclose=10.00, open=11.00（涨停开）
        bar = _bar(o="11.00", p="10.00")
        # 11.00 × 1.0005 × 1.0020 = 11.00 × 1.002501 = 11.02751 → tick 11.03 > limit_up=11.00 ⇒ 限回 11.00
        assert pm(_order(OrderSide.BUY), bar) == D("11.00")

    def test_tick_size_always_respected(self) -> None:
        # 无论缺口多大，成交价恒为 0.01 的整数倍
        pm = make_price_model(gap_slippage_pct=D("0.0030"))  # 30bps 缺口滑点
        bar = _bar(o="10.77", p="10.00")  # +7.7% 缺口
        p = pm(_order(OrderSide.BUY), bar)
        assert p == p.quantize(TICK_SIZE)


# ======================================================================
# G. 实战场景 — 5 日持续跳空压测
# ======================================================================

class TestMultiDayGapStress:
    """模拟连续 5 日跳空场景，累积滑点拖累净值。"""

    def test_consecutive_gaps_accumulate_drag(self) -> None:
        # 场景：T1–T5 每日 +3% 跳空开盘，持仓全程（BUY T1 → SELL T5）
        pm_no_gap = make_price_model(gap_slippage_pct=None)
        pm_gap = make_price_model(gap_slippage_pct=D("0.0010"))

        # T1 BUY：preclose=10.00, open=10.30
        bar_t1 = _bar(o="10.30", p="10.00")
        buy_no_gap = pm_no_gap(_order(OrderSide.BUY), bar_t1)
        buy_gap = pm_gap(_order(OrderSide.BUY), bar_t1)
        assert buy_no_gap == D("10.31")  # 仅基础 5bps
        assert buy_gap == D("10.32")     # 基础 + 缺口 10bps

        # T5 SELL：preclose=13.00（假设涨到），open=13.39（+3%）
        bar_t5 = _bar(o="13.39", p="13.00")
        sell_no_gap = pm_no_gap(_order(OrderSide.SELL), bar_t5)
        sell_gap = pm_gap(_order(OrderSide.SELL), bar_t5)
        assert sell_no_gap == D("13.38")  # 13.39 × 0.9995 = 13.383305 → 13.38
        assert sell_gap == D("13.37")     # 13.39 × 0.9995 × 0.9990 = 13.37028 → 13.37

        # 净收益：无缺口滑点 vs 有缺口滑点（10,000 股）
        pnl_no_gap = (sell_no_gap - buy_no_gap) * D("10000")
        pnl_gap = (sell_gap - buy_gap) * D("10000")
        drag = pnl_no_gap - pnl_gap
        # 无缺口：(13.38 - 10.31) × 10000 = 30700
        # 有缺口：(13.37 - 10.32) × 10000 = 30500
        # 拖累：30700 - 30500 = 200
        assert drag == D("200.00")  # 额外拖累 ¥200/万股（两端各约 1 分钱）
        assert pnl_gap < pnl_no_gap  # 缺口滑点吃收益
