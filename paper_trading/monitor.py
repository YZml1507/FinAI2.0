#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T402 偏差监控器 —— 对比回测与模拟盘结果，判定是否超出容忍带。

监控频率：
  · 日频：NAV 偏差（每个交易日对齐）
  · 周频：累计收益偏差（每周五或最后一个交易日对齐）
  · 月频：换手偏差（每月末交易日对齐）

输出：
  · DeviationReport（偏差值 + 超出标记 + 根因提示）
  · 超出容忍带 → 立即返回告警标记（调用方负责推送飞书）

红线：
  · ⛔ 监控器不做 IO（不推送告警、不记文件）—— 职责单一，只判定；
  · 超出判定必须基于 tolerance.py 的显式配置，⛔ 不许魔法数字；
  · 根因提示只做规则匹配（如"NAV 偏差>0.5% 且换手偏差大"），不做 AI 推理。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from decimal import Decimal

from paper_trading.deviation import DeviationMetrics
from paper_trading.tolerance import DEFAULT_TOLERANCE_BANDS, ToleranceBand

__all__ = [
    "DeviationReport",
    "DeviationMonitor",
]

_ZERO = Decimal("0")


@dataclass(frozen=True)
class DeviationReport:
    """偏差监控报告（包含偏差值 + 超出标记 + 根因提示）。"""

    date: _date
    metrics: DeviationMetrics

    # —— 超出标记（⛔ 超出即告警，fail-closed） ——
    nav_exceeded: bool
    return_exceeded: bool
    turnover_exceeded: bool
    price_exceeded: bool                        # 任一笔成交价超出 ⇒ True
    slippage_exceeded: bool                     # 任一笔滑点超出 ⇒ True

    # —— 根因提示（规则匹配，⛔ 不做 AI 推理） ——
    root_cause_hints: list[str] = field(default_factory=list)

    @property
    def any_exceeded(self) -> bool:
        """是否有任何指标超出容忍带。"""
        return (
            self.nav_exceeded
            or self.return_exceeded
            or self.turnover_exceeded
            or self.price_exceeded
            or self.slippage_exceeded
        )


class DeviationMonitor:
    """偏差监控器（无状态，纯函数组合器）。"""

    def __init__(
        self,
        *,
        tolerance_bands: dict[str, ToleranceBand] | None = None,
    ) -> None:
        """
        Args:
            tolerance_bands: 容忍带配置字典（默认用 DEFAULT_TOLERANCE_BANDS）。
        """
        self.tolerance_bands = tolerance_bands or DEFAULT_TOLERANCE_BANDS

    def check(self, metrics: DeviationMetrics) -> DeviationReport:
        """检查偏差指标是否超出容忍带（⛔ 纯函数，零副作用）。

        Args:
            metrics: 偏差指标（来自 compute_deviation_metrics）。

        Returns:
            :class:`DeviationReport`（包含超出标记 + 根因提示）。
        """
        nav_band = self.tolerance_bands["nav_daily"]
        return_band = self.tolerance_bands["return_monthly"]
        turnover_band = self.tolerance_bands["turnover_monthly"]
        price_band = self.tolerance_bands["price_per_trade"]
        slippage_band = self.tolerance_bands["slippage_per_trade"]

        # —— 超出判定 ——
        nav_exceeded = metrics.nav_deviation > nav_band.threshold
        return_exceeded = metrics.return_deviation > return_band.threshold

        # 换手：有一方为 None ⇒ 不判（无数据不算超出，fail-soft）
        turnover_exceeded = False
        if metrics.turnover_deviation is not None:
            turnover_exceeded = metrics.turnover_deviation > turnover_band.threshold

        # 成交价：任一笔超出 ⇒ True
        price_exceeded = any(
            dev > price_band.threshold for dev in metrics.price_deviations
        )

        # 滑点：任一笔绝对值超出 ⇒ True（偏差可正可负，取绝对值判）
        slippage_exceeded = any(
            abs(dev) > slippage_band.threshold for dev in metrics.slippage_deviations
        )

        # —— 根因提示（规则匹配） ——
        hints: list[str] = []
        if nav_exceeded:
            hints.append(
                f"NAV 偏差 {metrics.nav_deviation:.2%} 超出容忍带 "
                f"{nav_band.threshold:.2%}（{nav_band.description}）"
            )
        if return_exceeded:
            hints.append(
                f"累计收益偏差 {metrics.return_deviation:.2%} 超出容忍带 "
                f"{return_band.threshold:.2%}（{return_band.description}）"
            )
        if turnover_exceeded:
            hints.append(
                f"换手偏差 {metrics.turnover_deviation:.2%} 超出容忍带 "
                f"{turnover_band.threshold:.2%}（{turnover_band.description}）"
            )
        if price_exceeded:
            max_price_dev = max(metrics.price_deviations, default=_ZERO)
            hints.append(
                f"成交价偏差最大 {max_price_dev:.2%} 超出容忍带 "
                f"{price_band.threshold:.2%}（{price_band.description}）"
            )
        if slippage_exceeded:
            max_slip_dev = max((abs(d) for d in metrics.slippage_deviations), default=_ZERO)
            hints.append(
                f"滑点偏差最大 {max_slip_dev:.2%} 超出容忍带 "
                f"{slippage_band.threshold:.2%}（{slippage_band.description}）"
            )

        # 联合诊断（多指标同时超出 → 系统性问题提示）
        if nav_exceeded and turnover_exceeded:
            hints.append(
                "⚠️ NAV 与换手同时超出 → 可疑：成交执行路径可能与回测假设偏离"
                "（实盘冲击成本 > 模型估算 或 拒单率 > 预期）"
            )
        if price_exceeded and slippage_exceeded:
            hints.append(
                "⚠️ 成交价与滑点同时超出 → 可疑：市场流动性可能低于回测假设"
                "（盘口深度不足 或 波动率异常）"
            )

        return DeviationReport(
            date=metrics.date,
            metrics=metrics,
            nav_exceeded=nav_exceeded,
            return_exceeded=return_exceeded,
            turnover_exceeded=turnover_exceeded,
            price_exceeded=price_exceeded,
            slippage_exceeded=slippage_exceeded,
            root_cause_hints=hints,
        )
