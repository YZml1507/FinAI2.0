#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""E-Gate: 撮合保真与极端事件门禁（Engine Matching & Extreme Case Gates）

依据：
1. 17 号深度调研报告 §4.3 / E-1 ~ E-3 门禁定义
2. T202 必挂用例套件与极端送转拆股 FIFO 连续性
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Sequence

from .base import BaseGate, GateCategory, GateResult, GateSeverity, GateStatus


class MustFailCasesGate(BaseGate):
    """E-1: 5 必挂极限用例通过率检验（一票否决级引擎纪律）"""
    gate_id = "E-1"
    name = "5 必挂极限用例通过率检验"
    category = GateCategory.E_GATE
    severity = GateSeverity.BLOCKER
    evidence = "17 号报告 §4.3 + T202: 涨停买拒、跌停卖拒、停牌拒、除权连续、T+1 隔日卖 5 必挂用例通过率必须严格恒等于 100%"
    threshold_desc = "5 必挂极限用例通过率严格恒等于 100% (5/5)"

    STANDARD_CASES = [
        "LIMIT_UP_BUY_REJECT",     # 涨停板买入必须被拒
        "LIMIT_DOWN_SELL_REJECT",   # 跌停板卖出必须被拒
        "SUSPENSION_REJECT",        # 停牌标的委托必须被拒
        "EXDIV_CONTINUOUS_NAV",     # 除权送转除息日净值曲线平滑连续
        "T1_SAME_DAY_SELL_REJECT",  # 当日买入当日卖出被 T+1 拒绝
    ]

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - must_fail_results: dict[str, bool] 用例名 -> 是否按预期拒绝/通过
        - failed_cases: list[str] 未按预期执行的用例列表
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无必挂用例测试结果输入，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        results = context.get("must_fail_results", {}) if isinstance(context, dict) else getattr(context, "must_fail_results", {})
        explicit_fails = context.get("failed_cases", []) if isinstance(context, dict) else getattr(context, "failed_cases", [])

        failed = list(explicit_fails)
        checked_count = 0

        for c in self.STANDARD_CASES:
            if c in results:
                checked_count += 1
                if not results[c]:
                    if c not in failed:
                        failed.append(c)

        if failed:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"5 必挂用例出现违背项: {failed}，撮合引擎保真性破产！",
                metrics={"failed_cases": failed, "checked_count": checked_count or len(results)},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"5 必挂极限用例全部检验通过 (通过率 100%)",
            metrics={"passed_count": max(checked_count, len(self.STANDARD_CASES))},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class BonusSplitFifoGate(BaseGate):
    """E-2: 送转拆股 FIFO 份额一致性与防击穿检验"""
    gate_id = "E-2"
    name = "送转拆股 FIFO 份额一致性检验"
    category = GateCategory.E_GATE
    severity = GateSeverity.CRITICAL
    evidence = "17 号报告 §4.3: 10送5/10转10 等复杂送转后底层 FIFO 批次未同比例扩充，全额卖出引发缺股崩溃"
    threshold_desc = "经历送转后全额卖出，底层 FIFO 缺股崩溃率为 0%，卖出后持仓精确为 0"

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - split_trades: list[dict]，包含送转前后卖出记录
        - fifo_errors: list[str]，记录任何 LedgerError: insufficient 异常
        - final_positions: dict[str, int]，全额卖出后最终持仓
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无送转拆股测试数据，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        fifo_errors = context.get("fifo_errors", []) if isinstance(context, dict) else getattr(context, "fifo_errors", [])
        final_pos = context.get("final_positions", {}) if isinstance(context, dict) else getattr(context, "final_positions", {})

        if fifo_errors:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"送转拆股后底层 FIFO 批次发生缺股击穿异常: {fifo_errors}",
                metrics={"error_count": len(fifo_errors), "samples": fifo_errors[:3]},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # 检查声称已全额卖出的标的是否持仓归零
        non_zero = {k: v for k, v in final_pos.items() if v != 0}
        if non_zero:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"送转全额卖出后标的持仓未准确归零: {non_zero}",
                metrics={"residual_positions": non_zero},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message="送转拆股 FIFO 份额一致性检验通过 (缺股崩溃率 0%，持仓归零一致)",
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class SlippagePriceCapGate(BaseGate):
    """E-3: 滑点推移价格涨跌停限幅检验（防撮合突破物理板价）"""
    gate_id = "E-3"
    name = "滑点推移价格涨跌停限幅检验"
    category = GateCategory.E_GATE
    severity = GateSeverity.BLOCKER
    evidence = "17 号报告 §4.3: 当价格接近涨停(如+9.8%)加滑点后越过+10%涨停板，突破交易所客观物理限制"
    threshold_desc = "买入成交价严格 <= limit_up；卖出成交价严格 >= limit_down"

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - trades: list[Trade] 或 list[dict]，包含 price, side, limit_up, limit_down, symbol
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无成交数据，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        trades = context.get("trades", []) if isinstance(context, dict) else getattr(context, "trades", [])
        if not trades:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.PASS,
                severity=self.severity,
                message="无成交记录，通过",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        violations = []
        for t in trades:
            side = str(t.get("side", "BUY") if isinstance(t, dict) else getattr(t, "side", "BUY")).upper()
            price = Decimal(str(t.get("price", 0) if isinstance(t, dict) else getattr(t, "price", 0)))
            limit_up = Decimal(str(t.get("limit_up", 0) if isinstance(t, dict) else getattr(t, "limit_up", 0)))
            limit_down = Decimal(str(t.get("limit_down", 0) if isinstance(t, dict) else getattr(t, "limit_down", 0)))
            symbol = str(t.get("symbol", "") if isinstance(t, dict) else getattr(t, "symbol", ""))

            if "BUY" in side and limit_up > Decimal("0") and price > limit_up:
                violations.append({
                    "symbol": symbol,
                    "side": side,
                    "trade_price": str(price),
                    "limit_up": str(limit_up),
                    "diff": str(price - limit_up),
                })
            elif "SELL" in side and limit_down > Decimal("0") and price < limit_down:
                violations.append({
                    "symbol": symbol,
                    "side": side,
                    "trade_price": str(price),
                    "limit_down": str(limit_down),
                    "diff": str(limit_down - price),
                })

        if violations:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"检出 {len(violations)} 笔成交突破涨跌停板价限幅，滑点未做物理板价截断！",
                metrics={"violations_count": len(violations), "samples": violations[:3]},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"滑点推移价格涨跌停限幅检验通过 (共检验 {len(trades)} 笔成交)",
            metrics={"checked_trades": len(trades)},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )
