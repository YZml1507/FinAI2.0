#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L-Gate: 调用存活与参数落地门禁（Liveness & Parameter Fidelity Gates）

依据：
1. 17 号深度调研报告 §4.2 / L-1 ~ L-3 门禁定义
2. 规避 Knight Capital 级死代码灾难（特性声明后底层被注释、绕过或等权抹平）
"""

from __future__ import annotations

import ast
import math
from decimal import Decimal
from typing import Any, Sequence

from .base import BaseGate, GateCategory, GateResult, GateSeverity, GateStatus


class FeatureLivenessGate(BaseGate):
    """L-1: 特性真实扣费与账本存活检验（彻底切除 Knight Capital 级死代码）"""
    gate_id = "L-1"
    name = "特性真实扣费与账本存活检验"
    category = GateCategory.L_GATE
    severity = GateSeverity.BLOCKER
    evidence = "17 号报告 §4.2: SEC Knight Capital 调查报告——死代码未触发致 4.4 亿美元亏损；回测声明启用特性必须在账本产生实际扣费"
    threshold_desc = "若回测声明开启特性 (如 dividend_tax/management_fee)，账本对应科目记录数 >= 1 且累积发生额 > 0"

    def __init__(self, required_features: Sequence[str] | None = None) -> None:
        self.required_features = list(required_features or ["DIVIDEND_TAX"])

    def evaluate(self, context: Any = None) -> GateResult:
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无上下文数据，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        active = context.get("active_features", []) if isinstance(context, dict) else getattr(context, "active_features", [])
        fee_summary = context.get("fee_summary", {}) if isinstance(context, dict) else getattr(context, "fee_summary", {})
        ledger_entries = context.get("ledger_entries", []) if isinstance(context, dict) else getattr(context, "ledger_entries", [])
        is_pre_run = bool(context.get("is_pre_run", False) if isinstance(context, dict) else getattr(context, "is_pre_run", False))

        if is_pre_run:
            missing_req = [f for f in self.required_features if f not in active]
            if missing_req:
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.FAIL,
                    severity=self.severity,
                    message=f"前置特性配置缺失: {missing_req}，未启用必需特性",
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.PASS,
                severity=self.severity,
                message=f"前置特性配置存活检验通过 (已启用: {list(active)})",
                metrics={"active_features": list(active)},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # ⛔ Fail-Closed（运行期）：未声明任何启用特性 ⇒ 无从判定死代码（默认值不得代替证据）
        if not active:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="未声明本轮启用特性集合（active_features），无法判定死代码（无证据 ≠ 通过）",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        item_counts: dict[str, int] = {}
        item_amounts: dict[str, Decimal] = {}

        for entry in ledger_entries:
            entry_type = str(entry.get("entry_type", "") if isinstance(entry, dict) else getattr(entry, "entry_type", ""))
            amount = Decimal(str(entry.get("amount", 0) if isinstance(entry, dict) else getattr(entry, "amount", 0)))
            if entry_type:
                item_counts[entry_type] = item_counts.get(entry_type, 0) + 1
                item_amounts[entry_type] = item_amounts.get(entry_type, Decimal("0")) + abs(amount)

            fees = entry.get("fees", {}) if isinstance(entry, dict) else getattr(entry, "fees", {})
            if isinstance(fees, dict):
                for f_item, f_amt in fees.items():
                    k = str(f_item.value if hasattr(f_item, "value") else f_item)
                    dec_f = Decimal(str(f_amt))
                    item_counts[k] = item_counts.get(k, 0) + 1
                    item_amounts[k] = item_amounts.get(k, Decimal("0")) + abs(dec_f)

        for k, v in fee_summary.items():
            k_str = str(k.value if hasattr(k, "value") else k)
            v_dec = Decimal(str(v))
            if v_dec > 0:
                item_amounts[k_str] = item_amounts.get(k_str, Decimal("0")) + v_dec
                item_counts[k_str] = item_counts.get(k_str, 0) + 1

        dead_features = []
        for feat in active:
            feat_key = str(feat)
            cnt = item_counts.get(feat_key, 0)
            amt = item_amounts.get(feat_key, Decimal("0"))
            if cnt == 0 or amt <= Decimal("0"):
                dead_features.append(feat_key)

        if dead_features:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"检出声明启用的特性在账本中无实际发生流水: {dead_features}，判定为死代码或未接入真实撮合",
                metrics={"dead_features": dead_features, "observed_counts": item_counts},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"所有声明特性均在账本产生真实流水 (活跃特性: {list(active)})",
            metrics={"active_features": list(active), "item_counts": item_counts},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class AllocationFidelityGate(BaseGate):
    """L-2: 策略权重与分配资金保真度检验（防底层等权均分作弊）"""
    gate_id = "L-2"
    name = "权重与分配资金保真度检验"
    category = GateCategory.L_GATE
    severity = GateSeverity.CRITICAL
    evidence = "17 号报告 §4.2: 策略产出的多因子加权打分在执行层被强制均分为 1/N，使 alpha 打分完全失效"
    threshold_desc = (
        "非等权目标：W 与 V 的 Spearman 秩相关系数 >= 0.90；"
        "等权目标（1/N，Spearman 全域并列不可判）：须无「整只未建仓」(任一实际分配 == 0)，"
        "且已建仓任一只的相对权重比 r_i = s_i·n ∈ [0.5, 2.0]；"
        "非有限值（NaN/±inf）不得 PASS；"
        "（归一化总变差 TV 仅作观测，不再驱动判定）"
    )

    #: 等权目标下的**逐票相对权重比**判据（与 n 解耦）：
    #: ``r_i = s_i / (1/n) = s_i·n``，其中 ``s_i`` 为第 i 只标的实际分配占比（``Σs_i = 1``）。
    #: 等权目标下合法执行（含整手/价格四舍五入）应使 ``r_i ≈ 1.0``。
    #:
    #: ⛔ 弃用「归一化总变差 TV」与「单票绝对偏离 ``max_i|s_i−1/n|``」作**判据**：
    #:   * 二者**均随 n 缩小**——「整只漏建」时 ``TV = max_i|s_i−1/n| = 1/n``，
    #:     故 n 增大即**静默放宽**：n=10 恰 1 只漏建 TV=**0.1000** 因旧判据 ``TV > 0.10`` 为
    #:     False ⇒ 假 PASS；n≥11 整只漏建更是全部漏网（GATE-R2 §2.2/2.3）。
    #:   * 相对权重比 ``r_i`` 是**尺度不变**的（超配 k 倍即 ``r_i=k``，与 n 无关），
    #:     故用**固定比值带** ``[UNDERWEIGHT, OVERWEIGHT]`` 作判据，判据与 n 解耦。
    #:   * 「整只完全未建仓」另由**计数判据**（``v_i == 0``，见 evaluate 内）显式兜底，
    #:     该判据是**纯计数、天然与 n 解耦**。
    EQUAL_WEIGHT_OVERWEIGHT_LIMIT = 2.0   #: 已建仓任一票占比不得超过其等权份额的 2 倍
    EQUAL_WEIGHT_UNDERWEIGHT_LIMIT = 0.5  #: 已建仓任一票占比不得低于其等权份额的 0.5 倍

    #: 归一化总变差 ``TV = 0.5·Σ|s_i − 1/n|`` 的容差。
    #: ⛔ **仅保留供观测/历史度量**（写入 metrics），**不再驱动判定**——见上：TV 随 n 漂移，
    #: 用它作判据会在 n=10 恰一只漏建（TV=0.1000）处出现边界假通过。
    EQUAL_WEIGHT_MAX_TV = 0.10

    @staticmethod
    def _rank(seq: Sequence[float]) -> list[float]:
        n = len(seq)
        indexed = sorted(enumerate(seq), key=lambda x: x[1])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n - 1 and indexed[j][1] == indexed[j + 1][1]:
                j += 1
            avg_rank = 1.0 + (i + j) / 2.0
            for k in range(i, j + 1):
                ranks[indexed[k][0]] = avg_rank
            i = j + 1
        return ranks

    def evaluate(self, context: Any = None) -> GateResult:
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无数据输入，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        tw = context.get("target_weights", {}) if isinstance(context, dict) else getattr(context, "target_weights", {})
        av = context.get("actual_values", {}) if isinstance(context, dict) else getattr(context, "actual_values", {})

        if isinstance(tw, dict) and isinstance(av, dict):
            common_keys = sorted(set(tw.keys()) & set(av.keys()))
            if len(common_keys) < 3:
                # ⛔ Fail-Closed：样本 < 3 无法做秩相关 ⇒ INCONCLUSIVE（不得自动通过）
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.INCONCLUSIVE,
                    severity=self.severity,
                    message=f"重合标的数 {len(common_keys)} < 3，样本不足以做秩相关统计（无证据 ≠ 通过）",
                    metrics={"common_keys_count": len(common_keys)},
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )
            w_list = [float(tw[k]) for k in common_keys]
            v_list = [float(av[k]) for k in common_keys]
        elif isinstance(tw, (list, tuple)) and isinstance(av, (list, tuple)):
            if len(tw) != len(av) or len(tw) < 3:
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.INCONCLUSIVE,
                    severity=self.severity,
                    message="标的序列长度不足 3 或不匹配，样本不足（无证据 ≠ 通过）",
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )
            w_list = [float(x) for x in tw]
            v_list = [float(x) for x in av]
        else:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="权重或分配资金格式不合法",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # ⛔ Fail-Closed（入口有限性校验，覆盖**等权与非等权两条路径**）：
        # 目标权重或实际分配含**非有限值**（NaN / ±inf）时，保真度不等式（秩相关/权重比）
        # 在数学上**无定义**（NaN 参与比较恒为 False），⛔ 绝不允许"落到 PASS"。
        #   * 目标权重含非有限值 ⇒ 目标向量本身非法，无从建立保真基线 ⇒ INCONCLUSIVE；
        #   * 实际分配含非有限值 ⇒ 执行层产出的仓位分配已损坏（物理上不可能）⇒ FAIL。
        nonfinite_target = [i for i, x in enumerate(w_list) if not math.isfinite(x)]
        if nonfinite_target:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message=(
                    f"目标权重含非有限值（NaN/±inf，第 {nonfinite_target} 项），"
                    "目标向量本身非法，无法建立保真基线（退化目标 ≠ 通过）"
                ),
                metrics={"nonfinite_target_indices": nonfinite_target, "target_weights": w_list},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )
        nonfinite_actual = [i for i, x in enumerate(v_list) if not math.isfinite(x)]
        if nonfinite_actual:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=(
                    f"实际分配含非有限值（NaN/±inf，第 {nonfinite_actual} 项），"
                    "执行层产出的仓位分配已损坏，物理上不可能！"
                ),
                metrics={"nonfinite_actual_indices": nonfinite_actual, "actual_values": v_list},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        if len(set(w_list)) <= 1:
            # ⛔ 目标等权 ⇒ Spearman 秩相关全域并列、**不可判**，不得记 PASS。
            # 退化为「实际分配相对等权的**保真度**检验」（而非直接 INCONCLUSIVE）：
            # 当前生产策略即为等权 1/N，若此处一律 INCONCLUSIVE，该门禁在真实配置下将
            # **恒不可判、永无 PASS/FAIL** ⇒ 沦为摆设。故必须继续**实际校验分配**：
            #   * 目标本身退化（Σw<=0）            ⇒ INCONCLUSIVE（无有效目标）
            #   * 实际分配含负值（长仓物理不可能）  ⇒ FAIL
            #   * 实际分配总额为 0（全未建仓）      ⇒ INCONCLUSIVE（无分配证据）
            #   * 存在**整只完全未建仓**（v_i == 0）⇒ FAIL（计数判据，与 n 解耦）
            #   * 已建仓票的相对权重比 r_i 越界     ⇒ FAIL（比值判据，与 n 解耦）
            # （非有限值已在上方入口校验拦截，此处 w_list/v_list 均为有限值。）
            sum_w = sum(w_list)
            if sum_w <= 0:
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.INCONCLUSIVE,
                    severity=self.severity,
                    message="目标权重全为非正值（Σw<=0），目标向量本身退化，无法做等权偏离检验（无有效目标 ≠ 通过）",
                    metrics={"sum_target_weights": sum_w},
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )
            n = len(w_list)
            target_share = 1.0 / n
            if any(v < 0.0 for v in v_list):
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.FAIL,
                    severity=self.severity,
                    message="目标等权但实际分配出现负值，长仓策略物理上不可能持有负仓位，分配已失真！",
                    metrics={"actual_values": v_list},
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )
            sum_v = sum(v_list)
            if sum_v <= 0:
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.INCONCLUSIVE,
                    severity=self.severity,
                    message="目标等权但实际分配总额为 0（未建仓），无实际分配证据可判定（无证据 ≠ 通过）",
                    metrics={"sum_actual_values": sum_v},
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )

            # 判据 ①（**计数判据，与 n 解耦**）：任一目标标的**完全未建仓**（实际分配 == 0）⇒ FAIL。
            # 直接消灭「整只漏建」在旧 TV 判据下随 n 增大而漏网的问题：
            # n=10 恰 1 只漏建（旧 TV=0.1000 因 `>0.10` 假 PASS）、n≥11 整只漏建全部漏网 ⇒ 此处拦截。
            unbuilt_indices = [i for i, v in enumerate(v_list) if v == 0.0]
            if unbuilt_indices:
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.FAIL,
                    severity=self.severity,
                    message=(
                        f"目标权重为等权（1/{n}），但检出 {len(unbuilt_indices)} 只标的**完全未建仓**"
                        f"（实际分配为 0，序号 {unbuilt_indices}），等权目标在执行层被整只漏建！"
                    ),
                    metrics={
                        "unbuilt_count": len(unbuilt_indices),
                        "unbuilt_indices": unbuilt_indices,
                        "target_share": round(target_share, 4),
                        "sample_size": n,
                    },
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )

            actual_shares = [v / sum_v for v in v_list]
            # 判据 ②（**比值判据，与 n 解耦**）：相对权重比 r_i = s_i / (1/n) = s_i·n。
            # r_i 为**尺度不变**量（超配 k 倍即 r_i=k，与 n 无关），故用固定比值带作判据，
            # ⛔ 不再用随 n 漂移的 TV / max|s_i−1/n|（n 增大即静默放宽）。
            ratios = [s * n for s in actual_shares]
            max_ratio = max(ratios)
            min_ratio = min(ratios)
            # 以下两项**仅作观测**（写入 metrics），⛔ 不驱动判定。
            tv = 0.5 * sum(abs(s - target_share) for s in actual_shares)
            max_abs_dev = max(abs(s - target_share) for s in actual_shares)
            obs_metrics = {
                "total_variation": round(tv, 4),
                "max_abs_deviation": round(max_abs_dev, 4),
                "target_share": round(target_share, 4),
                "actual_shares": [round(s, 4) for s in actual_shares],
                "weight_ratios": [round(r, 4) for r in ratios],
                "sample_size": n,
            }
            if (
                max_ratio > self.EQUAL_WEIGHT_OVERWEIGHT_LIMIT
                or min_ratio < self.EQUAL_WEIGHT_UNDERWEIGHT_LIMIT
            ):
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.FAIL,
                    severity=self.severity,
                    message=(
                        f"目标权重为等权（1/{n}），但实际分配相对等权严重偏离"
                        f"（相对权重比 r_i∈[{min_ratio:.3f}, {max_ratio:.3f}] 越出容许带 "
                        f"[{self.EQUAL_WEIGHT_UNDERWEIGHT_LIMIT}, {self.EQUAL_WEIGHT_OVERWEIGHT_LIMIT}]），"
                        "等权目标在执行层被建得面目全非！"
                    ),
                    metrics=obs_metrics,
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.PASS,
                severity=self.severity,
                message=(
                    f"目标权重为等权（1/{n}），实际分配保真度检验通过"
                    f"（无整只漏建，相对权重比 r_i∈[{min_ratio:.3f}, {max_ratio:.3f}] 在容许带内）"
                ),
                metrics=obs_metrics,
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        if len(set(v_list)) <= 1:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message="目标权重存在显著分化，但实际分配资金完全等权均分，权重打分在执行层丢失！",
                metrics={"unique_target_weights": len(set(w_list)), "unique_actual_values": len(set(v_list))},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        r_w = self._rank(w_list)
        r_v = self._rank(v_list)
        n = len(w_list)
        d_sq = sum((rw - rv) ** 2 for rw, rv in zip(r_w, r_v))
        spearman_corr = 1.0 - (6.0 * d_sq) / (n * (n**2 - 1))

        if spearman_corr < 0.90:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"策略权重与实际分配金额秩相关系数 Spearman={spearman_corr:.3f} < 0.90，权重分配严重变形",
                metrics={"spearman_corr": round(spearman_corr, 4), "sample_size": n},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"权重分配保真度检验通过 (Spearman={spearman_corr:.3f} >= 0.90)",
            metrics={"spearman_corr": round(spearman_corr, 4), "sample_size": n},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class StaticAstCallGate(BaseGate):
    """L-3: 关键风控与计算调用链路审计（代码可达性与运行期审计）"""
    gate_id = "L-3"
    name = "关键风控与调用链路审计"
    category = GateCategory.L_GATE
    severity = GateSeverity.CRITICAL
    evidence = "17 号报告 §4.2: 防止导入风控模块但被注释或未被实际业务逻辑调用"
    threshold_desc = "指定的必调函数或方法在运行时被真实调用过 (执行计数 >= 1)"

    def evaluate(self, context: Any = None) -> GateResult:
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无调用追踪数据，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        executed_present = isinstance(context, dict) and "executed_calls" in context
        executed = set(context.get("executed_calls", []) if isinstance(context, dict) else getattr(context, "executed_calls", []))
        required = set(context.get("required_calls", []) if isinstance(context, dict) else getattr(context, "required_calls", []))
        source_code = context.get("source_code", "") if isinstance(context, dict) else getattr(context, "source_code", "")

        # ⛔ Fail-Closed：未提供必调链路清单 ⇒ 无从审计（无证据 ≠ 通过）
        if not required:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="未提供必调链路清单（required_calls），无法审计调用链（无证据 ≠ 通过）",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        if source_code and required:
            try:
                tree = ast.parse(source_code)
                calls_in_ast = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        if isinstance(node.func, ast.Name):
                            calls_in_ast.add(node.func.id)
                        elif isinstance(node.func, ast.Attribute):
                            calls_in_ast.add(node.func.attr)
                missing_ast = [r for r in required if r not in calls_in_ast and not any(r.endswith(f".{c}") for c in calls_in_ast)]
                if missing_ast:
                    return GateResult(
                        gate_id=self.gate_id,
                        name=self.name,
                        category=self.category,
                        status=GateStatus.FAIL,
                        severity=self.severity,
                        message=f"静态 AST 扫描发现关键调用缺失: {missing_ast}",
                        metrics={"missing_in_ast": missing_ast},
                        threshold=self.threshold_desc,
                        evidence=self.evidence,
                    )
            except SyntaxError as e:
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.FAIL,
                    severity=self.severity,
                    message=f"源码语法解析失败: {e}",
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )

        if required:
            # ⛔ Fail-Closed：未提供运行期调用追踪（executed_calls）时，静态可达 ≠ 运行时已调用，
            # 不得判 PASS；判定为 INCONCLUSIVE（证据不足），交由具备追踪能力的环境补证。
            if not executed_present:
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.INCONCLUSIVE,
                    severity=self.severity,
                    message=(
                        f"必调链路 {sorted(required)} 静态可达，但缺少运行期调用追踪（executed_calls），"
                        "无法判定是否真实调用（无证据 ≠ 通过）"
                    ),
                    metrics={"required_count": len(required), "missing_executed_trace": True},
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )
            missing_exec = [r for r in required if r not in executed]
            if missing_exec:
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.FAIL,
                    severity=self.severity,
                    message=f"运行时关键调用未发生: {missing_exec}",
                    metrics={"missing_calls": missing_exec, "executed_count": len(executed)},
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"调用链路审计通过 (共校验 {len(required)} 项必调链路)",
            metrics={"required_count": len(required), "executed_count": len(executed)},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )
