#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T301 组合管理器 —— FR-PM-1/2/4（信号 → 目标仓位 → 订单意图，三段纯函数全链可测）。

本模块**只产下单意图**（``OrderIntent``），不撮合、不记账、不碰日期推进——
那些是 ``backtest/`` 各层的活。事件链：

    信号 dict（symbol→score，由策略层产）
       ── ``select_targets`` ──► 目标标的清单（前 N 名 / 空 = 择时空仓）
       ── ``plan_positions`` ──► 目标市值表（等权 + 流动性/单票下限过滤）
       ── ``diff_to_orders`` ──► OrderIntent 列表（与现持仓 diff）

v1 口径声明（⛔ 改动须改本节 + 同步 tests/test_t301_portfolio.py）：

* **等权分配**（加权/风险平价留后）：资金分配基数 = ``total_nav``；
* **择时空仓/减仓为一等公民**（FR-PM-2）：空信号 ⇒ 空目标 ⇒ 全部 SELL 减仓/清仓；
* **SELL 不受单票下限约束，也不做参与率截断**——退出权优先（防「卖不出」风险），
  退出端的冲击成本由 T204 价格模型的滑点吸收；流动性过滤（``min_daily_amount`` /
  ``max_participation_rate``）**只拦建仓**（FR-PM-4 配合单票下限）；
* **金额全 Decimal**（⛔ 无 float），输出市值/股数全程 ROUND 到整手（``lot_size``）；
* fail-closed：契约外输入全部 raise ``PortfolioError``，⛔ 不静默降级。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from backtest.constants import OrderSide

__all__ = [
    "PortfolioError",
    "PortfolioConfig",
    "OrderIntent",
    "RebalanceReport",
    "select_targets",
    "plan_positions",
    "diff_to_orders",
]

_ZERO = Decimal("0")


class PortfolioError(ValueError):
    """组合管理器契约违约（越界持仓数 / 非 Decimal 金额 / 非法配置等）。"""


@dataclass(frozen=True)
class PortfolioConfig:
    """组合约束（FR-PM-1/4；⛔ 默认即合规域，越界构造即拒）。

    ``target_count`` 合法域 [``min_positions``, ``max_positions``]（默认 3/5/8）；
    ``hard_limit`` 是任何时刻总持仓数硬顶（默认 10），**必须** ≥ ``max_positions``。
    ``max_price``：高价股过滤（默认 300.0 元，``None`` 禁用），防集中度风险与冲击成本放大。
    """

    target_count: int = 5
    min_positions: int = 3
    max_positions: int = 8
    hard_limit: int = 10
    min_position_value: Decimal = Decimal("20000")       # 单票建仓下限（元）
    lot_size: int = 100                                  # 整手
    min_daily_amount: Decimal = Decimal("50000000")      # 流动性下限（当日成交额 5000 万）
    max_participation_rate: Decimal = Decimal("0.05")    # 单笔买入 ≤ 当日成交额 5%
    max_price: Decimal | None = Decimal("300.0")         # 高价股上限（元，None=禁用）

    def __post_init__(self) -> None:
        ints = ("target_count", "min_positions", "max_positions",
                "hard_limit", "lot_size")
        for name in ints:
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool):
                raise PortfolioError(f"{name} 须为 int: {v!r}")
        for name in ("min_position_value", "min_daily_amount", "max_participation_rate"):
            v = getattr(self, name)
            if not isinstance(v, Decimal):
                raise PortfolioError(f"{name} 须为 Decimal（⛔ 禁 float）: {v!r}")
            if v <= _ZERO:
                raise PortfolioError(f"{name} 须 > 0: {v}")
        # max_price 可选（None=禁用），非 None 则须为正 Decimal
        if self.max_price is not None:
            if not isinstance(self.max_price, Decimal):
                raise PortfolioError(f"max_price 须为 Decimal 或 None（⛔ 禁 float）: {self.max_price!r}")
            if self.max_price <= _ZERO:
                raise PortfolioError(f"max_price 须 > 0: {self.max_price}")
        if not (self.min_positions <= self.target_count <= self.max_positions):
            raise PortfolioError(
                f"target_count={self.target_count} 须在 "
                f"[{self.min_positions}, {self.max_positions}] 内")
        if self.hard_limit < self.max_positions:
            raise PortfolioError(
                f"hard_limit={self.hard_limit} 不得小于 max_positions={self.max_positions}")
        if self.max_participation_rate > Decimal("1"):
            raise PortfolioError(
                f"max_participation_rate={self.max_participation_rate} 不得 > 1")
        if self.lot_size <= 0:
            raise PortfolioError(f"lot_size 须 > 0: {self.lot_size}")


@dataclass(frozen=True)
class OrderIntent:
    """下单意图（⛔ 不是 ``Order``——由调用方在 broker.submit 时构造）。

    ``volume``：BUY 必为整手倍数；SELL 可为现持仓原值（清仓路径允许零股）。
    """

    symbol: str
    side: OrderSide
    volume: int


@dataclass(frozen=True)
class RebalanceReport:
    """``diff_to_orders`` 的产出：意图 + 被丢弃标的的原因（fail-visible）。"""

    intents: tuple[OrderIntent, ...]
    dropped: tuple[tuple[str, str], ...]     # (symbol, 中文原因)


# ----------------------------------------------------------------------
# 三段纯函数
# ----------------------------------------------------------------------

