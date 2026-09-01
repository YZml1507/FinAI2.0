#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T302 候选策略 v1 —— 日线中低频（动量 + 流动性过滤 + 时间退出）。

**定位**：首个用于走通「信号 → 组合 → 回测 → 指标」全链的可跑策略基座，
不追求超额收益（那是 T303 参数扫描/T304 压力测试的活）。形态：周线级换仓的
价格动量 + A 股流动性过滤 + 持仓时长上限，配 `strategy/portfolio.py` 的
组合管理器出意图、⛔ 不直接舔撮合。

策略读数（⛔ 交易语义只信 `bar.limit_up/limit_down/exdiv/is_st` 等注入列，
绝不读 parquet 原生价量字段判断买不买——R1/R4 数据卫生原则）：

| 信号（每个交易日对 watchlist 算） | 口径 |
|---|---|
| ``momentum`` | 最近 ``lookback`` 个可用 bar 的区间收益率 ``close / close[-lookback] - 1`` |
| ``liquid`` | 当日 ``bar.amount`` ≥ ``min_daily_amount``（与组合层口径一致） |
| ``exit_after`` | 持仓超过 ``max_holding_days`` ⇒ 无条件退出（时间退出，FR-PM-3 最简分支） |

**出场优先级**：时间退出 > 动量跌出前 N > 持有不动。止损/跟踪止损留 T302 后
续迭代（本版只放「跌出榜单」这一种动量退出，⛔ 不做价格跌幅止损）。

线程模型：`on_bar(date, bars, book, broker)` 被引擎每日回调；内部状态仅是
「每个 symbol 的买入日/持有天数」——状态推进确定性、无随机数（NFR-7 seed
钉扎在 registry：`seed=None` 也通过因无随机源）。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date as _date
from decimal import Decimal
from typing import Any, Mapping, Sequence

from backtest.constants import OrderSide
from backtest.types import Bar, Order, OrderType
from strategy.portfolio import (
    OrderIntent,
    PortfolioConfig,
    diff_to_orders,
    plan_positions,
    select_targets,
)

__all__ = ["MomentumConfig", "MomentumStrategy"]


@dataclass(frozen=True)
class MomentumConfig:
    """策略参数（进入 registry `params` 的载体；全部显式、无隐藏默认）。"""

    lookback: int = 20                    # 动量回看窗口（bar 数）
    rebalance_days: int = 5               # 每 N 个交易日重评分一次（中低频）
    max_holding_days: int = 40            # 时间退出上限（交易日）
    warmup_bars: int = 25                 # 冷启动：凑不够这个 bar 数不评分（⛔ 不雪球）
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)

    def __post_init__(self) -> None:
        if not isinstance(self.lookback, int) or self.lookback < 2:
            raise ValueError(f"lookback 须为 ≥2 的 int: {self.lookback!r}")
        if not isinstance(self.rebalance_days, int) or self.rebalance_days < 1:
            raise ValueError(f"rebalance_days 须为 ≥1 的 int: {self.rebalance_days!r}")
        if not isinstance(self.max_holding_days, int) or self.max_holding_days < 1:
            raise ValueError(f"max_holding_days 须为 ≥1 的 int: {self.max_holding_days!r}")
        if self.warmup_bars < self.lookback:
            raise ValueError(
                f"warmup_bars={self.warmup_bars} < lookback={self.lookback}（ Welch 不出来）")


