#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S-Gate: 散户小资金科学防伪与择时生存门禁（Retail Capital & Scientific Anti-Overfitting Gates）

依据：
1. 17 号深度调研报告 §4.5 / S-1 ~ S-5 门禁定义
2. 专为 10~15 万元、纯多头、无对冲、5 元最低佣金地板设立的散户科学约束
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Sequence

from .base import BaseGate, GateCategory, GateResult, GateSeverity, GateStatus


class TurnoverCeilingGate(BaseGate):
    """S-1: 年化单边换手率硬顶检验（防 5 元最低佣金地板暴击）"""
    gate_id = "S-1"
    name = "年化换手率硬顶检验"
    category = GateCategory.S_GATE
    severity = GateSeverity.BLOCKER
    evidence = "17 号报告 §3.1 + §4.5: 散户 10~15 万资金频繁交易，5 元最低佣金地板暴击致交易摩擦吃掉全部超额"
    threshold_desc = "年化单边换手率必须 <= 400%"

    def __init__(self, max_turnover: float = 4.0) -> None:
        self.max_turnover = max_turnover

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - annualized_turnover: float | Decimal (例如 3.5 代表 350%)
        - 或者 total_volume, avg_nav 等计算参数
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无换手率数据，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        turnover = context.get("annualized_turnover") if isinstance(context, dict) else getattr(context, "annualized_turnover", None)
        if turnover is None:
            # ⛔ Fail-Closed：缺换手率数据 ⇒ INCONCLUSIVE（无证据 ≠ 通过）
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="缺少 annualized_turnover 参数，无法判定换手率硬顶（无证据 ≠ 通过）",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        t_val = float(turnover)
        if t_val > self.max_turnover:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"年化单边换手率 {t_val*100:.1f}% 超过散户硬顶 400%，5 元佣金地板将吞噬账户利润！",
                metrics={"annualized_turnover_pct": round(t_val * 100, 2), "max_allowed_pct": self.max_turnover * 100},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"换手率硬顶检验通过 (年化单边换手率 {t_val*100:.1f}% <= 400%)",
            metrics={"annualized_turnover_pct": round(t_val * 100, 2)},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class TimingExitSurvivalGate(BaseGate):
    """S-2: 沪深 300 破 MA200 择时空仓生存检验（纯多头避险生命线）"""
    gate_id = "S-2"
    name = "破 MA200 择时空仓生存检验"
    category = GateCategory.S_GATE
    severity = GateSeverity.BLOCKER
    evidence = "17 号报告 §3.3 + §4.5: 散户无做空与对冲工具，防深幅回撤唯一有效方式为大盘破 MA200 空仓避险"
    threshold_desc = "当基准指数跌破 MA200 超过 1 个调仓日后，策略持仓比例必须 <= 5%"

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - index_below_ma200_dates: list[str] 指数处于 MA200 下方的日期
        - daily_positions_ratio: dict[str, float] 日期 -> 策略持仓比例 (0.0 ~ 1.0)
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无择时与仓位数据，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        below_dates = context.get("index_below_ma200_dates", []) if isinstance(context, dict) else getattr(context, "index_below_ma200_dates", [])
        pos_ratios = context.get("daily_positions_ratio", {}) if isinstance(context, dict) else getattr(context, "daily_positions_ratio", {})

        # ⛔ Fail-Closed：数据缺失 ≠ 通过。缺证据 ⇒ INCONCLUSIVE；有证据表明不适用 ⇒ SKIP。
        have_below = isinstance(context, dict) and "index_below_ma200_dates" in context
        if not have_below:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="缺少破 MA200 日期证据（index_below_ma200_dates），无法判定择时空仓生存（无证据 ≠ 通过）",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )
        if not below_dates:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="回测区间内基准指数未跌破 MA200（有证据表明该门禁不适用）",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )
        if not pos_ratios:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="存在破 MA200 交易日，但缺少逐日仓位比例数据，无法判定是否已空仓避险（证据不足 ≠ 通过）",
                metrics={"below_dates_count": len(below_dates)},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        violations = []
        for dt in below_dates:
            ratio = pos_ratios.get(dt, 0.0)
            if ratio > 0.05:  # 持仓大于 5% 视为未空仓避险
                violations.append({
                    "date": dt,
                    "position_ratio": round(ratio * 100, 2),
                })

        if violations:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"检出 {len(violations)} 个基准破 MA200 交易日策略未空仓避险，存在熊市死扛重大违规！",
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
            message="破 MA200 择时空仓生存检验通过 (熊市破位区间持仓比例严格 <= 5%)",
            metrics={"below_dates_count": len(below_dates)},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class DynamicSlippageAdvGate(BaseGate):
    """S-3: 散户流动性容量与滑点抗压测试检验"""
    gate_id = "S-3"
    name = "散户流动性与滑点抗压测试检验"
    category = GateCategory.S_GATE
    severity = GateSeverity.CRITICAL
    evidence = "17 号报告 §3.3 + §4.5: 单笔委托不得超过 ADV 2% 散户冲击限额，滑点 +50% 压测收益率不得由正转负"
    threshold_desc = "单笔委托 <= ADV 2%，滑点 +50% 扰动后年化收益率仍必须保持为正 (> 0)"

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - orders_adv_ratio: list[float] 每笔委托量 / ADV_20
        - baseline_return: float
        - stress_return: float (滑点 +50% 压测后收益率)
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无容量或滑点压测数据，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        adv_ratios = context.get("orders_adv_ratio", []) if isinstance(context, dict) else getattr(context, "orders_adv_ratio", [])
        base_ret = context.get("baseline_return") if isinstance(context, dict) else getattr(context, "baseline_return", None)
        stress_ret = context.get("stress_return") if isinstance(context, dict) else getattr(context, "stress_return", None)

        # 检验 ADV 约束
        exceeded_adv = [r for r in adv_ratios if r > 0.02]
        if exceeded_adv:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"检出 {len(exceeded_adv)} 笔委托超过散户 ADV 2% 无冲击阈值 (最大 {max(exceeded_adv)*100:.2f}%)",
                metrics={"exceeded_count": len(exceeded_adv), "max_ratio": max(exceeded_adv)},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # ⛔ Fail-Closed：缺基准/压力情景收益率 ⇒ INCONCLUSIVE（无证据 ≠ 通过）
        if base_ret is None or stress_ret is None:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="缺少基准收益率或滑点压力情景收益率，无法判定抗压性（无证据 ≠ 通过）",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # 检验滑点 +50% 压测
        if base_ret is not None and stress_ret is not None:
            if base_ret > 0 and stress_ret <= 0:
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.FAIL,
                    severity=self.severity,
                    message=f"滑点 +50% 压力测试失败：收益率由基准 {base_ret*100:.2f}% 崩塌至 {stress_ret*100:.2f}%，过度拟合超低滑点！",
                    metrics={"baseline_return": base_ret, "stress_return": stress_ret},
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message="散户流动性容量与滑点抗压测试通过",
            metrics={"adv_checked": len(adv_ratios)},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class DividendTaxLockGate(BaseGate):
    """S-4: 红利税跨期避税锁定期检验"""
    gate_id = "S-4"
    name = "红利税避税锁定期检验"
    category = GateCategory.S_GATE
    severity = GateSeverity.CRITICAL
    evidence = "17 号报告 §4.5 + T309: 除权除息后持股不满 30 天卖出征收 20% 惩罚性红利税，高频换仓会产生严重避税损耗"
    threshold_desc = "20% 档惩罚性红利税占总分红收益比例严格 <= 20%"

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - penalty_tax_amount: Decimal | float (20% 档税款总额)
        - total_dividend_received: Decimal | float (总分红现金)
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无红利税数据，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        penalty = Decimal(str(context.get("penalty_tax_amount", 0) if isinstance(context, dict) else getattr(context, "penalty_tax_amount", 0)))
        total_div = Decimal(str(context.get("total_dividend_received", 0) if isinstance(context, dict) else getattr(context, "total_dividend_received", 0)))
        penalty_given = isinstance(context, dict) and "penalty_tax_amount" in context

        # ⛔ Fail-Closed：无分红 / 缺分档税数据均不得判通过。
        if total_div <= Decimal("0"):
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="回测区间无分红入账（总分红为 0），无法检验红利税避税锁定期（无证据 ≠ 通过）",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )
        if not penalty_given:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="有分红入账但缺少 20% 档惩罚性红利税分项数据，无法判定跨期税损（证据不足 ≠ 通过）",
                metrics={"total_div": str(total_div)},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        penalty_ratio = penalty / total_div
        if penalty_ratio > Decimal("0.20"):
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"持股不足 1 个月惩罚性红利税占比达 {penalty_ratio*100:.2f}% > 20%，策略盲目调仓引发严重跨期税损！",
                metrics={"penalty_tax": str(penalty), "total_div": str(total_div), "ratio": float(penalty_ratio)},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"红利税避税锁定期检验通过 (惩罚性红利税占比 {penalty_ratio*100:.2f}% <= 20%)",
            metrics={"penalty_ratio_pct": round(float(penalty_ratio) * 100, 2)},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class AttributionEvidenceGate(BaseGate):
    """S-5: 收益归因证据链与关税作弊自查检验"""
    gate_id = "S-5"
    name = "收益归因证据链与关税作弊自查检验"
    category = GateCategory.S_GATE
    severity = GateSeverity.BLOCKER
    evidence = "17 号报告 §4.5: 杜绝关税置零与凭空编造归因，收益归因必须附带具体代码行号和真实扣费证明"
    threshold_desc = "印花税与佣金费率严格非零，归因报告必须附带代码出处证据链"

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - total_stamp_tax: Decimal | float
        - total_commission: Decimal | float
        - code_evidence: str
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无归因数据，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        tax = Decimal(str(context.get("total_stamp_tax", 0) if isinstance(context, dict) else getattr(context, "total_stamp_tax", 0)))
        comm = Decimal(str(context.get("total_commission", 0) if isinstance(context, dict) else getattr(context, "total_commission", 0)))
        trades_count = int(context.get("trades_count", 0) if isinstance(context, dict) else getattr(context, "trades_count", 0))
        code_ev = str(context.get("code_evidence", "") if isinstance(context, dict) else getattr(context, "code_evidence", ""))

        if trades_count > 0:
            if tax <= Decimal("0") or comm <= Decimal("0"):
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.FAIL,
                    severity=self.severity,
                    message=f"在存在 {trades_count} 笔成交的情况下，印花税({tax})或佣金({comm})为零，判定为关税作弊！",
                    metrics={"trades_count": trades_count, "stamp_tax": str(tax), "commission": str(comm)},
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )

        if not code_ev:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message="收益归因缺少具体代码行号与物理证据链引用",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message="收益归因证据链与费率真实性检验通过",
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )
