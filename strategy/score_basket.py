#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e65 外部分数宽篮策略 —— 预计算分数表驱动的宽篮等权组合（鸭子类型契约）。

与 ``MomentumStrategy``/``DividendStrategy`` 同一引擎契约（``on_bar`` +
``watchlist``），差异只在信号来源：**分数不从 bar 现算，而从外部分数表
（sig_date, ts_code, score）查**——用于把离线模型（如 e63 XGB fwd60 分数）
灌进真引擎，做含 T+1 撮合/费用/涨跌停/停牌语义的真实回测。

口径声明（fail-closed）：

* **分数对齐**：调仓日 T 使用 ``sig_date ≤ T`` 的最新一期分数；最新一期
  距 T 超过 ``max_score_age_days`` 自然日 ⇒ 该日不评分不交易（防吃过期
  信号）；无更早一期（暖机前）⇒ 同样不交易。
* **代码映射**：``600000.SH`` / ``000001.SZ`` / ``430xxx.BJ`` → ``sh.600000``
  /``sz.000001``/``bj.430xxx``（与 ``data/daily_bars/{symbol}/`` 目录名一致）。
* **可选标的过滤**：当日无 bar（停牌/未上市）→ 不评分；涨跌停不参与建仓
  （与动量策略同口径，不抢板）；得分缺失/NaN → 剔除。
* **组合**：复用 ``select_targets`` → ``plan_positions`` → ``diff_to_orders``
  三段链；宽篮构型经 ``PortfolioConfig`` 参数化（target/hard_limit/min_value
  由调用方注入，本类不设上限假设）。
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
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

__all__ = ["ScoreBasketConfig", "ScoreBasketStrategy", "normalize_score_code"]


def normalize_score_code(ts_code: str) -> str:
    """``600000.SH`` → ``sh.600000``；``430047.BJ`` → ``bj.430047``。

    裸 6 位兜底：6/9 开头 → sh；0/3 → sz；4/8 → bj。非法 ⇒ raise。
    """
    code = ts_code.strip()
    if "." in code:
        num, mkt = code.split(".", 1)
        m = mkt.upper()
        if m in ("SH", "SS"):
            return f"sh.{num}"
        if m == "SZ":
            return f"sz.{num}"
        if m == "BJ":
            return f"bj.{num}"
        raise ValueError(f"未知市场后缀: {ts_code!r}")
    if len(code) == 6 and code.isdigit():
        if code[0] in "69":
            return f"sh.{code}"
        if code[0] in "03":
            return f"sz.{code}"
        if code[0] in "48":
            return f"bj.{code}"
    raise ValueError(f"无法规范化的代码: {ts_code!r}")


@dataclass(frozen=True)
class ScoreBasketConfig:
    """外部分数宽篮构型。"""

    portfolio: PortfolioConfig
    rebalance_days: int = 20            # 调仓频率（交易日）
    max_score_age_days: int = 45        # 最新分数期距调仓日的最大自然日
    skip_limit: bool = True             # 涨跌停不参与评分（建仓侧）
    warmup_bars: int = 5                # 入场前 bar 暖机（防冷启动误调）

    def __post_init__(self) -> None:
        if self.rebalance_days < 1:
            raise ValueError("rebalance_days 须 ≥ 1")
        if self.max_score_age_days < 1:
            raise ValueError("max_score_age_days 须 ≥ 1")


class ScoreBasketStrategy:
    """外部分数���动的宽篮等权策略（月度调仓 default）。"""

    def __init__(
        self,
        config: ScoreBasketConfig,
        score_table: Mapping[_date, Mapping[str, float]],
        universe_provider: Any | None = None,
    ) -> None:
        """
        Args:
            score_table: {sig_date: {engine_symbol: score}}——调用方负责把
                parquet 读进来并按 ``normalize_score_code`` 规范代码。
            universe_provider: ``(date) -> Iterable[str]`` 当日可交易池
                （防幸存者偏差）；None = 用分数表覆盖域做 watchlist。
        """
        self.config = config
        self.universe_provider = universe_provider
        self.watchlist: list[str] = []
        # 按日期排序的分数期索引
        self._sig_dates: list[_date] = sorted(score_table.keys())
        self._score_by_date: dict[_date, dict[str, float]] = {
            d: dict(v) for d, v in score_table.items()
        }
        # 并集覆盖域（provider 缺席时的默认 watchlist）
        covered: set[str] = set()
        for v in self._score_by_date.values():
            covered.update(v.keys())
        self._covered = sorted(covered)
        self._bar_count = 0
        self._pending_ids: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 引擎契约
    # ------------------------------------------------------------------

    def on_bar(self, day: _date, bars: Mapping[str, Bar], book: Any, broker: Any) -> None:
        cfg = self.config
        self._bar_count += 1

        if self.universe_provider is not None:
            self.watchlist = list(self.universe_provider(day))
        else:
            # 分数覆盖域——停牌/未上市由 feed 缺席 + 评分侧 bars.get 双保险
            self.watchlist = self._covered

        if self._bar_count < cfg.warmup_bars:
            return
        if (self._bar_count - cfg.warmup_bars) % cfg.rebalance_days != 0:
            return

        # ① 分数对齐：取 sig_date ≤ day 的最新一期
        i = bisect.bisect_right(self._sig_dates, day) - 1
        if i < 0:
            return                                   # 暖机前无分数
        sig_day = self._sig_dates[i]
        if (day - sig_day).days > cfg.max_score_age_days:
            return                                   # 分数过期，不交易

        # ② 评分（当日有 bar + 分数非空；涨跌停不建仓）
        score_row = self._score_by_date[sig_day]
        scores: dict[str, Decimal] = {}
        for symbol in self.watchlist:
            raw = score_row.get(symbol)
            if raw is None:
                continue
            bar = bars.get(symbol)
            if bar is None:
                continue
            if cfg.skip_limit and (
                    getattr(bar, "limit_up", False) or getattr(bar, "limit_down", False)):
                continue
            scores[symbol] = Decimal(str(raw))
        if not scores:
            return

        # ③ 组合三段链
        held_symbols = list(book.positions.keys()) if hasattr(book, "positions") else []
        current = {s: int(book.positions[s].volume) for s in held_symbols}
        targets = select_targets(scores, cfg.portfolio)
        total_nav = book.total_nav if hasattr(book, "total_nav") else getattr(book, "nav", Decimal("0"))
        plan, _plan_dropped = plan_positions(targets, total_nav, bars, cfg.portfolio)
        report = diff_to_orders(current, plan, bars, cfg.portfolio)
        self._submit(broker, report.intents, day)

    # ------------------------------------------------------------------

    def _submit(self, broker: Any, intents: Sequence[OrderIntent], day: _date) -> None:
        for intent in intents:
            count = self._pending_ids.get(intent.symbol, 0) + 1
            self._pending_ids[intent.symbol] = count
            oid = f"e65-{intent.symbol}-{intent.side.value}-{day.isoformat()}-{count}"
            broker.submit(Order(
                client_order_id=oid, symbol=intent.symbol, side=intent.side,
                order_type=OrderType.MARKET, volume=intent.volume, price=None,
                created_date=day))
