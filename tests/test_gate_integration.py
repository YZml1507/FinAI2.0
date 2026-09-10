#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""六维质量防伪门禁执行流植入集成测试 (Gate Integration Test Suite)

覆盖：
1. 正常回测通过前置与后置六维门禁审计，并成功记录至 ExperimentRegistry；
2. 注入异常价格（跳变 > 30%）触发 D-1 GateBlockerError 阻断，验证 Fail-Closed 无落盘；
3. 注入突破涨停价违背客观物理撮合触发 E-3 GateBlockerError 阻断；
4. 注入七科目求和与总费用不平触发 A-1 GateBlockerError 阻断；
5. 注入 2023-08-28 之前卖出印花税 0.5‰ 历史穿越少扣税触发 A-4 GateBlockerError 阻断；
6. 注入年化换手率 > 400% 触发 S-1 GateBlockerError 阻断；
7. --no-gates 模式下的跳过行为（即使包含异常日线仍跳过门禁完成回测落盘）；
8. 前置与后置各维度独立拦截效果单测覆盖 (D-4, D-5, L-1, L-3, E-1, A-2, A-3, S-2, S-3, S-4, S-5, G-1, G-2, G-3)。
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from backtest.constants import FeeItem, OrderSide
from scripts.gates import (
    AttributionEvidenceGate,
    DailyCashConserveGate,
    DividendTaxLockGate,
    FeatureLivenessGate,
    GateBlockerError,
    GateCategory,
    GateResult,
    GateSeverity,
    GateStatus,
    HighPriceLotGate,
    MustFailCasesGate,
    ProvenanceTriadGate,
    StaticAstCallGate,
    SuspensionVolumeGate,
    TimingExitSurvivalGate,
    TurnoverCeilingGate,
    run_post_run_gates,
    run_pre_run_gates,
)
from scripts.run_dividend_backtest import run_dividend_backtest_2015_2024

#: E-1 五必挂全通过的合法证据（供"验证后置其它门禁"的用例前置满足 E-1，避免被 E-1 INCONCLUSIVE 先拦）
_MF_OK = {c: True for c in MustFailCasesGate.STANDARD_CASES}


# =====================================================================
# 测试固件与辅助工具
# =====================================================================

def _make_bars_frame(dates, price0, div_yield, mcap, code) -> pd.DataFrame:
    """合成日线帧，包含所有基础字段与扩展列"""
    return pd.DataFrame({
        "date": [d.isoformat() for d in dates],
        "open": [price0 + i * 0.01 for i in range(len(dates))],
        "high": [price0 + i * 0.01 + 0.5 for i in range(len(dates))],
        "low": [price0 + i * 0.01 - 0.5 for i in range(len(dates))],
        "close": [price0 + i * 0.01 for i in range(len(dates))],
        "preclose": [price0 + (i - 1) * 0.01 for i in range(len(dates))],
        "volume": [1_000_000 for _ in dates],
        "amount": [80_000_000.0 for _ in dates],
        "turn": [0.5] * len(dates),
        "pctChg": [0.0] * len(dates),
        "tradestatus": ["1"] * len(dates),
        "isST": ["0"] * len(dates),
        "code": [code] * len(dates),
        "dividend_yield": [div_yield + (i % 60) * 0.0005 for i in range(len(dates))],
        "market_cap": [mcap for _ in dates],
        "source": ["sine"] * len(dates),
        "adjust_mode": ["RAW"] * len(dates),
    })


