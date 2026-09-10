#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""晋升 / 准入层门禁（Acceptance Gate）——G-MDD-1 的 BLOCKER 归属地。

三层分层（M3 门禁改造 · 任务 1）：

| 层 | G-MDD-1 行为 |
|---|---|
| 推送期（pre-push / ``--ci``） | **WARN**（展示但不阻断）：推送代码不产生回撤，属研究质量而非工程诚实性 |
| 回测 / 测量期 | **报告、不阻断**：测量仪器不因测出差结果而停机 |
| 晋升 / 准入期（本模块） | **BLOCKER**：读产物 ``metrics``，超限即判 FAIL 并 ``exit≠0`` |

准入判据（任一命中 ⇒ FAIL）：

* ``max_drawdown > 0.35``（G-MDD-1 回撤上限）；
* ``win_rate < 0.35``（策略最低胜率）；
* ``annual_turnover > 4.0``（年化单边换手硬顶，S-1）；
* 附加守卫：``round_trips == 0``（0 成交 ⇒ 平凡结果，非可准入成果）。

命令行：
    ``py -3.11 -m scripts.gates.acceptance --artifact experiments/runs/<run>.json``
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "MDD_MAX",
    "WIN_RATE_MIN",
    "TURNOVER_MAX",
    "evaluate_acceptance",
    "main",
]

#: G-MDD-1 回撤上限（与 ``gate_consistency.MaxDrawdownCeilingGate.MAX_MDD`` 同源口径）。
MDD_MAX = Decimal("0.35")
#: 策略最低胜率。
WIN_RATE_MIN = Decimal("0.35")
#: 年化单边换手率硬顶。
TURNOVER_MAX = Decimal("4.0")


def _to_decimal(value: Any) -> Decimal | None:
    """尽力把产物字段转为 Decimal；缺失/非法返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def load_artifact(path: str | Path) -> dict[str, Any]:
    """读取产物 JSON；缺失/非法/非对象 ⇒ 抛 ``FileNotFoundError`` / ``ValueError``。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"准入产物不存在: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"准入产物非 JSON 对象: {p}")
    return data


def evaluate_acceptance(artifact_path: str | Path) -> tuple[bool, list[dict[str, Any]], dict[str, Any]]:
    """对准入产物做判定。

    Args:
        artifact_path: 回测产物 JSON 路径。

    Returns:
        ``(ok, criteria, meta)``：``ok`` 为全部判据通过；``criteria`` 为逐条判据结果；
        ``meta`` 含 run_id / 关键指标。
    """
    record = load_artifact(artifact_path)
    metrics = record.get("metrics") or {}
    run_id = str(record.get("run_id", Path(str(artifact_path)).name))

    mdd = _to_decimal(metrics.get("max_drawdown"))
    win_rate = _to_decimal(metrics.get("win_rate"))
    turnover = _to_decimal(metrics.get("annual_turnover"))
    round_trips_raw = metrics.get("round_trips")
    round_trips: int | None = None
    try:
        round_trips = int(round_trips_raw) if round_trips_raw is not None else None
    except (TypeError, ValueError):
        round_trips = None

    criteria: list[dict[str, Any]] = []

    def _add(cid: str, name: str, value: Any, threshold: str, passed: bool, reason: str) -> None:
        criteria.append({
            "id": cid, "name": name, "value": value,
            "threshold": threshold, "passed": passed, "reason": reason,
        })

    if mdd is None:
        _add("G-MDD-1", "最大回撤上限", None, "max_drawdown <= 0.35", False,
             "产物缺少 max_drawdown，无法判定（无证据 ≠ 通过）")
    else:
        _add("G-MDD-1", "最大回撤上限", str(mdd), "max_drawdown <= 0.35",
             mdd <= MDD_MAX,
             f"MDD={mdd:.4f} {'<=' if mdd <= MDD_MAX else '>'} 0.35" if mdd is not None else "")

    if win_rate is None:
        _add("ACCEPT-WIN", "胜率下限", None, "win_rate >= 0.35", False,
             "产物缺少 win_rate，无法判定（无证据 ≠ 通过）")
    else:
        _add("ACCEPT-WIN", "胜率下限", str(win_rate), "win_rate >= 0.35",
             win_rate >= WIN_RATE_MIN,
             f"win_rate={win_rate:.4f} {'>=' if win_rate >= WIN_RATE_MIN else '<'} 0.35")

    if turnover is None:
        _add("S-1", "年化换手硬顶", None, "annual_turnover <= 4.0", False,
             "产物缺少 annual_turnover，无法判定（无证据 ≠ 通过）")
    else:
        _add("S-1", "年化换手硬顶", str(turnover), "annual_turnover <= 4.0",
             turnover <= TURNOVER_MAX,
             f"annual_turnover={turnover:.4f} {'<=' if turnover <= TURNOVER_MAX else '>'} 4.0")

    if round_trips is None:
        _add("ACCEPT-RT", "有效成交守卫", None, "round_trips > 0", False,
             "产物缺少 round_trips，无法判定（无证据 ≠ 通过）")
    else:
        _add("ACCEPT-RT", "有效成交守卫", round_trips, "round_trips > 0",
             round_trips > 0,
             f"round_trips={round_trips}" + ("（0 成交 ⇒ 平凡结果，非可准入成果）" if round_trips == 0 else ""))

    ok = all(c["passed"] for c in criteria)
    meta = {
        "run_id": run_id,
        "artifact": str(artifact_path),
        "max_drawdown": None if mdd is None else str(mdd),
        "win_rate": None if win_rate is None else str(win_rate),
        "annual_turnover": None if turnover is None else str(turnover),
        "round_trips": round_trips,
    }
    return ok, criteria, meta


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FinAI2.0 晋升/准入层门禁（G-MDD-1 BLOCKER 归属）")
    parser.add_argument("--artifact", type=str, default="",
                        help="待准入判定的回测产物 JSON 路径")
    parser.add_argument("artifact_pos", nargs="?", default="",
                        help="回测产物 JSON 路径（位置参数备用）")
    args = parser.parse_args(argv)

    artifact = args.artifact or args.artifact_pos
    if not artifact:
        parser.print_help()
        return 2

    try:
        ok, criteria, meta = evaluate_acceptance(artifact)
    except Exception as exc:                # noqa: BLE001
        print(f"[FAIL] 准入判定失败：{exc}")
        return 1

    print("=" * 78)
    print(f"晋升/准入层判定：{meta['run_id']}")
    print(f"  产物: {meta['artifact']}")
    print("-" * 78)
    for c in criteria:
        flag = "PASS" if c["passed"] else "FAIL"
        print(f"  [{flag}] {c['id']:<10} {c['name']}：{c['reason']}")
    print("=" * 78)
    if ok:
        print("[准入通过] 全部判据达标，允许晋升。")
        return 0
    failed = [c["id"] for c in criteria if not c["passed"]]
    print(f"[准入失败] 命中 {len(failed)} 项未达标判据：{failed} —— BLOCK 晋升。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
