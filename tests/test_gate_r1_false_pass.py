#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GATE-R1 残留清理：门禁「无信息/不可判却返回 PASS」假通过分支的双向锁定测试。

战役背景：本仓曾发生「表面全绿、实际空转」失信事件（24 道门禁 23 SKIP 仍报全绿）。
本轮延续同一战役，系统清理 `scripts/gates/` 中 "无样本 / 退化输入 / 常量兜底 / 纯短路"
也能判 PASS 的分支。

测试纪律（⛔ 不在被测代码里自证）：
1. **反向**：每条被清理的反例输入，必须断言 ``status != PASS``（且校验具体状态）；
2. **正向**：每条修复都配一个合法输入，必须仍能判 ``PASS``（防"改过头"误杀）；
3. 既有测试**不得放宽**（如 ``tests/test_gates.py::test_l2_allocation_fidelity_pass`` 用不均权重，
   本就不受 L-2 等权分支影响）。
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from scripts.gates import GateStatus
from scripts.gates.gate_a_accounting import (
    DailyCashConserveGate,
    FeeSumBalanceGate,
    GoldenRoundtripGate,
    SegmentRateScheduleGate,
)
from scripts.gates.gate_d_data import (
    HighPriceLotGate,
    RawPriceJumpGate,
    SuspensionVolumeGate,
)
from scripts.gates.gate_e_engine import MustFailCasesGate, SlippagePriceCapGate
from scripts.gates.gate_consistency import DocPathReferenceGate, StressValidityGate
from scripts.gates.gate_g_governance import ProvenanceTriadGate, TasksSignGate
from scripts.gates.gate_l_liveness import AllocationFidelityGate
from scripts.gates.gate_repro import ReproducibilityGate
from scripts.gates.runner import run_pre_run_gates
from scripts.gates.gate_s_scientific import (
    AttributionEvidenceGate,
    DynamicSlippageAdvGate,
    TimingExitSurvivalGate,
)


# =====================================================================
# L-2 AllocationFidelityGate：等权目标 ⇒ 退化为"绝对偏离"检验
# =====================================================================

class TestL2EqualWeightNoFalsePass:
    """等权目标（Spearman 全域并列不可判）不得短路 PASS，须实际校验分配。"""

    @staticmethod
    def _ctx(actual: dict[str, float]) -> dict:
        return {
            "target_weights": {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3},
            "actual_values": actual,
        }

    def test_equal_target_severe_skew_fails(self):
        res = AllocationFidelityGate().evaluate(self._ctx({"A": 900000, "B": 50000, "C": 50000}))
        assert res.status == GateStatus.FAIL
        assert res.status != GateStatus.PASS

    def test_equal_target_only_one_built_fails(self):
        # 正是 A 路"日均持仓 0.5~1.8 只"的病理
        res = AllocationFidelityGate().evaluate(self._ctx({"A": 100000, "B": 0, "C": 0}))
        assert res.status == GateStatus.FAIL

    def test_equal_target_negative_value_fails(self):
        res = AllocationFidelityGate().evaluate(self._ctx({"A": -50000, "B": 80000, "C": 70000}))
        assert res.status == GateStatus.FAIL

    def test_equal_target_all_zero_inconclusive(self):
        res = AllocationFidelityGate().evaluate(self._ctx({"A": 0, "B": 0, "C": 0}))
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS

    def test_equal_target_near_equal_still_passes(self):
        # 正向：真正的等权执行（含整手/价格四舍五入）必须仍判 PASS（防改过头）
        res = AllocationFidelityGate().evaluate(self._ctx({"A": 34000, "B": 33000, "C": 33000}))
        assert res.status == GateStatus.PASS
        assert res.metrics["total_variation"] <= AllocationFidelityGate.EQUAL_WEIGHT_MAX_TV

    def test_uneven_target_branch_unaffected(self):
        # 既有活路径（不均目标 + 秩一致）不得被本轮改动影响
        res = AllocationFidelityGate().evaluate({
            "target_weights": {"A": 0.5, "B": 0.3, "C": 0.2},
            "actual_values": {"A": 50000.0, "B": 30000.0, "C": 20000.0},
        })
        assert res.status == GateStatus.PASS


# =====================================================================
# A-1 FeeSumBalanceGate：无 total_fee 不得默认"平衡"
# =====================================================================

class TestA1NoComparableFeeNoFalsePass:
    def test_trades_without_total_fee_inconclusive(self):
        res = FeeSumBalanceGate().evaluate(
            {"trades": [{"trade_id": "t1", "fees": {}}, {"trade_id": "t2", "fees": {}}]}
        )
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS
        assert res.metrics["checked_trades"] == 0

    def test_declared_zero_but_items_nonzero_fails(self):
        # total_fee 显式给 0，但七科目求和 > 0 ⇒ 不平，必须 FAIL（不得被 total_fee>0 条件短路）
        res = FeeSumBalanceGate().evaluate(
            {"trades": [{"trade_id": "t1", "fees": {"COMMISSION": Decimal("5.00")}, "total_fee": Decimal("0")}]}
        )
        assert res.status == GateStatus.FAIL

    def test_with_total_fee_still_passes(self):
        res = FeeSumBalanceGate().evaluate(
            {"trades": [{"trade_id": "t1", "fees": {"COMMISSION": Decimal("5.00")}, "total_fee": Decimal("5.00")}]}
        )
        assert res.status == GateStatus.PASS


# =====================================================================
# A-2 DailyCashConserveGate：缺锚点字段的行不得被 0 抹平
# =====================================================================

class TestA2MissingAnchorNoFalsePass:
    def test_rows_without_cash_anchors_inconclusive(self):
        res = DailyCashConserveGate().evaluate(
            {"daily_cash_flows": [{"date": "d1"}, {"date": "d2"}]}
        )
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS

    def test_partially_missing_anchor_inconclusive(self):
        res = DailyCashConserveGate().evaluate({
            "daily_cash_flows": [
                {"date": "d1", "cash_start": Decimal("100"), "cash_end": Decimal("50"), "trade_out": Decimal("50")},
                {"date": "d2"},
            ]
        })
        assert res.status == GateStatus.INCONCLUSIVE

    def test_complete_rows_still_pass(self):
        res = DailyCashConserveGate().evaluate({
            "daily_cash_flows": [
                {"date": "d1", "cash_start": Decimal("100"), "cash_end": Decimal("50"), "trade_out": Decimal("50")}
            ]
        })
        assert res.status == GateStatus.PASS


# =====================================================================
# A-4 SegmentRateScheduleGate：无卖出样本不得谎报"无穿越"
# =====================================================================

class TestA4NoSellSampleNoFalsePass:
    def test_buy_only_trades_skips(self):
        res = SegmentRateScheduleGate().evaluate(
            {"trades": [{"side": "BUY", "date": "2020-01-01", "price": 10, "volume": 100, "fees": {}}] * 3}
        )
        assert res.status == GateStatus.SKIP
        assert res.status != GateStatus.PASS

    def test_sell_without_date_inconclusive(self):
        res = SegmentRateScheduleGate().evaluate(
            {"trades": [{"side": "SELL", "price": Decimal("10"), "volume": 10000, "fees": {"STAMP_TAX": Decimal("100")}}]}
        )
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS

    def test_sell_with_date_still_passes(self):
        res = SegmentRateScheduleGate().evaluate({
            "trades": [{"date": "2023-08-20", "side": "SELL", "price": Decimal("10"),
                        "volume": 10000, "fees": {"STAMP_TAX": Decimal("100.00")}}]
        })
        assert res.status == GateStatus.PASS


# =====================================================================
# D-1 RawPriceJumpGate：无可比对相邻日不得判跳变正常
# =====================================================================

class TestD1NoValidPairNoFalsePass:
    def test_all_non_positive_closes_inconclusive(self):
        res = RawPriceJumpGate().evaluate(
            {"bars": [{"date": "d1", "close": 0.0}, {"date": "d2", "close": 0.0}, {"date": "d3", "close": 0.0}]}
        )
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS

    def test_normal_bars_still_pass(self):
        res = RawPriceJumpGate().evaluate(
            {"bars": [{"date": "d1", "close": 10.0}, {"date": "d2", "close": 10.5}, {"date": "d3", "close": 11.0}]}
        )
        assert res.status == GateStatus.PASS


# =====================================================================
# D-4 SuspensionVolumeGate：无停牌日不得判"所有停牌日成交量为0"
# =====================================================================

class TestD4NoSuspensionNoFalsePass:
    def test_no_suspension_days_skips(self):
        res = SuspensionVolumeGate().evaluate(
            {"bars": [{"date": "d1", "tradestatus": "1", "volume": 100},
                      {"date": "d2", "tradestatus": "1", "volume": 200}]}
        )
        assert res.status == GateStatus.SKIP
        assert res.status != GateStatus.PASS

    def test_suspension_with_zero_volume_still_passes(self):
        res = SuspensionVolumeGate().evaluate(
            {"bars": [{"date": "d1", "tradestatus": "1", "volume": 100},
                      {"date": "d2", "tradestatus": "0", "volume": 0}]}
        )
        assert res.status == GateStatus.PASS


# =====================================================================
# D-5 HighPriceLotGate：无买入单不得判"高价股/整手约束通过"
# =====================================================================

