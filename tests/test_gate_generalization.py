#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""E 路线第三步通用化单测：MarketRules 参数对象 / ExternalEvidenceAdapter 协议 /
audit_external 入口 / D-5·S-5 参数化后的默认行为不变性。

硬约束：本仓（A 股默认）行为逐位不变——既有 tests/test_gates.py 中 D-5/S-5
用例继续覆盖默认路径，本文件覆盖参数化与外部适配路径。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.gates import (
    AttributionEvidenceGate,
    ExternalEvidenceAdapter,
    GateStatus,
    HighPriceLotGate,
    MarketRules,
    build_external_context,
)
from scripts.gates.adapter import _maybe_call
from scripts.gates.audit_external import (
    ADAPTER_GATE_IDS,
    EXTERNAL_GATE_IDS,
    GENERAL_GATE_IDS,
    REPO_BOUND_GATE_IDS,
    audit_adapter,
    audit_external_context,
    get_external_gates,
)
from scripts.gates.context_builder import build_repo_context
from scripts.gates.market_rules import resolve_market_rules


# =====================================================================
# MarketRules 参数对象
# =====================================================================

class TestMarketRules:
    def test_defaults_are_ashare(self):
        """默认值必须等于现 A 股口径（本仓行为不变硬约束）。"""
        r = MarketRules()
        assert r.market_id == "CN_ASHARE"
        assert r.board_lot_size == 100
        assert r.high_price_limit == 300.0
        assert r.price_limit_pct == 0.10
        assert r.t_plus_1 is True
        assert r.required_fee_subjects == (
            ("total_stamp_tax", "印花税"),
            ("total_commission", "佣金"),
        )

    def test_empty_required_subjects_rejected(self):
        """必非零科目集为空 ⇒ 构造即拒（否则 S-5 关税置零断言被掏空）。"""
        with pytest.raises(ValueError):
            MarketRules(required_fee_subjects=())

    def test_invalid_fields_rejected(self):
        with pytest.raises(ValueError):
            MarketRules(board_lot_size=0)
        with pytest.raises(ValueError):
            MarketRules(high_price_limit=-1.0)
        with pytest.raises(ValueError):
            MarketRules(required_fee_subjects=(("total_commission",),))

    def test_to_from_dict_roundtrip(self):
        r = MarketRules(board_lot_size=1, high_price_limit=None, t_plus_1=False,
                        required_fee_subjects=(("total_commission", "佣金"),))
        r2 = MarketRules.from_dict(r.to_dict())
        assert r2 == r

    def test_resolve_precedence(self):
        """构造参 > ctx['market_rules'] > A 股默认。"""
        ashare = MarketRules()
        toy = MarketRules(market_id="T", board_lot_size=1, high_price_limit=None,
                          required_fee_subjects=(("total_commission", "佣金"),))
        assert resolve_market_rules({}, None) is not toy
        assert resolve_market_rules({}, None) == ashare
        assert resolve_market_rules({"market_rules": toy}, None) is toy
        assert resolve_market_rules({"market_rules": toy}, ashare) is ashare
        # JSON 回读形态（dict）也解析
        assert resolve_market_rules({"market_rules": toy.to_dict()}, None) == toy
        # 垃圾值不误用 → 回退 A 股默认（fail-closed）
        assert resolve_market_rules({"market_rules": "bogus"}, None) == ashare


# =====================================================================
# D-5 / S-5 参数化
# =====================================================================

class TestD5Parameterized:
    def test_default_identical_fail(self):
        """默认 A 股口径下违规判定与字面量版一致。"""
        res = HighPriceLotGate().evaluate({"orders": [
            {"side": "BUY", "price": 350.0, "volume": 200},
            {"side": "BUY", "price": 50.0, "volume": 150},
        ]})
        assert res.status == GateStatus.FAIL
        assert res.metrics["violations_count"] == 2

    def test_default_fail_message_bitidentical(self):
        res = HighPriceLotGate().evaluate({"orders": [
            {"side": "BUY", "price": 350.0, "volume": 200},
            {"side": "BUY", "price": 50.0, "volume": 150},
        ]})
        # 字面量版违规文案逐一保持
        samples = res.metrics["samples"]
        assert any("> 300 元" in s for s in samples)
        assert any("非 100 股整手" in s for s in samples)

    def test_ctor_rules_override(self):
        """构造参注入无整手/无高价线规则 ⇒ 同一组委托判 PASS。"""
        toy = MarketRules(market_id="T", board_lot_size=1, high_price_limit=None,
                          required_fee_subjects=(("total_commission", "佣金"),))
        res = HighPriceLotGate(market_rules=toy).evaluate({"orders": [
            {"side": "BUY", "price": 350.0, "volume": 150},
        ]})
        assert res.status == GateStatus.PASS

    def test_ctx_rules_override(self):
        """ctx['market_rules'] 注入亦生效（adapter 产 ctx 的注入口）。"""
        toy = MarketRules(market_id="T", board_lot_size=1, high_price_limit=None,
                          required_fee_subjects=(("total_commission", "佣金"),))
        res = HighPriceLotGate().evaluate({
            "orders": [{"side": "BUY", "price": 350.0, "volume": 150}],
            "market_rules": toy,
        })
        assert res.status == GateStatus.PASS

    def test_custom_lot_size(self):
        rules = MarketRules(board_lot_size=50, high_price_limit=None)
        res = HighPriceLotGate(market_rules=rules).evaluate({"orders": [
            {"side": "BUY", "price": 10.0, "volume": 150},   # 150 % 50 == 0 合法
            {"side": "BUY", "price": 10.0, "volume": 130},   # 130 % 50 != 0 违规
        ]})
        assert res.status == GateStatus.FAIL
        assert "非 50 股整手" in res.metrics["samples"][0]

    def test_sell_orders_unconstrained(self):
        """卖出单不受买入整手/高价约束（与原语义一致）。"""
        res = HighPriceLotGate().evaluate({"orders": [
            {"side": "SELL", "price": 500.0, "volume": 37},
        ]})
        assert res.status == GateStatus.SKIP  # 无买入单 ⇒ 不适用


