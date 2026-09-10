#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P0 一致性门禁单测套件（G-SKIP-1 / G-MDD-1 / G-DOC-1 / G-STRESS-1 / G-REF-1）。

对应 `docs/audit/roadmap_decision.md` §4「门禁重做的产品需求」，锁定四条不变量：

1. ``SKIP`` 与 ``INCONCLUSIVE`` **绝不计入通过**（G-SKIP-1）；
2. 0 成交产物的 MDD 判定必须 **INCONCLUSIVE**，不得 PASS（G-MDD-1）；
3. 文档数字与权威产物不符必须 **FAIL** 并指向具体行（G-DOC-1）；
4. 压测区间 0 成交/样本不足必须 **INCONCLUSIVE**（G-STRESS-1）；
5. 文档引用不存在的仓内路径必须 **FAIL**（G-REF-1）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.gates import (
    BaseGate,
    GateBlockerError,
    GateStatus,
    MustFailCasesGate,
    run_post_run_gates,
    sign_run_record,
)
from scripts.gates.base import GateCategory, GateResult, GateSeverity
from scripts.gates.gate_consistency import (
    DocMetricConsistencyGate,
    DocPathReferenceGate,
    MaxDrawdownCeilingGate,
    StressValidityGate,
)
from scripts.gates.gate_master_audit import GateMasterAudit
from scripts.gates.must_fail_probe import run_must_fail_cases

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNS_DIR = _REPO_ROOT / "experiments" / "runs"
_AUTHORITATIVE_RUN = _RUNS_DIR / "20260907-150402-t312-dividend-v1-noseed.json"


# =====================================================================
# 0. G-SKIP-1：SKIP / INCONCLUSIVE 语义
# =====================================================================

class TestSkipSemantics:
    """SKIP 与 INCONCLUSIVE 绝不等于通过。"""

    def _res(self, status: GateStatus) -> GateResult:
        return GateResult(
            gate_id="X-0", name="probe", category=GateCategory.G_GATE,
            status=status, severity=GateSeverity.INFO, message="probe",
        )

    def test_only_pass_counts_as_pass(self):
        assert self._res(GateStatus.PASS).is_pass is True
        assert self._res(GateStatus.FAIL).is_pass is False
        assert self._res(GateStatus.SKIP).is_pass is False
        assert self._res(GateStatus.INCONCLUSIVE).is_pass is False
        assert self._res(GateStatus.WARNING).is_pass is False

    def test_master_summary_never_green_with_skip(self, capsys):
        master = GateMasterAudit(gates=[])
        results = [
            self._res(GateStatus.PASS),
            self._res(GateStatus.SKIP),
        ]
        master.print_summary(results)
        out = capsys.readouterr().out
        assert "[全绿]" not in out, "存在 SKIP 时不得打印全绿"
        assert "[未全绿]" in out

    def test_master_summary_green_only_when_all_pass(self, capsys):
        master = GateMasterAudit(gates=[])
        master.print_summary([self._res(GateStatus.PASS)])
        assert "[全绿]" in capsys.readouterr().out


# =====================================================================
# 1. G-MDD-1：回撤上限门禁
# =====================================================================

class TestMaxDrawdownGate:
    """回撤上限门禁（BLOCKER）。"""

    def test_fails_on_authoritative_artifact(self):
        res = MaxDrawdownCeilingGate().evaluate({"artifact_path": str(_AUTHORITATIVE_RUN)})
        assert res.status == GateStatus.FAIL
        assert res.severity == GateSeverity.BLOCKER
        assert "0.4308" in res.message

    def test_inconclusive_on_zero_trade_artifact(self):
        artifact = _RUNS_DIR / "20260903-135508-t312-dividend-v1-noseed.json"
        res = MaxDrawdownCeilingGate().evaluate({"artifact_path": str(artifact)})
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS

    def test_pass_on_healthy_run(self):
        res = MaxDrawdownCeilingGate().evaluate(
            {"metrics": {"max_drawdown": "0.12", "round_trips": 20}}
        )
        assert res.status == GateStatus.PASS

    def test_fail_on_breach_threshold(self):
        res = MaxDrawdownCeilingGate().evaluate(
            {"metrics": {"max_drawdown": "0.40", "round_trips": 20}}
        )
        assert res.status == GateStatus.FAIL


