#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""六维质量防伪门禁体系独立工具包（FinAI2.0 D-L-E-A-S-G Gate Suite）

依据：《17_中低频量化研发防伪与工程质量门禁体系深度调研报告》
"""

from .base import (
    BaseGate,
    GateBlockerError,
    GateCategory,
    GateError,
    GateResult,
    GateSeverity,
    GateStatus,
)
from .gate_a_accounting import (
    DailyCashConserveGate,
    FeeSumBalanceGate,
    GoldenRoundtripGate,
    SegmentRateScheduleGate,
)
from .gate_d_data import (
    FloatMarketCapGate,
    HighPriceLotGate,
    PitDividendYieldGate,
    RawPriceJumpGate,
    SuspensionVolumeGate,
)
from .gate_e_engine import (
    BonusSplitFifoGate,
    MustFailCasesGate,
    SlippagePriceCapGate,
)
from .gate_g_governance import (
    MasterFindingGate,
    ProvenanceTriadGate,
    TasksSignGate,
)
from .gate_l_liveness import (
    AllocationFidelityGate,
    FeatureLivenessGate,
    StaticAstCallGate,
)
from .gate_s_scientific import (
    AttributionEvidenceGate,
    DividendTaxLockGate,
    DynamicSlippageAdvGate,
    TimingExitSurvivalGate,
    TurnoverCeilingGate,
)
from .runner import run_post_run_gates, run_pre_run_gates

__all__ = [
    "run_pre_run_gates",
    "run_post_run_gates",
    # Base
    "GateStatus",
    "GateSeverity",
    "GateCategory",
    "GateError",
    "GateBlockerError",
    "GateResult",
    "BaseGate",
    # D-Gate
    "RawPriceJumpGate",
    "FloatMarketCapGate",
    "PitDividendYieldGate",
    "SuspensionVolumeGate",
    "HighPriceLotGate",
    # L-Gate
    "FeatureLivenessGate",
    "AllocationFidelityGate",
    "StaticAstCallGate",
    # E-Gate
    "MustFailCasesGate",
    "BonusSplitFifoGate",
    "SlippagePriceCapGate",
    # A-Gate
    "FeeSumBalanceGate",
    "DailyCashConserveGate",
    "GoldenRoundtripGate",
    "SegmentRateScheduleGate",
    # S-Gate
    "TurnoverCeilingGate",
    "TimingExitSurvivalGate",
    "DynamicSlippageAdvGate",
    "DividendTaxLockGate",
    "AttributionEvidenceGate",
    # G-Gate
    "ProvenanceTriadGate",
    "TasksSignGate",
    "MasterFindingGate",
]
