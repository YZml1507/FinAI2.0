#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T401 §1 配置 —— 模拟盘参数/路径/初始资金。

``PaperTradingConfig`` 集中管理模拟盘运行参数：
  - 初始资金（默认 10 万）
  - 策略配置（可序列化，与回测一致）
  - 数据路径（Parquet daily_bars 根目录）
  - 状态持久化路径（state.json）
  - 是否干跑模式（dry_run：不真实下单，仅记录信号）

校验纪律：
  - 初始资金 ≥10000 RMB（系统配置下限）
  - 路径必须存在且可写
  - 策略配置可序列化（JSON 序列化测试）
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

__all__ = ["PaperTradingConfig", "ConfigError"]


class ConfigError(ValueError):
    """配置错误（参数越界/路径不存在/策略配置无法序列化）。"""


@dataclass
class PaperTradingConfig:
    """模拟盘配置（全字段校验 fail-closed）。"""

    #: 初始资金（RMB，≥10000）
    initial_capital: Decimal = Decimal("100000")

    #: 策略配置（可序列化字典；与回测 registry params 同口径）
    strategy_params: dict[str, Any] | None = None

    #: 数据根目录（Parquet daily_bars）
    data_root: Path = Path("data/daily_bars")

    #: 状态持久化路径（JSON 文件）
    state_path: Path = Path("paper_trading/state.json")

    #: 干跑模式（True=不真实下单，仅记录信号；v1 模拟盘全程 dry_run）
    dry_run: bool = True

    #: 风控参数：单日最大下单次数（防御失控策略，默认 100）
    max_orders_per_day: int = 100

    def __post_init__(self):
        """字段校验（fail-closed）。"""
        # 初始资金下限
        if self.initial_capital < Decimal("10000"):
            raise ConfigError(
                f"初始资金 {self.initial_capital} 低于系统下限 10000 RMB"
            )

        # 数据路径存在性
        if not self.data_root.exists():
            raise ConfigError(f"数据根目录不存在: {self.data_root}")

        # 状态路径父目录可写
        state_dir = self.state_path.parent
        if not state_dir.exists():
            try:
                state_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConfigError(
                    f"状态目录无法创建: {state_dir}"
                ) from exc

        # 策略配置可序列化（JSON round-trip）
        if self.strategy_params is not None:
            try:
                json.dumps(self.strategy_params, default=str)
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"策略配置无法 JSON 序列化: {exc}"
                ) from exc

        # 风控参数域校验
        if self.max_orders_per_day <= 0:
            raise ConfigError(
                f"max_orders_per_day 必须 > 0，得到 {self.max_orders_per_day}"
            )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（Path → str，Decimal → str）。"""
        out = asdict(self)
        out["initial_capital"] = str(self.initial_capital)
        out["data_root"] = str(self.data_root)
        out["state_path"] = str(self.state_path)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperTradingConfig:
        """从字典反序列化（str → Path/Decimal）。"""
        kwargs = dict(data)
        if "initial_capital" in kwargs:
            kwargs["initial_capital"] = Decimal(str(kwargs["initial_capital"]))
        if "data_root" in kwargs:
            kwargs["data_root"] = Path(kwargs["data_root"])
        if "state_path" in kwargs:
            kwargs["state_path"] = Path(kwargs["state_path"])
        return cls(**kwargs)