# =====================================================================
# 2. G-STRESS-1：压测有效性门禁
# =====================================================================

class TestStressValidityGate:
    """压测有效性门禁（CRITICAL）。"""

    def test_inconclusive_on_t313_windows(self):
        # T313 两区间：40 天 / 60 天且 0 成交（warmup 支配）
        for days in (40, 60):
            res = StressValidityGate().evaluate({"round_trips": 0, "trading_days": days})
            assert res.status == GateStatus.INCONCLUSIVE, f"{days} 天 0 成交必须 INCONCLUSIVE"

    def test_inconclusive_on_short_window_with_trades(self):
        res = StressValidityGate().evaluate({"round_trips": 15, "trading_days": 60})
        assert res.status == GateStatus.INCONCLUSIVE

    def test_pass_on_long_window_with_trades(self):
        res = StressValidityGate().evaluate({"round_trips": 40, "trading_days": 260})
        assert res.status == GateStatus.PASS

    def test_inconclusive_when_missing(self):
        assert StressValidityGate().evaluate({}).status == GateStatus.INCONCLUSIVE


# =====================================================================
# 3. G-DOC-1：文档数字 ↔ 产物一致性门禁
# =====================================================================

class TestDocMetricConsistencyGate:
    """文档数字与产物一致性门禁（CRITICAL）。"""

    def test_fails_on_injected_wrong_mdd(self, tmp_path: Path):
        md = tmp_path / "wrong.md"
        md.write_text("# 测试\n\n最大回撤 MDD 15.23%\n", encoding="utf-8")
        res = DocMetricConsistencyGate().evaluate(
            {"doc_paths": [str(md)], "truth_run_path": str(_AUTHORITATIVE_RUN)}
        )
        assert res.status == GateStatus.FAIL
        assert res.metrics["violations"][0]["metric"] == "最大回撤"

    def test_passes_on_matching_doc(self, tmp_path: Path):
        truth = json.loads(_AUTHORITATIVE_RUN.read_text(encoding="utf-8"))["metrics"]
        mdd_pct = f"{float(truth['max_drawdown']) * 100:.2f}%"
        md = tmp_path / "right.md"
        md.write_text(f"# 测试\n\n最大回撤 {mdd_pct}\n", encoding="utf-8")
        res = DocMetricConsistencyGate().evaluate(
            {"doc_paths": [str(md)], "truth_run_path": str(_AUTHORITATIVE_RUN)}
        )
        assert res.status == GateStatus.PASS

    def test_repo_compliance_doc_flags_stale_turnover(self):
        """合规文档仍载有 92.51%（真值 201.14%）⇒ 必须被指认为违规。"""
        res = DocMetricConsistencyGate().evaluate({})
        assert res.status == GateStatus.FAIL
        refs = {v["file"] for v in res.metrics["violations"]}
        assert any("strategy_description_template.md" in f for f in refs)


# =====================================================================
# 4. G-REF-1：引用路径存在性门禁
# =====================================================================

class TestDocPathReferenceGate:
    """文档引用路径存在性门禁（CRITICAL）。"""

    def test_fails_on_phantom_path(self, tmp_path: Path):
        md = tmp_path / "ghost.md"
        md.write_text("详见 `data/stress_test/` 与 `ops/feishu_alert.py`\n", encoding="utf-8")
        res = DocPathReferenceGate().evaluate({"doc_paths": [str(md)]})
        assert res.status == GateStatus.FAIL
        refs = {v["reference"] for v in res.metrics["violations"]}
        assert "data/stress_test/" in refs
        assert "ops/feishu_alert.py" in refs

    def test_passes_on_existing_paths(self, tmp_path: Path):
        md = tmp_path / "ok.md"
        md.write_text("见 `scripts/gates/base.py` 与 `docs/README.md`\n", encoding="utf-8")
        res = DocPathReferenceGate().evaluate({"doc_paths": [str(md)]})
        assert res.status == GateStatus.PASS

    def test_repo_reports_known_phantoms(self):
        res = DocPathReferenceGate().evaluate({})
        assert res.status == GateStatus.FAIL
        refs = {v["reference"] for v in res.metrics["violations"]}
        assert "ops/feishu_alert.py" in refs

    def test_ignores_method_symbol(self, tmp_path: Path):
        """`模块.方法名`（如 collector._atomic_write_parquet）不得被误判为路径。"""
        md = tmp_path / "sym.md"
        md.write_text("调用 `data/collector._atomic_write_parquet` 写入\n", encoding="utf-8")
        res = DocPathReferenceGate().evaluate({"doc_paths": [str(md)]})
        assert res.status == GateStatus.PASS


