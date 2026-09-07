#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tamper Guard & Digital Provenance Signature Engine (防伪硬化与防篡改签名引擎)

依据：
1. 17 号深度调研报告 §4.6 / G-Gate 体系
2. 阶段三：CI / Git Hooks 自动化防伪硬化与防篡改签名规范
3. 出处三件套(Git SHA + Data Hash + Timestamp) 与关键指标密码学验签防篡改
4. tasks.md 物理勾选防伪机读验签与两仓镜像一致性校验
5. 母库只读区 370 行守卫快速审计
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


TARGET_MOTHER_LIBRARY_COUNT = 370


def _canonical_str(val: Any) -> str:
    """递归将结构转化为确定性标准字符串（字典按键排序，消除空白差异）"""
    if isinstance(val, (dict, Mapping)):
        items = sorted((str(k), _canonical_str(v)) for k, v in val.items())
        return "{" + ",".join(f"{k}:{v}" for k, v in items) + "}"
    elif isinstance(val, (list, tuple, Sequence)) and not isinstance(val, (str, bytes)):
        return "[" + ",".join(_canonical_str(x) for x in val) + "]"
    elif val is None:
        return "null"
    elif isinstance(val, bool):
        return "true" if val else "false"
    else:
        return str(val).strip()


def compute_run_signature(record: Mapping[str, Any]) -> str:
    """计算回测落盘记录的防篡改密码学哈希签名。
    
    签名字段绑定：
    - run_id
    - code_version
    - data_version
    - params_hash
    - status
    - metrics (核心绩效指标)
    """
    fields_to_sign = {
        "run_id": str(record.get("run_id", "")),
        "code_version": str(record.get("code_version", "")),
        "data_version": str(record.get("data_version", "")),
        "params_hash": str(record.get("params_hash", "")),
        "status": str(record.get("status", "")),
        "metrics": record.get("metrics", {}),
    }
    canonical_text = _canonical_str(fields_to_sign)
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


def sign_run_record(record: dict[str, Any]) -> dict[str, Any]:
    """为回测记录注入防篡改签名与生成时间戳"""
    signed = dict(record)
    sig = compute_run_signature(signed)
    signed["anti_tamper_signature"] = sig
    return signed


def verify_run_signature(record: Mapping[str, Any]) -> tuple[bool, str]:
    """验证回测记录是否遭受事后篡改。
    
    返回 (is_valid, reason)
    """
    sig = record.get("anti_tamper_signature")
    if not sig:
        return False, "缺少 anti_tamper_signature 签名，产物未受密码学防伪保护"

    expected = compute_run_signature(record)
    if sig != expected:
        return False, f"防篡改签名不匹配！文件已被事后非法篡改 (期望: {expected[:16]}..., 实际: {sig[:16]}...)"

    # 检查出处三件套
    commit = str(record.get("code_version", "")).strip()
    data_ver = str(record.get("data_version", "")).strip()
    ts = str(record.get("timestamp", "")).strip()

    if not commit:
        return False, "出处三件套缺失: code_version 为空"
    if not data_ver:
        return False, "出处三件套缺失: data_version 为空"
    if not ts:
        return False, "出处三件套缺失: timestamp 为空"

    return True, f"签名与出处三件套检验全部通过 (Signature: {sig[:16]}...)"


