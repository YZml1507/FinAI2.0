#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T402 偏差监控 —— 单元测试（≥8 测试，覆盖全部偏差计算 + 容忍带判定）。

测试策略：
  · 纯函数黑盒测试（compute_deviation_metrics + DeviationMonitor.check）
  · 边界值测试（临界值 / 分母为 0 / 空数据）
  · fail-closed 验证（非法输入必须 raise，⛔ 不许静默返回 0）
"""
from datetime import date as _date
from decimal import Decimal

import pytest

from paper_trading.deviation import (
    DeviationError,
    DeviationMetrics,
    compute_deviation_metrics,
)
from paper_trading.monitor import DeviationMonitor, DeviationReport
from paper_trading.tolerance import DEFAULT_TOLERANCE_BANDS, ToleranceBand

_D = Decimal


# ============================================================================
# § 偏差计算正确性
# ============================================================================


class TestDeviationCalculation:
    """偏差指标计算公式验证（对照文档口径）。"""

    def test_nav_deviation_formula(self):
        """NAV 偏差 = |nav_paper − nav_backtest| / nav_backtest。"""
        metrics = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100500.00"),
            return_backtest=_D("0.05"),
            return_paper=_D("0.055"),
        )
        # |100500 − 100000| / 100000 = 500 / 100000 = 0.005
        assert metrics.nav_deviation == _D("0.005000")

    def test_return_deviation_formula(self):
        """收益偏差 = |return_paper − return_backtest|（差值，非比例）。"""
        metrics = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100000.00"),
            return_backtest=_D("0.15"),       # 15%
            return_paper=_D("0.12"),          # 12%
        )
        # |0.12 − 0.15| = 0.03 = 3pp
        assert metrics.return_deviation == _D("0.030000")

    def test_turnover_deviation_formula(self):
        """换手偏差 = |turnover_paper − turnover_backtest|（单位百分点 pp）。"""
        metrics = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100000.00"),
            return_backtest=_D("0.05"),
            return_paper=_D("0.05"),
            turnover_backtest=_D("3.20"),     # 320%
            turnover_paper=_D("3.35"),        # 335%
        )
        # |3.35 − 3.20| = 0.15 = 15pp
        assert metrics.turnover_deviation == _D("0.150000")

    def test_price_deviation_per_trade(self):
        """成交价偏差（逐笔百分比）。"""
        metrics = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100000.00"),
            return_backtest=_D("0.05"),
            return_paper=_D("0.05"),
            price_pairs=[
                (_D("10.00"), _D("10.05")),   # +0.5%
                (_D("20.00"), _D("19.80")),   # −1%
            ],
        )
        assert len(metrics.price_deviations) == 2
        # |10.05 − 10.00| / 10.00 = 0.005
        assert metrics.price_deviations[0] == _D("0.005000")
        # |19.80 − 20.00| / 20.00 = 0.01
        assert metrics.price_deviations[1] == _D("0.010000")

    def test_slippage_deviation_formula(self):
        """滑点偏差 = realized − model（可正可负）。"""
        metrics = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100000.00"),
            return_backtest=_D("0.05"),
            return_paper=_D("0.05"),
            slippage_pairs=[
                (_D("0.0005"), _D("0.0010")),   # 模型 5bps，实际 10bps → 差 +5bps
                (_D("0.0005"), _D("0.0002")),   # 模型 5bps，实际 2bps → 差 −3bps
            ],
        )
        assert len(metrics.slippage_deviations) == 2
        # 0.0010 − 0.0005 = +0.0005
        assert metrics.slippage_deviations[0] == _D("0.000500")
        # 0.0002 − 0.0005 = −0.0003
        assert metrics.slippage_deviations[1] == _D("-0.000300")


# ============================================================================
# § 边界与 fail-closed
# ============================================================================


class TestDeviationFailClosed:
    """偏差计算的契约违约检测（⛔ 非法输入必须 raise）。"""

    def test_nav_backtest_zero_raises(self):
        """回测 NAV ≤ 0 ⇒ raise。"""
        with pytest.raises(DeviationError, match="nav_backtest.*≤ 0"):
            compute_deviation_metrics(
                date=_date(2026, 9, 1),
                nav_backtest=_D("0"),
                nav_paper=_D("100000.00"),
                return_backtest=_D("0.05"),
                return_paper=_D("0.05"),
            )

    def test_nav_paper_zero_raises(self):
        """模拟盘 NAV ≤ 0 ⇒ raise。"""
        with pytest.raises(DeviationError, match="nav_paper.*≤ 0"):
            compute_deviation_metrics(
                date=_date(2026, 9, 1),
                nav_backtest=_D("100000.00"),
                nav_paper=_D("-1000.00"),
                return_backtest=_D("0.05"),
                return_paper=_D("0.05"),
            )

    def test_price_zero_raises(self):
        """成交价 ≤ 0 ⇒ raise。"""
        with pytest.raises(DeviationError, match="回测成交价.*≤ 0"):
            compute_deviation_metrics(
                date=_date(2026, 9, 1),
                nav_backtest=_D("100000.00"),
                nav_paper=_D("100000.00"),
                return_backtest=_D("0.05"),
                return_paper=_D("0.05"),
                price_pairs=[(_D("0"), _D("10.00"))],
            )

    def test_turnover_none_returns_none_deviation(self):
        """换手有一方为 None ⇒ turnover_deviation 也为 None（fail-soft）。"""
        metrics = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100000.00"),
            return_backtest=_D("0.05"),
            return_paper=_D("0.05"),
            turnover_backtest=None,
            turnover_paper=_D("3.2"),
        )
        assert metrics.turnover_deviation is None


# ============================================================================
# § 容忍带判定逻辑
# ============================================================================


class TestToleranceBandJudgment:
    """容忍带超出判定（临界值测试 + 逻辑正确性）。"""

    def test_nav_within_tolerance_not_exceeded(self):
        """NAV 偏差在容忍带内 → not exceeded。"""
        metrics = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100400.00"),      # 偏差 0.4% < 0.5% 容忍带
            return_backtest=_D("0.05"),
            return_paper=_D("0.05"),
        )
        monitor = DeviationMonitor()
        report = monitor.check(metrics)
        assert not report.nav_exceeded
        assert not report.any_exceeded

    def test_nav_exceed_tolerance_triggered(self):
        """NAV 偏差超出容忍带 → exceeded=True + hint。"""
        metrics = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100600.00"),      # 偏差 0.6% > 0.5% 容忍带
            return_backtest=_D("0.05"),
            return_paper=_D("0.05"),
        )
        monitor = DeviationMonitor()
        report = monitor.check(metrics)
        assert report.nav_exceeded
        assert report.any_exceeded
        assert any("NAV 偏差" in hint for hint in report.root_cause_hints)

    def test_return_deviation_threshold(self):
        """收益偏差临界值测试（2% 容忍带）。"""
        # 刚好在临界 → not exceeded
        metrics_on_edge = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100000.00"),
            return_backtest=_D("0.10"),
            return_paper=_D("0.12"),         # 偏差 2% = 临界
        )
        monitor = DeviationMonitor()
        report = monitor.check(metrics_on_edge)
        assert not report.return_exceeded

        # 超出临界 → exceeded
        metrics_over = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100000.00"),
            return_backtest=_D("0.10"),
            return_paper=_D("0.125"),        # 偏差 2.5% > 2%
        )
        report_over = monitor.check(metrics_over)
        assert report_over.return_exceeded
        assert any("收益偏差" in hint for hint in report_over.root_cause_hints)

    def test_price_any_exceeded_triggers(self):
        """成交价：任一笔超出 ⇒ price_exceeded=True。"""
        metrics = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100000.00"),
            return_backtest=_D("0.05"),
            return_paper=_D("0.05"),
            price_pairs=[
                (_D("10.00"), _D("10.05")),   # 0.5% < 1% → ok
                (_D("20.00"), _D("20.25")),   # 1.25% > 1% → 超出
            ],
        )
        monitor = DeviationMonitor()
        report = monitor.check(metrics)
        assert report.price_exceeded
        assert any("成交价偏差" in hint for hint in report.root_cause_hints)

    def test_slippage_absolute_value_check(self):
        """滑点偏差：取绝对值判定（可正可负）。"""
        metrics = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100000.00"),
            return_backtest=_D("0.05"),
            return_paper=_D("0.05"),
            slippage_pairs=[
                (_D("0.0005"), _D("0.0015")),   # +0.001 = 0.1% < 0.5% → ok
                (_D("0.0005"), _D("-0.0050")),  # −0.0055 → |−0.0055|=0.55% > 0.5% → 超出
            ],
        )
        monitor = DeviationMonitor()
        report = monitor.check(metrics)
        assert report.slippage_exceeded


# ============================================================================
# § 联合诊断
# ============================================================================


class TestRootCauseHints:
    """根因提示逻辑（多指标联合诊断）。"""

    def test_nav_and_turnover_joint_hint(self):
        """NAV 与换手同时超出 → 系统性问题提示。"""
        metrics = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100800.00"),        # NAV 偏差 0.8% > 0.5%
            return_backtest=_D("0.05"),
            return_paper=_D("0.05"),
            turnover_backtest=_D("3.0"),
            turnover_paper=_D("3.2"),         # 换手偏差 0.2 = 20pp > 10pp
        )
        monitor = DeviationMonitor()
        report = monitor.check(metrics)
        assert report.nav_exceeded and report.turnover_exceeded
        # 联合诊断提示应出现
        joint_hint = any(
            "NAV 与换手同时超出" in hint for hint in report.root_cause_hints
        )
        assert joint_hint

    def test_price_and_slippage_joint_hint(self):
        """成交价与滑点同时超出 → 流动性问题提示。"""
        metrics = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100000.00"),
            return_backtest=_D("0.05"),
            return_paper=_D("0.05"),
            price_pairs=[(_D("10.00"), _D("10.15"))],       # 1.5% > 1%
            slippage_pairs=[(_D("0.0005"), _D("0.0070"))],  # 0.65% > 0.5%
        )
        monitor = DeviationMonitor()
        report = monitor.check(metrics)
        assert report.price_exceeded and report.slippage_exceeded
        joint_hint = any(
            "成交价与滑点同时超出" in hint for hint in report.root_cause_hints
        )
        assert joint_hint


# ============================================================================
# § 容忍带配置注入
# ============================================================================


class TestCustomToleranceBands:
    """自定义容忍带（覆盖默认配置）。"""

    def test_custom_tolerance_overrides_default(self):
        """注入自定义容忍带 → 按自定义判定。"""
        custom_bands = {
            "nav_daily": ToleranceBand(
                name="nav_daily",
                threshold=_D("0.001"),  # 0.1%（更严格）
                unit="%",
                description="custom strict band",
            ),
            "return_monthly": DEFAULT_TOLERANCE_BANDS["return_monthly"],
            "turnover_monthly": DEFAULT_TOLERANCE_BANDS["turnover_monthly"],
            "price_per_trade": DEFAULT_TOLERANCE_BANDS["price_per_trade"],
            "slippage_per_trade": DEFAULT_TOLERANCE_BANDS["slippage_per_trade"],
        }
        metrics = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100150.00"),      # 0.15% > 0.1% 自定义带
            return_backtest=_D("0.05"),
            return_paper=_D("0.05"),
        )
        monitor = DeviationMonitor(tolerance_bands=custom_bands)
        report = monitor.check(metrics)
        assert report.nav_exceeded  # 用自定义更严格的 0.1% 判定


# ============================================================================
# § 输出格式验证
# ============================================================================


class TestDeviationReportFormat:
    """DeviationReport 输出格式与字段完整性。"""

    def test_report_contains_all_fields(self):
        """报告包含全部必需字段（5 个 exceeded 标记 + hints）。"""
        metrics = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100000.00"),
            return_backtest=_D("0.05"),
            return_paper=_D("0.05"),
        )
        monitor = DeviationMonitor()
        report = monitor.check(metrics)

        # 必有字段
        assert isinstance(report.date, _date)
        assert isinstance(report.metrics, DeviationMetrics)
        assert isinstance(report.nav_exceeded, bool)
        assert isinstance(report.return_exceeded, bool)
        assert isinstance(report.turnover_exceeded, bool)
        assert isinstance(report.price_exceeded, bool)
        assert isinstance(report.slippage_exceeded, bool)
        assert isinstance(report.root_cause_hints, list)

    def test_any_exceeded_property(self):
        """any_exceeded 正确反映至少一项超出。"""
        # 全部在容忍带内
        metrics_ok = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100100.00"),       # 0.1% < 0.5%
            return_backtest=_D("0.05"),
            return_paper=_D("0.051"),        # 0.1pp < 2%
        )
        monitor = DeviationMonitor()
        report_ok = monitor.check(metrics_ok)
        assert not report_ok.any_exceeded

        # 任一超出
        metrics_bad = compute_deviation_metrics(
            date=_date(2026, 9, 1),
            nav_backtest=_D("100000.00"),
            nav_paper=_D("100800.00"),       # 0.8% > 0.5%
            return_backtest=_D("0.05"),
            return_paper=_D("0.051"),
        )
        report_bad = monitor.check(metrics_bad)
        assert report_bad.any_exceeded
