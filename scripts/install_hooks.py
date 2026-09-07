#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Git Hooks 一键安装与防伪装配脚本 (Install Git Hooks)

功能：
1. 自动配置 git config core.hooksPath .githooks；
2. 同步写入 .git/hooks/ 作为双重保障；
3. 支持 --verify 验签测试；
4. 支持 --uninstall 卸载清理。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent


# 保证支持 utf-8 终端输出和模块导入
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))


def install_hooks() -> bool:
    """安装与激活 Git 门禁钩子"""
    print("=" * 70)
    print("正在为 FinAI2.0 装配 Git 自动化防伪门禁钩子...")
    print("=" * 70)

    githooks_dir = repo_root / ".githooks"
    git_hooks_fallback = repo_root / ".git" / "hooks"

    if not githooks_dir.exists():
        print(f"[ERROR] 门禁钩子目录不存在: {githooks_dir}")
        return False

    # 1. 配置 core.hooksPath
    try:
        subprocess.check_call(["git", "config", "core.hooksPath", ".githooks"], cwd=str(repo_root))
        print("[OK] 已配置 git config core.hooksPath = .githooks")
    except Exception as e:
        print(f"[WARN] git config core.hooksPath 配置失败: {e}")

    # 2. 复制到 .git/hooks/ 作为兜底
    if git_hooks_fallback.exists():
        for hook_name in ["pre-commit", "pre-push"]:
            src = githooks_dir / hook_name
            dst = git_hooks_fallback / hook_name
            if src.exists():
                shutil.copy2(src, dst)
                try:
                    # Linux/macOS 可执行权限赋予
                    os.chmod(dst, 0o755)
                except Exception:
                    pass
                print(f"[OK] 已同步兜底钩子: {dst}")

    print("=" * 70)
    print("[SUCCESS] Git 防伪硬拦截门禁钩子装配完成！")
    return True


def verify_hooks() -> bool:
    """验证钩子安装与执行状态"""
    print("=" * 70)
    print("正在核验 Git 门禁钩子配置与运行状态...")
    print("=" * 70)

    try:
        out = subprocess.check_output(["git", "config", "core.hooksPath"], text=True, cwd=str(repo_root)).strip()
        print(f"1. core.hooksPath: {out} (期望: .githooks)")
        if out != ".githooks":
            print("[FAIL] core.hooksPath 配置不正确！")
            return False
    except Exception as e:
        print(f"[FAIL] 读取 git config 失败: {e}")
        return False

    # 执行 pre_commit 检查 dry run
    from scripts.hooks.pre_commit import run_pre_commit_checks
    ok, errs = run_pre_commit_checks(staged_files=[])
    if ok:
        print("2. Pre-commit 自检: PASS (母库 370 行守卫安全)")
    else:
        print(f"2. Pre-commit 自检: FAIL ({errs})")
        return False

    print("=" * 70)
    print("[ALL PASS] Git 门禁钩子工作正常，防伪硬拦截生效！")
    return True


def uninstall_hooks() -> bool:
    """卸载 Git 门禁钩子"""
    try:
        subprocess.call(["git", "config", "--unset", "core.hooksPath"], cwd=str(repo_root))
        print("[OK] 已移除 git config core.hooksPath")
        return True
    except Exception as e:
        print(f"[FAIL] 卸载失败: {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="FinAI2.0 Git Hooks 安装器")
    parser.add_argument("--verify", action="store_true", help="校验已安装的钩子状态")
    parser.add_argument("--uninstall", action="store_true", help="卸载 Git 钩子")
    args = parser.parse_args()

    if args.uninstall:
        uninstall_hooks()
        return

    if args.verify:
        ok = verify_hooks()
        if not ok:
            sys.exit(1)
        return

    ok = install_hooks()
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