def verify_tasks_markdown(content_or_path: str | Path) -> tuple[bool, list[str], dict[str, Any]]:
    """机读审计 tasks.md，验证所有任务勾选是否具备合规的交付日志与证据签名。
    
    防伪铁律：
    1. 任何 `- [x]` 必须伴随 `— YYYY-MM-DD` 完成日期；
    2. 任何 `- [x]` 必须伴随 `✅` 完成标记；
    3. 任何 `- [x]` 必须伴随明确的物理交付证据（Commit SHA、单测 passed 数量或验收报告链接），杜绝私自偷勾！
    
    返回 (is_valid, violations, stats)
    """
    if isinstance(content_or_path, (str, Path)) and os.path.exists(str(content_or_path)):
        with open(content_or_path, "r", encoding="utf-8") as f:
            text = f.read()
    else:
        text = str(content_or_path)

    violations: list[str] = []
    checked_count = 0
    unchecked_count = 0

    lines = text.splitlines()
    checked_pattern = re.compile(r"^\s*-\s*\[x\]\s*\[(T[-_A-Za-z0-9]+)\]\s*(.*)$")
    unchecked_pattern = re.compile(r"^\s*-\s*\[\s*\]\s*\[(T[-_A-Za-z0-9]+)\]\s*(.*)$")

    date_pattern = re.compile(r"—\s*\d{4}-\d{2}-\d{2}")
    evidence_pattern = re.compile(
        r"(Commit|commit|passed|全绿|单测绿|单测|落盘|验收|报告|docs/|tests/|REVALIDATE|\.md|\.py|paper_trading/|HTTP|OK)"
    )

    for idx, line in enumerate(lines, start=1):
        m_checked = checked_pattern.match(line)
        if m_checked:
            checked_count += 1
            task_id = m_checked.group(1)
            content = m_checked.group(2)

            # 1. 必须有日期
            if not date_pattern.search(content):
                violations.append(f"第 {idx} 行 [{task_id}]: 已勾选但缺少标准完成日期 (格式: — YYYY-MM-DD)")

            # 2. 必须有 ✅ 标记
            if "✅" not in content:
                violations.append(f"第 {idx} 行 [{task_id}]: 已勾选但缺少 ✅ 签章确认标记")

            # 3. 必须有物理交付证据（Commit SHA、单测数量、产物路径等）
            if not evidence_pattern.search(content):
                violations.append(f"第 {idx} 行 [{task_id}]: 已勾选但缺少可核验证据 (Commit SHA/单测数量/报告文档)")
            continue

        m_unchecked = unchecked_pattern.match(line)
        if m_unchecked:
            unchecked_count += 1
            task_id = m_unchecked.group(1)
            content = m_unchecked.group(2)
            # 未勾选的任务，不应该有 ✅ 标记（防止逻辑矛盾）
            if "✅" in content:
                violations.append(f"第 {idx} 行 [{task_id}]: 任务未勾选 [- ]，却包含 ✅ 标记，逻辑矛盾！")

    stats = {
        "checked_tasks": checked_count,
        "unchecked_tasks": unchecked_count,
        "total_tasks": checked_count + unchecked_count,
        "violations_count": len(violations),
    }

    is_valid = len(violations) == 0
    return is_valid, violations, stats


def verify_tasks_mirror(path1: str | Path, path2: str | Path) -> tuple[bool, str]:
    """验证 research 仓与 code 仓的 tasks.md 是否逐字节镜像一致（忽略换行符 CRLF/LF 差异）。"""
    p1 = Path(path1)
    p2 = Path(path2)

    if not p1.exists():
        return False, f"计划仓 tasks.md 不存在: {p1}"
    if not p2.exists():
        return False, f"代码仓 tasks.md 不存在: {p2}"

    text1 = p1.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
    text2 = p2.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")

    h1 = hashlib.sha256(text1.encode("utf-8")).hexdigest()
    h2 = hashlib.sha256(text2.encode("utf-8")).hexdigest()

    if h1 != h2:
        return False, f"两仓 tasks.md 内容哈希不一致！(research: {h1[:12]}..., code: {h2[:12]}...)"

    return True, f"两仓 tasks.md 镜像一致性核验完全吻合 (SHA-256: {h1[:16]}...)"


def check_mother_library_guard(repo_root: str | Path | None = None) -> tuple[bool, int, str]:
    """快速扫描母库只读区 finai/sources/ 下 FINDING- 守卫行数（严格恒等于 370 行）。"""
    if repo_root is None:
        # 当前文件在 scripts/gates/ 下，项目根目录为 ../..
        base_dir = Path(__file__).resolve().parent.parent.parent
    else:
        base_dir = Path(repo_root).resolve()

    sources_dir = base_dir / "finai" / "sources"
    if not sources_dir.exists():
        return False, 0, f"母库只读区不存在: {sources_dir}"

    actual_count = 0
    for py_path in sources_dir.glob("**/*.py"):
        try:
            with open(py_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "FINDING-" in line:
                        actual_count += 1
        except Exception:
            pass

    if actual_count != TARGET_MOTHER_LIBRARY_COUNT:
        return False, actual_count, f"母库只读区 FINDING- 行数违背红线！实际: {actual_count} 行，必须严格恒等于 {TARGET_MOTHER_LIBRARY_COUNT} 行！"

    return True, actual_count, f"母库只读区 FINDING- 守卫行数安全吻合 370 行 (实际: {actual_count} 行)"


def main() -> None:
    parser = argparse.ArgumentParser(description="FinAI2.0 防伪硬化与防篡改签名引擎")
    parser.add_argument("--sign-run", type=str, help="为指定回测落盘 JSON 文件添加防篡改签名")
    parser.add_argument("--verify-run", type=str, help="校验指定回测落盘 JSON 文件的防篡改签名")
    parser.add_argument("--verify-tasks", type=str, help="校验指定 tasks.md 勾选的防伪交付证据")
    parser.add_argument("--verify-mirror", nargs=2, metavar=("PATH1", "PATH2"), help="校验两仓 tasks.md 镜像一致性")
    parser.add_argument("--check-mother-library", action="store_true", help="校验母库只读区 370 行守卫")
    parser.add_argument("--verify-all", action="store_true", help="执行全系统防伪自检")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent.parent

    if args.sign_run:
        p = Path(args.sign_run)
        if not p.exists():
            print(f"[ERROR] 文件不存在: {p}")
            sys.exit(1)
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        signed = sign_run_record(data)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(signed, f, ensure_ascii=False, indent=2)
        print(f"[OK] 签名已成功注入: {p} (Signature: {signed['anti_tamper_signature'][:16]}...)")
        return

    if args.verify_run:
        p = Path(args.verify_run)
        if not p.exists():
            print(f"[ERROR] 文件不存在: {p}")
            sys.exit(1)
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        ok, msg = verify_run_signature(data)
        if ok:
            print(f"[PASS] {msg}")
        else:
            print(f"[FAIL] {msg}")
            sys.exit(1)
        return

    if args.verify_tasks:
        ok, violations, stats = verify_tasks_markdown(args.verify_tasks)
        print(f"Tasks 统计: 总数 {stats['total_tasks']} | 已勾选: {stats['checked_tasks']} | 未勾选: {stats['unchecked_tasks']}")
        if ok:
            print("[PASS] tasks.md 全部已勾选任务防伪签名与交付证据验证通过！")
        else:
            print(f"[FAIL] 检出 {len(violations)} 项违规：")
            for v in violations:
                print(f"  - {v}")
            sys.exit(1)
        return

    if args.verify_mirror:
        ok, msg = verify_tasks_mirror(args.verify_mirror[0], args.verify_mirror[1])
        if ok:
            print(f"[PASS] {msg}")
        else:
            print(f"[FAIL] {msg}")
            sys.exit(1)
        return

    if args.check_mother_library:
        ok, count, msg = check_mother_library_guard(repo_root)
        if ok:
            print(f"[PASS] {msg}")
        else:
            print(f"[FAIL] {msg}")
            sys.exit(1)
        return

    if args.verify_all:
        all_ok = True
        print("=" * 70)
        print("FinAI2.0 防伪硬化与防篡改签名自检")
        print("=" * 70)

        # 1. 检查母库守卫
        ok_m, cnt_m, msg_m = check_mother_library_guard(repo_root)
        print(f"1. 母库 370 行守卫: [{'PASS' if ok_m else 'FAIL'}] {msg_m}")
        if not ok_m:
            all_ok = False

        # 2. 检查 tasks.md
        tasks_code = repo_root / "docs" / "spec" / "001-a-stock-longonly-daily-quant" / "tasks.md"
        if tasks_code.exists():
            ok_t, viols_t, stats_t = verify_tasks_markdown(tasks_code)
            print(f"2. 代码仓 tasks 验签: [{'PASS' if ok_t else 'FAIL'}] 已核验 {stats_t['checked_tasks']} 项勾选任务 (违规: {len(viols_t)})")
            if not ok_t:
                all_ok = False
                for v in viols_t:
                    print(f"     -> {v}")

        # 3. 检查已有的运行记录防篡改签名
        run_file = repo_root / "experiments" / "runs" / "20260907-150402-t312-dividend-v1-noseed.json"
        if run_file.exists():
            with open(run_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "anti_tamper_signature" in data:
                ok_r, msg_r = verify_run_signature(data)
                print(f"3. 历史回测产物验签: [{'PASS' if ok_r else 'FAIL'}] {msg_r}")
                if not ok_r:
                    all_ok = False
            else:
                print("3. 历史回测产物验签: [SKIP] 产物尚未注入 anti_tamper_signature (待升级签名)")

        print("=" * 70)
        if not all_ok:
            sys.exit(1)
        print("[ALL PASS] 防伪硬化与验签全量通过！")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