def select_targets(
    signals: Mapping[str, Decimal], config: PortfolioConfig | None = None,
) -> list[str]:
    """信号 → 目标标的清单（FR-PM-2；按分数降序，平分按代码升序稳定）。

    空映射 ⇒ ``[]``（择时空仓）。取前 ``config.target_count`` 名。
    """
    cfg = config or PortfolioConfig()
    for symbol, score in signals.items():
        if not isinstance(score, Decimal):
            raise PortfolioError(f"信号分 {symbol}={score!r} 须为 Decimal")
    ranking = sorted(signals.items(), key=lambda kv: (-float(kv[1]), kv[0]))
    # 排序的 -float 仅用于相对比较（不参与任何资金计算）。
    return [symbol for symbol, _ in ranking[: cfg.target_count]]


def _plan_one(
    symbol: str, base: Decimal, bar: Any, cfg: PortfolioConfig,
) -> tuple[Decimal | None, str | None]:
    return_ = (None, None)
    if bar is None:                                   # 停牌 = 无 bar（feed 契约）
        return None, "停牌不可建仓"
    amount = getattr(bar, "amount", None)
    close = getattr(bar, "close", None)
    if not isinstance(amount, Decimal) or not isinstance(close, Decimal):
        raise PortfolioError(f"{symbol} 的 bar 缺 Decimal 字段 amount/close")
    # 高价股过滤（防集中度风险与冲击成本放大）
    if cfg.max_price is not None and close > cfg.max_price:
        return None, "超过价格上限"
    if amount < cfg.min_daily_amount:
        return None, "流动性不足"
    # 参与率封顶（防冲击成本，FR-PM-4）
    value = min(base, amount * cfg.max_participation_rate)
    # 整手化（买入不许零股）
    shares = int(value / close) // cfg.lot_size * cfg.lot_size
    planned = close * Decimal(shares)
    if planned < cfg.min_position_value:
        return None, "低于单票下限"
    return planned, None


def plan_positions(
    targets: Sequence[str],
    total_nav: Decimal,
    bars: Mapping[str, Any],
    config: PortfolioConfig | None = None,
    weights: Mapping[str, Decimal] | None = None,
) -> tuple[dict[str, Decimal], tuple[tuple[str, str], ...]]:
    """目标标的 → 目标市值（等权/加权 + 过滤），返回 ``(计划, dropped)``。

    如果提供 ``weights``，在 targets 内部归一化按权重分配目标资金；
    如果未提供，则等权分配（``total_nav / len(targets)``）。

    ⛔ ``len(targets) > config.hard_limit`` ⇒ raise ``PortfolioError``（越硬顶）。
    """
    cfg = config or PortfolioConfig()
    if not isinstance(total_nav, Decimal):
        raise PortfolioError(f"total_nav 须为 Decimal: {total_nav!r}")
    if total_nav <= _ZERO:
        raise PortfolioError(f"total_nav 须 > 0: {total_nav}")
    if len(targets) > cfg.hard_limit:
        raise PortfolioError(
            f"目标数 {len(targets)} 越过硬顶 hard_limit={cfg.hard_limit}")
    if not targets:
        return {}, ()

    # 权重计算：若传入有效 weights 则按比例分配，否则等权
    target_weights: dict[str, Decimal] = {}
    sum_w = _ZERO
    if weights is not None:
        target_weights = {s: Decimal(str(weights.get(s, _ZERO))) for s in targets}
        sum_w = sum(target_weights.values(), _ZERO)

    plan: dict[str, Decimal] = {}
    dropped: list[tuple[str, str]] = []
    for symbol in targets:
        if sum_w > _ZERO:
            base = (total_nav * target_weights[symbol]) / sum_w
        else:
            base = total_nav / Decimal(len(targets))
        value, reason = _plan_one(symbol, base, bars.get(symbol), cfg)
        if value is None:
            dropped.append((symbol, reason or ""))
        else:
            plan[symbol] = value
    return plan, tuple(dropped)


def diff_to_orders(
    current: Mapping[str, int],
    plan: Mapping[str, Decimal],
    bars: Mapping[str, Any],
    config: PortfolioConfig | None = None,
) -> RebalanceReport:
    """现持仓 vs 目标计划 → 下单意图列表（先卖后买排序，先释放资金）。

    * plan 外现持仓 ⇒ SELL 全清（择时退出；⛔ 不受单票下限约束，见模块 docstring）；
    * plan 内：目标股数 − 现股数 ⇒ 正 BUY / 负 SELL / 0 不动。
    """
    cfg = config or PortfolioConfig()
    intents: list[OrderIntent] = []

    for symbol, held in current.items():
        if not isinstance(held, int) or held < 0:
            raise PortfolioError(f"{symbol} 现持仓须为非负 int: {held!r}")

    # 先卖（释放资金口径）
    for symbol, held in current.items():
        if symbol not in plan and held > 0:
            intents.append(OrderIntent(symbol, OrderSide.SELL, held))

    # 后买/减仓
    dropped: list[tuple[str, str]] = []
    for symbol, target_value in plan.items():
        bar = bars.get(symbol)
        if bar is None:
            dropped.append((symbol, "停牌无法调仓"))
            continue
        close = getattr(bar, "close", None)
        if not isinstance(close, Decimal):
            raise PortfolioError(f"{symbol} 的 bar.close 须为 Decimal: {close!r}")
        target_shares = int(target_value / close) // cfg.lot_size * cfg.lot_size
        held = current.get(symbol, 0)
        delta = target_shares - held
        if delta > 0:
            intents.append(OrderIntent(symbol, OrderSide.BUY, delta))
        elif delta < 0:
            intents.append(OrderIntent(symbol, OrderSide.SELL, -delta))
    return RebalanceReport(intents=tuple(intents), dropped=tuple(dropped))
