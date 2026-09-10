#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase 3 Hardening Tests: 自动化防伪硬化、防篡改签名与 Git Hooks 拦截集成测试

覆盖：
1. 回测记录数字签名生成与确定性验证；
2. 回测记录关键指标（CAGR、版本号、费用）篡改拦截检测；
3. 缺少出处三件套拦截；
4. G-4 产物防篡改签名门禁 (AntiTamperSignatureGate) PASS/FAIL 判定；
5. tasks.md 物理勾选防伪机读验签（合规条目 PASS、未附证据偷勾 FAIL）；
6. tasks.md 两仓镜像一致性校验（完全一致 PASS、哈希偏离 FAIL）；
7. 母库只读区 370 行守卫检查（实际仓 PASS、模拟篡改行数 FAIL）；
8. Pre-commit 门禁运行器逻辑核验（合法文件放行、违规件拦截）；
9. GateMasterAudit 调度器标准门禁包含 G-1 ~ G-4 治理门禁与 P0 一致性四道门禁 + G-REPRO-1（共 29 道门禁）。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
import pytest

from scripts.gates import (
    AntiTamperSignatureGate,
    GateCategory,
    GateMasterAudit,
    GateSeverity,
    GateStatus,
    ProvenanceTriadGate,
    TasksSignGate,
    check_mother_library_guard,
    compute_run_signature,
    sign_run_record,
    verify_run_signature,
    verify_tasks_markdown,
    verify_tasks_mirror,
)
from scripts.hooks.pre_commit import run_pre_commit_checks


@pytest.fixture
def sample_run_record() -> dict:
    """提供标准回测结果样本"""
    return {
        "run_id": "20260907-150402-t312-dividend-v1-noseed",
        "status": "FINISHED",
        "timestamp": "2026-09-07T15:04:02+08:00",
        "code_version": "t312-dividend-v1",
        "data_version": "dividend-stocks-2015-2024",
        "seed": None,
        "params_hash": "f54c298d5168eac5",
        "params": {"index_symbol": "sh.000300", "min_dividend_yield": "0.03"},
        "metrics": {
            "cagr": "-0.031979",
            "total_return": "-0.2771924",
            "max_drawdown": "0.430765",
            "sharpe_ratio": "-0.379071",
            "annual_turnover": "2.011435",
            "win_rate": "0.282051",
            "fees_sum": "9738.26",
            "fees_total": {
                "COMMISSION": "1556.18",
                "DIVIDEND_TAX": "5043.75",
                "STAMP_TAX": "2651.94",
            },
        },
    }


# =====================================================================
# 1. 签名引擎与防篡改测试
# =====================================================================

def test_signature_deterministic(sample_run_record):
    """验证相同内容生成唯一且确定性的签名"""
    sig1 = compute_run_signature(sample_run_record)
    sig2 = compute_run_signature(sample_run_record)
    assert sig1 == sig2
    assert len(sig1) == 64  # SHA-256 hex


def test_sign_and_verify_valid(sample_run_record):
    """验证签名生成后可通过防篡改验签"""
    signed = sign_run_record(sample_run_record)
    assert "anti_tamper_signature" in signed
    ok, msg = verify_run_signature(signed)
    assert ok is True
    assert "检验全部通过" in msg


def test_verify_detects_tampered_cagr(sample_run_record):
    """核心防伪：若有人事后将亏损收益率篡改为正收益，验签必须坚决拦截"""
    signed = sign_run_record(sample_run_record)
    tampered = copy.deepcopy(signed)
    tampered["metrics"]["cagr"] = "0.150000"  # 偷改 CAGR

    ok, msg = verify_run_signature(tampered)
    assert ok is False
    assert "防篡改签名不匹配" in msg


def test_verify_detects_tampered_version(sample_run_record):
    """核心防伪：若有人篡改关联的代码版本号，验签必须拦截"""
    signed = sign_run_record(sample_run_record)
    tampered = copy.deepcopy(signed)
    tampered["code_version"] = "tampered-commit-fake"

    ok, msg = verify_run_signature(tampered)
    assert ok is False
    assert "防篡改签名不匹配" in msg


def test_verify_detects_missing_signature(sample_run_record):
    """未签名的裸数据必须被拒"""
    ok, msg = verify_run_signature(sample_run_record)
    assert ok is False
    assert "缺少 anti_tamper_signature" in msg


def test_verify_detects_missing_provenance(sample_run_record):
    """缺失出处三件套之一必须被拒"""
    signed = sign_run_record(sample_run_record)
    signed["timestamp"] = ""  # 清空时间戳
    ok, msg = verify_run_signature(signed)
    assert ok is False
    assert "出处三件套缺失" in msg


# =====================================================================
# 2. G-4 AntiTamperSignatureGate 门禁测试
# =====================================================================

def test_anti_tamper_gate_pass(sample_run_record):
    """G-4 门禁对合法签名记录返回 PASS"""
    signed = sign_run_record(sample_run_record)
    gate = AntiTamperSignatureGate()
    res = gate.evaluate(signed)
    assert res.status == GateStatus.PASS
    assert res.gate_id == "G-4"
    assert res.severity == GateSeverity.BLOCKER


def test_anti_tamper_gate_fail(sample_run_record):
    """G-4 门禁对篡改记录返回 FAIL 并标为 BLOCKER"""
    signed = sign_run_record(sample_run_record)
    signed["metrics"]["total_return"] = "0.500000"
    gate = AntiTamperSignatureGate()
    res = gate.evaluate(signed)
    assert res.status == GateStatus.FAIL
    assert res.severity == GateSeverity.BLOCKER
    assert "防篡改签名不匹配" in res.message


# =====================================================================
# 3. tasks.md 物理勾选防伪机读验签测试
# =====================================================================

