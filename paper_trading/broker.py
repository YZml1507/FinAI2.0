#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T401 §3 模拟盘经纪商 —— ``PaperBroker``（复用回测撮合）。

``PaperBroker`` 实现 ``Broker`` 协议，完全复用回测引擎的撮合逻辑：
  - 继承 ``BacktestBroker``（零重写撮合规则）
  - 数据源：从 ``data/daily_bars`` Parquet 读取（与回测同构）
  - T+1 约束：今日信号 → 明日开盘成交（与回测完全一致）

v1 简化路径（盘后采集+次日回放）：
  - ⛔ 不做实时行情接入（避免 WebSocket/消息队列）
  - 日终任务触发：盘后采集 → 策略信号 → 订单提交 → 次日开盘撮合
  - 数据来源：复用 ``ParquetDailyFeed``（与回测同一份代码）

与回测唯一差异：
  - 回测：一次性跑完整个区间
  - 模拟盘：每日增量执行一次，状态持久化

SDD-1 同构验证：
  - 同一策略 + 同一数据 + 同一参数 ⇒ 回测与模拟盘 NAV 曲线一致
"""
from __future__ import annotations

import logging
from datetime import date as _date
from decimal import Decimal

from backtest.broker import BacktestBroker
from backtest.feed import DataFeed, ParquetDailyFeed
from backtest.ledger import Ledger
from backtest.matching import MatchEngine

logger = logging.getLogger(__name__)

__all__ = ["PaperBroker"]


class PaperBroker(BacktestBroker):
    """模拟盘经纪商（完全复用回测撮合，SDD-1 同构）。

    与 ``BacktestBroker`` 的差异：
      - 实例化时可注入 ``ParquetDailyFeed``（v1 从本地 Parquet 读取）
      - 未来可扩展：实时行情接入（替换 feed）、实盘桥接（替换撮合）

    使用示例::

        from paper_trading.broker import PaperBroker
        from backtest.feed import ParquetDailyFeed
        from backtest.ledger import Ledger
        from backtest.matching import MatchEngine

        feed = ParquetDailyFeed(root="data/daily_bars")
        ledger = Ledger()
        matcher = MatchEngine()
        broker = PaperBroker(matcher, ledger, feed)

        # 入金
        broker.deposit(Decimal("100000"), date=today, ref_id="INIT")

        # 策略下单
        order = Order(...)
        broker.submit(order)

        # 次日撮合
        bars = feed.get_bars(["sh.600000"], next_day)
        broker.on_bars(next_day, bars)
        broker.settle(next_day)
    """

    def __init__(
        self,
        matcher: MatchEngine,
        ledger: Ledger,
        feed: DataFeed | None = None,
    ) -> None:
        """
        Args:
            matcher: 撮合引擎（复用 ``backtest.matching.MatchEngine``）。
            ledger: 双账本（复用 ``backtest.ledger.Ledger``）。
            feed: 行情源（v1 用 ``ParquetDailyFeed``；未来可扩展实时源）。
        """
        super().__init__(matcher, ledger, feed)
        logger.info(
            "PaperBroker 初始化完成（复用回测撮合，SDD-1 同构）"
        )

    # ⛔ 不重写任何撮合逻辑 —— 全部继承 BacktestBroker
    # submit / cancel / on_bars / settle / _match_one 全部复用

    def reset_for_new_day(self, date: _date) -> None:
        """日间重置（清理上一日遗留状态，v1 预留）。

        v1 模拟盘按日执行，每日独立调用 on_bars → settle，
        不需要显式重置（状态由 settle 推进）。
        本方法预留给未来"日内多次执行"场景。
        """
        pass  # v1 无需实现

    def get_summary(self) -> dict:
        """获取账户摘要（便于日志/监控）。"""
        book = self.book
        return {
            "cash": str(book.cash),
            "frozen_cash": str(book.frozen_cash),
            "market_value": str(book.total_market_value()),
            "nav": str(book.total_nav),
            "positions_count": len([p for p in book.positions.values() if p.volume > 0]),
            "pending_orders_count": len(self._pending),
        }
