#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""六维质量防伪门禁自动化单测套件 (Test Suite for D-L-E-A-S-G Gates)

覆盖六大维度 18+ 道门禁的 PASS、FAIL 阻断与边界异常，确保所有门禁具备坚固的机读断言能力。
"""

import datetime
from decimal import Decimal
import os
import pytest

from scripts.gates import (
    BaseGate,
    GateBlockerError,
    GateCategory,
    GateResult,
    GateSeverity,
    GateStatus,
    # D-Gate
    RawPriceJumpGate,
    FloatMarketCapGate,
    PitDividendYieldGate,
    SuspensionVolumeGate,
    HighPriceLotGate,
    # L-Gate
    FeatureLivenessGate,
    AllocationFidelityGate,
    StaticAstCallGate,
    # E-Gate
    MustFailCasesGate,
    BonusSplitFifoGate,
    SlippagePriceCapGate,
    # A-Gate
    FeeSumBalanceGate,
    DailyCashConserveGate,
    GoldenRoundtripGate,
    SegmentRateScheduleGate,
    # S-Gate
    TurnoverCeilingGate,
    TimingExitSurvivalGate,
    DynamicSlippageAdvGate,
    DividendTaxLockGate,
    AttributionEvidenceGate,
    # G-Gate
    ProvenanceTriadGate,
    TasksSignGate,
    MasterFindingGate,
)
from scripts.gates.gate_master_audit import GateMasterAudit


# =====================================================================
# 1. D-Gate (数据真值与反未来门禁) 测试
# =====================================================================

class TestDGate:
    """D-Gate 测试套件"""

    def test_d1_raw_price_jump_pass(self):
        gate = RawPriceJumpGate(max_jump_ratio=0.30)
        bars = [
            {"date": "2024-01-02", "close": 10.0, "is_exdiv": False},
            {"date": "2024-01-03", "close": 10.5, "is_exdiv": False},
            {"date": "2024-01-04", "close": 11.2, "is_exdiv": False},
        ]
        res = gate.evaluate({"bars": bars, "symbol": "sh.600000"})
        assert res.status == GateStatus.PASS

    def test_d1_raw_price_jump_fail_dirty_data(self):
        gate = RawPriceJumpGate(max_jump_ratio=0.30)
        # 单日收盘从 10 飙升到 25 (150% 跳变，非除权，Baostock 后复权典型硬伤)
        bars = [
            {"date": "2024-01-02", "close": 10.0, "is_exdiv": False},
            {"date": "2024-01-03", "close": 25.0, "is_exdiv": False},
        ]
        res = gate.evaluate({"bars": bars, "symbol": "sh.600000"})
        assert res.status == GateStatus.FAIL
        assert "异常日跳变" in res.message

    def test_d1_raw_price_jump_pass_with_exdiv(self):
        gate = RawPriceJumpGate(max_jump_ratio=0.30)
        # 除权除息日价格跳变允许豁免
        bars = [
            {"date": "2024-01-02", "close": 20.0, "is_exdiv": False},
            {"date": "2024-01-03", "close": 10.0, "is_exdiv": True},
        ]
        res = gate.evaluate({"bars": bars, "symbol": "sh.600000"})
        assert res.status == GateStatus.PASS

    def test_d2_float_market_cap_pass(self):
        gate = FloatMarketCapGate()
        # 30 只股票，市值分布在 500 亿~ 1500 亿 (Std > 100 亿)，成交额 1 亿~ 5 亿 (偏离度 > 95%)
        mvs = [Decimal(f"{500 + i * 50}00000000") for i in range(35)]
        amts = [Decimal("200000000") for _ in range(35)]
        res = gate.evaluate({"float_mv_list": mvs, "amount_list": amts})
        assert res.status == GateStatus.PASS

    def test_d2_float_market_cap_fail_amount_as_mv(self):
        gate = FloatMarketCapGate()
        # 10 只样本 (避开 >=30 的标准差检查)，用成交额冒充流通市值 (偏离度 0%)
        mvs = [Decimal(f"{200 + i * 50}00000000") for i in range(10)]
        amts = [Decimal(f"{200 + i * 50}00000000") for i in range(10)]
        res = gate.evaluate({"float_mv_list": mvs, "amount_list": amts})
        assert res.status == GateStatus.FAIL
        assert "存在将成交额当作市值的伪造特征" in res.message

    def test_d2_float_market_cap_fail_low_std(self):
        gate = FloatMarketCapGate()
        # 市值完全固定，标准差为 0
        mvs = [Decimal("50000000000") for _ in range(35)]
        amts = [Decimal("200000000") for _ in range(35)]
        res = gate.evaluate({"float_mv_list": mvs, "amount_list": amts})
        assert res.status == GateStatus.FAIL
        assert "流通市值分布过于集中" in res.message

    def test_d3_pit_dividend_yield_pass(self):
        gate = PitDividendYieldGate()
        # 年内动态变异值 60 种
        yields = [0.03 + (i % 60) * 0.001 for i in range(240)]
        res = gate.evaluate({"daily_yields": yields, "year": 2023})
        assert res.status == GateStatus.PASS

    def test_d3_pit_dividend_yield_fail_static_leak(self):
        gate = PitDividendYieldGate()
        # 全年单一常数均值 (只有 1 种值，未来函数泄露)
        yields = [0.045 for _ in range(240)]
        res = gate.evaluate({"daily_yields": yields, "year": 2023})
        assert res.status == GateStatus.FAIL
        assert "静态未来函数泄露" in res.message

    def test_d4_suspension_volume_pass(self):
        gate = SuspensionVolumeGate()
        bars = [
            {"date": "2024-01-02", "tradestatus": "1", "volume": 100000},
            {"date": "2024-01-03", "tradestatus": "0", "volume": 0},
        ]
        res = gate.evaluate({"bars": bars})
        assert res.status == GateStatus.PASS

    def test_d4_suspension_volume_fail_dirty_volume(self):
        gate = SuspensionVolumeGate()
        # 停牌日返回非零成交量 (脏数据)
        bars = [
            {"date": "2024-01-02", "tradestatus": "1", "volume": 100000},
            {"date": "2024-01-03", "tradestatus": "0", "volume": 5000},
        ]
        res = gate.evaluate({"bars": bars})
        assert res.status == GateStatus.FAIL
        assert "停牌日成交量非零脏数据" in res.message

    def test_d5_high_price_lot_pass(self):
        gate = HighPriceLotGate()
        orders = [
            {"side": "BUY", "price": 85.0, "volume": 200},
            {"side": "BUY", "price": 120.0, "volume": 500},
            {"side": "SELL", "price": 90.0, "volume": 123},  # 卖出允许碎股
        ]
        res = gate.evaluate({"orders": orders})
        assert res.status == GateStatus.PASS

    def test_d5_high_price_lot_fail_odd_and_price(self):
        gate = HighPriceLotGate()
        orders = [
            {"side": "BUY", "price": 350.0, "volume": 200},  # 单价 > 300
            {"side": "BUY", "price": 50.0, "volume": 150},   # 买入非整手
        ]
        res = gate.evaluate({"orders": orders})
        assert res.status == GateStatus.FAIL
        assert res.metrics["violations_count"] == 2


# =====================================================================
# 2. L-Gate (调用存活与参数落地门禁) 测试
# =====================================================================

class TestLGate:
    """L-Gate 测试套件"""

    def test_l1_feature_liveness_pass(self):
        gate = FeatureLivenessGate()
        context = {
            "active_features": ["DIVIDEND_TAX"],
            "fee_summary": {"DIVIDEND_TAX": Decimal("125.50")},
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.PASS

    def test_l1_feature_liveness_fail_dead_code(self):
        gate = FeatureLivenessGate()
        # 声明启用了 DIVIDEND_TAX，但在账本中金额为 0
        context = {
            "active_features": ["DIVIDEND_TAX"],
            "fee_summary": {"DIVIDEND_TAX": Decimal("0.00")},
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.FAIL
        assert "死代码" in res.message

    def test_l2_allocation_fidelity_pass(self):
        gate = AllocationFidelityGate()
        tw = {"sh.600000": 0.5, "sh.600036": 0.3, "sh.601398": 0.2}
        av = {"sh.600000": 50000.0, "sh.600036": 30000.0, "sh.601398": 20000.0}
        res = gate.evaluate({"target_weights": tw, "actual_values": av})
        assert res.status == GateStatus.PASS

    def test_l2_allocation_fidelity_fail_forced_equal(self):
        gate = AllocationFidelityGate()
        tw = {"sh.600000": 0.6, "sh.600036": 0.3, "sh.601398": 0.1}
        # 实际分配被强制抹平为 1/3 等权
        av = {"sh.600000": 33333.3, "sh.600036": 33333.3, "sh.601398": 33333.3}
        res = gate.evaluate({"target_weights": tw, "actual_values": av})
        assert res.status == GateStatus.FAIL
        assert "完全等权均分" in res.message

    def test_l3_static_ast_call_pass(self):
        gate = StaticAstCallGate()
        context = {
            "executed_calls": ["apply_dividend_tax", "check_risk_limit"],
            "required_calls": ["apply_dividend_tax"],
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.PASS

    def test_l3_static_ast_call_fail_missing_exec(self):
        gate = StaticAstCallGate()
        context = {
            "executed_calls": ["submit_order"],
            "required_calls": ["apply_slippage", "check_risk_limit"],
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.FAIL
        assert "运行时关键调用未发生" in res.message


# =====================================================================
# 3. E-Gate (撮合保真与极端事件门禁) 测试
# =====================================================================

class TestEGate:
    """E-Gate 测试套件"""

    def test_e1_must_fail_cases_pass(self):
        gate = MustFailCasesGate()
        context = {
            "must_fail_results": {
                "LIMIT_UP_BUY_REJECT": True,
                "LIMIT_DOWN_SELL_REJECT": True,
                "SUSPENSION_REJECT": True,
                "EXDIV_CONTINUOUS_NAV": True,
                "T1_SAME_DAY_SELL_REJECT": True,
            }
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.PASS

    def test_e1_must_fail_cases_fail_breach(self):
        gate = MustFailCasesGate()
        # 涨停板买单被放行了 (未拒绝)
        context = {
            "must_fail_results": {
                "LIMIT_UP_BUY_REJECT": False,
                "LIMIT_DOWN_SELL_REJECT": True,
                "SUSPENSION_REJECT": True,
                "EXDIV_CONTINUOUS_NAV": True,
                "T1_SAME_DAY_SELL_REJECT": True,
            }
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.FAIL
        assert "撮合引擎保真性破产" in res.message

    def test_e2_bonus_split_fifo_pass(self):
        gate = BonusSplitFifoGate()
        context = {
            "fifo_errors": [],
            "final_positions": {"sh.600000": 0, "sh.600036": 0},
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.PASS

    def test_e2_bonus_split_fifo_fail_deficiency(self):
        gate = BonusSplitFifoGate()
        context = {
            "fifo_errors": ["LedgerError: sellable insufficient for sh.600000 (requested 1500, available 1000)"],
            "final_positions": {"sh.600000": 500},
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.FAIL
        assert "缺股击穿异常" in res.message

    def test_e3_slippage_price_cap_pass(self):
        gate = SlippagePriceCapGate()
        trades = [
            {"symbol": "sh.600000", "side": "BUY", "price": Decimal("10.95"), "limit_up": Decimal("11.00"), "limit_down": Decimal("9.00")},
            {"symbol": "sh.600000", "side": "SELL", "price": Decimal("9.05"), "limit_up": Decimal("11.00"), "limit_down": Decimal("9.00")},
        ]
        res = gate.evaluate({"trades": trades})
        assert res.status == GateStatus.PASS

    def test_e3_slippage_price_cap_fail_breach_limit_up(self):
        gate = SlippagePriceCapGate()
        # 加滑点后买入价突破 11.00 涨停板
        trades = [
            {"symbol": "sh.600000", "side": "BUY", "price": Decimal("11.02"), "limit_up": Decimal("11.00"), "limit_down": Decimal("9.00")},
        ]
        res = gate.evaluate({"trades": trades})
        assert res.status == GateStatus.FAIL
        assert "突破涨跌停板价限幅" in res.message


# =====================================================================
# 4. A-Gate (双账本分厘级会计对账门禁) 测试
# =====================================================================

class TestAGate:
    """A-Gate 测试套件"""

    def test_a1_fee_sum_balance_pass(self):
        gate = FeeSumBalanceGate()
        trades = [
            {
                "trade_id": "T1",
                "fees": {
                    "COMMISSION": Decimal("5.00"),
                    "STAMP_TAX": Decimal("0.00"),
                    "TRANSFER_FEE": Decimal("0.20"),
                    "HANDLING_FEE": Decimal("0.41"),
                },
                "total_fee": Decimal("5.61"),
            }
        ]
        res = gate.evaluate({"trades": trades})
        assert res.status == GateStatus.PASS

    def test_a1_fee_sum_balance_fail_discrepancy(self):
        gate = FeeSumBalanceGate()
        trades = [
            {
                "trade_id": "T1",
                "fees": {
                    "COMMISSION": Decimal("5.00"),
                    "TRANSFER_FEE": Decimal("0.20"),
                },
                "total_fee": Decimal("6.00"),  # 声明 6.00，实际只有 5.20，差 0.80
            }
        ]
        res = gate.evaluate({"trades": trades})
        assert res.status == GateStatus.FAIL
        assert "七科目费用求和与总费用不平" in res.message

    def test_a2_daily_cash_conserve_pass(self):
        gate = DailyCashConserveGate()
        flows = [
            {
                "date": "2024-01-02",
                "cash_start": Decimal("100000.00"),
                "trade_in": Decimal("0.00"),
                "trade_out": Decimal("50000.00"),
                "fee_out": Decimal("25.00"),
                "dividend_in": Decimal("0.00"),
                "dividend_tax_out": Decimal("0.00"),
                "cash_end": Decimal("49975.00"),
            }
        ]
        res = gate.evaluate({"daily_cash_flows": flows})
        assert res.status == GateStatus.PASS

    def test_a2_daily_cash_conserve_fail_leak(self):
        gate = DailyCashConserveGate()
        flows = [
            {
                "date": "2024-01-02",
                "cash_start": Decimal("100000.00"),
                "trade_in": Decimal("0.00"),
                "trade_out": Decimal("50000.00"),
                "fee_out": Decimal("25.00"),
                "dividend_in": Decimal("0.00"),
                "dividend_tax_out": Decimal("0.00"),
                "cash_end": Decimal("49900.00"),  # 缺失 75 元
            }
        ]
        res = gate.evaluate({"daily_cash_flows": flows})
        assert res.status == GateStatus.FAIL
        assert "存在未解释账本漏损" in res.message

    def test_a3_golden_roundtrip_pass(self):
        gate = GoldenRoundtripGate()
        # 引擎权威逐项口径黄金值（tests/test_t203_fees.py::test_golden_round_trip_100k 锁定）。
        res = gate.evaluate({"roundtrip_total_fee": Decimal("112.82")})
        assert res.status == GateStatus.PASS

    def test_a3_golden_roundtrip_fail_out_of_bounds(self):
        gate = GoldenRoundtripGate()
        # 费用为 50 元 (严重少收，比如把印花税漏了)：距最近基准 102.00 差 52 元 ⇒ 超差 FAIL。
        res = gate.evaluate({"roundtrip_total_fee": Decimal("50.00")})
        assert res.status == GateStatus.FAIL
        assert "超出容许容差" in res.message

    def test_a4_segment_rate_schedule_pass(self):
        gate = SegmentRateScheduleGate()
        trades = [
            # 2023-08-20 卖出 10 万元，印花税 100 元 (1‰)
            {"date": "2023-08-20", "side": "SELL", "price": Decimal("10"), "volume": 10000, "fees": {"STAMP_TAX": Decimal("100.00")}},
            # 2023-09-01 卖出 10 万元，印花税 50 元 (0.5‰)
            {"date": "2023-09-01", "side": "SELL", "price": Decimal("10"), "volume": 10000, "fees": {"STAMP_TAX": Decimal("50.00")}},
        ]
        res = gate.evaluate({"trades": trades})
        assert res.status == GateStatus.PASS

    def test_a4_segment_rate_schedule_fail_time_travel(self):
        gate = SegmentRateScheduleGate()
        # 2023-08-20 卖出 10 万元，错误使用了减半后的 50 元 (穿越少扣税)
        trades = [
            {"date": "2023-08-20", "side": "SELL", "price": Decimal("10"), "volume": 10000, "fees": {"STAMP_TAX": Decimal("50.00")}},
        ]
        res = gate.evaluate({"trades": trades})
        assert res.status == GateStatus.FAIL
        assert "少扣税" in res.message and "穿越" in res.message


# =====================================================================
# 5. S-Gate (散户小资金科学防伪与择时生存门禁) 测试
# =====================================================================

class TestSGate:
    """S-Gate 测试套件"""

    def test_s1_turnover_ceiling_pass(self):
        gate = TurnoverCeilingGate(max_turnover=4.0)
        res = gate.evaluate({"annualized_turnover": 3.2})
        assert res.status == GateStatus.PASS

    def test_s1_turnover_ceiling_fail_high_turnover(self):
        gate = TurnoverCeilingGate(max_turnover=4.0)
        res = gate.evaluate({"annualized_turnover": 5.8})
        assert res.status == GateStatus.FAIL
        assert "超过散户硬顶 400%" in res.message

    def test_s2_timing_exit_survival_pass(self):
        gate = TimingExitSurvivalGate()
        context = {
            "index_below_ma200_dates": ["2024-01-15", "2024-01-16"],
            "daily_positions_ratio": {"2024-01-15": 0.0, "2024-01-16": 0.02},
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.PASS

    def test_s2_timing_exit_survival_fail_carrying(self):
        gate = TimingExitSurvivalGate()
        context = {
            "index_below_ma200_dates": ["2024-01-15"],
            "daily_positions_ratio": {"2024-01-15": 0.95},  # 破位熊市满仓死扛
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.FAIL
        assert "熊市死扛重大违规" in res.message

    def test_s3_dynamic_slippage_adv_pass(self):
        gate = DynamicSlippageAdvGate()
        context = {
            "orders_adv_ratio": [0.005, 0.012],
            "baseline_return": 0.15,
            "stress_return": 0.10,
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.PASS

    def test_s3_dynamic_slippage_adv_fail_capacity(self):
        gate = DynamicSlippageAdvGate()
        context = {
            "orders_adv_ratio": [0.035],  # 3.5% > 2% ADV
            "baseline_return": 0.15,
            "stress_return": 0.10,
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.FAIL
        assert "超过散户 ADV 2% 无冲击阈值" in res.message

    def test_s3_dynamic_slippage_adv_fail_stress(self):
        gate = DynamicSlippageAdvGate()
        context = {
            "orders_adv_ratio": [0.01],
            "baseline_return": 0.08,
            "stress_return": -0.04,  # 加滑点由正转负崩塌
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.FAIL
        assert "压力测试失败" in res.message

    def test_s4_dividend_tax_lock_pass(self):
        gate = DividendTaxLockGate()
        context = {
            "penalty_tax_amount": Decimal("100.00"),
            "total_dividend_received": Decimal("2000.00"),  # 5% <= 20%
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.PASS

    def test_s4_dividend_tax_lock_fail_penalty(self):
        gate = DividendTaxLockGate()
        context = {
            "penalty_tax_amount": Decimal("500.00"),
            "total_dividend_received": Decimal("1000.00"),  # 50% > 20%
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.FAIL
        assert "盲目调仓引发严重跨期税损" in res.message

    def test_s5_attribution_evidence_pass(self):
        gate = AttributionEvidenceGate()
        context = {
            "trades_count": 20,
            "total_stamp_tax": Decimal("200.00"),
            "total_commission": Decimal("100.00"),
            "code_evidence": "backtest/metrics.py:L142",
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.PASS

    def test_s5_attribution_evidence_fail_tariff_cheat(self):
        gate = AttributionEvidenceGate()
        context = {
            "trades_count": 20,
            "total_stamp_tax": Decimal("0.00"),  # 关税置零作弊
            "total_commission": Decimal("100.00"),
            "code_evidence": "backtest/metrics.py:L142",
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.FAIL
        assert "关税作弊" in res.message


# =====================================================================
# 6. G-Gate (工程物理留痕与交付门禁) 测试
# =====================================================================

class TestGGate:
    """G-Gate 测试套件"""

    def test_g1_provenance_triad_pass(self):
        gate = ProvenanceTriadGate()
        context = {
            "git_commit": "8fcf318a02c5f1b8a8e527d2c3e1e2d3f4a5b6c7",
            "data_hash": "a1b2c3d4e5f67890123456789abcdef0",
            "timestamp": "2026-09-07T16:00:00Z",
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.PASS

    def test_g1_provenance_triad_fail_missing(self):
        gate = ProvenanceTriadGate()
        context = {
            "git_commit": "",
            "data_hash": "abc",
            "timestamp": "",
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.FAIL
        assert "出处三件套不完整" in res.message

    def test_g2_tasks_sign_pass(self):
        gate = TasksSignGate()
        context = {
            "task_id": "T401",
            "is_checked": True,
            "gate_signature": "audit_report_sha256_pass",
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.PASS

    def test_g2_tasks_sign_fail_unsigned_check(self):
        gate = TasksSignGate()
        context = {
            "task_id": "T401",
            "is_checked": True,
            "gate_signature": None,  # 无签名勾选
        }
        res = gate.evaluate(context)
        assert res.status == GateStatus.FAIL
        assert "涉嫌虚假汇报" in res.message

    def test_g3_master_finding_pass(self):
        gate = MasterFindingGate()
        res = gate.evaluate()
        # 真实验证母库只读区 FINDING- 守卫行数严格等于 370 行
        assert res.status == GateStatus.PASS
        assert res.metrics["count"] == 370

    def test_g3_master_finding_fail_tampered(self, tmp_path):
        # 伪造一个只有 10 行 FINDING 的目录进行测试
        fake_dir = tmp_path / "sources"
        fake_dir.mkdir()
        fake_file = fake_dir / "mod.py"
        fake_file.write_text("# FINDING-1\n# FINDING-2\n", encoding="utf-8")

        gate = MasterFindingGate(sources_dir=str(fake_dir))
        res = gate.evaluate()
        assert res.status == GateStatus.FAIL
        assert "违背恒等于 370 行铁律" in res.message


# =====================================================================
# 7. GateMasterAudit 总调度器测试
# =====================================================================

class TestGateMasterAudit:
    """总调度器跑测"""

    def test_master_audit_run_all(self):
        master = GateMasterAudit()
        assert len(master.gates) >= 18
        results = master.audit(context={})
        assert len(results) == len(master.gates)

    def test_master_audit_strict_blocker(self):
        # 使用一个故意 FAIL 的 Blocker 门禁
        class FailingGate(BaseGate):
            gate_id = "X-1"
            name = "Failing Gate"
            category = GateCategory.D_GATE
            severity = GateSeverity.BLOCKER

            def evaluate(self, context=None):
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.FAIL,
                    severity=self.severity,
                    message="Hard Failure",
                )

        master = GateMasterAudit(gates=[FailingGate()])
        with pytest.raises(GateBlockerError) as exc_info:
            master.audit(strict=True)
        assert "Hard Failure" in str(exc_info.value)

    def test_master_audit_report_generation(self, tmp_path):
        master = GateMasterAudit()
        results = master.audit(context={})
        report_file = tmp_path / "gate_report.json"
        rep = master.generate_json_report(results, output_path=str(report_file))
        assert rep["summary"]["total"] >= 18
        assert os.path.exists(report_file)
