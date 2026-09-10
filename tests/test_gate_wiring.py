#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""接线元测试（Wiring Meta-Test）—— 让"建了机制没接线"自动化暴露（㉜ 任务 3）。

**要治的病**：`scripts/gates/adoption.py` 建好了采纳登记与 `run_adoption_gate`，
但 `grep run_adoption_gate scripts/hooks/ scripts/gates/gate_master_audit.py .github/` **零命中**
⇒ 「准入 BLOCKER」没有任何自动路径调用 ⇒ "MDD 超限不得晋升"**不可强制**。
这类"声明了却没接线"的失效**不能靠人撞**，必须由测试自动暴露。

**判据**：

1. 每个"已声明的治理入口"必须能在**真实调用路径**上被找到（符号名，或 CLI 模块调用）；
2. "定义了但零调用" ⇒ 测试失败并**打印符号名**；
3. 确有理由不接线的 ⇒ 进入显式白名单，且**理由非空**（白名单本身可审查）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    path = _REPO_ROOT / relpath
    assert path.is_file(), f"接线元测试目标文件不存在: {relpath}"
    return path.read_text(encoding="utf-8")


# =====================================================================
# 1. 治理入口 → 真实调用路径（逐条断言，缺任一条即红）
# =====================================================================

#: (符号, 应有引用它的文件, 该文件中的**引用形态**) —— 引用形态允许是符号名，
#: 或（对 CLI 门禁）`python -m <模块>` 的模块路径（模块 `main()` 再调符号，
#: 由 :func:`test_cli_main_delegates_to_gate_symbol` 单独锁定该链条末端）。
_WIRING_REQUIREMENTS: tuple[tuple[str, str, str], ...] = (
    # ㉜ 采纳准入步：pre-push 直接调用 + 两处 CI 经 CLI 调用
    ("run_adoption_gate", "scripts/hooks/pre_push.py", "run_adoption_gate"),
    ("run_adoption_gate", ".github/workflows/ci.yml", "scripts.gates.adoption"),
    ("run_adoption_gate", ".github/workflows/scheduled_audit.yml", "scripts.gates.adoption"),
    # 准入判定：被采纳登记层引用（→ 经 run_adoption_gate 挂到 pre-push / CI）
    ("evaluate_acceptance", "scripts/gates/adoption.py", "evaluate_acceptance"),
    # CI 策略：被总调度器 --ci 引用
    ("ci_policy", "scripts/gates/gate_master_audit.py", "ci_policy"),
    # 阻断判定：pre-push 与 CI 总调度器均须引用
    ("is_blocking_result", "scripts/hooks/pre_push.py", "is_blocking_result"),
    ("is_blocking_result", "scripts/gates/gate_master_audit.py", "is_blocking_result"),
)


class TestGovernanceEntrypointsAreWired:
    """已声明的治理入口必须出现在真实调用路径上（⛔ 不得"定义了零调用"）。"""

    @pytest.mark.parametrize("symbol,relpath,token", _WIRING_REQUIREMENTS,
                             ids=[f"{s}@{p}" for s, p, _ in _WIRING_REQUIREMENTS])
    def test_symbol_referenced_in_call_path(self, symbol: str, relpath: str, token: str):
        text = _read(relpath)
        assert token in text, (
            f"治理入口 `{symbol}` 未在 `{relpath}` 中被引用"
            f"（期望出现 `{token}`）—— 机制建了但没接线"
        )

    def test_adoption_module_is_invoked_as_cli_in_workflows(self):
        """两处 workflow 必须以**可执行命令**（``python -m scripts.gates.adoption``）调用，
        ⛔ 不得只是注释里提一嘴。"""
        for relpath in (".github/workflows/ci.yml", ".github/workflows/scheduled_audit.yml"):
            text = _read(relpath)
            lines = [
                ln for ln in text.splitlines()
                if "scripts.gates.adoption" in ln and not ln.lstrip().startswith("#")
            ]
            assert lines, f"{relpath} 中 `scripts.gates.adoption` 只出现在注释里（未真正接线）"
            assert any("python" in ln for ln in lines), f"{relpath} 未以 python 命令调用采纳门禁"

    def test_cli_main_delegates_to_gate_symbol(self):
        """CLI `main()` 必须真的委托给门禁符号（否则 CI 跑了 CLI 也等于没跑门禁）。"""
        text = _read("scripts/gates/adoption.py")
        assert re.search(r"def main\(", text), "adoption.py 缺少 CLI main()"
        assert "run_adoption_gate(" in text, "adoption.main() 未调用 run_adoption_gate ⇒ CLI 空转"

    def test_pre_push_main_calls_adoption_guard(self):
        """pre-push 的 main() 必须实际调用 `run_adoption_guard()`（否则只是定义了个函数）。"""
        text = _read("scripts/hooks/pre_push.py")
        assert "run_adoption_guard()" in text, "pre_push.main() 未调用 run_adoption_guard ⇒ 未接线"