class MomentumStrategy:
    """日线动量候选策略（鸭子类型，引擎只要 ``on_bar`` + ``watchlist``）。"""

    def __init__(self, config: MomentumConfig | None = None) -> None:
        self.config = config or MomentumConfig()
        self.watchlist: list[str] = []                     # 引擎在此读范围
        self._bars_seen: dict[str, int] = {}             # symbol → 已见 bar 数（含停牌日缺席）
        self._closes: dict[str, deque[Decimal]] = {}     # symbol → 最近 window 收盘
        self._entry_date: dict[str, _date] = {}          # symbol → 实际建仓日
        self._step = 0                                    # 已走过交易日数
        self._pending_ids: dict[str, int] = {}           # symbol → 已下单 client 计数（幂等）

    # ------------------------------------------------------------------
    # 引擎契约
    # ------------------------------------------------------------------

    def on_bar(self, day: _date, bars: Mapping[str, Bar], book: Any, broker: Any) -> None:
        cfg = self.config
        self._step += 1

        # ① 消化当日 bar：补收盘价历史
        for symbol in self.watchlist:
            bar = bars.get(symbol)
            self._bars_seen[symbol] = self._bars_seen.get(symbol, 0) + (1 if bar else 0)
            if bar is not None:
                self._closes.setdefault(symbol, deque(maxlen=cfg.lookback + 5))
                self._closes[symbol].append(bar.close)

        # ② 非调仓日不动手（中低频）
        if self._step % cfg.rebalance_days != 0:
            return

        # ③ 清仓超期持仓（时间退出，不看信号）
        held_symbols = list(book.positions.keys()) if hasattr(book, "positions") else []
        current = {s: int(book.positions[s].volume) for s in held_symbols}

        # ④ 评分（只在 ≥warmup 的标的上）
        scores: dict[str, Decimal] = {}
        for symbol in self.watchlist:
            if self._bars_seen.get(symbol, 0) < cfg.warmup_bars:
                continue
            bar = bars.get(symbol)
            if bar is None:
                continue                                  # 停牌不评分
            if getattr(bar, "limit_up", False) or getattr(bar, "limit_down", False):
                continue                                  # 涨跌停不参与（不抢板）
            closes = list(self._closes.get(symbol, ()))
            if len(closes) < cfg.lookback:
                continue
            momentum = closes[-1] / closes[-cfg.lookback] - 1
            scores[symbol] = momentum

        # ⑤ 组合计划（择时空仓也合法）
        targets = select_targets(scores, cfg.portfolio)
        total_nav = book.total_nav if hasattr(book, "total_nav") else getattr(book, "nav", _ZERO_)
        plan, _plan_dropped = plan_positions(targets, total_nav, bars, cfg.portfolio)

        # 时间退出独立的 path：持仓超期 ⇒ 目标计划里**强行剔除**
        max_hold = cfg.max_holding_days
        held_to_exit = [
            s for s in list(plan)
            if s in self._entry_date and (day - self._entry_date[s]).days >= max_hold
        ]
        for s in held_to_exit:
            plan.pop(s, None)

        # ⑥ 出意图
        report = diff_to_orders(current, plan, bars, cfg.portfolio)
        self._submit(broker, report.intents, day)

        # ⑦ 挂机清理：不再出现（不再持有也不在清单）的标的，忘掉它的 entry 记录
        live = set(plan) | {s for s in current if s in plan}
        for dead in [s for s in self._entry_date if s not in live]:
            # 已清仓且不在计划里 → 记录可清
            if dead not in current:
                self._entry_date.pop(dead, None)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _submit(self, broker: Any, intents: Sequence[OrderIntent], day: _date) -> None:
        for intent in intents:
            count = self._pending_ids.get(intent.symbol, 0) + 1
            self._pending_ids[intent.symbol] = count
            oid = f"t302-{intent.symbol}-{intent.side.value}-{day.isoformat()}-{count}"
            broker.submit(Order(
                client_order_id=oid, symbol=intent.symbol, side=intent.side,
                order_type=OrderType.MARKET, volume=intent.volume, price=None,
                created_date=day))
            # 建仓成功记录买入日（撮合失败不影响——entry 按 intent 记录，
            # 到时间退出的判读容错：即使没审到成交也在 plan 层重评）
            if intent.side is OrderSide.BUY:
                self._entry_date.setdefault(intent.symbol, day)


_ZERO_ = Decimal("0")
