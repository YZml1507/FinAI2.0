#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pre-push Hook: 推送前防倒退与六维门禁总检拦截（FinAI2.0 防伪硬化）

在 git push 时自动触发，执行以下刚性检查：
1. 离线回归单测套件全量执行，要求 100% PASS（0 failed）且通过数不得低于基线
   （``scripts.gates.constants.TEST_BASELINE_PASSED``，单一事实源）；
2. 六维门禁总调度器 (GateMasterAudit) 以**仓库现状真实 ctx** 运行推送期门禁，FAIL 或 INCONCLUSIVE 均阻断；
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
from scripts.gates.base import GateStatus, GateSeverity, is_blocking_result


#: 历史核准的单测最低通过基线（任何时候不得低于此数值）。
#: ⛔ 单一事实源 = ``scripts/gates/constants.TEST_BASELINE_PASSED``（全仓只此一处定义）。
from scripts.gates.constants import TEST_BASELINE_PASSED

MIN_TEST_BASELINE = TEST_BASELINE_PASSED


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

    # 硬断言 0 failed（防倒退的语义核心；仅比 passed 数会在"有 skip"时误判）
    failed_match = re.search(r"(\d+)\s+failed", output)
    failed_count = int(failed_match.group(1)) if failed_match else 0
    if failed_count > 0:
        return False, f"检出 {failed_count} 个失败用例（要求 0 failed）:\n{output[-1000:]}"

    if proc.returncode != 0:
        return False, f"单测套件执行失败 (exit code: {proc.returncode}):\n{output[-1000:]}"

    # 正则提取 passed 数量，如 "742 passed in 18.84s"
    match = re.search(r"(\d+)\s+passed", output)
    if not match:
        return False, f"未能从 pytest 输出中解析出 passed 数量:\n{output[-500:]}"

    passed_count = int(match.group(1))
    if passed_count < baseline:
        return False, f"单测通过数 ({passed_count}) 低于法定基线 ({baseline})！检测到测试用例倒退！"

    return True, f"回归单测 100% 全绿 (0 failed，实际通过: {passed_count} passed，高于基线 {baseline})"


def _build_pre_push_context() -> tuple[dict, str]:
    """（薄封装）推送期门禁 ctx：复用 ``context_builder.build_repo_context``（与 CI 同源，㉖）。"""
    from scripts.gates.context_builder import build_repo_context

    return build_repo_context(repo_root)


