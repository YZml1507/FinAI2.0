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
    is_blocking_result,
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
    AntiTamperSignatureGate,
    MasterFindingGate,
    ProvenanceTriadGate,
    TasksSignGate,
)
from .gate_consistency import (
    DocMetricConsistencyGate,
    DocPathReferenceGate,
    MaxDrawdownCeilingGate,
    StressValidityGate,
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
from .gate_master_audit import GateMasterAudit
from .must_fail_probe import run_must_fail_cases
from .runner import run_post_run_gates, run_pre_run_gates
from .tamper_guard import (
    check_mother_library_guard,
    compute_run_signature,
    sign_run_record,
    verify_run_signature,
    verify_tasks_markdown,
    verify_tasks_mirror,
)

__all__ = [
    "GateMasterAudit",
    "run_pre_run_gates",
    "run_post_run_gates",
    "run_must_fail_cases",
    # Base
    "GateStatus",
    "GateSeverity",
    "GateCategory",
    "GateError",
    "GateBlockerError",
    "GateResult",
    "BaseGate",
    "is_blocking_result",
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
    "AntiTamperSignatureGate",
    # P0 一致性门禁
    "MaxDrawdownCeilingGate",
    "DocMetricConsistencyGate",
    "StressValidityGate",
    "DocPathReferenceGate",
    # Tamper Guard utils
    "compute_run_signature",
    "sign_run_record",
    "verify_run_signature",
    "verify_tasks_markdown",
    "verify_tasks_mirror",
    "check_mother_library_guard",
]
