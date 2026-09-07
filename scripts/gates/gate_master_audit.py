#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Gate Master Audit: 六维门禁体系统一调度器与独立审计命令行入口

支持独立跑测、一键全门禁审计、导出 JSON 报告与 Fail-Closed 阻断。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any, Sequence

from .base import (
    BaseGate,
    GateBlockerError,
    GateCategory,
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


class GateMasterAudit:
    """六维门禁总调度器"""

    def __init__(self, gates: Sequence[BaseGate] | None = None) -> None:
        if gates is not None:
            self.gates = list(gates)
        else:
            self.gates = self.get_standard_gates()

    @classmethod
    def get_standard_gates(cls) -> list[BaseGate]:
        """获取标准 18 道门禁全家桶"""
        return [
            # D-Gate
            RawPriceJumpGate(),
            FloatMarketCapGate(),
            PitDividendYieldGate(),
            SuspensionVolumeGate(),
            HighPriceLotGate(),
            # L-Gate
            FeatureLivenessGate(),
            AllocationFidelityGate(),
            StaticAstCallGate(),
            # E-Gate
            MustFailCasesGate(),
            BonusSplitFifoGate(),
            SlippagePriceCapGate(),
            # A-Gate
            FeeSumBalanceGate(),
            DailyCashConserveGate(),
            GoldenRoundtripGate(),
            SegmentRateScheduleGate(),
            # S-Gate
            TurnoverCeilingGate(),
            TimingExitSurvivalGate(),
            DynamicSlippageAdvGate(),
            DividendTaxLockGate(),
            AttributionEvidenceGate(),
            # G-Gate
            ProvenanceTriadGate(),
            TasksSignGate(),
            MasterFindingGate(),
        ]

    def audit(self, context: Any = None, strict: bool = False) -> list[GateResult]:
        """执行所有注册门禁审计"""
        results: list[GateResult] = []
        blockers: list[GateResult] = []

        for gate in self.gates:
            try:
                res = gate.evaluate(context)
            except Exception as e:
                res = GateResult(
                    gate_id=gate.gate_id,
                    name=gate.name,
                    category=gate.category,
                    status=GateStatus.FAIL,
                    severity=gate.severity,
                    message=f"门禁执行抛出未捕获异常: {e}",
                    threshold=gate.threshold_desc,
                    evidence=gate.evidence,
                )

            results.append(res)
            if res.status == GateStatus.FAIL and res.severity in (GateSeverity.BLOCKER, GateSeverity.CRITICAL):
                blockers.append(res)

        if strict and blockers:
            first = blockers[0]
            raise GateBlockerError(first.gate_id, first.message, first.metrics)

        return results

    def print_summary(self, results: list[GateResult]) -> None:
        """打印终端格式化摘要"""
        print("=" * 80)
        print("                FinAI2.0 六维质量防伪门禁体系 (D-L-E-A-S-G) 审计报告")
        print("=" * 80)
        print(f"{'ID':<6} | {'分类':<18} | {'状态':<7} | {'级别':<8} | {'门禁名称'}")
        print("-" * 80)

        pass_count = 0
        fail_count = 0
        skip_count = 0

        for r in results:
            cat_short = r.category.value.split(" ")[0]
            status_str = r.status.value
            if r.status == GateStatus.PASS:
                pass_count += 1
            elif r.status == GateStatus.FAIL:
                fail_count += 1
            else:
                skip_count += 1

            print(f"{r.gate_id:<6} | {cat_short:<18} | {status_str:<7} | {r.severity.value:<8} | {r.name}")
            if r.status == GateStatus.FAIL:
                print(f"       -> [FAIL 详情] {r.message}")

        print("=" * 80)
        total = len(results)
        print(f"总览: 共 {total} 项门禁 | PASS: {pass_count} | FAIL: {fail_count} | SKIP: {skip_count}")
        if fail_count > 0:
            print("[警告] 检出未通过门禁！请修复相关缺陷后再行推进！")
        else:
            print("[全绿] 所有门禁检验通过！符合散户客观物理约束与反欺诈防伪标准！")
        print("=" * 80)

    def generate_json_report(self, results: list[GateResult], output_path: str | None = None) -> dict[str, Any]:
        """导出 JSON 格式审计报告"""
        report = {
            "title": "FinAI2.0 Gate Master Audit Report",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "summary": {
                "total": len(results),
                "pass": sum(1 for r in results if r.status == GateStatus.PASS),
                "fail": sum(1 for r in results if r.status == GateStatus.FAIL),
                "skip": sum(1 for r in results if r.status == GateStatus.SKIP),
            },
            "results": [r.to_dict() for r in results],
        }
        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description="FinAI2.0 六维门禁总调度器")
    parser.add_argument("--strict", action="store_true", help="阻断模式：一旦失败立即非零退出")
    parser.add_argument("--report", type=str, default="", help="输出 JSON 审计报告路径")
    parser.add_argument("--category", type=str, default="", help="仅运行指定分类，如 D, L, E, A, S, G")
    args = parser.parse_args()

    master = GateMasterAudit()
    if args.category:
        cat_key = f"{args.category.upper()}-Gate"
        master.gates = [g for g in master.gates if cat_key in g.category.value]

    # 默认针对系统母库与当前环境做无参自检
    results = master.audit(context={}, strict=False)
    master.print_summary(results)

    if args.report:
        master.generate_json_report(results, args.report)
        print(f"报告已保存至: {args.report}")

    if args.strict and any(r.status == GateStatus.FAIL for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
