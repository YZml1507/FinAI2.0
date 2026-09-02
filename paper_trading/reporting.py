#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T403 报告生成 —— 复用 T205 metrics.py（FR-REP-1）。

职责：
  1. 复用 `backtest/metrics.py::compute_metrics` 计算绩效指标
  2. 输出格式：JSON（机器可读）+ HTML（人工查看）
  3. 频率：每日更新 NAV，每周/月生成完整报告

红线：
  ① 指标计算**完全复用** T205 `compute_metrics`（SDD-1 同构）
  ② HTML 输出应用 design_sense 审美（深色调 / Inter 字体 / 数据密度）
  ③ 不重复造轮子：指标逻辑全在 metrics.py，本模块只做格式化输出

"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from typing import Any

from backtest.metrics import PerformanceReport, compute_metrics

__all__ = [
    "save_report_json",
    "save_report_html",
    "generate_reports",
]


def _decimal_to_str(obj: Any) -> Any:
    """递归转换 Decimal → str（JSON 序列化用）。

    特殊处理：
    - Decimal → str
    - date → isoformat()
    - dict 的 tuple 键 → "YYYY-MM" 字符串（monthly_returns 的 (year, month) 键）
    """
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            # tuple 键转为字符串（monthly_returns 的 (year, month)）
            if isinstance(k, tuple) and len(k) == 2:
                key_str = f"{k[0]}-{k[1]:02d}"
            else:
                key_str = str(k) if not isinstance(k, str) else k
            result[key_str] = _decimal_to_str(v)
        return result
    if isinstance(obj, (list, tuple)):
        return [_decimal_to_str(v) for v in obj]
    if isinstance(obj, _date):
        return obj.isoformat()
    return obj


