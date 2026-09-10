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

    def test_stale_turnover_doc_flagged(self, tmp_path: Path):
        """合规类文档若载有失实换手率（92.51%，真值 201.14%）⇒ 必须被指认为违规。

        ⚠ 断言改用合成文档（不再耦合仓库文档现状）——仓库 docs/ 由其它工作流并行修订，
        门禁对"仓库现状"的判定会随文档修订而变，不应把测试绑死在某一时刻的文档内容上。
        """
        md = tmp_path / "strategy_description_template.md"
        md.write_text("# 策略说明\n\n年化换手率 92.51%\n", encoding="utf-8")
        res = DocMetricConsistencyGate().evaluate(
            {"doc_paths": [str(md)], "truth_run_path": str(_AUTHORITATIVE_RUN)}
        )
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

    def test_phantom_reference_flagged(self, tmp_path: Path):
        """幽灵路径引用（如 ``ops/feishu_alert.py``）必须被指认为违规。

        ⚠ 断言改用合成文档（不再耦合仓库文档现状）——docs/ 由其它工作流并行修订，
        门禁对"仓库现状"的判定会随文档修订而变，不应把测试绑死在某一时刻的文档内容上。
        """
        md = tmp_path / "compliance_like.md"
        md.write_text("变更通知见 `ops/feishu_alert.py`\n", encoding="utf-8")
        res = DocPathReferenceGate().evaluate({"doc_paths": [str(md)]})
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
        "G-REPRO-1": {"run_records": [
            {"run_id": "r-a", "params_hash": "ph", "repro_fingerprint": "fp-ok",
             "metrics": {"max_drawdown": "0.10", "round_trips": 20, "cagr": "0.05"}},
            {"run_id": "r-b", "params_hash": "ph", "repro_fingerprint": "fp-ok",
             "metrics": {"max_drawdown": "0.10", "round_trips": 20, "cagr": "0.05"}},
        ]},
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
        "G-REPRO-1": {"run_records": [
            {"run_id": "r-a", "params_hash": "ph", "repro_fingerprint": "fp-bad",
             "metrics": {"max_drawdown": "0.10", "round_trips": 20, "cagr": "0.05"}},
            {"run_id": "r-b", "params_hash": "ph", "repro_fingerprint": "fp-bad",
             "metrics": {"max_drawdown": "0.10", "round_trips": 20, "cagr": "0.09"}},
        ]},
    }
    return mapping.get(gate_id)