class TestD5NoBuyOrderNoFalsePass:
    def test_only_sell_orders_skips(self):
        res = HighPriceLotGate().evaluate(
            {"orders": [{"side": "SELL", "price": 500, "volume": 50},
                        {"side": "SELL", "price": 800, "volume": 30}]}
        )
        assert res.status == GateStatus.SKIP
        assert res.status != GateStatus.PASS

    def test_buy_orders_still_pass(self):
        res = HighPriceLotGate().evaluate({"orders": [{"side": "BUY", "price": 85.0, "volume": 200}]})
        assert res.status == GateStatus.PASS


# =====================================================================
# E-1 MustFailCasesGate：5 必挂用例不全不得谎报 100%
# =====================================================================

class TestE1IncompleteCasesNoFalsePass:
    def test_empty_results_inconclusive(self):
        res = MustFailCasesGate().evaluate({"must_fail_results": {}})
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS

    def test_unrelated_key_inconclusive(self):
        res = MustFailCasesGate().evaluate({"must_fail_results": {"SOMETHING": True}})
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS

    def test_partial_standard_cases_inconclusive(self):
        res = MustFailCasesGate().evaluate({"must_fail_results": {"LIMIT_UP_BUY_REJECT": True}})
        assert res.status == GateStatus.INCONCLUSIVE

    def test_full_five_cases_still_pass(self):
        res = MustFailCasesGate().evaluate(
            {"must_fail_results": {c: True for c in MustFailCasesGate.STANDARD_CASES}}
        )
        assert res.status == GateStatus.PASS

    def test_breach_still_fails(self):
        ctx = {c: True for c in MustFailCasesGate.STANDARD_CASES}
        ctx["LIMIT_UP_BUY_REJECT"] = False
        res = MustFailCasesGate().evaluate({"must_fail_results": ctx})
        assert res.status == GateStatus.FAIL


# =====================================================================
# E-3 SlippagePriceCapGate：无板价样本不得判限幅通过
# =====================================================================

class TestE3NoLimitEvidenceNoFalsePass:
    def test_trades_without_limits_inconclusive(self):
        res = SlippagePriceCapGate().evaluate(
            {"trades": [{"side": "BUY", "price": 100}, {"side": "BUY", "price": 200}]}
        )
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS

    def test_trades_with_limits_still_pass(self):
        res = SlippagePriceCapGate().evaluate({
            "trades": [{"side": "BUY", "price": Decimal("10.95"),
                        "limit_up": Decimal("11.0"), "limit_down": Decimal("9.0")}]
        })
        assert res.status == GateStatus.PASS


# =====================================================================
# S-2 TimingExitSurvivalGate：破位日缺仓位比例不得被 0 顶替
# =====================================================================

class TestS2MissingRatioNoFalsePass:
    def test_below_dates_without_ratio_inconclusive(self):
        res = TimingExitSurvivalGate().evaluate(
            {"index_below_ma200_dates": ["d1", "d2", "d3"], "daily_positions_ratio": {"x": 1.0}}
        )
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS

    def test_full_ratios_still_pass(self):
        res = TimingExitSurvivalGate().evaluate({
            "index_below_ma200_dates": ["d1", "d2"],
            "daily_positions_ratio": {"d1": 0.0, "d2": 0.02},
        })
        assert res.status == GateStatus.PASS


# =====================================================================
# S-3 DynamicSlippageAdvGate：压力情景收益 <=0 一律 FAIL
# =====================================================================

class TestS3NegativeStressNoFalsePass:
    def test_negative_stress_both_negative_fails(self):
        res = DynamicSlippageAdvGate().evaluate(
            {"orders_adv_ratio": [0.01], "baseline_return": -0.10, "stress_return": -0.20}
        )
        assert res.status == GateStatus.FAIL
        assert res.status != GateStatus.PASS

    def test_positive_stress_still_passes(self):
        res = DynamicSlippageAdvGate().evaluate(
            {"orders_adv_ratio": [0.005], "baseline_return": 0.15, "stress_return": 0.10}
        )
        assert res.status == GateStatus.PASS


# =====================================================================
# S-5 AttributionEvidenceGate：缺 trades_count + 零费不得静默跳过
# =====================================================================

class TestS5MissingTradesNoFalsePass:
    def test_missing_trades_count_with_zero_fees_inconclusive(self):
        res = AttributionEvidenceGate().evaluate(
            {"total_stamp_tax": 0, "total_commission": 0, "code_evidence": "scripts/x.py:12"}
        )
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS

    def test_explicit_trades_with_zero_tax_still_fails(self):
        res = AttributionEvidenceGate().evaluate(
            {"trades_count": 5, "total_stamp_tax": 0, "total_commission": Decimal("100"),
             "code_evidence": "x.py:L1"}
        )
        assert res.status == GateStatus.FAIL

    def test_trades_and_fees_still_pass(self):
        res = AttributionEvidenceGate().evaluate(
            {"trades_count": 20, "total_stamp_tax": Decimal("200"),
             "total_commission": Decimal("100"), "code_evidence": "x.py:L1"}
        )
        assert res.status == GateStatus.PASS