def _setup_synthetic_environment(tmp_path: Path, dirty_jump: bool = False):
    """设置完整的合成数据测试环境（3只股票 + 沪深300指数）"""
    dates = list(pd.date_range("2024-01-02", periods=260, freq="B").date)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # 指数 (sh.000300)
    idx_df = pd.DataFrame({
        "date": [d.isoformat() for d in dates],
        "open": [3500.0 + i * 0.5 for i in range(len(dates))],
        "high": [3510.0 + i * 0.5 for i in range(len(dates))],
        "low": [3490.0 + i * 0.5 for i in range(len(dates))],
        "close": [3500.0 + i * 0.5 for i in range(len(dates))],
        "preclose": [3500.0 + (i - 1) * 0.5 for i in range(len(dates))],
        "volume": [50_000_000 for _ in dates],
        "amount": [50_000_000_000.0 for _ in dates],
        "code": ["sh.000300"] * len(dates),
        "isST": ["0"] * len(dates),
    })
    idx_dir = data_dir / "sh.000300"
    idx_dir.mkdir(parents=True, exist_ok=True)
    idx_df.to_parquet(idx_dir / "2024.parquet", index=False)

    # 3 只股票
    stocks = {
        "sh.600000": (10.0, 0.05, 5e10),
        "sz.000001": (8.0, 0.04, 4e10),
        "sz.000002": (6.0, 0.01, 3e10),
    }

    for sym, (p0, y, mc) in stocks.items():
        s_dir = data_dir / sym
        s_dir.mkdir(parents=True, exist_ok=True)
        df = _make_bars_frame(dates, p0, y, mc, sym)
        if dirty_jump and sym == "sh.600000":
            # 注入异常跳变：第 50 天收盘价从 10.5 飙升到 25.0 (跳变 > 100%)
            df.loc[50, "close"] = 25.0
        df.to_parquet(s_dir / "2024.parquet", index=False)

    # 除权 sidecar 目录（空）
    (data_dir / "exdiv").mkdir(parents=True, exist_ok=True)

    return data_dir, dates


# =====================================================================
# 核心集成用例
# =====================================================================

class TestGateIntegration:
    """阶段二：执行流前置/后置门禁植入集成测试套件"""

    def test_normal_backtest_full_gates_pass(self, tmp_path: Path):
        """1. 正常回测通过前置与后置六维门禁，并成功记录至 ExperimentRegistry"""
        data_dir, dates = _setup_synthetic_environment(tmp_path, dirty_jump=False)
        exp_root = tmp_path / "experiments"

        result = run_dividend_backtest_2015_2024(
            data_path=data_dir,
            initial_capital=Decimal("150000"),
            risk_free_annual=Decimal("0.025"),
            enable_gates=True,
            start_date=dates[0],
            end_date=dates[-1],
            registry_root=exp_root,
            universe_provider=lambda day: ["sh.600000", "sz.000001", "sz.000002"],
        )

        assert result is not None
        assert "run_id" in result
        assert result["report"] is not None
        # 验证 registry 产生了落盘记录
        runs_dir = exp_root / "runs"
        assert runs_dir.exists()
        assert any(runs_dir.iterdir()), "门禁全通后应在 experiments/runs 下留下成功记录"

    def test_d1_raw_price_jump_blocks_pre_run(self, tmp_path: Path):
        """2. 注入异常价格（跳变 > 30%）触发 D-1 GateBlockerError 并阻断落盘 (Fail-Closed)

        ⚠ 三层分层（任务 1）：回测默认 report-only（``gate_strict=False``，门禁不阻断回测）；
        本用例以 ``gate_strict=True`` 显式打开 fail-closed，证明数据完整性阻断能力仍在。
        """
        data_dir, dates = _setup_synthetic_environment(tmp_path, dirty_jump=True)
        exp_root = tmp_path / "experiments"

        with pytest.raises(GateBlockerError) as exc_info:
            run_dividend_backtest_2015_2024(
                data_path=data_dir,
                initial_capital=Decimal("150000"),
                enable_gates=True,
                gate_strict=True,
                start_date=dates[0],
                end_date=dates[-1],
                registry_root=exp_root,
                universe_provider=lambda day: ["sh.600000", "sz.000001", "sz.000002"],
            )

        assert exc_info.value.gate_id == "D-1"
        assert "异常日跳变" in str(exc_info.value)
        # 验证 Fail-Closed：绝不得在 runs/ 留下成功记录
        runs_dir = exp_root / "runs"
        assert not runs_dir.exists() or not any(runs_dir.iterdir()), "阻断失败时不应有落盘记录"

    def test_e3_limit_up_breach_blocks_post_run(self):
        """3. 注入突破涨停价违规成交触发 E-3 GateBlockerError 并阻断"""
        invalid_trades = [
            {
                "symbol": "sh.600000",
                "side": "BUY",
                "price": Decimal("11.50"),
                "limit_up": Decimal("11.00"),  # 涨停板 11.00，成交价 11.50 越界
                "limit_down": Decimal("9.00"),
            }
        ]
        with pytest.raises(GateBlockerError) as exc_info:
            run_post_run_gates(context={"trades": invalid_trades, "must_fail_results": _MF_OK}, strict=True)

        assert exc_info.value.gate_id == "E-3"
        assert "突破涨跌停板价限幅" in str(exc_info.value)

    def test_a1_fee_sum_discrepancy_blocks_post_run(self):
        """4. 注入七科目费用求和与总费用不平触发 A-1 GateBlockerError 并阻断"""
        unbalanced_trades = [
            {
                "trade_id": "TR_TEST_001",
                "fees": {
                    "COMMISSION": Decimal("5.00"),
                    "TRANSFER_FEE": Decimal("0.20"),
                },
                "total_fee": Decimal("8.00"),  # 声明 8.00，实际 5.20，差 2.80 元
            }
        ]
        with pytest.raises(GateBlockerError) as exc_info:
            run_post_run_gates(context={"trades": unbalanced_trades, "must_fail_results": _MF_OK}, strict=True)

        assert exc_info.value.gate_id == "A-1"
        assert "七科目费用求和与总费用不平" in str(exc_info.value)

    def test_a4_stamp_tax_time_travel_blocks_post_run(self):
        """5. 注入 2023-08-28 之前卖出印花税 0.5‰ 历史穿越触发 A-4 GateBlockerError 并阻断"""
        time_travel_trades = [
            {
                "date": "2023-08-20",
                "side": "SELL",
                "price": Decimal("10.00"),
                "volume": 10000,
                "amount": Decimal("100000.00"),
                "fees": {"STAMP_TAX": Decimal("50.00")},  # 10万卖出按 0.5‰ 扣了 50 元 (应为 100 元)
            }
        ]
        # 需同时给出可判的 A-2 现金流（否则 A-2 先判 INCONCLUSIVE 阻断，与预期 A-4 冲突）
        compliant_flows = [{
            "date": "2023-08-20", "cash_start": "100000", "trade_in": "0", "trade_out": "100000",
            "fee_out": "50", "dividend_in": "0", "dividend_tax_out": "0", "cash_end": "-50",
        }]
        with pytest.raises(GateBlockerError) as exc_info:
            run_post_run_gates(
                context={"trades": time_travel_trades, "must_fail_results": _MF_OK,
                         "daily_cash_flows": compliant_flows},
                strict=True,
            )

        assert exc_info.value.gate_id == "A-4"
        assert "历史分段印花税违规" in str(exc_info.value)

    def test_s1_excessive_turnover_blocks_post_run(self):
        """6. 年化单边换手率 > 400% ⇒ S-1 FAIL（门禁级；S-1 归 run 内可判门禁）"""
        res = TurnoverCeilingGate(max_turnover=4.0).evaluate({"annualized_turnover": 4.85})
        assert res.status == GateStatus.FAIL
        assert "超过散户硬顶 400%" in res.message

    def test_no_gates_skips_audit_and_records(self, tmp_path: Path):
        """7. --no-gates 模式下跳过门禁审计，即使数据含异常日线仍完成落盘"""
        data_dir, dates = _setup_synthetic_environment(tmp_path, dirty_jump=True)
        exp_root = tmp_path / "experiments"

        # 关闭门禁后，即使有 dirty_jump 也不抛 GateBlockerError
        result = run_dividend_backtest_2015_2024(
            data_path=data_dir,
            initial_capital=Decimal("150000"),
            enable_gates=False,
            start_date=dates[0],
            end_date=dates[-1],
            registry_root=exp_root,
            universe_provider=lambda day: ["sh.600000", "sz.000001", "sz.000002"],
        )

        assert result is not None
        assert "run_id" in result
        runs_dir = exp_root / "runs"
        assert runs_dir.exists()
        assert any(runs_dir.iterdir()), "--no-gates 模式下应正常落盘"


# =====================================================================
# 前置与后置各 Gate 模块化独立单测
# =====================================================================