# =====================================================================
# 5. E-1：五必挂极限用例（真实执行，⛔ 不得无证据预设通过）
# =====================================================================

class TestMustFailCasesGate:
    """E-1 五必挂用例：真实探针 + fail-closed。"""

    def test_probe_runs_all_five_real_cases(self):
        outcome = run_must_fail_cases()
        assert set(outcome) == set(MustFailCasesGate.STANDARD_CASES)
        assert all(outcome.values()), f"五必挂用例应全部符合预期，实际 {outcome}"

    def test_e1_inconclusive_without_evidence(self):
        """未提供逐用例结果时，E-1 必须 INCONCLUSIVE（⛔ 不得预设 True）。"""
        results = run_post_run_gates(context={}, strict=False)
        e1 = next(r for r in results if r.gate_id == "E-1")
        assert e1.status == GateStatus.INCONCLUSIVE

    def test_e1_nonempty_ctx_without_results_inconclusive(self):
        """⑲：ctx **非空** 但缺 must_fail_results ⇒ E-1 门禁**自身**判 INCONCLUSIVE（不靠调用方兜底）。"""
        for ctx in ({"foo": 1}, {"failed_cases": []}):
            res = MustFailCasesGate().evaluate(ctx)
            assert res.status == GateStatus.INCONCLUSIVE, f"ctx={ctx} 应为 INCONCLUSIVE"

    def test_run_post_run_gates_strict_blocks_inconclusive(self):
        """⑳：run_post_run_gates(strict=True) 遇 INCONCLUSIVE 抛 GateBlockerError。"""
        with pytest.raises(GateBlockerError):
            run_post_run_gates(context={}, strict=True)

    def test_consistency_cli_exit_nonzero_on_inconclusive(self):
        """㉑：gate_consistency CLI 默认 fail-closed —— INCONCLUSIVE 与 FAIL 均非零退出。"""
        from scripts.gates import gate_consistency as gc

        assert gc.main(["--stress-rt", "0", "--stress-days", "40"]) == 1   # INCONCLUSIVE
        assert gc.main(["--mdd", str(_AUTHORITATIVE_RUN)]) == 1           # FAIL

    def test_e2_inconclusive_without_evidence(self):
        """E-2 无送转拆股证据时必须 INCONCLUSIVE（⛔ 不得空数据判通过）。"""
        from scripts.gates import BonusSplitFifoGate

        res = BonusSplitFifoGate().evaluate({"fifo_errors": [], "final_positions": {}})
        assert res.status == GateStatus.INCONCLUSIVE

    def test_l3_inconclusive_without_runtime_trace(self):
        """L-3 仅有静态可达、无运行期追踪时必须 INCONCLUSIVE（⛔ 不得恒过）。"""
        from scripts.gates.gate_l_liveness import StaticAstCallGate

        res = StaticAstCallGate().evaluate({
            "source_code": "def f():\n    import x\n    x.MatchEngine()\n",
            "required_calls": ["MatchEngine"],
        })
        assert res.status == GateStatus.INCONCLUSIVE


# =====================================================================
# 6. ⑦/⑫ 拦截一致性：INCONCLUSIVE 必须在所有拦截路径阻断
# =====================================================================

class _InconclusiveGate(BaseGate):
    """仅含 INCONCLUSIVE 的场景探针（severity=BLOCKER）。"""

    gate_id = "X-INCL"
    name = "inconclusive probe"
    category = GateCategory.G_GATE
    severity = GateSeverity.BLOCKER

    def evaluate(self, context: Any = None) -> GateResult:  # noqa: ANN401
        return GateResult(
            gate_id=self.gate_id, name=self.name, category=self.category,
            status=GateStatus.INCONCLUSIVE, severity=self.severity, message="no evidence",
        )


