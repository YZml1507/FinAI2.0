#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Spike 实证（产品化升级版）：玩具外部账本 → adapter → audit_external 入口。

与 spike 版差异：不再逐门手动 ``evaluate(ctx)``，而是走产品化路径——
``scripts.gates.adapter.build_external_context`` 装配 ctx，
``scripts.gates.audit_external`` 的 22 门外部集（✅直接通用 + 🔧需适配器）
统一调度、分层报告 PASS/FAIL/INCONCLUSIVE。

用法：
    .venv/bin/python experiments/spikes/gate_generalization/run_spike.py

输出：
    - 终端打印分层结果表；
    - ``RESULTS.txt`` 落盘同一结果表（供报告引用）。

纪律（与任务要求一致）：如实汇报跑不通的门与卡因，不硬凑通过数。
本仓特有 🏠 门（E-1/E-2/S-2/G-2/G-3/G-DOC-1/G-REF-1）由外部入口显式排除。
"""

from __future__ import annotations

import sys
from pathlib import Path

_SPIKE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SPIKE_DIR.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_SPIKE_DIR) not in sys.path:
    sys.path.insert(0, str(_SPIKE_DIR))

from scripts.gates.audit_external import (  # noqa: E402
    ADAPTER_GATE_IDS,
    GENERAL_GATE_IDS,
    REPO_BOUND_GATE_IDS,
    audit_adapter,
)
from scripts.gates.base import is_blocking_result  # noqa: E402

from toy_adapter import ToyEvidenceAdapter  # noqa: E402


def main() -> int:
    adapter = ToyEvidenceAdapter(_SPIKE_DIR / "toy_ledger.csv")
    results, ctx = audit_adapter(adapter)

    header = f"{'gate':<10} {'层':<11} {'severity':<10} {'status':<13} {'blocking':<9} message"
    lines = [
        "外部审计实证（adapter → audit_external，22 门 = ✅7 + 🔧15；🏠7 门显式排除）",
        f"ctx 键数: {len(ctx)}；market_rules: {ctx['market_rules'].market_id} "
        f"(lot={ctx['market_rules'].board_lot_size}, cap={ctx['market_rules'].high_price_limit}, "
        f"fee_subjects={ctx['market_rules'].required_fee_subjects}, t+1={ctx['market_rules'].t_plus_1})",
        f"排除门集: {', '.join(REPO_BOUND_GATE_IDS)}",
        "",
        header,
        "-" * len(header),
    ]
    counts: dict[str, int] = {}
    layer_counts: dict[str, dict[str, int]] = {}
    for r in results:
        layer = "✅" if r.gate_id in GENERAL_GATE_IDS else ("🔧" if r.gate_id in ADAPTER_GATE_IDS else "?")
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
        layer_counts.setdefault(layer, {})
        layer_counts[layer][r.status.value] = layer_counts[layer].get(r.status.value, 0) + 1
        blocking = is_blocking_result(r)
        lines.append(
            f"{r.gate_id:<10} {layer:<11} {r.severity.value:<10} {r.status.value:<13} "
            f"{str(blocking):<9} {r.message[:100]}"
        )
    lines.append("-" * len(header))
    for layer in ("✅", "🔧"):
        lines.append(f"layer {layer}: {dict(sorted(layer_counts.get(layer, {}).items()))}")
    lines.append(f"summary: {dict(sorted(counts.items()))} / total={len(results)}")

    out = "\n".join(lines)
    print(out)
    (_SPIKE_DIR / "RESULTS.txt").write_text(out + "\n", encoding="utf-8")

    # fail-closed：外部入口对 FAIL/INCONCLUSIVE>=CRITICAL 记阻断（本脚本如实返回其退出码）
    n_blocking = sum(1 for r in results if is_blocking_result(r))
    print(f"\nblocking={n_blocking}（INCONCLUSIVE 缺证据亦计——外部审计无'只告警'豁免）")
    return 1 if n_blocking else 0


if __name__ == "__main__":
    sys.exit(main())
