#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T403 对账模块 —— 模拟盘账本 vs 预期持仓/资金（FR-ACC-2）。

核心职责：日终对账，验证账本内部一致性 + 净值计算正确性。模拟盘对账与实盘对账
共享同一套逻辑（SDD-2 双账本 + 对账四态：匹配/长款/短款/不一致）。

v1 简化路径（模拟盘无券商回报）：
  - 只做账本**自对账**：持仓市值 + 现金 = NAV（账本内部一致性）
  - 检查项：持仓非负 / 现金非负 / 冻结资金 <= 现金 / NAV 单调性（除分红）
  - 实盘时扩展：券商三报告（成交/持仓/资金）→ 匹配 → 差异进人工确认通道

红线：
  ① 对账差异 > 0.01 元（1 分）→ raise ReconciliationError（fail-closed）
  ② 持仓/现金/冻结资金违反不变式 → raise ReconciliationError
  ③ NAV 异常下跌（>5% 且非分红日）→ 告警但不 raise（人工介入裁决）

"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from decimal import Decimal
from typing import Mapping

from backtest.ledger import BookView
from backtest.types import Bar

__all__ = [
    "ReconciliationError",
    "ReconciliationReport",
    "reconcile_account",
]

_ZERO = Decimal("0")
_TOLERANCE = Decimal("0.01")  # 1 分容差
_NAV_DROP_THRESHOLD = Decimal("0.05")  # 5% NAV 异常下跌告警阈值


class ReconciliationError(RuntimeError):
    """对账失败（差异超容差 / 违反不变式）。"""


@dataclass
class ReconciliationReport:
    """对账报告（FR-ACC-2）。"""

    date: _date
    ok: bool                                # 是否通过（差异 <= 容差 且无不变式违反）
    cash: Decimal                           # 账本现金
    frozen_cash: Decimal                    # 冻结资金（T+1 未结算）
    positions_market_value: Decimal         # 持仓市值（按最新价）
    nav: Decimal                            # 净值（现金 + 市值）
    nav_from_book: Decimal                  # 账本推导净值（交叉验证）

    # 检查项清单（True = 通过）
    checks: dict[str, bool] = field(default_factory=dict)

    # 差异明细（空 = 无差异）
    discrepancies: list[str] = field(default_factory=list)

    # 告警（非致命，需人工复查）
    warnings: list[str] = field(default_factory=list)

    # 前一日 NAV（用于单调性检查，None = 首日）
    prev_nav: Decimal | None = None
    nav_change_pct: Decimal | None = None   # NAV 变化百分比


def reconcile_account(
    book: BookView,
    date: _date,
    bars: Mapping[str, Bar],
    *,
    prev_nav: Decimal | None = None,
) -> ReconciliationReport:
    """日终对账（账本自对账 + 净值计算验证）。

    Args:
        book: 当日结算后的账本视图（已调用 settle_day）。
        date: 对账日期。
        bars: 当日行情（用于计算持仓市值；停牌标的用 book.positions[sym].last_close）。
        prev_nav: 前一日 NAV（用于单调性检查，None = 首日）。

    Returns:
        ReconciliationReport（ok=False 时 checks/discrepancies 记录失败原因）。

    Raises:
        ReconciliationError: 致命违反不变式（持仓负数/现金负数/差异超容差）。
    """
    report = ReconciliationReport(
        date=date,
        ok=True,
        cash=book.cash,
        frozen_cash=book.frozen_cash,
        positions_market_value=_ZERO,
        nav=_ZERO,
        nav_from_book=book.nav,
        prev_nav=prev_nav,
    )

    # ===== 1. 不变式检查（致命，直接 raise）=====
    if book.cash < _ZERO:
        raise ReconciliationError(
            f"{date} 现金为负 {book.cash}（爆仓检测，不可容忍）")

    if book.frozen_cash < _ZERO:
        raise ReconciliationError(
            f"{date} 冻结资金为负 {book.frozen_cash}（账本不一致）")

    if book.frozen_cash > book.cash:
        raise ReconciliationError(
            f"{date} 冻结资金 {book.frozen_cash} > 现金 {book.cash}（账本不一致）")

    for symbol, pos in book.positions.items():
        if pos.volume < 0:
            raise ReconciliationError(
                f"{date} {symbol} 持仓为负 {pos.volume}（v1 不支持空头）")

    report.checks["cash_nonnegative"] = True
    report.checks["frozen_valid"] = True
    report.checks["positions_nonnegative"] = True

    # ===== 2. 持仓市值重算（与账本交叉验证）=====
    market_value_recomputed = _ZERO
    for symbol, pos in book.positions.items():
        # 优先用 bars 当日 close，停牌则用 book.positions[sym].last_close
        bar = bars.get(symbol)
        if bar is not None:
            price = bar.close
        else:
            price = pos.last_close
            report.warnings.append(
                f"{symbol} 停牌，使用账本 last_close={price}")

        value = price * Decimal(pos.volume)
        market_value_recomputed += value

    report.positions_market_value = market_value_recomputed

    # ===== 3. NAV 计算与交叉验证 =====
    nav_computed = book.cash + market_value_recomputed
    report.nav = nav_computed

    diff = abs(nav_computed - book.nav)
    if diff > _TOLERANCE:
        report.ok = False
        report.checks["nav_match"] = False
        report.discrepancies.append(
            f"NAV 差异 {diff}（计算值={nav_computed} vs 账本={book.nav}）"
            f"> 容差 {_TOLERANCE}")
        raise ReconciliationError(
            f"{date} NAV 差异 {diff} 超容差（计算={nav_computed} / 账本={book.nav}）")

    report.checks["nav_match"] = True

    # ===== 4. NAV 单调性检查（非致命告警）=====
    if prev_nav is not None and prev_nav > _ZERO:
        change_pct = (nav_computed - prev_nav) / prev_nav
        report.nav_change_pct = change_pct

        if change_pct < -_NAV_DROP_THRESHOLD:
            report.warnings.append(
                f"NAV 下跌 {change_pct:.2%} 超阈值 {_NAV_DROP_THRESHOLD:.0%}"
                f"（{prev_nav} → {nav_computed}），需人工复查是否正常")

    return report