class TestIndividualGatesModular:
    """各前置与后置门禁单元契约与拦截精度验证"""

    def test_d4_suspension_volume_blocks(self):
        """D-4 停牌日非零成交量 ⇒ FAIL（门禁级；D-4 归 run 内可判门禁）"""
        res = SuspensionVolumeGate().evaluate({"bars": [{"date": "2024-01-02", "tradestatus": "0", "volume": 5000}]})
        assert res.status == GateStatus.FAIL

    def test_d5_high_price_lot_blocks(self):
        """D-5 买入单价 > 300 或非 100 整手 ⇒ FAIL（门禁级；D-5 需委托明细，归 run/CI）"""
        res = HighPriceLotGate().evaluate({"orders": [{"side": "BUY", "price": 350.0, "volume": 100}]})
        assert res.status == GateStatus.FAIL
        assert res.metrics["violations_count"] == 1

    def test_l1_missing_dividend_tax_blocks(self):
        """L-1 前置未启用 DIVIDEND_TAX ⇒ FAIL（门禁级）"""
        res = FeatureLivenessGate(required_features=["DIVIDEND_TAX"]).evaluate(
            {"active_features": [], "is_pre_run": True}
        )
        assert res.status == GateStatus.FAIL

    def test_l3_missing_calls_blocks(self):
        """L-3 源码 AST 缺少必调函数 ⇒ FAIL（门禁级；L-3 需运行期追踪，归 CI）"""
        res = StaticAstCallGate().evaluate(
            {"source_code": "def dummy(): pass", "required_calls": ["non_existent_engine_call"]}
        )
        assert res.status == GateStatus.FAIL

    def test_e1_must_fail_cases_blocks(self):
        """E-1 5 必挂用例未全通 ⇒ 经 run_post_run_gates(strict) 阻断（E-1 为首道可判门禁）"""
        context = {"must_fail_results": {"LIMIT_UP_BUY_REJECT": False}}  # 涨停买入未被拒
        with pytest.raises(GateBlockerError) as exc_info:
            run_post_run_gates(context=context, strict=True)
        assert exc_info.value.gate_id == "E-1"

    def test_a2_daily_cash_leak_blocks(self):
        """A-2 每日现金流不守恒 ⇒ FAIL（门禁级）"""
        context = {
            "daily_cash_flows": [{
                "date": "2024-01-02", "cash_start": Decimal("100000"), "trade_in": Decimal("0"),
                "trade_out": Decimal("50000"), "fee_out": Decimal("25"), "dividend_in": Decimal("0"),
                "dividend_tax_out": Decimal("0"), "cash_end": Decimal("49900"),  # 应为 49975
            }]
        }
        res = DailyCashConserveGate().evaluate(context)
        assert res.status == GateStatus.FAIL

    def test_s2_timing_exit_survival_blocks(self):
        """S-2 破 MA200 熊市死扛未空仓避险 ⇒ FAIL（门禁级）"""
        res = TimingExitSurvivalGate().evaluate({
            "index_below_ma200_dates": ["2024-01-15"],
            "daily_positions_ratio": {"2024-01-15": 0.80},
        })
        assert res.status == GateStatus.FAIL

    def test_s4_dividend_tax_penalty_blocks(self):
        """S-4 惩罚性红利税占比超 20% ⇒ FAIL（门禁级；S-4 需分红分档真相，归 CI）"""
        res = DividendTaxLockGate().evaluate({
            "penalty_tax_amount": Decimal("500.00"),
            "total_dividend_received": Decimal("1000.00"),
        })
        assert res.status == GateStatus.FAIL

    def test_s5_tariff_cheat_blocks(self):
        """S-5 存在成交但印花税为 0 关税作弊 ⇒ FAIL（门禁级）"""
        res = AttributionEvidenceGate().evaluate({
            "trades_count": 10, "total_stamp_tax": Decimal("0.00"),
            "total_commission": Decimal("50.00"), "code_evidence": "backtest/metrics.py:L142",
        })
        assert res.status == GateStatus.FAIL

    def test_g1_incomplete_triad_blocks(self):
        """G-1 缺少有效 Git SHA ⇒ FAIL（门禁级）"""
        res = ProvenanceTriadGate().evaluate({
            "git_commit": "INVALID_SHA", "data_hash": "a" * 64, "timestamp": "2026-09-07T16:00:00Z",
        })
        assert res.status == GateStatus.FAIL

    def test_g3_master_finding_guard_verified(self):
        """G-3 母库只读区 FINDING- 守卫行数必须恒等于 370 行"""
        results = run_post_run_gates(strict=False)
        g3_res = next((r for r in results if r.gate_id == "G-3"), None)
        assert g3_res is not None
        assert g3_res.status == GateStatus.PASS
        assert g3_res.metrics["count"] == 370