# =====================================================================
# 2. 「定义了但零调用」通用侦查（不靠人工具名清单）
# =====================================================================

#: 需被"定义模块之外"引用的治理符号 → 其定义模块。
_GOVERNANCE_SYMBOLS: dict[str, str] = {
    "run_adoption_gate": "scripts/gates/adoption.py",
    "adopt": "scripts/gates/adoption.py",
    "load_adopted": "scripts/gates/adoption.py",
    "evaluate_acceptance": "scripts/gates/acceptance.py",
    "ci_policy": "scripts/gates/context_builder.py",
    "is_blocking_result": "scripts/gates/base.py",
    "ci_blocking": "scripts/gates/gate_repro.py",      # ㉝：该字段必须被 ci_policy 真正读取
}

#: 显式白名单：确有不接线理由的符号（⛔ 理由必须非空，白名单本身可审查）。
_WIRING_WHITELIST: dict[str, str] = {
    "adopt": "登记动作由 `acceptance --adopt` CLI 与 `run_adoption_gate` 的对抗路径调用；"
             "生产晋升路径本就是人工触发（须人签字），不做自动接线。",
}

#: 扫描面（真实调用路径所在位置）。
_SCAN_GLOBS: tuple[str, ...] = (
    "scripts/**/*.py",
    ".github/workflows/*.yml",
)


def _iter_scan_files() -> list[Path]:
    files: list[Path] = []
    for pattern in _SCAN_GLOBS:
        files.extend(p for p in _REPO_ROOT.glob(pattern) if p.is_file())
    return files


class TestNoOrphanGovernanceSymbols:
    """「定义了但零调用」⇒ 失败并打印符号名（白名单须带理由）。"""

    def test_every_governance_symbol_has_a_caller_outside_its_module(self):
        offenders: list[str] = []
        for symbol, defining in _GOVERNANCE_SYMBOLS.items():
            if symbol in _WIRING_WHITELIST:
                continue
            referenced = False
            for p in _iter_scan_files():
                rel = str(p.relative_to(_REPO_ROOT)).replace("\\", "/")
                if rel == defining:
                    continue                     # 同模块内的自引用不算"接线"
                if symbol in p.read_text(encoding="utf-8", errors="ignore"):
                    referenced = True
                    break
            if not referenced:
                offenders.append(f"{symbol}（定义于 {defining}，模块外零引用）")
        assert not offenders, f"定义了但零调用的治理符号（建了机制没接线）: {offenders}"

    def test_ci_blocking_field_is_actually_read(self):
        """㉝：`ci_blocking` 若被写入门禁产物，必须被 CI 策略**真正读取**——
        ⛔ 不许保留"看起来生效、实际不生效"的死字段。"""
        writer = _read("scripts/gates/gate_repro.py")
        reader = _read("scripts/gates/context_builder.py")
        assert "ci_blocking" in writer, "前置：gate_repro 应声明 ci_blocking"
        assert "ci_blocking" in reader, "⛔ ci_blocking 只被写、从未被 ci_policy 读取（死字段）"

    def test_whitelist_reasons_are_non_empty(self):
        for symbol, reason in _WIRING_WHITELIST.items():
            assert reason.strip(), f"接线白名单 {symbol} 缺少理由"
            assert symbol in _GOVERNANCE_SYMBOLS, f"白名单 {symbol} 不在受检符号集合内（多余条目）"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
