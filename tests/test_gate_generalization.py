#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""E 路线通用化单测：MarketRules 默认值 / adapter 协议 / audit_external 门集合。

硬约束：``MarketRules()`` 默认值 = 现 A 股口径——本仓门禁行为逐位不变
（D-5 整手 100/高价线 300、S-5 印花税+佣金双科目必非零）。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from scripts.gates.adapter import assemble_context
from scripts.gates.audit_external import (
    EXTERNAL_GATE_IDS,
    REPO_ONLY_GATE_IDS,
    get_external_gates,
    run_external_audit,
)
from scripts.gates.base import GateStatus
from scripts.gates.gate_d_data import HighPriceLotGate
from scripts.gates.gate_s_scientific import AttributionEvidenceGate
from scripts.gates.gate_master_audit import GateMasterAudit
from scripts.gates.market_rules import MarketRules, coerce_market_rules, resolve_market_rules

_SPIKE_DIR = Path(__file__).resolve().parents[1] / "experiments" / "spikes" / "gate_generalization"


class TestMarketRulesDefaults:
    """MarketRules 默认值即 A 股口径（本仓行为不变的硬约束）。"""

    def test_defaults_are_ashare(self):
        r = MarketRules()
        assert r.lot_size == 100
        assert r.max_buy_price == Decimal("300")
        assert r.stamp_tax_item == "STAMP_TAX"
        assert r.commission_item == "COMMISSION"
        assert r.required_fee_totals == (("total_stamp_tax", "印花税"), ("total_commission", "佣金"))
        assert r.limit_pct == Decimal("0.10")
        assert r.t_plus_1 is True

    def test_frozen(self):
        r = MarketRules()
        with pytest.raises(Exception):
            r.lot_size = 1  # type: ignore[misc]

    def test_resolve_default_when_absent(self):
        assert resolve_market_rules(None) == MarketRules()
        assert resolve_market_rules({}) == MarketRules()
        assert resolve_market_rules({"orders": []}) == MarketRules()

    def test_resolve_passthrough_and_dict(self):
        toy = MarketRules(name="玩具市场", lot_size=None, max_buy_price=None,
                          required_fee_totals=(("total_commission", "佣金"),), t_plus_1=False)
        assert resolve_market_rules({"market_rules": toy}) is toy
        d = resolve_market_rules({"market_rules": {"lot_size": 1, "max_buy_price": "500.5",
                                                   "required_fee_totals": [["total_commission", "佣金"]],
                                                   "t_plus_1": 0}})
        assert d.lot_size == 1
        assert d.max_buy_price == Decimal("500.5")
        assert d.required_fee_totals == (("total_commission", "佣金"),)
        assert d.t_plus_1 is False

    def test_resolve_rejects_garbage(self):
        with pytest.raises(TypeError):
            resolve_market_rules({"market_rules": {"bogus_key": 1}})  # 未知键拒收
        with pytest.raises(TypeError):
            resolve_market_rules({"market_rules": "A股"})  # 错误类型拒收


class TestD5Parameterized:
    """D-5：门体从 MarketRules 读规则；无 market_rules 键时行为与硬编码时代逐位一致。"""

    def test_default_rules_unchanged(self):
        ctx = {"orders": [{"side": "BUY", "price": 350.0, "volume": 100},
                          {"side": "BUY", "price": 50.0, "volume": 150}]}
        res = HighPriceLotGate().evaluate(ctx)
        assert res.status == GateStatus.FAIL
        assert res.metrics["violations_count"] == 2
        assert "买入高价股单价 350.0 > 300 元" in res.metrics["samples"]
        assert "买入股数 150 非 100 股整手" in res.metrics["samples"]

    def test_toy_rules_legitimize_odd_lot(self):
        ctx = {"orders": [{"side": "BUY", "price": 30.0, "volume": 150}],
               "market_rules": MarketRules(name="玩具市场", lot_size=None, max_buy_price=None)}
        assert HighPriceLotGate().evaluate(ctx).status == GateStatus.PASS

    def test_custom_lot_size_enforced(self):
        ctx = {"orders": [{"side": "BUY", "price": 10.0, "volume": 150}],
               "market_rules": {"lot_size": 50}}
        res = HighPriceLotGate().evaluate(ctx)
        assert res.status == GateStatus.PASS
        ctx2 = {"orders": [{"side": "BUY", "price": 10.0, "volume": 130}],
                "market_rules": {"lot_size": 50}}
        res2 = HighPriceLotGate().evaluate(ctx2)
        assert res2.status == GateStatus.FAIL
        assert "买入股数 130 非 50 股整手" in res2.metrics["samples"]


