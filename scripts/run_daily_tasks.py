#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T403 日终任务手动触发脚本。

用法：
    py -3.11 scripts/run_daily_tasks.py --date 2026-09-02
    py -3.11 scripts/run_daily_tasks.py --date 2026-09-02 --force-update
    py -3.11 scripts/run_daily_tasks.py --date 2026-09-02 --symbols sz.000001,sz.000002

环境变量：
    PAPER_TRADING_ROOT     —— 模拟盘数据根目录（默认 paper_trading/data）
    FEISHU_WEBHOOK_URL     —— 飞书告警 webhook（可选）

退出码：
    0 = 成功
    1 = 对账失败 / 致命错误
    2 = 参数错误
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date as _date
from decimal import Decimal
from pathlib import Path

# 注入 finai/ 到 sys.path（与其他 scripts/ 统一）
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from data.collector import DailyCollector
from data.incremental import IncrementalUpdater
from paper_trading.daily_tasks import DailyTaskRunner

# TODO: PaperBroker / Strategy 需要实现后取消注释
# from paper_trading.broker import PaperBroker
# from strategy.candidates import DividendStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _send_feishu_alert(message: str) -> None:
    """发送飞书告警（T001 webhook）。"""
    webhook_url = os.getenv("FEISHU_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("FEISHU_WEBHOOK_URL 未设置，跳过告警")
        return

    import requests

    payload = {
        "msg_type": "text",
        "content": {"text": message},
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=5, proxies={
            "http": "http://127.0.0.1:7897",
            "https": "http://127.0.0.1:7897",
        })
        resp.raise_for_status()
        logger.info("飞书告警已发送")
    except Exception as exc:  # noqa: BLE001
        logger.error("飞书告警失败: %s", exc)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="T403 日终任务手动触发",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--date",
        type=str,
        required=True,
        help="交易日（ISO 格式，如 2026-09-02）",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        help="股票代码列表（逗号分隔，如 sz.000001,sz.000002）",
    )
    parser.add_argument(
        "--force-update",
        action="store_true",
        help="强制重新采集数据（忽略水位）",
    )
    parser.add_argument(
        "--root",
        type=str,
        default=os.getenv("PAPER_TRADING_ROOT", "paper_trading/data"),
        help="模拟盘数据根目录",
    )

    args = parser.parse_args()

    try:
        trade_date = _date.fromisoformat(args.date)
    except ValueError:
        logger.error("日期格式错误（需 ISO 格式，如 2026-09-02）: %s", args.date)
        return 2

    symbols = args.symbols.split(",") if args.symbols else None

    # ===== 初始化依赖 =====
    root_dir = Path(args.root)
    root_dir.mkdir(parents=True, exist_ok=True)

    # 数据采集器
    collector = DailyCollector(root=root_dir.parent / "daily_bars")
    updater = IncrementalUpdater(collector)

    # TODO: 实现 PaperBroker + Strategy 后取消注释
    # broker = PaperBroker(...)
    # strategy = DividendStrategy(...)

    # 占位：暂时返回错误（待 T401/T402 实现 broker/strategy）
    logger.error(
        "PaperBroker / Strategy 尚未实现（待 T401/T402 完成）\n"
        "当前仅可测试数据更新部分")
    return 1

    # # 日终任务编排器
    # runner = DailyTaskRunner(
    #     updater=updater,
    #     broker=broker,
    #     strategy=strategy,
    #     alert_fn=_send_feishu_alert,
    #     nav_file=root_dir / "nav_series.parquet",
    #     report_dir=root_dir.parent / "reports",
    #     risk_free_annual=Decimal("0.02"),
    # )
    #
    # # 执行日终任务
    # report = runner.run_daily_tasks(
    #     trade_date,
    #     symbols=symbols,
    #     force_update=args.force_update,
    # )
    #
    # # 输出报告摘要
    # logger.info("=" * 60)
    # logger.info("日终任务报告 %s", trade_date)
    # logger.info("=" * 60)
    # logger.info("成功: %s", report.success)
    # logger.info("数据更新: %d 成功 / %d 失败",
    #             report.data_symbols_ok, report.data_symbols_failed)
    # logger.info("信号生成: %d", report.signals_generated)
    # logger.info("订单提交: %d", report.orders_submitted)
    # logger.info("成交执行: %d", report.trades_executed)
    # logger.info("NAV: %s", report.nav)
    # logger.info("对账: %s", "通过" if report.reconciliation_ok else "失败")
    # if report.reconciliation_warnings:
    #     logger.warning("对账告警: %s", report.reconciliation_warnings)
    # if report.report_generated:
    #     logger.info("报告: %s", report.report_html_path)
    # if report.error:
    #     logger.error("错误: %s", report.error)
    #
    # return 0 if report.success else 1


if __name__ == "__main__":
    sys.exit(main())
