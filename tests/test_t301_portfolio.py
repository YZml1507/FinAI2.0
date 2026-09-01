#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T301 组合管理器单测（离线，⛔ 无网络/磁盘依赖）—— FR-PM-1/2/4 全链可测验收。

覆盖：A 配置校验 / B select_targets / C plan_positions / D diff_to_orders / E 全链。
bar 替身 = SimpleNamespace（本模块鸭子类型只消费 ``.close`` / ``.amount``）。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backtest.constants import OrderSide
from strategy.portfolio import (
    OrderIntent,
    PortfolioConfig,
    PortfolioError,
    diff_to_orders,
    plan_positions,
    select_targets,
)

D = Decimal
_D = date(2026, 9, 1)
_LOQ = D("60000000")     # 6000 万成交额（>5kw 下限）


def _bar(close: str = "10.00", amount: Decimal | str = _LOQ) -> SimpleNamespace:
    """流动性充足的正常 bar 替身。"""
    return SimpleNamespace(date=_D, close=D(close), amount=D(str(amount)))


class TestConfigValidation:
    def test_default_config_ok(self) -> None:
        cfg = PortfolioConfig()
        assert cfg.target_count == 5 and cfg.hard_limit == 10

    def test_target_count_out_of_range_rejected(self) -> None:
        for bad in (2, 9, 0, -1):
            with pytest.raises(PortfolioError):
                PortfolioConfig(target_count=bad)

    def test_float_in_decimal_fields_rejected(self) -> None:
        for field, value in (("min_position_value", 20000.0),
                             ("min_daily_amount", 50000000.0),
                             ("max_participation_rate", 0.05)):
            with pytest.raises(PortfolioError):
                PortfolioConfig(**{field: value})

    def test_participation_bounds(self) -> None:
        with pytest.raises(PortfolioError):
            PortfolioConfig(max_participation_rate=D("0"))
        with pytest.raises(PortfolioError):
            PortfolioConfig(max_participation_rate=D("1.01"))

    def test_hard_limit_below_max_positions_rejected(self) -> None:
        with pytest.raises(PortfolioError):
            PortfolioConfig(max_positions=8, hard_limit=7)

    def test_lot_size_non_positive_rejected(self) -> None:
        with pytest.raises(PortfolioError):
            PortfolioConfig(lot_size=0)


class TestSelectTargets:
    def test_empty_signals_means_clear_out(self) -> None:
        # FR-PM-2 一等公民：空仓 = 空清单（⛔ 不报错）
        assert select_targets({}, PortfolioConfig()) == []

    def test_top_n_by_score_desc(self) -> None:
        signals = {f"s{i}": D(str(i)) for i in range(7)}   # s6 > s5 > …
        got = select_targets(signals, PortfolioConfig(target_count=5))
        assert got == ["s6", "s5", "s4", "s3", "s2"]

    def test_tie_break_by_symbol_asc(self) -> None:
        signals = {"sz.002": D("0.5"), "sh.001": D("0.5"), "bj.003": D("0.5")}
        got = select_targets(signals, PortfolioConfig(target_count=3))
        assert got == ["bj.003", "sh.001", "sz.002"]        # 平分按代码升序

    def test_non_decimal_score_rejected(self) -> None:
        with pytest.raises(PortfolioError):
            select_targets({"a": 0.5}, PortfolioConfig())  # float 分数


class TestPlanPositions:
    def test_equal_weight_exact_boundary_kept(self) -> None:
        # nav=100000、5 票等权 → 每票 20000 恰压下限，全保留且整手化（10.00→2000 股）
        plan, dropped = plan_positions(
            [f"s{i}" for i in range(5)], D("100000"),
            {f"s{i}": _bar() for i in range(5)}, PortfolioConfig())
        assert not dropped
        assert len(plan) == 5
        assert all(v == D("20000") for v in plan.values())

    def test_below_min_position_value_all_dropped(self) -> None:
        # 每票 19000 < 20000 → 全 dropped，计划为空
        plan, dropped = plan_positions(
            [f"s{i}" for i in range(5)], D("95000"),
            {f"s{i}": _bar() for i in range(5)}, PortfolioConfig())
        assert plan == {}
        assert len(dropped) == 5 and all(r == "低于单票下限" for _, r in dropped)

    def test_liquidity_floor_filtered(self) -> None:
        bars = {"a": _bar(), "b": _bar(amount=D("40000000"))}   # b 4 千万 < 5 千万
        plan, dropped = plan_positions(["a", "b"], D("400000"), bars,
                                       PortfolioConfig())
        assert "a" in plan and "b" not in plan
        assert ("b", "流动性不足") in dropped

    def test_participation_cap_shrinks(self) -> None:
        # nav 1000 万单票等权 base=1000 万；amount=5000 万 ×5% = 250 万封顶 ⇒ 计划收缩到 250 万
        bars = {"a": _bar(amount=D("50000000"))}
        plan, dropped = plan_positions(["a"], D("10000000"), bars,
                                       PortfolioConfig(max_participation_rate=D("0.05")))
        assert plan == {"a": D("2500000")} and not dropped

    def test_suspended_symbol_absent_bar_dropped(self) -> None:
        plan, dropped = plan_positions(["a", "b"], D("1000000"), {"a": _bar()},
                                       PortfolioConfig())
        assert "a" in plan and "b" not in plan
        assert ("b", "停牌不可建仓") in dropped

    def test_hard_limit_exceeded_raises(self) -> None:
        with pytest.raises(PortfolioError):
            plan_positions([f"s{i}" for i in range(11)], D("1000000"),
                           {f"s{i}": _bar() for i in range(11)}, PortfolioConfig())

    def test_total_nav_must_be_positive_decimal(self) -> None:
        with pytest.raises(PortfolioError):
            plan_positions(["a"], D("0"), {"a": _bar()}, PortfolioConfig())
        with pytest.raises(PortfolioError):
            plan_positions(["a"], 100000, {"a": _bar()}, PortfolioConfig())


