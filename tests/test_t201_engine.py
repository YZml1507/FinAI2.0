#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T201 §7~§10 撮合 / Broker / 结算 / 引擎 单测（离线，⛔ 无任何网络调用）。

数据全部走 ``ParquetDailyFeed(preloaded=...)`` 内存帧 + mock 交易日历；期初资金
经 ``Ledger`` 的 ``CASH_IN`` 流水注入（⛔ 不直接改 ``book.cash``）。

覆盖锚点：

  §7 撮合：规则 1-8 逐条 + 成交价 = ``bar.open`` + ``trade_id`` 幂等形状。
  §8 Broker：submit/cancel 状态机、撮合落账、残单 EXPIRED、除权、T+1 推进。
  §9 结算：bar 在 → 刷市值；bar 缺 → **市值冻结**且 symbol 进受影响集合。
  §10 引擎：**先撮合后信号**（当日信号不可能当日成交）、日期顺序、
      ``nav_curve`` 键集合 == 交易日历集合、多持仓 NAV = 现金 + Σ 市值。
  T202 子集冒烟：涨停买入被拒 / 停牌日卖出被拒（市值冻结）/ T+1 当日买卖被拒。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from backtest.broker import BacktestBroker, BrokerError
from backtest.constants import FeeItem, OrderSide, OrderStatus, OrderType
from backtest.engine import BacktestEngine, BacktestResult, EngineError
from backtest.feed import ParquetDailyFeed
from backtest.ledger import JournalType, Ledger
from backtest.matching import (
    REJECT_BUY_LOT_SIZE,
    REJECT_CASH_INSUFFICIENT,
    REJECT_LIMIT_DOWN_SELL,
    REJECT_LIMIT_UP_BUY,
    REJECT_ODD_LOT_SELL,
    REJECT_SUSPENDED,
    REJECT_T1_INSUFFICIENT,
    MatchContext,
    MatchEngine,
    MatchResult,
)
from backtest.order_fsm import OrderStateError
from backtest.settle import ExdivEvent, settle_day, settle_day_detail
from backtest.types import Bar, Order

D = Decimal

_A = "sh.600000"          # 主板，±10%
_B = "sh.600519"          # 主板，±10%

_D1 = date(2024, 3, 1)
_D2 = date(2024, 3, 4)
_D3 = date(2024, 3, 5)
_D4 = date(2024, 3, 6)
_CALENDAR = [_D1, _D2, _D3, _D4]

_INITIAL_CASH = D("100000")

#: T105 落盘列（与 tests/test_t201_feed.py 同口径；⛔ 无 limit_up/limit_down/exdiv）
_COLS = (
    "date", "open", "high", "low", "close", "preclose",
    "volume", "amount", "turn", "pctChg",
    "tradestatus", "isST", "code", "adjust_mode", "source",
)


# ---------------------------------------------------------------------- 造数

def _row(
    d: date,
    *,
    open_: float,
    close: float,
    preclose: float,
    code: str = _A,
    high: float | None = None,
    low: float | None = None,
) -> dict:
    """一行落盘形状的 bars 记录（数值 float64，模拟真实 parquet）。"""
    return {
        "date": d,
        "open": open_,
        "high": max(open_, close) if high is None else high,
        "low": min(open_, close) if low is None else low,
        "close": close,
        "preclose": preclose,
        "volume": 1_000_000.0,
        "amount": 10_000_000.0,
        "turn": 1.0,
        "pctChg": (close - preclose) / preclose * 100.0,
        "tradestatus": "1",
        "isST": "0",
        "code": code,
        "adjust_mode": "hfq",
        "source": "baostock",
    }


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(_COLS))


def _calendar_fn(dates: list[date]):
    """mock 交易日历（⛔ feed 未注入日历会 raise，这里显式注入）。"""
    return lambda start, end: [d for d in dates if start <= d <= end]


def _bar(
    d: date = _D2,
    *,
    symbol: str = _A,
    open_: str = "10.00",
    close: str = "10.00",
    preclose: str = "10.00",
    limit_up: bool = False,
    limit_down: bool = False,
) -> Bar:
    """直接造 Bar（撮合层单测用，不经 feed）。"""
    return Bar(
        date=d, symbol=symbol,
        open=D(open_), high=D(open_), low=D(open_),
        close=D(close), preclose=D(preclose),
        volume=D("1000000"), amount=D("10000000"),
        limit_up=limit_up, limit_down=limit_down,
    )