# =====================================================================
# G-REPRO-1：0 个同源分组不得谎报复现一致性成立
# =====================================================================

class TestReproNoGroupNoFalsePass:
    def test_single_fingerprinted_record_inconclusive(self):
        res = ReproducibilityGate().evaluate(
            {"run_records": [{"run_id": "r1", "repro_fingerprint": "abc", "metrics": {"a": 1}}]}
        )
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS

    def test_two_same_fingerprint_identical_still_passes(self):
        res = ReproducibilityGate().evaluate({
            "run_records": [
                {"run_id": "a", "repro_fingerprint": "f1", "metrics": {"x": 1}},
                {"run_id": "b", "repro_fingerprint": "f1", "metrics": {"x": 1}},
            ]
        })
        assert res.status == GateStatus.PASS


# =====================================================================
# G-REF-1：文档不可读不得判"引用路径全部存在"
# =====================================================================

class TestRefUnreadableNoFalsePass:
    def test_unreadable_doc_inconclusive(self):
        res = DocPathReferenceGate().evaluate({"doc_paths": ["/nonexistent/r1_missing.md"]})
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS


# =====================================================================
# GATE-R3：L-2 残留假通过（阈值边界 / NaN·inf 退化输入 / 判据与 n 解耦）
# =====================================================================

class TestL2R3NoResidualFalsePass:
    """L-2 等权分支：消除「恰在阈值上逃逸」「NaN/inf 退化输入」，判据与 n 解耦。"""

    @staticmethod
    def _eq(n: int) -> dict:
        return {f"S{i}": 1.0 / n for i in range(n)}

    def test_n10_one_missing_fails(self):
        # QA §2.2：n=10 恰 1 只漏建，旧 TV=0.1000 因 `TV > 0.10` 为 False ⇒ 假 PASS。现须 FAIL。
        n = 10
        av = {f"S{i}": (0.0 if i == 0 else 1.0 / (n - 1)) for i in range(n)}
        res = AllocationFidelityGate().evaluate({"target_weights": self._eq(n), "actual_values": av})
        assert res.status == GateStatus.FAIL
        assert res.status != GateStatus.PASS

    def test_n11_one_missing_fails(self):
        n = 11
        av = {f"S{i}": (0.0 if i == 0 else 1.0 / (n - 1)) for i in range(n)}
        res = AllocationFidelityGate().evaluate({"target_weights": self._eq(n), "actual_values": av})
        assert res.status == GateStatus.FAIL

    def test_n50_five_missing_fails(self):
        n = 50
        av = {f"S{i}": (0.0 if i < 5 else 1.0 / (n - 5)) for i in range(n)}
        res = AllocationFidelityGate().evaluate({"target_weights": self._eq(n), "actual_values": av})
        assert res.status == GateStatus.FAIL

    def test_unbuilt_count_is_n_decoupled(self):
        # 判据「任一票完全未建仓」与 n 解耦：n=10/11/20/50 均 FAIL（旧 TV 判据在大 n 全漏网）。
        for n in (10, 11, 20, 50):
            av = {f"S{i}": (0.0 if i == 0 else 1.0 / (n - 1)) for i in range(n)}
            res = AllocationFidelityGate().evaluate({"target_weights": self._eq(n), "actual_values": av})
            assert res.status == GateStatus.FAIL, f"n={n} 整只漏建必须 FAIL"

    def test_target_nan_inconclusive(self):
        res = AllocationFidelityGate().evaluate({
            "target_weights": {"A": float("nan"), "B": float("nan"), "C": float("nan")},
            "actual_values": {"A": 1000.0, "B": 1000.0, "C": 1000.0},
        })
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS

    def test_target_inf_not_pass(self):
        res = AllocationFidelityGate().evaluate({
            "target_weights": {"A": float("inf"), "B": float("inf"), "C": float("inf")},
            "actual_values": {"A": 1000.0, "B": 1000.0, "C": 1000.0},
        })
        assert res.status != GateStatus.PASS

    def test_actual_nan_fails(self):
        res = AllocationFidelityGate().evaluate({
            "target_weights": {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3},
            "actual_values": {"A": float("nan"), "B": 1000.0, "C": 1000.0},
        })
        assert res.status == GateStatus.FAIL
        assert res.status != GateStatus.PASS

    def test_actual_inf_fails(self):
        res = AllocationFidelityGate().evaluate({
            "target_weights": {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3},
            "actual_values": {"A": float("inf"), "B": 1000.0, "C": 1000.0},
        })
        assert res.status == GateStatus.FAIL

    def test_uneven_target_with_nan_actual_not_pass(self):
        # 非等权分支：旧实现 nan 参与 `nan < 0.90` 为 False ⇒ 假 PASS（QA 实测 9 例）。
        # ⛔ P3-④ 强化锁定：必须用**能区分修复前后**的输入。旧输入（target=0.5/0.3/0.2 + nan）在旧实现下
        #    也 FAIL（无区分力）；本输入 target={A:0.2,B:0.3,C:0.5} + actual={A:nan,B:30000,C:60000}
        #    在旧实现下 Spearman=1.0 ⇒ **假 PASS**，修复后入口有限性校验直接 FAIL（反转实现即变红）。
        res = AllocationFidelityGate().evaluate({
            "target_weights": {"A": 0.2, "B": 0.3, "C": 0.5},
            "actual_values": {"A": float("nan"), "B": 30000.0, "C": 60000.0},
        })
        assert res.status == GateStatus.FAIL
        assert res.status != GateStatus.PASS

    def test_severe_skew_still_fails(self):
        res = AllocationFidelityGate().evaluate({
            "target_weights": {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3},
            "actual_values": {"A": 900000, "B": 50000, "C": 50000},
        })
        assert res.status == GateStatus.FAIL

    def test_mild_manual_lot_skew_passes(self):
        # 正向对照：n=3 因整手约束的温和偏斜（40/40/20，相对权重比∈[0.5,2]）须 PASS（防改过头误杀）。
        res = AllocationFidelityGate().evaluate({
            "target_weights": {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3},
            "actual_values": {"A": 40000, "B": 40000, "C": 20000},
        })
        assert res.status == GateStatus.PASS

    def test_heavy_underweight_still_fails(self):
        # 45/45/10：一只仅得等权份额的 0.3 倍 ⇒ 越出容许带 ⇒ FAIL
        res = AllocationFidelityGate().evaluate({
            "target_weights": {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3},
            "actual_values": {"A": 45000, "B": 45000, "C": 10000},
        })
        assert res.status == GateStatus.FAIL

    def test_equal_target_near_equal_still_passes(self):
        res = AllocationFidelityGate().evaluate({
            "target_weights": {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3},
            "actual_values": {"A": 34000, "B": 33000, "C": 33000},
        })
        assert res.status == GateStatus.PASS


