#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""出处四要素的内容寻址实现（Content-addressed Provenance）。

根因（`docs/audit/repro_root_cause.md` §1）：``params_hash`` 只哈希 ``params`` 一个实参，
真正决定回测结果的**输入数据内容**与**代码/候选池状态**却只是两个手写常量标签，
于是出现"同参 ≠ 同输入"，4 份产物 ``params_hash`` 全同却给出 3 种互斥结果（PM-1）。

本模块把四类输入变成**内容寻址哈希**（纯函数、零 IO 依赖注入、可离线单测）：

* :func:`hash_path_manifest` —— 数据快照指纹（路径 + 文件内容 SHA-256 聚合）；
* :func:`hash_sequence`      —— 交易日历 / 候选池码表等顺序敏感序列的确定性哈希；
* :func:`repro_fingerprint`  —— 把上述四要素 + ``params_hash`` + ``seed`` 聚合为**完整出处键**：
  任何输入变动 ⇒ 键变动 ⇒ "同参同输入必得同结果"从"未证明"变为"可被门禁强制"。

**缺失即显式失败（Fail-Closed）**：本模块**绝不**把"输入缺失"包装成一个看似合法的哈希值
（第 1~9 层反复出现的病：无证据 ⇒ 产出一个合法外观的值）。具体约定：

* 快照根**不存在** ⇒ 抛 :class:`MissingDataError`（路径写错/被误删 ⇒ 响亮失败）；
* 快照**匹配到 0 个文件**（空目录）⇒ 返回 ``None``（⇒ 上层出处键为 ``None``）；
* 序列**为空** ⇒ 返回 ``None``；
* :func:`repro_fingerprint` 任一要素为 ``None`` ⇒ 抛 ``ValueError``（⛔ 不 ``str(None)`` 兜底）。

> ⛔ 旧实现对"不存在/空目录"返回 ``sha256("")[:16]`` 常量 ⇒ 任何数据不可用都坍缩到**同一**指纹，
> 且非空字符串能骗过 ``registry`` 的真值守卫——这正是本次修复要根除的静默兜底。
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "MissingDataError",
    "hash_path_manifest",
    "hash_sequence",
    "repro_fingerprint",
]

#: 读取文件内容的块大小（大 parquet 也不整块入内存）。
_CHUNK_BYTES = 1 << 20


class MissingDataError(RuntimeError):
    """内容寻址的输入缺失（快照根不存在等）。

    ⛔ 不得被静默吞掉后坍缩为常量哈希——"无证据"必须表现为**缺失**（``None`` / 抛错），
    而不是"一个合法外观的值"。
    """


def _sha256_file(path: Path, *, chunk_bytes: int = _CHUNK_BYTES) -> str:
    """流式计算单文件内容 SHA-256（十六进制全串）。"""
    digest = sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def hash_path_manifest(root: Path | str, *, patterns: tuple[str, ...] = ("**/*",)) -> str | None:
    """数据快照指纹：对 ``root`` 下所有匹配文件按 ``(相对路径, 内容 SHA-256)`` 排序聚合。

    ⛔ 相对路径**必须**入哈希：symbol 集合本身变化（增删标的）也必须改变指纹
    （报告 §6.1(A)）。

    Args:
        root: 快照根目录。
        patterns: ``glob`` 模式元组，默认全部文件。

    Returns:
        清单聚合哈希的 SHA-256 前 16 hex；**匹配到 0 个文件（空目录）⇒ ``None``**。

    Raises:
        MissingDataError: ``root`` 不存在（路径写错 / 被误删）——
            ⛔ 不得坍缩为 ``sha256("")[:16]`` 常量（那样"两个不同缺失路径"会共享同一指纹）。
    """
    base = Path(root)
    if not base.exists():
        raise MissingDataError(
            f"数据快照根不存在: {base}（⛔ 不得静默坍缩为常量哈希；请检查路径/采集是否完成）"
        )
    items: list[tuple[str, str]] = []
    for pat in patterns:
        for p in sorted(base.glob(pat)):
            if p.is_file():
                rel = str(p.relative_to(base)).replace("\\", "/")
                items.append((rel, _sha256_file(p)))
    if not items:
        # 空清单 ⇒ **显式缺失**（⛔ 不返回 sha256("") 常量，否则所有"空"输入坍缩为同一指纹）。
        return None
    items.sort()
    blob = "\n".join(f"{rel}:{h}" for rel, h in items)
    return sha256(blob.encode("utf-8")).hexdigest()[:16]


def hash_sequence(values: Iterable[Any], *, label: str) -> str | None:
    """对序列（交易日历日期表 / 候选池码表）做**顺序敏感**的确定性哈希。

    顺序是语义的一部分（日历顺序、码表顺序），故不入排序。``values`` 逐元素 ``str()``。

    Args:
        values: 可迭代序列（如 ``list[date]``、``list[str]``）。
        label: 命名空间标签（防不同语义的序列撞哈希，如 ``"cal"`` / ``"universe"``）。

    Returns:
        序列哈希的 SHA-256 前 16 hex；**空序列 ⇒ ``None``**（⛔ 不返回 ``label|`` 的常量哈希）。
    """
    seq = [str(v) for v in values]
    if not seq:
        return None
    blob = str(label) + "|" + "|".join(seq)
    return sha256(blob.encode("utf-8")).hexdigest()[:16]


def repro_fingerprint(
    *,
    params_hash: str | None,
    code_hash: str | None,
    data_hash: str | None,
    calendar_hash: str | None,
    universe_hash: str | None,
    seed: int | None,
) -> str:
    """完整出处键：任何输入变动 ⇒ 键变动（报告 §6.1(A)）。

    Args:
        params_hash: 策略参数字典指纹（沿用 ``registry._params_hash`` 同一把尺）。
        code_hash: 代码内容指纹（git HEAD(+dirty)）。
        data_hash: 数据清单内容指纹。
        calendar_hash: 实际消费的交易日历指纹。
        universe_hash: 候选池时点快照指纹。
        seed: 随机种子；``None`` 归一为字面量 ``"noseed"``（与 run_id 口径一致）。

    Returns:
        聚合出处键的 SHA-256 前 32 hex。

    Raises:
        ValueError: 上述**任一哈希要素为 ``None``** ⇒ 拒绝生成。
            ⛔ 绝不 ``str(None)`` 变成字面量 ``"None"`` 参与哈希——那会把"缺失"
            包装成看似合法的指纹（违反"任何输入变动 ⇒ 键变动"）。
    """
    required = {
        "params_hash": params_hash,
        "code_hash": code_hash,
        "data_hash": data_hash,
        "calendar_hash": calendar_hash,
        "universe_hash": universe_hash,
    }
    missing = [k for k, v in required.items() if v is None]
    if missing:
        raise ValueError(
            f"repro_fingerprint 拒绝缺失要素（⛔ 不静默兜底 / 不 str(None)）: {missing}"
        )
    parts: dict[str, str] = {k: str(v) for k, v in required.items()}
    parts["seed"] = "noseed" if seed is None else str(seed)
    text = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return sha256(text.encode("utf-8")).hexdigest()[:32]
