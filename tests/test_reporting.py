#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T403 报告生成测试（paper_trading/reporting.py）。

覆盖范围：
  - save_report_json（JSON 序列化 + Decimal 转 str）
  - save_report_html（HTML 生成 + design_sense 审美）
  - generate_reports（JSON + HTML 双份输出）
"""
import json
import shutil
import tempfile
from datetime import date as dt
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.constants import FeeItem
from backtest.metrics import PerformanceReport
from paper_trading.reporting import (
    generate_reports,
    save_report_html,
    save_report_json,
)

_ZERO = Decimal("0")


@pytest.fixture
def temp_report_dir():
    """临时报告目录（自动清理）。"""
    tmpdir = Path(tempfile.mkdtemp())
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def sample_report():
    """样本绩效报告（T205 PerformanceReport）。"""
    return PerformanceReport(
        start=dt(2026, 9, 1),
        end=dt(2026, 9, 5),
        calendar_days=4,
        trading_days=3,
        initial_nav=Decimal("100000"),
        final_nav=Decimal("102000"),
        total_return=Decimal("0.02"),
        cagr=Decimal("0.05"),
        annual_volatility=Decimal("0.15"),
        max_drawdown=Decimal("0.03"),
        max_dd_peak=dt(2026, 9, 2),
        max_dd_trough=dt(2026, 9, 3),
        max_dd_recovery=dt(2026, 9, 5),
        suspension_trapped_days=0,
        sharpe_ratio=Decimal("0.8"),
        calmar_ratio=Decimal("1.67"),
        risk_free_annual=Decimal("0.02"),
        annual_turnover=Decimal("0.5"),
        win_rate=Decimal("0.6"),
        round_trips=10,
        fees_total={
            FeeItem.COMMISSION: Decimal("100.00"),
            FeeItem.STAMP_TAX: Decimal("50.00"),
            FeeItem.TRANSFER_FEE: Decimal("2.00"),
            FeeItem.EXCHANGE_FEE: Decimal("3.41"),
            FeeItem.REGULATION_FEE: Decimal("0.20"),
            FeeItem.SLIPPAGE: Decimal("20.00"),
        },
        fees_sum=Decimal("175.61"),
        monthly_returns={(2026, 9): Decimal("0.02")},
    )


class TestSaveReportJSON:
    """JSON 报告保存测试。"""

    def test_save_json_creates_file(self, temp_report_dir, sample_report):
        """保存 JSON 报告：文件创建成功。"""
        json_path = temp_report_dir / "test_report.json"
        save_report_json(sample_report, json_path)

        assert json_path.exists()

    def test_json_content_valid(self, temp_report_dir, sample_report):
        """JSON 内容：可解析 + 字段齐全。"""
        json_path = temp_report_dir / "test_report.json"
        save_report_json(sample_report, json_path)

        with json_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        assert data["start"] == "2026-09-01"
        assert data["end"] == "2026-09-05"
        assert data["initial_nav"] == "100000"
        assert data["final_nav"] == "102000"
        assert data["total_return"] == "0.02"
        assert data["cagr"] == "0.05"

    def test_decimal_to_str_conversion(self, temp_report_dir, sample_report):
        """Decimal → str 转换正确（精度保留）。"""
        json_path = temp_report_dir / "test_report.json"
        save_report_json(sample_report, json_path)

        with json_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        # Decimal 字段应为字符串
        assert isinstance(data["total_return"], str)
        assert data["total_return"] == "0.02"
        assert data["fees_sum"] == "175.61"


class TestSaveReportHTML:
    """HTML 报告保存测试。"""

    def test_save_html_creates_file(self, temp_report_dir, sample_report):
        """保存 HTML 报告：文件创建成功。"""
        html_path = temp_report_dir / "test_report.html"
        save_report_html(sample_report, html_path)

        assert html_path.exists()

    def test_html_content_structure(self, temp_report_dir, sample_report):
        """HTML 内容：基本结构完整。"""
        html_path = temp_report_dir / "test_report.html"
        save_report_html(sample_report, html_path)

        html = html_path.read_text(encoding="utf-8")

        assert "<!DOCTYPE html>" in html
        assert "<title>绩效报告" in html
        assert "2026-09-01 ~ 2026-09-05" in html

    def test_html_contains_metrics(self, temp_report_dir, sample_report):
        """HTML 包含核心指标。"""
        html_path = temp_report_dir / "test_report.html"
        save_report_html(sample_report, html_path)

        html = html_path.read_text(encoding="utf-8")

        assert "2.00%" in html  # total_return
        assert "5.00%" in html  # cagr
        assert "3.00%" in html  # max_drawdown
        assert "0.80" in html   # sharpe_ratio

    def test_html_design_sense_styles(self, temp_report_dir, sample_report):
        """HTML 包含 design_sense 审美元素。"""
        html_path = temp_report_dir / "test_report.html"
        save_report_html(sample_report, html_path)

        html = html_path.read_text(encoding="utf-8")

        # 深色调 + Inter 字体
        assert "'Inter'" in html or "Inter" in html
        assert "#0B0E14" in html or "#0F172A" in html  # 深色背景
        assert "font-variant-numeric: tabular-nums" in html  # 等宽数字

    def test_html_fee_breakdown_table(self, temp_report_dir, sample_report):
        """HTML 包含费用明细表。"""
        html_path = temp_report_dir / "test_report.html"
        save_report_html(sample_report, html_path)

        html = html_path.read_text(encoding="utf-8")

        assert "COMMISSION" in html
        assert "STAMP_TAX" in html
        assert "100.00" in html  # 佣金金额
        assert "50.00" in html   # 印花税金额


class TestGenerateReports:
    """generate_reports 双份输出测试。"""

    def test_generate_both_files(self, temp_report_dir):
        """生成 JSON + HTML 双份报告。"""
        # 构造伪 BacktestResult
        class _Result:
            def __init__(self):
                self.nav_curve = {
                    "2026-09-01": Decimal("100000"),
                    "2026-09-02": Decimal("101000"),
                    "2026-09-03": Decimal("102000"),
                }
                self.trades = []
                self.final_nav = Decimal("102000")
                self.journal_entries = []
                self.orders = []
                self.bars_by_date = {}

        result = _Result()

        json_path, html_path = generate_reports(
            result,
            temp_report_dir,
            "20260903",
            risk_free_annual=Decimal("0.02"),
        )

        assert json_path.exists()
        assert html_path.exists()
        assert json_path.name == "20260903_metrics.json"
        assert html_path.name == "20260903_report.html"

    def test_generate_reports_consistent_data(self, temp_report_dir):
        """JSON 与 HTML 报告数据一致性。"""
        class _Result:
            def __init__(self):
                self.nav_curve = {
                    "2026-09-01": Decimal("100000"),
                    "2026-09-05": Decimal("102000"),
                }
                self.trades = []
                self.final_nav = Decimal("102000")
                self.journal_entries = []
                self.orders = []
                self.bars_by_date = {}

        result = _Result()

        json_path, html_path = generate_reports(
            result,
            temp_report_dir,
            "20260905",
            risk_free_annual=Decimal("0.02"),
        )

        # JSON 数据
        with json_path.open("r", encoding="utf-8") as f:
            json_data = json.load(f)

        # HTML 包含对应数据
        html = html_path.read_text(encoding="utf-8")

        assert json_data["initial_nav"] == "100000"
        assert json_data["final_nav"] == "102000"
        assert "100000" in html
        assert "102000" in html