# =====================================================================
# GATE-R3：runner 逐票聚合（⛔ 不得"无 FAIL 即自造 PASS"）
# =====================================================================

class TestRunnerAggregationNoSyntheticPass:
    """runner D-1/D-4：逐票结果按 FAIL>INCONCLUSIVE>SKIP>PASS 上抛，INCONCLUSIVE/SKIP 必须透出。"""

    @staticmethod
    def _table(close, tradestatus, volume) -> dict:
        return {"s1": pd.DataFrame({
            "date": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "close": close,
            "tradestatus": tradestatus,
            "volume": volume,
        })}

    def test_d1_all_nonpositive_close_surfaces_inconclusive(self):
        # D-1 本体 INCONCLUSIVE（全零收盘）不得被合成 PASS 掩蔽。
        res = run_pre_run_gates(
            context={},
            tables=self._table([0.0, 0.0, 0.0], ["1", "1", "1"], [100.0, 100.0, 100.0]),
            strict=False,
        )
        d1 = next(r for r in res if r.gate_id == "D-1")
        assert d1.status == GateStatus.INCONCLUSIVE
        assert d1.status != GateStatus.PASS

    def test_d4_no_suspension_surfaces_skip(self):
        # D-4 本体 SKIP（样本内无停牌日）不得被合成 PASS 掩蔽。
        res = run_pre_run_gates(
            context={},
            tables=self._table([10.0, 10.1, 10.2], ["1", "1", "1"], [100.0, 100.0, 100.0]),
            strict=False,
        )
        d4 = next(r for r in res if r.gate_id == "D-4")
        assert d4.status == GateStatus.SKIP
        assert d4.status != GateStatus.PASS

    def test_d1_inconclusive_blocks_under_strict(self):
        from scripts.gates import GateBlockerError

        with pytest.raises(GateBlockerError) as ei:
            run_pre_run_gates(
                context={},
                tables=self._table([0.0, 0.0, 0.0], ["1", "1", "1"], [100.0, 100.0, 100.0]),
                strict=True,
            )
        assert ei.value.gate_id == "D-1"

    def test_normal_tables_still_pass(self):
        # 正向对照：正常日线 + 有停牌日成交量为 0 ⇒ D-1/D-4 均 PASS。
        res = run_pre_run_gates(
            context={},
            tables=self._table([10.0, 10.1, 10.2], ["1", "0", "1"], [100.0, 0.0, 100.0]),
            strict=False,
        )
        assert next(r for r in res if r.gate_id == "D-1").status == GateStatus.PASS
        assert next(r for r in res if r.gate_id == "D-4").status == GateStatus.PASS


