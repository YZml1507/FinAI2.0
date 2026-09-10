#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""晋升 / 准入层门禁（Acceptance Gate）——G-MDD-1 的 BLOCKER 归属地。

三层分层（M3 门禁改造 · 任务 1）：

| 层 | G-MDD-1 行为 |
|---|---|
| 推送期（pre-push / ``--ci``） | **WARN**（展示但不阻断）：推送代码不产生回撤，属研究质量而非工程诚实性 |
| 回测 / 测量期 | **报告、不阻断**：测量仪器不因测出差结果而停机 |
| 晋升 / 准入期（本模块） | **BLOCKER**：先验来源、再判指标，任一不满足即 FAIL 并 ``exit≠0`` |

**先验证、再判定（治理层 ㉗）**——任一不满足即 FAIL：

* ``PROV-SIG``   ① ``anti_tamper_signature`` **验签通过**（复用 ``tamper_guard.verify_run_signature``）；
* ``SCHEMA``     ② ``schema_version`` **存在且为现行值**（legacy ⇒ FAIL，不得 PASS）；
* ``STATUS``     ③ ``status == "FINISHED"``；``NO-ERROR`` 终态无 ``error``；
* ``MDD-SANITY`` ④ ``0 <= max_drawdown <= 1``（**负值 / 超 1 / null / bool / 字符串一律 FAIL**，堵 ``mdd=-0.30``）；
* ``WIN-SANITY`` / ``TURNOVER-SANITY`` ④'（㉞）``win_rate ∈ [0,1]``、``annual_turnover >= 0``
  ——与 MDD-SANITY 对称；否则 ``win_rate=9.9`` / ``annual_turnover=-3.0`` 能"满足阈值"而洗白；
* ``G-MDD-1`` / ``ACCEPT-WIN`` / ``S-1`` / ``ACCEPT-RT`` ⑤ 策略阈值与有效成交守卫。

命令行：
    ``py -3.11 -m scripts.gates.acceptance --artifact experiments/runs/<run>.json``
    ``py -3.11 -m scripts.gates.acceptance --artifact <run>.json --adopt``   # 采纳登记（晋升留证）
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
    "load_artifact",
    "main",
]

#: G-MDD-1 回撤上限（与 ``gate_consistency.MaxDrawdownCeilingGate.MAX_MDD`` 同源口径）。
MDD_MAX = Decimal("0.35")
#: 策略最低胜率。
WIN_RATE_MIN = Decimal("0.35")
#: 年化单边换手率硬顶。
TURNOVER_MAX = Decimal("4.0")


def _to_decimal(value: Any) -> Decimal | None:
    """尽力把产物字段转为 Decimal；缺失/非法/bool 返回 None。"""
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


def _current_schema_version() -> int:
    """现行产物 schema 版本（单一事实源 = ``reporting.registry.SCHEMA_VERSION``；延迟导入避环）。"""
    from reporting.registry import SCHEMA_VERSION

    return int(SCHEMA_VERSION)


def evaluate_acceptance(
    artifact_path: str | Path,
    *,
    require_signed: bool = True,
) -> tuple[bool, list[dict[str, Any]], dict[str, Any]]:
    """对准入产物做判定：**先验证来源，再判指标**。

    Returns:
        ``(ok, criteria, meta)``：``ok`` 为全部判据通过；``criteria`` 为逐条判据结果；
        ``meta`` 含 run_id / 关键指标。
    """
    record = load_artifact(artifact_path)
    metrics = record.get("metrics") or {}
    run_id = str(record.get("run_id", Path(str(artifact_path)).name))

    criteria: list[dict[str, Any]] = []

    def _add(cid: str, name: str, value: Any, threshold: str, passed: bool, reason: str) -> None:
        criteria.append({
            "id": cid, "name": name, "value": value,
            "threshold": threshold, "passed": passed, "reason": reason,
        })

    # ① 防篡改签名验签（⛔ 准入层必须看签名，否则改数字即可洗白超限产物）
    if require_signed:
        from .tamper_guard import verify_run_signature

        sig_ok, sig_msg = verify_run_signature(record)
        _add("PROV-SIG", "防篡改签名验签", record.get("anti_tamper_signature"), "anti_tamper_signature 有效",
             sig_ok, sig_msg)

    # ② schema_version 必须存在且为现行值（legacy ⇒ FAIL，⛔ 不得 PASS）
    expected_sv = _current_schema_version()
    sv = record.get("schema_version")
    sv_ok = sv == expected_sv
    _add("SCHEMA", "产物 schema_version", sv, f"== {expected_sv}", sv_ok,
         f"schema_version={sv}（legacy/缺失 ⇒ 无内容寻址出处，不得准入）" if not sv_ok
         else f"schema_version={sv}（现行）")

    # ③ 终态与 error 一致性
    status = str(record.get("status", ""))
    _add("STATUS", "运行终态", status, '== "FINISHED"', status == "FINISHED",
         f"status={status!r}（仅 FINISHED 可准入）" if status != "FINISHED" else "status=FINISHED")
    err = record.get("error")
    _add("NO-ERROR", "终态无 error", err, "error is None", err is None,
         "FINISHED 却带 error 字段（状态自相矛盾）" if err is not None else "无 error")

    # ④ 回撤数值健全性（堵 mdd=-0.30 / >1 / null / bool / 字符串）
    mdd_raw = metrics.get("max_drawdown")
    mdd = _to_decimal(mdd_raw)
    mdd_sane = mdd is not None and Decimal("0") <= mdd <= Decimal("1")
    _add("MDD-SANITY", "回撤数值健全", None if mdd is None else str(mdd),
         "0 <= max_drawdown <= 1（数值，非 null/bool/字符串）", mdd_sane,
         f"max_drawdown={mdd_raw!r} 非法或越界（回撤无负值/不得 >1）" if not mdd_sane
         else f"max_drawdown={mdd:.4f} ∈ [0,1]")

    # ⑤ 策略阈值（㉞：与 MDD-SANITY 对称——阈值判据必须**同时**校验值域上下界，
    #    否则 win_rate=9.9 / annual_turnover=-3.0 这类越界值能"满足阈值"而洗白通过）
    _add("G-MDD-1", "最大回撤上限", None if mdd is None else str(mdd), "max_drawdown <= 0.35",
         mdd_sane and mdd <= MDD_MAX,
         (f"MDD={mdd:.4f} {'<=' if (mdd_sane and mdd <= MDD_MAX) else '>'} 0.35"
          if mdd_sane else "回撤数值不健全，无法判定（无证据 ≠ 通过）"))

    win_rate = _to_decimal(metrics.get("win_rate"))
    win_sane = win_rate is not None and Decimal("0") <= win_rate <= Decimal("1")
    _add("WIN-SANITY", "胜率数值健全", None if win_rate is None else str(win_rate),
         "0 <= win_rate <= 1（数值，非 null/bool/字符串）", win_sane,
         f"win_rate={metrics.get('win_rate')!r} 非法或越界（胜率属比例，必在 [0,1]）" if not win_sane
         else f"win_rate={win_rate:.4f} ∈ [0,1]")
    _add("ACCEPT-WIN", "胜率下限", None if win_rate is None else str(win_rate), "0.35 <= win_rate <= 1",
         win_sane and win_rate >= WIN_RATE_MIN,
         (f"win_rate={win_rate} {'>=' if (win_sane and win_rate >= WIN_RATE_MIN) else '<'} 0.35"
          if win_sane else "胜率数值不健全，无法判定（无证据 ≠ 通过）"))

    turnover = _to_decimal(metrics.get("annual_turnover"))
    turnover_sane = turnover is not None and turnover >= Decimal("0")
    _add("TURNOVER-SANITY", "换手数值健全", None if turnover is None else str(turnover),
         "annual_turnover >= 0（数值，非 null/bool/字符串）", turnover_sane,
         f"annual_turnover={metrics.get('annual_turnover')!r} 非法或为负（换手率无负值）" if not turnover_sane
         else f"annual_turnover={turnover:.4f} >= 0")
    _add("S-1", "年化换手硬顶", None if turnover is None else str(turnover), "0 <= annual_turnover <= 4.0",
         turnover_sane and turnover <= TURNOVER_MAX,
         (f"annual_turnover={turnover} {'<=' if (turnover_sane and turnover <= TURNOVER_MAX) else '>'} 4.0"
          if turnover_sane else "换手数值不健全，无法判定（无证据 ≠ 通过）"))

    round_trips_raw = metrics.get("round_trips")
    round_trips: int | None
    try:
        round_trips = int(round_trips_raw) if round_trips_raw is not None else None
    except (TypeError, ValueError):
        round_trips = None
    _add("ACCEPT-RT", "有效成交守卫", round_trips, "round_trips > 0",
         round_trips is not None and round_trips > 0,
         f"round_trips={round_trips}" + ("（0 成交 ⇒ 平凡结果，非可准入成果）" if round_trips == 0 else "")
         if round_trips is not None else "产物缺少 round_trips，无法判定（无证据 ≠ 通过）")

    ok = all(c["passed"] for c in criteria)
    meta = {
        "run_id": run_id,
        "artifact": str(artifact_path),
        "schema_version": sv,
        "status": status,
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
    parser.add_argument("--adopt", action="store_true",
                        help="把 --artifact 登记为『已采纳产物』（晋升留证；⛔ 未通过准入则拒绝登记）")
    args = parser.parse_args(argv)

    artifact = args.artifact or args.artifact_pos
    if not artifact:
        parser.print_help()
        return 2

    if args.adopt:
        from .adoption import adopt

        try:
            entry = adopt(artifact)
        except Exception as exc:             # noqa: BLE001
            print(f"[拒绝采纳] {exc}")
            return 1
        print(f"[采纳登记] {entry['run_id']} ⇒ experiments/acceptance/ADOPTED.json（by {entry['adopted_by']}）")
        return 0

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
        print(f"  [{flag}] {c['id']:<12} {c['name']}：{c['reason']}")
    print("=" * 78)
    if ok:
        print("[准入通过] 全部判据达标，允许晋升。")
        return 0
    failed = [c["id"] for c in criteria if not c["passed"]]
    print(f"[准入失败] 命中 {len(failed)} 项未达标判据：{failed} —— BLOCK 晋升。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