class TestS5Parameterized:
    def _ctx(self, **kw):
        base = {"trades_count": 20, "total_stamp_tax": Decimal("200.00"),
                "total_commission": Decimal("100.00"),
                "code_evidence": "backtest/metrics.py:L142"}
        base.update(kw)
        return base

    def test_default_identical_fail_on_zero_stamp(self):
        res = AttributionEvidenceGate().evaluate(self._ctx(total_stamp_tax=Decimal("0")))
        assert res.status == GateStatus.FAIL
        assert "印花税(0)" in res.message and "佣金(100.00)" in res.message
        assert "关税作弊" in res.message

    def test_default_inconclusive_message_bitidentical(self):
        """缺 trades_count + 零费 ⇒ INCONCLUSIVE，文案含 印花税(...)/佣金(...) 格式。"""
        res = AttributionEvidenceGate().evaluate({
            "total_stamp_tax": Decimal("0"), "total_commission": Decimal("0"),
        })
        assert res.status == GateStatus.INCONCLUSIVE
        assert "印花税(0)/佣金(0)" in res.message

    def test_no_stamp_market_passes_with_commission(self):
        """无印花税市场：required_fee_subjects 只声明佣金 ⇒ 成交非零费即 PASS。"""
        toy = MarketRules(market_id="T", board_lot_size=1, high_price_limit=None,
                          required_fee_subjects=(("total_commission", "佣金"),))
        res = AttributionEvidenceGate(market_rules=toy).evaluate(self._ctx(
            total_stamp_tax=Decimal("0"),      # 该市场无印花税科目，恒 0
        ))
        assert res.status == GateStatus.PASS

    def test_no_stamp_market_still_catches_zero_commission(self):
        """fail-closed 不降级：该市场声明的佣金为零 ⇒ 仍判作弊。"""
        toy = MarketRules(market_id="T", board_lot_size=1, high_price_limit=None,
                          required_fee_subjects=(("total_commission", "佣金"),))
        res = AttributionEvidenceGate(market_rules=toy).evaluate(self._ctx(
            total_stamp_tax=Decimal("0"), total_commission=Decimal("0"),
        ))
        assert res.status == GateStatus.FAIL
        assert "佣金(0)" in res.message


# =====================================================================
# ExternalEvidenceAdapter 协议 + build_external_context
# =====================================================================

def _toy_adapter():
    """延迟导入玩具 adapter（experiments/ 非包——namespace package 经 repo root 解析）。"""
    from experiments.spikes.gate_generalization.toy_adapter import ToyEvidenceAdapter
    return ToyEvidenceAdapter()


class TestExternalAdapterProtocol:
    def test_protocol_isinstance(self):
        """runtime_checkable：实现协议方法者 isinstance 为真，空壳为假。"""
        assert isinstance(_toy_adapter(), ExternalEvidenceAdapter)

        class _Empty:
            pass
        assert not isinstance(_Empty(), ExternalEvidenceAdapter)

    def test_build_context_keys(self):
        ctx = build_external_context(_toy_adapter())
        for key in ("trades", "orders", "daily_cash_flows", "ledger_entries",
                    "active_features", "fee_summary", "target_weights", "actual_values",
                    "run_record", "run_records", "git_commit", "data_hash", "timestamp",
                    "roundtrip_total_fee", "expected_fee", "trades_count",
                    "total_commission", "market_rules"):
            assert key in ctx, f"缺键 {key}"
        assert ctx["trades_count"] == 5
        # 费科目派生：fees{COMMISSION:5,STAMP_TAX:0} ×5 笔 ⇒ 25 / 0
        assert ctx["total_commission"] == "25"
        assert ctx["total_stamp_tax"] == "0"
        # metrics 抬升
        assert ctx["annualized_turnover"] == "0.15"
        assert ctx["round_trips"] == 2
        assert ctx["trading_days"] == 250
        # 市场规则注入
        assert ctx["market_rules"].market_id == "TOY_MKT"

    def test_missing_methods_inconclusive_path(self):
        """协议可选实现：空 adapter ⇒ 产物键缺席 ⇒ ctx 仅含兜底 market_rules。"""
        class _Empty:
            pass
        ctx = build_external_context(_Empty())
        assert "trades" not in ctx
        assert ctx["market_rules"] == MarketRules()  # 未声明 ⇒ A 股默认（fail-closed）

    def test_maybe_call_non_callable(self):
        assert _maybe_call(object(), "trades") is None

    def test_extra_context_overrides(self):
        """extra_context 逃生口最后并入、可覆盖派生键。"""
        class _A:
            def trades(self):
                return [{"fees": {"COMMISSION": "5"}}]
            def extra_context(self):
                return {"trades_count": 999, "code_evidence": "x.py:L1"}
        ctx = build_external_context(_A())
        assert ctx["trades_count"] == 999  # 覆盖派生
        assert ctx["code_evidence"] == "x.py:L1"


