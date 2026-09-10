#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase 4 自动化验收测试套件 (T405 报备材料合规核验 + 模拟盘端到端日终执行与防篡改验签).

覆盖范围：
  1. T405 合规材料完整性断言：
     - 策略说明书 docs/compliance/strategy_description_template.md (Commit 4878ffe / T312 10年回测 / 单测基线声明，指标真值读产物)
     - 系统架构说明 docs/compliance/system_architecture_template.md (六层物理架构 / 零杠杆 / 5元佣金地板)
     - 报备材料清单 docs/compliance/filing_checklist.md (必须项 7 项 + 必须项审签闭环)
     - T405 合规审计报告 docs/compliance/T405_COMPLIANCE_AUDIT.md (100% PASS 终审)
  2. 模拟盘端到端管道测试：
     - 冷启动初始化 (本金 100,000，NAV 100,000，对账 PASS)
     - 热启动状态恢复与连贯推进
     - 密码学防篡改签名注水与验签 (SHA-256 出处三件套绑定)
     - 篡改检测 (修改 NAV 后签名失效拦截)
     - 幂等保护 (同日重跑安全跳过)
     - 总账与索引流式追加核验
     - 经纪商红利税开启校验
"""
from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from backtest.ledger import Ledger
from backtest.matching import MatchEngine
from paper_trading.broker import PaperBroker
from paper_trading.config import ConfigError, PaperTradingConfig
from paper_trading.runner import PaperTradingRunner
from paper_trading.state import PaperTradingState
from scripts.gates.tamper_guard import compute_run_signature, sign_run_record
from scripts.run_paper_trading_daily import run_daily_pipeline
from strategy.candidates import DividendConfig, DividendStrategy

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: 权威回测产物（T312 10 年全周期，带 anti_tamper_signature 的唯一事实源）。
_AUTHORITATIVE_RUN = (
    _REPO_ROOT / "experiments" / "runs" / "20260907-150402-t312-dividend-v1-noseed.json"
)


def _authoritative_metrics() -> dict[str, Any]:
    """从权威产物读取真值指标（⛔ 测试不得反向锁定过期数字）。"""
    data = json.loads(_AUTHORITATIVE_RUN.read_text(encoding="utf-8"))
    return data["metrics"]


def _pct(value: Any) -> str:
    """把产物中的小数口径指标格式化为百分比字符串（如 2.011435 → '201.14%'）。"""
    return f"{float(value) * 100:.2f}%"


def _doc_declares_gate_count(text: str) -> bool:
    """文档是否**声明了**六维门禁数量（数字声明 或 单一事实源引用）。

    PM 现行文档政策（2026-09-10）= **不硬编码冻结值**，改为引用单一事实源
    （``scripts/gates/constants.TEST_BASELINE_PASSED`` / 门禁注册表）；
    故接受两种合法形态之一。⛔ 两者皆无 ⇒ 判 False（不得"永远通过"）。
    """
    return bool(
        re.search(r"\d+\s*(项|道)[^\n]{0,12}门禁", text)
        or "门禁注册表" in text
        or "单一事实源" in text
    )


# ======================================================================
# 1. T405 合规报备材料完整性与一致性测试
# ======================================================================

class TestT405ComplianceDocs:
    """T405 报备材料核对与归档自动化审计。"""

    def test_strategy_description_template_complete(self):
        """策略说明书必须锁定 commit 4878ffe、T312 10 年实证核心指标与门禁/基线声明。

        ⛔ TEST-FIX-1：指标真值一律从权威产物 `experiments/runs/20260907-150402-*.json`
        读取（移除 `assert "92.51%" in text` 这类**反向锁定过期数字**的断言）；
        测试基线（717）与门禁数量（24）改为只断言"存在声明"的格式，不再锁死会过期的具体数字。
        """
        doc = _REPO_ROOT / "docs" / "compliance" / "strategy_description_template.md"
        assert doc.exists(), f"策略说明书不存在: {doc}"
        text = doc.read_text(encoding="utf-8")
        metrics = _authoritative_metrics()

        # 核心出处锁定
        assert "4878ffe" in text, "策略说明书未锁定 Phase 4 基准 commit 4878ffe"
        # 单测基线与门禁数量：只断言"存在声明"，不锁死会过期的具体数字。
        # ⚠ 门禁数量现行文档政策（2026-09-10 PM 口径统一）= **不硬编码冻结值**，
        #    改为引用"单一事实源 / 门禁注册表"；故此处接受「数字声明」或「单一事实源引用」二者之一。
        assert re.search(r"\d+\s+passed", text), "策略说明书未记录单测基线声明（格式: '<N> passed'）"
        assert _doc_declares_gate_count(text), (
            "策略说明书未记录六维防御门禁数量声明（数字声明 或 单一事实源引用）"
        )

        # T312 10 年实证核心数据穿透（真值取自权威产物，而非硬编码）
        assert "2015-01-05" in text and "2024-12-31" in text, "未记录 10 年回测完整区间"
        assert _pct(metrics["cagr"]) in text, f"未载明与产物一致的 CAGR {_pct(metrics['cagr'])}"
        assert _pct(metrics["max_drawdown"]) in text, f"未载明与产物一致的 MDD {_pct(metrics['max_drawdown'])}"
        assert _pct(metrics["annual_turnover"]) in text, (
            f"未载明与产物一致的年化换手率 {_pct(metrics['annual_turnover'])}"
        )
        assert _pct(metrics["win_rate"]) in text, f"未载明与产物一致的胜率 {_pct(metrics['win_rate'])}"
        assert "5,043.75" in text or "5043.75" in text, "未载明实扣红利税 5043.75 元"

        # 物理约束声明
        assert "最高申报速率" in text and "<1 笔/分钟" in text
        assert "单日最高申报笔数" in text and "20 笔" in text
        assert "无杠杆" in text

    def test_gate_count_declaration_assertion_not_vacuous(self):
        """防'永远通过'：文档**既不写数字、又不引用单一源**时，该断言必须判 False。

        证明 :func:`_doc_declares_gate_count` 的放宽（接受单一事实源引用）不是空断言。
        """
        assert _doc_declares_gate_count("六维防御门禁：已实现（无数字、无引用）") is False
        assert _doc_declares_gate_count("22 项门禁已闭环") is True
        assert _doc_declares_gate_count("数量真值以 `gate_master_audit.py` 的门禁注册表 为单一事实源") is True

    def test_system_architecture_template_complete(self):
        """系统架构说明书必须完备披露六层物理架构与防伪防线。"""
        doc = _REPO_ROOT / "docs" / "compliance" / "system_architecture_template.md"
        assert doc.exists(), f"系统架构说明书不存在: {doc}"
        text = doc.read_text(encoding="utf-8")

        # 六层架构声明
        assert "数据层" in text
        assert "回测与撮合引擎" in text or "引擎" in text
        assert "策略" in text
        assert "风控" in text
        assert "运维" in text
        assert "六维防伪门禁" in text or "门禁" in text

        # 物理约束与防伪门禁
        assert "10~15 万" in text or "10-15 万" in text
        assert "5 元佣金地板" in text or "最低 5 元" in text
        assert "纯多头现货" in text
        assert "六维防御门禁" in text or "D-L-E-A-S-G" in text

    def test_filing_checklist_and_audit_report(self):
        """报备材料清单与 T405 审计报告必须完备且签字闭环。"""
        checklist = _REPO_ROOT / "docs" / "compliance" / "filing_checklist.md"
        audit = _REPO_ROOT / "docs" / "compliance" / "T405_COMPLIANCE_AUDIT.md"

        assert checklist.exists(), f"清单不存在: {checklist}"
        assert audit.exists(), f"审计报告不存在: {audit}"

        cl_text = checklist.read_text(encoding="utf-8")
        au_text = audit.read_text(encoding="utf-8")

        # 清单项核验状态
        assert "[x] **2. 策略说明书**" in cl_text
        assert "[x] **3. 系统架构说明**" in cl_text
        assert "[x] **4. 回测与压力测试报告**" in cl_text
        assert "[x] **6. 风控措施说明**" in cl_text
        assert "[x] **7. 最高申报速率/笔数承诺**" in cl_text

        # 审计报告结论
        assert "FR-COMP-1" in au_text
        assert "100% PASS" in au_text or "核验通过" in au_text
        assert "4878ffe" in au_text


# ======================================================================
# 2. 模拟盘端到端管道与状态推进测试
# ======================================================================

class TestPaperTradingPipelineE2E:
    """模拟盘日终单日与多日执行管道端到端测试。"""

    def test_paper_broker_enables_dividend_tax_by_default(self):
        """PaperBroker 默认必须开启红利税 modeling。"""
        matcher = MatchEngine()
        ledger = Ledger(Decimal("100000"), date=date(2026, 9, 7))
        broker = PaperBroker(matcher, ledger)
        assert broker.enable_dividend_tax is True, "PaperBroker 默认必须开启红利税"

    def test_cold_start_daily_pipeline(self, tmp_path: Path):
        """测试模拟盘冷启动首日全流程执行、落盘与签名。"""
        runs_dir = tmp_path / "runs" / "paper_trading"
        data_dir = tmp_path / "data" / "daily_bars"
        data_dir.mkdir(parents=True, exist_ok=True)

        test_date = "2026-09-07"
        success, record = run_daily_pipeline(
            run_date=test_date,
            capital=Decimal("100000"),
            data_root=data_dir,
            output_dir=runs_dir,
            offline=True,
            dry_run=False,
            repo_root=tmp_path,
        )

        assert success is True
        assert record["status"] == "FINISHED"
        assert record["metrics"]["nav"] == "100000"
        assert record["metrics"]["cash"] == "100000"
        assert record["metrics"]["reconciliation_ok"] is True
        assert "anti_tamper_signature" in record
        assert len(record["anti_tamper_signature"]) == 64

        # 验证物理落盘文件
        daily_json = runs_dir / f"daily_run_{test_date}.json"
        index_file = runs_dir / "daily_index.jsonl"
        state_file = runs_dir / "state.json"
        ledger_file = tmp_path / "docs" / "paper_trading" / "paper_trading_ledger.md"

        assert daily_json.exists(), "每日运行记录未落盘"
        assert index_file.exists(), "运行索引未落盘"
        assert state_file.exists(), "状态文件未保存"
        assert ledger_file.exists(), "模拟盘总账未生成"

        # 校验落盘内容与签名一致性
        saved_record = json.loads(daily_json.read_text(encoding="utf-8"))
        assert saved_record["run_id"] == record["run_id"]
        expected_sig = compute_run_signature(saved_record)
        assert saved_record["anti_tamper_signature"] == expected_sig

        # 校验总账包含当日行
        ledger_content = ledger_file.read_text(encoding="utf-8")
        assert test_date in ledger_content
        assert "100,000.00" in ledger_content

    def test_hot_start_consecutive_days_idempotency(self, tmp_path: Path):
        """测试热启动次日执行与幂等跳过保护。"""
        runs_dir = tmp_path / "runs" / "paper_trading"
        data_dir = tmp_path / "data" / "daily_bars"
        data_dir.mkdir(parents=True, exist_ok=True)

        # Day 1
        d1 = "2026-09-07"
        s1, r1 = run_daily_pipeline(
            run_date=d1,
            capital=Decimal("100000"),
            data_root=data_dir,
            output_dir=runs_dir,
            offline=True,
            dry_run=False,
            repo_root=tmp_path,
        )
        assert s1 is True
        assert r1["status"] == "FINISHED"

        # Day 1 重跑 (幂等保护：已执行过跳过)
        s1_repeat, r1_repeat = run_daily_pipeline(
            run_date=d1,
            capital=Decimal("100000"),
            data_root=data_dir,
            output_dir=runs_dir,
            offline=True,
            dry_run=False,
            repo_root=tmp_path,
        )
        assert s1_repeat is True
        assert r1_repeat["metrics"]["success"] is True

        # Day 2 热启动推进
        d2 = "2026-09-08"
        s2, r2 = run_daily_pipeline(
            run_date=d2,
            capital=Decimal("100000"),
            data_root=data_dir,
            output_dir=runs_dir,
            offline=True,
            dry_run=False,
            repo_root=tmp_path,
        )
        assert s2 is True
        assert r2["status"] == "FINISHED"
        assert r2["date"] == d2

        # 验证状态中的 last_trading_date 推进到 Day 2
        state = PaperTradingState.load(runs_dir / "state.json")
        assert state.last_trading_date == d2

    def test_tamper_guard_catches_metric_modification(self, tmp_path: Path):
        """防篡改验签引擎能够精准检测出指标篡改。"""
        runs_dir = tmp_path / "runs" / "paper_trading"
        data_dir = tmp_path / "data" / "daily_bars"
        data_dir.mkdir(parents=True, exist_ok=True)

        _, record = run_daily_pipeline(
            run_date="2026-09-07",
            capital=Decimal("100000"),
            data_root=data_dir,
            output_dir=runs_dir,
            offline=True,
            dry_run=False,
            repo_root=tmp_path,
        )

        original_sig = record["anti_tamper_signature"]
        # 验证原始签名匹配
        assert compute_run_signature(record) == original_sig

        # 恶意篡改 NAV
        tampered_record = dict(record)
        tampered_metrics = dict(record["metrics"])
        tampered_metrics["nav"] = "200000"
        tampered_record["metrics"] = tampered_metrics

        # 篡改后签名必然不匹配
        tampered_sig = compute_run_signature(tampered_record)
        assert tampered_sig != original_sig, "防篡改引擎未能识别 NAV 被篡改"

    def test_dry_run_does_not_pollute_disk(self, tmp_path: Path):
        """干跑模式测试：不生成持久化 state.json 与 daily_run 文件。"""
        runs_dir = tmp_path / "runs" / "paper_trading"
        data_dir = tmp_path / "data" / "daily_bars"
        data_dir.mkdir(parents=True, exist_ok=True)

        test_date = "2026-09-07"
        success, record = run_daily_pipeline(
            run_date=test_date,
            capital=Decimal("100000"),
            data_root=data_dir,
            output_dir=runs_dir,
            offline=True,
            dry_run=True,
            repo_root=tmp_path,
        )

        assert success is True
        assert record["status"] == "FINISHED"
        assert not (runs_dir / f"daily_run_{test_date}.json").exists()
        assert not (runs_dir / "state.json").exists()
        assert not (runs_dir / ".state_dryrun.json").exists()