# =====================================================================
# GATE-R3：P2 四处新漏网
# =====================================================================

class TestP2ResidualNoFalsePass:
    def test_g_stress_missing_days_inconclusive(self):
        # G-STRESS-1：有成交但缺 trading_days ⇒ 不得谎称"≥200 日"通过。
        res = StressValidityGate().evaluate({"round_trips": 5})
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS

    def test_g_stress_long_window_still_passes(self):
        res = StressValidityGate().evaluate({"round_trips": 40, "trading_days": 260})
        assert res.status == GateStatus.PASS

    def test_repro_empty_metrics_inconclusive(self):
        res = ReproducibilityGate().evaluate({"run_records": [
            {"run_id": "a", "repro_fingerprint": "f1", "metrics": {}},
            {"run_id": "b", "repro_fingerprint": "f1", "metrics": {}},
        ]})
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS

    def test_repro_missing_metrics_inconclusive(self):
        res = ReproducibilityGate().evaluate({"run_records": [
            {"run_id": "a", "repro_fingerprint": "f1"},
            {"run_id": "b", "repro_fingerprint": "f1"},
        ]})
        assert res.status == GateStatus.INCONCLUSIVE

    def test_repro_real_metrics_still_pass(self):
        res = ReproducibilityGate().evaluate({"run_records": [
            {"run_id": "a", "repro_fingerprint": "f1", "metrics": {"x": 1}},
            {"run_id": "b", "repro_fingerprint": "f1", "metrics": {"x": 1}},
        ]})
        assert res.status == GateStatus.PASS

    def test_g2_unchecked_not_pass(self):
        res = TasksSignGate().evaluate({"task_id": "T1", "is_checked": False})
        assert res.status != GateStatus.PASS
        assert res.status == GateStatus.SKIP

    def test_g2_checked_with_sig_still_pass(self):
        res = TasksSignGate().evaluate({"task_id": "T1", "is_checked": True, "gate_signature": "sig"})
        assert res.status == GateStatus.PASS

    def test_g1_nonhex_hash_fails(self):
        res = ProvenanceTriadGate().evaluate({
            "git_commit": "8fcf318a02c5f1b8a8e527d2c3e1e2d3f4a5b6c7",
            "data_hash": "zzzzzzzzzzzzzzzz",  # 16 位非 hex
            "timestamp": "2026-09-07T16:00:00Z",
        })
        assert res.status == GateStatus.FAIL
        assert res.status != GateStatus.PASS

    def test_g1_valid_hex_hash_still_pass(self):
        res = ProvenanceTriadGate().evaluate({
            "git_commit": "8fcf318a02c5f1b8a8e527d2c3e1e2d3f4a5b6c7",
            "data_hash": "a1b2c3d4e5f67890123456789abcdef0",
            "timestamp": "2026-09-07T16:00:00Z",
        })
        assert res.status == GateStatus.PASS


# =====================================================================
# GATE-R5/R7：A-3 判定对齐声明阈值（基准集 = 引擎权威 112.82 + 行业含规费全佣 102.00）
# =====================================================================

