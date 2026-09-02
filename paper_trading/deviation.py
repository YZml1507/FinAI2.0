#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T402 偏差指标计算 —— 回测与模拟盘结果的量化比对。

口径声明（⛔ 改口径必改文档 + 测试同步红）：

| 指标 | 口径 | 说明 |
|---|---|---|
| ``nav_deviation`` | ``abs(nav_paper − nav_backtest) / nav_backtest`` | NAV 日偏差（百分比，绝对值） |
| ``return_deviation`` | ``abs(return_paper − return_backtest)`` | 累计收益偏差（差值，非比例）|
| ``turnover_deviation`` | ``abs(turnover_paper − turnover_backtest)`` | 换手偏差（差值，单位百分点 pp） |
| ``price_deviation`` | ``abs(price_paper − price_backtest) / price_backtest`` | 单笔成交价偏差（百分比） |
| ``slippage_deviation`` | ``slippage_realized − slippage_model`` | 单笔滑点偏差（已实现 − 模型估算，可正可负） |

红线：
  · ⛔ 纯函数、零 IO、零 pandas；
  · 金额全 Decimal（输出 6 位小数 ROUND_HALF_UP）；
  · 分母为 0 / NAV 为空 ⇒ raise DeviationError（fail-closed）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping, Sequence

__all__ = [
    "DeviationError",
    "DeviationMetrics",
    "compute_deviation_metrics",
]

_QUANT_6 = Decimal("0.000001")  # 指标输出统一 6 位小数
_ZERO = Decimal("0")


class DeviationError(RuntimeError):
    """偏差计算的契约违约（空净值曲线 / 分母为 0 / 数据缺失等）。"""


def _q6(value: float | Decimal) -> Decimal:
    """float → Decimal（str 中转断二进制尾巴）→ 6 位 ROUND_HALF_UP。"""
    return Decimal(str(value)).quantize(_QUANT_6, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class DeviationMetrics:
    """一组偏差指标（纯数据，⛔ 无方法）。"""

    # —— NAV 对齐偏差（按日） ——
    date: _date                                 # 对齐日期
    nav_backtest: Decimal
    nav_paper: Decimal
    nav_deviation: Decimal                      # abs((nav_paper − nav_backtest) / nav_backtest)

    # —— 收益偏差（累计） ——
    return_backtest: Decimal
    return_paper: Decimal
    return_deviation: Decimal                   # abs(return_paper − return_backtest)

    # —— 换手偏差（按区间，单位百分点 pp） ——
    turnover_backtest: Decimal | None           # 可能为 None（无成交）
    turnover_paper: Decimal | None
    turnover_deviation: Decimal | None          # 有一方为 None ⇒ 本字段也 None

    # —— 成交价 & 滑点偏差（单笔或批次均值） ——
    price_deviations: list[Decimal]             # 各笔成交价偏差（百分比）
    slippage_deviations: list[Decimal]          # 各笔滑点偏差（已实现 − 模型）


def compute_deviation_metrics(
    *,
    date: _date,
    nav_backtest: Decimal,
    nav_paper: Decimal,
    return_backtest: Decimal,
    return_paper: Decimal,
    turnover_backtest: Decimal | None = None,
    turnover_paper: Decimal | None = None,
    price_pairs: Sequence[tuple[Decimal, Decimal]] = (),
    slippage_pairs: Sequence[tuple[Decimal, Decimal]] = (),
) -> DeviationMetrics:
    """计算偏差指标（纯函数，⛔ 零副作用）。

    Args:
        date: 对齐日期。
        nav_backtest: 回测净值（必须 > 0）。
        nav_paper: 模拟盘净值（必须 > 0）。
        return_backtest: 回测累计收益率（如 0.15 = 15%）。
        return_paper: 模拟盘累计收益率。
        turnover_backtest: 回测换手率（可选，⛔ 已年化口径：如 3.2 = 320%）。
        turnover_paper: 模拟盘换手率（可选）。
        price_pairs: 成交价配对 ``[(backtest_price, paper_price), ...]``（逐笔）。
        slippage_pairs: 滑点配对 ``[(model_slippage, realized_slippage), ...]``（逐笔）。

    Returns:
        :class:`DeviationMetrics`。

    Raises:
        DeviationError: NAV ≤ 0 / 分母为 0 / 数据缺失等契约违约。
    """
    # —— NAV 偏差 ——
    if nav_backtest <= 0:
        raise DeviationError(f"nav_backtest={nav_backtest} ≤ 0（必须为正）")
    if nav_paper <= 0:
        raise DeviationError(f"nav_paper={nav_paper} ≤ 0（必须为正）")
    nav_deviation = _q6(abs((nav_paper - nav_backtest) / nav_backtest))

    # —— 收益偏差（差值，非比例） ——
    return_deviation = _q6(abs(return_paper - return_backtest))

    # —— 换手偏差（有一方为 None ⇒ 整体 None） ——
    if turnover_backtest is not None and turnover_paper is not None:
        turnover_deviation = _q6(abs(turnover_paper - turnover_backtest))
    else:
        turnover_deviation = None

    # —— 成交价偏差（逐笔百分比） ——
    price_deviations: list[Decimal] = []
    for bt_price, pp_price in price_pairs:
        if bt_price <= 0:
            raise DeviationError(f"回测成交价 {bt_price} ≤ 0")
        if pp_price <= 0:
            raise DeviationError(f"模拟盘成交价 {pp_price} ≤ 0")
        dev = _q6(abs((pp_price - bt_price) / bt_price))
        price_deviations.append(dev)

    # —— 滑点偏差（已实现 − 模型，可正可负） ——
    slippage_deviations: list[Decimal] = []
    for model_slip, realized_slip in slippage_pairs:
        dev = _q6(realized_slip - model_slip)
        slippage_deviations.append(dev)

    return DeviationMetrics(
        date=date,
        nav_backtest=nav_backtest,
        nav_paper=nav_paper,
        nav_deviation=nav_deviation,
        return_backtest=return_backtest,
        return_paper=return_paper,
        return_deviation=return_deviation,
        turnover_backtest=turnover_backtest,
        turnover_paper=turnover_paper,
        turnover_deviation=turnover_deviation,
        price_deviations=price_deviations,
        slippage_deviations=slippage_deviations,
    )