class TestNoSilentPassMeta:
    """⑫ 元测试：杜绝"恒过门禁"。"""

    def test_no_gate_passes_on_empty_context_except_self_sourced(self):
        """空 context 下，除"证据直接来自仓库现状"的门禁外，一律不得 PASS。

        ``G-3``  母库 370 行守卫；``G-DOC-1`` / ``G-REF-1`` 默认扫描全仓文档——
        三者的 PASS **反映真实仓库合规状态**（文档已合规/作废 ⇒ 无违规 ⇒ PASS），
        并非"恒过门禁"（其对违规输入的 FAIL 能力由 ``test_every_gate_has_at_least_one_failing_input``
        与 ``test_violating_contexts_are_specific`` 另行保证）。
        """
        repo_self_sourced = {"G-3", "G-DOC-1", "G-REF-1"}
        offenders = [
            g.gate_id for g in GateMasterAudit.get_standard_gates()
            if g.gate_id not in repo_self_sourced and g.evaluate({}).status == GateStatus.PASS
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
        """㉖：CI 策略 —— FAIL 阻断；静态门禁 INCONCLUSIVE 阻断；需 run 产物门禁 INCONCLUSIVE 只告警；
        WARN 门禁（G-MDD-1，任务 1 三层分层）FAIL 亦降级为告警。"""
        from scripts.gates.context_builder import ci_policy

        def res(gid, status):
            return GateResult(gate_id=gid, name=gid, category=GateCategory.G_GATE,
                              status=status, severity=GateSeverity.BLOCKER, message="x")

        b1, bl1, w1 = ci_policy([res("G-DOC-1", GateStatus.FAIL)])           # 静态 FAIL ⇒ 阻断
        assert b1 and len(bl1) == 1 and not w1

        b2, bl2, w2 = ci_policy([res("G-1", GateStatus.INCONCLUSIVE)])       # 静态门禁
        assert b2 and len(bl2) == 1 and not w2

        b3, bl3, w3 = ci_policy([res("G-STRESS-1", GateStatus.INCONCLUSIVE)])  # 需 run 产物
        assert not b3 and not bl3 and len(w3) == 1

        # 三层分层（任务 1）：G-MDD-1 FAIL 只告警不阻断（BLOCKER 能力下移到准入层）。
        b4, bl4, w4 = ci_policy([res("G-MDD-1", GateStatus.FAIL)])
        assert not b4 and not bl4 and len(w4) == 1 and w4[0].gate_id == "G-MDD-1"

    def test_ci_cli_exit_nonzero_when_blocker_present(self, monkeypatch):
        """㉖：`gate_master_audit --ci` 在存在静态 FAIL 阻断项时 exit=1。

        ⚠ 用**注入的 FAIL 门禁**判定（不再依赖仓库文档现状）——docs/ 由其它工作流并行修订，
        若把"真实缺陷"写死在测试里，会随文档修订而失真。
        """
        import scripts.gates.gate_master_audit as gma

        def _fail():
            return GateResult(gate_id="G-DOC-1", name="doc", category=GateCategory.G_GATE,
                              status=GateStatus.FAIL, severity=GateSeverity.CRITICAL, message="x")

        class _FakeMaster:
            def __init__(self, *a, **k):  # noqa: ANN002, ANN003
                pass

            def audit(self, context=None, strict=False):  # noqa: ANN001
                return [_fail()]

            def print_summary(self, results):  # noqa: ANN001
                return None

        monkeypatch.setattr(gma, "GateMasterAudit", _FakeMaster)
        monkeypatch.setattr(gma, "build_repo_context", lambda *a, **k: ({}, "fake"))
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


# =====================================================================
# 7. 任务 3 元测试之一：门禁分类完备性（防"新增门禁忘记登记 ⇒ 被 ci_policy 静默忽略"）
# =====================================================================

class TestGateClassificationCompleteness:
    """``STATIC_GATE_IDS ∪ RUN_EVIDENCE_GATE_IDS`` 必须覆盖全部注册门禁。"""

    def test_classification_covers_all_registered_gates(self):
        from scripts.gates.context_builder import (
            RUN_EVIDENCE_GATE_IDS,
            STATIC_GATE_IDS,
            WARN_GATE_IDS,
        )

        registered = {g.gate_id for g in GateMasterAudit.get_standard_gates()}
        classified = set(STATIC_GATE_IDS) | set(RUN_EVIDENCE_GATE_IDS)
        assert classified == registered, (
            f"未登记门禁 {sorted(registered - classified)}（其 SKIP/INCONCLUSIVE 会被静默忽略）；"
            f"登记了不存在的门禁 {sorted(classified - registered)}"
        )
        # 两类必须互斥（否则 ci_policy 语义歧义）。
        assert not (set(STATIC_GATE_IDS) & set(RUN_EVIDENCE_GATE_IDS)), "静态/需产物门禁集合必须互斥"
        # WARN 门禁必须也是注册门禁（三层分层的降级集合须合法）。
        assert set(WARN_GATE_IDS) <= registered, f"WARN 门禁未注册: {sorted(set(WARN_GATE_IDS) - registered)}"

    def test_ci_policy_never_silently_ignores_registered_gate(self):
        """每道注册门禁的 INCONCLUSIVE / FAIL 必须落入 blocker 或 warning 之一（⛔ 无静默忽略）。"""
        from scripts.gates.context_builder import ci_policy

        offenders: list[str] = []
        for gid in sorted({g.gate_id for g in GateMasterAudit.get_standard_gates()}):
            for status in (GateStatus.INCONCLUSIVE, GateStatus.FAIL):
                r = GateResult(gate_id=gid, name=gid, category=GateCategory.G_GATE,
                               status=status, severity=GateSeverity.BLOCKER, message="x")
                _, blockers, warnings = ci_policy([r])
                if (r in blockers) == (r in warnings):
                    offenders.append(f"{gid}/{status.value}")
        assert not offenders, f"被 ci_policy 静默忽略的门禁结果: {offenders}"


# =====================================================================
# 8. 任务 3 元测试之二 + G-REPRO-1：复现一致性门禁（PM-1）
# =====================================================================

class TestReproducibilityGate:
    """G-REPRO-1：同 repro_fingerprint 必得同 metrics；legacy 报 LEGACY_UNVERIFIED。"""

    def test_fails_on_same_fingerprint_inconsistent_metrics(self):
        """任务 3 反向测试：同 repro_fingerprint 但 metrics 不一致 ⇒ 必须判 FAIL。"""
        from scripts.gates.gate_repro import ReproducibilityGate

        recs = [
            {"run_id": "a", "params_hash": "ph", "repro_fingerprint": "fp1",
             "metrics": {"cagr": "0.05", "max_drawdown": "0.10"}},
            {"run_id": "b", "params_hash": "ph", "repro_fingerprint": "fp1",
             "metrics": {"cagr": "0.09", "max_drawdown": "0.10"}},
        ]
        res = ReproducibilityGate().evaluate({"run_records": recs})
        assert res.status == GateStatus.FAIL
        assert res.metrics["violations_total"] == 1

    def test_passes_on_same_fingerprint_identical_metrics(self):
        from scripts.gates.gate_repro import ReproducibilityGate

        same = {"cagr": "0.05", "max_drawdown": "0.10"}
        recs = [
            {"run_id": "a", "params_hash": "ph", "repro_fingerprint": "fp1", "metrics": dict(same)},
            {"run_id": "b", "params_hash": "ph", "repro_fingerprint": "fp1", "metrics": dict(same)},
        ]
        assert ReproducibilityGate().evaluate({"run_records": recs}).status == GateStatus.PASS

    def test_legacy_same_params_hash_never_fails_but_unverified(self):
        """legacy 组（无 repro_fingerprint）即便 metrics 不一致也**不判 FAIL**——因为无出处键无法归因。"""
        from scripts.gates.gate_repro import ReproducibilityGate

        recs = [
            {"run_id": "l1", "params_hash": "f54c298d5168eac5", "metrics": {"cagr": "0.05"}},
            {"run_id": "l2", "params_hash": "f54c298d5168eac5", "metrics": {"cagr": "-0.27"}},
        ]
        res = ReproducibilityGate().evaluate({"run_records": recs})
        assert res.status == GateStatus.INCONCLUSIVE
        assert res.status != GateStatus.PASS
        assert "LEGACY_UNVERIFIED" in res.message

    def test_repo_legacy_artifacts_report_legacy_unverified(self):
        """对真实仓库现状（4 份 legacy 产物，无 repro_fingerprint）⇒ LEGACY_UNVERIFIED。"""
        from scripts.gates.gate_repro import ReproducibilityGate

        res = ReproducibilityGate().evaluate({})
        assert res.status == GateStatus.INCONCLUSIVE
        assert "LEGACY_UNVERIFIED" in res.message


# =====================================================================
# 9. 任务 4：gate-doc-void 作废文档标记机制
# =====================================================================

class TestGateDocVoid:
    """``<!-- gate-doc-void: date=YYYY-MM-DD; reason=<非空> -->`` 豁免；非法标记不生效。"""

    def test_valid_marker_skips_doc_and_counts(self, tmp_path: Path):
        md = tmp_path / "void_ok.md"
        md.write_text(
            "<!-- gate-doc-void: date=2026-09-10; reason=数字已失实，待 M4' 重写 -->\n\n"
            "最大回撤 MDD 15.23%\n",
            encoding="utf-8",
        )
        res = DocMetricConsistencyGate().evaluate(
            {"doc_paths": [str(md)], "truth_run_path": str(_AUTHORITATIVE_RUN)}
        )
        assert res.status == GateStatus.PASS            # 该行本应违规，但被 void 豁免
        assert res.metrics["void_docs"] == 1
        assert "void_docs: 1" in res.message

    def test_invalid_marker_missing_date_still_validated(self, tmp_path: Path):
        md = tmp_path / "void_bad_date.md"
        md.write_text(
            "<!-- gate-doc-void: reason=缺了 date -->\n\n最大回撤 MDD 15.23%\n",
            encoding="utf-8",
        )
        res = DocMetricConsistencyGate().evaluate(
            {"doc_paths": [str(md)], "truth_run_path": str(_AUTHORITATIVE_RUN)}
        )
        assert res.status == GateStatus.FAIL            # 标记不合法 ⇒ 照常校验 ⇒ 违规
        assert res.metrics["void_docs"] == 0

    def test_invalid_marker_empty_reason_still_validated(self, tmp_path: Path):
        md = tmp_path / "void_bad_reason.md"
        md.write_text(
            "<!-- gate-doc-void: date=2026-09-10; reason= -->\n\n最大回撤 MDD 15.23%\n",
            encoding="utf-8",
        )
        res = DocMetricConsistencyGate().evaluate(
            {"doc_paths": [str(md)], "truth_run_path": str(_AUTHORITATIVE_RUN)}
        )
        assert res.status == GateStatus.FAIL
        assert res.metrics["void_docs"] == 0

    def test_is_void_doc_helper_contract(self):
        from scripts.gates.gate_consistency import is_void_doc

        assert is_void_doc("<!-- gate-doc-void: date=2026-09-10; reason=作废 -->") is True
        assert is_void_doc("<!-- gate-doc-void: reason=无日期 -->") is False
        assert is_void_doc("<!-- gate-doc-void: date=2026-09-10; reason=   -->") is False
        assert is_void_doc("普通文档，无标记") is False

    def test_ref_gate_void_skips_phantom_path_and_counts(self, tmp_path: Path):
        md = tmp_path / "ref_void.md"
        md.write_text(
            "<!-- gate-doc-void: date=2026-09-10; reason=路径已迁移 -->\n见 `data/stress_test/`\n",
            encoding="utf-8",
        )
        res = DocPathReferenceGate().evaluate({"doc_paths": [str(md)]})
        assert res.status == GateStatus.PASS
        assert res.metrics["void_docs"] == 1
        assert "void_docs: 1" in res.message

    def test_ref_gate_invalid_marker_still_validated(self, tmp_path: Path):
        md = tmp_path / "ref_bad.md"
        md.write_text(
            "<!-- gate-doc-void: date=2026-09-10; reason= -->\n见 `data/stress_test/`\n",
            encoding="utf-8",
        )
        res = DocPathReferenceGate().evaluate({"doc_paths": [str(md)]})
        assert res.status == GateStatus.FAIL
        assert res.metrics["void_docs"] == 0


# =====================================================================
# 9b. 任务 3 补充：门禁数量 / 单测基线声明一致性（动态核，⛔ 不写死）
# =====================================================================

class TestGateCountBaselineConsistency:
    """G-DOC-1 必须核验"文档声称的门禁数量 / 单测基线"。"""

    @staticmethod
    def _write_truth_doc(tmp_path: Path, name: str, extra_line: str) -> Path:
        truth = json.loads(_AUTHORITATIVE_RUN.read_text(encoding="utf-8"))["metrics"]
        md = tmp_path / name
        md.write_text(
            f"# t\n\n最大回撤 {float(truth['max_drawdown']) * 100:.2f}%\n{extra_line}\n",
            encoding="utf-8",
        )
        return md

    def _eval(self, md: Path):
        return DocMetricConsistencyGate().evaluate(
            {"doc_paths": [str(md)], "truth_run_path": str(_AUTHORITATIVE_RUN)}
        )

    def test_wrong_gate_count_fails(self, tmp_path: Path):
        n = len(GateMasterAudit.get_standard_gates())       # 动态：⛔ 不写死 29
        md = self._write_truth_doc(tmp_path, "wrong_count.md", f"本仓共 {n + 7} 道门禁。")
        res = self._eval(md)
        assert res.status == GateStatus.FAIL
        assert res.metrics["declaration_violations"][0]["metric"] == "门禁数量"
        assert res.metrics["declaration_violations"][0]["expected"] == str(n)

    def test_correct_gate_count_passes(self, tmp_path: Path):
        n = len(GateMasterAudit.get_standard_gates())
        md = self._write_truth_doc(tmp_path, "ok_count.md", f"本仓共 {n} 道机读门禁。")
        assert self._eval(md).status == GateStatus.PASS

    def test_gate_count_phrase_variant_fails(self, tmp_path: Path):
        """数字**不紧跟**"门禁"的变体（『门禁包已重做为 28 道（…）』）也必须被命中。

        真实漂移实例：`docs/delivery/PHASE4_ADMISSION_RESOLUTION.md:32` —— 当前时态结论写 28 道。
        """
        n = len(GateMasterAudit.get_standard_gates())
        md = self._write_truth_doc(
            tmp_path, "variant.md", f"门禁包已重做为 **{n + 3} 道**（`scripts/gates/`）；全面闭环。",
        )
        res = self._eval(md)
        assert res.status == GateStatus.FAIL
        assert any(
            d["metric"] == "门禁数量" and d["doc_value"] == str(n + 3)
            for d in res.metrics["declaration_violations"]
        )

    def test_gate_count_no_false_positive(self, tmp_path: Path):
        """放宽后的误报防线：① 有关键词但无「N 道」；② 有「N 道」但无关键词 ⇒ 均不得报。"""
        md1 = self._write_truth_doc(tmp_path, "kw_only.md", "六维防御门禁已全面闭环。")
        assert self._eval(md1).status == GateStatus.PASS
        md2 = self._write_truth_doc(tmp_path, "num_only.md", "本章共 7 道工序。")
        assert self._eval(md2).status == GateStatus.PASS

    def test_baseline_constant_matches_collected_count(self):
        """防'单一事实源自己漂移'：常量必须**等于** pytest 真实收集数（⛔ 不许 ``>=`` 软化）。

        口径 = **收集数**（``--collect-only``，稳定）；⛔ 不是 ``passed``（会因偶发 skip 波动）。
        """
        import re as _re
        import subprocess
        import sys as _sys

        from scripts.gates.constants import TEST_BASELINE_PASSED

        repo_root = Path(__file__).resolve().parents[1]
        proc = subprocess.run(
            [_sys.executable, "-m", "pytest", str(repo_root / "tests"), "--collect-only", "-q",
             "-p", "no:ddtrace", "-p", "no:ddtrace.pytest_bdd"],
            capture_output=True, text=True, cwd=str(repo_root),
        )
        output = proc.stdout + "\n" + proc.stderr
        m = _re.search(r"(\d+)\s+tests?\s+collected", output)
        assert m, f"未能从 pytest --collect-only 解析收集数:\n{output[-800:]}"
        actual_collected = int(m.group(1))
        assert TEST_BASELINE_PASSED == actual_collected, (
            f"单一事实源漂移：TEST_BASELINE_PASSED={TEST_BASELINE_PASSED} "
            f"!= 实际收集数 {actual_collected}（请同步 scripts/gates/constants.py）"
        )

    def test_wrong_baseline_fails(self, tmp_path: Path):
        from scripts.gates.constants import TEST_BASELINE_PASSED

        md = self._write_truth_doc(tmp_path, "wrong_baseline.md",
                                   f"当前基线 {TEST_BASELINE_PASSED + 5} passed。")
        res = self._eval(md)
        assert res.status == GateStatus.FAIL
        assert any(d["metric"] == "单测基线" for d in res.metrics["declaration_violations"])

    def test_correct_baseline_passes(self, tmp_path: Path):
        from scripts.gates.constants import TEST_BASELINE_PASSED

        md = self._write_truth_doc(tmp_path, "ok_baseline.md",
                                   f"当前基线 {TEST_BASELINE_PASSED} passed。")
        assert self._eval(md).status == GateStatus.PASS

    def test_historical_snapshot_doc_exempt_and_counted(self, tmp_path: Path):
        """历史归档快照（GATE_PHASE*）的门禁数/基线**豁免**，但必须可见计数。"""
        d = tmp_path / "docs" / "delivery"
        d.mkdir(parents=True)
        md = d / "GATE_PHASE3_COMPLETION_SUMMARY.md"
        md.write_text(
            "# t\n\n最大回撤 43.08%\n24 道机读门禁 / 717 passed\n", encoding="utf-8",
        )
        res = self._eval(md)
        assert res.status == GateStatus.PASS
        assert res.metrics["historical_snapshot_docs"] == 1

    def test_expected_values_are_dynamic(self):
        from scripts.gates.constants import TEST_BASELINE_PASSED
        from scripts.gates.gate_consistency import expected_gate_count, expected_test_baseline

        assert expected_gate_count({}) == len(GateMasterAudit.get_standard_gates())
        assert expected_test_baseline({}) == TEST_BASELINE_PASSED
        # ctx 注入可覆盖（测试/复跑用）
        assert expected_gate_count({"expected_gate_count": 7}) == 7
        assert expected_test_baseline({"expected_test_baseline": 123}) == 123


# =====================================================================
# 9c. ㉘ 收紧后的结构化门禁数模式：正例必命中、反例必不命中（双向）
# =====================================================================

class TestGateCountStructuredPatterns:
    """把门禁数判定从"同行含关键词即命中"收紧为**结构化匹配**后，正/反例双向锁定。

    正例（应违规）：数字**直接修饰**门禁/闸门（模式 A）或门禁在前、数量在后（模式 B）；
    反例（不得违规）：量词修饰的是**指标/单测/月数**等非门禁之物，或"引用单一事实源"。
    """

    @staticmethod
    def _doc(tmp_path: Path, name: str, line: str) -> Path:
        truth = json.loads(_AUTHORITATIVE_RUN.read_text(encoding="utf-8"))["metrics"]
        md = tmp_path / name
        md.write_text(
            f"# t\n\n最大回撤 {float(truth['max_drawdown']) * 100:.2f}%\n{line}\n",
            encoding="utf-8",
        )
        return md

    @staticmethod
    def _decls(md: Path) -> list[dict]:
        res = DocMetricConsistencyGate().evaluate(
            {"doc_paths": [str(md)], "truth_run_path": str(_AUTHORITATIVE_RUN)}
        )
        return [d for d in res.metrics.get("declaration_violations", []) if d["metric"] == "门禁数量"]

    @pytest.mark.parametrize("phrase", [
        "共 24 道门禁。",
        "共 28 道门禁。",
        "**28** 道门禁。",
        "28 项机读门禁。",
        "门禁包已重做为 28 道（`scripts/gates/`）。",
        "总计 28 道机读门禁。",
        "共 129 道门禁（子串不得放过）。",
    ])
    def test_positive_phrases_are_flagged(self, tmp_path: Path, phrase: str):
        """正例：数字直接修饰门禁/闸门（模式 A）或门禁在前、数量在后（模式 B）⇒ 必命中。"""
        n = len(GateMasterAudit.get_standard_gates())
        # 前提：这些字面量须 ≠ 当前门禁数，否则"命中"无意义（自校验，防未来漂移失真）。
        assert n not in (24, 28, 129), "测试前提失效：门禁数恰为 24/28/129，正例字面量失真"
        decls = self._decls(self._doc(tmp_path, "pos.md", phrase))
        assert decls, f"正例未被命中（漏判）：{phrase!r}"

    @pytest.mark.parametrize("phrase", [
        "G4.5 门禁 3 条必达指标全部满足。",
        "（52 个单测 100% 全绿覆盖）",
        "模拟盘 ≥6 个月 + 用户书面确认后才可小额实盘。",
        "门禁数量以 scripts/gates/gate_master_audit.py 的注册表为单一事实源（⛔ 不硬编码）。",
    ])
    def test_negative_phrases_are_not_flagged(self, tmp_path: Path, phrase: str):
        """反例：量词修饰的是指标/单测/月数等非门禁之物 ⇒ 必不命中（不得误报）。"""
        decls = self._decls(self._doc(tmp_path, "neg.md", phrase))
        assert not decls, f"反例被误报：{phrase!r} -> {decls}"

    def test_no_cross_line_false_positive(self, tmp_path: Path):
        """旧实现 ``\\s*`` 吞换行 ⇒「…门禁\\n3 项…」被误连成 ``门禁\\n3``；收紧后不得跨行。

        真实误报实例：`docs/t313_dividend_stress_report.md:290`（行尾"…门禁" + 下行"3 项…"）。
        """
        truth = json.loads(_AUTHORITATIVE_RUN.read_text(encoding="utf-8"))["metrics"]
        md = tmp_path / "cross.md"
        md.write_text(
            f"# t\n\n最大回撤 {float(truth['max_drawdown']) * 100:.2f}%\n"
            "该产物未通过回撤上限门禁\n3 项必达指标中 1 项未满足\n",
            encoding="utf-8",
        )
        assert not self._decls(md), "跨行（门禁\\n3）误报未被消除"

    def test_real_repo_docs_have_no_declaration_drift(self):
        """真实仓库现状：收紧模式后，G-DOC-1 的门禁数/基线声明漂移应为 0（收口目标）。"""
        res = DocMetricConsistencyGate().evaluate({})
        assert res.status == GateStatus.PASS, res.message
        assert res.metrics.get("declaration_violations_total", 0) == 0, (
            f"仍有声明漂移：{res.metrics.get('declaration_violations')}"
        )


# =====================================================================
# 9d. ② 行内 gate-doc-ignore 豁免的作用域/可见性/理由契约（防新逃逸口）
# =====================================================================

class TestGateDocIgnoreScope:
    """行内豁免必须：① 只豁免本行（不扩散）；② 计数可见；③ 须带**非空理由**。"""

    _REASON = "阶段一历史快照（当时值），⛔ 不改史"

    @staticmethod
    def _doc(tmp_path: Path, name: str, *extra_lines: str) -> Path:
        truth = json.loads(_AUTHORITATIVE_RUN.read_text(encoding="utf-8"))["metrics"]
        md = tmp_path / name
        md.write_text(
            "# t\n\n"
            f"最大回撤 {float(truth['max_drawdown']) * 100:.2f}%\n"
            + "\n".join(extra_lines) + "\n",
            encoding="utf-8",
        )
        return md

    def _eval(self, md: Path):
        return DocMetricConsistencyGate().evaluate(
            {"doc_paths": [str(md)], "truth_run_path": str(_AUTHORITATIVE_RUN)}
        )

    def test_ignore_is_line_scoped_not_file_wide(self, tmp_path: Path):
        """豁免**只作用于本行**：同文件另一行（无 ignore）的漂移仍必须被报出。"""
        n = len(GateMasterAudit.get_standard_gates())
        md = self._doc(
            tmp_path, "scope.md",
            f"本仓共 {n + 5} 道门禁。 <!-- gate-doc-ignore: {self._REASON} -->",   # 豁免
            f"另有 {n + 7} 道门禁。",                                              # 未豁免 ⇒ 必须报
        )
        res = self._eval(md)
        assert res.status == GateStatus.FAIL, "行内豁免不得扩散到整份文档"
        decls = res.metrics["declaration_violations"]
        assert all(d["doc_value"] == str(n + 7) for d in decls), f"被豁免行不应报出：{decls}"
        assert res.metrics["ignored_lines"] == 1, "豁免计数应只含被豁免的那 1 行"

    def test_bare_marker_without_reason_is_not_effective(self, tmp_path: Path):
        """裸标记 ``<!-- gate-doc-ignore -->`` **不生效** ⇒ 该行照常校验（类比 void 需非空 reason）。"""
        n = len(GateMasterAudit.get_standard_gates())
        md = self._doc(tmp_path, "bare.md", f"本仓共 {n + 5} 道门禁。 <!-- gate-doc-ignore -->")
        res = self._eval(md)
        assert res.status == GateStatus.FAIL, "裸 ignore 标记不得生效"
        assert res.metrics["ignored_lines"] == 0

    def test_empty_reason_is_not_effective(self, tmp_path: Path):
        """``<!-- gate-doc-ignore: -->`` 理由栏为空 ⇒ **不生效**。"""
        n = len(GateMasterAudit.get_standard_gates())
        md = self._doc(tmp_path, "empty_reason.md", f"本仓共 {n + 5} 道门禁。 <!-- gate-doc-ignore: -->")
        res = self._eval(md)
        assert res.status == GateStatus.FAIL, "空理由 ignore 标记不得生效"
        assert res.metrics["ignored_lines"] == 0

    def test_wrong_number_with_empty_reason_is_flagged(self, tmp_path: Path):
        """② 点名场景：含 ignore 的行若数字与真值不符**且理由为空** ⇒ 不得生效（必须报）。"""
        n = len(GateMasterAudit.get_standard_gates())
        md = self._doc(tmp_path, "wrong_no_reason.md", f"共 {n + 9} 道门禁 <!-- gate-doc-ignore:  -->")
        res = self._eval(md)
        assert res.status == GateStatus.FAIL
        assert any(d["metric"] == "门禁数量" and d["doc_value"] == str(n + 9)
                   for d in res.metrics["declaration_violations"])

    def test_ignore_count_is_visible_in_message(self, tmp_path: Path):
        """豁免必须**可见**：``ignored_lines`` 须出现在 message 与 metrics（⛔ 不得静默隐藏）。"""
        n = len(GateMasterAudit.get_standard_gates())
        md = self._doc(tmp_path, "visible.md", f"本仓共 {n + 5} 道门禁。 <!-- gate-doc-ignore: {self._REASON} -->")
        res = self._eval(md)
        assert res.metrics["ignored_lines"] == 1
        assert "ignored_lines: 1" in res.message

    def test_line_ignore_reason_helper_contract(self):
        from scripts.gates.gate_consistency import line_ignore_reason

        assert line_ignore_reason("x <!-- gate-doc-ignore: 历史快照 -->") == "历史快照"
        assert line_ignore_reason("x <!-- gate-doc-ignore -->") is None
        assert line_ignore_reason("x <!-- gate-doc-ignore: -->") is None
        assert line_ignore_reason("x <!-- gate-doc-ignore:    -->") is None
        assert line_ignore_reason("普通行，无标记") is None

    def test_ref_gate_ignore_line_scoped_and_counted(self, tmp_path: Path):
        """G-REF-1 同行内豁免：被豁免行的幽灵路径不报，未豁免行照报，计数可见。"""
        md = tmp_path / "ref_scope.md"
        md.write_text(
            "# t\n"
            "旧路径 `data/stress_test/` <!-- gate-doc-ignore: 路径已迁移（历史） -->\n"
            "新幽灵 `data/stress_test/other/`\n",
            encoding="utf-8",
        )
        res = DocPathReferenceGate().evaluate({"doc_paths": [str(md)]})
        assert res.status == GateStatus.FAIL
        refs = {v["reference"] for v in res.metrics["violations"]}
        assert "data/stress_test/other/" in refs
        assert res.metrics["ignored_lines"] == 1
        assert "ignored_lines: 1" in res.message

    def test_ref_gate_bare_marker_not_effective(self, tmp_path: Path):
        md = tmp_path / "ref_bare.md"
        md.write_text("见 `data/stress_test/` <!-- gate-doc-ignore -->\n", encoding="utf-8")
        res = DocPathReferenceGate().evaluate({"doc_paths": [str(md)]})
        assert res.status == GateStatus.FAIL
        assert res.metrics["ignored_lines"] == 0

    def test_repo_ignore_usage_is_only_the_historical_snapshot_line(self):
        """全仓 ignore 使用清单守卫：当前仅 1 处（阶段一历史快照行），⛔ 防滥用扩散。"""
        from scripts.gates.gate_consistency import line_ignore_reason

        root = Path(__file__).resolve().parents[1]
        hits: list[tuple[str, int]] = []
        for p in list(root.glob("docs/**/*.md")) + [root / "README.md", root / "CLAUDE.md"]:
            if not p.is_file():
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
                if line_ignore_reason(line) is not None:
                    hits.append((str(p.relative_to(root)).replace("\\", "/"), i))
        assert hits == [("docs/project_status_flowchart.md", 117)], (
            f"行内 ignore 使用清单发生变化（新增豁免须复核是否为真历史快照）：{hits}"
        )


# =====================================================================
# 10. 任务 1：晋升/准入层（G-MDD-1 的 BLOCKER 归属地）
# =====================================================================

class TestAcceptanceGate:
    """准入入口：对 43.08% 回撤产物判 FAIL 且 exit≠0；健康产物 PASS。"""

    def test_acceptance_fails_on_authoritative_artifact(self):
        from scripts.gates.acceptance import evaluate_acceptance, main as acc_main

        ok, criteria, _ = evaluate_acceptance(_AUTHORITATIVE_RUN)
        assert ok is False
        mdd_crit = next(c for c in criteria if c["id"] == "G-MDD-1")
        assert mdd_crit["passed"] is False
        assert acc_main(["--artifact", str(_AUTHORITATIVE_RUN)]) == 1    # exit≠0

    def test_acceptance_passes_on_healthy_artifact(self, tmp_path: Path):
        """完整健康产物（签署 + 现行 schema + 出处三件套）⇒ 放行。

        ⛔ 不得为了通过而放松准入门禁：本用例构造的是**真正完整**的产物，
        未签名/legacy 的产物必须被挡住（见 TestAcceptanceFailClosed）。
        """
        from scripts.gates.acceptance import evaluate_acceptance, main as acc_main
        from scripts.gates.tamper_guard import sign_run_record

        art = tmp_path / "healthy.json"
        art.write_text(json.dumps(sign_run_record({
            "run_id": "healthy", "status": "FINISHED", "schema_version": 2,
            "code_version": "healthy-code", "data_version": "healthy-data",
            "timestamp": "2026-09-10T00:00:00+08:00",
            "params_hash": "healthy-params",
            "metrics": {"max_drawdown": "0.12", "win_rate": "0.55",
                        "annual_turnover": "2.0", "round_trips": 40},
        }), ensure_ascii=False), encoding="utf-8")
        ok, criteria, _ = evaluate_acceptance(art)
        assert ok is True, [c for c in criteria if not c["passed"]]
        assert acc_main(["--artifact", str(art)]) == 0

    def test_master_audit_acceptance_flag_exits_nonzero(self, monkeypatch):
        import scripts.gates.gate_master_audit as gma

        monkeypatch.setattr(sys, "argv",
                            ["gate_master_audit", "--acceptance", str(_AUTHORITATIVE_RUN)])
        with pytest.raises(SystemExit) as ei:
            gma.main()
        assert ei.value.code == 1


# =====================================================================
# 11. 任务 1 三层分层：推送期 WARN（G-MDD-1 展示但不阻断）
# =====================================================================

class TestThreeLayerMddPushWarn:
    """推送期 G-MDD-1 命中 43.08% 也不得阻断推送，但必须明确打印 ``[WARN]``。"""

    def test_push_guard_does_not_block_on_mdd_fail(self, monkeypatch, capsys):
        import scripts.hooks.pre_push as pp
        from scripts.gates.base import GateResult as _GR

        def _mdd_fail() -> _GR:
            return _GR(
                gate_id="G-MDD-1", name="最大回撤上限门禁", category=GateCategory.G_GATE,
                status=GateStatus.FAIL, severity=GateSeverity.BLOCKER,
                message="检出 1 份产物最大回撤超限：x MDD=0.4308 > 0.35 （43.08%，往返 78 笔）",
            )

        class _FakeMaster:
            def __init__(self, *a, **k):  # noqa: ANN002, ANN003
                pass

            @staticmethod
            def get_push_time_gates():
                return []

            @staticmethod
            def get_standard_gates():
                return [object()]      # ①③ 口径行需要"全库"计数

            def audit(self, context=None, strict=False):  # noqa: ANN001
                return [_mdd_fail()]

        monkeypatch.setattr(pp, "GateMasterAudit", _FakeMaster)
        ok, msg = pp.run_master_gate_guard()
        out = capsys.readouterr().out
        assert ok is True, "推送期 G-MDD-1 属 WARN，不得阻断推送"
        assert "[WARN] G-MDD-1" in out
        assert "MDD=0.4308 > 0.35" in out


# =====================================================================
# 11b. ①③ 推送期成功行的**口径**：推送期子集 vs 全库，⛔ 不得混用
# =====================================================================

class TestPrePushGateCountWording:
    """``len(results)`` 是**推送期子集**（``PUSH_TIME_GATE_IDS``），⛔ 不得打印成"共注册 N 道"。

    并须提示"另有 N 道需回测证据的门禁不在推送期校验"，否则"推送期全绿"会被误读为"全库全绿"。
    """

    def _fake_master(self, push_results_n: int, total_n: int):
        from scripts.gates.base import GateResult as _GR
        from scripts.gates.base import GateCategory as _GC, GateSeverity as _GS

        def _pass(i: int) -> _GR:
            return _GR(gate_id=f"X-{i}", name=f"g{i}", category=_GC.G_GATE,
                       status=GateStatus.PASS, severity=_GS.INFO, message="ok")

        class _FakeMaster:
            def __init__(self, *a, **k):  # noqa: ANN002, ANN003
                pass

            @staticmethod
            def get_push_time_gates():
                return []

            @staticmethod
            def get_standard_gates():
                return [object()] * total_n

            def audit(self, context=None, strict=False):  # noqa: ANN001
                return [_pass(i) for i in range(push_results_n)]

        return _FakeMaster

    def test_success_line_states_both_scopes(self, monkeypatch):
        import scripts.hooks.pre_push as pp

        monkeypatch.setattr(pp, "GateMasterAudit", self._fake_master(3, 29))
        ok, msg = pp.run_master_gate_guard()
        assert ok is True
        assert "推送期 3 道" in msg, f"须标出推送期子集口径：{msg}"
        assert "全库 29 道" in msg, f"须标出全库口径：{msg}"
        assert "另有 26 道" in msg and "不在推送期校验" in msg, f"须提示未覆盖门禁：{msg}"
        assert "共注册" not in msg, f"⛔ 不得把子集说成'共注册 N 道门禁'：{msg}"

    def test_success_line_no_deferred_note_when_full_scope(self, monkeypatch):
        """全库 == 推送期（子集已全覆盖）时，无需"另有"提示。"""
        import scripts.hooks.pre_push as pp

        monkeypatch.setattr(pp, "GateMasterAudit", self._fake_master(29, 29))
        ok, msg = pp.run_master_gate_guard()
        assert ok is True
        assert "推送期 29 道 / 全库 29 道" in msg
        assert "另有" not in msg


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