def save_report_json(
    report: PerformanceReport,
    output_path: Path,
) -> None:
    """保存绩效报告为 JSON（机器可读）。

    Args:
        report: T205 PerformanceReport。
        output_path: 输出路径（如 `paper_trading/reports/20260902_metrics.json`）。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data = asdict(report)
    data = _decimal_to_str(data)  # Decimal → str

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def save_report_html(
    report: PerformanceReport,
    output_path: Path,
) -> None:
    """保存绩效报告为 HTML（人工查看，应用 design_sense 审美）。

    Args:
        report: T205 PerformanceReport。
        output_path: 输出路径（如 `paper_trading/reports/20260902_report.html`）。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 格式化辅助函数
    def fmt_pct(val: Decimal | None) -> str:
        if val is None:
            return "N/A"
        return f"{float(val) * 100:.2f}%"

    def fmt_decimal(val: Decimal | None, precision: int = 2) -> str:
        if val is None:
            return "N/A"
        return f"{float(val):.{precision}f}"

    def fmt_date(d: _date | None) -> str:
        return d.isoformat() if d else "—"

    # HTML 内容（design_sense：深色调 slate #0F172A / Inter 字体 / 数据密度）
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>绩效报告 {report.start} ~ {report.end}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0B0E14;
    color: #F8FAFC;
    padding: 2rem;
    line-height: 1.6;
}}
.container {{ max-width: 1200px; margin: 0 auto; }}
h1 {{
    font-size: 1.75rem;
    font-weight: 600;
    margin-bottom: 0.5rem;
    color: #F8FAFC;
}}
.meta {{
    color: #94A3B8;
    font-size: 0.875rem;
    margin-bottom: 2rem;
    font-variant-numeric: tabular-nums;
}}
.section {{
    background: #1E293B;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 1.5rem;
    margin-bottom: 1.5rem;
}}
.section-title {{
    font-size: 1.125rem;
    font-weight: 600;
    margin-bottom: 1rem;
    color: #38BDF8;
}}
.grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1rem;
}}
.metric {{
    padding: 0.75rem;
    background: #0F172A;
    border-radius: 4px;
    border: 1px solid #334155;
}}
.metric-label {{
    font-size: 0.75rem;
    color: #94A3B8;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 0.25rem;
}}
.metric-value {{
    font-size: 1.5rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: #F8FAFC;
}}
.metric-value.positive {{ color: #4FD1C5; }}
.metric-value.negative {{ color: #F87171; }}
.table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.875rem;
    font-variant-numeric: tabular-nums;
}}
.table th {{
    text-align: left;
    padding: 0.5rem;
    color: #94A3B8;
    font-weight: 600;
    border-bottom: 1px solid #334155;
}}
.table td {{
    padding: 0.5rem;
    border-bottom: 1px solid #334155;
}}
.table tr:last-child td {{ border-bottom: none; }}
</style>
</head>
<body>
<div class="container">
<h1>模拟盘绩效报告</h1>
<div class="meta">
    {report.start} ~ {report.end} · {report.trading_days} 交易日 · {report.calendar_days} 日历日
</div>

<div class="section">
<div class="section-title">收益指标</div>
<div class="grid">
    <div class="metric">
        <div class="metric-label">总收益</div>
        <div class="metric-value {'positive' if report.total_return >= 0 else 'negative'}">
            {fmt_pct(report.total_return)}
        </div>
    </div>
    <div class="metric">
        <div class="metric-label">年化收益 (CAGR)</div>
        <div class="metric-value {'positive' if report.cagr >= 0 else 'negative'}">
            {fmt_pct(report.cagr)}
        </div>
    </div>
    <div class="metric">
        <div class="metric-label">初始净值</div>
        <div class="metric-value">{fmt_decimal(report.initial_nav, 2)}</div>
    </div>
    <div class="metric">
        <div class="metric-label">最终净值</div>
        <div class="metric-value">{fmt_decimal(report.final_nav, 2)}</div>
    </div>
</div>
</div>

<div class="section">
<div class="section-title">风险指标</div>
<div class="grid">
    <div class="metric">
        <div class="metric-label">年化波动</div>
        <div class="metric-value">{fmt_pct(report.annual_volatility)}</div>
    </div>
    <div class="metric">
        <div class="metric-label">最大回撤</div>
        <div class="metric-value negative">{fmt_pct(report.max_drawdown)}</div>
    </div>
    <div class="metric">
        <div class="metric-label">夏普比率</div>
        <div class="metric-value">{fmt_decimal(report.sharpe_ratio, 2)}</div>
    </div>
    <div class="metric">
        <div class="metric-label">卡玛比率</div>
        <div class="metric-value">{fmt_decimal(report.calmar_ratio, 2)}</div>
    </div>
</div>
<table class="table" style="margin-top: 1rem;">
<tr>
    <th>最大回撤峰值日</th>
    <td>{fmt_date(report.max_dd_peak)}</td>
    <th>最大回撤谷底日</th>
    <td>{fmt_date(report.max_dd_trough)}</td>
</tr>
<tr>
    <th>回撤恢复日</th>
    <td colspan="3">{fmt_date(report.max_dd_recovery)}</td>
</tr>
<tr>
    <th>停牌陷阱天数</th>
    <td colspan="3">{report.suspension_trapped_days} 天</td>
</tr>
</table>
</div>

<div class="section">
<div class="section-title">交易活动</div>
<div class="grid">
    <div class="metric">
        <div class="metric-label">年化换手率</div>
        <div class="metric-value">{fmt_pct(report.annual_turnover)}</div>
    </div>
    <div class="metric">
        <div class="metric-label">胜率 (FIFO)</div>
        <div class="metric-value">{fmt_pct(report.win_rate)}</div>
    </div>
    <div class="metric">
        <div class="metric-label">完整往返次数</div>
        <div class="metric-value">{report.round_trips}</div>
    </div>
    <div class="metric">
        <div class="metric-label">总费用</div>
        <div class="metric-value negative">{fmt_decimal(report.fees_sum, 2)}</div>
    </div>
</div>
</div>

<div class="section">
<div class="section-title">费用明细</div>
<table class="table">
<thead>
<tr>
    <th>科目</th>
    <th style="text-align: right;">金额</th>
</tr>
</thead>
<tbody>
{"".join(f'<tr><td>{item.name}</td><td style="text-align: right;">{fmt_decimal(amount, 2)}</td></tr>'
         for item, amount in report.fees_total.items())}
</tbody>
</table>
</div>

<div class="section">
<div class="section-title">配置参数</div>
<table class="table">
<tr>
    <th>无风险年化利率</th>
    <td>{fmt_pct(report.risk_free_annual)}</td>
</tr>
</table>
</div>

</div>
</body>
</html>"""

    with output_path.open("w", encoding="utf-8") as f:
        f.write(html)


def generate_reports(
    result,
    output_dir: Path,
    date_suffix: str,
    *,
    risk_free_annual: Decimal,
) -> tuple[Path, Path]:
    """生成 JSON + HTML 双份报告。

    Args:
        result: BacktestResult（含 nav_curve / trades / final_nav）。
        output_dir: 输出目录（如 `paper_trading/reports/`）。
        date_suffix: 日期后缀（如 `20260902`）。
        risk_free_annual: 无风险年化利率（必须显式传入）。

    Returns:
        (json_path, html_path) 路径元组。
    """
    report = compute_metrics(result, risk_free_annual=risk_free_annual)

    json_path = output_dir / f"{date_suffix}_metrics.json"
    html_path = output_dir / f"{date_suffix}_report.html"

    save_report_json(report, json_path)
    save_report_html(report, html_path)

    return json_path, html_path
