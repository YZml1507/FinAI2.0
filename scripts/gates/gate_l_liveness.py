#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L-Gate: 调用存活与参数落地门禁（Liveness & Parameter Fidelity Gates）

依据：
1. 17 号深度调研报告 §4.2 / L-1 ~ L-3 门禁定义
2. 规避 Knight Capital 级死代码灾难（特性声明后底层被注释、绕过或等权抹平）
"""

from __future__ import annotations

import ast
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
    threshold_desc = "策略分配目标权重向量 W 与实际分配金额向量 V 的 Spearman 秩相关系数 >= 0.90"

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
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.PASS,
                    severity=self.severity,
                    message=f"重合标的数 {len(common_keys)} < 3，不进行秩相关统计，默认通过",
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
                    status=GateStatus.PASS,
                    severity=self.severity,
                    message="标的序列长度不足 3 或不匹配，通过",
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

        if len(set(w_list)) <= 1:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.PASS,
                severity=self.severity,
                message="目标权重本身为等权，通过",
                metrics={"unique_target_weights": len(set(w_list))},
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

        executed = set(context.get("executed_calls", []) if isinstance(context, dict) else getattr(context, "executed_calls", []))
        required = set(context.get("required_calls", []) if isinstance(context, dict) else getattr(context, "required_calls", []))
        source_code = context.get("source_code", "") if isinstance(context, dict) else getattr(context, "source_code", "")

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