class TestInconclusiveBlocksEverywhere:
    """⑦：展示与退出码必须一致——INCONCLUSIVE 同为阻断。"""

    def test_strict_audit_raises_on_inconclusive(self):
        master = GateMasterAudit(gates=[_InconclusiveGate()])
        with pytest.raises(GateBlockerError):
            master.audit(strict=True)

    def test_master_cli_strict_exits_nonzero_on_inconclusive(self, monkeypatch):
        import scripts.gates.gate_master_audit as gma

        class _FakeMaster:
            def __init__(self, *a, **k):  # noqa: ANN002, ANN003
                self.gates = []

            def audit(self, context=None, strict=False):  # noqa: ANN001
                return [_InconclusiveGate().evaluate()]

            def print_summary(self, results):  # noqa: ANN001
                return None

        monkeypatch.setattr(gma, "GateMasterAudit", _FakeMaster)
        monkeypatch.setattr(sys, "argv", ["gate_master_audit", "--strict"])
        with pytest.raises(SystemExit) as ei:
            gma.main()
        assert ei.value.code == 1

    def test_pre_push_guard_blocks_inconclusive(self, monkeypatch):
        import scripts.hooks.pre_push as pp

        class _FakeMaster:
            def __init__(self, *a, **k):  # noqa: ANN002, ANN003
                pass

            @staticmethod
            def get_push_time_gates():
                return []

            def audit(self, context=None, strict=False):  # noqa: ANN001
                return [_InconclusiveGate().evaluate()]

        monkeypatch.setattr(pp, "GateMasterAudit", _FakeMaster)
        ok, msg = pp.run_master_gate_guard()
        assert ok is False
        assert "INCONCLUSIVE" in msg or "未通过" in msg


#: 明确无法产出 FAIL 的门禁（设计使然）——必须显式白名单 + 理由，白名单本身可被审查。
_META_NO_FAIL_WHITELIST: dict[str, str] = {
    "G-STRESS-1": "有效性门禁只产出 PASS/INCONCLUSIVE（0 成交/样本不足即 INCONCLUSIVE），按设计不产出 FAIL",
}

#: 元测试允许的"关联 FAIL"（显式声明 + 理由）：这些门禁**不依赖注入的违规 ctx**，
#: 而是按默认/全仓取证，因此在"证据不全的非空 ctx"或"真实仓库现状"下必然 FAIL（fail-closed 正确）。
_META_ALLOWED_COFAIL: dict[str, str] = {
    "S-5": "证据不全的非空 ctx 下无 code_evidence ⇒ FAIL（fail-closed 正确）",
    "G-1": "缺 data_hash ⇒ FAIL（fail-closed 正确）",
    "G-4": "缺 run_record 签名 ⇒ FAIL（fail-closed 正确）",
    "G-MDD-1": "无 run_record/metrics 时按仓库产物扫描 ⇒ 命中真实 43.08% ⇒ FAIL（正确）",
    "G-DOC-1": "默认扫描全仓文档 ⇒ 命中真实文档不一致 ⇒ FAIL（正确）",
    "G-REF-1": "默认扫描全仓文档 ⇒ 命中真实幽灵路径 ⇒ FAIL（正确）",
}