def _order(
    side: OrderSide,
    volume: int,
    *,
    oid: str = "O1",
    symbol: str = _A,
    price: str | None = None,
    created: date = _D1,
    submitted: bool = True,
) -> Order:
    """造委托。``submitted=True`` 直接置 ``SUBMITTED``（撮合层单测的前置态）。"""
    order = Order(
        client_order_id=oid,
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET if price is None else OrderType.LIMIT,
        volume=volume,
        price=None if price is None else D(price),
        created_date=created,
    )
    if submitted:
        order.status = OrderStatus.SUBMITTED
    return order


def _default_frames() -> dict[str, pd.DataFrame]:
    """两只标的、四个交易日的常规帧（无涨跌停、无停牌）。"""
    return {
        _A: _frame([
            _row(_D1, open_=10.00, close=10.00, preclose=10.00),
            _row(_D2, open_=10.10, close=10.50, preclose=10.00),
            _row(_D3, open_=10.50, close=10.60, preclose=10.50),
            _row(_D4, open_=10.60, close=10.70, preclose=10.60),
        ]),
        _B: _frame([
            _row(_D1, open_=20.00, close=20.00, preclose=20.00, code=_B),
            _row(_D2, open_=20.20, close=21.00, preclose=20.00, code=_B),
            _row(_D3, open_=21.00, close=21.20, preclose=21.00, code=_B),
            _row(_D4, open_=21.20, close=21.40, preclose=21.20, code=_B),
        ]),
    }


def _make(
    frames: dict[str, pd.DataFrame] | None = None,
    *,
    cash: Decimal = _INITIAL_CASH,
    calendar: list[date] | None = None,
) -> tuple[BacktestEngine, BacktestBroker, ParquetDailyFeed]:
    """装一套 feed + ledger + broker + engine（期初资金走 CASH_IN 流水）。"""
    feed = ParquetDailyFeed(
        preloaded=frames if frames is not None else _default_frames(),
        trade_calendar=_calendar_fn(calendar or _CALENDAR),
    )
    ledger = Ledger(cash, date=_D1)
    broker = BacktestBroker(MatchEngine(), ledger, feed)
    return BacktestEngine(broker, feed), broker, feed


class ScriptStrategy:
    """脚本化策略：``script = {date: [(side, volume, symbol, oid), ...]}``。

    鸭子类型（⛔ 不继承基类）：只提供 ``on_bar`` + 可选 ``watchlist``。
    """

    def __init__(self, script: dict[date, list[tuple]], watchlist=(_A, _B)) -> None:
        self.script = script
        self.watchlist = list(watchlist)
        self.seen_dates: list[date] = []
        self.seen_bar_dates: list[list[date]] = []
        self.navs: list[Decimal] = []

    def on_bar(self, day, bars, book, broker):
        self.seen_dates.append(day)
        self.seen_bar_dates.append(sorted(b.date for b in bars.values()))
        self.navs.append(book.total_nav)
        for i, spec in enumerate(self.script.get(day, [])):
            side, volume, symbol = spec[0], spec[1], spec[2]
            oid = spec[3] if len(spec) > 3 else f"{day.isoformat()}-{i}"
            broker.submit(_order(side, volume, oid=oid, symbol=symbol,
                                 created=day, submitted=False))


# ======================================================================
# §7 撮合引擎
# ======================================================================

def test_rule1_missing_bar_rejected_suspended():
    """规则 1：bar 缺席（停牌 / 无行情）→ REJECTED("停牌不可下单")。"""
    engine = MatchEngine()
    result, trade, reason = engine.match(
        MatchContext(bar=None, order=_order(OrderSide.SELL, 100),
                     sellable=100, position_volume=100))
    assert result is MatchResult.REJECTED
    assert trade is None
    assert reason == REJECT_SUSPENDED


def test_rule2_limit_up_buy_rejected():
    """规则 2：涨停买入不可成交（T202 必挂用例 #1）。"""
    result, trade, reason = MatchEngine().match(
        MatchContext(bar=_bar(limit_up=True), order=_order(OrderSide.BUY, 100),
                     cash_available=D("100000")))
    assert (result, trade, reason) == (MatchResult.REJECTED, None, REJECT_LIMIT_UP_BUY)


def test_rule3_limit_down_sell_rejected():
    """规则 3：跌停卖出不可成交（T202 必挂用例 #2）。"""
    result, _, reason = MatchEngine().match(
        MatchContext(bar=_bar(limit_down=True), order=_order(OrderSide.SELL, 100),
                     sellable=100, position_volume=100))
    assert (result, reason) == (MatchResult.REJECTED, REJECT_LIMIT_DOWN_SELL)


def test_rule4_t1_sellable_insufficient_rejected():
    """规则 4：可卖不足（T+1，T202 必挂用例 #5）。"""
    result, _, reason = MatchEngine().match(
        MatchContext(bar=_bar(), order=_order(OrderSide.SELL, 200),
                     sellable=100, position_volume=200))
    assert (result, reason) == (MatchResult.REJECTED, REJECT_T1_INSUFFICIENT)


def test_rule5_buy_lot_size_rejected():
    """规则 5：买入须整手 100 股。"""
    result, _, reason = MatchEngine().match(
        MatchContext(bar=_bar(), order=_order(OrderSide.BUY, 150),
                     cash_available=D("100000")))
    assert (result, reason) == (MatchResult.REJECTED, REJECT_BUY_LOT_SIZE)


def test_rule6_odd_lot_sell_must_clear_position():
    """规则 6：零股卖出只允许清仓；等于总持仓则放行。"""
    engine = MatchEngine()
    # 零股且 != 总持仓 → 拒
    result, _, reason = engine.match(
        MatchContext(bar=_bar(), order=_order(OrderSide.SELL, 50),
                     sellable=150, position_volume=150))
    assert (result, reason) == (MatchResult.REJECTED, REJECT_ODD_LOT_SELL)
    # 零股 == 总持仓（清仓）→ 成交
    result, trade, reason = engine.match(
        MatchContext(bar=_bar(), order=_order(OrderSide.SELL, 50),
                     sellable=50, position_volume=50))
    assert result is MatchResult.FILLED
    assert trade is not None and trade.volume == 50


def test_rule7_cash_insufficient_uses_conservative_fee_pad():
    """规则 7：需款含万三保守费用垫 ⇒ 刚好等于毛额的现金也不够。"""
    engine = MatchEngine()
    bar = _bar(open_="10.00")
    gross = D("10.00") * 100          # 1000
    # 恰好只有毛额 ⇒ 垫上万三后不够 → 拒
    result, _, reason = engine.match(
        MatchContext(bar=bar, order=_order(OrderSide.BUY, 100),
                     cash_available=gross))
    assert (result, reason) == (MatchResult.REJECTED, REJECT_CASH_INSUFFICIENT)
    # 毛额 + 万三 ⇒ 够 → 成交
    result, trade, _ = engine.match(
        MatchContext(bar=bar, order=_order(OrderSide.BUY, 100),
                     cash_available=gross + gross * D("0.0003")))
    assert result is MatchResult.FILLED
    assert trade is not None


def test_rule8_fill_price_is_bar_open_and_trade_id_idempotent():
    """规则 8：成交价 = ``bar.open``（FR-BT-6）；``trade_id`` = oid:日期（幂等）。"""
    engine = MatchEngine()
    bar = _bar(_D2, open_="10.10", close="10.50")
    ctx = MatchContext(bar=bar, order=_order(OrderSide.BUY, 1000, oid="OX"),
                       cash_available=D("100000"))
    result, trade, reason = engine.match(ctx)
    assert (result, reason) == (MatchResult.FILLED, "")
    assert trade is not None
    assert trade.price == D("10.10")          # ⛔ 不是 close
    assert trade.trade_id == "OX:2024-03-04"
    assert trade.date == _D2
    # 必填费用科目齐备（T201 §3），默认全 0（真实费用 T203 接管）
    assert set(trade.fees) >= {
        FeeItem.COMMISSION, FeeItem.STAMP_TAX,
        FeeItem.TRANSFER_FEE, FeeItem.HANDLING_FEE,
    }
    assert all(v == D("0") for v in trade.fees.values())
    # 幂等：同委托同日再撮合 → 同 trade_id
    assert engine.match(ctx)[1].trade_id == trade.trade_id


def test_buy_fill_sets_sellable_date_never_none():
    """BUY 成交必须带 ``sellable_date``（⛔ None 在 ledger 里 = 立即可卖 = T+1 失守）。"""
    _, buy, _ = MatchEngine().match(
        MatchContext(bar=_bar(_D2), order=_order(OrderSide.BUY, 100),
                     cash_available=D("100000")))
    assert buy.sellable_date is not None and buy.sellable_date > _D2
    # 显式注入优先
    _, buy2, _ = MatchEngine().match(
        MatchContext(bar=_bar(_D2), order=_order(OrderSide.BUY, 100, oid="O2"),
                     cash_available=D("100000"), sellable_date=_D3))
    assert buy2.sellable_date == _D3
    # SELL 不产生锁定批次
    _, sell, _ = MatchEngine().match(
        MatchContext(bar=_bar(_D2), order=_order(OrderSide.SELL, 100, oid="O3"),
                     sellable=100, position_volume=100))
    assert sell.sellable_date is None


def test_rule_order_limit_up_beats_cash_check():
    """规则顺序即契约：涨停（#2）先于资金（#7）—— 没钱也报涨停。"""
    result, _, reason = MatchEngine().match(
        MatchContext(bar=_bar(limit_up=True), order=_order(OrderSide.BUY, 100),
                     cash_available=D("0")))
    assert reason == REJECT_LIMIT_UP_BUY


# ======================================================================
# §8 Broker
# ======================================================================

def test_submit_moves_to_submitted_and_duplicate_oid_rejected():
    _, broker, _ = _make()
    order = _order(OrderSide.BUY, 100, oid="DUP", submitted=False)
    assert broker.submit(order).status is OrderStatus.SUBMITTED
    assert broker.pending_symbols() == {_A}
    with pytest.raises(BrokerError):
        broker.submit(_order(OrderSide.BUY, 100, oid="DUP", submitted=False))
    # 同一实例重复 submit → 状态机拦（SUBMITTED → SUBMITTED 非法）
    with pytest.raises(OrderStateError):
        broker.submit(order)


def test_cancel_submitted_and_missing():
    _, broker, _ = _make()
    order = broker.submit(_order(OrderSide.BUY, 100, oid="C1", submitted=False))
    assert broker.cancel("C1") is order
    assert order.status is OrderStatus.CANCELLED
    assert broker.pending_orders == []
    assert broker.cancel("C1") is None          # 已终态 → None
    assert broker.cancel("NOPE") is None        # 不存在 → None（⛔ 不 raise）


def test_on_bars_fill_updates_ledger_and_journal():
    """撮合成交 → TRADE 流水 + 现金/持仓更新 + 订单 FILLED。"""
    _, broker, _ = _make()
    broker.submit(_order(OrderSide.BUY, 1000, oid="B1", submitted=False))
    broker.on_bars(_D2, {_A: _bar(_D2, open_="10.10", close="10.50")})

    order = broker.orders[0]
    assert order.status is OrderStatus.FILLED
    assert order.filled_volume == 1000
    assert order.avg_fill_price == D("10.10")
    assert broker.book.cash == _INITIAL_CASH - D("10.10") * 1000
    assert broker.book.positions[_A].volume == 1000
    assert broker.book.positions[_A].sellable == 0        # T+1 未解禁
    assert len(broker.ledger.journal.filter_by_type(JournalType.TRADE)) == 1
    assert broker.pending_orders == []


def test_on_bars_reject_fills_reason_and_drops_pending():
    _, broker, _ = _make()
    broker.submit(_order(OrderSide.BUY, 100, oid="R1", submitted=False))
    broker.on_bars(_D2, {_A: _bar(_D2, limit_up=True)})
    order = broker.orders[0]
    assert order.status is OrderStatus.REJECTED
    assert order.reject_reason == REJECT_LIMIT_UP_BUY
    assert broker.pending_orders == []
    assert broker.trades == []


def test_settle_expires_unmatched_day_orders():
    """DAY 单纪律：当日没被撮合到（symbol 无行情且未撮合）→ 日终 EXPIRED。"""
    _, broker, _ = _make()
    broker.submit(_order(OrderSide.BUY, 100, oid="E1", submitted=False))
    broker.settle(_D2, bars={})       # 没走 on_bars → 残单过期
    assert broker.orders[0].status is OrderStatus.EXPIRED
    assert broker.pending_orders == []


def test_settle_advances_sellable_next_day():
    """T+1：买入当日 settle 后，下一交易日才可卖。"""
    _, broker, _ = _make()
    broker.submit(_order(OrderSide.BUY, 1000, oid="S1", submitted=False))
    bars_d2 = {_A: _bar(_D2, open_="10.10", close="10.50")}
    broker.on_bars(_D2, bars_d2)
    assert broker.book.positions[_A].sellable == 0
    broker.settle(_D2, bars=bars_d2)
    assert broker.book.positions[_A].sellable == 1000    # 截至 D3 开盘可卖


def test_settle_processes_exdiv_before_market_value_refresh():
    """除权先于刷市值（FR-BT-4）：10 送 10 ⇒ 股数 ×2、市值用除权后 close。"""
    _, broker, _ = _make()
    broker.submit(_order(OrderSide.BUY, 1000, oid="X1", submitted=False))
    bars_d2 = {_A: _bar(_D2, open_="10.00", close="10.00")}
    broker.on_bars(_D2, bars_d2)
    broker.settle(_D2, bars=bars_d2)
    cash_before = broker.book.cash

    # D3 除权：factor=2（10 送 10）+ 每股 0.1 元现金分红；除权后 close = 5.05
    bars_d3 = {_A: _bar(_D3, open_="5.00", close="5.05")}
    broker.on_bars(_D3, bars_d3)
    broker.settle(_D3, {_A: ExdivEvent(_A, D("2"), D("0.1"))}, bars=bars_d3)

    pos = broker.book.positions[_A]
    assert pos.volume == 2000
    assert broker.book.cash == cash_before + D("0.1") * 1000
    assert pos.market_value == D("5.05") * 2000
    entries = broker.ledger.journal.filter_by_type(JournalType.EXDIV_ADJUST)
    assert len(entries) == 1


def test_settle_writes_exactly_one_settle_entry_per_day():
    """⛔ 无双重结算：一天只写一条 SETTLE 流水。"""
    _, broker, _ = _make()
    broker.on_bars(_D2, {_A: _bar(_D2)})
    broker.settle(_D2, bars={_A: _bar(_D2)})
    entries = broker.ledger.journal.filter_by_type(JournalType.SETTLE)
    assert len(entries) == 1
    assert entries[0].meta["refreshed"] == []


# ======================================================================
# §9 settle_day
# ======================================================================

def test_settle_day_refreshes_present_and_freezes_missing():
    """bar 在 → 刷市值；bar 缺（停牌）→ 冻结不动，但仍算"受影响"。"""
    _, broker, _ = _make()
    broker.submit(_order(OrderSide.BUY, 1000, oid="P1", symbol=_A, submitted=False))
    broker.submit(_order(OrderSide.BUY, 100, oid="P2", symbol=_B, submitted=False))
    bars_d2 = {
        _A: _bar(_D2, symbol=_A, open_="10.00", close="10.00"),
        _B: _bar(_D2, symbol=_B, open_="20.00", close="20.00"),
    }
    broker.on_bars(_D2, bars_d2)
    broker.settle(_D2, bars=bars_d2)
    frozen_mv = broker.book.positions[_B].market_value

    # D3：A 有行情（close 11），B 停牌（缺席）
    affected = settle_day(
        broker.book, _D3, {_A: _bar(_D3, symbol=_A, close="11.00")})
    assert affected == {_A, _B}
    assert broker.book.positions[_A].market_value == D("11.00") * 1000
    assert broker.book.positions[_B].market_value == frozen_mv     # ⛔ 不清零
    assert broker.book.positions[_B].last_close == D("20.00")


def test_settle_day_detail_splits_refreshed_and_frozen():
    _, broker, _ = _make()
    broker.submit(_order(OrderSide.BUY, 100, oid="Q1", symbol=_A, submitted=False))
    bars = {_A: _bar(_D2, symbol=_A, open_="10.00", close="10.00")}
    broker.on_bars(_D2, bars)
    report = settle_day_detail(broker.book, _D2, bars)
    assert report.refreshed == frozenset({_A})
    assert report.frozen == frozenset()
    assert report.affected == frozenset({_A})
    assert report.nav == broker.book.cash + D("10.00") * 100

    report2 = settle_day_detail(broker.book, _D3, {})
    assert report2.frozen == frozenset({_A})
    assert report2.refreshed == frozenset()


def test_settle_day_nav_equals_cash_plus_market_value():
    _, broker, _ = _make()
    broker.submit(_order(OrderSide.BUY, 1000, oid="N1", symbol=_A, submitted=False))
    broker.submit(_order(OrderSide.BUY, 500, oid="N2", symbol=_B, submitted=False))
    bars = {
        _A: _bar(_D2, symbol=_A, open_="10.00", close="10.50"),
        _B: _bar(_D2, symbol=_B, open_="20.00", close="21.00"),
    }
    broker.on_bars(_D2, bars)
    settle_day(broker.book, _D2, bars)
    expected_cash = _INITIAL_CASH - D("10.00") * 1000 - D("20.00") * 500
    expected_mv = D("10.50") * 1000 + D("21.00") * 500
    assert broker.book.cash == expected_cash
    assert broker.book.total_market_value() == expected_mv
    assert broker.book.total_nav == expected_cash + expected_mv


