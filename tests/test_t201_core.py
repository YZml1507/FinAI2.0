#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T201 核心单测（离线，⛔ 无任何网络调用 / 无磁盘依赖）。

覆盖 T201_design.md §2–§5 四个模块：

  ① ``constants`` —— OrderStatus 恰好七态（SDD-2），枚举均为 ``str`` 子类。
  ② ``order_fsm`` —— 全部 10 条合法迁移逐条走通 ×  非法迁移 raise
     ``OrderStateError`` × 非法迁移**不改变**订单状态（失败原子性）。
  ③ ``ledger.Journal`` —— tx_hash 稳定（同数据同 hash、键序无关）+ append 幂等
     （同 tx_hash 双 append 只留一条并返回 False）。
  ④ ``ledger.Ledger`` —— 买入 cash/position/avg_cost、卖出回补、加权成本、
     除权（10 送 10 → 股数翻倍 / 成本对折 / NAV 不变 / 红利入账）、
     结算（bar 存在刷新 / bar 缺失冻结 / NAV = cash + Σmv）、T+1 推进。

⛔ 永不静默原则：断言不被 try/except 吞错；任何失败让 pytest 红。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from backtest.constants import FeeItem, OrderSide, OrderStatus, OrderType
from backtest.ledger import (
    BookView,
    Journal,
    JournalEntry,
    JournalType,
    Ledger,
    LedgerError,
    canonical_repr,
    compute_tx_hash,
)
from backtest.order_fsm import (
    LEGAL_TRANSITIONS,
    OrderStateError,
    OrderStateMachine,
    can_transition,
)
from backtest.types import Bar, Order, PortfolioView, Position, Trade

D = Decimal
D0 = Decimal("0")

_D1 = date(2024, 1, 4)
_D2 = date(2024, 1, 5)
_D3 = date(2024, 1, 8)


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------

def _fees(
    commission: str = "5",
    stamp: str = "0",
    transfer: str = "0.1",
    handling: str = "0.2",
) -> dict[FeeItem, Decimal]:
    """标准四项费用（契约要求 fees 必含这四个键）。"""
    return {
        FeeItem.COMMISSION: D(commission),
        FeeItem.STAMP_TAX: D(stamp),
        FeeItem.TRANSFER_FEE: D(transfer),
        FeeItem.HANDLING_FEE: D(handling),
    }


def _trade(
    *,
    trade_id: str,
    side: OrderSide,
    volume: int,
    price: str,
    symbol: str = "sh.600000",
    trade_date: date = _D1,
    fees: dict[FeeItem, Decimal] | None = None,
    sellable_date: date | None = None,
) -> Trade:
    return Trade(
        trade_id=trade_id,
        client_order_id=f"CO-{trade_id}",
        symbol=symbol,
        side=side,
        volume=volume,
        price=D(price),
        date=trade_date,
        fees=fees if fees is not None else _fees(),
        sellable_date=sellable_date,
    )


def _bar(
    *,
    symbol: str = "sh.600000",
    close: str = "10.00",
    bar_date: date = _D1,
    **kwargs,
) -> Bar:
    defaults = dict(
        open=D(close),
        high=D(close),
        low=D(close),
        preclose=D(close),
        volume=D("1000000"),
        amount=D("10000000"),
    )
    defaults.update(kwargs)
    return Bar(date=bar_date, symbol=symbol, close=D(close), **defaults)


def _order(status: OrderStatus = OrderStatus.PENDING_SUBMIT) -> Order:
    return Order(
        client_order_id="CO-1",
        symbol="sh.600000",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        volume=100,
        price=D("10.00"),
        status=status,
        created_date=_D1,
    )


def _fees_sum(fees: dict[FeeItem, Decimal]) -> Decimal:
    return sum(fees.values(), D0)


# ======================================================================
# ① constants —— 枚举契约
# ======================================================================

def test_order_status_has_exactly_seven_states():
    """OrderStatus 恰好 7 态（SDD-2 七态状态机），成员名逐字与契约一致。"""
    assert len(list(OrderStatus)) == 7
    assert {s.name for s in OrderStatus} == {
        "PENDING_SUBMIT",
        "SUBMITTED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELLED",
        "EXPIRED",
        "REJECTED",
    }