def _passing_context(gate_id: str, tmp_path: Path) -> dict | None:
    """为指定门禁构造一个"合法"输入（期望 PASS）；无法构造返回 None。"""
    ok_md = tmp_path / "meta_ok.md"
    truth = json.loads(_AUTHORITATIVE_RUN.read_text(encoding="utf-8"))["metrics"]
    ok_md.write_text(f"# t\n\n最大回撤 {float(truth['max_drawdown']) * 100:.2f}%\n", encoding="utf-8")
    ok_ref = tmp_path / "meta_ok_ref.md"
    ok_ref.write_text("见 `scripts/gates/base.py`\n", encoding="utf-8")
    signed = sign_run_record({
        "run_id": "r", "code_version": "c", "data_version": "d", "params_hash": "p",
        "status": "FINISHED", "timestamp": "2026-09-07T16:00:00Z",
        "metrics": {"max_drawdown": "0.10", "round_trips": 20},
    })
    mapping: dict[str, dict] = {
        "D-1": {"bars": [{"date": "2024-01-02", "close": 10.0, "is_exdiv": False},
                         {"date": "2024-01-03", "close": 10.1, "is_exdiv": False}]},
        "D-2": {"float_mv_list": [(500 + i * 50) * 1e8 for i in range(35)], "amount_list": [2e8] * 35},
        "D-3": {"daily_yields": [0.03 + (i % 60) * 0.001 for i in range(240)], "year": 2023},
        "D-4": {"bars": [{"date": "2024-01-02", "tradestatus": "1", "volume": 1000},
                         {"date": "2024-01-03", "tradestatus": "0", "volume": 0}]},
        "D-5": {"orders": [{"side": "BUY", "price": 50.0, "volume": 100}]},
        "L-1": {"active_features": ["DIVIDEND_TAX"], "fee_summary": {"DIVIDEND_TAX": 125.5}},
        "L-2": {"target_weights": {"a": 0.5, "b": 0.3, "c": 0.2},
                "actual_values": {"a": 50000.0, "b": 30000.0, "c": 20000.0}},
        "L-3": {"source_code": "def f():\n    x.MatchEngine()\n",
                "required_calls": ["MatchEngine"], "executed_calls": ["MatchEngine"]},
        "E-1": {"must_fail_results": {c: True for c in MustFailCasesGate.STANDARD_CASES}},
        "E-2": {"fifo_errors": [], "final_positions": {"sh.600000": 0}},
        "E-3": {"trades": [{"symbol": "s", "side": "BUY", "price": "10.5",
                            "limit_up": "11", "limit_down": "9"}]},
        "A-1": {"trades": [{"trade_id": "T", "fees": {"COMMISSION": "5.00"}, "total_fee": "5.00"}]},
        "A-2": {"daily_cash_flows": [{"date": "d", "cash_start": "100", "trade_in": "0",
                                      "trade_out": "50", "fee_out": "0", "dividend_in": "0",
                                      "dividend_tax_out": "0", "cash_end": "50"}]},
        "A-3": {"roundtrip_total_fee": "103.22"},
        "A-4": {"trades": [{"date": "2023-08-20", "side": "SELL", "price": "10",
                            "volume": 10000, "amount": "100000", "fees": {"STAMP_TAX": "100"}}]},
        "S-1": {"annualized_turnover": 3.2},
        "S-2": {"index_below_ma200_dates": ["2024-01-15"], "daily_positions_ratio": {"2024-01-15": 0.0}},
        "S-3": {"orders_adv_ratio": [0.01], "baseline_return": 0.15, "stress_return": 0.10},
        "S-4": {"penalty_tax_amount": "100", "total_dividend_received": "2000"},
        "S-5": {"trades_count": 20, "total_stamp_tax": "200", "total_commission": "100", "code_evidence": "x"},
        "G-1": {"git_commit": "8fcf318a02c5f1b8a8e527d2c3e1e2d3f4a5b6c7",
                "data_hash": "a1b2c3d4e5f67890123456789abcdef0", "timestamp": "2026-09-07T16:00:00Z"},
        "G-2": {"task_id": "T", "is_checked": True, "gate_signature": "sig"},
        "G-3": {},                                  # 自源门禁：仓库 370 行守卫
        "G-4": {"run_record": signed},
        "G-MDD-1": {"metrics": {"max_drawdown": "0.10", "round_trips": 20}},
        "G-DOC-1": {"doc_paths": [str(ok_md)], "truth_run_path": str(_AUTHORITATIVE_RUN)},
        "G-REF-1": {"doc_paths": [str(ok_ref)]},
        "G-STRESS-1": {"round_trips": 40, "trading_days": 260},
    }
    return mapping.get(gate_id)


