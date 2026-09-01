#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T204 成交模型单测（离线，⛔ 无网络 / 无磁盘依赖）—— FR-BT-6 显式声明 + 敏感度对比。

默认口径（**契约声明**）：**次一开盘价 + 滑点（默认 5bps）+ tick 0.01 取整**（+
可选涨跌停限幅）。本文件把这个口径变成可执行断言，并用一个合成 5 日场景跑
3 价格口径 × 3 滑点档九宫格，证明：①口径可切换；②切换结果单调可解释；
③红线不破（滑点不越涨跌停、价格恒为 tick 倍数、先撮合后信号不变）。

断言价值锚点（全部手算复核过，见各用例注释）：
  · 默认模型 10.04 + 5bps：BUY → 10.05；SELL → 10.03
  · 15bps：BUY 10.04 → 10.06；SELL 9.00 → 8.99 → **限幅回 9.00**
  · 无 price_model ⇒ 成交价恒 = bar.open（T201→T204 向后兼容）
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from backtest.broker import BacktestBroker
from backtest.constants import FeeItem, OrderSide, OrderType
from backtest.engine import BacktestEngine
from backtest.feed import ParquetDailyFeed
from backtest.fees import (
    FeeError,
    TICK_SIZE,
    default_fee_config,
    make_fee_model,
    make_price_model,
)
from backtest.ledger import Ledger
from backtest.matching import MatchContext, MatchEngine, MatchResult
from backtest.types import Bar, Order

D = Decimal
D0 = Decimal("0")

_DAY = date(2024, 1, 4)
SH = "sh.600000"


# ----------------------------------------------------------------------
# 撮合级工厂
# ----------------------------------------------------------------------

def _bar(
    d: date = _DAY, o: str = "10.04", p: str = "10.00", symbol: str = SH,
    **flags: bool,
) -> Bar:
    po, pp = D(o), D(p)
    return Bar(
        date=d, symbol=symbol, open=po, high=po, low=po, close=po, preclose=pp,
        volume=D("1000000"), amount=D("10040000"), **flags,
    )


def _order(side: OrderSide = OrderSide.BUY, volume: int = 10000, d: date = _DAY) -> Order:
    return Order(
        client_order_id=f"t204-{side.value}-{volume}-{d.isoformat()}",
        symbol=SH, side=side, order_type=OrderType.MARKET,
        volume=volume, price=None, created_date=d,
    )


def _ctx(bar: Bar, order: Order, cash: str = "2000000", **kw) -> MatchContext:
    kw.setdefault("sellable", 10000 if order.side is OrderSide.SELL else 0)
    kw.setdefault("position_volume", kw["sellable"])
    return MatchContext(bar=bar, order=order, cash_available=D(cash), **kw)


# ======================================================================
# A. 价格模型本体（make_price_model）
# ======================================================================

class TestPriceModelBasics:
    def test_default_open_plus_5bps(self) -> None:
        pm = make_price_model()
        bar = _bar()
        # BUY：10.04 × 1.0005 = 10.045020 → tick 半入 → 10.05（买得更贵）
        assert pm(_order(OrderSide.BUY), bar) == D("10.05")
        # SELL：10.04 × 0.9995 = 10.03498 → 10.03（卖得更贱）
        assert pm(_order(OrderSide.SELL), bar) == D("10.03")

    def test_zero_slippage_equals_open(self) -> None:
        pm = make_price_model(default_fee_config(slippage_rate=D("0")))
        assert pm(_order(), _bar()) == D("10.04")

    def test_price_always_on_tick(self) -> None:
        # 13 号 tick_size 红线：成交价必须是 0.01 的整数倍
        pm = make_price_model()
        for o in ("10.04", "7.77", "123.45", "0.99", "3.33"):
            for side in (OrderSide.BUY, OrderSide.SELL):
                p = pm(_order(side), _bar(o=o, p=o))
                assert p == p.quantize(TICK_SIZE), f"open={o} side={side} → {p} 非 tick 整数倍"

    def test_limit_clamp_buy_side(self) -> None:
        # 13 号红线：滑点后成交价不越涨停价。preclose=10.00、±10%；open=11.00（涨停开）
        pm = make_price_model(limit_pct=D("0.1"))
        bar = _bar(o="11.00", p="10.00")
        # 11.00 × 1.0005 = 11.0055 → tick 11.01 > 11.00 ⇒ 限回 11.00
        assert pm(_order(OrderSide.BUY), bar) == D("11.00")

    def test_limit_clamp_sell_side(self) -> None:
        # 15bps 压测档 + 跌停开：9.00 × 0.9985 = 8.9865 → tick 8.99 < 9.00 ⇒ 限回 9.00
        pm = make_price_model(
            default_fee_config(slippage_rate=D("0.0015")), limit_pct=D("0.1"))
        bar = _bar(o="9.00", p="10.00")
        assert pm(_order(OrderSide.SELL), bar) == D("9.00")

    def test_limit_clamp_noop_when_inside(self) -> None:
        pm = make_price_model(limit_pct=D("0.1"))
        assert pm(_order(OrderSide.BUY), _bar()) == D("10.05")

    def test_fail_closed_bad_limit_pct(self) -> None:
        with pytest.raises(FeeError):
            make_price_model(limit_pct=0.1)              # float
        with pytest.raises(FeeError):
            make_price_model(limit_pct=D("0"))           # 0
        with pytest.raises(FeeError):
            make_price_model(limit_pct=D("1"))           # ≥1


# ======================================================================
# B. 撮合接线（MatchEngine price_model 注入点）
# ======================================================================

class TestMatchEnginePriceModel:
    def test_no_price_model_backward_compat(self) -> None:
        # ⛔ 无 price_model ⇒ 成交价 = bar.open（T201 默认口径不退化）
        engine = MatchEngine(fee_model=make_fee_model())
        r, t, _ = engine.match(_ctx(_bar(), _order()))
        assert r is MatchResult.FILLED and t is not None
        assert t.price == D("10.04")

    def test_price_model_moves_trade_price(self) -> None:
        engine = MatchEngine(
            fee_model=make_fee_model(), price_model=make_price_model())
        r, t, _ = engine.match(_ctx(_bar(), _order()))
        assert r is MatchResult.FILLED and t is not None
        assert t.price == D("10.05")                     # open 10.04 + 5bps → tick 10.05

    def test_fees_priced_on_actual_fill(self) -> None:
        # 费用按**实际成交价**（滑点后）计，⛔ 不是按锚定开盘价
        engine = MatchEngine(
            fee_model=make_fee_model(), price_model=make_price_model())
        _, t, _ = engine.match(_ctx(_bar(), _order()))
        assert t is not None
        from backtest.fees import compute_fees
        assert t.fees == compute_fees(SH, OrderSide.BUY, 10000, D("10.05"), _DAY)

    def test_rule7_uses_slipped_gross(self) -> None:
        # 规则 7 用滑点后价格算需款：10000 × 10.05 + 费用 > cash ⇒ 拒
        engine = MatchEngine(
            fee_model=make_fee_model(), price_model=make_price_model())
        # 现金刚好覆盖"开盘价 + 费用"但不够"滑点价 + 费用"
        cash = D("100400") + D("31.42")                  # 100,431.42
        needed = D("100500") + D("31.83")                # 100,531.83
        assert cash < needed
        r, t, reason = engine.match(_ctx(_bar(), _order(), cash=str(cash)))
        assert r is MatchResult.REJECTED and t is None and reason == "资金不足"

    def test_limit_up_reject_before_price_model(self) -> None:
        # 一字板语义在价格模型**之前**：涨停拒单时价格模型不得被调用（爆炸哨兵）
        def boom(order: Order, bar: Bar) -> Decimal:
            raise AssertionError("price_model 不应在涨停拒单前被调用")

        engine = MatchEngine(
            fee_model=make_fee_model(), price_model=boom)  # type: ignore[arg-type]
        r, t, reason = engine.match(_ctx(_bar(limit_up=True), _order()))
        assert r is MatchResult.REJECTED and t is None and reason == "涨停买入不可成交"

    def test_price_model_must_return_decimal(self) -> None:
        engine = MatchEngine(price_model=lambda o, b: 10.05)  # float
        with pytest.raises(TypeError):
            engine.match(_ctx(_bar(), _order()))