# ======================================================================
# §10 引擎
# ======================================================================

def test_engine_simple_buy_position_and_nav():
    """首日下买单 → 次日开盘成交 → 现金减少 / 持仓增加 / NAV 正确。"""
    engine, broker, _ = _make()
    strategy = ScriptStrategy({_D1: [(OrderSide.BUY, 1000, _A)]})
    result = engine.run(strategy, "2024-03-01", "2024-03-06")

    assert isinstance(result, BacktestResult)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.date == _D2                  # ⛔ 不是 D1（当日信号不当日成交）
    assert trade.price == D("10.10")           # D2 开盘

    cash = _INITIAL_CASH - D("10.10") * 1000
    assert broker.book.cash == cash
    assert broker.book.positions[_A].volume == 1000
    # D1 还没成交 ⇒ 净值 = 期初现金
    assert result.nav_curve["2024-03-01"] == _INITIAL_CASH
    # D2 起 = 现金 + 1000 × 当日 close
    assert result.nav_curve["2024-03-04"] == cash + D("10.50") * 1000
    assert result.nav_curve["2024-03-05"] == cash + D("10.60") * 1000
    assert result.final_nav == cash + D("10.70") * 1000


def test_engine_nav_curve_keys_equal_trading_calendar():
    """揭账：``nav_curve`` 键集合 == 交易日历集合（⛔ 不跳日）。"""
    engine, _, feed = _make()
    result = engine.run(ScriptStrategy({}), "2024-03-01", "2024-03-06")
    assert set(result.nav_curve) == {d.isoformat() for d in _CALENDAR}
    assert list(result.nav_curve) == [d.isoformat() for d in _CALENDAR]  # 升序
    assert result.trading_dates == _CALENDAR
    assert feed.get_trading_dates(_D1, _D4) == _CALENDAR


def test_engine_bar_dates_in_ascending_order():
    """策略每日拿到的 bar 日期 == 当日日期，且日期严格递增（先 D1 再 D2）。"""
    engine, _, _ = _make()
    strategy = ScriptStrategy({})
    engine.run(strategy, "2024-03-01", "2024-03-06")
    assert strategy.seen_dates == _CALENDAR
    for day, bar_dates in zip(strategy.seen_dates, strategy.seen_bar_dates):
        assert set(bar_dates) == {day}


def test_engine_match_before_signal_no_same_day_fill():
    """**先撮合后信号**：D1 下的单在 D1 收盘时仍是 SUBMITTED，D2 才成交。"""
    engine, broker, _ = _make()

    class Probe(ScriptStrategy):
        def __init__(self):
            super().__init__({_D1: [(OrderSide.BUY, 1000, _A, "SIG")]})
            self.status_seen: list = []

        def on_bar(self, day, bars, book, broker_):
            # 进 on_bar 时先看昨天下的单是否已成交
            self.status_seen.append(
                (day, [(o.client_order_id, o.status) for o in broker_.orders]))
            super().on_bar(day, bars, book, broker_)

    probe = Probe()
    engine.run(probe, "2024-03-01", "2024-03-06")
    # D1 进 on_bar 时还没有订单；D2 进 on_bar 时 SIG 已 FILLED（当日 ② 撮合的）
    assert probe.status_seen[0] == (_D1, [])
    assert probe.status_seen[1] == (_D2, [("SIG", OrderStatus.FILLED)])
    assert broker.trades[0].date == _D2


def test_engine_multi_position_nav_equals_cash_plus_sum_mv():
    """多持仓：NAV = 现金 + Σ 市值。"""
    engine, broker, _ = _make()
    strategy = ScriptStrategy({
        _D1: [(OrderSide.BUY, 1000, _A, "MA"), (OrderSide.BUY, 500, _B, "MB")],
    })
    result = engine.run(strategy, "2024-03-01", "2024-03-06")
    assert len(result.trades) == 2
    cash = _INITIAL_CASH - D("10.10") * 1000 - D("20.20") * 500
    mv = D("10.70") * 1000 + D("21.40") * 500
    assert broker.book.cash == cash
    assert result.final_nav == cash + mv
    assert result.nav_curve["2024-03-06"] == cash + mv