def _violating_context(gate_id: str, tmp_path: Path) -> dict | None:
    """为指定门禁构造一个"明确违规"的输入；无法构造返回 None。"""
    md_bad = tmp_path / "meta_bad.md"
    md_bad.write_text("# t\n\n最大回撤 MDD 15.23%\n", encoding="utf-8")
    md_ref = tmp_path / "meta_ref.md"
    md_ref.write_text("见 `data/stress_test/`\n", encoding="utf-8")
    mapping: dict[str, dict] = {
        "D-1": {"bars": [{"date": "2024-01-02", "close": 10.0, "is_exdiv": False},
                         {"date": "2024-01-03", "close": 25.0, "is_exdiv": False}]},
        "D-2": {"float_mv_list": [5e10] * 35, "amount_list": [2e8] * 35},
        "D-3": {"daily_yields": [0.045] * 240, "year": 2023},
        "D-4": {"bars": [{"date": "2024-01-02", "tradestatus": "0", "volume": 5000}]},
        "D-5": {"orders": [{"side": "BUY", "price": 350.0, "volume": 100}]},
        "L-1": {"active_features": ["DIVIDEND_TAX"], "fee_summary": {"DIVIDEND_TAX": 0}},
        "L-2": {"target_weights": {"a": 0.6, "b": 0.3, "c": 0.1},
                "actual_values": {"a": 1.0, "b": 1.0, "c": 1.0}},
        "L-3": {"source_code": "def d():\n    pass\n", "required_calls": ["nonexistent_call"]},
        "E-1": {"must_fail_results": {"LIMIT_UP_BUY_REJECT": False}},
        "E-2": {"fifo_errors": ["boom"]},
        "E-3": {"trades": [{"symbol": "sh.600000", "side": "BUY",
                            "price": "11.50", "limit_up": "11.00", "limit_down": "9.00"}]},
        "A-1": {"trades": [{"trade_id": "T", "fees": {"COMMISSION": "5"}, "total_fee": "8"}]},
        "A-2": {"daily_cash_flows": [{"date": "d", "cash_start": "100", "trade_in": "0",
                                      "trade_out": "50", "fee_out": "0", "dividend_in": "0",
                                      "dividend_tax_out": "0", "cash_end": "40"}]},
        "A-3": {"roundtrip_total_fee": "50"},
        "A-4": {"trades": [{"date": "2023-08-20", "side": "SELL", "price": "10",
                            "volume": 10000, "amount": "100000", "fees": {"STAMP_TAX": "50"}}]},
        "S-1": {"annualized_turnover": 5.8},
        "S-2": {"index_below_ma200_dates": ["2024-01-15"], "daily_positions_ratio": {"2024-01-15": 0.9}},
        "S-3": {"orders_adv_ratio": [0.035], "baseline_return": 0.15, "stress_return": 0.10},
        "S-4": {"penalty_tax_amount": "500", "total_dividend_received": "1000"},
        "S-5": {"trades_count": 20, "total_stamp_tax": "0", "total_commission": "100", "code_evidence": "x"},
        "G-1": {"git_commit": "", "data_hash": "abc", "timestamp": ""},
        "G-2": {"task_id": "T", "is_checked": True, "gate_signature": None},
        "G-3": {"sources_dir": str(tmp_path / "nope" / "finai" / "sources")},
        "G-4": {"run_record": {"run_id": "r", "code_version": "c", "data_version": "d",
                               "params_hash": "p", "status": "FINISHED", "metrics": {},
                               "anti_tamper_signature": "bad"}},
        "G-MDD-1": {"metrics": {"max_drawdown": "0.40", "round_trips": 20}},
        "G-DOC-1": {"doc_paths": [str(md_bad)]},
        "G-REF-1": {"doc_paths": [str(md_ref)]},
    }
    return mapping.get(gate_id)


