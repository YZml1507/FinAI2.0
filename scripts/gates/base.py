#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""六维质量门禁基础协议与数据结构（Base definitions for D-L-E-A-S-G Gate Suite）

依据：《17_中低频量化研发防伪与工程质量门禁体系深度调研报告》
定位：不可绕过、机读化、Fail-Closed 的量化研发质量防伪门禁体系基底。
"""

from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class GateStatus(str, Enum):
    """门禁评定状态"""
    PASS = "PASS"
    FAIL = "FAIL"
    WARNING = "WARNING"
    SKIP = "SKIP"


class GateSeverity(str, Enum):
    """门禁严苛度与阻断级别"""
    BLOCKER = "BLOCKER"   # 阻断性硬门禁：一旦失败立即抛出异常终止流程
    CRITICAL = "CRITICAL" # 核心门禁：阻断回测落盘与阶段准入
    WARNING = "WARNING"   # 警告门禁：标记风险，需人工确认
    INFO = "INFO"         # 提示信息：仅记录指标


class GateCategory(str, Enum):
    """六维防御分类"""
    D_GATE = "D-Gate (数据真值与反未来)"
    L_GATE = "L-Gate (调用存活与参数落地)"
    E_GATE = "E-Gate (撮合保真与极端事件)"
    A_GATE = "A-Gate (双账本分厘级会计对账)"
    S_GATE = "S-Gate (散户小资金科学防伪与择时生存)"
    G_GATE = "G-Gate (工程物理留痕与交付门禁)"


class GateError(Exception):
    """门禁检验通用基类异常"""
    pass


class GateBlockerError(GateError):
    """阻断级门禁未通过异常（Fail-Closed 终止信号）"""
    def __init__(self, gate_id: str, message: str, metrics: dict[str, Any] | None = None) -> None:
        super().__init__(f"[{gate_id}] 门禁阻断: {message} | 指标: {metrics or {}}")
        self.gate_id = gate_id
        self.metrics = metrics or {}


@dataclass
class GateResult:
    """门禁检验结果记录"""
    gate_id: str
    name: str
    category: GateCategory
    status: GateStatus
    severity: GateSeverity
    message: str
    metrics: dict[str, Any] = field(default_factory=dict)
    threshold: str = ""
    evidence: str = ""
    timestamp: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())

    @property
    def is_pass(self) -> bool:
        return self.status in (GateStatus.PASS, GateStatus.SKIP)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "name": self.name,
            "category": self.category.value,
            "status": self.status.value,
            "severity": self.severity.value,
            "message": self.message,
            "metrics": self.metrics,
            "threshold": self.threshold,
            "evidence": self.evidence,
            "timestamp": self.timestamp,
        }


class BaseGate(ABC):
    """门禁抽象基类"""
    gate_id: str
    name: str
    category: GateCategory
    severity: GateSeverity = GateSeverity.CRITICAL
    evidence: str = ""
    threshold_desc: str = ""

    @abstractmethod
    def evaluate(self, context: Any = None) -> GateResult:
        """评估门禁条件并返回 GateResult"""
        pass
