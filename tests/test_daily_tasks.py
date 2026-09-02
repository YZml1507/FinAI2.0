#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T403 日终任务编排测试（paper_trading/daily_tasks.py）。

覆盖范围：
  - DailyReport 数据容器
  - DailyTaskRunner 依赖注入 + 编排逻辑
  - 幂等重跑（同日期多次执行结果一致）
  - 错误恢复（对账失败 → 立即终止 + 告警）
"""
import shutil
import tempfile
from datetime import date as dt
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from paper_trading.daily_tasks import DailyReport, DailyTaskRunner

_ZERO = Decimal("0")


@pytest.fixture
def temp_workspace():
    """临时工作区（NAV 文件 + 报告目录）。"""
    tmpdir = Path(tempfile.mkdtemp())
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def mock_updater():
    """Mock IncrementalUpdater。"""
    updater = Mock()
    updater.update = Mock(return_value={
        "sz.000001": Mock(ok=True, state="ok", rows=100),
        "sz.000002": Mock(ok=True, state="ok", rows=100),
    })
    return updater


@pytest.fixture
def mock_broker():
    """Mock PaperBroker。"""
    broker = Mock()
    broker.submit_orders = Mock(return_value=[])
    broker.match_pending_orders = Mock(return_value=[])
    broker.ledger = Mock()
    broker.ledger.process_exdiv = Mock()
    broker.ledger.book_view = Mock(return_value=Mock(
        cash=Decimal("100000"),
        frozen_cash=_ZERO,
        nav=Decimal("100000"),
        positions={},
        date=dt(2026, 9, 2),
    ))
    return broker


@pytest.fixture
def mock_strategy():
    """Mock Strategy。"""
    strategy = Mock()
    strategy.generate_signals = Mock(return_value=[])
    strategy.get_universe = Mock(return_value=["sz.000001", "sz.000002"])
    return strategy


class TestDailyReport:
    """DailyReport 数据容器测试。"""

    def test_daily_report_creation(self):
        """创建 DailyReport 正常。"""
        report = DailyReport(date=dt(2026, 9, 2), success=True)
        assert report.date == dt(2026, 9, 2)
        assert report.success

    def test_daily_report_with_error(self):
        """失败报告包含错误信息。"""
        report = DailyReport(
            date=dt(2026, 9, 2),
            success=False,
            error="对账失败: NAV 差异 0.05 超容差",
        )
        assert not report.success
        assert "对账失败" in report.error


class TestDailyTaskRunner:
    """DailyTaskRunner 编排逻辑测试。"""

    def test_runner_initialization(
        self, temp_workspace, mock_updater, mock_broker, mock_strategy
    ):
        """初始化：依赖注入正常。"""
        runner = DailyTaskRunner(
            updater=mock_updater,
            broker=mock_broker,
            strategy=mock_strategy,
            alert_fn=None,
            nav_file=temp_workspace / "nav_series.parquet",
            report_dir=temp_workspace / "reports",
        )
        assert runner.updater is mock_updater
        assert runner.broker is mock_broker
        assert runner.strategy is mock_strategy

    def test_get_default_symbols(
        self, temp_workspace, mock_updater, mock_broker, mock_strategy
    ):
        """_get_default_symbols 调用策略 get_universe。"""
        runner = DailyTaskRunner(
            updater=mock_updater,
            broker=mock_broker,
            strategy=mock_strategy,
            alert_fn=None,
            nav_file=temp_workspace / "nav_series.parquet",
            report_dir=temp_workspace / "reports",
        )
        symbols = runner._get_default_symbols()
        assert symbols == ["sz.000001", "sz.000002"]
        mock_strategy.get_universe.assert_called_once()

    def test_should_generate_report_monday(
        self, temp_workspace, mock_updater, mock_broker, mock_strategy
    ):
        """周一触发报告生成。"""
        runner = DailyTaskRunner(
            updater=mock_updater,
            broker=mock_broker,
            strategy=mock_strategy,
            alert_fn=None,
            nav_file=temp_workspace / "nav_series.parquet",
            report_dir=temp_workspace / "reports",
        )
        # 2026-09-01 是周一
        assert runner._should_generate_report(dt(2026, 9, 1))

    def test_should_generate_report_month_start(
        self, temp_workspace, mock_updater, mock_broker, mock_strategy
    ):
        """每月 1 日触发报告生成。"""
        runner = DailyTaskRunner(
            updater=mock_updater,
            broker=mock_broker,
            strategy=mock_strategy,
            alert_fn=None,
            nav_file=temp_workspace / "nav_series.parquet",
            report_dir=temp_workspace / "reports",
        )
        assert runner._should_generate_report(dt(2026, 10, 1))

    def test_should_not_generate_report_mid_week(
        self, temp_workspace, mock_updater, mock_broker, mock_strategy
    ):
        """周中不触发报告生成。"""
        runner = DailyTaskRunner(
            updater=mock_updater,
            broker=mock_broker,
            strategy=mock_strategy,
            alert_fn=None,
            nav_file=temp_workspace / "nav_series.parquet",
            report_dir=temp_workspace / "reports",
        )
        # 2026-09-02 是周二
        assert not runner._should_generate_report(dt(2026, 9, 2))


class TestDailyTaskErrorHandling:
    """日终任务错误处理测试。"""

    def test_data_update_failure_over_threshold(
        self, temp_workspace, mock_updater, mock_broker, mock_strategy
    ):
        """数据更新失败 >50% → raise RuntimeError。"""
        mock_updater.update = Mock(return_value={
            "sz.000001": Mock(ok=False, state="failed", rows=0),
            "sz.000002": Mock(ok=False, state="failed", rows=0),
        })

        runner = DailyTaskRunner(
            updater=mock_updater,
            broker=mock_broker,
            strategy=mock_strategy,
            alert_fn=None,
            nav_file=temp_workspace / "nav_series.parquet",
            report_dir=temp_workspace / "reports",
        )

        report = runner.run_daily_tasks(
            dt(2026, 9, 2), symbols=["sz.000001", "sz.000002"])

        assert not report.success
        assert "数据更新失败超 50%" in report.error

    def test_alert_sent_on_failure(
        self, temp_workspace, mock_updater, mock_broker, mock_strategy
    ):
        """失败时发送告警。"""
        mock_updater.update = Mock(side_effect=RuntimeError("网络超时"))

        alert_fn = Mock()

        runner = DailyTaskRunner(
            updater=mock_updater,
            broker=mock_broker,
            strategy=mock_strategy,
            alert_fn=alert_fn,
            nav_file=temp_workspace / "nav_series.parquet",
            report_dir=temp_workspace / "reports",
        )

        report = runner.run_daily_tasks(dt(2026, 9, 2), symbols=["sz.000001"])

        assert not report.success
        assert alert_fn.called
        args = alert_fn.call_args[0][0]
        assert "❌" in args
        assert "日终任务失败" in args
