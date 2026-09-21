#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Spike 实证：把玩具外部回测账本喂给全部 29 门，如实记录每门结果。

用法：
    .venv/bin/python experiments/spikes/gate_generalization/run_spike.py

输出：
    - 终端打印每门 status/message；
    - ``RESULTS.txt`` 落盘同一结果表（供报告引用）。

纪律（与任务要求一致）：如实汇报跑不通的门与卡因，不硬凑通过数。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Any

_SPIKE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SPIKE_DIR.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gates.gate_master_audit import GateMasterAudit  # noqa: E402

from toy_adapter import ToyEvidenceAdapter  # noqa: E402


def evaluate_all(ctx: dict[str, Any]) -> list[dict[str, str]]:
    gates = GateMasterAudit.get_standard_gates()
    rows: list[dict[str, str]] = []
    for gate in gates:
        try:
            result = gate.evaluate(dict(ctx))
            rows.append({
                "gate_id": gate.gate_id,
                "severity": str(gate.severity.value if hasattr(gate.severity, "value") else gate.severity),
                "status": result.status.value,
                "blocking": str(getattr(result, "blocking", "")),
                "message": result.message,
            })
        except Exception as exc:  # noqa: BLE001 —— spike 如实记录崩溃
            rows.append({
                "gate_id": gate.gate_id,
                "severity": str(gate.severity),
                "status": "ERROR",
                "blocking": "?",
                "message": f"EXCEPTION {type(exc).__name__}: {exc}",
            })
            traceback.print_exc()
    return rows


def main() -> int:
    adapter = ToyEvidenceAdapter(_SPIKE_DIR / "toy_ledger.csv")
    ctx = adapter.build_context()
    rows = evaluate_all(ctx)

    lines = []
    header = f"{'gate':<10} {'severity':<10} {'status':<13} {'blocking':<9} message"
    lines.append(header)
    lines.append("-" * len(header))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        lines.append(f"{r['gate_id']:<10} {r['severity']:<10} {r['status']:<13} {r['blocking']:<9} {r['message'][:110]}")
    lines.append("-" * len(header))
    lines.append(f"summary: {dict(sorted(counts.items()))} / total={len(rows)}")

    out = "\n".join(lines)
    print(out)
    (_SPIKE_DIR / "RESULTS.txt").write_text(out + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