class TestNoSilentPassMeta:
    """⑫ 元测试：杜绝"恒过门禁"。"""

    def test_no_gate_passes_on_empty_context_except_self_sourced(self):
        # G-3（母库 370 守卫）证据来自仓库自身，允许空 context 下 PASS；其余一律不得 PASS。
        self_sourced = {"G-3"}
        offenders = [
            g.gate_id for g in GateMasterAudit.get_standard_gates()
            if g.gate_id not in self_sourced and g.evaluate({}).status == GateStatus.PASS
        ]
        assert not offenders, f"空 context 下仍 PASS 的门禁: {offenders}"

    def test_every_gate_has_at_least_one_failing_input(self, tmp_path: Path):
        """每道注册门禁都必须能被某个"明确违规"输入判 FAIL（否则是恒过门禁）。"""
        offenders: list[str] = []
        for gate in GateMasterAudit.get_standard_gates():
            if gate.gate_id in _META_NO_FAIL_WHITELIST:
                continue
            ctx = _violating_context(gate.gate_id, tmp_path)
            if ctx is None:
                offenders.append(f"{gate.gate_id}(缺违规输入)")
                continue
            res = gate.evaluate(ctx)
            if res.status != GateStatus.FAIL:
                offenders.append(f"{gate.gate_id}(got {res.status.value})")
        assert not offenders, f"无法判 FAIL 的门禁（恒过风险）: {offenders}"

    def test_meta_whitelist_has_reasons(self):
        for gid, reason in _META_NO_FAIL_WHITELIST.items():
            assert reason.strip(), f"白名单 {gid} 缺少理由"
        for gid, reason in _META_ALLOWED_COFAIL.items():
            assert reason.strip(), f"关联 FAIL 白名单 {gid} 缺少理由"

    def test_violating_contexts_are_specific(self, tmp_path: Path):
        """㉒：每个违规 ctx 只应让**目标门禁** FAIL（允许显式声明的关联 FAIL）。"""
        allowed = set(_META_ALLOWED_COFAIL)
        offenders: list[tuple[str, list[str]]] = []
        for gate in GateMasterAudit.get_standard_gates():
            if gate.gate_id in _META_NO_FAIL_WHITELIST:
                continue
            ctx = _violating_context(gate.gate_id, tmp_path)
            if ctx is None:
                continue
            failed = {
                g.gate_id for g in GateMasterAudit.get_standard_gates()
                if g.evaluate(ctx).status == GateStatus.FAIL
            }
            extra = failed - {gate.gate_id} - allowed
            if extra:
                offenders.append((gate.gate_id, sorted(extra)))
        assert not offenders, f"违规 ctx 交叉污染（应只让目标门禁 FAIL）: {offenders}"

    def test_ci_policy_blocks_fail_and_static_inconclusive_warns_run_evidence(self):
        """㉖：CI 策略 —— FAIL 阻断；静态门禁 INCONCLUSIVE 阻断；需 run 产物门禁 INCONCLUSIVE 只告警。"""
        from scripts.gates.context_builder import ci_policy

        def res(gid, status):
            return GateResult(gate_id=gid, name=gid, category=GateCategory.G_GATE,
                              status=status, severity=GateSeverity.BLOCKER, message="x")

        b1, bl1, w1 = ci_policy([res("G-MDD-1", GateStatus.FAIL)])
        assert b1 and len(bl1) == 1 and not w1

        b2, bl2, w2 = ci_policy([res("G-1", GateStatus.INCONCLUSIVE)])       # 静态门禁
        assert b2 and len(bl2) == 1 and not w2

        b3, bl3, w3 = ci_policy([res("G-STRESS-1", GateStatus.INCONCLUSIVE)])  # 需 run 产物
        assert not b3 and not bl3 and len(w3) == 1

    def test_ci_cli_exit_nonzero_with_real_defects(self, monkeypatch):
        """㉖：`gate_master_audit --ci` 在"3 条真实 FAIL 存在"时 exit=1。"""
        import scripts.gates.gate_master_audit as gma

        monkeypatch.setattr(sys, "argv", ["gate_master_audit", "--ci"])
        with pytest.raises(SystemExit) as ei:
            gma.main()
        assert ei.value.code == 1

    def test_every_gate_has_a_passing_input(self, tmp_path: Path):
        """㉒ 反向：每道门禁必须存在合法输入使其 PASS（防"恒不过"）。"""
        offenders: list[str] = []
        for gate in GateMasterAudit.get_standard_gates():
            ctx = _passing_context(gate.gate_id, tmp_path)
            if ctx is None:
                offenders.append(f"{gate.gate_id}(缺合法输入)")
                continue
            res = gate.evaluate(ctx)
            if res.status != GateStatus.PASS:
                offenders.append(f"{gate.gate_id}(got {res.status.value})")
        assert not offenders, f"无法判 PASS 的门禁（恒不过风险）: {offenders}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
