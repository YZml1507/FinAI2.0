#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T317 运行证据链测试 —— reporting/evidence.py + registry(evidence=) +
context_builder 回填 + broker 税档 meta 的逐项契约钉死。

口径声明（fail-closed，非"看起来对"）：
* 序列化产出必须 round-trip 成立（JSON 落盘重载后键值不变）；
* 逐日现金流采用 SETTLE 锚定**真实**守恒——成分对不上锚就是账漏；
* 红利税分档明细总额 == 税额（S-4 对账基线）；
* 无 evidence 的产物回填后 ctx 不得凭空冒出证据键（缺失 ≠ 通过）。
"""
from __future__ import annotations

import json
from datetime import date as _date
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.constants import FeeItem, OrderSide, OrderStatus, OrderType
from backtest.ledger import JournalEntry, JournalType
from backtest.types import Bar, Order, Trade
from reporting import evidence as ev
from reporting.evidence import (
    allocation_evidence,
    daily_cash_flows,
    dividend_tax_evidence,
    golden_roundtrip_fee,
    orders_adv_ratio,
    serialize_journal,
    serialize_orders,
    serialize_trades,
    to_json_safe,
)


def _entry(*, date, etype, amount=Decimal("0"), symbol="", side=None,
           volume=0, price=Decimal("0"), fees=None, ref_id="", meta=None):
    return JournalEntry.create(
        date=date, entry_type=etype, symbol=symbol, side=side,
        volume=volume, price=price, amount=amount, fees=fees,
        ref_id=ref_id, meta=meta)


def _settle(day, cash):
    return _entry(date=day, etype=JournalType.SETTLE,
                  meta={"cash": str(cash), "nav": str(cash)})


# ------------------------------------------------------------------ to_json_safe

def test_to_json_safe_types():
    out = to_json_safe({
        "d": Decimal("1.5"), "day": _date(2020, 1, 2), "s": OrderSide.BUY,
        "f": 1.25, "n": None, "l": [Decimal("2"), "x"], "m": {"k": Decimal("3")},
    })
    assert out == {"d": "1.5", "day": "2020-01-02", "s": "BUY", "f": 1.25,
                   "n": None, "l": ["2", "x"], "m": {"k": "3"}}
    json.dumps(out)  # 可落盘


def test_to_json_safe_rejects_unknown():
    class Weird: ...
    with pytest.raises(TypeError):
        to_json_safe({"bad": Weird()})
    with pytest.raises(TypeError):
        to_json_safe({"bad": {1, 2}})          # set 无定义序，不静默降级


# ------------------------------------------------------------------ 序列化

def _order(symbol="sh.600000", side=OrderSide.BUY, volume=100,
           day=_date(2020, 3, 2), filled=100, avg=Decimal("10.01")):
    return Order(
        client_order_id=f"t311-{symbol}-{side.value}-{day.isoformat()}-1",
        symbol=symbol, side=side, order_type=OrderType.MARKET,
        volume=volume, price=None, status=(
            OrderStatus.FILLED if filled else OrderStatus.REJECTED),
        created_date=day, filled_volume=filled,
        avg_fill_price=avg if filled else None)


def _trade(symbol="sh.600000", side=OrderSide.BUY, volume=100,
           price=Decimal("10.01"), day=_date(2020, 3, 3), fees=None):
    return Trade(
        trade_id=f"tr-{symbol}-{day.isoformat()}-{side.value}",
        client_order_id=f"t311-{symbol}-{side.value}-2020-03-02-1",
        symbol=symbol, side=side, volume=volume, price=price, date=day,
        fees=fees if fees is not None else {FeeItem.COMMISSION: Decimal("5.00")},
        sellable_date=day)


def test_serialize_orders_market_uses_filled_price():
    o = _order()
    [d] = serialize_orders([o])
    assert d["order_type"] == "MARKET"
    assert d["price"] == "10.01"            # MARKET 无申报价 ⇒ 已实现均价
    assert d["side"] == "BUY" and d["volume"] == 100
    assert d["status"] == "FILLED"
    assert json.loads(json.dumps(d)) == d   # round-trip

    rejected = _order(filled=0)
    [d2] = serialize_orders([rejected])
    assert "price" not in d2                # 未成交无价格键 ⇒ 门侧默认 0
    assert d2["status"] == "REJECTED"


def test_serialize_trades_enriches_limit_prices():
    import pandas as pd
    frames = {"sh.600000": pd.DataFrame({
        "date": ["2020-03-03"], "preclose": [10.0], "open": [10.0],
        "amount": [5e8], "close": [10.5], "isST": ["0"]})}
    t = _trade()
    [d] = serialize_trades([t], frames)
    # 主板 ±10%：10.00×1.10=11.00 / ×0.90=9.00（tick 0.01 HALF_UP）
    assert d["limit_up"] == "11" or d["limit_up"] == "11.00"
    assert d["limit_down"] == "9" or d["limit_down"] == "9.00"
    assert d["total_fee"] == "5.00"
    assert d["fees"]["COMMISSION"] == "5.00"
    assert json.loads(json.dumps(d)) == d


def test_serialize_trades_missing_bar_emits_zero_limits():
    t = _trade(day=_date(2020, 4, 1))
    [d] = serialize_trades([t], frames=None)
    assert d["limit_up"] == "0" and d["limit_down"] == "0"


def test_serialize_journal_shape():
    e = _entry(date=_date(2020, 1, 2), etype=JournalType.CASH_IN,
               amount=Decimal("150000"), ref_id="INITIAL_CAPITAL")
    [d] = serialize_journal([e])
    assert d["entry_type"] == "CASH_IN"
    assert d["amount"] == "150000"
    assert d["date"] == "2020-01-02"
    assert "tx_hash" in d
    json.loads(json.dumps(d))


# ------------------------------------------------------------------ A-2 现金流

def test_daily_cash_flows_conservation_real_anchor():
    """成分 + 上日锚 == 当日 SETTLE 锚（真实守恒，非恒等式自证）。"""
    entries = [
        _entry(date=_date(2020, 1, 2), etype=JournalType.CASH_IN,
               amount=Decimal("150000"), ref_id="INITIAL_CAPITAL"),
        _settle(_date(2020, 1, 2), Decimal("150000.00")),
        # 2020-01-03：买入 100 股 @10（毛额 1000）+ 费 5 ⇒ 现金 148995
        _entry(date=_date(2020, 1, 3), etype=JournalType.TRADE,
               symbol="sh.600000", side=OrderSide.BUY, volume=100,
               price=Decimal("10"), amount=Decimal("-1005"),
               fees={FeeItem.COMMISSION: Decimal("5")}),
        _settle(_date(2020, 1, 3), Decimal("148995.00")),
        # 2020-01-06：分红 +50，红利税 -10（20% 档）⇒ 现金 149035
        _entry(date=_date(2020, 1, 6), etype=JournalType.EXDIV_ADJUST,
               symbol="sh.600000", amount=Decimal("50"),
               meta={"cash_dividend": "0.05", "old_volume": 1000}),
        _entry(date=_date(2020, 1, 6), etype=JournalType.DIVIDEND_TAX,
               symbol="sh.600000", amount=Decimal("-10"),
               fees={FeeItem.DIVIDEND_TAX: Decimal("10")},
               meta={"tax_by_bracket": {"0.20": "10"}}),
        _settle(_date(2020, 1, 6), Decimal("149035.00")),
    ]
    flows, unanchored = daily_cash_flows(entries)
    assert unanchored == []
    assert len(flows) == 3
    # 逐日守恒：cash_start + 成分 == cash_end（SETTLE 锚定值）
    for f in flows:
        comp = (Decimal(f["cash_start"]) + Decimal(f["trade_in"])
                - Decimal(f["trade_out"]) - Decimal(f["fee_out"])
                + Decimal(f["dividend_in"]) - Decimal(f["dividend_tax_out"])
                + Decimal(f["other_in"]) - Decimal(f["other_out"]))
        assert comp == Decimal(f["cash_end"]), f
    # DIVIDEND_TAX 的 fees 字段是 amount 明细 ⇒ 不得重复计入 fee_out
    assert Decimal(flows[2]["fee_out"]) == 0
    assert Decimal(flows[2]["dividend_tax_out"]) == Decimal("10")


def test_daily_cash_flows_unanchored_day_flagged():
    """缺 SETTLE 锚的日期须显式列入 unanchored（fail-visible）。"""
    entries = [
        _entry(date=_date(2020, 1, 2), etype=JournalType.CASH_IN,
               amount=Decimal("1000")),
        _entry(date=_date(2020, 1, 3), etype=JournalType.FEE,
               amount=Decimal("-1")),
    ]
    flows, unanchored = daily_cash_flows(entries)
    assert unanchored == ["2020-01-02", "2020-01-03"]
    assert all(not f["anchored"] for f in flows)


# ------------------------------------------------------------------ A-3 / S-3 / S-4

def test_golden_roundtrip_fee_matches_gate_bases():
    """A-3 黄金算例：10 万往返逐项口径 112.82（行业含规费口径 102.00，容差 0.05）。"""
    total = golden_roundtrip_fee()
    assert abs(total - Decimal("112.82")) < Decimal("0.01")


def test_orders_adv_ratio_computation():
    import pandas as pd
    # 20 日成交额 1e8/日；委托 10000 股 @10 = 10 万 ⇒ ratio 0.001
    days = [f"2020-01-{d:02d}" for d in range(2, 32)]
    frames = {"sh.600000": pd.DataFrame({
        "date": days, "open": [10.0] * len(days),
        "amount": [1e8] * len(days)})}
    o = _order(volume=10_000, day=_date(2020, 1, 10))
    out = orders_adv_ratio([o], frames)
    assert out["skipped"] == 0
    assert len(out["ratios"]) == 1
    # ref = created_date 之后首个 bar（2020-01-11 无 bar → 顺延 01-12）
    # 已成交委托分子 = 委托量 × 已实现均价 10.01 ⇒ 100100/1e8
    assert abs(out["ratios"][0] - 100_100 / 1e8) < 1e-9


def test_orders_adv_ratio_skips_missing_series():
    o = _order(symbol="sz.999999")
    out = orders_adv_ratio([o], frames={"sh.600000": None})
    assert out["ratios"] == [] and out["skipped"] == 1


def test_dividend_tax_evidence_aggregates_brackets():
    entries = [
        _entry(date=_date(2020, 6, 1), etype=JournalType.EXDIV_ADJUST,
               symbol="s1", amount=Decimal("100")),
        _entry(date=_date(2020, 6, 1), etype=JournalType.DIVIDEND_TAX,
               symbol="s1", amount=Decimal("-20"),
               meta={"tax_by_bracket": {"0.20": "20"}}),
        _entry(date=_date(2021, 6, 1), etype=JournalType.DIVIDEND_TAX,
               symbol="s1", amount=Decimal("-5"),
               meta={"tax_by_bracket": {"0.05": "5"}}),
    ]
    ev_out = dividend_tax_evidence(entries)
    assert ev_out["penalty_tax_amount"] == "20"          # 20% 档合计
    assert ev_out["total_dividend_received"] == "100"
    assert ev_out["tax_by_bracket"] == {"0.05": "5", "0.20": "20"}


# ------------------------------------------------------------------ L-2 权重保真

def test_allocation_evidence_picks_last_filled_rebalance():
    import pandas as pd
    frames = {"sh.600000": pd.DataFrame({
        "date": ["2020-03-03"], "preclose": [10.0], "open": [10.0],
        "amount": [1e8], "close": [10.5], "isST": ["0"]})}
    o = _order(symbol="sh.600000", day=_date(2020, 3, 2))
    t = _trade(symbol="sh.600000", day=_date(2020, 3, 3), volume=100)

    class _R: ...
    result = _R()
    result.orders = [o]
    result.trades = [t]
    result.trading_dates = [_date(2020, 3, 2), _date(2020, 3, 3)]

    history = [
        {"date": "2020-02-01", "target_weights": {"sh.600000": "5000"}},
        {"date": "2020-03-02", "target_weights": {"sh.600000": "3000"}},
    ]
    out = allocation_evidence(result, frames, history)
    assert out["rebalance_date"] == "2020-03-02"
    assert out["valuation_date"] == "2020-03-03"
    assert out["target_weights"] == {"sh.600000": "3000"}
    # 持仓 100 股 × 收盘 10.5 = 1050
    assert abs(out["actual_values"]["sh.600000"] - 1050.0) < 1e-6


def test_allocation_evidence_no_fills_empty():
    class _R: ...
    result = _R()
    result.orders = [_order(filled=0)]
    result.trades = []
    result.trading_dates = [_date(2020, 3, 2), _date(2020, 3, 3)]
    out = allocation_evidence(result, {},
                              [{"date": "2020-03-02",
                                "target_weights": {"sh.600000": "1"}}])
    assert out["target_weights"] == {} and out["actual_values"] == {}


# ------------------------------------------------------------------ registry / context_builder

def _fake_report():
    class R:
        start = _date(2020, 1, 1)
        end = _date(2020, 12, 31)
        calendar_days = 366
        trading_days = 244
        initial_nav = Decimal("150000")
        final_nav = Decimal("160000")
        total_return = Decimal("0.06")
        cagr = Decimal("0.06")
        annual_volatility = Decimal("0.1")
        max_drawdown = Decimal("0.05")
        sharpe_ratio = Decimal("0.5")
        annual_turnover = Decimal("1.0")
        win_rate = Decimal("0.5")
        round_trips = 10
        fees_sum = Decimal("100")
        fees_total = {FeeItem.COMMISSION: Decimal("100")}
    return R()


def test_record_run_evidence_outside_signature(tmp_path):
    """evidence 并入产物但在签名域外：篡改/补注 evidence 不破坏签名校验。"""
    from reporting.registry import ExperimentRegistry
    from scripts.gates.tamper_guard import verify_run_signature

    reg = ExperimentRegistry(
        root=tmp_path, code_version="test-v1", data_version="dv",
        code_hash="abc", data_hash="def", calendar_hash="ghi",
        universe_hash="jkl")
    run_id = reg.record_run(
        params={"a": 1}, report=_fake_report(), seed=None,
        evidence={"trades": [{"price": "10.01"}], "penalty_tax_amount": "12"})
    path = tmp_path / "runs" / f"{run_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["evidence"]["trades"][0]["price"] == "10.01"
    ok, msg = verify_run_signature(record)
    assert ok, msg
    # 修改 evidence 不应使签名失效（签名域不含 evidence）
    record["evidence"]["extra_note"] = "post-hoc annotation"
    ok2, _ = verify_run_signature(record)
    assert ok2


def test_context_builder_backfills_evidence(tmp_path):
    """context_builder 把最新签名产物的 evidence 键并入 ctx 顶层。"""
    from scripts.gates.context_builder import build_repo_context
    from scripts.gates.tamper_guard import sign_run_record

    runs = tmp_path / "experiments" / "runs"
    runs.mkdir(parents=True)
    record = sign_run_record({
        "run_id": "t-1", "status": "FINISHED", "timestamp": "2020-01-02T00:00:00+00:00",
        "code_version": "cv", "data_version": "dv", "seed": None,
        "params_hash": "ph", "params": {}, "metrics": {"total_return": "0.1"},
        "error": None})
    record["evidence"] = {"orders": [{"side": "BUY"}], "penalty_tax_amount": "7"}
    (runs / "t-1.json").write_text(json.dumps(record), encoding="utf-8")

    ctx, _src = build_repo_context(tmp_path)
    assert ctx["run_record"]["run_id"] == "t-1"
    assert ctx["orders"] == [{"side": "BUY"}]
    assert ctx["penalty_tax_amount"] == "7"


def test_context_builder_missing_evidence_failclosed(tmp_path):
    """无 evidence 的产物回填后 ctx 不凭空出现证据键（缺失 ≠ 通过）。"""
    from scripts.gates.context_builder import build_repo_context
    from scripts.gates.tamper_guard import sign_run_record

    runs = tmp_path / "experiments" / "runs"
    runs.mkdir(parents=True)
    record = sign_run_record({
        "run_id": "t-2", "status": "FINISHED",
        "timestamp": "2020-01-02T00:00:00+00:00",
        "code_version": "cv", "data_version": "dv", "seed": None,
        "params_hash": "ph", "params": {}, "metrics": {"total_return": "0.1"},
        "error": None})
    (runs / "t-2.json").write_text(json.dumps(record), encoding="utf-8")
    ctx, _src = build_repo_context(tmp_path)
    assert "orders" not in ctx
    assert "daily_cash_flows" not in ctx
    assert "penalty_tax_amount" not in ctx


# ------------------------------------------------------------------ 税档明细（broker 侧真跑）

def test_dividend_tax_detail_bracket_sum_and_keys():
    """compute_dividend_tax_detail: by_rate 分档合计 == total；键=税率字符串。"""
    from backtest.dividend_tax import DividendEvent, compute_dividend_tax_detail
    div = [DividendEvent(ex_date=_date(2020, 6, 1), symbol="sh.600000",
                        dividend_per_share=Decimal("1"), shares_held=1000)]
    buys = [(_date(2020, 5, 20), "sh.600000", 1000)]
    total, by_rate = compute_dividend_tax_detail(div, buys, [])
    assert total == Decimal("200.00")
    assert sum(by_rate.values()) == total
    assert set(by_rate) == {"0.20"}


def test_build_run_evidence_end_to_end_minimal():
    """build_run_evidence 总装配冒烟：合成 result 产出全部证据键。"""
    class _R: ...
    result = _R()
    result.orders = [_order()]
    result.trades = [_trade()]
    result.journal_entries = [
        _entry(date=_date(2020, 3, 2), etype=JournalType.CASH_IN,
               amount=Decimal("150000")),
        _entry(date=_date(2020, 3, 3), etype=JournalType.TRADE,
               symbol="sh.600000", side=OrderSide.BUY, volume=100,
               price=Decimal("10.01"), amount=Decimal("-1006"),
               fees={FeeItem.COMMISSION: Decimal("5")}),
        _settle(_date(2020, 3, 3), Decimal("148994.00")),
    ]
    result.trading_dates = [_date(2020, 3, 2), _date(2020, 3, 3)]
    out = ev.build_run_evidence(
        result, tables=None,
        rebalance_history=[{"date": "2020-03-02",
                            "target_weights": {"sh.600000": "1000"}}],
        extras={"executed_calls": ["MatchEngine"], "stress_return": 0.05})
    for k in ("orders", "trades", "ledger_entries", "daily_cash_flows",
              "roundtrip_total_fee", "orders_adv_ratio", "target_weights",
              "actual_values", "penalty_tax_amount", "total_dividend_received",
              "trades_count", "executed_calls", "stress_return"):
        assert k in out, k
    assert out["executed_calls"] == ["MatchEngine"]
    assert json.loads(json.dumps(out)) == out      # 全量 JSON-safe round-trip