def run_master_gate_guard() -> tuple[bool, str]:
    """运行六维门禁体系总检（以仓库现状构造的真实 ctx），确保无阻断违规。

    三层分层（M3 任务 1）：:data:`WARN_GATE_IDS`（如 ``G-MDD-1``）**展示但不阻断**——
    推送代码不产生回撤，G-MDD-1 属研究质量门禁，其 BLOCKER 能力保留在准入层
    （``scripts/gates/acceptance.py``）。其余门禁 ``FAIL``/``INCONCLUSIVE`` 照常阻断。
    """
    print("[PRE-PUSH] 2. 正在运行六维防伪门禁体系全量自检 (Gate Master Audit)...")
    ctx, source = _build_pre_push_context()
    print(f"[PRE-PUSH]    门禁取证来源: {source}（ctx 键 {len(ctx)} 个）")
    # ㉓ 归属：推送期只跑"能真取证"的门禁子集（静态 + WARN）；其余归回测后 + 定时全量 CI
    master = GateMasterAudit(gates=GateMasterAudit.get_push_time_gates())
    results = master.audit(context=ctx, strict=False)

    from scripts.gates.context_builder import WARN_GATE_IDS

    # ⛔ INCONCLUSIVE（门禁适用但证据不足）与 FAIL 同为"未通过"，必须阻断（⑦：退出码不得与展示背离）；
    #    但 WARN 门禁（G-MDD-1）降级为展示——不阻断推送。
    warn_results = [
        r for r in results
        if r.gate_id in WARN_GATE_IDS and r.status in (GateStatus.FAIL, GateStatus.INCONCLUSIVE, GateStatus.WARNING)
    ]
    blockers = [
        r for r in results
        if is_blocking_result(r) and r.gate_id not in WARN_GATE_IDS
    ]

    # WARN 必须**明确打印**，让人一直看得见（不得隐藏）。
    for w in warn_results:
        print(f"[WARN] {w.gate_id} {w.message}")

    if blockers:
        msgs = [f"[{b.gate_id}/{b.status.value}] {b.name}: {b.message}" for b in blockers]
        return False, f"检出 {len(blockers)} 项阻断性门禁未通过（FAIL/INCONCLUSIVE）:\n    " + "\n    ".join(msgs)

    pass_cnt = sum(1 for r in results if r.status == GateStatus.PASS)
    warn_note = f"，WARN {len(warn_results)} 项（展示不阻断）" if warn_results else ""
    # ⓿ 口径必须显式：``len(results)`` 是**推送期子集**（PUSH_TIME_GATE_IDS），
    #    ⛔ 不得打印成"共注册 N 道门禁"——那会让人以为全库只有这么几道（口径混用）。
    push_cnt = len(results)
    total_cnt = len(GateMasterAudit.get_standard_gates())
    deferred_cnt = total_cnt - push_cnt
    deferred_note = (
        f"；另有 {deferred_cnt} 道需回测证据的门禁不在推送期校验（见定时全量审计）"
        if deferred_cnt > 0 else ""
    )
    return True, (
        f"六维门禁总检通过（推送期 {push_cnt} 道 / 全库 {total_cnt} 道；"
        f"PASS: {pass_cnt}{warn_note}{deferred_note}）"
    )


def _write_bypass_audit() -> dict:
    """逃生阀留痕**落盘**（stdout 会丢 ⇒ 必须写 runs/gate_bypass_audit.jsonl）。"""
    import datetime as _dt
    import json
    import subprocess

    operator = os.environ.get("USERNAME") or os.environ.get("USER", "unknown")
    try:
        git_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:                       # noqa: BLE001
        git_head = ""
    try:
        changed = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=str(repo_root), text=True, stderr=subprocess.DEVNULL
        ).splitlines()
    except Exception:                       # noqa: BLE001
        changed = []

    record = {
        "timestamp": _dt.datetime.now().astimezone().isoformat(),
        "operator": operator,
        "git_head": git_head,
        "skipped_scope": "Gate Master Audit (六维门禁总检)",
        "reason": os.environ.get("FINAI_SKIP_PUSH_GATES_REASON", ""),
        "changed_files_count": len(changed),
        "changed_files": changed[:200],
    }
    audit_path = repo_root / "runs" / "gate_bypass_audit.jsonl"
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:                # noqa: BLE001
        print(f"[!!! 告警：逃生阀留痕落盘失败: {exc}]")
    return record


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

    # 2. 六维门禁总检（显式逃生阀：FINAI_SKIP_PUSH_GATES=1，醒目告警 + **落盘**留痕，⛔ 不得静默）
    if os.environ.get("FINAI_SKIP_PUSH_GATES", "").strip().lower() in ("1", "true", "yes", "on"):
        import datetime as _dt

        record = _write_bypass_audit()
        print("=" * 70)
        print("[!!! 紧急逃生阀已开启 !!!] FINAI_SKIP_PUSH_GATES=1 —— 跳过六维门禁总检")
        print("[!!! 告警：本次推送未经门禁保护，禁止用于常规提交，仅限紧急恢复 !!!]")
        print(f"[!!! 留痕] 时间={record['timestamp']}；操作者={record['operator']}；"
              f"git HEAD={record['git_head']}；跳过={record['skipped_scope']}")
        print(f"[!!! 留痕已落盘] runs/gate_bypass_audit.jsonl（本次共 {record['changed_files_count']} 个改动文件）")
        print("=" * 70)
    else:
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