class TestS5Parameterized:
    """S-5：必需非零规费科目由 MarketRules 决定，默认仍要求印花税+佣金。"""

    def test_default_rules_unchanged(self):
        res = AttributionEvidenceGate().evaluate({
            "trades_count": 20, "total_stamp_tax": "0", "total_commission": "100",
            "code_evidence": "x"})
        assert res.status == GateStatus.FAIL
        assert "印花税(0)或佣金(100)为零" in res.message

    def test_stamp_free_market_passes_without_stamp_tax(self):
        toy_rules = MarketRules(name="玩具市场",
                                required_fee_totals=(("total_commission", "佣金"),))
        res = AttributionEvidenceGate().evaluate({
            "trades_count": 5, "total_stamp_tax": "0", "total_commission": "25",
            "code_evidence": "x", "market_rules": toy_rules})
        assert res.status == GateStatus.PASS

    def test_commission_zero_still_fails_in_stamp_free_market(self):
        toy_rules = MarketRules(name="玩具市场",
                                required_fee_totals=(("total_commission", "佣金"),))
        res = AttributionEvidenceGate().evaluate({
            "trades_count": 5, "total_commission": "0",
            "code_evidence": "x", "market_rules": toy_rules})
        assert res.status == GateStatus.FAIL
        assert "佣金(0)为零" in res.message


class TestAdapterAssemble:
    """分键型 adapter → assemble_context 聚合推导。"""

    class _MiniAdapter:
        def trades(self):
            return [{"side": "BUY", "price": "10", "volume": "1",
                     "fees": {"COMMISSION": "5"}, "total_fee": "5"}]

        def run_records(self):
            return [{"run_id": "r1", "metrics": {"round_trips": 3, "trading_days": 250,
                                                 "annual_turnover": "1.5"}}]

        def provenance(self):
            return {"git_commit": "abcdef0123456789", "data_hash": "a" * 32,
                    "timestamp": "2024-01-01T00:00:00Z"}

        def market_rules(self):
            return MarketRules(name="mini", required_fee_totals=(("total_commission", "佣金"),))

    def test_derived_keys(self):
        ctx = assemble_context(self._MiniAdapter())
        assert ctx["trades_count"] == 1
        assert ctx["total_commission"] == "5"
        assert ctx["run_record"]["run_id"] == "r1"
        assert ctx["round_trips"] == 3
        assert ctx["annualized_turnover"] == "1.5"
        assert ctx["git_commit"] == "abcdef0123456789"
        assert ctx["market_rules"].name == "mini"

    def test_missing_methods_leave_keys_absent(self):
        ctx = assemble_context(object())
        assert "trades" not in ctx          # 缺键 ⇒ 门 INCONCLUSIVE，不得伪造
        assert "trades_count" not in ctx    # 不得伪造「零成交」断言
        assert ctx["market_rules"] == MarketRules()


class TestExternalAudit:
    """audit_external 门集合 = 标准 29 门剔除 🏠 本仓特有 7 门。"""

    def test_gate_set_excludes_repo_only(self):
        assert REPO_ONLY_GATE_IDS == frozenset(
            {"E-1", "E-2", "S-2", "G-2", "G-3", "G-DOC-1", "G-REF-1"})
        ids = [g.gate_id for g in get_external_gates()]
        assert len(ids) == len(set(ids)) == 22
        assert not REPO_ONLY_GATE_IDS & set(ids)
        assert "D-5" in ids and "S-5" in ids and "G-REPRO-1" in ids

    def test_toy_ledger_end_to_end(self):
        from experiments.spikes.gate_generalization.toy_adapter import ToyEvidenceAdapter

        adapter = ToyEvidenceAdapter(_SPIKE_DIR / "toy_ledger.csv")
        ctx = assemble_context(adapter)
        assert isinstance(ctx["market_rules"], MarketRules)
        assert ctx["market_rules"].name == "玩具市场"

        results = {r.gate_id: r for r in run_external_audit(ctx)}
        assert len(results) == 22
        # spike 中 FAIL 的两门经 MarketRules 参数化后转 PASS
        assert results["D-5"].status == GateStatus.PASS
        assert results["S-5"].status == GateStatus.PASS
        # 原 ✅ 层保持 PASS
        for gid in ("A-1", "A-2", "E-3", "G-1", "G-4", "G-MDD-1", "G-STRESS-1", "G-REPRO-1"):
            assert results[gid].status == GateStatus.PASS, gid
        # 缺证据仍如实 INCONCLUSIVE（fail-closed 不降）
        for gid in ("D-1", "D-2", "D-3", "D-4", "L-3", "S-3", "S-4"):
            assert results[gid].status == GateStatus.INCONCLUSIVE, gid
        assert not any(r.status == GateStatus.FAIL for r in results.values())

    def test_context_builder_injects_default_rules(self):
        from scripts.gates.context_builder import build_repo_context

        ctx, _ = build_repo_context()
        assert ctx["market_rules"] == MarketRules()