def test_enums_are_str_subclasses_with_identity_values():
    """四个枚举都继承 str，且 value == name（落盘/回包比对无需映射）。"""
    for enum_cls in (OrderSide, OrderType, OrderStatus, FeeItem):
        for member in enum_cls:
            assert isinstance(member, str)
            assert member.value == member.name
    assert OrderSide.BUY == "BUY"
    assert len(list(OrderSide)) == 2
    assert len(list(OrderType)) == 2
    assert len(list(FeeItem)) == 6


def test_types_defaults_and_frozen_contract():
    """Bar/Trade 不可变；Position/PortfolioView 默认值符合契约。"""
    bar = _bar()
    with pytest.raises(Exception):
        bar.close = D("1")            # frozen dataclass
    trade = _trade(trade_id="T1", side=OrderSide.BUY, volume=100, price="10")
    with pytest.raises(Exception):
        trade.volume = 200
    assert bar.adjust_mode == "hfq"
    assert (bar.limit_up, bar.limit_down, bar.exdiv, bar.is_st) == (
        False, False, False, False)
    pos = Position(symbol="sh.600000")
    assert (pos.volume, pos.sellable) == (0, 0)
    assert pos.avg_cost == D0 and pos.market_value == D0
    pv = PortfolioView(date=_D1)
    assert pv.cash == D0 and pv.frozen_cash == D0 and pv.nav == D0
    assert pv.positions == {}


# ======================================================================
# ② order_fsm —— 七态状态机
# ======================================================================

_LEGAL_PAIRS = [
    (OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMITTED),
    (OrderStatus.PENDING_SUBMIT, OrderStatus.REJECTED),
    (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED),
    (OrderStatus.SUBMITTED, OrderStatus.FILLED),
    (OrderStatus.SUBMITTED, OrderStatus.CANCELLED),
    (OrderStatus.SUBMITTED, OrderStatus.EXPIRED),
    (OrderStatus.SUBMITTED, OrderStatus.REJECTED),
    (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED),
    (OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCELLED),
    (OrderStatus.PARTIALLY_FILLED, OrderStatus.EXPIRED),
]


@pytest.mark.parametrize("src,dst", _LEGAL_PAIRS)
def test_legal_transitions_all_pass(src, dst):
    """契约 §4 的 10 条合法迁移逐条走通，状态确实落到目标态。"""
    fsm = OrderStateMachine()
    assert can_transition(src, dst) is True
    order = _order(status=src)
    returned = fsm.transition(order, dst)
    assert returned is order
    assert order.status is dst


def test_legal_transition_table_is_exactly_the_contract():
    """迁移表与契约逐条对齐：合法集合恰好是那 10 条，其余全非法。"""
    legal = {(s, d) for s, outs in LEGAL_TRANSITIONS.items() for d in outs}
    assert legal == set(_LEGAL_PAIRS)
    assert len(legal) == 10


_ILLEGAL_PAIRS = [
    (src, dst)
    for src in OrderStatus
    for dst in OrderStatus
    if (src, dst) not in set(_LEGAL_PAIRS)
]


@pytest.mark.parametrize("src,dst", _ILLEGAL_PAIRS)
def test_illegal_transitions_raise_and_do_not_mutate(src, dst):
    """非法迁移 raise OrderStateError，且订单状态一字不改（失败原子性）。"""
    fsm = OrderStateMachine()
    order = _order(status=src)
    assert can_transition(src, dst) is False
    with pytest.raises(OrderStateError):
        fsm.transition(order, dst)
    assert order.status is src


def test_order_state_error_is_value_error():
    assert issubclass(OrderStateError, ValueError)


def test_terminal_statuses_have_no_outgoing_edges():
    """四个终态无出边（含同态自迁移也非法 → 重复回报不会二次记账）。"""
    fsm = OrderStateMachine()
    for status in (
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.REJECTED,
    ):
        assert fsm.is_terminal(status) is True
        assert fsm.allowed_targets(status) == frozenset()
        assert can_transition(status, status) is False


def test_transition_to_rejected_records_reason():
    """迁移到 REJECTED 时写入 reject_reason。"""
    fsm = OrderStateMachine()
    order = _order(status=OrderStatus.SUBMITTED)
    fsm.transition(order, OrderStatus.REJECTED, reason="停牌不可下单")
    assert order.status is OrderStatus.REJECTED
    assert order.reject_reason == "停牌不可下单"


# ======================================================================
# ③ Journal —— tx_hash 稳定 + append 幂等
# ======================================================================

def _entry_payload(ref_id: str = "T1") -> dict:
    return {
        "date": _D1,
        "entry_type": JournalType.TRADE,
        "symbol": "sh.600000",
        "side": OrderSide.BUY,
        "volume": 100,
        "price": D("10.00"),
        "amount": D("-1005.30"),
        "fees": _fees(),
        "ref_id": ref_id,
        "meta": {},
    }


def test_tx_hash_is_stable_for_same_data():
    """同数据 → 同 hash；hash 为 64 位 hex（SHA-256）。"""
    h1 = compute_tx_hash(_entry_payload())
    h2 = compute_tx_hash(_entry_payload())
    assert h1 == h2
    assert len(h1) == 64 and all(c in "0123456789abcdef" for c in h1)


def test_tx_hash_is_key_order_independent_and_decimal_normalized():
    """键插入顺序无关；Decimal("10") 与 Decimal("10.00") 同 hash（canonical 归一）。"""
    base = _entry_payload()
    shuffled = {k: base[k] for k in reversed(list(base))}
    assert compute_tx_hash(base) == compute_tx_hash(shuffled)
    variant = dict(base, price=D("10"))
    assert compute_tx_hash(variant) == compute_tx_hash(base)
    # canonical 表示是 JSON 文本，Decimal 以字符串出现（⛔ 不是 float）
    text = canonical_repr(base)
    assert '"price":"10"' in text
    assert '"date":"2024-01-04"' in text


def test_tx_hash_changes_when_payload_changes():
    """任一字段变 → hash 变（不同事实不会撞键）。"""
    base = compute_tx_hash(_entry_payload())
    assert compute_tx_hash(_entry_payload(ref_id="T2")) != base
    assert compute_tx_hash(dict(_entry_payload(), volume=200)) != base


def test_tx_hash_rejects_float_amount():
    """float 入账直接 raise（精度不可复现 → 禁止）。"""
    with pytest.raises(LedgerError):
        compute_tx_hash(dict(_entry_payload(), amount=1005.30))


def test_journal_append_is_idempotent_on_same_tx_hash():
    """同 tx_hash 双 append → 只留一条，第二次返回 False。"""
    journal = Journal()
    entry = JournalEntry.create(**_entry_payload())
    assert journal.append(entry) is True
    assert journal.append(entry) is False
    # 内容相同但另行构造的实例，tx_hash 相同 → 同样被幂等吸收
    twin = JournalEntry.create(**_entry_payload())
    assert twin.tx_hash == entry.tx_hash
    assert journal.append(twin) is False
    assert len(journal) == 1
    assert len(journal.entries) == 1
    assert journal.entries[0].tx_hash == entry.tx_hash


def test_journal_entries_is_read_only_snapshot():
    """entries 返回快照：外部改动不影响账本。"""
    journal = Journal()
    journal.append(JournalEntry.create(**_entry_payload()))
    snapshot = journal.entries
    snapshot.append(JournalEntry.create(**_entry_payload(ref_id="T9")))
    assert len(journal) == 1


def test_journal_entry_to_dict_stores_decimal_as_string():
    """落盘形态：Decimal → 字符串、date → isoformat、枚举 → value。"""
    entry = JournalEntry.create(**_entry_payload())
    payload = entry.to_dict()
    assert payload["date"] == "2024-01-04"
    assert payload["entry_type"] == "TRADE"
    assert payload["side"] == "BUY"
    assert isinstance(payload["price"], str) and payload["price"] == "10.00"
    assert isinstance(payload["amount"], str)
    assert all(isinstance(v, str) for v in payload["fees"].values())
    assert set(payload["fees"]) == {
        "COMMISSION", "STAMP_TAX", "TRANSFER_FEE", "HANDLING_FEE"}


def test_journal_append_strict_raises_on_duplicate():
    from backtest.ledger import DuplicateEntryError

    journal = Journal()
    entry = JournalEntry.create(**_entry_payload())
    journal.append_strict(entry)
    with pytest.raises(DuplicateEntryError):
        journal.append_strict(entry)


# ======================================================================
# ④ Ledger.process_trade —— 现金 / 持仓 / 成本
# ======================================================================

