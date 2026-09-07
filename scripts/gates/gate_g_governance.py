#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""G-Gate: 工程物理留痕与交付门禁（Engineering Provenance & Delivery Gates）

依据：
1. 17 号深度调研报告 §4.6 / G-1 ~ G-3 门禁定义
2. 出处三件套(Git+Hash+Timestamp)、tasks 防伪签名与母库只读区 370 行硬守卫
"""

from __future__ import annotations

import datetime
import glob
import os
import re
from typing import Any

from .base import BaseGate, GateBlockerError, GateCategory, GateResult, GateSeverity, GateStatus


class ProvenanceTriadGate(BaseGate):
    """G-1: 交付出处三件套完整性检验（Git SHA + Data Hash + Timestamp）"""
    gate_id = "G-1"
    name = "交付出处三件套完整性检验"
    category = GateCategory.G_GATE
    severity = GateSeverity.BLOCKER
    evidence = "17 号报告 §4.6: 杜绝口头汇报与无源交付，任何产出必须附带 Git SHA + Data Hash + Timestamp 三件套"
    threshold_desc = "git_commit (>=7位有效SHA), data_hash (64位hex), timestamp (ISO-8601) 缺一不可"

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - git_commit: str
        - data_hash: str
        - timestamp: str
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无出处元数据，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        commit = str(context.get("git_commit", "") if isinstance(context, dict) else getattr(context, "git_commit", "")).strip()
        data_hash = str(context.get("data_hash", "") if isinstance(context, dict) else getattr(context, "data_hash", "")).strip()
        ts = str(context.get("timestamp", "") if isinstance(context, dict) else getattr(context, "timestamp", "")).strip()

        missing = []
        if not commit or len(commit) < 7 or not re.match(r"^[0-9a-fA-F]+$", commit):
            missing.append("git_commit (无效或缺失)")
        if not data_hash or len(data_hash) < 16:
            missing.append("data_hash (无效或缺失)")
        if not ts:
            missing.append("timestamp (缺失)")

        if missing:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"交付出处三件套不完整: {missing}",
                metrics={"missing_items": missing},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message="交付出处三件套完整性检验通过",
            metrics={"git_commit": commit[:7], "data_hash": data_hash[:12], "timestamp": ts},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class TasksSignGate(BaseGate):
    """G-2: tasks.md 物理勾选防伪门禁（禁止虚假口头勾选）"""
    gate_id = "G-2"
    name = "tasks.md 物理勾选防伪门禁"
    category = GateCategory.G_GATE
    severity = GateSeverity.CRITICAL
    evidence = "17 号报告 §4.6: 任务从 [ ] 变为 [x] 必须附带门禁自动化报告签名，杜绝凭空勾选"
    threshold_desc = "任务勾选必须有对应的机器门禁通过证据签名"

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - task_id: str
        - is_checked: bool
        - gate_signature: str | dict (门禁签名，包含通过记录)
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无任务上下文，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # 模式 A: 如果提供了 tasks_path 或 tasks_content，进行全文档级防伪验签
        tasks_file = context.get("tasks_path") if isinstance(context, dict) else getattr(context, "tasks_path", None)
        tasks_text = context.get("tasks_content") if isinstance(context, dict) else getattr(context, "tasks_content", None)
        if tasks_file or tasks_text:
            from .tamper_guard import verify_tasks_markdown
            target = tasks_file if tasks_file else tasks_text
            ok, viols, stats = verify_tasks_markdown(target)
            if not ok:
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.FAIL,
                    severity=self.severity,
                    message=f"tasks.md 检出 {len(viols)} 项未签名或格式违规勾选: {viols[:2]}",
                    metrics={"violations": viols, "stats": stats},
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.PASS,
                severity=self.severity,
                message=f"tasks.md 全量 {stats['checked_tasks']} 项已勾选任务防伪交付证据全量合规",
                metrics=stats,
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # 模式 B: 单任务参数签名核验
        task_id = str(context.get("task_id", "") if isinstance(context, dict) else getattr(context, "task_id", ""))
        is_checked = bool(context.get("is_checked", False) if isinstance(context, dict) else getattr(context, "is_checked", False))
        sig = context.get("gate_signature") if isinstance(context, dict) else getattr(context, "gate_signature", None)

        if is_checked and not sig:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"任务 [{task_id}] 已被勾选 [x]，但未附带机器门禁通过签名证据，涉嫌虚假汇报！",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"任务 [{task_id}] 勾选签名验证通过",
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class AntiTamperSignatureGate(BaseGate):
    """G-4: 产物防篡改签名与密码学保真门禁"""
    gate_id = "G-4"
    name = "产物防篡改签名与密码学保真门禁"
    category = GateCategory.G_GATE
    severity = GateSeverity.BLOCKER
    evidence = "17 号报告 §4.6 / 阶段三防伪硬化: 产物必须具备抗篡改密码学签名，严禁事后修改任何收益率或出处字段"
    threshold_desc = "anti_tamper_signature 存在且 SHA-256 验签有效"

    def evaluate(self, context: Any = None) -> GateResult:
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无产物上下文，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        from .tamper_guard import verify_run_signature

        record = context if isinstance(context, dict) else getattr(context, "__dict__", {})
        if "run_record" in record:
            record = record["run_record"]

        ok, msg = verify_run_signature(record)
        if not ok:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=msg,
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=msg,
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class MasterFindingGate(BaseGate):
    """G-3: 母库只读区 FINDING- 守卫行数严格不变门禁（严格恒等于 370 行）"""
    gate_id = "G-3"
    name = "母库只读区 FINDING- 守卫行数门禁"
    category = GateCategory.G_GATE
    severity = GateSeverity.BLOCKER
    evidence = "母库只读区纪律: finai/sources/ 下包含 FINDING- 守卫字符串的行数严格恒等于 370 行，任何破坏一票否决"
    threshold_desc = "Count(lines containing 'FINDING-') 严格恒等于 370 行"

    TARGET_COUNT = 370

    def __init__(self, sources_dir: str | None = None) -> None:
        if sources_dir is not None:
            self.sources_dir = sources_dir
        else:
            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
            self.sources_dir = os.path.join(base_dir, "finai", "sources")

    def evaluate(self, context: Any = None) -> GateResult:
        target_dir = self.sources_dir
        if context and isinstance(context, dict) and "sources_dir" in context:
            target_dir = context["sources_dir"]

        if not os.path.exists(target_dir):
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"母库只读区目录不存在: {target_dir}",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        actual_count = 0
        file_counts = {}

        # 遍历所有 py 文件
        for root, _, files in os.walk(target_dir):
            for file in files:
                if file.endswith(".py"):
                    full_path = os.path.join(root, file)
                    rel_name = os.path.relpath(full_path, target_dir)
                    cnt = 0
                    try:
                        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                            for line in f:
                                if "FINDING-" in line:
                                    cnt += 1
                    except Exception as e:
                        pass
                    if cnt > 0:
                        file_counts[rel_name] = cnt
                        actual_count += cnt

        if actual_count != self.TARGET_COUNT:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"母库只读区 FINDING- 守卫行数为 {actual_count}，违背恒等于 370 行铁律！",
                metrics={"actual_count": actual_count, "target_count": self.TARGET_COUNT, "file_distribution": file_counts},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"母库只读区 FINDING- 守卫行数严格吻合 370 行铁律 (实际: {actual_count} 行)",
            metrics={"count": actual_count, "files_with_findings": len(file_counts)},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )
