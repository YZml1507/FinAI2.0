#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""采纳登记（Adoption Registry）—— **"晋升必须留证"**（治理层 ㉗）。

三层分层的「准入 BLOCKER」要在工程上可强制，前提是"晋升"必须留下**可机读的登记**，
否则 `--acceptance` 只是一条没人跑的手工命令。

单一事实源：``experiments/acceptance/ADOPTED.json``（当前被采纳为 Phase 4 / 模拟盘候选的产物）；
每次采纳另追加一行到 ``experiments/acceptance/index.jsonl``（审计轨迹）。

纪律：

* 采纳目录为空 ⇒ **无产物被采纳** ⇒ 准入步为 **PASS / 无操作**
  （⛔ 绝不让 CI / pre-push 因"尚未采纳"而永久红）；
* 采纳写入前**必须先通过** :func:`acceptance.evaluate_acceptance`（不合格产物不得被登记）；
* 任何"启动模拟盘 / 标记已采纳 / 解锁 Phase 4"的代码路径都应先查本登记并要求 acceptance=PASS。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "ADOPTION_RELPATH",
    "ADOPTED_FILENAME",
    "adoption_dir",
    "load_adopted",
    "adopt",
    "run_adoption_gate",
]

#: 采纳登记目录（相对仓根）。
ADOPTION_RELPATH = "experiments/acceptance"
#: 当前采纳指针文件名。
ADOPTED_FILENAME = "ADOPTED.json"

_REPO_ROOT = Path(__file__).resolve().parents[2]


def adoption_dir(base_dir: Path | str | None = None) -> Path:
    """采纳登记目录（``base_dir`` 用于测试隔离；缺省为真实仓根）。"""
    root = Path(base_dir) if base_dir is not None else _REPO_ROOT
    return root / "experiments" / "acceptance"


def load_adopted(base_dir: Path | str | None = None) -> dict[str, Any] | None:
    """读取当前采纳指针；未采纳 / 指针非法 ⇒ ``None``（⇒ 准入步无操作）。

    「非法」不仅指 JSON 坏/非对象/缺 ``artifact``，也包括 ``artifact`` **非非空字符串**
    （如 ``123`` / ``None`` / ``[]`` / ``{}``）——否则下游 ``Path()``/``open()`` 会抛异常，
    把"脏指针"变成"异常 ⇒ 阻断"，与"未采纳 ⇒ 无操作"两种语义混淆。
    ⛔ fail-closed 不受影响：``run_adoption_gate`` 对**登记在册**的产物一律**重跑**准入判定，
    从不信任指针里的状态字段。
    """
    path = adoption_dir(base_dir) / ADOPTED_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                        # noqa: BLE001 —— 脏指针视同"未采纳"
        return None
    if not isinstance(data, dict):
        return None
    artifact = data.get("artifact")
    if not isinstance(artifact, str) or not artifact.strip():
        return None                          # 非非空字符串 ⇒ 契约上与"未采纳"同义
    return data


def adopt(
    artifact_path: str | Path,
    *,
    adopted_by: str | None = None,
    require_acceptance_pass: bool = True,
    base_dir: Path | str | None = None,
) -> dict[str, Any]:
    """把 ``artifact_path`` 登记为"已采纳产物"（晋升留证）。

    ⛔ 默认**先跑准入**：未通过 ``evaluate_acceptance`` 的产物不得被登记（拒绝"洗白"）。

    Raises:
        FileNotFoundError: 产物不存在。
        PermissionError: ``require_acceptance_pass`` 为真且该产物准入未通过。
    """
    from .acceptance import evaluate_acceptance

    art = Path(artifact_path)
    if not art.exists():
        candidate = (Path(base_dir) if base_dir is not None else _REPO_ROOT) / art
        if not candidate.exists():
            raise FileNotFoundError(f"待采纳产物不存在: {artifact_path}")
        art = candidate

    if require_acceptance_pass:
        ok, criteria, _ = evaluate_acceptance(art)
        if not ok:
            failed = [c["id"] for c in criteria if not c["passed"]]
            raise PermissionError(f"产物未通过准入，拒绝登记采纳（命中 {failed}）")

    record = json.loads(art.read_text(encoding="utf-8"))
    run_id = str(record.get("run_id", "")) or art.stem
    entry = {
        "run_id": run_id,
        "artifact": str(art),
        "adopted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "adopted_by": adopted_by or os.environ.get("USERNAME") or os.environ.get("USER", "unknown"),
    }
    directory = adoption_dir(base_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ADOPTED_FILENAME).write_text(
        json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    with (directory / "index.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def run_adoption_gate(base_dir: Path | str | None = None) -> tuple[bool, str, dict[str, Any]]:
    """准入步（CI / pre-push 接线）：仅对**已被采纳登记**的产物跑 acceptance。

    Returns:
        ``(ok, message, meta)``；**采纳目录为空 ⇒ ``(True, ...)`` 无操作**
        （⛔ 不阻断——否则 CI 会因"尚未采纳"永久红）。
    """
    adopted = load_adopted(base_dir)
    if adopted is None:
        return True, "未采纳任何产物（experiments/acceptance/ 为空）⇒ 准入步无操作（PASS）", {"adopted": None}

    from .acceptance import evaluate_acceptance

    artifact = adopted.get("artifact")
    try:
        ok, criteria, _meta = evaluate_acceptance(artifact)
    except Exception as exc:                 # noqa: BLE001
        return False, f"已采纳产物准入判定异常: {exc}", {"adopted": adopted}
    failed = [c["id"] for c in criteria if not c["passed"]]
    if ok:
        return True, f"已采纳产物 {adopted.get('run_id')} 准入 PASS", {"adopted": adopted}
    return False, f"已采纳产物 {adopted.get('run_id')} 准入 FAIL：{failed}", {"adopted": adopted}
