#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T403 日终任务编排 —— 对账/净值/报告（FR-ACC-2/3 + FR-REP-1）。

核心职责：编排模拟盘日终自动化任务，确保账本一致性 + 净值正确 + 报告生成。

执行顺序（⛔ 不可颠倒，违反即算错净值）：
  1. 数据更新（调用 T109 增量更新，确保盘后数据齐备）
  2. 策略信号生成（调用策略 generate_signals）
  3. 订单提交（PaperBroker 入队）
  4. 撮合执行（次日开盘，复用 T201 撮合引擎）
  5. 除权处理（如有，必须先于结算）
  6. 结算（settle_day，刷市值 + NAV）
  7. 对账（reconcile_account，验证账本一致性）
  8. 净值记录（append_nav，持久化时间序列）
  9. 报告生成（每周/月触发，generate_reports）
  10. 告警（对账失败/NAV 异常下跌/停牌陷阱）

红线：
  ① 对账失败 → 立即终止，不继续执行后续任务（fail-closed）
  ② 告警必须发送（飞书 webhook，复用 T001 配置）
  ③ 幂等性：同日期重跑 → 覆盖前值（水位检查 + NAV 幂等写入）

"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as _date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable

from backtest.settle import settle_day_detail
from backtest.types import Bar
from data.incremental import IncrementalUpdater
from paper_trading.nav import NAVRecord, append_nav, load_nav_series
from paper_trading.reconciliation import ReconciliationError, reconcile_account
from paper_trading.reporting import generate_reports

logger = logging.getLogger(__name__)

__all__ = [
    "DailyReport",
    "DailyTaskRunner",
]

_ZERO = Decimal("0")


@dataclass
class DailyReport:
    """日终任务执行报告（完整记录，可用于审计）。"""

    date: _date
    success: bool                           # 是否完整执行（对账失败 → False）

    # 数据更新结果
    data_updated: bool = False
    data_symbols_ok: int = 0
    data_symbols_failed: int = 0

    # 策略信号
    signals_generated: int = 0

    # 订单提交
    orders_submitted: int = 0

    # 撮合结果
    trades_executed: int = 0

    # 除权处理
    exdiv_events: int = 0

    # 结算结果
    nav: Decimal = _ZERO
    cash: Decimal = _ZERO
    market_value: Decimal = _ZERO
    positions_count: int = 0

    # 对账结果
    reconciliation_ok: bool = False
    reconciliation_warnings: list[str] = field(default_factory=list)

    # 报告生成
    report_generated: bool = False
    report_json_path: str | None = None
    report_html_path: str | None = None

    # 告警
    alerts_sent: list[str] = field(default_factory=list)

    # 错误（如有）
    error: str | None = None


