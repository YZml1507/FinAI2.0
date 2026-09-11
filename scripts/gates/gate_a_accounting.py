#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A-Gate: 双账本分厘级会计对账门禁（Accounting & Balance Conservation Gates）

依据：
1. 17 号深度调研报告 §4.4 / A-1 ~ A-4 门禁定义
2. A 股真实规费体系与 Decimal 分厘级双账本守恒约束
"""

from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from .base import BaseGate, GateCategory, GateResult, GateSeverity, GateStatus


class FeeSumBalanceGate(BaseGate):
    """A-1: 七科目费用逐笔分厘平衡检验"""
    gate_id = "A-1"
    name = "七科目费用逐笔分厘平衡检验"
    category = GateCategory.A_GATE
    severity = GateSeverity.BLOCKER
    evidence = "17 号报告 §4.4: 交易各科目费用之和与账本总扣除差额绝对值 strictly |Delta| == 0.00 元"
    threshold_desc = "Trade fees 七科目求和与实际扣除总费用差额绝对值严格恒等于 0.00 元"

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - trades: list[Trade] 或 list[dict] 含 fees (dict)
        - ledger_entries: list[JournalEntry] 或 list[dict]
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无交易数据，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        trades = context.get("trades", []) if isinstance(context, dict) else getattr(context, "trades", [])
        if not trades:
            # ⛔ Fail-Closed：无成交证据 ⇒ INCONCLUSIVE（无证据 ≠ 通过）
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="无交易记录，无法对账七科目费用平衡（无证据 ≠ 通过）",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        discrepancies = []
        checked = 0
        for t in trades:
            trade_id = str(t.get("trade_id", "") if isinstance(t, dict) else getattr(t, "trade_id", ""))
            fees = t.get("fees", {}) if isinstance(t, dict) else getattr(t, "fees", {})
            total_fee_declared = Decimal(str(t.get("total_fee", 0) if isinstance(t, dict) else getattr(t, "total_fee", 0)))
            total_fee_given = ("total_fee" in t) if isinstance(t, dict) else hasattr(t, "total_fee")

            item_sum = Decimal("0")
            for _, amt in fees.items():
                item_sum += Decimal(str(amt))

            # 仅在明确提供了 total_fee（键存在，或值 > 0）时才可对账；
            # ⛔ 缺该字段的成交不得被默认值 0 抹平为"平衡"（0 == item_sum 平凡成立）。
            if total_fee_given or total_fee_declared > Decimal("0"):
                checked += 1
                diff = abs(item_sum - total_fee_declared)
                if diff > Decimal("0.0001"):
                    discrepancies.append({
                        "trade_id": trade_id,
                        "item_sum": str(item_sum),
                        "declared": str(total_fee_declared),
                        "diff": str(diff),
                    })

        if discrepancies:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"检出 {len(discrepancies)} 笔成交七科目费用求和与总费用不平！",
                metrics={"discrepancies_count": len(discrepancies), "samples": discrepancies[:3]},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # ⛔ Fail-Closed：所有成交均未提供可对账的 total_fee ⇒ 实际对账 0 笔 ⇒ INCONCLUSIVE（无证据 ≠ 通过）
        if checked == 0:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="无任何成交提供可对账的总费用（total_fee），实际对账 0 笔，无法判定费用平衡（无证据 ≠ 通过）",
                metrics={"checked_trades": 0, "total_trades": len(trades)},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"七科目费用逐笔分厘平衡检验通过 (共校验 {checked} 笔成交，差额严格为 0.00)",
            metrics={"checked_trades": checked, "total_trades": len(trades)},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class DailyCashConserveGate(BaseGate):
    """A-2: 每日资产现金流守恒检验（双账本核心等式）"""
    gate_id = "A-2"
    name = "每日资产现金流守恒检验"
    category = GateCategory.A_GATE
    severity = GateSeverity.BLOCKER
    evidence = "17 号报告 §4.4: 每日 Cash_T = Cash_{T-1} + 流入 - 流出，未解释差额严格恒等于 0.00 元"
    threshold_desc = "逐日对账未解释差额严格恒等于 0.00 元"

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - daily_cash_flows: list[dict]，包含 date, cash_start, cash_end, trade_in, trade_out, fee_out, dividend_in, dividend_tax_out
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无每日对账流水，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        flows = context.get("daily_cash_flows", []) if isinstance(context, dict) else getattr(context, "daily_cash_flows", [])
        if not flows:
            # ⛔ Fail-Closed：空账本 ⇒ INCONCLUSIVE（无证据 ≠ 守恒）
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="无每日对账流水（空账本），无法验证现金流守恒（无证据 ≠ 守恒）",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        leaks = []
        # ⛔ Fail-Closed：逐日对账要求每行提供锚点字段 cash_start / cash_end。
        # 缺字段的行会被默认值 0 抹平 ⇒ c_end(=0) == expected_end(=0) 平凡成立 ⇒ 假通过。
        anchor_keys = ("cash_start", "cash_end")
        uninformative = [
            i for i, row in enumerate(flows)
            if not isinstance(row, dict) or not all(k in row for k in anchor_keys)
        ]
        if uninformative:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message=(
                    f"检出 {len(uninformative)} 行对账流水缺少现金锚点字段 cash_start/cash_end"
                    f"（行号 {uninformative[:3]}），其等式被默认值 0 抹平、无法真实对账（无证据 ≠ 守恒）"
                ),
                metrics={"uninformative_rows": len(uninformative), "total_rows": len(flows)},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        for row in flows:
            dt = str(row.get("date", ""))
            c_start = Decimal(str(row.get("cash_start", 0)))
            c_end = Decimal(str(row.get("cash_end", 0)))
            t_in = Decimal(str(row.get("trade_in", 0)))
            t_out = Decimal(str(row.get("trade_out", 0)))
            fee_out = Decimal(str(row.get("fee_out", 0)))
            div_in = Decimal(str(row.get("dividend_in", 0)))
            div_tax = Decimal(str(row.get("dividend_tax_out", 0)))
            other_in = Decimal(str(row.get("other_in", 0)))
            other_out = Decimal(str(row.get("other_out", 0)))

            expected_end = c_start + t_in - t_out - fee_out + div_in - div_tax + other_in - other_out
            diff = abs(c_end - expected_end)
            if diff > Decimal("0.0001"):
                leaks.append({
                    "date": dt,
                    "expected_end": str(expected_end),
                    "actual_end": str(c_end),
                    "unexplained_diff": str(diff),
                })

        if leaks:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"检出 {len(leaks)} 天现金流不守恒，存在未解释账本漏损！",
                metrics={"leaks_count": len(leaks), "samples": leaks[:3]},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"每日资产现金流守恒检验通过 (共对账 {len(flows)} 个交易日，未解释差额 0.00)",
            metrics={"days_checked": len(flows)},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class GoldenRoundtripGate(BaseGate):
    """A-3: 10 万元往返黄金算例基准检验"""
    gate_id = "A-3"
    name = "10 万元往返黄金算例检验"
    category = GateCategory.A_GATE
    severity = GateSeverity.CRITICAL
    evidence = "17 号报告 §3.2: 散户 10 万元往返买卖同一只股票 (2023-08-28 之后)，总规费理论基准精确可核"
    threshold_desc = (
        "10 万元往返买卖总规费与基准理论值的绝对误差 <= 0.05 元；"
        "基准集 = {引擎权威逐项口径 112.82 元；行业「含规费全佣」报价口径 102.00 元}"
    )

    # 2023-08-28 之后基准（**按引擎真实费率逐项核算**，2026-09 依 backtest/fees.py 现率改正）：
    #   沪深经手费率 = 0.0000341（backtest/fees.py:234 现率；旧注释误用 0.0000487⇒4.87，已作废）。
    #   买入 10 万：佣金 25 + 经手 3.41 + 证管 2.00 + 过户 1.00 = 31.41
    #   卖出 10 万：印花税 50 + 佣金 25 + 经手 3.41 + 证管 2.00 + 过户 1.00 = 81.41
    #   往返合计：31.41 + 81.41 = 112.82 元 —— **引擎权威逐项口径**
    #             （tests/test_t203_fees.py::test_golden_round_trip_100k 断言 buy/sell/合计）。
    #   行业「含规费全佣」口径 = 102.00 元（规费并入佣金报价；与逐项口径差 10.82，已在 docs/t305 §归因）。
    #   ⛔ 旧注释的 114.20（经手误用 4.10 ⇒ 每边多 0.69、双边多 1.38）与 103.22（全仓无出处）
    #      **均已删除**——二者会误杀引擎自身黄金算例 112.82（GATE-R7）。
    #
    # ⛔ 判定口径（与 threshold_desc 声明逐字一致）：实测 10 万元往返总规费与**任一合法黄金基准**
    #    的绝对误差 <= 0.05 元 ⇒ PASS，否则 FAIL。
    #    * 严禁退化回旧版"物理区间 95~125 元"式宽区间判定——该区间会把 95.5 / 100.0 / 125.0
    #      全部假判 PASS（⑫ 声明↔实现背离）。
    #: 合法黄金基准（2023-08-28 之后 10 万元往返总规费）：
    #:   * 112.82 —— 引擎权威逐项口径（primary；佣金/经手/证管/过户/印花税分列，与 fees.py 现率一致）；
    #:   * 102.00 —— 行业「含规费全佣」报价口径（备选合法口径）。
    GOLDEN_FEE_BASIS: tuple[Decimal, ...] = (
        Decimal("112.82"),
        Decimal("102.00"),
    )

    #: 各黄金基准口径的**人读标签**（FAIL 报文须点明"最近基准 + 其口径"）。
    GOLDEN_FEE_BASIS_LABEL: dict[Decimal, str] = {
        Decimal("112.82"): "引擎权威逐项口径（佣金+经手+证管+过户+印花税分列）",
        Decimal("102.00"): "行业「含规费全佣」报价口径",
    }

    #: 实测值与最近基准的绝对误差容差（元）——与 threshold_desc 声明逐字一致。
    GOLDEN_FEE_ABS_TOLERANCE = Decimal("0.05")

    @staticmethod
    def _parse_finite_decimal(value: Any) -> Decimal | None:
        """把任意输入**安全**解析为有限 ``Decimal``；不可解析或非有限 ⇒ 返回 ``None``。

        ⛔ 兜底（QA §R5 登记）：字符串/布尔/列表/字典等非法类型、``NaN``/``±inf`` 一律返回
        ``None``，**绝不抛 ``decimal.InvalidOperation``**（由调用方据此判非 PASS）。
        """
        if isinstance(value, bool):        # bool 为 int 子类，但 str(True)=="True" 不可解析
            return None
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
        if not dec.is_finite():
            return None
        return dec

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - roundtrip_total_fee: Decimal | float | str（**必需**；缺失/非法 ⇒ 非 PASS，⛔ 不得自证）
        - expected_fee: Decimal | float | str（**可选**；一旦显式提供即作为唯一基准，覆盖内置基准）
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无黄金算例数据，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        rt_fee = context.get("roundtrip_total_fee") if isinstance(context, dict) else getattr(context, "roundtrip_total_fee", None)
        if rt_fee is None:
            # ⛔ Fail-Closed：缺外部基准实测值 ⇒ INCONCLUSIVE（不得由被检引擎自算自证）
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="缺少独立黄金算例实测费用（roundtrip_total_fee），无法判定（无外部基准 ≠ 通过）",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # ⛔ 兜底：实测值非法类型 / 非有限值（NaN/±inf）⇒ 被检值损坏、无法与基准求差 ⇒ FAIL。
        #   （`roundtrip_total_fee` 是**被检对象**——引擎产出，其损坏即缺陷，方向取 FAIL。）
        val = self._parse_finite_decimal(rt_fee)
        if val is None:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"10 万元往返实测费用非法（{rt_fee!r}，非有限数值），无法与黄金基准比对！",
                metrics={"actual_fee": str(rt_fee), "parseable_finite": False},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # 显式基准覆盖：`expected_fee` 一旦显式提供，即作为**唯一**基准（不再并列内置基准）。
        expected_override = (
            context.get("expected_fee", None) if isinstance(context, dict)
            else getattr(context, "expected_fee", None)
        )
        if expected_override is not None:
            # ⛔ 兜底：显式基准非法/非有限 ⇒ 外部参照无效 ⇒ INCONCLUSIVE（与"缺外部基准"同口径）。
            #   （`expected_fee` 是**外部参照**——参照无效 = 无有效基准，方向取 INCONCLUSIVE。）
            override_basis = self._parse_finite_decimal(expected_override)
            if override_basis is None:
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.INCONCLUSIVE,
                    severity=self.severity,
                    message=(
                        f"显式基准 expected_fee 非法（{expected_override!r}，非有限数值），"
                        "无有效外部基准可判定（无效基准 ≠ 通过）"
                    ),
                    metrics={"expected_fee_raw": str(expected_override), "parseable_finite": False},
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )
            bases: tuple[Decimal, ...] = (override_basis,)
            labels: dict[Decimal, str] = {override_basis: "调用方显式指定基准（expected_fee）"}
            override_used = True
        else:
            bases = self.GOLDEN_FEE_BASIS
            labels = self.GOLDEN_FEE_BASIS_LABEL
            override_used = False

        # 与**最近**基准求绝对误差（多合法口径取最接近者，避免误杀合法核算口径）。
        nearest = min(bases, key=lambda g: abs(val - g))
        abs_diff = abs(val - nearest)
        basis_label = labels.get(nearest, "自定义基准")
        tol = self.GOLDEN_FEE_ABS_TOLERANCE
        metrics = {
            "actual_fee": str(val),
            "nearest_basis": str(nearest),
            "basis_label": basis_label,
            "abs_diff": str(abs_diff),
            "tolerance": str(tol),
            "expected_override_used": override_used,
        }

        if abs_diff <= tol:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.PASS,
                severity=self.severity,
                message=(
                    f"10 万元往返黄金算例检验通过（实际费用 {val} 元，"
                    f"最近基准 {nearest} 元〔{basis_label}〕，绝对误差 {abs_diff} 元 ≤ {tol} 元）"
                ),
                metrics=metrics,
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.FAIL,
            severity=self.severity,
            message=(
                f"10 万元往返费用为 {val} 元，与最近基准 {nearest} 元〔{basis_label}〕"
                f"绝对误差 {abs_diff} 元，超出容许容差 {tol} 元（黄金算例基准不吻合）！"
            ),
            metrics=metrics,
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class SegmentRateScheduleGate(BaseGate):
    """A-4: 历史分段费率时序穿透检验（严禁穿越少扣税）"""
    gate_id = "A-4"
    name = "历史分段费率时序穿透检验"
    category = GateCategory.A_GATE
    severity = GateSeverity.BLOCKER
    evidence = "17 号报告 §4.4: 2023-08-28 前卖出严格执行 1‰ 印花税，严禁穿越历史使用 0.5‰ 导致少扣税"
    threshold_desc = "2023-08-28 之前卖出印花税率必须为 1‰，严禁历史穿越"

    CUTOFF_STAMP = datetime.date(2023, 8, 28)
    CUTOFF_TRANSFER = datetime.date(2022, 4, 29)

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - trades: list[Trade] 或 list[dict]，包含 date, side, amount, fees
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
            # ⛔ Fail-Closed：无成交证据 ⇒ INCONCLUSIVE（无证据 ≠ 通过）
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="无成交数据，无法穿透历史分段费率时序（无证据 ≠ 通过）",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        violations = []
        sell_seen = 0          # 卖出成交总数
        sell_eligible = 0      # 可穿透（有可解析日期且金额 > 0）的卖出成交数
        for t in trades:
            side = str(t.get("side", "") if isinstance(t, dict) else getattr(t, "side", "")).upper()
            dt_raw = t.get("date") if isinstance(t, dict) else getattr(t, "date", None)
            if isinstance(dt_raw, str):
                dt = datetime.date.fromisoformat(dt_raw)
            elif isinstance(dt_raw, datetime.date):
                dt = dt_raw
            else:
                if "SELL" in side:
                    sell_seen += 1
                continue

            price = Decimal(str(t.get("price", 0) if isinstance(t, dict) else getattr(t, "price", 0)))
            vol = Decimal(str(t.get("volume", 0) if isinstance(t, dict) else getattr(t, "volume", 0)))
            amount = price * vol if price and vol else Decimal(str(t.get("amount", 0) if isinstance(t, dict) else getattr(t, "amount", 0)))

            fees = t.get("fees", {}) if isinstance(t, dict) else getattr(t, "fees", {})
            stamp_tax = Decimal("0")
            for k, v in fees.items():
                k_name = str(k.value if hasattr(k, "value") else k)
                if "STAMP" in k_name:
                    stamp_tax = Decimal(str(v))
                    break

            # 仅卖出有印花税
            if "SELL" in side:
                sell_seen += 1
                if amount > Decimal("0"):
                    sell_eligible += 1
                    effective_rate = stamp_tax / amount
                    if dt < self.CUTOFF_STAMP:
                        # 2023-08-28 之前应为 1‰ (0.001)
                        if effective_rate < Decimal("0.0008"):
                            violations.append({
                                "date": dt.isoformat(),
                                "side": side,
                                "amount": str(amount),
                                "stamp_tax": str(stamp_tax),
                                "effective_rate": f"{effective_rate*1000:.2f}‰",
                                "expected": "1.00‰",
                                "reason": "2023-08-28 之前卖出印花税少于 1‰，发生穿越历史少扣税！",
                            })

        if violations:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"检出 {len(violations)} 笔历史分段印花税违规（在 2023-08-28 之前少扣税穿越）",
                metrics={"violations_count": len(violations), "samples": violations[:3]},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # ⛔ Fail-Closed：无卖出成交 ⇒ 该维（卖出印花税时序）不适用 ⇒ SKIP（不得记 PASS）。
        if sell_seen == 0:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无卖出成交，历史分段印花税时序检验不适用（有证据表明该门禁不适用）",
                metrics={"sell_trades": 0},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )
        # ⛔ 有卖出成交但均缺可解析日期/正金额 ⇒ 无可穿透样本 ⇒ INCONCLUSIVE（证据不足 ≠ 通过）。
        if sell_eligible == 0:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="存在卖出成交但均缺少可解析日期或正金额，无法穿透历史分段费率时序（证据不足 ≠ 通过）",
                metrics={"sell_trades": sell_seen, "sell_trades_eligible": 0},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message="历史分段费率时序穿透检验通过 (历史印花税率无穿越)",
            metrics={"trades_checked": len(trades), "sell_trades_evaluated": sell_eligible},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )
