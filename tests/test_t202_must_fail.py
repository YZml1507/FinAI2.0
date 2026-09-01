#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T202 五必挂用例验收测试（13 号文档 / T201_design.md §11 锚点表）。

⛔ 本文件**只驱动引擎公开接口**，不含任何实现逻辑、不 monkeypatch backtest/*：
   ``ParquetDailyFeed(preloaded=...)`` 内存帧 + mock 交易日历 ⇒ 纯离线、无网络、
   无磁盘。期初资金一律经 ``Ledger`` 的 ``CASH_IN`` 流水注入（⛔ 不直接改
   ``book.cash``）。金额断言全部 ``Decimal``（⛔ 无 float 参与比较）。

锚点对照（T201_design.md §11）：

  | # | IEEE        | 引擎行为锚点                                           | 本文件 |
  |---|-------------|--------------------------------------------------------|--------|
  | 1 | 涨停买入    | matching rule#2 → REJECTED                             | ``TestCase1LimitUpBuy`` |
  | 2 | 跌停卖出    | matching rule#3 → REJECTED；净值含未卖出持仓           | ``TestCase2LimitDownSell`` |
  | 3 | 停牌日下单  | feed 键缺席 → REJECTED；settle 市值冻结 ⇒ NAV 水平线    | ``TestCase3SuspendedDay`` |
  | 4 | 除权日持仓  | settle.exdiv → volume×factor + cash += dividend；NAV 无跳变 | ``TestCase4ExdivDay`` |
  | 5 | T+1 当日买卖 | matching rule#4 sellable 校验 → REJECTED               | ``TestCase5T1SameDay`` |

⭐ **时间轴口径（读断言前必须先懂这条，否则会误判用例写错）**：引擎的日内顺序是
   "② 先撮合 → ③ 后信号"（``engine.py`` L143-152，零前视的结构性保证）。所以
   **策略在 T 日 ``on_bar`` 里下的单，撮合发生在 T+1 交易日**，成交价 = T+1 的
   ``bar.open``。因此 13 号文档里"D1 下单 / D1 被拒"这类表述，在本引擎语义下
   落成"**D0 下单 → D1 撮合被拒**"—— 被拒当日就是那根异常 bar 所在的交易日，
   语义完全一致，只是下单动作前移一个交易日。每个用例的 docstring 都把
   given/when/then 与实际下单/撮合日逐条写清。

⚠ 与 13 号文档措辞的两处口径澄清（引擎既有实现口径，本文件按引擎实现断言）：
   ① 用例 4 的"10 送 10 → factor=0.5"是**价格**折算因子；引擎
      ``ledger.process_exdiv`` 的 ``factor`` 是**股数**因子（``volume' =
      volume × factor``，见 ``ledger.py`` L449 与 ``settle.ExdivEvent`` docstring：
      "10 送 10 → Decimal('2')"）。故本文件传 ``factor=Decimal("2")``，断言
      ``volume 100 → 200`` / ``avg_cost 10.00 → 5.00``，与文档预期结果一致。
   ② 用例 5 场景 A 的"买入成交日当天再下卖单"：在本引擎里当天下的卖单要到**次日**
      才撮合，那时 T+1 已解禁（``settle`` 末尾 ``advance_sellable``）⇒ 会成交。
      真正的"当日买卖"是**同一撮合日**买卖两单同时到场（同日提交 ⇒ 同日撮合），
      买单先成交、卖单撞上 ``sellable == 0`` 被拒 —— 这才是 rule#4 的锚点，
      场景 A 按此实现。
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

#: 主板标的（±10% 档，``BOARD_LIMIT_PCT`` 前缀 "60" → ``main_pct``）。
SYMBOL = "sh.600000"

#: 交易日历（含一个自然日跳档 03-01 → 03-04，顺带覆盖"周末不提前解禁 T+1"）。
_D1 = date(2024, 3, 1)
_D2 = date(2024, 3, 4)
_D3 = date(2024, 3, 5)
_D4 = date(2024, 3, 6)
CALENDAR = [_D1, _D2, _D3, _D4]

#: 期初资金（系统配置：初始资金 10–15 万 RMB）。
INITIAL_CASH = D("120000")

#: T105 落盘列（⛔ 无 limit_up / limit_down / exdiv —— 那三列由 feed 经
#: ``data.cleaner`` 派生，测试里伪造它们等于绕过被验收的判定逻辑）。
_COLS = (
    "date", "open", "high", "low", "close", "preclose",
    "volume", "amount", "turn", "pctChg",
    "tradestatus", "isST", "code", "adjust_mode", "source",
)


# ---------------------------------------------------------------------- 造数

def _row(
    d: date,
    *,
    open_: str,
    close: str,
    preclose: str,
    code: str = SYMBOL,
) -> dict:
    """一行 T105 落盘形状的日线记录。

    价格先用 ``Decimal`` 算准（涨跌停靠 ``close/preclose`` 判定，⛔ 不能让浮点
    噪声决定触板），落帧时才转 float 模拟真实 parquet 的 float64 列。
    停牌**不是** ``tradestatus='0'`` 的行 —— 停牌 = 该日**无行**（feed 契约 §6-1）。
    """
    o, c, p = D(open_), D(close), D(preclose)
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
        "pctChg": float((c - p) / p * D("100")),
        "tradestatus": "1",          # 正常交易（落盘只留 '1'，R1）
        "isST": "0",
        "code": code,
        "adjust_mode": "hfq",
        "source": "baostock",
    }


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(_COLS))


def _calendar_fn(dates: list[date]):
    """mock 交易日历（⛔ 未注入日历时 feed 会 raise，不静默打网）。"""
    return lambda start, end: [d for d in dates if start <= d <= end]


def _make(
    frame: pd.DataFrame,
    *,
    calendar: list[date] | None = None,
    cash: Decimal = INITIAL_CASH,
) -> tuple[BacktestEngine, BacktestBroker]:
    """装一套 feed + ledger + broker + engine（全离线）。"""
    days = calendar or CALENDAR
    feed = ParquetDailyFeed(
        preloaded={SYMBOL: frame},
        trade_calendar=_calendar_fn(days),
    )
    ledger = Ledger(cash, date=days[0])
    broker = BacktestBroker(MatchEngine(), ledger, feed)
    return BacktestEngine(broker, feed), broker


class ScriptStrategy:
    """脚本化策略（鸭子类型，⛔ 不继承基类）。

    ``script = {下单日: [(side, volume, client_order_id), ...]}``；
    ``exdiv = {除权日: {symbol: ExdivEvent}}`` 经可选钩子 ``exdiv_events_for``
    交给引擎（``engine._exdiv_for`` → ``broker.settle`` → ``ledger.process_exdiv``）。
    """

    def __init__(
        self,
        script: dict[date, list[tuple]],
        *,
        exdiv: dict[date, dict[str, ExdivEvent]] | None = None,
    ) -> None:
        self.script = script
        self.exdiv = exdiv or {}
        self.watchlist = [SYMBOL]
        #: 每个交易日进 on_bar 时看到的 NAV（撮合后、settle 前）
        self.navs: dict[date, Decimal] = {}

    def on_bar(self, day, bars, book, broker):
        self.navs[day] = book.total_nav
        for side, volume, oid in self.script.get(day, []):
            broker.submit(
                Order(
                    client_order_id=oid,
                    symbol=SYMBOL,
                    side=side,
                    order_type=OrderType.MARKET,
                    volume=volume,
                    price=None,
                    created_date=day,
                )
            )

    def exdiv_events_for(self, day):
        return self.exdiv.get(day)


def _settle_meta(result, day: date) -> dict:
    """取某交易日 SETTLE 流水的 meta 快照（含 ``nav`` / ``cash`` / ``market_value``）。

    ⭐ 断言**某一日的**账本状态必须读这里，⛔ 不能读跑完回测后的 ``broker.book``
    —— 那是最后一个交易日的状态（``BookView`` 是推导态，会被后续日覆盖）。
    """
    from backtest.ledger import JournalType

    matched = [
        e for e in result.journal_entries
        if e.entry_type is JournalType.SETTLE and e.date == day
    ]
    assert len(matched) == 1, (
        f"{day} 应恰有一条 SETTLE 流水，实际 {len(matched)} 条")
    return matched[0].meta


def _order_by_id(result, oid: str) -> Order:
    """按 client_order_id 取订单（缺失即 AssertionError，⛔ 不返回 None 让断言散开）。"""
    matched = [o for o in result.orders if o.client_order_id == oid]
    assert matched, f"订单 {oid} 不在 result.orders 里（实际：" \
                    f"{[o.client_order_id for o in result.orders]}）"
    return matched[0]


# ======================================================================
# 用例 1：涨停买入（FR-BT-1 / §11 锚点 #1）
# ======================================================================

class TestCase1LimitUpBuy:
    """涨停日买入不可成交。

    given
      · ``sh.600000``（主板 ±10%）。
      · D1=2024-03-01：``close == preclose == 10.00``（平盘）。
      · D2=2024-03-04：``preclose=10.00, open=11.00, close=11.00``
        —— 一字涨停：涨幅正好 +10%、开盘即收盘、全天封板。
      · 帧经 ``ParquetDailyFeed(preloaded=...)`` 注入；``limit_up`` 由
        ``data.cleaner.mark_limit_flags`` 从 ``close/preclose`` 派生（⛔ 不手工造列）。

    when
      · 策略在 **D1** ``on_bar`` 里下市价买单 ``volume=100``（"先撮合后信号" ⇒
        该单的撮合机会在 D2）。

    then
      a. D2 撮合时该订单 ``REJECTED``，``reject_reason`` == ``涨停买入不可成交``
         （含"涨停"）。
      b. ``trades`` 里**没有**该 symbol 的成交。
      c. ``NAV(D2) == NAV(D1)``：无持仓、现金未动 ⇒ 整条净值曲线是水平线。
    """

    @staticmethod
    def _run():
        frame = _frame([
            _row(_D1, open_="10.00", close="10.00", preclose="10.00"),
            # D2 一字涨停：(11.00 - 10.00) / 10.00 = +10.00%
            _row(_D2, open_="11.00", close="11.00", preclose="10.00"),
            _row(_D3, open_="11.00", close="11.00", preclose="11.00"),
            _row(_D4, open_="11.00", close="11.00", preclose="11.00"),
        ])
        engine, broker = _make(frame)
        strategy = ScriptStrategy({_D1: [(OrderSide.BUY, 100, "C1-BUY")]})
        return engine.run(strategy, _D1, _D4), broker

    def test_a_order_rejected_with_limit_up_reason(self):
        """then a：REJECTED + reject_reason 含"涨停"。"""
        result, _ = self._run()
        order = _order_by_id(result, "C1-BUY")
        assert order.status is OrderStatus.REJECTED, (
            f"涨停日买单必须被拒，实际状态 {order.status}")
        assert order.reject_reason == REJECT_LIMIT_UP_BUY, (
            f"拒绝理由须为 {REJECT_LIMIT_UP_BUY!r}，实际 {order.reject_reason!r}")
        assert "涨停" in order.reject_reason
        assert order.filled_volume == 0

    def test_b_no_trade_for_symbol(self):
        """then b：trades 里没有该 symbol 的成交。"""
        result, _ = self._run()
        assert [t for t in result.trades if t.symbol == SYMBOL] == [], (
            f"涨停日不该有成交，实际 {result.trades}")
        assert result.trades == []

    def test_c_nav_flat_and_cash_untouched(self):
        """then c：NAV(D2) == NAV(D1)，无持仓、现金未动。"""
        result, broker = self._run()
        nav_d1 = result.nav_at(_D1)
        nav_d2 = result.nav_at(_D2)
        assert nav_d1 == INITIAL_CASH, f"D1 净值应为期初资金，实际 {nav_d1}"
        assert nav_d2 == nav_d1, f"涨停被拒 ⇒ D2 净值应与 D1 相同，实际 {nav_d2}"
        assert set(result.nav_curve.values()) == {INITIAL_CASH}, (
            f"整条净值曲线应为水平线 {INITIAL_CASH}，实际 {result.nav_curve}")
        assert broker.book.cash == INITIAL_CASH
        assert broker.book.positions.get(SYMBOL) is None, "被拒的买单不该建仓"


# ======================================================================
# 用例 2：跌停卖出（FR-BT-2 / §11 锚点 #2）
# ======================================================================

class TestCase2LimitDownSell:
    """跌停日卖出不可成交，持仓与现金原样保留。

    given
      · D1 下买单 → **D2 成交 100 股** @ ``D2.open = 10.00``（预先建仓）。
      · D3=2024-03-05 跌停：``preclose=10.00, open=close=9.00`` ⇒ 跌幅 -10.00%。
      · T+1：买入成交日（D2）日终 ``advance_sellable`` 已解禁 ⇒ D3 撮合时
        ``sellable == 100``，所以被拒**只可能**来自 rule#3（跌停），
        ⛔ 不是 rule#4（可卖不足）—— 断言直接钉死拒绝理由排除混淆。

    when
      · 策略在 **D2**（持仓已到手当日）下市价卖单 ``volume=100`` ⇒ D3 撮合。

    then
      · 订单 ``REJECTED``，理由 == ``跌停卖出不可成交``。
      · 无卖出成交（``trades`` 只有那一笔买入）。
      · 持仓仍为 100 股。
      · NAV 反映市值缩水（``9.00 × 100 = 900``）但现金未变
        （``120000 - 10.00 × 100 = 119000`` ⇒ ``NAV(D3) = 119900``）。
    """

    @staticmethod
    def _run():
        frame = _frame([
            _row(_D1, open_="10.00", close="10.00", preclose="10.00"),
            _row(_D2, open_="10.00", close="10.00", preclose="10.00"),
            # D3 一字跌停：(9.00 - 10.00) / 10.00 = -10.00%
            _row(_D3, open_="9.00", close="9.00", preclose="10.00"),
            _row(_D4, open_="9.00", close="9.00", preclose="9.00"),
        ])
        engine, broker = _make(frame)
        strategy = ScriptStrategy({
            _D1: [(OrderSide.BUY, 100, "C2-BUY")],    # D2 成交
            _D2: [(OrderSide.SELL, 100, "C2-SELL")],  # D3 撮合 → 跌停被拒
        })
        return engine.run(strategy, _D1, _D4), broker

    def test_sell_rejected_with_limit_down_reason(self):
        """REJECTED + 理由 == 跌停卖出不可成交（⛔ 不是 T+1 可卖不足）。"""
        result, broker = self._run()
        buy = _order_by_id(result, "C2-BUY")
        sell = _order_by_id(result, "C2-SELL")
        assert buy.status is OrderStatus.FILLED, "前置建仓买单必须先成交"
        assert sell.status is OrderStatus.REJECTED, (
            f"跌停日卖单必须被拒，实际 {sell.status}")
        assert sell.reject_reason == REJECT_LIMIT_DOWN_SELL, (
            f"拒绝理由须为 {REJECT_LIMIT_DOWN_SELL!r}（rule#3），"
            f"实际 {sell.reject_reason!r}")
        assert "跌停" in sell.reject_reason
        # 排除"其实是 T+1 拦下的"这种假绿：D2 日终已解禁 ⇒ D3 撮合时可卖 100
        assert sell.reject_reason != REJECT_T1_INSUFFICIENT
        assert broker.book.positions[SYMBOL].sellable == 100, (
            "撮合当日可卖数应为 100（T+1 已解禁），否则拒因不能归给跌停")

    def test_no_sell_trade_and_position_kept(self):
        """无卖出成交；持仓 still 100。"""
        result, broker = self._run()
        sells = [t for t in result.trades if t.side is OrderSide.SELL]
        assert sells == [], f"跌停日不该有卖出成交，实际 {sells}"
        assert len(result.trades) == 1, f"只该有那笔买入，实际 {result.trades}"
        pos = broker.book.positions[SYMBOL]
        assert pos.volume == 100, f"持仓应保持 100，实际 {pos.volume}"

    def test_nav_reflects_shrunk_market_value_cash_unchanged(self):
        """NAV 含未卖出持仓的缩水市值（9.00×100），现金未变。"""
        result, broker = self._run()
        cash_after_buy = INITIAL_CASH - D("10.00") * 100        # 119000
        assert broker.book.cash == cash_after_buy, (
            f"跌停被拒 ⇒ 现金不该变动，应为 {cash_after_buy}，实际 {broker.book.cash}")
        # 读 D3 的 SETTLE 快照（⛔ 不读跑完后的 book —— 那是 D4 状态）
        settle_d3 = _settle_meta(result, _D3)
        assert settle_d3["market_value"] == D("9.00") * 100, (
            f"D3 市值应为 900，实际 {settle_d3['market_value']}")
        assert settle_d3["cash"] == cash_after_buy
        assert result.nav_at(_D3) == cash_after_buy + D("900"), (
            f"NAV(D3) 应为 {cash_after_buy + D('900')}，实际 {result.nav_at(_D3)}")
        assert result.nav_at(_D2) == cash_after_buy + D("10.00") * 100
        assert result.nav_at(_D3) < result.nav_at(_D2), "跌停日净值必须体现缩水"


# ======================================================================
# 用例 3：停牌日下单（FR-BT-3 / §11 锚点 #3）
# ======================================================================

class TestCase3SuspendedDay:
    """停牌日下单被拒；净值冻结为水平线，⛔ 不虚构价格。

    given
      · D1 下买单 → **D2 成交 100 股** @ ``D2.open = 10.00``（D2 收 10.50）。
      · **D3 停牌**：该 symbol 该日在 preloaded 帧里**无行**（feed 契约"停牌 =
        键缺席"，⛔ 不是 ``tradestatus='0'`` 的脏行、⛔ 不前向填充价格）。
      · D4 复牌正常：``preclose=10.50, open=close=11.00``。

    when
      · 策略在 **D2** 下市价卖单 ⇒ D3 撮合。

    then
      · 订单 ``REJECTED``，理由 == ``停牌不可下单``；无成交。
      · **更关键**：``NAV(D3) == NAV(D2)`` —— 停牌日市值冻结（沿用 D2 的
        ``last_close``），净值是水平线。
      · D4 复牌后按 ``D4.close = 11.00`` 重新计市值 ⇒ ``NAV(D4) = 119000 + 1100``。
    """

    @staticmethod
    def _run():
        frame = _frame([
            _row(_D1, open_="10.00", close="10.00", preclose="10.00"),
            _row(_D2, open_="10.00", close="10.50", preclose="10.00"),
            # ⛔ D3 无行 = 停牌
            _row(_D4, open_="11.00", close="11.00", preclose="10.50"),
        ])
        engine, broker = _make(frame)
        strategy = ScriptStrategy({
            _D1: [(OrderSide.BUY, 100, "C3-BUY")],    # D2 成交
            _D2: [(OrderSide.SELL, 100, "C3-SELL")],  # D3 撮合 → 停牌被拒
        })
        return engine.run(strategy, _D1, _D4), broker

    def test_sell_rejected_as_suspended(self):
        """停牌日（bar 键缺席）卖出 → REJECTED("停牌不可下单")，无成交。"""
        result, _ = self._run()
        sell = _order_by_id(result, "C3-SELL")
        assert sell.status is OrderStatus.REJECTED, (
            f"停牌日卖单必须被拒，实际 {sell.status}")
        assert sell.reject_reason == REJECT_SUSPENDED, (
            f"拒绝理由须为 {REJECT_SUSPENDED!r}，实际 {sell.reject_reason!r}")
        assert [t for t in result.trades if t.side is OrderSide.SELL] == []

    def test_nav_frozen_on_suspended_day(self):
        """NAV(D3) == NAV(D2)：停牌日净值冻结为水平线（⛔ 不虚构价格）。"""
        result, broker = self._run()
        cash = INITIAL_CASH - D("10.00") * 100                  # 119000
        nav_d2 = result.nav_at(_D2)
        assert nav_d2 == cash + D("10.50") * 100, (
            f"NAV(D2) 应为 {cash + D('10.50') * 100}，实际 {nav_d2}")
        assert result.nav_at(_D3) == nav_d2, (
            f"停牌日 NAV 必须冻结等于前一交易日，D2={nav_d2} 实际 D3="
            f"{result.nav_at(_D3)}")
        # 冻结口径 = 沿用 D2 的 last_close，⛔ 不是清零、也不是别的价。
        # ⚠ 这里读 D3 的 SETTLE 流水快照，⛔ 不读 broker.book —— 回测跑完后
        # book 已是 D4 复牌态（那会是本断言的假红）。
        settle_d3 = _settle_meta(result, _D3)
        assert settle_d3["market_value"] == D("10.50") * 100, (
            f"停牌日市值应冻结在 1050（沿用 D2 close），实际 "
            f"{settle_d3['market_value']}")
        assert settle_d3["cash"] == cash, "停牌被拒 ⇒ 现金不动"
        assert broker.book.positions[SYMBOL].volume == 100, "持仓不该消失"

    def test_nav_refreshed_after_resume(self):
        """D4 复牌 → 按 D4.close 重新计市值。"""
        result, broker = self._run()
        cash = INITIAL_CASH - D("10.00") * 100
        assert result.nav_at(_D4) == cash + D("11.00") * 100, (
            f"复牌日应按 close=11.00 计市值，实际 {result.nav_at(_D4)}")
        assert broker.book.positions[SYMBOL].last_close == D("11.00")


# ======================================================================
# 用例 4：除权日持仓（FR-BT-4 / §11 锚点 #4）
# ======================================================================

class TestCase4ExdivDay:
    """除权日：股数×factor、现金 += 每股分红×老股数、成本对折、NAV 无跳变。

    given
      · D1 下买单 → **D2 成交 100 股** @ 10.00（``avg_cost = 10.00``，
        ``D2.close = 10.00`` ⇒ ``NAV(D2) = 119000 + 1000 = 120000``）。
      · **D3 是除权日**：10 送 10 + 每股派现 0.5 元。事件经策略钩子
        ``exdiv_events_for(D3)`` 注入 ⇒ ``engine._exdiv_for`` → ``broker.settle``
        → ``ledger.process_exdiv``（⛔ 不直接调 ledger，走引擎公开路径）。
        ``ExdivEvent(factor=Decimal("2"), cash_dividend=Decimal("0.5"))``
        —— ``factor`` 是**股数**因子（10 送 10 → 2，见模块 docstring 口径澄清 ①）。
      · D3 除权后价格：``(10.00 - 0.5) / 2 = 4.75``（open=close=preclose=4.75）。

    then
      a. 持仓 ``volume == 200``（100 × 2）。
      b. 现金 ``+50``（0.5 × 100 老股数）⇒ 119050。
      c. ``avg_cost`` 减半：10.00 → 5.00。
      d. ``NAV(D3) ≈ NAV(D2)``：总资产无跳变（容差 ±0.5 元）。
         精确值：119050 + 4.75×200 = 120000 == NAV(D2)。
    """

    @staticmethod
    def _run():
        frame = _frame([
            _row(_D1, open_="10.00", close="10.00", preclose="10.00"),
            _row(_D2, open_="10.00", close="10.00", preclose="10.00"),
            # D3 除权后价格（10.00 - 0.5) / 2 = 4.75；preclose 同口径 ⇒ 不触板
            _row(_D3, open_="4.75", close="4.75", preclose="4.75"),
        ])
        engine, broker = _make(frame, calendar=[_D1, _D2, _D3])
        strategy = ScriptStrategy(
            {_D1: [(OrderSide.BUY, 100, "C4-BUY")]},
            exdiv={_D3: {SYMBOL: ExdivEvent(
                symbol=SYMBOL,
                factor=D("2"),            # 10 送 10 ⇒ 股数 ×2
                cash_dividend=D("0.5"),   # 每股派现 0.5
                date=_D3,
            )}},
        )
        return engine.run(strategy, _D1, _D3), broker

    def test_a_volume_doubled(self):
        """then a：持仓 volume 200（100 × 2）。"""
        _, broker = self._run()
        pos = broker.book.positions[SYMBOL]
        assert pos.volume == 200, f"10 送 10 后持仓应为 200，实际 {pos.volume}"

    def test_b_cash_plus_dividend(self):
        """then b：现金 +50（0.5 × 100 老股数）。"""
        _, broker = self._run()
        expected = INITIAL_CASH - D("10.00") * 100 + D("0.5") * 100   # 119050
        assert broker.book.cash == expected, (
            f"现金应为 {expected}（买入 -1000、分红 +50），实际 {broker.book.cash}")

    def test_c_avg_cost_halved(self):
        """then c：avg_cost 减半（10.00 → 5.00）。"""
        _, broker = self._run()
        pos = broker.book.positions[SYMBOL]
        assert pos.avg_cost == D("5"), (
            f"除权后 avg_cost 应为 5（10.00 / factor 2），实际 {pos.avg_cost}")

    def test_d_nav_no_jump_across_exdiv(self):
        """then d：NAV(D3) ≈ NAV(D2)，总资产无跳变（容差 ±0.5 元）。"""
        result, _ = self._run()
        nav_d2 = result.nav_at(_D2)
        nav_d3 = result.nav_at(_D3)
        assert nav_d2 == D("120000"), f"NAV(D2) 应为 120000，实际 {nav_d2}"
        assert abs(nav_d3 - nav_d2) <= D("0.5"), (
            f"除权日净值不得跳变：NAV(D2)={nav_d2} NAV(D3)={nav_d3} "
            f"差额 {nav_d3 - nav_d2} 超出容差 ±0.5")
        # 精确复算（Decimal 全程，⛔ 无 float）：119050 + 4.75×200 = 120000
        assert nav_d3 == D("119050") + D("4.75") * 200


# ======================================================================
# 用例 5：T+1 当日买卖（FR-BT-5 / §11 锚点 #5）
# ======================================================================

class TestCase5T1SameDay:
    """T+1：当日买入当日不可卖；次一交易日解禁后可卖。

    共同 given
      · 初始无持仓（D1 只有现金 120000）。
      · 帧无涨跌停、无停牌 ⇒ 被拒只可能来自 rule#4。

    场景 A（当日买卖，``test_scenario_a_*``）
      · when：买单与卖单**同日提交**（D1）⇒ **同一撮合日 D2** 到场；买单先成交
        （@ D2.open = 10.00），卖单撞上 ``sellable == 0``。
        （见模块 docstring 口径澄清 ②：本引擎"当日买卖"= 同一撮合日两单同时到场。）
      · then：卖单 ``REJECTED``，理由 == ``T+1 可卖不足``；``trades`` 里没有该笔卖出。

    场景 B（T+1 后解禁，``test_scenario_b_*``）
      · when：D1 买单 → D2 成交；**D2 下卖单** ⇒ D3 撮合。
      · then：D3 正常成交 @ ``D3.open = 10.30``（T+1 已解禁），持仓归零，
        现金 = 120000 - 1000 + 1030 = 120030。
    """

    @staticmethod
    def _frame() -> pd.DataFrame:
        return _frame([
            _row(_D1, open_="10.00", close="10.00", preclose="10.00"),
            _row(_D2, open_="10.00", close="10.20", preclose="10.00"),
            _row(_D3, open_="10.30", close="10.40", preclose="10.20"),
            _row(_D4, open_="10.40", close="10.50", preclose="10.40"),
        ])

    @classmethod
    def _run_a(cls):
        engine, broker = _make(cls._frame())
        strategy = ScriptStrategy({_D1: [
            (OrderSide.BUY, 100, "C5A-BUY"),
            (OrderSide.SELL, 100, "C5A-SELL"),
        ]})
        return engine.run(strategy, _D1, _D4), broker

    @classmethod
    def _run_b(cls):
        engine, broker = _make(cls._frame())
        strategy = ScriptStrategy({
            _D1: [(OrderSide.BUY, 100, "C5B-BUY")],    # D2 成交
            _D2: [(OrderSide.SELL, 100, "C5B-SELL")],  # D3 撮合 → T+1 已解禁
        })
        return engine.run(strategy, _D1, _D4), broker

    # —— 场景 A ——

    def test_scenario_a_same_day_sell_rejected(self):
        """场景 A：买入成交当日的卖出被拒（可卖数 0）。"""
        result, broker = self._run_a()
        buy = _order_by_id(result, "C5A-BUY")
        sell = _order_by_id(result, "C5A-SELL")
        assert buy.status is OrderStatus.FILLED, (
            f"买单应在 D2 成交，实际 {buy.status}")
        assert buy.fills[0].date == _D2 and buy.fills[0].price == D("10.00")
        assert sell.status is OrderStatus.REJECTED, (
            f"当日买入当日卖出必须被拒（T+1），实际 {sell.status}")
        assert sell.reject_reason == REJECT_T1_INSUFFICIENT, (
            f"拒绝理由须为 {REJECT_T1_INSUFFICIENT!r}，实际 {sell.reject_reason!r}")

    def test_scenario_a_no_sell_trade_position_intact(self):
        """场景 A：trades 里没有该笔卖出；持仓完整保留 100 股。"""
        result, broker = self._run_a()
        assert [t for t in result.trades if t.side is OrderSide.SELL] == [], (
            f"T+1 被拒 ⇒ 不该有卖出成交，实际 {result.trades}")
        assert len(result.trades) == 1
        pos = broker.book.positions[SYMBOL]
        assert pos.volume == 100, f"持仓应为 100，实际 {pos.volume}"
        # 成交日日终 advance_sellable 已解禁（次一交易日起可卖）
        assert pos.sellable == 100, (
            f"买入成交日日终应解禁 100 股，实际 sellable={pos.sellable}")

    # —— 场景 B ——

    def test_scenario_b_sell_fills_after_t1(self):
        """场景 B：T+1 之后（D3 撮合）正常成交 @ D3.open。"""
        result, broker = self._run_b()
        sell = _order_by_id(result, "C5B-SELL")
        assert sell.status is OrderStatus.FILLED, (
            f"T+1 解禁后卖单应成交，实际 {sell.status}"
            f"（reject_reason={sell.reject_reason!r}）")
        trade = sell.fills[0]
        assert trade.date == _D3, f"卖出应在 D3 撮合，实际 {trade.date}"
        assert trade.price == D("10.30"), (
            f"成交价应为 D3 开盘 10.30（FR-BT-6），实际 {trade.price}")
        assert broker.book.positions[SYMBOL].volume == 0, "卖出后应清仓"

    def test_scenario_b_cash_after_round_trip(self):
        """场景 B：一轮买卖后现金 = 120000 - 1000 + 1030（费用默认 0，T203 接管）。"""
        result, broker = self._run_b()
        expected = INITIAL_CASH - D("10.00") * 100 + D("10.30") * 100
        assert broker.book.cash == expected, (
            f"现金应为 {expected}，实际 {broker.book.cash}")
        assert result.final_nav == expected, (
            f"清仓后 final_nav 应等于现金 {expected}，实际 {result.final_nav}")
