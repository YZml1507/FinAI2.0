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
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "hash_path_manifest",
    "hash_sequence",
    "repro_fingerprint",
]

#: 读取文件内容的块大小（大 parquet 也不整块入内存）。
_CHUNK_BYTES = 1 << 20


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


def hash_path_manifest(root: Path | str, *, patterns: tuple[str, ...] = ("**/*",)) -> str:
    """数据快照指纹：对 ``root`` 下所有匹配文件按 ``(相对路径, 内容 SHA-256)`` 排序聚合。

    ⛔ 相对路径**必须**入哈希：symbol 集合本身变化（增删标的）也必须改变指纹
    （报告 §6.1(A)）。

    Args:
        root: 快照根目录（不存在时返回空清单的哈希，不抛）。 
        patterns: ``glob`` 模式元组，默认全部文件。

    Returns:
        清单聚合哈希的 SHA-256 前 16 hex。
    """
    base = Path(root)
    items: list[tuple[str, str]] = []
    if base.exists():
        for pat in patterns:
            for p in sorted(base.glob(pat)):
                if p.is_file():
                    rel = str(p.relative_to(base)).replace("\\", "/")
                    items.append((rel, _sha256_file(p)))
    items.sort()
    blob = "\n".join(f"{rel}:{h}" for rel, h in items)
    return sha256(blob.encode("utf-8")).hexdigest()[:16]


def hash_sequence(values: Iterable[Any], *, label: str) -> str:
    """对序列（交易日历日期表 / 候选池码表）做**顺序敏感**的确定性哈希。

    顺序是语义的一部分（日历顺序、码表顺序），故不入排序。``values`` 逐元素 ``str()``。

    Args:
        values: 可迭代序列（如 ``list[date]``、``list[str]``）。
        label: 命名空间标签（防不同语义的序列撞哈希，如 ``"cal"`` / ``"universe"``）。

    Returns:
        序列哈希的 SHA-256 前 16 hex。
    """
    blob = str(label) + "|" + "|".join(str(v) for v in values)
    return sha256(blob.encode("utf-8")).hexdigest()[:16]


def repro_fingerprint(
    *,
    params_hash: str,
    code_hash: str,
    data_hash: str,
    calendar_hash: str,
    universe_hash: str,
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
    """
    parts = {
        "params_hash": str(params_hash),
        "code_hash": str(code_hash),
        "data_hash": str(data_hash),
        "calendar_hash": str(calendar_hash),
        "universe_hash": str(universe_hash),
        "seed": ("noseed" if seed is None else str(seed)),
    }
    text = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return sha256(text.encode("utf-8")).hexdigest()[:32]
