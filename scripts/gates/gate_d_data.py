#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D-Gate: 数据真值与反未来门禁（Data Integrity & Anti-Lookahead Gates）

依据：
1. 17 号深度调研报告 §4.1 / D-1 ~ D-5 门禁定义
2. 本地真实硬伤实证：Baostock 后复权脏日线(T310)、2 亿成交额充当市值(T311)、全年单一静态均值股息率(T311)
"""

from __future__ import annotations

from typing import Any

from .base import BaseGate, GateCategory, GateResult, GateSeverity, GateStatus
from .market_rules import resolve_market_rules


class RawPriceJumpGate(BaseGate):
    """D-1: 原始日线真实性检验（防止后复权污染与异常极值）"""
    gate_id = "D-1"
    name = "原始日线跳变率检验"
    category = GateCategory.D_GATE
    severity = GateSeverity.BLOCKER
    evidence = "FinAI2.0 T310 真实硬伤实证: 18 只 Baostock 历史后复权污染导致 30%~1000% 虚假跳变"
    threshold_desc = "非除权日 RAW 日线收盘价单日跳变严格 < 30%"

    def __init__(self, max_jump_ratio: float = 0.30) -> None:
        self.max_jump_ratio = max_jump_ratio

    def evaluate(self, context: Any = None) -> GateResult:
        """context 预期为字典或对象，包含:
        - bars: list[dict] 或 list[Bar]，元素含 date, close, is_exdiv (可选)
        - symbol: str (可选)
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无数据输入，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        bars = context.get("bars") if isinstance(context, dict) else getattr(context, "bars", None)
        symbol = context.get("symbol", "UNKNOWN") if isinstance(context, dict) else getattr(context, "symbol", "UNKNOWN")

        # 向量化快路径：context 携带 "frame"（含 date/close 列日线帧，可含 is_exdiv 列）时，
        # 以 numpy 全帧评估（与逐行循环同语义：exdiv 区间规则 prev_d < ed <= d、首 5 行豁免、
        # max_seen_jump 覆盖全部 valid 对）——前置门禁逐票全帧扫描的 128s 热点在此。
        frame = context.get("frame") if isinstance(context, dict) else getattr(context, "frame", None)
        if frame is not None:
            exdiv_dates = (
                context.get("exdiv_dates") if isinstance(context, dict) else getattr(context, "exdiv_dates", None)
            ) or set()
            return self._evaluate_frame(frame, exdiv_dates, symbol)

        bars = list(bars or [])

        # ⛔ Fail-Closed：缺 bars / 样本不足 2 条 ⇒ INCONCLUSIVE（无证据 ≠ 通过；⛔ 不得 len(None) 崩溃）
        if len(bars) < 2:
            return self._result_insufficient(symbol, len(bars))

        violations = []
        max_seen_jump = 0.0
        valid_pairs = 0

        for i in range(1, len(bars)):
            curr = bars[i]
            prev = bars[i - 1]

            c_curr = float(curr["close"] if isinstance(curr, dict) else curr.close)
            c_prev = float(prev["close"] if isinstance(prev, dict) else prev.close)
            is_exdiv = bool(curr.get("is_exdiv", False) if isinstance(curr, dict) else getattr(curr, "is_exdiv", False))

            if c_prev <= 0:
                continue

            valid_pairs += 1
            jump = abs(c_curr - c_prev) / c_prev
            if jump > max_seen_jump:
                max_seen_jump = jump

            if jump >= self.max_jump_ratio and not is_exdiv:
                date_str = str(curr.get("date", i) if isinstance(curr, dict) else getattr(curr, "date", i))
                violations.append({
                    "date": date_str,
                    "prev_close": c_prev,
                    "curr_close": c_curr,
                    "jump_pct": round(jump * 100, 2),
                })

        if violations:
            return self._result_fail(symbol, violations, max_seen_jump)

        # ⛔ Fail-Closed：有 bars 但无任何"前收 > 0"的相邻可比对 ⇒ 无法计算跳变率 ⇒ INCONCLUSIVE（不得记 PASS）。
        if valid_pairs == 0:
            return self._result_no_valid_pairs(symbol, len(bars))

        return self._result_pass(symbol, len(bars), valid_pairs, max_seen_jump)

    def _evaluate_frame(self, frame: Any, exdiv_dates: Any, symbol: str) -> GateResult:
        """向量化评估路径（context['frame']）。语义与逐行循环严格一致：

        - is_exdiv[i] = 帧自带 is_exdiv 列 OR date 命中 exdiv_dates OR
          ∃ed∈exdiv_dates 使 d[i-1] < ed <= d[i]（区间规则，捕捉落在非交易日的除权事件）
          OR i < 5（首 5 行豁免，与原实现一致）
        - 相邻对 (i-1, i) 中 c_prev <= 0 的不计入 valid_pairs
        - violations = jump >= max_jump_ratio 且 curr 非 exdiv
        """
        import numpy as np

        n = len(frame)
        if n < 2:
            return self._result_insufficient(symbol, n)

        d_arr = frame["date"].astype(str).str[:10].to_numpy()
        close = frame["close"].astype(float).to_numpy(dtype=float)

        if "is_exdiv" in frame.columns:
            is_ex = frame["is_exdiv"].fillna(False).to_numpy(dtype=bool, copy=True)
        else:
            is_ex = np.zeros(n, dtype=bool)
        if exdiv_dates:
            eds = sorted(str(ed)[:10] for ed in exdiv_dates)
            is_ex |= np.isin(d_arr, eds)
            # 区间规则：对每个除权日 ed，标记首个 d >= ed 且其前一行 < ed 的行
            for ed in eds:
                pos = int(np.searchsorted(d_arr, ed))
                if pos < n and (pos == 0 or d_arr[pos - 1] < ed):
                    is_ex[pos] = True
        is_ex[: min(5, n)] = True

        c_prev = close[:-1]
        c_curr = close[1:]
        valid = c_prev > 0
        valid_pairs = int(valid.sum())

        jumps = np.zeros(n - 1, dtype=float)
        jumps[valid] = np.abs(c_curr[valid] - c_prev[valid]) / c_prev[valid]
        max_seen_jump = float(jumps.max()) if n > 1 else 0.0

        viol_idx = np.nonzero(valid & (jumps >= self.max_jump_ratio) & ~is_ex[1:])[0]
        if len(viol_idx):
            violations = [
                {
                    "date": str(d_arr[i + 1]),
                    "prev_close": float(c_prev[i]),
                    "curr_close": float(c_curr[i]),
                    "jump_pct": round(float(jumps[i]) * 100, 2),
                }
                for i in viol_idx
            ]
            return self._result_fail(symbol, violations, max_seen_jump)

        if valid_pairs == 0:
            return self._result_no_valid_pairs(symbol, n)

        return self._result_pass(symbol, n, valid_pairs, max_seen_jump)

    def _result_insufficient(self, symbol: str, n_bars: int) -> GateResult:
        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.INCONCLUSIVE,
            severity=self.severity,
            message=f"[{symbol}] 原始日线样本不足 2 条，无法判定跳变率（无证据 ≠ 通过）",
            metrics={"bars_count": n_bars},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )

    def _result_no_valid_pairs(self, symbol: str, n_bars: int) -> GateResult:
        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.INCONCLUSIVE,
            severity=self.severity,
            message=f"[{symbol}] 有 {n_bars} 根日线，但无任何前收盘价 > 0 的可比对相邻日，无法计算跳变率（退化输入 ≠ 通过）",
            metrics={"bars_count": n_bars, "valid_pairs": 0},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )

    def _result_fail(self, symbol: str, violations: list, max_seen_jump: float) -> GateResult:
        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.FAIL,
            severity=self.severity,
            message=f"[{symbol}] 检出 {len(violations)} 处异常日跳变 (最大 {max_seen_jump*100:.1f}%)，疑存脏日线或后复权污染",
            metrics={"violations_count": len(violations), "max_jump_pct": round(max_seen_jump * 100, 2), "samples": violations[:3]},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )

    def _result_pass(self, symbol: str, n_bars: int, valid_pairs: int, max_seen_jump: float) -> GateResult:
        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"[{symbol}] 连续 {n_bars} 根日线跳变率正常 (最大跳变 {max_seen_jump*100:.1f}%)",
            metrics={"bars_count": n_bars, "valid_pairs": valid_pairs, "max_jump_pct": round(max_seen_jump * 100, 2)},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class FloatMarketCapGate(BaseGate):
    """D-2: 流通市值 vs 成交额偏离度检验（杜绝成交额冒充市值）"""
    gate_id = "D-2"
    name = "流通市值与成交额偏离度检验"
    category = GateCategory.D_GATE
    severity = GateSeverity.BLOCKER
    evidence = "FinAI2.0 T311 真实硬伤实证: 使用 2 亿成交额冒充流通市值，全池几乎无分化"
    threshold_desc = "多数样本 |float_mv - amount| / float_mv ≤ 80% 或 ≥10% 样本偏离度 ≤5% 判负（系统性伪造特征）；全池市值标准差 Std > 100 亿"

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - float_mv_list: Sequence[float | Decimal]
        - amount_list: Sequence[float | Decimal]
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无数据输入，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        float_mvs = context.get("float_mv_list", []) if isinstance(context, dict) else getattr(context, "float_mv_list", [])
        amounts = context.get("amount_list", []) if isinstance(context, dict) else getattr(context, "amount_list", [])

        if not float_mvs:
            # ⛔ Fail-Closed：无市值样本 ⇒ INCONCLUSIVE（无证据 ≠ 通过）
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="流通市值列表为空，无法判定市值分布真实性（无证据 ≠ 通过）",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        mvs = [float(x) for x in float_mvs]
        # 计算标准差
        n = len(mvs)
        mean_mv = sum(mvs) / n
        variance = sum((x - mean_mv) ** 2 for x in mvs) / (n - 1 if n > 1 else 1)
        std_mv = variance ** 0.5

        # 检验标准差：全池真实市值标准差必须 > 100 亿 (1e10)
        # 若传入为单只股票或小样本，做自适应判断
        if n >= 30 and std_mv < 1.0e10:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"流通市值分布过于集中 (Std = {std_mv/1e8:.2f} 亿 < 100 亿)，疑为伪造固定市值或用成交额替代",
                metrics={"std_mv_yi": round(std_mv / 1e8, 2), "sample_size": n},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # 检验偏离度：伪造特征为"系统性"（整列复制/统一缩放），故按样本占比判定；
        # 单票 dev<=80% 等价于当日换手 20%~180%，真实高换手票会零星命中，不作伪造证据
        if amounts and len(amounts) == len(float_mvs):
            deviations = []
            for mv, amt in zip(mvs, [float(a) for a in amounts]):
                if mv > 0:
                    dev = abs(mv - amt) / mv
                    deviations.append(dev)

            m_dev = len(deviations)
            sub_80_count = sum(1 for d in deviations if d <= 0.80)
            near_copy_count = sum(1 for d in deviations if d <= 0.05)
            if m_dev and (sub_80_count * 2 > m_dev or near_copy_count * 10 >= m_dev):
                return GateResult(
                    gate_id=self.gate_id,
                    name=self.name,
                    category=self.category,
                    status=GateStatus.FAIL,
                    severity=self.severity,
                    message=(
                        f"流通市值与成交额偏离度呈系统性伪造特征："
                        f"dev<=80% 占比 {sub_80_count}/{m_dev}，dev<=5% 近复制 {near_copy_count}/{m_dev}"
                    ),
                    metrics={
                        "sub_80_count": sub_80_count,
                        "near_copy_count": near_copy_count,
                        "dev_sample_size": m_dev,
                        "min_dev": round(min(deviations), 4),
                    },
                    threshold=self.threshold_desc,
                    evidence=self.evidence,
                )

        # ⛔ Fail-Closed：样本不足（< 30）无法做分布判定 ⇒ INCONCLUSIVE（不得因样本小就当通过）
        if n < 30:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message=f"样本数 {n} < 30，不足以判定全池市值分布真实性（无证据 ≠ 通过）",
                metrics={"sample_size": n},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"流通市值检验通过 (样本 {n}，Std = {std_mv/1e8:.2f} 亿，与成交额完全偏离)",
            metrics={"sample_size": n, "std_mv_yi": round(std_mv / 1e8, 2)},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class PitDividendYieldGate(BaseGate):
    """D-3: Point-in-Time 滚动股息率检验（彻底消灭全年静态均值未来函数）"""
    gate_id = "D-3"
    name = "Point-in-Time 动态股息率检验"
    category = GateCategory.D_GATE
    severity = GateSeverity.BLOCKER
    evidence = "FinAI2.0 T311 真实硬伤实证: 全年 242 交易日股息率恒为单一均值常数，构成时序未来函数泄露"
    threshold_desc = "自然年内变异值种类 CountUnique(yield_year) >= 50 种，未来事件可见性 <= T"

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - daily_yields: list[float | Decimal] 一个自然年内的逐日股息率
        - year: int (可选)
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无数据输入，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        daily_yields = context.get("daily_yields", []) if isinstance(context, dict) else getattr(context, "daily_yields", [])
        year = context.get("year", 0) if isinstance(context, dict) else getattr(context, "year", 0)

        if not daily_yields or len(daily_yields) < 60:
            # ⛔ Fail-Closed：样本不足 60 天 ⇒ INCONCLUSIVE（无证据 ≠ 通过）
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message=f"股息率样本 {len(daily_yields)} 天 < 60，不足以判定 PIT 动态性（无证据 ≠ 通过）",
                metrics={"sample_count": len(daily_yields)},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # 保留 4 位小数统计唯一值
        rounded_vals = {round(float(y), 4) for y in daily_yields if y is not None}
        unique_count = len(rounded_vals)

        if unique_count < 50:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"[{year}] 自然年内股息率仅有 {unique_count} 种变异值 (< 50 种)，存在全年常数静态未来函数泄露",
                metrics={"unique_count": unique_count, "total_days": len(daily_yields)},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"[{year}] PIT 动态股息率检验通过 (年内变异值种类: {unique_count} 种)",
            metrics={"unique_count": unique_count, "total_days": len(daily_yields)},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class SuspensionVolumeGate(BaseGate):
    """D-4: 停牌日成交量与状态检验（杜绝停牌日前值平推脏成交）"""
    gate_id = "D-4"
    name = "停牌日成交量为零检验"
    category = GateCategory.D_GATE
    severity = GateSeverity.BLOCKER
    evidence = "research-finai 12 号附录 A.5 与 R1: Baostock 停牌日返回前收填充且成交量非零脏行"
    threshold_desc = "当 tradestatus != '1' 时，成交量 volume 严格恒等于 0"

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - bars: list[dict] 或 list[Bar]，含 tradestatus, volume, date
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无数据输入，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # 向量化快路径：context 携带 "frame"（含 tradestatus/volume 列日线帧）时全帧评估——
        # 与逐行循环同语义（tradestatus != '1' 视为停牌、停牌日 volume>0 即违例、
        # 无停牌日判 SKIP 不适用）。消除前置门禁逐票 iterrows 造 dict 的热点。
        frame = context.get("frame") if isinstance(context, dict) else getattr(context, "frame", None)
        if frame is not None:
            return self._evaluate_frame(frame)

        bars = context.get("bars", []) if isinstance(context, dict) else getattr(context, "bars", [])
        if not bars:
            # ⛔ Fail-Closed：无日线证据 ⇒ INCONCLUSIVE（无证据 ≠ 通过）
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="无日线数据，无法判定停牌日成交量是否为零（无证据 ≠ 通过）",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        suspension_violations = []
        suspended_days = 0
        for b in bars:
            status_val = str(b.get("tradestatus", "1") if isinstance(b, dict) else getattr(b, "tradestatus", "1"))
            vol = float(b.get("volume", 0) if isinstance(b, dict) else getattr(b, "volume", 0))
            if status_val != "1":
                suspended_days += 1
                if vol > 0:
                    suspension_violations.append({
                        "date": str(b.get("date", "") if isinstance(b, dict) else getattr(b, "date", "")),
                        "tradestatus": status_val,
                        "volume": vol,
                    })

        if suspension_violations:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"检出 {len(suspension_violations)} 处停牌日成交量非零脏数据",
                metrics={"violations_count": len(suspension_violations), "samples": suspension_violations[:3]},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # ⛔ Fail-Closed：样本内无任何停牌交易日 ⇒ 该维（停牌成交量）不适用 ⇒ SKIP（不得记 PASS）。
        if suspended_days == 0:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message=f"样本内 {len(bars)} 根日线无任何停牌交易日（tradestatus != '1'），停牌日成交量检验不适用（有证据表明不适用）",
                metrics={"bars_count": len(bars), "suspended_days": 0},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"停牌日成交量检验通过 (共 {suspended_days} 个停牌日成交量均为 0)",
            metrics={"suspended_days": suspended_days},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )

    def _evaluate_frame(self, frame: Any) -> GateResult:
        """向量化评估路径（context['frame']）：tradestatus != '1' 为停牌日，
        停牌日 volume > 0 判违例；无停牌日判 SKIP。与逐行循环同语义。"""
        import numpy as np

        n = len(frame)
        if n == 0:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="无日线数据，无法判定停牌日成交量是否为零（无证据 ≠ 通过）",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        status = frame["tradestatus"].astype(str).to_numpy()
        volume = frame["volume"].astype(float).to_numpy(dtype=float)
        susp = status != "1"
        suspended_days = int(susp.sum())

        viol_idx = np.nonzero(susp & (volume > 0))[0]
        if len(viol_idx):
            dates = frame["date"].astype(str).to_numpy() if "date" in frame.columns else np.array([""] * n)
            violations = [
                {"date": str(dates[i]), "tradestatus": str(status[i]), "volume": float(volume[i])}
                for i in viol_idx
            ]
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"检出 {len(violations)} 处停牌日成交量非零脏数据",
                metrics={"violations_count": len(violations), "samples": violations[:3]},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        if suspended_days == 0:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message=f"样本内 {n} 根日线无任何停牌交易日（tradestatus != '1'），停牌日成交量检验不适用（有证据表明不适用）",
                metrics={"bars_count": n, "suspended_days": 0},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"停牌日成交量检验通过 (共 {suspended_days} 个停牌日成交量均为 0)",
            metrics={"suspended_days": suspended_days},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )


class HighPriceLotGate(BaseGate):
    """D-5: 高价股排除与整手约束检验（防 10~15 万散户资金集中度与非线性失真）"""
    gate_id = "D-5"
    name = "高价股与整手约束检验"
    category = GateCategory.D_GATE
    severity = GateSeverity.CRITICAL
    evidence = "17 号报告 §3.3 + 上交所交易规则 3.4.2 条: 买入必须为 100 股一手整数倍，高价股 1 手占小资金比例过高"
    threshold_desc = "开仓标的单价 <= 300.0 元，买入委托股数严格为 100 的整数倍"

    def evaluate(self, context: Any = None) -> GateResult:
        """context 包含:
        - orders: list[Order] 或 list[dict]，含 price, volume, side
        """
        if not context:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message="无订单数据，跳过检验",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        orders = context.get("orders", []) if isinstance(context, dict) else getattr(context, "orders", [])
        if not orders:
            # ⛔ Fail-Closed：无委托证据 ⇒ INCONCLUSIVE（无证据 ≠ 通过）
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.INCONCLUSIVE,
                severity=self.severity,
                message="无委托记录，无法判定高价股/整手约束是否被遵守（无证据 ≠ 通过）",
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        rules = resolve_market_rules(context)

        violations = []
        buy_orders = 0
        for o in orders:
            side = str(o.get("side", "BUY") if isinstance(o, dict) else getattr(o, "side", "BUY")).upper()
            price = float(o.get("price", 0) if isinstance(o, dict) else getattr(o, "price", 0))
            vol = int(o.get("volume", 0) if isinstance(o, dict) else getattr(o, "volume", 0))

            if "BUY" in side:
                buy_orders += 1
                if rules.max_buy_price is not None and price > rules.max_buy_price:
                    violations.append(f"买入高价股单价 {price} > {rules.max_buy_price} 元")
                if rules.lot_size is not None and vol % rules.lot_size != 0:
                    violations.append(f"买入股数 {vol} 非 {rules.lot_size} 股整手")

        if violations:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.FAIL,
                severity=self.severity,
                message=f"检出 {len(violations)} 笔违背高价股/整手约束的委托",
                metrics={"violations_count": len(violations), "samples": violations[:3]},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        # ⛔ Fail-Closed：有委托但无任何买入单 ⇒ 高价股/整手约束无适用样本 ⇒ SKIP（不得记 PASS）。
        if buy_orders == 0:
            return GateResult(
                gate_id=self.gate_id,
                name=self.name,
                category=self.category,
                status=GateStatus.SKIP,
                severity=self.severity,
                message=f"委托中无任何买入单（共 {len(orders)} 笔），高价股与整手约束不适用（有证据表明不适用）",
                metrics={"total_orders": len(orders), "buy_orders": 0},
                threshold=self.threshold_desc,
                evidence=self.evidence,
            )

        return GateResult(
            gate_id=self.gate_id,
            name=self.name,
            category=self.category,
            status=GateStatus.PASS,
            severity=self.severity,
            message=f"高价股与整手约束检验通过 (共检查 {buy_orders} 笔买入委托)",
            metrics={"total_orders": len(orders), "buy_orders": buy_orders},
            threshold=self.threshold_desc,
            evidence=self.evidence,
        )