class TestDiffToOrders:
    def test_new_buy_lot_rounded(self) -> None:
        # 目标 20000 @ 10.00 → 2000 股 BUY
        plan = {"a": D("20000")}
        report = diff_to_orders({}, plan, {"a": _bar()}, PortfolioConfig())
        assert len(report.intents) == 1
        intent = report.intents[0]
        assert intent.symbol == "a" and intent.side is OrderSide.BUY \
            and intent.volume == 2000

    def test_incremental_buy_only_delta(self) -> None:
        # 持有 2000 → 目标 3000 股（市值 30000 @ 10.00）→ 只 BUY 500 增量
        report = diff_to_orders({"a": 2500}, {"a": D("30000")},
                                {"a": _bar()}, PortfolioConfig())
        assert [(i.symbol, i.side, i.volume) for i in report.intents] == \
            [("a", OrderSide.BUY, 500)]

    def test_empty_plan_sells_everything(self) -> None:
        # 择时 clear：plan={} ⇒ 全部清仓（不受 2 万下限约束）
        report = diff_to_orders({"a": 100, "b": 250}, {}, {}, PortfolioConfig())
        sides = {(i.symbol, i.side, i.volume) for i in report.intents}
        assert sides == {("a", OrderSide.SELL, 100), ("b", OrderSide.SELL, 250)}

    def test_position_not_in_plan_sold_regardless_of_value(self) -> None:
        # 300 股 × 10.00 = 3000 元 市值 << 2 万，也卖（退出权优先）
        report = diff_to_orders({"a": 300}, {}, {"a": _bar()}, PortfolioConfig())
        assert [(i.symbol, i.side, i.volume) for i in report.intents] == \
            [("a", OrderSide.SELL, 300)]

    def test_no_diff_no_order(self) -> None:
        report = diff_to_orders({"a": 2000}, {"a": D("20000")},
                                {"a": _bar()}, PortfolioConfig())
        assert report.intents == ()

    def test_odd_lot_full_clear_allowed(self) -> None:
        # 零股 150 股不在 plan → SELL 150（清仓允许零股）
        report = diff_to_orders({"a": 150}, {}, {}, PortfolioConfig())
        assert [(i.symbol, i.side, i.volume) for i in report.intents] == \
            [("a", OrderSide.SELL, 150)]

    def test_sells_come_before_buys(self) -> None:
        # 先释放资金 ⇒ SELL 意图排在 BUY 前
        report = diff_to_orders({"old": 500},
                                {"new": D("50000")}, {"new": _bar()},
                                PortfolioConfig())
        assert [i.side for i in report.intents] == [OrderSide.SELL, OrderSide.BUY]

    def test_bad_current_holdings_rejected(self) -> None:
        with pytest.raises(PortfolioError):
            diff_to_orders({"a": -5}, {}, {}, PortfolioConfig())


class TestFullChain:
    def test_signal_to_orders_end_to_end(self) -> None:
        """FR-PM-2 全链：signals → targets → plan → orders（含一路被丢弃）。"""
        cfg = PortfolioConfig(target_count=3)
        signals = {"a": D("0.9"), "b": D("0.8"), "c": D("0.7"),
                   "d": D("0.6"), "e": D("0.1")}
        targets = select_targets(signals, cfg)
        assert targets == ["a", "b", "c"]                  # top-3
        bars = {"a": _bar(), "b": _bar(), "c": _bar(amount=D("30000000"))}
        # c 流动性不足（3000 万 < 5000 万下限）→ plan 只留 a/b，c 记入 dropped
        plan, dropped_plan = plan_positions(targets, D("90000"), bars, cfg)
        assert set(plan) == {"a", "b"}
        assert dropped_plan == (("c", "流动性不足"),)
        report = diff_to_orders({}, plan, bars, cfg)
        assert {i.symbol for i in report.intents} == {"a", "b"}
        assert all(i.side is OrderSide.BUY for i in report.intents)
        assert all(i.volume > 0 for i in report.intents)

    def test_full_chain_clear_out(self) -> None:
        """择时空仓：无信号 → 全清。"""
        cfg = PortfolioConfig()
        targets = select_targets({}, cfg)
        plan, _ = plan_positions(targets, D("100000"), {}, cfg)
        report = diff_to_orders({"a": 100, "b": 200}, plan, {}, cfg)
        assert {i.symbol for i in report.intents} == {"a", "b"}
        assert all(i.side is OrderSide.SELL for i in report.intents)