def test_verify_tasks_markdown_valid():
    """合规 tasks 文本通过验签"""
    sample_text = """
# Tasks
- [x] [T101] 环境清单 — 2026-08-31 ✅ Commit `8282cd4`，699 passed
- [x] [T102] 接口探测 — 2026-08-31 ✅ REVALIDATE.md §R3 关闭，单测全绿
- [ ] [T103] 未做任务
"""
    ok, viols, stats = verify_tasks_markdown(sample_text)
    assert ok is True
    assert len(viols) == 0
    assert stats["checked_tasks"] == 2
    assert stats["unchecked_tasks"] == 1


def test_verify_tasks_markdown_catches_unauthorized_check():
    """严打偷勾：勾选了 [x] 但没有完成日期和 ✅ 签章，直接判违规"""
    sample_text = """
- [x] [T999] 偷跑任务，未给日期未给证据
"""
    ok, viols, stats = verify_tasks_markdown(sample_text)
    assert ok is False
    assert len(viols) >= 2
    assert any("缺少标准完成日期" in v for v in viols)
    assert any("缺少 ✅ 签章" in v for v in viols)


def test_verify_tasks_markdown_catches_missing_evidence():
    """有日期和勾选，但无任何代码/测试/文档证据，判定违规"""
    sample_text = """
- [x] [T999] 口头声称已完成 — 2026-09-07 ✅
"""
    ok, viols, stats = verify_tasks_markdown(sample_text)
    assert ok is False
    assert any("缺少可核验证据" in v for v in viols)


def test_tasks_sign_gate_evaluates_file():
    """G-2 TasksSignGate 支持传入 tasks_content 校验"""
    gate = TasksSignGate()
    res_pass = gate.evaluate({
        "tasks_content": "- [x] [T101] 测试任务 — 2026-08-31 ✅ Commit `1234567` 全绿\n- [ ] [T102] 待办",
    })
    assert res_pass.status == GateStatus.PASS

    res_fail = gate.evaluate({
        "tasks_content": "- [x] [T101] 未附证据直接勾选\n",
    })
    assert res_fail.status == GateStatus.FAIL
    assert res_fail.severity == GateSeverity.CRITICAL


# =====================================================================
# 4. 两仓 tasks.md 镜像一致性比对测试
# =====================================================================

def test_verify_tasks_mirror(tmp_path):
    f1 = tmp_path / "tasks1.md"
    f2 = tmp_path / "tasks2.md"
    f3 = tmp_path / "tasks3.md"

    f1.write_bytes(b"Hello\r\nTasks\n")
    f2.write_bytes(b"Hello\nTasks\n")  # 换行符差异应被吸收
    f3.write_bytes(b"Hello\nDifferent Tasks\n")

    ok1, msg1 = verify_tasks_mirror(f1, f2)
    assert ok1 is True
    assert "完全吻合" in msg1

    ok2, msg2 = verify_tasks_mirror(f1, f3)
    assert ok2 is False
    assert "内容哈希不一致" in msg2


# =====================================================================
# 5. 母库 370 行守卫检查
# =====================================================================

def test_check_mother_library_real():
    """实测 FinAI2.0 本地母库只读区恒等于 370 行"""
    ok, count, msg = check_mother_library_guard()
    assert ok is True
    assert count == 370


def test_check_mother_library_tampered(tmp_path):
    """模拟破坏母库行数触发红线"""
    fake_root = tmp_path / "repo"
    src_dir = fake_root / "finai" / "sources"
    src_dir.mkdir(parents=True)
    (src_dir / "test.py").write_text("# FINDING-1\n# FINDING-2\n", encoding="utf-8")

    ok, count, msg = check_mother_library_guard(fake_root)
    assert ok is False
    assert count == 2
    assert "违背红线" in msg


# =====================================================================
# 6. Pre-commit 门禁运行器测试
# =====================================================================

def test_run_pre_commit_checks_clean():
    """无暂存文件或干净工作区时 pre-commit 检查 PASS"""
    ok, errs = run_pre_commit_checks(staged_files=[])
    assert ok is True
    assert len(errs) == 0


def test_run_pre_commit_catches_tampered_run(tmp_path, sample_run_record):
    """Pre-commit 拦截被篡改的回测记录暂存文件（⛔ 全程隔离在 tmp_path，不碰真实仓库）"""
    tampered = copy.deepcopy(sample_run_record)
    tampered["anti_tamper_signature"] = "fake_bad_sig_123"

    # 在临时仓根下的 runs/ 目录造被篡改产物，绝不写真实仓库 runs/
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    fake_run_file = runs_dir / "temp_tampered_test.json"
    fake_run_file.write_text(json.dumps(tampered), encoding="utf-8")

    try:
        rel_path = "runs/temp_tampered_test.json"
        ok, errs = run_pre_commit_checks(staged_files=[rel_path], repo_root=tmp_path)
        assert ok is False
        assert any("防篡改校验失败" in e for e in errs)
    finally:
        # 清理失败（如沙箱删除 shim 抛错）绝不允许污染业务断言
        try:
            fake_run_file.unlink(missing_ok=True)
        except OSError:
            pass


# =====================================================================
# 7. GateMasterAudit 完整架构检验
# =====================================================================

def test_master_audit_contains_g4_and_all_standard_gates():
    """六维门禁总调度器涵盖 29 项全量门禁，且包含 G-1~G-4 与 P0 一致性四道 + G-REPRO-1"""
    master = GateMasterAudit()
    gate_ids = [g.gate_id for g in master.gates]
    assert len(gate_ids) == 29
    for gid in ("G-1", "G-2", "G-3", "G-4", "G-MDD-1", "G-DOC-1", "G-STRESS-1", "G-REF-1", "G-REPRO-1"):
        assert gid in gate_ids