class DailyTaskRunner:
    """日终任务编排器（SDD-1 四环境同构原则）。

    依赖注入：
      - updater: IncrementalUpdater（数据更新）
      - broker: PaperBroker（订单提交/撮合）
      - strategy: 策略实例（generate_signals 方法）
      - alert_fn: 告警回调（如飞书 webhook）
      - nav_file: 净值时间序列路径
      - report_dir: 报告输出目录
      - risk_free_annual: 无风险年化利率
    """

    def __init__(
        self,
        *,
        updater: IncrementalUpdater,
        broker,  # PaperBroker（避免循环 import，鸭子类型）
        strategy,
        alert_fn: Callable[[str], None] | None = None,
        nav_file: Path,
        report_dir: Path,
        risk_free_annual: Decimal = Decimal("0.02"),
    ) -> None:
        self.updater = updater
        self.broker = broker
        self.strategy = strategy
        self.alert_fn = alert_fn
        self.nav_file = nav_file
        self.report_dir = report_dir
        self.risk_free_annual = risk_free_annual

    # ------------------------------------------------------------------
    def run_daily_tasks(
        self,
        trade_date: _date,
        *,
        symbols: list[str] | None = None,
        force_update: bool = False,
    ) -> DailyReport:
        """执行日终任务全链路（10 步编排）。

        Args:
            trade_date: 交易日（盘后执行，处理当日数据）。
            symbols: 股票池（None = 使用策略默认池）。
            force_update: 是否强制更新数据（True = 忽略水位重新采集）。

        Returns:
            DailyReport（成功/失败均返回，error 字段记录失败原因）。
        """
        report = DailyReport(date=trade_date, success=False)

        try:
            # ===== 1. 数据更新 =====
            logger.info("[1/10] 数据更新中... date=%s", trade_date)
            if symbols is None:
                symbols = self._get_default_symbols()

            update_results = self.updater.update(
                symbols, str(trade_date),
                start_date=None if not force_update else str(trade_date))

            report.data_updated = True
            report.data_symbols_ok = sum(1 for r in update_results.values() if r.ok)
            report.data_symbols_failed = sum(
                1 for r in update_results.values() if r.state == "failed")

            if report.data_symbols_failed > len(symbols) * 0.5:
                raise RuntimeError(
                    f"数据更新失败超 50%（{report.data_symbols_failed}/{len(symbols)}）")

            # ===== 2. 策略信号生成 =====
            logger.info("[2/10] 策略信号生成中...")
            signals = self.strategy.generate_signals(trade_date)
            report.signals_generated = len(signals)

            # ===== 3. 订单提交 =====
            logger.info("[3/10] 订单提交中... signals=%d", len(signals))
            orders = self.broker.submit_orders(signals, trade_date)
            report.orders_submitted = len(orders)

            # ===== 4. 撮合执行（次日开盘）=====
            logger.info("[4/10] 撮合执行中...")
            next_day = trade_date + timedelta(days=1)
            bars = self._load_bars(next_day, symbols)
            trades = self.broker.match_pending_orders(next_day, bars)
            report.trades_executed = len(trades)

            # ===== 5. 除权处理 =====
            logger.info("[5/10] 除权处理中...")
            exdiv_events = self._get_exdiv_events(trade_date)
            if exdiv_events:
                for symbol, event in exdiv_events.items():
                    self.broker.ledger.process_exdiv(symbol, event)
                report.exdiv_events = len(exdiv_events)

            # ===== 6. 结算 =====
            logger.info("[6/10] 结算中...")
            book = self.broker.ledger.book_view()
            settle_report = settle_day_detail(book, trade_date, bars, exdiv_events)
            report.nav = settle_report.nav
            report.cash = book.cash
            report.market_value = sum(
                pos.market_value for pos in book.positions.values())
            report.positions_count = len(book.positions)

            # ===== 7. 对账 =====
            logger.info("[7/10] 对账中...")
            nav_series = load_nav_series(self.nav_file)
            prev_nav = nav_series[-1][1] if nav_series else None

            recon_report = reconcile_account(
                book, trade_date, bars, prev_nav=prev_nav)

            report.reconciliation_ok = recon_report.ok
            report.reconciliation_warnings = list(recon_report.warnings)

            if not recon_report.ok:
                error_msg = f"对账失败: {recon_report.discrepancies}"
                self._send_alert(f"⚠️ {trade_date} 对账失败\n{error_msg}")
                raise ReconciliationError(error_msg)

            if recon_report.warnings:
                self._send_alert(
                    f"⚠️ {trade_date} 对账告警\n" +
                    "\n".join(recon_report.warnings))

            # ===== 8. 净值记录 =====
            logger.info("[8/10] 净值记录中...")
            nav_record = NAVRecord(
                date=trade_date,
                nav=settle_report.nav,
                cash=book.cash,
                market_value=report.market_value,
            )
            append_nav(self.nav_file, [nav_record], idempotent=True)

            # ===== 9. 报告生成（每周一 / 每月 1 日）=====
            if self._should_generate_report(trade_date):
                logger.info("[9/10] 报告生成中...")
                result = self._build_backtest_result(book, nav_series)
                json_path, html_path = generate_reports(
                    result,
                    self.report_dir,
                    trade_date.strftime("%Y%m%d"),
                    risk_free_annual=self.risk_free_annual,
                )
                report.report_generated = True
                report.report_json_path = str(json_path)
                report.report_html_path = str(html_path)

                self._send_alert(
                    f"📊 {trade_date} 绩效报告已生成\n"
                    f"HTML: {html_path}")
            else:
                logger.info("[9/10] 报告生成跳过（非周/月报告日）")

            # ===== 10. 告警汇总 =====
            logger.info("[10/10] 任务完成")
            report.success = True

            # 发送成功通知（每日摘要）
            self._send_alert(
                f"✅ {trade_date} 日终任务完成\n"
                f"NAV: {report.nav}\n"
                f"持仓: {report.positions_count} 只\n"
                f"成交: {report.trades_executed} 笔")

        except ReconciliationError as exc:
            report.error = str(exc)
            logger.error("对账失败: %s", exc)
        except Exception as exc:  # noqa: BLE001
            report.error = str(exc)
            logger.exception("日终任务失败")
            self._send_alert(f"❌ {trade_date} 日终任务失败\n{exc}")

        return report

    # ------------------------------------------------------------------
    def _get_default_symbols(self) -> list[str]:
        """获取默认股票池（策略依赖的全部标的）。"""
        if hasattr(self.strategy, "get_universe"):
            return self.strategy.get_universe()
        # Fallback：返回空列表（需手动传 symbols）
        return []

    def _load_bars(self, date: _date, symbols: list[str]) -> dict[str, Bar]:
        """加载当日行情（停牌 = 键缺席）。"""
        # TODO: 从 Feed 读取（复用 backtest.feed）
        # 暂时返回空字典（占位）
        return {}

    def _get_exdiv_events(self, date: _date) -> dict[str, dict]:
        """获取当日除权事件（symbol → {factor, cash_dividend}）。"""
        # TODO: 从数据层读取除权表
        return {}

    def _build_backtest_result(self, book, nav_series: list):
        """构造伪 BacktestResult（供 compute_metrics 使用）。"""
        # 鸭子类型：只需 nav_curve / trades / final_nav 三字段
        class _Result:
            def __init__(self):
                self.nav_curve = {str(d): nav for d, nav in nav_series}
                self.trades = list(book.ledger._journal.values())  # 读流水
                self.final_nav = nav_series[-1][1] if nav_series else _ZERO
                # 补充字段（metrics.py 可能需要）
                self.journal_entries = []
                self.orders = []
                self.bars_by_date = {}

        return _Result()

    def _should_generate_report(self, date: _date) -> bool:
        """判断是否生成报告（每周一 + 每月 1 日）。"""
        return date.weekday() == 0 or date.day == 1

    def _send_alert(self, message: str) -> None:
        """发送告警（飞书 webhook）。"""
        if self.alert_fn:
            try:
                self.alert_fn(message)
            except Exception as exc:  # noqa: BLE001
                logger.warning("告警发送失败: %s", exc)
