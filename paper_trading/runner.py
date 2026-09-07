#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T401 §4 模拟盘执行器 —— ``PaperTradingRunner``（日终任务编排）。

``PaperTradingRunner`` 是模拟盘的日终任务编排器，负责：
  1. 状态加载（持仓/资金/订单队列）
  2. 数据更新（增量采集，复用 ``data.incremental``）
  3. 策略信号生成（调用策略 ``on_bar``）
  4. 订单提交（broker.submit）
  5. 撮合执行（broker.on_bars / settle）
  6. 状态保存（持久化到 JSON）

日内时序（与回测引擎对齐）：
  - T 日盘后：采集 T 日数据 → 策略看 T 日行情 → 生成信号 → 下单（SUBMITTED）
  - T+1 日盘后：采集 T+1 日数据 → 先撮合（T 日挂单用 T+1 开盘价成交）→ 后信号

幂等保护：
  - ``last_trading_date`` 防止重复执行同一交易日
  - 增量采集自带幂等（``data.incremental``）
  - 状态哈希校验（``PaperTradingState``）

与回测差异：
  - 回测：一次性跑完整个区间
  - 模拟盘：每日增量执行一次，中断可恢复
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from typing import Any

from backtest.feed import ParquetDailyFeed
from backtest.fees import make_fee_model, make_price_model
from backtest.ledger import BookView, Ledger
from backtest.matching import MatchEngine
from data.collector import DailyCollector
from data.incremental import IncrementalUpdater

from paper_trading.broker import PaperBroker
from paper_trading.config import PaperTradingConfig
from paper_trading.state import PaperTradingState, StateError

logger = logging.getLogger(__name__)

__all__ = ["PaperTradingRunner", "RunnerError"]


class RunnerError(RuntimeError):
    """执行器错误（幂等冲突/数据缺失/策略异常）。"""


@dataclass
class DailyRunResult:
    """单日执行结果（日志/监控用）。"""

    date: _date
    success: bool
    nav: Decimal
    cash: Decimal
    positions_count: int
    orders_submitted: int
    orders_filled: int
    orders_rejected: int
    error: str = ""


class PaperTradingRunner:
    """模拟盘日终任务执行器（复用回测引擎，SDD-1 同构）。

    使用示例::

        from paper_trading.runner import PaperTradingRunner
        from paper_trading.config import PaperTradingConfig
        from strategy.candidates import MomentumStrategy

        config = PaperTradingConfig(
            initial_capital=Decimal("100000"),
            data_root=Path("data/daily_bars"),
            state_path=Path("paper_trading/state.json"),
        )
        strategy = MomentumStrategy(...)
        runner = PaperTradingRunner(config, strategy)

        # 首次运行（冷启动）
        result = runner.run_daily("2026-09-02")

        # 次日运行（热启动，自动加载状态）
        result = runner.run_daily("2026-09-03")
    """

    def __init__(
        self,
        config: PaperTradingConfig,
        strategy: Any,
    ) -> None:
        """
        Args:
            config: 模拟盘配置（初始资金/路径/策略参数）。
            strategy: 策略对象（鸭子类型，须有 ``on_bar`` 方法）。
        """
        self.config = config
        self.strategy = strategy

        # 行情源（复用回测）
        self.feed = ParquetDailyFeed(root=config.data_root)

        # 增量更新器（盘后采集）
        collector = DailyCollector(root=config.data_root)
        self.updater = IncrementalUpdater(collector)

        # 撮合引擎（复用回测，费用模型/成交模型注入）
        self.matcher = MatchEngine(
            fee_model=make_fee_model(),
            price_model=make_price_model(),
        )

        # 账本与经纪商（每次 run_daily 时初始化或恢复）
        self.ledger: Ledger | None = None
        self.broker: PaperBroker | None = None

        # 状态（冷启动=None，热启动=加载）
        self._state: PaperTradingState | None = None

        logger.info("PaperTradingRunner 初始化完成")

    def run_daily(self, date: str | _date) -> DailyRunResult:
        """执行单日任务（幂等，可重复调用）。

        时序：
          1. 幂等检查（已执行过该日期 → 跳过）
          2. 状态加载/初始化
          3. 数据更新（增量采集）
          4. 先撮合（昨日挂单用今日开盘价成交）
          5. 后信号（策略看今日行情，下单进 pending）
          6. 日终结算
          7. 状态保存

        Args:
            date: 交易日（"YYYY-MM-DD" 或 datetime.date）。

        Returns:
            ``DailyRunResult``（成功/NAV/订单统计）。

        Raises:
            RunnerError: 幂等冲突/数据缺失/策略异常。
        """
        run_date = _as_date(date)
        logger.info("=" * 60)
        logger.info("模拟盘日终任务开始: %s", run_date)

        try:
            # ① 幂等检查
            if self._is_already_run(run_date):
                logger.warning("日期 %s 已执行过，跳过（幂等保护）", run_date)
                return self._make_skip_result(run_date)

            # ② 状态加载/初始化
            self._init_or_restore_state(run_date)
            assert self.broker is not None
            assert self.ledger is not None

            # ③ 数据更新（增量采集）
            self._update_data(run_date)

            # ④ 策略 watchlist（取数范围）
            symbols = self._get_symbols_for_date(run_date)

            # ⑤ 先撮合（昨日挂单用今日开盘价成交）
            bars = self.feed.get_bars(symbols, run_date)
            orders_before = len(self.broker.orders)
            self.broker.on_bars(run_date, bars)
            orders_after_match = len(self.broker.orders)

            # ⑥ 后信号（策略看今日行情，下单进 pending）
            if not hasattr(self.strategy, "on_bar"):
                raise RunnerError(
                    f"策略 {type(self.strategy).__name__} 缺 on_bar 方法"
                )
            self.strategy.on_bar(run_date, bars, self.broker.book, self.broker)
            orders_after_signal = len(self.broker.orders)

            # 风控：单日下单次数上限
            new_orders = orders_after_signal - orders_before
            if new_orders > self.config.max_orders_per_day:
                raise RunnerError(
                    f"单日下单 {new_orders} 笔，超过上限 "
                    f"{self.config.max_orders_per_day}（防御失控策略）"
                )

            # ⑦ 日终结算
            self.broker.settle(run_date, bars=bars)

            # ⑧ 状态保存
            self._save_state(run_date)

            # 统计
            filled = sum(
                1 for o in self.broker.orders
                if o.created_date == run_date and o.status.name == "FILLED"
            )
            rejected = sum(
                1 for o in self.broker.orders
                if o.created_date == run_date and o.status.name == "REJECTED"
            )

            result = DailyRunResult(
                date=run_date,
                success=True,
                nav=self.broker.book.total_nav,
                cash=self.broker.book.cash,
                positions_count=len([
                    p for p in self.broker.book.positions.values()
                    if p.volume > 0
                ]),
                orders_submitted=new_orders,
                orders_filled=filled,
                orders_rejected=rejected,
            )

            logger.info(
                "模拟盘日终任务完成: %s | NAV=%s | 持仓=%d | 新单=%d | 成交=%d | 拒单=%d",
                run_date, result.nav, result.positions_count,
                result.orders_submitted, result.orders_filled, result.orders_rejected,
            )
            return result

        except Exception as exc:
            logger.exception("模拟盘日终任务失败: %s", run_date)
            return DailyRunResult(
                date=run_date,
                success=False,
                nav=Decimal("0"),
                cash=Decimal("0"),
                positions_count=0,
                orders_submitted=0,
                orders_filled=0,
                orders_rejected=0,
                error=str(exc),
            )

    def _is_already_run(self, date: _date) -> bool:
        """幂等检查：该日期是否已执行过。"""
        if not self.config.state_path.exists():
            return False
        try:
            state = PaperTradingState.load(self.config.state_path)
            last_date = _date.fromisoformat(state.last_trading_date)
            return last_date >= date
        except (StateError, ValueError):
            return False

    def _init_or_restore_state(self, date: _date) -> None:
        """状态加载/初始化（冷启动入金，热启动恢复）。"""
        # 冷启动：首次运行
        if not self.config.state_path.exists():
            logger.info("冷启动：创建新账本（初始资金 %s）", self.config.initial_capital)
            self.ledger = Ledger(self.config.initial_capital, date=date)
            self.broker = PaperBroker(
                self.matcher, self.ledger, self.feed, enable_dividend_tax=True
            )
            return

        # 热启动：加载已有状态
        logger.info("热启动：加载已有状态")
        state = PaperTradingState.load(self.config.state_path)
        self.ledger = Ledger(self.config.initial_capital, date=date)
        self.broker = PaperBroker(
            self.matcher, self.ledger, self.feed, enable_dividend_tax=True
        )
        state.restore_to_broker(self.broker)
        logger.info("状态恢复完成: %s", self.broker.get_summary())

    def _update_data(self, date: _date) -> None:
        """增量数据更新（调用 data.incremental）。"""
        if getattr(self.config, "offline", False):
            logger.info("离线模式：跳过增量数据网络采集: %s", date)
            return
        logger.info("增量数据更新: %s", date)
        # v1 简化：只更新 watchlist 标的（全市场更新太慢）
        symbols = _watchlist(self.strategy)
        if not symbols:
            logger.warning("策略 watchlist 为空，跳过数据更新")
            return

        result = self.updater.update(
            symbols=symbols,
            end_date=date.isoformat(),
        )
        for res in result:
            if res.state == "failed":
                logger.warning(
                    "增量更新失败: %s (%s)", res.symbol, res.warnings
                )
            elif res.state == "ok":
                logger.debug("增量更新成功: %s (+%d 行)", res.symbol, res.rows)

    def _get_symbols_for_date(self, date: _date) -> list[str]:
        """当日取数范围：挂单 ∪ 持仓 ∪ watchlist（与回测引擎一致）。"""
        assert self.broker is not None
        symbols = set(self.broker.pending_symbols())
        symbols |= self.broker.position_symbols()
        symbols |= _watchlist(self.strategy)
        return sorted(symbols)

    def _save_state(self, date: _date) -> None:
        """保存状态到 JSON（原子写）。"""
        assert self.broker is not None
        state = PaperTradingState.from_broker(self.broker, date)
        state.save(self.config.state_path)

    def _make_skip_result(self, date: _date) -> DailyRunResult:
        """构造"跳过"结果（幂等保护触发时）。"""
        return DailyRunResult(
            date=date,
            success=True,
            nav=Decimal("0"),
            cash=Decimal("0"),
            positions_count=0,
            orders_submitted=0,
            orders_filled=0,
            orders_rejected=0,
            error="幂等跳过",
        )


# ---------------------------------------------------------------------- 工具

def _as_date(value: str | _date) -> _date:
    """``'YYYY-MM-DD'`` / ``date`` → ``datetime.date``。"""
    if isinstance(value, _date):
        return value
    text = str(value).strip()
    parts = text.split("-")
    if len(parts) != 3:
        raise RunnerError(f"日期须为 'YYYY-MM-DD' 或 datetime.date，得到 {value!r}")
    try:
        return _date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError as exc:
        raise RunnerError(f"无法解析日期 {value!r}: {exc}") from exc


def _watchlist(strategy: Any) -> set[str]:
    """读策略的 ``watchlist``（属性或无参可调用；缺失 → 空集）。"""
    raw = getattr(strategy, "watchlist", None)
    if raw is None:
        return set()
    if callable(raw):
        raw = raw()
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {raw}
    try:
        return {str(s) for s in raw}
    except (TypeError, ValueError):
        return set()