def test_engine_limit_up_buy_rejected_smoke():
    """T202 子集 a：涨停日买入被拒（净值不变、无持仓）。"""
    frames = _default_frames()
    frames[_A] = _frame([
        _row(_D1, open_=10.00, close=10.00, preclose=10.00),
        # D2 一字涨停：+10%
        _row(_D2, open_=11.00, close=11.00, preclose=10.00),
        _row(_D3, open_=11.00, close=11.00, preclose=11.00),
        _row(_D4, open_=11.00, close=11.00, preclose=11.00),
    ])
    engine, broker, _ = _make(frames)
    result = engine.run(
        ScriptStrategy({_D1: [(OrderSide.BUY, 1000, _A, "LU")]}, watchlist=(_A,)),
        "2024-03-01", "2024-03-06")

    order = result.orders[0]
    assert order.status is OrderStatus.REJECTED
    assert order.reject_reason == REJECT_LIMIT_UP_BUY
    assert result.trades == []
    assert broker.book.positions.get(_A) is None
    assert set(result.nav_curve.values()) == {_INITIAL_CASH}   # 净值一条水平线


def test_engine_suspended_day_sell_rejected_and_value_frozen():
    """T202 子集 b：停牌日（无行）卖出被拒；已有持仓市值冻结 → NAV 水平线。"""
    frames = _default_frames()
    frames[_A] = _frame([
        _row(_D1, open_=10.00, close=10.00, preclose=10.00),
        _row(_D2, open_=10.00, close=10.00, preclose=10.00),
        # D3 停牌（无行）；D4 复牌
        _row(_D4, open_=12.00, close=12.00, preclose=10.00),
    ])
    engine, broker, _ = _make(frames)
    strategy = ScriptStrategy(
        {
            _D1: [(OrderSide.BUY, 1000, _A, "SB")],     # D2 成交
            _D2: [(OrderSide.SELL, 1000, _A, "SS")],    # D3 撮合 → 停牌被拒
        },
        watchlist=(_A,),
    )
    result = engine.run(strategy, "2024-03-01", "2024-03-06")

    sell = next(o for o in result.orders if o.client_order_id == "SS")
    assert sell.status is OrderStatus.REJECTED
    assert sell.reject_reason == REJECT_SUSPENDED
    # 持仓还在，D3 市值冻结在 D2 的 close 口径
    pos = broker.book.positions[_A]
    assert pos.volume == 1000
    cash = _INITIAL_CASH - D("10.00") * 1000
    assert result.nav_curve["2024-03-04"] == cash + D("10.00") * 1000
    assert result.nav_curve["2024-03-05"] == cash + D("10.00") * 1000   # 冻结
    assert result.nav_curve["2024-03-06"] == cash + D("12.00") * 1000   # 复牌刷新


def test_engine_t1_same_day_buy_then_sell_rejected():
    """T202 子集 c：T+1 —— 同一撮合日买入 + 卖出 ⇒ 卖出被拒（可卖为 0）。"""
    engine, broker, _ = _make()
    # 两单都在 D1 提交 ⇒ 都在 D2 撮合：买单先成交，卖单撞上 sellable=0
    strategy = ScriptStrategy(
        {_D1: [(OrderSide.BUY, 1000, _A, "T1B"), (OrderSide.SELL, 1000, _A, "T1S")]},
        watchlist=(_A,),
    )
    result = engine.run(strategy, "2024-03-01", "2024-03-06")

    buy = next(o for o in result.orders if o.client_order_id == "T1B")
    sell = next(o for o in result.orders if o.client_order_id == "T1S")
    assert buy.status is OrderStatus.FILLED
    assert sell.status is OrderStatus.REJECTED
    assert sell.reject_reason == REJECT_T1_INSUFFICIENT
    assert broker.book.positions[_A].volume == 1000
    # 次日（D3）已解禁 ⇒ 可卖
    assert broker.book.positions[_A].sellable == 1000


