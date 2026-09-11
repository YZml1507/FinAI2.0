#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""G-REPRO-1 复现一致性门禁（Reproduction Consistency Gate）。

对应 `docs/audit/repro_root_cause.md` §6.2(T3)：把 PM-1（同一 ``params_hash`` 产出 3 种互斥
结果却无门禁可拦）钉死为可机读断言。

判定规则（Fail-Closed）：

* 按**完整出处键** ``repro_fingerprint`` 分组；同组内 ``metrics`` 必须逐字段一致，
  否则 **FAIL**（违反"同参同输入必得同结果"）。
* 无 ``repro_fingerprint`` 的产物（历史 legacy，缺内容寻址出处）**无法验证**，
  归组前缀 ``LEGACY::<params_hash>``，报 **INCONCLUSIVE**（``LEGACY_UNVERIFIED``），
  ⛔ **绝不静默 PASS**（"无法验证"≠"验证通过"）。
* 无任何产物 ⇒ **INCONCLUSIVE**（无证据 ≠ 通过）。

> 注：legacy 组内即便 ``metrics`` 不一致也**不判 FAIL**——因为 legacy 的 ``params_hash``
> 并不覆盖输入，组内输入本就可能不同，无法据此归因"不可复现"。真正的不可复现，
> 只在**有完整出处键**的组内暴露。
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .base import BaseGate, GateCategory, GateResult, GateSeverity, GateStatus
from .gate_consistency import _REPO_ROOT, _artifact_from_path, _run_artifact_files

__all__ = ["ReproducibilityGate", "LEGACY_PREFIX"]

#: legacy 归组前缀（缺内容寻址出处）。
LEGACY_PREFIX = "LEGACY::"


def _collect_run_records(context: Any) -> list[dict[str, Any]]:
    """解析待评估的产物记录集合；缺省扫描 ``experiments/runs/*.json``。

    仅消费 ``run_records`` / ``artifact_path(s)``（⛔ **不**读单个 ``run_record``/``metrics``）：
    单条记录无法做"跨产物分组一致性"判定；缺省时扫描**全部**仓内产物，
    以便真正暴露"同出处键却异果"的 PM-1 缺陷。
    """
    ctx: dict[str, Any] = context if isinstance(context, dict) else {}

    explicit: list[Any] = []
    if ctx.get("run_records"):
        explicit.extend(ctx["run_records"])
    if ctx.get("artifact_path"):
        explicit.append(ctx["artifact_path"])
    if ctx.get("artifact_paths"):
        explicit.extend(ctx["artifact_paths"])

    records: list[dict[str, Any]] = []
    for item in explicit:
        if isinstance(item, dict):
            records.append(item)
        else:
            art = _artifact_from_path(Path(str(item)))
            if art is not None:
                records.append(art["record"])
    if records:
        return records

    for path in _run_artifact_files(_REPO_ROOT):
        art = _artifact_from_path(path)
        if art is not None:
            records.append(art["record"])
    return records


def _load_adopted_run_id() -> str | None:
    """当前"已采纳产物"的 run_id（无采纳 / 读取失败 ⇒ ``None``）。"""
    try:
        from .adoption import load_adopted

        adopted = load_adopted()
    except Exception:                        # noqa: BLE001
        return None
    if not adopted:
        return None
    return str(adopted.get("run_id") or "") or None


