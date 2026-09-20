# -*- coding: utf-8 -*-
"""共享秘密加载器（安全加固：仓库内不再硬编码任何 API key/token）。

纪律：
- ``require_env(name)`` 先查 ``os.environ``，再回落解析仓根 ``.env``
  （``KEY=VALUE`` 行，剥离引号/空白），找不到 ⇒ ``SystemExit`` fail-closed。
- ⛔ 任何错误消息不得回显秘密值本身（只回显变量名与文件名）。
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_FILE = _REPO_ROOT / ".env"


def _read_env_file(name: str) -> str | None:
    """从仓根 .env 解析 KEY=VALUE（不带 set/export 前缀也行），缺失返回 None。"""
    if not _ENV_FILE.exists():
        return None
    for raw in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == name:
            return v.strip().strip("'\"")
    return None


def require_env(name: str) -> str:
    """取秘密值：环境变量优先，.env 回落；缺失 fail-closed（不回显值）。"""
    v = os.environ.get(name)
    if v:
        return v
    v = _read_env_file(name)
    if v:
        return v
    raise SystemExit(
        f"[FAIL-CLOSED] 缺少秘密变量 {name}：请写入环境变量或仓根 .env"
        f"（{_ENV_FILE.name} 不入库）。⛔ 仓库内不得硬编码秘密。")