class TestA3GoldenBasisAlignment:
    """A-3：threshold_desc 声明「绝对误差 <= 0.05」，实现取与最近合法基准的绝对误差。

    ⛔ GATE-R7：旧基准集 (114.20, 103.22) 属注释算错(经手误用 4.10)/全仓无出处，
       会误杀引擎权威黄金 112.82（`tests/test_t203_fees.py::test_golden_round_trip_100k`）；已删除。
    """

    def test_engine_authoritative_golden_112_82_passes(self):
        # 引擎权威逐项口径黄金值（test_t203_fees 断言 buy31.41+sell81.41=112.82）⇒ 必须 PASS。
        res = GoldenRoundtripGate().evaluate({"roundtrip_total_fee": Decimal("112.82")})
        assert res.status == GateStatus.PASS
        assert res.status != GateStatus.FAIL

    def test_industry_bundled_102_00_passes(self):
        res = GoldenRoundtripGate().evaluate({"roundtrip_total_fee": Decimal("102.00")})
        assert res.status == GateStatus.PASS

    def test_within_tolerance_of_engine_basis_passes(self):
        # 112.80 距最近基准 112.82 仅 0.02 <= 0.05 ⇒ PASS（端点内合法）。
        res = GoldenRoundtripGate().evaluate({"roundtrip_total_fee": Decimal("112.80")})
        assert res.status == GateStatus.PASS

    def test_removed_basis_114_20_now_fails(self):
        # 114.20 系旧注释「经手误用 4.10」算错值，距最近基准 112.82 差 1.38 > 0.05 ⇒ FAIL。
        res = GoldenRoundtripGate().evaluate({"roundtrip_total_fee": Decimal("114.20")})
        assert res.status == GateStatus.FAIL
        assert res.status != GateStatus.PASS

    def test_removed_basis_103_22_now_fails(self):
        # 103.22 全仓无出处，距最近基准 102.00 差 1.22 > 0.05 ⇒ FAIL。
        res = GoldenRoundtripGate().evaluate({"roundtrip_total_fee": Decimal("103.22")})
        assert res.status == GateStatus.FAIL
        assert res.status != GateStatus.PASS

    def test_r4_regression_95_5_now_fails(self):
        # QA §R4 反例：95.5 距最近基准 102.00 差 6.50（旧实现 95~125 宽区间假 PASS）；现须 FAIL。
        res = GoldenRoundtripGate().evaluate({"roundtrip_total_fee": Decimal("95.5")})
        assert res.status == GateStatus.FAIL
        assert res.status != GateStatus.PASS

    def test_r4_regression_125_0_now_fails(self):
        # QA §R4 反例：125.0 距最近基准 112.82 差 12.18（旧实现假 PASS）；现须 FAIL。
        res = GoldenRoundtripGate().evaluate({"roundtrip_total_fee": Decimal("125.0")})
        assert res.status == GateStatus.FAIL

    def test_r4_regression_100_0_now_fails(self):
        # QA §R4 反例：100.0 距最近基准 102.00 差 2.00（旧实现假 PASS）；现须 FAIL。
        res = GoldenRoundtripGate().evaluate({"roundtrip_total_fee": Decimal("100.0")})
        assert res.status == GateStatus.FAIL

    def test_fail_message_reports_actual_nearest_basis_and_diff(self):
        # FAIL 报文必须点明：实际值 + 最近基准 + 其口径 + 绝对误差 + 容差。
        res = GoldenRoundtripGate().evaluate({"roundtrip_total_fee": Decimal("95.5")})
        assert res.status == GateStatus.FAIL
        assert "95.5" in res.message                      # 实际值
        assert "102.00" in res.message                    # 最近基准
        assert "6.50" in res.message                      # 绝对误差
        assert "0.05" in res.message                      # 容差

    def test_missing_roundtrip_fee_inconclusive(self):
        res = GoldenRoundtripGate().evaluate({"something_else": 1})
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS

    def test_expected_fee_override_is_sole_basis(self):
        # 显式 expected_fee=100.00 ⇒ 唯一基准：100.05 在容差内 PASS；
        # 若覆盖未生效，100.05 距内置最近基准 102.00 差 1.95 ⇒ 必 FAIL。以此证明覆盖生效。
        gate = GoldenRoundtripGate()
        ok = gate.evaluate({"roundtrip_total_fee": Decimal("100.05"), "expected_fee": Decimal("100.00")})
        assert ok.status == GateStatus.PASS
        bad = gate.evaluate({"roundtrip_total_fee": Decimal("100.10"), "expected_fee": Decimal("100.00")})
        assert bad.status == GateStatus.FAIL

    def test_expected_fee_override_can_reject_builtin_basis_value(self):
        # 覆盖为唯一基准：112.82 本是内置基准，但 expected_fee=100.00 时差 12.82 ⇒ FAIL。
        res = GoldenRoundtripGate().evaluate({
            "roundtrip_total_fee": Decimal("112.82"), "expected_fee": Decimal("100.00"),
        })
        assert res.status == GateStatus.FAIL

    def test_golden_basis_constants_locked(self):
        # 单一事实源锁定：两个合法基准 + 容差，⛔ 不得静默漂移（114.20/103.22 已删除）。
        assert GoldenRoundtripGate.GOLDEN_FEE_BASIS == (Decimal("112.82"), Decimal("102.00"))
        assert GoldenRoundtripGate.GOLDEN_FEE_ABS_TOLERANCE == Decimal("0.05")

    def test_threshold_desc_lists_corrected_bases(self):
        desc = GoldenRoundtripGate.threshold_desc
        assert "112.82" in desc and "102.00" in desc
        assert "114.20" not in desc and "103.22" not in desc
        assert "0.05" in desc

    # ---- 兜底（QA §R5 登记）：非法输入不得崩溃、不得 PASS ----

    def test_nonfinite_fee_fails(self):
        res = GoldenRoundtripGate().evaluate({"roundtrip_total_fee": float("nan")})
        assert res.status == GateStatus.FAIL
        assert res.status != GateStatus.PASS

    def test_invalid_roundtrip_fee_types_do_not_crash(self):
        # QA §R5：roundtrip_total_fee = "abc" / "" / True / [103.22] 等均须**不抛异常**且非 PASS。
        for bad in ("abc", "", True, False, [103.22], {"x": 1}, None):
            res = GoldenRoundtripGate().evaluate({"roundtrip_total_fee": bad})
            assert res.status != GateStatus.PASS, f"非法实测值 {bad!r} 不得 PASS"
            assert res.status in (GateStatus.FAIL, GateStatus.INCONCLUSIVE)

    def test_nonfinite_expected_fee_does_not_crash(self):
        # expected_fee = NaN/±inf ⇒ 不得崩溃、不得 PASS（无有效外部基准 ⇒ INCONCLUSIVE）。
        for bad in (float("nan"), float("inf"), float("-inf")):
            res = GoldenRoundtripGate().evaluate({
                "roundtrip_total_fee": Decimal("112.82"), "expected_fee": bad,
            })
            assert res.status != GateStatus.PASS
            assert res.status == GateStatus.INCONCLUSIVE

    def test_invalid_expected_fee_types_do_not_crash(self):
        for bad in ("abc", "", True, [100.0], {"x": 1}):
            res = GoldenRoundtripGate().evaluate({
                "roundtrip_total_fee": Decimal("112.82"), "expected_fee": bad,
            })
            assert res.status != GateStatus.PASS
            assert res.status == GateStatus.INCONCLUSIVE


