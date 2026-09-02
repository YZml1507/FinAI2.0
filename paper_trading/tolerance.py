#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T402 容忍带配置 —— 回测与模拟盘偏差的可接受阈值。

容忍带设定依据（来自 T204 敏感度分析）：

| 指标 | 容忍带 | 依据 |
|---|---|---|
| NAV 日偏差 | 0.5% | 单档滑点（10bps）→ 终值约 0.1%；累计+撮合微差允许 5×单档 |
| 月度收益偏差 | 2% | 15bps 滑点档在 T204 场景下终值影响 −0.16%；保守×12 留 2% 月度空间 |
| 月换手偏差 | 10pp | 动量策略换手 ≈300%~1300%（T304 压力报告）；偏离 10 个百分点（非比例）视为可疑 |
| 单笔成交价偏差 | 1% | 1%=100bps，远大于滑点（5bps），足够吸收 tick 取整+深度微差 |
| 单笔滑点偏差 | 0.5% | 模型滑点 5bps；实际滑点上下波动 ±50bps（10×模型）为正常流动性微差 |

变更纪律：
  ① 修改容忍带必须在 docs/t402_deviation_tolerance.md 登记依据；
  ② 新增偏差指标必须补对应容忍带与单测；
  ③ 超出容忍带 → 必告警（fail-closed），不许静默记录。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

__all__ = [
    "ToleranceBand",
    "DEFAULT_TOLERANCE_BANDS",
]

_D = Decimal  # 简写


@dataclass(frozen=True)
class ToleranceBand:
    """单项偏差的容忍带（阈值 + 说明）。"""

    name: str                           # 指标名（如 "nav_daily"）
    threshold: Decimal                  # 容忍阈值（绝对值或百分比，按指标口径解释）
    unit: str                           # 单位说明（如 "%"）
    description: str                    # 设定依据简述


#: 默认容忍带配置（全局唯一登记点，⛔ 不要在代码里写魔法数字）。
DEFAULT_TOLERANCE_BANDS: dict[str, ToleranceBand] = {
    "nav_daily": ToleranceBand(
        name="nav_daily",
        threshold=_D("0.005"),
        unit="%",
        description="NAV 日偏差 ≤0.5%（单档滑点终值影响 ≈0.1%，累计+撮合微差允许 5×）",
    ),
    "return_monthly": ToleranceBand(
        name="return_monthly",
        threshold=_D("0.02"),
        unit="%",
        description="月度收益偏差 ≤2%（15bps 滑点 T204 场景 −0.16%，保守×12）",
    ),
    "turnover_monthly": ToleranceBand(
        name="turnover_monthly",
        threshold=_D("0.10"),
        unit="pp",
        description="月换手偏差 ≤10pp（T304 动量换手 300%~1300%，偏离 10 个百分点为可疑）",
    ),
    "price_per_trade": ToleranceBand(
        name="price_per_trade",
        threshold=_D("0.01"),
        unit="%",
        description="单笔成交价偏差 ≤1%（100bps 远大于滑点 5bps，吸收 tick+深度微差）",
    ),
    "slippage_per_trade": ToleranceBand(
        name="slippage_per_trade",
        threshold=_D("0.005"),
        unit="%",
        description="单笔滑点偏差 ≤0.5%（模型 5bps，实际 ±50bps 为正常流动性微差）",
    ),
}


def get_tolerance_band(name: str) -> ToleranceBand:
    """取容忍带配置（name 不存在 ⇒ raise，fail-closed）。"""
    if name not in DEFAULT_TOLERANCE_BANDS:
        raise KeyError(f"未定义的容忍带指标：{name!r}（需先登记到 DEFAULT_TOLERANCE_BANDS）")
    return DEFAULT_TOLERANCE_BANDS[name]
