#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""采纳登记（Adoption Registry）单测套件 —— 治理层 ㉗「晋升必须留证」。

锁定五条不变量（Fail-Closed + 不误伤）：

1. **采纳目录为空 ⇒ 准入步无操作、不阻断**（⛔ 绝不让 CI / pre-push 因"尚未采纳"永久红）；
2. 合格产物被采纳 ⇒ 登记留证 + 准入步放行；
3. 不合格产物尝试采纳 ⇒ **被拒**（``require_acceptance_pass=True`` 生效，拒绝"洗白"）；
4. 指针文件非法/脏 ⇒ **视同未采纳**（``load_adopted`` 返回 ``None``）；
5. 采纳只对**登记在册**的产物生效——未采纳的产物不得被误认为已采纳。

另断言 :func:`adoption.adoption_dir` 默认指向 ``experiments/acceptance/``，且该目录
**未被意外创建**（仓库现状不存在；写入只在 ``adopt`` 时发生）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.gates import acceptance as acceptance_mod
from scripts.gates import adoption
from scripts.gates.adoption import (
    ADOPTED_FILENAME,
    adoption_dir,
    adopt,
    load_adopted,
    run_adoption_gate,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _healthy_artifact(path: Path, run_id: str = "healthy-adopt") -> Path:
    """构造一份**真正完整**的合格产物（签署 + 现行 schema + 出处三件套 + 达标指标）。"""
    from scripts.gates.tamper_guard import sign_run_record

    path.write_text(json.dumps(sign_run_record({
        "run_id": run_id, "status": "FINISHED", "schema_version": 2,
        "code_version": "healthy-code", "data_version": "healthy-data",
        "timestamp": "2026-09-10T00:00:00+08:00", "params_hash": "healthy-params",
        "metrics": {"max_drawdown": "0.12", "win_rate": "0.55",
                    "annual_turnover": "2.0", "round_trips": 40},
    }), ensure_ascii=False), encoding="utf-8")
    return path


def _unhealthy_artifact(path: Path) -> Path:
    """构造一份不合格产物（未签署 / legacy schema / MDD 超限 / 0 成交）。"""
    path.write_text(json.dumps({
        "run_id": "unhealthy", "status": "FINISHED",
        "metrics": {"max_drawdown": "0.43", "win_rate": "0.20",
                    "annual_turnover": "5.0", "round_trips": 0},
    }, ensure_ascii=False), encoding="utf-8")
    return path


# =====================================================================
# 1. 采纳目录为空 ⇒ 无操作、不阻断
# =====================================================================

class TestEmptyAdoptionDirIsNoop:
    """关键：采纳目录为空时准入步**不得阻断**（否则 CI / pre-push 永久红）。"""

    def test_empty_dir_gate_is_pass_noop(self, tmp_path: Path):
        assert not adoption_dir(tmp_path).exists(), "前置：隔离目录不应存在"
        ok, msg, meta = run_adoption_gate(tmp_path)
        assert ok is True, "采纳目录为空 ⇒ 准入步必须无操作放行"
        assert "无操作" in msg or "未采纳" in msg
        assert meta["adopted"] is None

    def test_empty_dir_load_returns_none(self, tmp_path: Path):
        assert load_adopted(tmp_path) is None

    def test_empty_dir_is_not_created_by_readonly_calls(self, tmp_path: Path):
        """只读调用（load / gate）**不得**创建采纳目录（无副作用）。"""
        load_adopted(tmp_path)
        run_adoption_gate(tmp_path)
        assert not adoption_dir(tmp_path).exists(), "只读调用意外创建了 experiments/acceptance/"


# =====================================================================
# 2. 合格产物被采纳 ⇒ 放行
# =====================================================================

class TestHealthyArtifactAdopted:
    def test_healthy_artifact_adopted_and_gate_passes(self, tmp_path: Path):
        art = _healthy_artifact(tmp_path / "healthy.json", run_id="healthy-A")
        entry = adopt(art, adopted_by="tester", base_dir=tmp_path)

        assert entry["run_id"] == "healthy-A"
        assert entry["adopted_by"] == "tester"
        # 登记留证：指针 + 审计轨迹双写。
        d = adoption_dir(tmp_path)
        assert (d / ADOPTED_FILENAME).exists()
        assert (d / "index.jsonl").exists()

        adopted = load_adopted(tmp_path)
        assert adopted is not None and adopted["run_id"] == "healthy-A"

        ok, msg, meta = run_adoption_gate(tmp_path)
        assert ok is True
        assert "healthy-A" in msg
        assert meta["adopted"]["run_id"] == "healthy-A"


# =====================================================================
# 3. 不合格产物尝试采纳 ⇒ 被拒
# =====================================================================

class TestUnhealthyArtifactRejected:
    def test_unhealthy_artifact_adopt_rejected(self, tmp_path: Path):
        art = _unhealthy_artifact(tmp_path / "bad.json")
        with pytest.raises(PermissionError):
            adopt(art, base_dir=tmp_path)
        # 被拒后**不得**留下任何登记（拒绝"洗白"）。
        assert load_adopted(tmp_path) is None

    def test_require_acceptance_pass_false_skips_check(self, tmp_path: Path):
        """显式关闭准入前置（仅测试/迁移场景）时方可登记不合格产物。"""
        art = _unhealthy_artifact(tmp_path / "bad2.json")
        entry = adopt(art, require_acceptance_pass=False, base_dir=tmp_path)
        assert entry["run_id"] == "unhealthy"

    def test_missing_artifact_raises_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            adopt(tmp_path / "does_not_exist.json", base_dir=tmp_path)


# =====================================================================
# 4. 指针文件非法/脏 ⇒ 视同未采纳
# =====================================================================

class TestDirtyPointerTreatedAsUnadopted:
    @pytest.mark.parametrize("payload", [
        "not-json-at-all",                        # 非法 JSON
        "",                                       # 空文件
        json.dumps([1, 2, 3]),                    # 非对象
        json.dumps({"run_id": "x"}),              # 缺 artifact 键
        json.dumps({"artifact": ""}),             # artifact 为空
    ])
    def test_dirty_pointer_returns_none(self, tmp_path: Path, payload: str):
        d = adoption_dir(tmp_path)
        d.mkdir(parents=True, exist_ok=True)
        (d / ADOPTED_FILENAME).write_text(payload, encoding="utf-8")
        assert load_adopted(tmp_path) is None, f"脏指针应视同未采纳：{payload!r}"

    def test_dirty_pointer_gate_is_noop_not_block(self, tmp_path: Path):
        """脏指针 ⇒ 视同未采纳 ⇒ 准入步无操作（⛔ 不得因脏指针把 CI 判红）。"""
        d = adoption_dir(tmp_path)
        d.mkdir(parents=True, exist_ok=True)
        (d / ADOPTED_FILENAME).write_text("{ 脏 JSON", encoding="utf-8")
        ok, _msg, meta = run_adoption_gate(tmp_path)
        assert ok is True and meta["adopted"] is None


# =====================================================================
# 5. 采纳只对登记在册的产物生效（未采纳者不得被误认为已采纳）
# =====================================================================

class TestAdoptionScopedToRegisteredArtifact:
    def test_only_registered_artifact_is_adopted(self, tmp_path: Path):
        a = _healthy_artifact(tmp_path / "a.json", run_id="adopted-A")
        b = _healthy_artifact(tmp_path / "b.json", run_id="not-adopted-B")

        adopt(a, base_dir=tmp_path)
        adopted = load_adopted(tmp_path)
        assert adopted is not None and adopted["run_id"] == "adopted-A"
        # B 虽合格但**未被采纳** ⇒ 指针不得指向 B。
        assert adopted["artifact"] != str(b)
        # 未采纳产物的准入判定仍按其自身证据独立给出（不受"已采纳 A"影响）。
        ok_b, criteria_b, _ = acceptance_mod.evaluate_acceptance(b)
        assert ok_b is True  # 合格产物本身当然达标
        assert any(c["id"] == "G-MDD-1" for c in criteria_b)

    def test_readopting_replaces_pointer(self, tmp_path: Path):
        a = _healthy_artifact(tmp_path / "a.json", run_id="A")
        b = _healthy_artifact(tmp_path / "b.json", run_id="B")
        adopt(a, base_dir=tmp_path)
        adopt(b, base_dir=tmp_path)
        adopted = load_adopted(tmp_path)
        assert adopted is not None and adopted["run_id"] == "B", "重新采纳应替换指针（A 不再是当前采纳）"
        # 审计轨迹 append-only：两次采纳均留痕。
        lines = (adoption_dir(tmp_path) / "index.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

    def test_unhealthy_registered_artifact_fails_gate(self, tmp_path: Path):
        """若登记在册的产物不达标（迁移场景）⇒ 准入步**必须判 FAIL**（不得静默放行）。"""
        art = _unhealthy_artifact(tmp_path / "reg_bad.json")
        adopt(art, require_acceptance_pass=False, base_dir=tmp_path)
        ok, msg, _meta = run_adoption_gate(tmp_path)
        assert ok is False
        assert "FAIL" in msg


# =====================================================================
# 6. 默认目录契约 + 仓库现状（不得意外创建）
# =====================================================================

class TestAdoptionDirContract:
    def test_default_dir_points_to_experiments_acceptance(self):
        d = adoption.adoption_dir()
        assert d == _REPO_ROOT / "experiments" / "acceptance"
        assert adoption.ADOPTION_RELPATH == "experiments/acceptance"

    def test_repo_acceptance_dir_not_created(self):
        """仓库现状：``experiments/acceptance/`` **不存在**（写入只在 adopt 时发生）。"""
        assert not (_REPO_ROOT / "experiments" / "acceptance").exists(), (
            "experiments/acceptance/ 不应被测试/只读路径意外创建"
        )

    def test_adoption_dir_isolated_by_base_dir(self, tmp_path: Path):
        assert adoption_dir(tmp_path) == tmp_path / "experiments" / "acceptance"
        assert adoption_dir(tmp_path) != adoption.adoption_dir()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