def test_process_trade_buy_updates_cash_and_position():
    """买入：cash -= price×volume + Σfees；position += volume；avg_cost = 成交价。"""
    ledger = Ledger(D("100000"), date=_D1)
    fees = _fees()
    trade = _trade(trade_id="T1", side=OrderSide.BUY, volume=1000,
                   price="10.00", fees=fees)
    ledger.process_trade(trade)

    expected_cash = D("100000") - (D("10.00") * 1000 + _fees_sum(fees))
    assert ledger.cash == expected_cash
    pos = ledger.positions["sh.600000"]
    assert pos.volume == 1000
    assert pos.avg_cost == D("10")
    # TRADE 流水记了一条，amount = 现金流出（负）
    trades = ledger.journal.filter_by_type(JournalType.TRADE)
    assert len(trades) == 1
    assert trades[0].amount == -(D("10.00") * 1000 + _fees_sum(fees))
    assert trades[0].ref_id == "T1"


def test_process_trade_sell_restores_cash_and_reduces_position():
    """卖出：cash += price×volume - Σfees；position -= volume；成本不变。"""
    ledger = Ledger(D("100000"), date=_D1)
    buy_fees = _fees()
    ledger.process_trade(_trade(trade_id="T1", side=OrderSide.BUY, volume=1000,
                                price="10.00", fees=buy_fees))
    cash_after_buy = ledger.cash

    sell_fees = _fees(commission="6", stamp="6")
    ledger.process_trade(_trade(trade_id="T2", side=OrderSide.SELL, volume=400,
                                price="11.00", trade_date=_D2, fees=sell_fees))

    assert ledger.cash == cash_after_buy + (D("11.00") * 400 - _fees_sum(sell_fees))
    pos = ledger.positions["sh.600000"]
    assert pos.volume == 600
    assert pos.avg_cost == D("10")       # 卖出不改变剩余持仓成本

    # 全部卖光 → 持仓归零、成本归零
    ledger.process_trade(_trade(trade_id="T3", side=OrderSide.SELL, volume=600,
                                price="11.00", trade_date=_D2, fees=sell_fees))
    pos = ledger.positions["sh.600000"]
    assert pos.volume == 0
    assert pos.avg_cost == D0
    assert pos.market_value == D0


def test_process_trade_avg_cost_is_volume_weighted():
    """两笔不同价买入 → avg_cost 为加权均价（1000@10 + 500@13 → 11）。"""
    ledger = Ledger(D("100000"), date=_D1)
    zero = {k: D0 for k in _fees()}
    ledger.process_trade(_trade(trade_id="T1", side=OrderSide.BUY, volume=1000,
                                price="10.00", fees=zero))
    ledger.process_trade(_trade(trade_id="T2", side=OrderSide.BUY, volume=500,
                                price="13.00", fees=zero))
    pos = ledger.positions["sh.600000"]
    assert pos.volume == 1500
    assert pos.avg_cost == D("11")
    assert ledger.cash == D("100000") - D("10000") - D("6500")


def test_process_trade_is_idempotent():
    """同一笔成交重复 process → 流水一条、现金/持仓只动一次。"""
    ledger = Ledger(D("100000"), date=_D1)
    trade = _trade(trade_id="T1", side=OrderSide.BUY, volume=1000, price="10.00")
    ledger.process_trade(trade)
    cash, volume = ledger.cash, ledger.positions["sh.600000"].volume
    ledger.process_trade(trade)
    assert ledger.cash == cash
    assert ledger.positions["sh.600000"].volume == volume
    assert len(ledger.journal.filter_by_type(JournalType.TRADE)) == 1


def test_oversell_raises_ledger_error():
    """超卖（long-only 禁负持仓）→ raise，⛔ 不静默截断。"""
    ledger = Ledger(D("100000"), date=_D1)
    ledger.process_trade(_trade(trade_id="T1", side=OrderSide.BUY, volume=1000,
                                price="10.00"))
    with pytest.raises(LedgerError):
        ledger.process_trade(_trade(trade_id="T2", side=OrderSide.SELL,
                                    volume=1100, price="10.00"))


# ======================================================================
# ④b process_exdiv —— 10 送 10 + 红利
# ======================================================================

def test_process_exdiv_ten_for_ten_doubles_volume_halves_cost_and_keeps_nav():
    """10 送 10（factor=2）：股数翻倍、成本对折、NAV 不变（净值曲线无跳变）。"""
    ledger = Ledger(D("100000"), date=_D1)
    zero = {k: D0 for k in _fees()}
    ledger.process_trade(_trade(trade_id="T1", side=OrderSide.BUY, volume=1000,
                                price="10.00", fees=zero))
    ledger.settle(_D1, {"sh.600000": _bar(close="10.00")})
    nav_before = ledger.nav
    assert nav_before == D("100000")        # 90000 现金 + 10000 市值

    ledger.process_exdiv("sh.600000", D("2"), D0, date=_D2)

    pos = ledger.positions["sh.600000"]
    assert pos.volume == 2000
    assert pos.avg_cost == D("5")
    assert pos.last_close == D("5")
    assert pos.market_value == D("10000")
    assert ledger.nav == nav_before          # ⛔ NAV 不跳变