# =====================================================================
# audit_external 入口
# =====================================================================

class TestAuditExternal:
    def test_gate_set_partition(self):
        """22 门外部集 = ✅7 + 🔧15；与 🏠7 本仓特有集构成 29 门划分、互不相交。"""
        assert len(EXTERNAL_GATE_IDS) == 22
        assert set(GENERAL_GATE_IDS) | set(ADAPTER_GATE_IDS) == EXTERNAL_GATE_IDS
        assert not (set(GENERAL_GATE_IDS) & set(ADAPTER_GATE_IDS))
        assert not (EXTERNAL_GATE_IDS & set(REPO_BOUND_GATE_IDS))
        assert len(EXTERNAL_GATE_IDS) + len(REPO_BOUND_GATE_IDS) == 29
        gates = get_external_gates()
        assert {g.gate_id for g in gates} == EXTERNAL_GATE_IDS

    def test_repo_bound_gates_excluded(self):
        for gid in REPO_BOUND_GATE_IDS:
            assert gid not in EXTERNAL_GATE_IDS

    def test_toy_ledger_end_to_end(self):
        """玩具账本走产品化链路：D-5/S-5 转 PASS；缺证据门如实 INCONCLUSIVE。"""
        results, ctx = audit_adapter(_toy_adapter())
        by_id = {r.gate_id: r for r in results}
        assert len(results) == 22
        # 参数化翻转（spike 阶段 FAIL 的两门）
        assert by_id["D-5"].status == GateStatus.PASS
        assert by_id["S-5"].status == GateStatus.PASS
        # 通用层全 PASS（玩具证据齐备的 7 门）
        for gid in GENERAL_GATE_IDS:
            assert by_id[gid].status == GateStatus.PASS, gid
        # 缺证据门 fail-closed INCONCLUSIVE（账本不含行情/AST/压测/分红）
        for gid in ("D-1", "D-2", "D-3", "D-4", "L-3", "S-3", "S-4"):
            assert by_id[gid].status == GateStatus.INCONCLUSIVE, gid
        # 无 FAIL、无 ERROR
        assert not any(r.status == GateStatus.FAIL for r in results)

    def test_ctx_json_dict_rules(self):
        """--ctx-json 路径：market_rules 以 dict 承载亦被解析（序列化兼容）。"""
        ctx = build_external_context(_toy_adapter())
        ctx["market_rules"] = ctx["market_rules"].to_dict()  # 模拟 JSON 落盘形态
        results = audit_external_context(ctx)
        by_id = {r.gate_id: r for r in results}
        assert by_id["D-5"].status == GateStatus.PASS
        assert by_id["S-5"].status == GateStatus.PASS


# =====================================================================
# 本仓默认行为不变（硬约束）
# =====================================================================

class TestRepoDefaultsUnchanged:
    def test_repo_ctx_injects_ashare_rules(self):
        """build_repo_context 产 ctx 必须含默认 A 股 MarketRules。"""
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[1]
        ctx, _src = build_repo_context(repo_root)
        rules = ctx.get("market_rules")
        assert isinstance(rules, MarketRules)
        assert rules == MarketRules()  # A 股默认口径

    def test_d5_s5_same_verdict_with_and_without_rules(self):
        """同一证据下：默认构造、显式 A 股构造、ctx 注入 —— 三者判定一致。"""
        orders = [{"side": "BUY", "price": 350.0, "volume": 150}]
        a = HighPriceLotGate().evaluate({"orders": orders})
        b = HighPriceLotGate(market_rules=MarketRules()).evaluate({"orders": orders})
        c = HighPriceLotGate().evaluate({"orders": orders, "market_rules": MarketRules()})
        assert (a.status, a.metrics.get("violations_count")) == \
               (b.status, b.metrics.get("violations_count")) == \
               (c.status, c.metrics.get("violations_count"))
        s5_ctx = {"trades_count": 3, "total_stamp_tax": Decimal("0"),
                  "total_commission": Decimal("15"), "code_evidence": "x"}
        a5 = AttributionEvidenceGate().evaluate(s5_ctx)
        b5 = AttributionEvidenceGate(market_rules=MarketRules()).evaluate(s5_ctx)
        c5 = AttributionEvidenceGate().evaluate({**s5_ctx, "market_rules": MarketRules()})
        assert a5.status == b5.status == c5.status
        assert a5.message == b5.message == c5.message