# =====================================================================
# GATE-R5：L-2 端点浮点确定性 + 消息口径拆分 + threshold_desc 等价声明（P3 ①/②/③）
# =====================================================================

class TestL2EndpointDeterminismAndWording:
    """GATE-R5 P3：端点含端点判定确定（不受浮点支配）+ "全为0"≠"等权均分"口径拆分。"""

    @staticmethod
    def _eq(n: int) -> dict:
        return {f"S{i}": 1.0 / n for i in range(n)}

    def test_n5_nominal_endpoint_passes(self):
        # 名义 r_i = [2.0, 0.5, 5/6, 5/6, 5/6]，恰在 [0.5, 2.0] 端点（含端点）⇒ 须 PASS。
        # 浮点归一化会把 max 算成 2.0000000000000004、min 算成 0.5000000000000001
        # （旧严格 `>2.0 / <0.5` ⇒ 端点被误判 FAIL）。修复后端点确定、判 PASS。
        av = {"S0": 0.4, "S1": 0.1, "S2": 1 / 6, "S3": 1 / 6, "S4": 1 / 6}
        res = AllocationFidelityGate().evaluate({"target_weights": self._eq(5), "actual_values": av})
        assert res.status == GateStatus.PASS

    def test_n4_nominal_endpoint_passes(self):
        # 同构 n=4：名义 r_i=[2.0, 0.5, 0.75, 0.75] ⇒ PASS（端点确定性、与 n 无关）。
        av = {"S0": 0.5, "S1": 0.125, "S2": 0.1875, "S3": 0.1875}
        res = AllocationFidelityGate().evaluate({"target_weights": self._eq(4), "actual_values": av})
        assert res.status == GateStatus.PASS

    def test_just_beyond_endpoint_still_fails(self):
        # 端点容差只包容浮点噪声：真正越界（max r=2.5）仍须 FAIL（防"容差变宽松口子"）。
        av = {"S0": 0.5, "S1": 0.2, "S2": 0.1, "S3": 0.1, "S4": 0.1}
        res = AllocationFidelityGate().evaluate({"target_weights": self._eq(5), "actual_values": av})
        assert res.status == GateStatus.FAIL

    def test_uneven_target_all_zero_inconclusive_wording(self):
        # P3-②：非等权目标 + 实际全 0 —— **并非等权均分** ⇒ INCONCLUSIVE，
        # 消息须明确"整只未建仓/全为 0"，⛔ 不得再输出"完全等权均分"。
        res = AllocationFidelityGate().evaluate({
            "target_weights": {"A": 0.6, "B": 0.3, "C": 0.1},
            "actual_values": {"A": 0.0, "B": 0.0, "C": 0.0},
        })
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS
        assert "完全等权均分" not in res.message
        assert ("全为 0" in res.message) or ("未建仓" in res.message)

    def test_uneven_target_all_equal_nonzero_fail_wording(self):
        # 对照：实际各票相等且非零 ⇒ 这才是"被抹平等权"⇒ FAIL，消息含"完全等权均分"。
        res = AllocationFidelityGate().evaluate({
            "target_weights": {"A": 0.6, "B": 0.3, "C": 0.1},
            "actual_values": {"A": 1.0, "B": 1.0, "C": 1.0},
        })
        assert res.status == GateStatus.FAIL
        assert "完全等权均分" in res.message

    def test_threshold_desc_states_endpoint_and_equivalence(self):
        # P3-③：threshold_desc 须明示"含端点"与等价式（max/min 仓位比 ≤ 4×）。
        desc = AllocationFidelityGate.threshold_desc
        assert "[0.5, 2.0]" in desc
        assert "含端点" in desc
        assert "4×" in desc


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))