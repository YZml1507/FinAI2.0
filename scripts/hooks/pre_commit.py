#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pre-commit Hook: 本地提交硬拦截门禁（FinAI2.0 防伪硬化）

在 git commit 时自动触发，执行以下刚性检查：
1. 母库只读区 finai/sources/ 下 FINDING- 守卫行数严格恒等于 370 行；
2. 若暂存区包含 tasks.md，严审所有勾选任务的防伪签名与交付证据；
3. 若暂存区包含回测产物 (runs/*.json)，严审防篡改密码学签名与出处三件套；
4. 暂存区 Python 代码 AST 语法树检查，杜绝低级语法错误提交。
"""

from __future__ import annotations

import os
import py_compile
import subprocess
import sys
from pathlib import Path

# 确保支持 utf-8 终端输出
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 确保能加载 scripts.gates 模块
repo_root = Path(__file__).resolve().parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from scripts.gates.tamper_guard import (
    check_mother_library_guard,
    verify_run_signature,
    verify_tasks_markdown,
)


def get_staged_files() -> list[str]:
    """获取 git 暂存区文件列表"""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            text=True,
            cwd=str(repo_root),
        )
        return [f.strip() for f in out.splitlines() if f.strip()]
    except Exception:
        return []


def run_pre_commit_checks(
    staged_files: list[str] | None = None,
    repo_root: str | Path | None = None,
) -> tuple[bool, list[str]]:
    """执行全部 pre-commit 门禁检验。返回 (all_passed, error_messages)

    Args:
        staged_files: 暂存文件名列表（相对仓根）；``None`` 时读 git 暂存区。
        repo_root: 仓根覆盖（⛔ 测试必须传 ``tmp_path``，不得写真实仓库）；
            ``None`` 时回退到本模块所在仓库根。
    """
    base_root = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parent.parent.parent

    errors: list[str] = []
    if staged_files is None:
        staged_files = get_staged_files()

    print("[PRE-COMMIT] 正在执行 FinAI2.0 提交前防伪与质量硬门禁检查...")

    # 1. 母库只读区 370 行红线检查（每次提交必检）
    ok_m, cnt_m, msg_m = check_mother_library_guard(base_root)
    if not ok_m:
        errors.append(f"母库红线违约: {msg_m}")
    else:
        print(f"  [PASS] 母库只读区 FINDING- 守卫: 严格保持 {cnt_m} 行")

    # 2. tasks.md 防伪签名检查（若暂存区涉及 tasks.md）
    tasks_staged = [f for f in staged_files if f.endswith("tasks.md")]
    for tf in tasks_staged:
        full_path = base_root / tf
        if full_path.exists():
            ok_t, viols_t, stats_t = verify_tasks_markdown(full_path)
            if not ok_t:
                errors.append(f"tasks.md [{tf}] 检出 {len(viols_t)} 项防伪签名违规:\n    " + "\n    ".join(viols_t))
            else:
                print(f"  [PASS] {tf}: {stats_t['checked_tasks']} 项勾选任务防伪签名全量合规")

    # 3. 回测落盘产物防篡改签名检查（若暂存区涉及 runs/*.json）
    runs_staged = [f for f in staged_files if f.endswith(".json") and ("runs/" in f.replace("\\", "/") or "experiments/" in f.replace("\\", "/"))]
    for rf in runs_staged:
        full_path = base_root / rf
        if full_path.exists():
            try:
                import json
                with open(full_path, "r", encoding="utf-8") as jf:
                    data = json.load(jf)
                # 仅对实验登记件做签名校验
                if "run_id" in data and "metrics" in data:
                    ok_r, msg_r = verify_run_signature(data)
                    if not ok_r:
                        errors.append(f"回测产物防篡改校验失败 [{rf}]: {msg_r}")
                    else:
                        print(f"  [PASS] {rf}: 密码学防篡改验签通过")
            except Exception as e:
                errors.append(f"回测产物解析异常 [{rf}]: {e}")

    # 4. Python 语法编译检查
    py_staged = [f for f in staged_files if f.endswith(".py")]
    for pf in py_staged:
        full_path = base_root / pf
        if full_path.exists():
            try:
                py_compile.compile(str(full_path), doraise=True)
            except py_compile.PyCompileError as e:
                errors.append(f"Python 语法错误 [{pf}]: {e}")

    if py_staged:
        print(f"  [PASS] 暂存区 {len(py_staged)} 个 Python 文件 AST 语法树无错误")

    return len(errors) == 0, errors


def main() -> None:
    passed, errors = run_pre_commit_checks()
    if not passed:
        print("\n" + "=" * 70)
        print("[BLOCKER] Pre-commit 门禁拦截已触发！禁止提交！")
        print("=" * 70)
        for err in errors:
            print(f"  [-] {err}")
        print("=" * 70)
        print("请根据上述提示修复代码或防伪证据后再行提交！\n")
        sys.exit(1)

    print("[ALL PASS] Pre-commit 所有门禁通过，允许提交！\n")


if __name__ == "__main__":
    main()
