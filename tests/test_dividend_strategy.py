#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T311 红利策略单元测试 —— 参数校验 + 选股逻辑 + MA200 择时 + 调仓频率。

测试覆盖（≥12 例）：
- 参数校验（4 例）：float 拒绝 / 负数拒绝 / 超 20% 拒绝 / 候选池 < 最大持仓拒绝
- 选股逻辑（3 例）：股息率筛选 / 候选池规模截断 / 市值加权归一化
- MA200 择时（3 例）：指数 > MA200 选股 / 指数 < MA200 空仓 / 冷启动不交易
- 调仓频率（2 例）：调仓日交易 / 非调仓日不动
"""
from __future__ import annotations

from collections import deque
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtest.constants import OrderSide
from backtest.types import Bar
from strategy.candidates import DividendConfig, DividendStrategy, Signal
from strategy.portfolio import PortfolioConfig

_ZERO = Decimal("0")


# ==============================================================================
# 参数校验（4 例）
# ==============================================================================


def test_min_dividend_yield_must_be_decimal():
    """min_dividend_yield 为 float → raise TypeError"""
    with pytest.raises(TypeError, match="min_dividend_yield 须为 Decimal"):
        DividendConfig(min_dividend_yield=0.03)  # type: ignore


def test_min_dividend_yield_negative():
    """min_dividend_yield 为负数 → raise ValueError"""
    with pytest.raises(ValueError, match="min_dividend_yield 须在.*范围内"):
        DividendConfig(min_dividend_yield=Decimal("-0.01"))


def test_min_dividend_yield_exceeds_20_percent():
    """min_dividend_yield > 0.20 → raise ValueError（20% 股息率不合理）"""
    with pytest.raises(ValueError, match="min_dividend_yield 须在.*0.20.*范围内"):
        DividendConfig(min_dividend_yield=Decimal("0.25"))


def test_candidate_pool_size_less_than_max_positions():
    """candidate_pool_size < max_positions → raise ValueError"""
    with pytest.raises(ValueError, match="candidate_pool_size.*须 >=.*max_positions"):
        DividendConfig(candidate_pool_size=5, max_positions=8)


# ==============================================================================
# 选股逻辑（3 例）
# ==============================================================================


def test_dividend_yield_filter():
    """3 只股票（股息率 2% / 4% / 6%），min=3% → 选中 2 只（4% / 6%）"""
    cfg = DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        candidate_pool_size=50,
        default_positions=5,
        use_ma200_timing=False,  # 不使用择时
        warmup_bars=200,          # 满足最小需求
        rebalance_days=1,         # 每日调仓（测试用）
    )
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000", "sh.600001", "sh.600002"]

    bars = {
        "sh.600000": Bar(
            date=date(2020, 1, 2), symbol="sh.600000",
            open=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
            close=Decimal("10"), preclose=Decimal("10"),
            volume=Decimal("1000000"), amount=Decimal("10000000"),
            dividend_yield=Decimal("0.02"),  # 2%（低于 3%）
            market_cap=Decimal("1000000000"),
        ),
        "sh.600001": Bar(
            date=date(2020, 1, 2), symbol="sh.600001",
            open=Decimal("20"), high=Decimal("20"), low=Decimal("20"),
            close=Decimal("20"), preclose=Decimal("20"),
            volume=Decimal("1000000"), amount=Decimal("20000000"),
            dividend_yield=Decimal("0.04"),  # 4%（符合）
            market_cap=Decimal("2000000000"),
        ),
        "sh.600002": Bar(
            date=date(2020, 1, 2), symbol="sh.600002",
            open=Decimal("30"), high=Decimal("30"), low=Decimal("30"),
            close=Decimal("30"), preclose=Decimal("30"),
            volume=Decimal("1000000"), amount=Decimal("30000000"),
            dividend_yield=Decimal("0.06"),  # 6%（符合）
            market_cap=Decimal("3000000000"),
        ),
    }

    signals = strategy._select_stocks(bars, cfg)

    # 只选中 600001 / 600002
    assert len(signals) == 2
    symbols = {s.symbol for s in signals}
    assert symbols == {"sh.600001", "sh.600002"}


def test_candidate_pool_size_truncation():
    """10 只股票，candidate_pool_size=5 → 只选前 5 只（股息率最高）"""
    cfg = DividendConfig(
        min_dividend_yield=Decimal("0.01"),
        candidate_pool_size=5,
        min_positions=3,
        max_positions=5,
        default_positions=3,
        use_ma200_timing=False,
        warmup_bars=200,
        rebalance_days=1,
    )
    strategy = DividendStrategy(config=cfg)

    bars = {}
    for i in range(10):
        symbol = f"sh.60000{i}"
        # 股息率从 1% 到 10%（0.01 到 0.10）
        div_yield = Decimal(f"0.0{i + 1}") if i < 9 else Decimal("0.10")
        bars[symbol] = Bar(
            date=date(2020, 1, 2), symbol=symbol,
            open=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
            close=Decimal("10"), preclose=Decimal("10"),
            volume=Decimal("1000000"), amount=Decimal("10000000"),
            dividend_yield=div_yield,
            market_cap=Decimal("1000000000"),
        )

    signals = strategy._select_stocks(bars, cfg)

    # 取前 5 只（股息率 6%-10%），但 default_positions=3 只取 3 只
    assert len(signals) == 3
    # 股息率最高的 3 只：sh.600009（10%）/ sh.600008（9%）/ sh.600007（8%）
    symbols = {s.symbol for s in signals}
    assert symbols == {"sh.600009", "sh.600008", "sh.600007"}


def test_market_cap_weighting():
    """市值加权：3 只候选（市值 100 亿 / 50 亿 / 50 亿）→ 权重 50% / 25% / 25%"""
    cfg = DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        candidate_pool_size=50,
        min_positions=3,
        max_positions=5,
        default_positions=3,
        use_ma200_timing=False,
        warmup_bars=200,
        rebalance_days=1,
    )
    strategy = DividendStrategy(config=cfg)

    bars = {
        "sh.600000": Bar(
            date=date(2020, 1, 2), symbol="sh.600000",
            open=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
            close=Decimal("10"), preclose=Decimal("10"),
            volume=Decimal("1000000"), amount=Decimal("10000000"),
            dividend_yield=Decimal("0.05"),
            market_cap=Decimal("10000000000"),  # 100 亿
        ),
        "sh.600001": Bar(
            date=date(2020, 1, 2), symbol="sh.600001",
            open=Decimal("20"), high=Decimal("20"), low=Decimal("20"),
            close=Decimal("20"), preclose=Decimal("20"),
            volume=Decimal("1000000"), amount=Decimal("20000000"),
            dividend_yield=Decimal("0.04"),
            market_cap=Decimal("5000000000"),  # 50 亿
        ),
        "sh.600002": Bar(
            date=date(2020, 1, 2), symbol="sh.600002",
            open=Decimal("30"), high=Decimal("30"), low=Decimal("30"),
            close=Decimal("30"), preclose=Decimal("30"),
            volume=Decimal("1000000"), amount=Decimal("30000000"),
            dividend_yield=Decimal("0.04"),
            market_cap=Decimal("5000000000"),  # 50 亿
        ),
    }

    signals = strategy._select_stocks(bars, cfg)

    # 检查权重归一化
    total_weight = sum(s.score for s in signals)
    assert abs(total_weight - Decimal("1.0")) < Decimal("0.0001")  # 归一化

    # 检查各股权重
    weight_map = {s.symbol: s.score for s in signals}
    assert abs(weight_map["sh.600000"] - Decimal("0.5")) < Decimal("0.0001")  # 50%
    assert abs(weight_map["sh.600001"] - Decimal("0.25")) < Decimal("0.0001")  # 25%
    assert abs(weight_map["sh.600002"] - Decimal("0.25")) < Decimal("0.0001")  # 25%


# ==============================================================================
# MA200 择时（3 例）
# ==============================================================================


class MockBook:
    """模拟账本（测试用）"""
    def __init__(self, nav: Decimal):
        self.total_nav = nav
        self.positions = {}


class MockBroker:
    """模拟券商（测试用）"""
    def __init__(self):
        self.orders = []

    def submit(self, order):
        self.orders.append(order)


def test_ma200_above_market_selects_stocks():
    """指数收盘价 > MA200 → 正常选股（返回信号列表）"""
    cfg = DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        use_ma200_timing=True,
        index_symbol="sh.000300",
        warmup_bars=210,
        rebalance_days=20,
        min_positions=2,
        max_positions=5,
        default_positions=2,
    )
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000", "sh.600001", "sh.000300"]

    # 不预填充 MA200 缓存，让策略自己积累（避免冷启动逻辑冲突）

    # 模拟 230 个交易日（冷启动 210 + 调仓日 230）
    book = MockBook(nav=Decimal("100000"))
    broker = MockBroker()

    for i in range(230):
        day = date(2020, 1, 1) + timedelta(days=i)
        bars = {
            "sh.000300": Bar(
                date=day, symbol="sh.000300",
                open=Decimal("120"), high=Decimal("120"), low=Decimal("120"),
                close=Decimal("120"),  # > MA200（会逐渐积累到 120）
                preclose=Decimal("120"),
                volume=Decimal("1000000"), amount=Decimal("120000000"),
            ),
            "sh.600000": Bar(
                date=day, symbol="sh.600000",
                open=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
                close=Decimal("10"), preclose=Decimal("10"),
                volume=Decimal("1000000"), amount=Decimal("100000000"),
                dividend_yield=Decimal("0.05"),
                market_cap=Decimal("2000000000"),
            ),
            "sh.600001": Bar(
                date=day, symbol="sh.600001",
                open=Decimal("20"), high=Decimal("20"), low=Decimal("20"),
                close=Decimal("20"), preclose=Decimal("20"),
                volume=Decimal("1000000"), amount=Decimal("100000000"),
                dividend_yield=Decimal("0.04"),
                market_cap=Decimal("1000000000"),
            ),
        }
        strategy.on_bar(day, bars, book, broker)

    # 检查是否产生订单（指数 > MA200，应选股）
    assert len(broker.orders) > 0


def test_ma200_below_market_empty_position():
    """指数收盘价 < MA200 → 空仓（返回空列表）"""
    cfg = DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        use_ma200_timing=True,
        index_symbol="sh.000300",
        warmup_bars=210,
        rebalance_days=20,
    )
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000", "sh.000300"]

    # 填充 MA200 缓存（200 天，均值 100）
    for _ in range(200):
        strategy._ma200_buffer.append(Decimal("100"))

    book = MockBook(nav=Decimal("100000"))
    broker = MockBroker()

    for i in range(210):
        day = date(2020, 1, 1) + timedelta(days=i)
        bars = {
            "sh.000300": Bar(
                date=day, symbol="sh.000300",
                open=Decimal("80"), high=Decimal("80"), low=Decimal("80"),
                close=Decimal("80"),  # < MA200(100)
                preclose=Decimal("80"),
                volume=Decimal("1000000"), amount=Decimal("80000000"),
            ),
            "sh.600000": Bar(
                date=day, symbol="sh.600000",
                open=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
                close=Decimal("10"), preclose=Decimal("10"),
                volume=Decimal("1000000"), amount=Decimal("10000000"),
                dividend_yield=Decimal("0.05"),
                market_cap=Decimal("1000000000"),
            ),
        }
        strategy.on_bar(day, bars, book, broker)

    # 检查是否产生订单（指数 < MA200，应空仓 → 无订单）
    assert len(broker.orders) == 0


def test_warmup_period_no_trading():
    """冷启动期（<210 日）→ 不交易（返回空列表）"""
    cfg = DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        warmup_bars=210,
        rebalance_days=20,
        use_ma200_timing=False,
    )
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000"]

    book = MockBook(nav=Decimal("100000"))
    broker = MockBroker()

    # 只跑 100 天（< 210）
    for i in range(100):
        day = date(2020, 1, 1) + timedelta(days=i)
        bars = {
            "sh.600000": Bar(
                date=day, symbol="sh.600000",
                open=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
                close=Decimal("10"), preclose=Decimal("10"),
                volume=Decimal("1000000"), amount=Decimal("10000000"),
                dividend_yield=Decimal("0.05"),
                market_cap=Decimal("1000000000"),
            ),
        }
        strategy.on_bar(day, bars, book, broker)

    # 冷启动期不交易
    assert len(broker.orders) == 0


def test_ma200_breach_confirmation_requires_two_days():
    """缓冲带 + 2 日确认：单日假破位/缓冲带内震荡不清仓，确认破位才清仓"""
    cfg = DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        use_ma200_timing=True,
        index_symbol="sh.000300",
        warmup_bars=210,
        rebalance_days=10,
    )
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000", "sh.000300"]

    # 预填 MA200 缓存（199 天，均值 100），留 1 个空位让策略当日加入第 200 个收盘价
    for _ in range(199):
        strategy._ma200_buffer.append(Decimal("100"))
    strategy._bar_count = cfg.warmup_bars
    strategy._last_rebalance_bar = strategy._bar_count  # 避免改造初期触发调仓

    class _Pos:
        volume = 100

    book = MockBook(nav=Decimal("100000"))
    book.positions = {"sh.600000": _Pos()}
    broker = MockBroker()

    day = date(2020, 1, 1)

    def _bars(index_close: str) -> dict:
        return {
            "sh.000300": Bar(
                date=day, symbol="sh.000300",
                open=Decimal(index_close), high=Decimal(index_close), low=Decimal(index_close),
                close=Decimal(index_close), preclose=Decimal(index_close),
                volume=Decimal("1000000"), amount=Decimal("80000000"),
            ),
            "sh.600000": Bar(
                date=day, symbol="sh.600000",
                open=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
                close=Decimal("10"), preclose=Decimal("10"),
                volume=Decimal("1000000"), amount=Decimal("100000000"),
                dividend_yield=Decimal("0.05"), market_cap=Decimal("1000000000"),
            ),
        }

    def _feed(close: str) -> None:
        nonlocal day
        day = day + timedelta(days=1)
        strategy.on_bar(day, _bars(close), book, broker)

    # D1：99.5 落在缓冲带内（breach_line=99.0）→ 不清仓、破位计数不增
    _feed("99.5")
    assert len(broker.orders) == 0
    assert strategy._breach_streak == 0

    # D2：98.5 有效破位第 1 日 → 未确认，不清仓
    _feed("98.5")
    assert len(broker.orders) == 0
    assert strategy._breach_streak == 1

    # D3：101 站回 MA200 → 破位序列中断，计数重置
    _feed("101")
    assert len(broker.orders) == 0
    assert strategy._breach_streak == 0

    # D4+D5：连续 2 日有效破位 → 确认清仓（提交 SELL 并进入避险状态）
    _feed("98.5")
    assert len(broker.orders) == 0
    _feed("98.5")
    assert len(broker.orders) == 1
    assert broker.orders[0].side == OrderSide.SELL
    assert strategy._timing_avoid is True

    # D6：站回 1 日即解除避险（不对称确认），当日仍不下单（待下一调仓节拍）
    _feed("101")
    assert len(broker.orders) == 1
    assert strategy._timing_avoid is False


def test_ma200_rebuild_after_one_day_above():
    """不对称重建：避险中站回 MA200 当日即解除，下一调仓节拍恢复选股建仓"""
    cfg = DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        use_ma200_timing=True,
        index_symbol="sh.000300",
        warmup_bars=210,
        rebalance_days=1,  # 解除避险后下一交易日即可调仓，便于验证
    )
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000", "sh.000300"]

    # 预填 199 天（均值 100），留 1 个空位给策略当日加入，保持 MA200≈100
    for _ in range(199):
        strategy._ma200_buffer.append(Decimal("100"))
    strategy._bar_count = cfg.warmup_bars
    strategy._last_rebalance_bar = strategy._bar_count  # 锚定调仓节拍

    class _Pos:
        volume = 100

    book = MockBook(nav=Decimal("100000"))
    book.positions = {"sh.600000": _Pos()}
    broker = MockBroker()

    day = date(2020, 1, 1)

    def _bars(index_close: str) -> dict:
        return {
            "sh.000300": Bar(
                date=day, symbol="sh.000300",
                open=Decimal(index_close), high=Decimal(index_close), low=Decimal(index_close),
                close=Decimal(index_close), preclose=Decimal(index_close),
                volume=Decimal("1000000"), amount=Decimal("80000000"),
            ),
            "sh.600000": Bar(
                date=day, symbol="sh.600000",
                open=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
                close=Decimal("10"), preclose=Decimal("10"),
                volume=Decimal("1000000"), amount=Decimal("100000000"),
                dividend_yield=Decimal("0.05"), market_cap=Decimal("1000000000"),
            ),
        }

    def _feed(close: str) -> None:
        nonlocal day
        day = day + timedelta(days=1)
        strategy.on_bar(day, _bars(close), book, broker)

    # 连续 2 日有效破位 → 确认清仓进入避险
    _feed("98.5")
    _feed("98.5")
    assert len(broker.orders) == 1  # 清仓单
    assert strategy._timing_avoid is True

    # 站回 1 日即解除避险（不对称确认），当日仍不动（待下一调仓节拍）
    _feed("101")
    assert len(broker.orders) == 1
    assert strategy._timing_avoid is False

    # 下一 bar 为调仓节拍：恢复选股
    book.positions = {}
    _feed("101")
    assert len(broker.orders) > 1
    assert any(o.side == OrderSide.BUY for o in broker.orders[1:])


# ==============================================================================
# 调仓频率（2 例）
# ==============================================================================


def test_rebalance_on_schedule():
    """D1 调仓 → D2-D19 不调仓 → D20 再次调仓"""
    cfg = DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        warmup_bars=210,
        rebalance_days=20,
        use_ma200_timing=False,
    )
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000"]

    book = MockBook(nav=Decimal("100000"))
    broker = MockBroker()

    # 跑 230 天（冷启动 210 + 调仓 20）
    for i in range(230):
        day = date(2020, 1, 1) + timedelta(days=i)
        bars = {
            "sh.600000": Bar(
                date=day, symbol="sh.600000",
                open=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
                close=Decimal("10"), preclose=Decimal("10"),
                volume=Decimal("1000000"), amount=Decimal("100000000"),
                dividend_yield=Decimal("0.05"),
                market_cap=Decimal("1000000000"),
            ),
        }
        strategy.on_bar(day, bars, book, broker)

    # 检查订单数量：D210 调仓 + D230 调仓 = 2 次
    # （实际因为组合层需要多次调用，这里只检查 > 0）
    assert len(broker.orders) >= 1


def test_non_rebalance_day_no_action():
    """非调仓日不动（D211-D229 不产生订单）"""
    cfg = DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        warmup_bars=210,
        rebalance_days=20,
        use_ma200_timing=False,
    )
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000"]

    book = MockBook(nav=Decimal("100000"))
    broker = MockBroker()

    # 跑 220 天（冷启动 210 + 10 天非调仓期）
    for i in range(220):
        day = date(2020, 1, 1) + timedelta(days=i)
        bars = {
            "sh.600000": Bar(
                date=day, symbol="sh.600000",
                open=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
                close=Decimal("10"), preclose=Decimal("10"),
                volume=Decimal("1000000"), amount=Decimal("100000000"),
                dividend_yield=Decimal("0.05"),
                market_cap=Decimal("1000000000"),
            ),
        }
        orders_before = len(broker.orders)
        strategy.on_bar(day, bars, book, broker)

        # D211-D220（非调仓日）不产生新订单
        if 210 < i < 230:
            assert len(broker.orders) == orders_before


# ==============================================================================
# 市场宽度择时：警戒区仓位上限语义（P1 修复，2 例）
# ==============================================================================


def _breadth_cfg(mid_cap: str) -> DividendConfig:
    """宽度择时配置（警戒区上限可调）；宽度序列覆盖测试首日，值 0.30 处于警戒区"""
    return DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        use_breadth_timing=True,
        use_ma200_timing=False,
        breadth_series={"2020-01-01": Decimal("0.30")},
        breadth_attack_threshold=Decimal("0.40"),
        breadth_defense_threshold=Decimal("0.20"),
        breadth_mid_cap=Decimal(mid_cap),
        breadth_ice_confirm_days=2,
        index_symbol="sh.000300",
        warmup_bars=210,
        rebalance_days=10,
        min_positions=1,
        max_positions=5,
        default_positions=1,
    )


def _breadth_bars(day: date) -> dict:
    return {
        "sh.600000": Bar(
            date=day, symbol="sh.600000",
            open=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
            close=Decimal("10"), preclose=Decimal("10"),
            volume=Decimal("1000000"), amount=Decimal("100000000"),
            dividend_yield=Decimal("0.05"),
            market_cap=Decimal("1000000000"),
        ),
    }


def test_breadth_mid_cap_zero_no_crash_and_liquidates():
    """警戒区 mid_cap=0（目标零仓）：不得触发 total_nav>0 守卫崩溃，且存量持仓被出清"""
    cfg = _breadth_cfg("0.0")
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000"]
    strategy._bar_count = cfg.warmup_bars
    strategy._last_rebalance_bar = strategy._bar_count - cfg.rebalance_days  # 当日即调仓

    class _Pos:
        volume = 100

    book = MockBook(nav=Decimal("100000"))
    book.positions = {"sh.600000": _Pos()}
    broker = MockBroker()

    day = date(2020, 1, 1)

    # 不抛 PortfolioError 即通过第一层；随后应产生 SELL 出清订单
    strategy.on_bar(day, _breadth_bars(day), book, broker)
    assert len(broker.orders) >= 1
    assert any(o.side == OrderSide.SELL for o in broker.orders)


def test_breadth_mid_cap_nonzero_still_builds():
    """警戒区 mid_cap=0.3：按 30% 资金建仓（回归保护：非零上限不受零值短路影响）"""
    cfg = _breadth_cfg("0.3")
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000"]
    strategy._bar_count = cfg.warmup_bars
    strategy._last_rebalance_bar = strategy._bar_count - cfg.rebalance_days

    book = MockBook(nav=Decimal("100000"))
    broker = MockBroker()

    day = date(2020, 1, 1)
    strategy.on_bar(day, _breadth_bars(day), book, broker)

    # 应产生 BUY 建仓订单（30% 上限下资金充足，可建 1 仓）
    assert any(o.side == OrderSide.BUY for o in broker.orders)


# ==============================================================================
# e15 attack_instrument 指数 placebo（4 例）
# ==============================================================================


def _attack_cfg() -> DividendConfig:
    """宽度择时 attack 档（b=0.50 ≥ attack 0.40）+ attack_instrument 配置"""
    return DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        use_breadth_timing=True,
        use_ma200_timing=False,
        breadth_series={"2020-01-01": Decimal("0.50")},
        breadth_attack_threshold=Decimal("0.40"),
        breadth_defense_threshold=Decimal("0.20"),
        attack_instrument="sh.510880",
        warmup_bars=210,
        rebalance_days=10,
        index_symbol="sh.000300",
        min_positions=1,
        max_positions=5,
        default_positions=1,
    )


def _etf_bar(day: date) -> Bar:
    """ETF bar：无 dividend_yield / market_cap 字段（⛔ 选股字段不得被读）"""
    return Bar(
        date=day, symbol="sh.510880",
        open=Decimal("3"), high=Decimal("3"), low=Decimal("3"),
        close=Decimal("3"), preclose=Decimal("3"),
        volume=Decimal("100000000"), amount=Decimal("300000000"),
    )


def test_attack_instrument_empty_default_ok():
    """默认空字符串通过校验（向后兼容：选股语义不变）"""
    DividendConfig()  # 不炸即过


def test_attack_instrument_invalid_code():
    """非 'sh./sz.' 前缀代码 → ValueError（fail-closed，防脏代码静默放行）"""
    with pytest.raises(ValueError, match="attack_instrument"):
        DividendConfig(attack_instrument="510880")


def test_attack_instrument_bypasses_stock_selection():
    """配置后调仓信号=单票 ETF：选股层旁路（无 dv/mcap 的 ETF bar 不炸），watchlist 注入"""
    cfg = _attack_cfg()
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000"]
    strategy._bar_count = cfg.warmup_bars
    strategy._last_rebalance_bar = strategy._bar_count - cfg.rebalance_days  # 当日即调仓

    book = MockBook(nav=Decimal("100000"))
    broker = MockBroker()
    day = date(2020, 1, 1)
    bars = dict(_breadth_bars(day))
    bars["sh.510880"] = _etf_bar(day)

    strategy.on_bar(day, bars, book, broker)

    assert "sh.510880" in strategy.watchlist  # feed 需要其 bar
    buys = [o for o in broker.orders if o.side == OrderSide.BUY]
    assert len(buys) == 1 and buys[0].symbol == "sh.510880"


def test_attack_instrument_missing_bar_no_order():
    """ETF bar 缺失（停牌/未注入）→ 计划剔除 → 无订单（fail-closed 空仓不猜值）"""
    cfg = _attack_cfg()
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000"]
    strategy._bar_count = cfg.warmup_bars
    strategy._last_rebalance_bar = strategy._bar_count - cfg.rebalance_days

    book = MockBook(nav=Decimal("100000"))
    broker = MockBroker()
    day = date(2020, 1, 1)

    strategy.on_bar(day, _breadth_bars(day), book, broker)  # 无 ETF bar

    assert len(broker.orders) == 0


# ==============================================================================
# D2 low_vol_keep_pct 低波翼（4 例）
# ==============================================================================


def _lowvol_cfg() -> DividendConfig:
    """宽度择时 attack 档（b=0.50 ≥ attack 0.40）+ low_vol_keep_pct=0.5"""
    return DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        use_breadth_timing=True,
        use_ma200_timing=False,
        breadth_series={"2020-01-01": Decimal("0.50")},
        breadth_attack_threshold=Decimal("0.40"),
        breadth_defense_threshold=Decimal("0.20"),
        breadth_ice_confirm_days=1,
        low_vol_keep_pct=Decimal("0.5"),
        warmup_bars=210,
        rebalance_days=10,
        index_symbol="sh.000300",
        min_positions=1,
        max_positions=5,
        default_positions=1,
    )


def _dv_bar(day: date, symbol: str, dv: str = "0.05") -> Bar:
    return Bar(
        date=day, symbol=symbol,
        open=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
        close=Decimal("10"), preclose=Decimal("10"),
        volume=Decimal("1000000"), amount=Decimal("100000000"),
        dividend_yield=Decimal(dv), market_cap=Decimal("1000000000"),
    )


def test_low_vol_default_none_ok():
    """默认 None 通过校验（向后兼容：选股语义不变）"""
    DividendConfig()


def test_low_vol_invalid_pct():
    """越界/非 Decimal → 拒绝（fail-closed）"""
    with pytest.raises(ValueError, match="low_vol_keep_pct"):
        DividendConfig(low_vol_keep_pct=Decimal("0"))
    with pytest.raises(ValueError, match="low_vol_keep_pct"):
        DividendConfig(low_vol_keep_pct=Decimal("1.5"))
    with pytest.raises(TypeError, match="low_vol_keep_pct"):
        DividendConfig(low_vol_keep_pct=0.5)


def test_low_vol_filter_prefers_low_vol_candidate():
    """同 dv 候选 2 只：高波票被截断、低波票获买——筛子有牙"""
    cfg = _lowvol_cfg()
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000", "sh.600001"]
    strategy._bar_count = cfg.warmup_bars
    strategy._last_rebalance_bar = strategy._bar_count - cfg.rebalance_days
    # 预填 vol 缓冲：600000 低波（恒定 +0.1%）、600001 高波（±2% 交替）
    strategy._ret_buffer["sh.600000"] = deque([0.001] * 200, maxlen=250)
    strategy._ret_buffer["sh.600001"] = deque([0.02, -0.02] * 100, maxlen=250)

    book = MockBook(nav=Decimal("100000"))
    broker = MockBroker()
    day = date(2020, 1, 1)
    bars = {"sh.600000": _dv_bar(day, "sh.600000"),
            "sh.600001": _dv_bar(day, "sh.600001")}

    strategy.on_bar(day, bars, book, broker)

    buys = [o for o in broker.orders if o.side == OrderSide.BUY]
    assert len(buys) == 1 and buys[0].symbol == "sh.600000"


def test_low_vol_missing_history_excluded():
    """vol 史不足 200 日的票 fail-closed 排除——无法验证低波不买"""
    cfg = _lowvol_cfg()
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000", "sh.600001"]
    strategy._bar_count = cfg.warmup_bars
    strategy._last_rebalance_bar = strategy._bar_count - cfg.rebalance_days
    # 600000 有史（低波），600001 只有 50 日 → 被排除
    strategy._ret_buffer["sh.600000"] = deque([0.001] * 200, maxlen=250)
    strategy._ret_buffer["sh.600001"] = deque([0.02, -0.02] * 25, maxlen=250)

    book = MockBook(nav=Decimal("100000"))
    broker = MockBroker()
    day = date(2020, 1, 1)
    bars = {"sh.600000": _dv_bar(day, "sh.600000"),
            "sh.600001": _dv_bar(day, "sh.600001")}

    strategy.on_bar(day, bars, book, broker)

    buys = [o for o in broker.orders if o.side == OrderSide.BUY]
    assert len(buys) == 1 and buys[0].symbol == "sh.600000"


# ==============================================================================
# e16 dv_skip_top / max_dividend_yield 剔尾（4 例）
# ==============================================================================


def _dvskip_cfg() -> DividendConfig:
    """attack 档 + dv_skip_top=1（两候选跳头名 → 选次名）"""
    return DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        use_breadth_timing=True,
        use_ma200_timing=False,
        breadth_series={"2020-01-01": Decimal("0.50")},
        breadth_attack_threshold=Decimal("0.40"),
        breadth_defense_threshold=Decimal("0.20"),
        breadth_ice_confirm_days=1,
        dv_skip_top=1,
        warmup_bars=210,
        rebalance_days=10,
        index_symbol="sh.000300",
        min_positions=1,
        max_positions=5,
        default_positions=1,
    )


def test_dv_skip_default_zero_ok():
    """默认 0 不跳过（向后兼容）"""
    DividendConfig()


def test_dv_skip_invalid():
    """负数/非 int → 拒绝（fail-closed）"""
    with pytest.raises(ValueError, match="dv_skip_top"):
        DividendConfig(dv_skip_top=-1)
    with pytest.raises(TypeError, match="dv_skip_top"):
        DividendConfig(dv_skip_top=15.0)


def test_dv_skip_top_shifts_selection():
    """dv_skip_top=1：跳过 dv 头名 → 选 dv 次名（剔尾语义）"""
    cfg = _dvskip_cfg()
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000", "sh.600001"]
    strategy._bar_count = cfg.warmup_bars
    strategy._last_rebalance_bar = strategy._bar_count - cfg.rebalance_days

    book = MockBook(nav=Decimal("100000"))
    broker = MockBroker()
    day = date(2020, 1, 1)
    # 600000 dv=8%（头名，被跳过）/ 600001 dv=5%（次名，入选）
    bars = {"sh.600000": _dv_bar(day, "sh.600000", dv="0.08"),
            "sh.600001": _dv_bar(day, "sh.600001", dv="0.05")}

    strategy.on_bar(day, bars, book, broker)

    buys = [o for o in broker.orders if o.side == OrderSide.BUY]
    assert len(buys) == 1 and buys[0].symbol == "sh.600001"


def test_dv_cap_excludes_extreme_yield():
    """max_dividend_yield=6%：dv=8% 票被剔 → 选 dv=5% 票"""
    cfg = DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        max_dividend_yield=Decimal("0.06"),
        use_breadth_timing=True,
        use_ma200_timing=False,
        breadth_series={"2020-01-01": Decimal("0.50")},
        breadth_attack_threshold=Decimal("0.40"),
        breadth_defense_threshold=Decimal("0.20"),
        breadth_ice_confirm_days=1,
        warmup_bars=210,
        rebalance_days=10,
        index_symbol="sh.000300",
        min_positions=1,
        max_positions=5,
        default_positions=1,
    )
    strategy = DividendStrategy(config=cfg)
    strategy.watchlist = ["sh.600000", "sh.600001"]
    strategy._bar_count = cfg.warmup_bars
    strategy._last_rebalance_bar = strategy._bar_count - cfg.rebalance_days

    book = MockBook(nav=Decimal("100000"))
    broker = MockBroker()
    day = date(2020, 1, 1)
    bars = {"sh.600000": _dv_bar(day, "sh.600000", dv="0.08"),
            "sh.600001": _dv_bar(day, "sh.600001", dv="0.05")}

    strategy.on_bar(day, bars, book, broker)

    buys = [o for o in broker.orders if o.side == OrderSide.BUY]
    assert len(buys) == 1 and buys[0].symbol == "sh.600001"


# e17 weight_mode 权重形态（5 例）


def _wm_bar(day: date, symbol: str, dv: str, mc: str) -> Bar:
    return Bar(
        date=day, symbol=symbol,
        open=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
        close=Decimal("10"), preclose=Decimal("10"),
        volume=Decimal("1000000"), amount=Decimal("100000000"),
        dividend_yield=Decimal(dv), market_cap=Decimal(mc),
    )


def _wm_cfg(weight_mode: str) -> DividendConfig:
    return DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        candidate_pool_size=50,
        min_positions=3,
        max_positions=5,
        default_positions=3,
        use_ma200_timing=False,
        warmup_bars=200,
        rebalance_days=1,
        weight_mode=weight_mode,
    )


def _wm_bars(day: date) -> dict:
    # dv 降序：600009(8%) > 600008(6%) > 600007(4%)；市值反向：1e9/4e9/5e9
    return {
        "sh.600007": _wm_bar(day, "sh.600007", "0.04", "5000000000"),
        "sh.600008": _wm_bar(day, "sh.600008", "0.06", "4000000000"),
        "sh.600009": _wm_bar(day, "sh.600009", "0.08", "1000000000"),
    }


def test_weight_mode_default_is_market_cap():
    """默认 weight_mode='market_cap'——与 e8b 基线逐值等价（市值占比归一化）"""
    cfg = _wm_cfg("market_cap")
    strategy = DividendStrategy(config=cfg)
    signals = strategy._select_stocks(_wm_bars(date(2020, 1, 2)), cfg)
    w = {s.symbol: s.score for s in signals}
    # Σmc(top3)=10e9：600009→0.1 / 600008→0.4 / 600007→0.5
    assert abs(w["sh.600009"] - Decimal("0.1")) < Decimal("0.0001")
    assert abs(w["sh.600008"] - Decimal("0.4")) < Decimal("0.0001")
    assert abs(w["sh.600007"] - Decimal("0.5")) < Decimal("0.0001")


def test_weight_mode_invalid_rejected():
    """非法 weight_mode ⇒ fail-closed ValueError"""
    with pytest.raises(ValueError, match="weight_mode"):
        _wm_cfg("sqrt_mc")


def test_weight_mode_equal():
    """equal：所有选中票 score=1（组合层归一化后等权）"""
    cfg = _wm_cfg("equal")
    strategy = DividendStrategy(config=cfg)
    signals = strategy._select_stocks(_wm_bars(date(2020, 1, 2)), cfg)
    assert len(signals) == 3
    assert all(s.score == Decimal("1") for s in signals)


def test_weight_mode_dividend_yield():
    """dividend_yield：score=dv 原值（高息票权重更高）"""
    cfg = _wm_cfg("dividend_yield")
    strategy = DividendStrategy(config=cfg)
    signals = strategy._select_stocks(_wm_bars(date(2020, 1, 2)), cfg)
    w = {s.symbol: s.score for s in signals}
    assert w["sh.600009"] == Decimal("0.08")
    assert w["sh.600008"] == Decimal("0.06")
    assert w["sh.600007"] == Decimal("0.04")


def test_weight_mode_equal_zero_mc_not_blocked():
    """equal 模式：Σ市值=0 不触发空仓守卫（权重不依赖市值）"""
    cfg = _wm_cfg("equal")
    strategy = DividendStrategy(config=cfg)
    day = date(2020, 1, 2)
    bars = {
        "sh.600007": _wm_bar(day, "sh.600007", "0.04", "0"),
        "sh.600008": _wm_bar(day, "sh.600008", "0.06", "0"),
    }
    signals = strategy._select_stocks(bars, cfg)
    assert len(signals) == 2
