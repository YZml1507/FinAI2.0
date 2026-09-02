#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""高价股过滤单测（离线，⛔ 无网络/磁盘依赖）—— 验证 max_price 机制。

覆盖：A 配置校验（float 拒绝、负值拒绝、None 合法）/ B plan_positions 过滤逻辑 / C 全链集成。
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from strategy.portfolio import (
    PortfolioConfig,
    PortfolioError,
    plan_positions,
    select_targets,
)

D = Decimal


def _bar(close: str = "10.00", amount: str = "60000000") -> SimpleNamespace:
    """流动性充足的正常 bar 替身。"""
    return SimpleNamespace(close=D(close), amount=D(amount))


class TestMaxPriceConfigValidation:
    """高价股配置校验：float 拒绝、负值拒绝、None 合法。"""

    def test_default_max_price_is_300(self) -> None:
        cfg = PortfolioConfig()
        assert cfg.max_price == D("300.0")

    def test_max_price_none_disables_filter(self) -> None:
        cfg = PortfolioConfig(max_price=None)
        assert cfg.max_price is None

    def test_max_price_float_rejected(self) -> None:
        with pytest.raises(PortfolioError, match="max_price 须为 Decimal 或 None"):
            PortfolioConfig(max_price=300.0)

    def test_max_price_zero_or_negative_rejected(self) -> None:
        with pytest.raises(PortfolioError, match="max_price 须 > 0"):
            PortfolioConfig(max_price=D("0"))
        with pytest.raises(PortfolioError, match="max_price 须 > 0"):
            PortfolioConfig(max_price=D("-1"))

    def test_max_price_positive_decimal_ok(self) -> None:
        cfg = PortfolioConfig(max_price=D("500.0"))
        assert cfg.max_price == D("500.0")


class TestMaxPricePlanFiltering:
    """plan_positions 过滤逻辑：超价格上限即 dropped。"""

    def test_stock_above_max_price_dropped(self) -> None:
        cfg = PortfolioConfig(max_price=D("300.0"))
        targets = ["high1", "low1"]
        bars = {
            "high1": _bar(close="350.00"),   # 超过 300
            "low1": _bar(close="50.00"),
        }
        plan, dropped = plan_positions(targets, D("100000"), bars, cfg)
        assert "high1" not in plan
        assert "low1" in plan
        assert ("high1", "超过价格上限") in dropped

    def test_stock_at_max_price_dropped(self) -> None:
        # 边界：等于上限也过滤（> 判定）
        cfg = PortfolioConfig(max_price=D("300.0"))
        targets = ["boundary"]
        bars = {"boundary": _bar(close="300.01")}
        plan, dropped = plan_positions(targets, D("100000"), bars, cfg)
        assert "boundary" not in plan
        assert ("boundary", "超过价格上限") in dropped

    def test_stock_just_below_max_price_passes(self) -> None:
        cfg = PortfolioConfig(max_price=D("300.0"))
        targets = ["ok"]
        bars = {"ok": _bar(close="299.99")}
        plan, dropped = plan_positions(targets, D("100000"), bars, cfg)
        assert "ok" in plan
        assert len(dropped) == 0

    def test_max_price_none_allows_all(self) -> None:
        cfg = PortfolioConfig(max_price=None)
        targets = ["extreme"]
        bars = {"extreme": _bar(close="999.00")}
        plan, dropped = plan_positions(targets, D("100000"), bars, cfg)
        assert "extreme" in plan
        assert len(dropped) == 0

    def test_multiple_high_price_stocks_all_filtered(self) -> None:
        cfg = PortfolioConfig(max_price=D("200.0"))
        targets = ["high1", "high2", "low1"]
        bars = {
            "high1": _bar(close="300.00"),
            "high2": _bar(close="250.00"),
            "low1": _bar(close="100.00"),
        }
        plan, dropped = plan_positions(targets, D("150000"), bars, cfg)
        assert "low1" in plan
        assert "high1" not in plan
        assert "high2" not in plan
        assert len(dropped) == 2
        dropped_dict = dict(dropped)
        assert dropped_dict["high1"] == "超过价格上限"
        assert dropped_dict["high2"] == "超过价格上限"


class TestMaxPriceIntegration:
    """全链集成：select → plan 过滤高价股。"""

    def test_high_scorer_filtered_by_price(self) -> None:
        # 高分股票因价格过高被过滤，低分低价股入选
        cfg = PortfolioConfig(target_count=3, min_positions=3, max_price=D("200.0"))
        signals = {
            "expensive_winner": D("0.8"),   # 高分但贵
            "cheap_second": D("0.5"),       # 低分但便宜
            "cheap_third": D("0.3"),
        }
        targets = select_targets(signals, cfg)
        assert targets == ["expensive_winner", "cheap_second", "cheap_third"]  # select 不看价格

        bars = {
            "expensive_winner": _bar(close="350.00"),  # 超价格上限
            "cheap_second": _bar(close="50.00"),
            "cheap_third": _bar(close="30.00"),
        }
        plan, dropped = plan_positions(targets, D("150000"), bars, cfg)
        # expensive_winner 虽排第一但被价格过滤
        assert "expensive_winner" not in plan
        assert "cheap_second" in plan
        assert "cheap_third" in plan
        assert ("expensive_winner", "超过价格上限") in dropped