def test_process_exdiv_cash_dividend_credited_on_old_volume():
    """现金分红：cash += cash_dividend × old_volume，且写 EXDIV_ADJUST 流水。"""
    ledger = Ledger(D("100000"), date=_D1)
    zero = {k: D0 for k in _fees()}
    ledger.process_trade(_trade(trade_id="T1", side=OrderSide.BUY, volume=1000,
                                price="10.00", fees=zero))
    ledger.settle(_D1, {"sh.600000": _bar(close="10.00")})
    cash_before, nav_before = ledger.cash, ledger.nav

    ledger.process_exdiv("sh.600000", D("2"), D("0.35"), date=_D2, ref_id="EX-1")

    assert ledger.cash == cash_before + D("0.35") * 1000
    assert ledger.nav == nav_before + D("350")   # 分红是净流入，其余无跳变
    entries = ledger.journal.filter_by_type(JournalType.EXDIV_ADJUST)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.symbol == "sh.600000"
    assert entry.volume == 1000                 # old_volume
    assert entry.amount == D("350")
    assert entry.meta["factor"] == D("2")
    assert entry.meta["cash_dividend"] == D("0.35")
    assert entry.date == _D2


def test_process_exdiv_rounds_half_up_and_rejects_bad_factor():
    """股数整数化 round-half-up；factor <= 0 → raise。"""
    ledger = Ledger(D("100000"), date=_D1)
    zero = {k: D0 for k in _fees()}
    ledger.process_trade(_trade(trade_id="T1", side=OrderSide.BUY, volume=101,
                                price="10.00", fees=zero))
    # 101 × 1.15 = 116.15 → 116
    ledger.process_exdiv("sh.600000", D("1.15"), D0, date=_D2)
    assert ledger.positions["sh.600000"].volume == 116
    with pytest.raises(LedgerError):
        ledger.process_exdiv("sh.600000", D("0"), D0, date=_D2)


def test_process_exdiv_noop_without_position():
    """无持仓 → 不记流水、不动现金。"""
    ledger = Ledger(D("100000"), date=_D1)
    ledger.process_exdiv("sh.600000", D("2"), D("0.5"), date=_D1)
    assert ledger.cash == D("100000")
    assert ledger.journal.filter_by_type(JournalType.EXDIV_ADJUST) == []


# ======================================================================
# ④c settle —— 刷新 / 冻结 / NAV
# ======================================================================

def test_settle_refreshes_market_value_when_bar_present():
    """bar 存在 → last_close/market_value 刷新，NAV = cash + Σmv。"""
    ledger = Ledger(D("100000"), date=_D1)
    zero = {k: D0 for k in _fees()}
    ledger.process_trade(_trade(trade_id="T1", side=OrderSide.BUY, volume=1000,
                                price="10.00", fees=zero))
    refreshed = ledger.settle(_D2, {"sh.600000": _bar(close="12.50", bar_date=_D2)})

    assert refreshed == {"sh.600000"}
    pos = ledger.positions["sh.600000"]
    assert pos.last_close == D("12.50")
    assert pos.market_value == D("12500")
    assert ledger.nav == ledger.cash + D("12500")
    assert ledger.nav == D("90000") + D("12500")


def test_settle_freezes_market_value_when_bar_missing():
    """bar 缺失（停牌）→ 市值冻结不动，NAV 走水平线（必挂用例 #3 锚点）。"""
    ledger = Ledger(D("100000"), date=_D1)
    zero = {k: D0 for k in _fees()}
    ledger.process_trade(_trade(trade_id="T1", side=OrderSide.BUY, volume=1000,
                                price="10.00", fees=zero))
    ledger.settle(_D1, {"sh.600000": _bar(close="11.00")})
    nav_before = ledger.nav

    refreshed = ledger.settle(_D2, {})            # 停牌：无 bar
    assert refreshed == set()
    pos = ledger.positions["sh.600000"]
    assert pos.last_close == D("11.00")
    assert pos.market_value == D("11000")
    assert ledger.nav == nav_before
    assert ledger.view.date == _D2                # 日期仍推进