def test_engine_t1_sell_next_day_succeeds():
    """T+1 正向：D2 买入、D3 提交卖出 → D4 成交（可卖已解禁）。"""
    engine, broker, _ = _make()
    strategy = ScriptStrategy(
        {_D1: [(OrderSide.BUY, 1000, _A, "OKB")],
         _D3: [(OrderSide.SELL, 1000, _A, "OKS")]},
        watchlist=(_A,),
    )
    result = engine.run(strategy, "2024-03-01", "2024-03-06")
    sell = next(o for o in result.orders if o.client_order_id == "OKS")
    assert sell.status is OrderStatus.FILLED
    assert sell.fills[0].price == D("10.60")        # D4 开盘
    assert broker.book.positions[_A].volume == 0
    assert result.final_nav == broker.book.cash


def test_nav_visible_to_strategy_right_after_fill():
    """建仓当日策略进 on_bar 时看到的 NAV 必须已含新持仓市值（⛔ 不是"现金已扣、
    市值还是 0"的中间态）—— broker 用当日 bar.close 给新仓位播种 last_close。"""
    engine, _, _ = _make()
    strategy = ScriptStrategy({_D1: [(OrderSide.BUY, 1000, _A, "SEED")]},
                              watchlist=(_A,))
    engine.run(strategy, "2024-03-01", "2024-03-06")
    cash = _INITIAL_CASH - D("10.10") * 1000
    # navs[1] = D2 进 on_bar 时（撮合已完成、settle 未跑）看到的净值
    assert strategy.navs[1] == cash + D("10.50") * 1000
    # ⛔ 不是漏掉市值的那个错值
    assert strategy.navs[1] != cash


def test_engine_journal_entries_and_final_book_exposed():
    """产物完备：journal 含 CASH_IN + TRADE + 每日 SETTLE；final_book 就是活账本。"""
    engine, broker, _ = _make()
    result = engine.run(
        ScriptStrategy({_D1: [(OrderSide.BUY, 100, _A, "J1")]}, watchlist=(_A,)),
        "2024-03-01", "2024-03-06")
    types = [e.entry_type for e in result.journal_entries]
    assert types.count(JournalType.CASH_IN) == 1
    assert types.count(JournalType.TRADE) == 1
    assert types.count(JournalType.SETTLE) == len(_CALENDAR)
    assert result.journal is result.journal_entries
    assert result.final_book is broker.book
    assert result.nav_at(_D4) == result.final_nav
    with pytest.raises(KeyError):
        result.nav_at(date(2024, 3, 2))          # 非交易日


def test_engine_before_run_hook_deposits_cash():
    """``before_run`` 钩子：期初入金走 CASH_IN 流水（⛔ 不直接改 book.cash）。"""
    engine, broker, _ = _make(cash=D("0"))

    class Funded(ScriptStrategy):
        def before_run(self, broker_):
            broker_.deposit(D("50000"), date=_D1, ref_id="SEED")

    result = engine.run(Funded({}, watchlist=()), "2024-03-01", "2024-03-06")
    assert broker.book.cash == D("50000")
    assert result.nav_curve["2024-03-01"] == D("50000")
    cash_in = broker.ledger.journal.filter_by_type(JournalType.CASH_IN)
    assert [e.ref_id for e in cash_in] == ["SEED"]


def test_engine_requires_on_bar_and_rejects_inverted_range():
    engine, _, _ = _make()
    with pytest.raises(EngineError):
        engine.run(object(), "2024-03-01", "2024-03-06")
    with pytest.raises(EngineError):
        engine.run(ScriptStrategy({}), "2024-03-06", "2024-03-01")
    with pytest.raises(EngineError):
        engine.run(ScriptStrategy({}), "2024/03/01", "2024-03-06")


def test_engine_symbols_cover_positions_and_pending_without_watchlist():
    """取数范围必须覆盖持仓与挂单 —— 即使策略没给 watchlist。"""
    engine, broker, _ = _make()

    class NoWatch:
        """只有 on_bar，第一天靠自己指定 symbol 下单（watchlist 缺失）。"""

        def __init__(self):
            self.done = False

        def on_bar(self, day, bars, book, broker_):
            if not self.done:
                broker_.submit(_order(OrderSide.BUY, 1000, oid="NW",
                                      symbol=_A, created=day, submitted=False))
                self.done = True

    result = engine.run(NoWatch(), "2024-03-01", "2024-03-06")
    # D1 挂单 → D2 因"挂单标的"被取数并成交；此后持仓被取数 ⇒ 市值持续刷新
    assert len(result.trades) == 1
    assert result.nav_curve["2024-03-06"] == (
        _INITIAL_CASH - D("10.10") * 1000 + D("10.70") * 1000)