class ReproducibilityGate(BaseGate):
    """G-REPRO-1: 复现一致性门禁（BLOCKER）。

    同一 ``repro_fingerprint`` 的产物 ``metrics`` 必须逐字段一致；legacy 产物报
    ``LEGACY_UNVERIFIED``（INCONCLUSIVE）。
    """

    gate_id = "G-REPRO-1"
    name = "复现一致性门禁"
    category = GateCategory.G_GATE
    severity = GateSeverity.BLOCKER
    evidence = "docs/audit/repro_root_cause.md §6.2(T3)：4 份产物 params_hash 全同 f54c298d5168eac5 却给出 3 种互斥结果（PM-1）"
    threshold_desc = "同一 repro_fingerprint 分组的产物 metrics 必须逐字段一致；legacy 产物须报 LEGACY_UNVERIFIED"

    def evaluate(self, context: Any = None) -> GateResult:
        records = _collect_run_records(context)
        if not records:
            return self._make(
                GateStatus.INCONCLUSIVE,
                "未找到任何回测产物，无法判定复现一致性（无证据 ≠ 通过）",
            )

        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        legacy_records: list[dict[str, Any]] = []
        verified_groups = 0
        for rec in records:
            fingerprint = rec.get("repro_fingerprint")
            if fingerprint:
                groups[str(fingerprint)].append(rec)
            else:
                legacy_records.append(rec)
                groups[LEGACY_PREFIX + str(rec.get("params_hash", "unknown"))].append(rec)

        violations: list[dict[str, Any]] = []
        empty_metric_groups: list[str] = []
        for key, recs in groups.items():
            if key.startswith(LEGACY_PREFIX) or len(recs) < 2:
                continue
            metrics_seq = [(r.get("metrics") or {}) for r in recs]
            # ⛔ Fail-Closed（GATE-R3）：同源分组但 metrics **全为空** ⇒ 逐字段比对退化为
            # "空集平凡相等"（**0 信息**）⇒ ⛔ 不得据此判 PASS（此前会谎报"逐字段一致"）。
            # 该组不计入 verified_groups，透出到下方 INCONCLUSIVE 分支（无证据 ≠ 通过）。
            if all(not m for m in metrics_seq):
                empty_metric_groups.append(key)
                continue
            verified_groups += 1
            base = metrics_seq[0]
            base_id = recs[0].get("run_id", "<unknown>")
            for r, r_metrics in zip(recs[1:], metrics_seq[1:]):
                if r_metrics != base:
                    violations.append({
                        "repro_fingerprint": key,
                        "run_id_a": base_id,
                        "run_id_b": r.get("run_id", "<unknown>"),
                        "metric_keys_diverged": sorted(
                            set(base.keys()) ^ set(r_metrics.keys())
                        ) or [
                            k for k in base
                            if base.get(k) != r_metrics.get(k)
                        ],
                    })

        if violations:
            first = violations[0]
            return self._make(
                GateStatus.FAIL,
                (
                    f"检出 {len(violations)} 组产物违反'同参同输入必得同结果'："
                    f"出处键 {first['repro_fingerprint'][:12]} 下 "
                    f"{first['run_id_a']} 与 {first['run_id_b']} 的 metrics 不一致"
                    f"（分歧指标 {first['metric_keys_diverged'][:5]}）"
                ),
                metrics={
                    "violations_total": len(violations),
                    "violations": violations[:20],
                    "verified_groups": verified_groups,
                    "total_records": len(records),
                },
            )

        if legacy_records:
            legacy_keys = sorted({
                LEGACY_PREFIX + str(r.get("params_hash", "unknown")) for r in legacy_records
            })
            # ㉛：legacy 可存在、可研究，但**不得作为晋升依据**——
            # 若"已被采纳登记的产物"落在 legacy 里，其 INCONCLUSIVE 必须是 BLOCKER（不是 WARN）。
            adopted_run_id = _load_adopted_run_id()
            adopted_is_legacy = adopted_run_id is not None and any(
                str(r.get("run_id", "")) == adopted_run_id for r in legacy_records
            )
            if adopted_is_legacy:
                return self._make(
                    GateStatus.INCONCLUSIVE,
                    (
                        f"ADOPTED_LEGACY_UNVERIFIED: 被采纳产物 {adopted_run_id} 为 legacy"
                        "（缺 repro_fingerprint 内容寻址出处）⇒ 复现性无法验证 ⇒ **BLOCKER**"
                        "（legacy 可存在、可研究，⛔ 但不得作为晋升依据）"
                    ),
                    metrics={
                        "adopted_run_id": adopted_run_id,
                        "ci_blocking": True,     # ci_policy 据此把它当 BLOCKER（非 WARN）
                        "legacy_records": len(legacy_records),
                        "legacy_groups": legacy_keys[:20],
                        "total_records": len(records),
                    },
                    severity=GateSeverity.BLOCKER,
                )
            return self._make(
                GateStatus.INCONCLUSIVE,
                (
                    f"LEGACY_UNVERIFIED: 检出 {len(legacy_records)} 份 legacy 产物（缺 repro_fingerprint "
                    f"内容寻址出处），无法验证复现一致性（归组 {legacy_keys[:5]}）——"
                    "无法验证 ≠ 通过，须补内容寻址出处后方可判定"
                ),
                metrics={
                    "legacy_records": len(legacy_records),
                    "legacy_groups": legacy_keys[:20],
                    "verified_groups": verified_groups,
                    "total_records": len(records),
                },
            )

        # ⛔ Fail-Closed：无任何"含 >=2 份产物且 metrics 非空"的同源分组 ⇒ 从未发生过
        # **实质**跨产物比对 ⇒ INCONCLUSIVE（此前会以 "0 组同源产物...一致" 谎报复现一致性成立）。
        if verified_groups == 0:
            if empty_metric_groups:
                detail = (
                    f"其中 {len(empty_metric_groups)} 组同源产物的 metrics **全为空**"
                    "（0 信息、空集平凡相等 ⇒ 不得据此判通过）"
                )
            else:
                detail = "且无任何含 >=2 份产物的同源分组"
            return self._make(
                GateStatus.INCONCLUSIVE,
                (
                    f"检出 {len(records)} 份带内容寻址出处的产物，但无任何**可实质比对**的同源分组"
                    f"（{detail}），无法验证'同参同输入必得同结果'（无证据 ≠ 通过）"
                ),
                metrics={
                    "verified_groups": 0,
                    "empty_metric_groups": len(empty_metric_groups),
                    "empty_metric_group_keys": sorted(empty_metric_groups)[:20],
                    "total_records": len(records),
                    "legacy_records": 0,
                },
            )

        return self._make(
            GateStatus.PASS,
            (
                f"{len(records)} 份产物均带内容寻址出处，{verified_groups} 组同源产物的 metrics "
                "逐字段一致，复现一致性成立"
                + (
                    f"（另有 {len(empty_metric_groups)} 组 metrics 全为空，未纳入实质比对）"
                    if empty_metric_groups else ""
                )
            ),
            metrics={
                "verified_groups": verified_groups,
                "empty_metric_groups": len(empty_metric_groups),
                "total_records": len(records),
                "legacy_records": 0,
            },
        )

    def _make(
        self,
        status: GateStatus,
        message: str,
        metrics: dict[str, Any] | None = None,
        severity: GateSeverity | None = None,
    ) -> GateResult:
        return GateResult(
            gate_id=self.gate_id, name=self.name, category=self.category,
            status=status, severity=severity or self.severity, message=message,
            metrics=metrics or {}, threshold=self.threshold_desc, evidence=self.evidence,
        )