def test_settle_nav_equals_cash_plus_sum_market_value_multi_symbol():
    """多标的：一只有 bar 刷新、一只停牌冻结，NAV = cash + Σmv。"""
    ledger = Ledger(D("200000"), date=_D1)
    zero = {k: D0 for k in _fees()}
    ledger.process_trade(_trade(trade_id="T1", side=OrderSide.BUY, volume=1000,
                                price="10.00", symbol="sh.600000", fees=zero))
    ledger.process_trade(_trade(trade_id="T2", side=OrderSide.BUY, volume=500,
                                price="20.00", symbol="sz.000001", fees=zero))
    ledger.settle(_D1, {
        "sh.600000": _bar(close="10.00"),
        "sz.000001": _bar(symbol="sz.000001", close="20.00"),
    })
    ledger.settle(_D2, {"sh.600000": _bar(close="9.00", bar_date=_D2)})

    positions = ledger.positions
    assert positions["sh.600000"].market_value == D("9000")     # 刷新
    assert positions["sz.000001"].market_value == D("10000")    # 冻结
    total_mv = D("9000") + D("10000")
    assert ledger.nav == ledger.cash + total_mv
    assert ledger.book.total_market_value() == total_mv
    assert ledger.book.total_nav == ledger.nav


def test_settle_writes_settle_entry_snapshot():
    """settle 写 SETTLE 流水快照（nav/cash/market_value 均为 Decimal）。"""
    ledger = Ledger(D("100000"), date=_D1)
    ledger.settle(_D1, {})
    entries = ledger.journal.filter_by_type(JournalType.SETTLE)
    assert len(entries) == 1
    assert entries[0].meta["nav"] == D("100000")
    assert entries[0].to_dict()["meta"]["cash"] == "100000"


# ======================================================================
# ④d advance_sellable —— T+1
# ======================================================================

def test_advance_sellable_unlocks_on_sellable_date():
    """T+1：买入当日不可卖；sellable_date 到达后转可卖（必挂用例 #5 锚点）。"""
    ledger = Ledger(D("100000"), date=_D1)
    zero = {k: D0 for k in _fees()}
    ledger.process_trade(_trade(trade_id="T1", side=OrderSide.BUY, volume=1000,
                                price="10.00", trade_date=_D1,
                                sellable_date=_D2, fees=zero))
    assert ledger.positions["sh.600000"].sellable == 0

    assert ledger.advance_sellable(_D1) == {}            # 当日不解禁
    assert ledger.positions["sh.600000"].sellable == 0

    assert ledger.advance_sellable(_D2) == {"sh.600000": 1000}
    assert ledger.positions["sh.600000"].sellable == 1000
    assert ledger.advance_sellable(_D3) == {}            # 已解禁不重复


def test_register_trades_rebuilds_sellable_index():
    """register_trades 从历史成交重建可卖索引（调用方先注册再推进）。"""
    ledger = Ledger(D("100000"), date=_D1)
    book = BookView(cash=D("100000"), date=_D1)
    book.positions["sh.600000"] = Position(symbol="sh.600000", volume=1500)
    trades = [
        _trade(trade_id="H1", side=OrderSide.BUY, volume=1000, price="10.00",
               trade_date=_D1, sellable_date=_D2),
        _trade(trade_id="H2", side=OrderSide.BUY, volume=500, price="11.00",
               trade_date=_D2, sellable_date=_D3),
    ]
    book.register_trades(trades)
    assert book.advance_sellable(_D2) == {"sh.600000": 1000}
    assert book.advance_sellable(_D3) == {"sh.600000": 500}
    assert book.positions["sh.600000"].sellable == 1500
    # Ledger 也暴露同名入口
    ledger.register_trades([])


def test_journal_replay_reconstructs_cash_and_positions():
    """Journal.replay 从流水重建视图：现金/持仓与在线视图一致（可查账本漂移）。"""
    ledger = Ledger(D("100000"), date=_D1)
    fees = _fees()
    ledger.process_trade(_trade(trade_id="T1", side=OrderSide.BUY, volume=1000,
                                price="10.00", fees=fees))
    ledger.process_trade(_trade(trade_id="T2", side=OrderSide.SELL, volume=400,
                                price="11.00", trade_date=_D2, fees=fees))
    replayed = ledger.journal.replay()
    assert replayed.cash == ledger.cash
    assert replayed.positions["sh.600000"].volume == ledger.positions["sh.600000"].volume
