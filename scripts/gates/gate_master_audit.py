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
    is_blocking_result,
)
from .context_builder import STATIC_GATE_IDS, WARN_GATE_IDS, build_repo_context, ci_policy
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
from .gate_repro import ReproducibilityGate
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
        """获取标准全量门禁全家桶（D-L-E-A-S-G 六维 + P0 一致性四道 + G-REPRO-1 = 29 道）"""
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
            AntiTamperSignatureGate(),
            # P0 一致性门禁（roadmap_decision.md §4）
            MaxDrawdownCeilingGate(),
            DocMetricConsistencyGate(),
            StressValidityGate(),
            DocPathReferenceGate(),
            # G-REPRO-1 复现一致性门禁（M2/PM-1 修复，见 docs/audit/repro_root_cause.md）
            ReproducibilityGate(),
        ]

    #: 推送期归属（㉓）：推送期即可真取证的门禁（静态 + 展示性 WARN 门禁）。
    #: 其余门禁归"回测后 + 定时全量 CI"。
    #: 单一事实源在 ``context_builder``：``STATIC_GATE_IDS`` ∪ ``WARN_GATE_IDS``（㉖）。
    #: ⚠ 三层分层（任务 1）：``G-MDD-1`` 归 WARN——**评估并展示**但 ``run_master_gate_guard`` 不阻断。
    PUSH_TIME_GATE_IDS: frozenset[str] = frozenset(set(STATIC_GATE_IDS) | set(WARN_GATE_IDS))

    @classmethod
    def get_push_time_gates(cls) -> list[BaseGate]:
        """仅返回推送期可判门禁（供 pre-push 使用）。"""
        return [g for g in cls.get_standard_gates() if g.gate_id in cls.PUSH_TIME_GATE_IDS]

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
            if is_blocking_result(res):      # FAIL 或 INCONCLUSIVE（应检未检）均阻断
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
        inconclusive_count = 0

        for r in results:
            cat_short = r.category.value.split(" ")[0]
            status_str = r.status.value
            if r.status == GateStatus.PASS:
                pass_count += 1
            elif r.status == GateStatus.FAIL:
                fail_count += 1
            elif r.status == GateStatus.INCONCLUSIVE:
                inconclusive_count += 1
            else:
                skip_count += 1

            print(f"{r.gate_id:<6} | {cat_short:<18} | {status_str:<13} | {r.severity.value:<8} | {r.name}")
            if r.status in (GateStatus.FAIL, GateStatus.INCONCLUSIVE):
                print(f"       -> [{r.status.value} 详情] {r.message}")

        print("=" * 80)
        total = len(results)
        print(
            f"总览: 共 {total} 项门禁 | PASS: {pass_count} | FAIL: {fail_count} | "
            f"SKIP: {skip_count} | INCONCLUSIVE: {inconclusive_count}"
        )
        # ⛔ 只有在 FAIL / SKIP / INCONCLUSIVE 均为 0 时才允许打印"全绿"（G-SKIP-1）。
        if fail_count > 0:
            print("[警告] 检出未通过门禁！请修复相关缺陷后再行推进！")
        elif skip_count > 0 or inconclusive_count > 0:
            print(
                f"[未全绿] 存在未检验项：SKIP {skip_count} 项（不适用）、"
                f"INCONCLUSIVE {inconclusive_count} 项（证据不足）；"
                "未检验项不得视为通过，请补齐证据后再行推进！"
            )
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
                "inconclusive": sum(1 for r in results if r.status == GateStatus.INCONCLUSIVE),
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
    parser.add_argument("--mdd", type=str, default="", help="仅对指定回测产物运行 G-MDD-1 回撤上限门禁")
    parser.add_argument("--acceptance", type=str, default="",
                        help="晋升/准入层判定（任务 1）：读产物 metrics，MDD>0.35/胜率<0.35/换手>4.0 判 FAIL（exit≠0）")
    parser.add_argument("--adopt", action="store_true",
                        help="配合 --acceptance：评估通过后调 adopt() 登记『已采纳产物』（晋升留证；⛔ 未通过则拒绝登记）")
    parser.add_argument("--ci", action="store_true",
                        help="CI 模式（㉖）：以仓库现状真实 ctx 运行全部 29 道门禁；"
                             "FAIL 或(静态门禁)INCONCLUSIVE 阻断；WARN 门禁与需 run 产物的 INCONCLUSIVE 只告警")
    parser.add_argument("--scheduled", action="store_true",
                        help="㊳ 定时全量审计模式：同 --ci，但**需 run 产物的 INCONCLUSIVE 也阻断**"
                             "（定时场景本就该拿到 run 产物；只告警会让 workflow 永不变红、等于没在判）")
    args = parser.parse_args()

    # 晋升/准入层（任务 1）：G-MDD-1 的 BLOCKER 能力归属地（推送/回测期均不阻断）。
    if args.acceptance:
        from .acceptance import main as _acceptance_main
        argv = ["--artifact", args.acceptance, "--adopt"] if args.adopt else ["--artifact", args.acceptance]
        sys.exit(_acceptance_main(argv))

    # CI 模式：真实 ctx（复用 context_builder，与 pre_push 同源），避免空 ctx 永久红
    if args.ci or args.scheduled:
        ctx, source = build_repo_context()
        print(f"[CI] 门禁取证来源: {source}（ctx 键 {len(ctx)} 个）")
        mode = "scheduled" if args.scheduled else "ci"
        print(f"[CI] 策略: {'--scheduled（需 run 产物的 INCONCLUSIVE 也阻断）' if args.scheduled else '--ci'}")
        results = GateMasterAudit().audit(context=ctx, strict=False)
        GateMasterAudit().print_summary(results)
        # ㊳ 定时全量审计：run-evidence 的 INCONCLUSIVE 亦阻断（否则该 workflow 永不变红）。
        blocking, blockers, warnings = ci_policy(results, run_evidence_blocks=args.scheduled)
        # 三层分层（任务 1）：WARN 门禁（如 G-MDD-1）**显式打印但不阻断**。
        warn_gates = [w for w in warnings if w.gate_id in WARN_GATE_IDS]
        for w in warn_gates:
            print(f"[WARN] {w.gate_id} {w.message}")
        other_warns = [w for w in warnings if w.gate_id not in WARN_GATE_IDS]
        if other_warns:
            print("=" * 80)
            if args.scheduled:
                # 定时场景不该有"只告警"的 run-evidence INCONCLUSIVE（run_evidence_blocks=True ⇒ 它们已成 blocker）。
                print(f"[CI][告警] {len(other_warns)} 道门禁未判 PASS（明细见上）：")
            else:
                print(f"[CI][告警] {len(other_warns)} 道需 run 产物的门禁因无证据判 INCONCLUSIVE（只告警不阻断）：")
            for w in other_warns:
                print(f"    [{w.gate_id}] {w.name}: {w.message[:90]}")
        print("=" * 80)
        if blockers:
            print(f"[CI][BLOCKED] 检出 {len(blockers)} 项阻断（{mode} 策略）：")
            for b in blockers:
                print(f"    [{b.gate_id}/{b.status.value}] {b.name}: {b.message[:90]}")
            sys.exit(1)
        print(f"[CI][PASS] 无阻断项（{mode} 策略，真实 ctx 下判定）。")
        return

    # G-MDD-1 单点复跑模式（验收可一条命令复现）
    if args.mdd:
        res = MaxDrawdownCeilingGate().evaluate({"artifact_path": args.mdd})
        print(f"[{res.status.value}] {res.gate_id} {res.name}")
        print(f"  message : {res.message}")
        print(f"  evidence: {res.evidence}")
        if res.metrics:
            print(f"  metrics : {json.dumps(res.metrics, ensure_ascii=False)}")
        # ⛔ INCONCLUSIVE（应检未检）同样阻断，退出码必须与展示一致
        if args.strict and is_blocking_result(res):
            sys.exit(1)
        return

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

    # ⛔ strict：FAIL 或 INCONCLUSIVE（应检未检）任一即非零退出（与 print_summary 的"未全绿"一致）
    if args.strict and any(is_blocking_result(r) for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