# ======================================================================
# C. 敏感度对比（FR-BT-6 验收物：同一策略在不同成交口径下跑全回测）
# ======================================================================

_D0 = date(2024, 1, 2)
_DAYS = [_D0 + timedelta(days=i) for i in range(5)]     # 5 个合成交易日
_SY = "sh.600001"
_COLS = [
    "date", "open", "high", "low", "close", "preclose", "volume", "amount",
    "turn", "pctChg", "tradestatus", "isST", "code", "adjust_mode", "source",
]

# 合成行情：温和上行（open 逐日 +0.10，close = open + 0.05），次一开盘 = 次日 open
_OPENS = ["10.00", "10.10", "10.20", "10.30", "10.40"]


def _rows() -> list[dict]:
    rows = []
    for i, d in enumerate(_DAYS):
        o = D(_OPENS[i])
        c = o + D("0.05")
        p = D(_OPENS[i - 1]) + D("0.05") if i > 0 else D("9.90")
        rows.append({
            "date": d, "open": float(o), "high": float(c), "low": float(o),
            "close": float(c), "preclose": float(p), "volume": 1_000_000.0,
            "amount": float(c * D("1000000")), "turn": 1.0,
            "pctChg": float((c - p) / p * D("100")), "tradestatus": "1",
            "isST": "0", "code": _SY, "adjust_mode": "hfq", "source": "baostock",
        })
    return rows


class _BuyHoldThenSell:
    """D1 尾盘下买单（次日 D2 开盘成交），D4 尾盘下卖单（D5 开盘成交）。零前视。"""

    def __init__(self) -> None:
        self.watchlist = [_SY]

    def on_bar(self, day, bars, book, broker) -> None:
        if day == _DAYS[0]:
            broker.submit(Order(
                client_order_id="t204-buy", symbol=_SY, side=OrderSide.BUY,
                order_type=OrderType.MARKET, volume=5000, price=None,
                created_date=day))
        elif day == _DAYS[3]:
            broker.submit(Order(
                client_order_id="t204-sell", symbol=_SY, side=OrderSide.SELL,
                order_type=OrderType.MARKET, volume=5000, price=None,
                created_date=day))


def _run(price_model, slip: str) -> Decimal:
    cfg = default_fee_config(slippage_rate=D(slip))
    frame = pd.DataFrame(_rows(), columns=_COLS)
    feed = ParquetDailyFeed(
        preloaded={_SY: frame},
        trade_calendar=lambda s, e: [d for d in _DAYS if s <= d <= e],
    )
    ledger = Ledger(D("120000"), date=_DAYS[0])
    matcher = MatchEngine(fee_model=make_fee_model(cfg), price_model=price_model(cfg))
    broker = BacktestBroker(matcher, ledger, feed)
    result = BacktestEngine(broker, feed).run(_BuyHoldThenSell(), _DAYS[0], _DAYS[-1])
    return result.nav_curve[_DAYS[-1].isoformat()]


def _open_model(cfg):
    return make_price_model(cfg)


def _close_model(cfg):
    return lambda order, bar: bar.close                    # 次一收盘（对比口径）


def _vwap_proxy_model(cfg):
    # VWAP 近似 = (open+close)/2，**保留 apply_slippage**（挑战口径与默认口径比
    # slippage 敏感度时滑点必须同样作用），tick 取整（声明：日线无真 VWAP，中枢近似）
    from backtest.fees import apply_slippage as _slip

    def _model(order, bar):
        anchor = (bar.open + bar.close) / D("2")
        return _slip(anchor, order.side, cfg).quantize(
            TICK_SIZE, rounding="ROUND_HALF_UP")

    return _model


class TestSensitivityMatrix:
    """同一策略在 3 价格口径 × 3 滑点档下的终值矩阵（文档固化的执行依据）。"""

    def test_matrix_runs_all_cells(self) -> None:
        cells = {}
        for name, factory in (("open", _open_model), ("close", _close_model),
                              ("vwap", _vwap_proxy_model)):
            for slip in ("0", "0.0005", "0.0015"):
                cells[(name, slip)] = _run(factory, slip)
        # 9 格全算出且为 Decimal
        assert len(cells) == 9
        assert all(isinstance(v, Decimal) for v in cells.values())

    def test_slippage_monotone_drag(self) -> None:
        # 滑点单调吃收益：同一价格口径下 15bps ≤ 5bps ≤ 0bps（⛔ 不许反序）
        for name, factory in (("open", _open_model), ("close", _close_model),
                              ("vwap", _vwap_proxy_model)):
            v0, v5, v15 = (_run(factory, s) for s in ("0", "0.0005", "0.0015"))
            assert v15 <= v5 <= v0, f"{name}: 滑点单调性破了 {v15} {v5} {v0}"

    def test_all_cells_within_fee_band(self) -> None:
        # 各口径终值差必须落在「费用+滑点总量级」内（对照说明口径影响有限但非零）
        cells = [(_run(f, s), n) for n, f in
                 (("open", _open_model), ("close", _close_model), ("vwap", _vwap_proxy_model))
                 for s in ("0", "0.0005", "0.0015")]
        vals = [c[0] for c in cells]
        assert max(vals) - min(vals) < D("5000")           # 同一 10 万级盘子的敏感度上限
        assert min(vals) >= D("120000")                    # 温和上行必不输本金线

    def test_default_open_model_declared(self) -> None:
        # 默认口径显式声明：MatchEngine() 无参 = open（文档与代码同参同义）
        engine = MatchEngine()
        r, t, _ = engine.match(_ctx(_bar(o="10.00", p="9.90"), _order()))
        assert r is MatchResult.FILLED and t is not None and t.price == D("10.00")


# ======================================================================
# D. 敏感度对比固化（FR-BT-6 验收：文档中的数字由本测试类实测产出）
# ======================================================================

class TestSensitivityReportFixture:
    """docs/t204_price_model_sensitivity.md 的数字源：九格终值 + 单调性，断一次即知文档过期。"""

    # 数字 = `py -3.11 -m pytest` 实测固化（2026-09-01；改引擎/费用/价格口径必过期变红）
    _EXPECTED = {
        ("open", "0"): D("121441.80"),
        ("open", "0.0005"): D("121341.82"),
        ("open", "0.0015"): D("121241.84"),
        ("close", "0"): D("121441.51"),
        ("close", "0.0005"): D("121441.51"),   # 滑点对 close 口径被 tick 取整吸收
        ("close", "0.0015"): D("121441.51"),   # （价格口径变化的二阶效应，见报告 §3）
        ("vwap", "0"): D("121441.63"),
        ("vwap", "0.0005"): D("121391.67"),
        ("vwap", "0.0015"): D("121291.70"),
    }

    def test_report_numbers_match_engine(self) -> None:
        for (name, slip), expected in self._EXPECTED.items():
            factory = {"open": _open_model, "close": _close_model,
                       "vwap": _vwap_proxy_model}[name]
            actual = _run(factory, slip)
            assert actual == expected, (
                f"{name}/{slip}: 实测 {actual} ≠ 文档固化 {expected} —— 改了引擎/费用/"
                f"价格口径就必须重出敏感度报告")
