#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pre-push Hook: 推送前防倒退与六维门禁总检拦截（FinAI2.0 防伪硬化）

在 git push 时自动触发，执行以下刚性检查：
1. 离线回归单测套件全量执行，要求 100% PASS 且通过数不得低于基线 (当前基线 699 passed)；
2. 六维门禁总调度器 (GateMasterAudit) 自动化运行，严禁存在 BLOCKER 或 CRITICAL 违约；
3. 任一条件不满足坚决拒绝向远端仓库推送。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# 确保支持 utf-8 终端输出
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

repo_root = Path(__file__).resolve().parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from scripts.gates.gate_master_audit import GateMasterAudit
from scripts.gates.base import GateStatus, GateSeverity


#: 历史核准的单测最低通过基线（任何时候不得低于此数值）
MIN_TEST_BASELINE = 717


def run_pytest_guard(baseline: int = MIN_TEST_BASELINE) -> tuple[bool, str]:
    """运行全量离线单测并断言通过数不低于基线"""
    print(f"[PRE-PUSH] 1. 正在运行全量离线回归单测套件 (最低基线: {baseline} passed)...")
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/",
        "-p",
        "no:ddtrace",
        "-p",
        "no:ddtrace.pytest_bdd",
        "-q",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "回归测试执行超时 (>120s)"
    except Exception as e:
        return False, f"回归测试启动异常: {e}"

    output = proc.stdout + "\n" + proc.stderr
    if proc.returncode != 0:
        return False, f"单测套件执行失败 (exit code: {proc.returncode}):\n{output[-1000:]}"

    # 正则提取 passed 数量，如 "699 passed in 18.84s"
    match = re.search(r"(\d+)\s+passed", output)
    if not match:
        return False, f"未能从 pytest 输出中解析出 passed 数量:\n{output[-500:]}"

    passed_count = int(match.group(1))
    if passed_count < baseline:
        return False, f"单测通过数 ({passed_count}) 低于法定基线 ({baseline})！检测到测试用例倒退！"

    return True, f"回归单测 100% 全绿 (实际通过: {passed_count} passed，高于基线 {baseline})"


def run_master_gate_guard() -> tuple[bool, str]:
    """运行六维门禁体系总检，确保无阻断违规"""
    print("[PRE-PUSH] 2. 正在运行六维防伪门禁体系全量自检 (Gate Master Audit)...")
    master = GateMasterAudit()
    results = master.audit(context={}, strict=False)

    blockers = [
        r for r in results
        if r.status == GateStatus.FAIL and r.severity in (GateSeverity.BLOCKER, GateSeverity.CRITICAL)
    ]

    if blockers:
        msgs = [f"[{b.gate_id}] {b.name}: {b.message}" for b in blockers]
        return False, f"检出 {len(blockers)} 项阻断性门禁失败:\n    " + "\n    ".join(msgs)

    pass_cnt = sum(1 for r in results if r.status == GateStatus.PASS)
    return True, f"六维门禁总检通过 (共注册 {len(results)} 道门禁，PASS: {pass_cnt})"


def main() -> None:
    print("=" * 70)
    print("FinAI2.0 推送前防倒退与门禁硬核验 (Pre-Push Gate Guard)")
    print("=" * 70)

    # 1. 单测防倒退
    ok_test, msg_test = run_pytest_guard()
    if not ok_test:
        print(f"[-] [BLOCKED] {msg_test}")
        print("=" * 70)
        sys.exit(1)
    print(f"[PASS] {msg_test}")

    # 2. 六维门禁总检
    ok_gate, msg_gate = run_master_gate_guard()
    if not ok_gate:
        print(f"[-] [BLOCKED] {msg_gate}")
        print("=" * 70)
        sys.exit(1)
    print(f"[PASS] {msg_gate}")

    print("=" * 70)
    print("[ALL PASS] 验证完毕，允许向远端仓库推送！\n")


if __name__ == "__main__":
    main()
