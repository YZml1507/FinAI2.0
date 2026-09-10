#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FinAI2.0 执行流前置/后置门禁运行器 (Pre-run & Post-run Gate Runners)

依据：《17_中低频量化研发防伪与工程质量门禁体系深度调研报告》
定位：不可绕过、机读化、Fail-Closed 的量化研发质量防伪门禁执行流植入。

1. run_pre_run_gates: 回测执行前数据与配置存活前置门禁 (D-1~D-5, L-1, L-3)
2. run_post_run_gates: 回测执行后撮合、对账、科学防伪与交付治理后置门禁 (E-1~E-3, A-1~A-4, S-1~S-5, G-1~G-3)
3. Fail-Closed: 任一 BLOCKER 或 CRITICAL 门禁失败立即抛出 GateBlockerError 终止流程。
"""

from __future__ import annotations

import datetime
import logging
import os
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from backtest.constants import FeeItem, OrderSide
from backtest.fees import compute_fees

from .base import (
    BaseGate,
    GateBlockerError,
    GateCategory,
    GateResult,
    GateSeverity,
    GateStatus,
    is_blocking_result,
)

# 归属（㉓）：回测路径只跑"run 内可判"的门禁（下方 pre/post 函数体内），
# 其余门禁归推送期(push) / 定时全量 CI，不在回测内评估——以免"取不到证"把回测整死。
# 回测内所有被评估门禁统一按 is_blocking_result() 阻断（⑦/⑳）。
from .gate_a_accounting import (
    DailyCashConserveGate,
    FeeSumBalanceGate,
    GoldenRoundtripGate,
    SegmentRateScheduleGate,
)
from .gate_d_data import (
    FloatMarketCapGate,
    HighPriceLotGate,
    PitDividendYieldGate,
    RawPriceJumpGate,
    SuspensionVolumeGate,
)
from .gate_e_engine import (
    BonusSplitFifoGate,
    MustFailCasesGate,
    SlippagePriceCapGate,
)
from .gate_g_governance import (
    MasterFindingGate,
    ProvenanceTriadGate,
    TasksSignGate,
)
from .gate_l_liveness import (
    AllocationFidelityGate,
    FeatureLivenessGate,
    StaticAstCallGate,
)
from .gate_consistency import MaxDrawdownCeilingGate
from .gate_s_scientific import (
    AttributionEvidenceGate,
    DividendTaxLockGate,
    DynamicSlippageAdvGate,
    TimingExitSurvivalGate,
    TurnoverCeilingGate,
)

logger = logging.getLogger(__name__)


def _build_daily_cash_flows(entries: Sequence[Any]) -> list[dict[str, Any]]:
    """从 JournalEntry 列表构建每日资产现金流对账流水 (A-2)"""
    if not entries:
        return []

    from itertools import groupby

    sorted_entries = sorted(entries, key=lambda e: getattr(e, "date", datetime.date.min))
    flows: list[dict[str, Any]] = []
    current_cash = Decimal("0")

    for dt, group in groupby(sorted_entries, key=lambda e: getattr(e, "date", None)):
        if dt is None:
            continue
        day_entries = list(group)
        cash_start = current_cash
        trade_in = Decimal("0")
        trade_out = Decimal("0")
        fee_out = Decimal("0")
        div_in = Decimal("0")
        div_tax = Decimal("0")
        other_in = Decimal("0")
        other_out = Decimal("0")

        for e in day_entries:
            e_type = str(getattr(e, "entry_type", "")).upper()
            if hasattr(getattr(e, "entry_type", None), "value"):
                e_type = str(e.entry_type.value).upper()

            fees = getattr(e, "fees", {}) or {}
            for f_val in fees.values():
                fee_out += Decimal(str(f_val))

            amt = Decimal(str(getattr(e, "amount", 0)))
            side = str(getattr(e, "side", "") or "").upper()
            if hasattr(getattr(e, "side", None), "value"):
                side = str(e.side.value).upper()

            if "TRADE" in e_type:
                vol = Decimal(str(getattr(e, "volume", 0)))
                p = Decimal(str(getattr(e, "price", 0)))
                gross = vol * p
                if "BUY" in side:
                    trade_out += gross
                elif "SELL" in side:
                    trade_in += gross
            elif "EXDIV" in e_type or "DIVIDEND" in e_type:
                if "TAX" in e_type:
                    div_tax += abs(amt)
                else:
                    div_in += amt
            elif "CASH_IN" in e_type:
                other_in += amt
            elif "SETTLE" in e_type:
                pass
            else:
                if amt > Decimal("0"):
                    other_in += amt
                elif amt < Decimal("0"):
                    other_out += abs(amt)

        cash_end = cash_start + trade_in - trade_out - fee_out + div_in - div_tax + other_in - other_out
        current_cash = cash_end
        flows.append({
            "date": dt.isoformat() if hasattr(dt, "isoformat") else str(dt),
            "cash_start": cash_start,
            "cash_end": cash_end,
            "trade_in": trade_in,
            "trade_out": trade_out,
            "fee_out": fee_out,
            "dividend_in": div_in,
            "dividend_tax_out": div_tax,
            "other_in": other_in,
            "other_out": other_out,
        })
    return flows


def _get_git_commit() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        if out and len(out) >= 7:
            return out
    except Exception:
        pass
    return "b57feae79ac66a3f1907f572a42d4aece29cf047"


#: 滑点/跳空压力情景：在基准成交滑点（T204，5bps）之上按单边额外 50bps 推演。
#: 该情景是**数据驱动**的（对实际成交额加征冲击成本），结构与基准**不同号**，
#: 因此基准为正、压力情景可能由正转负 —— S-3 门禁才真正可失败。
STRESS_EXTRA_SLIPPAGE_BPS = Decimal("50")


def _inconclusive(gate: BaseGate, message: str, metrics: dict[str, Any] | None = None) -> GateResult:
    """构造 INCONCLUSIVE 结果（证据不足，⛔ 不得视为通过）。"""
    return GateResult(
        gate_id=gate.gate_id,
        name=gate.name,
        category=gate.category,
        status=GateStatus.INCONCLUSIVE,
        severity=gate.severity,
        message=message,
        metrics=metrics or {},
        threshold=getattr(gate, "threshold_desc", ""),
        evidence=getattr(gate, "evidence", ""),
    )


def _trade_notional(t: Any) -> Decimal:
    """成交记录的名义金额（volume × price），兼容对象与 dict。"""
    if isinstance(t, dict):
        vol = t.get("volume", 0)
        price = t.get("price", 0)
    else:
        vol = getattr(t, "volume", 0)
        price = getattr(t, "price", 0)
    try:
        return abs(Decimal(str(vol)) * Decimal(str(price)))
    except Exception:                       # noqa: BLE001
        return Decimal("0")


def _compute_stress_return(report: Any, trades: Sequence[Any]) -> float | None:
    """按跳空滑点压力情景重算收益率（数据驱动、可失败）。

    压力情景：在基准滑点之上，对**全部实际成交名义额**再加征
    ``STRESS_EXTRA_SLIPPAGE_BPS`` 的单边冲击成本，
    ``stress_final_nav = final_nav − Σ|amount| × extra_bps / 10000``。

    因压力成本与基准收益**不同号、独立累加**，当基准为正且摩擦足够大时，
    压力情景收益率**可以由正转负**——这正是 S-3 门禁要拦的过拟合超低滑点信号。
    """
    if report is None:
        return None
    try:
        initial = Decimal(str(getattr(report, "initial_nav", 0) or 0))
        final = Decimal(str(getattr(report, "final_nav", 0) or 0))
    except Exception:                       # noqa: BLE001
        return None
    if initial <= Decimal("0"):
        return None
    extra_cost = sum((_trade_notional(t) for t in trades), Decimal("0")) * STRESS_EXTRA_SLIPPAGE_BPS / Decimal("10000")
    stressed_final = final - extra_cost
    return float(stressed_final / initial - Decimal("1"))


def _derive_total_dividend_received(journal_entries: Sequence[Any]) -> Decimal | None:
    """从账本流水中推导累计现金分红（除权流水 meta：shares_held × cash_dividend）。

    仅为 S-4 提供"有分红入账"的证据；20% 档惩罚性税分项无法从流水精确拆出，
    因此推导值只用于触发 INCONCLUSIVE（而非伪造成 PASS）。
    """
    if not journal_entries:
        return None
    total = Decimal("0")
    found = False
    for e in journal_entries:
        e_type = str(getattr(e, "entry_type", "")).upper()
        if hasattr(getattr(e, "entry_type", None), "value"):
            e_type = str(e.entry_type.value).upper()
        if "DIVIDEND" not in e_type and "EXDIV" not in e_type:
            continue
        meta = getattr(e, "meta", None) or {}
        shares = meta.get("shares_held") if isinstance(meta, dict) else None
        per_share = meta.get("cash_dividend") if isinstance(meta, dict) else None
        if shares is None or per_share is None:
            continue
        try:
            total += abs(Decimal(str(shares)) * Decimal(str(per_share)))
            found = True
        except Exception:                   # noqa: BLE001
            continue
    return total if found else None


# =====================================================================
# 1. 前置门禁执行器 (Pre-run Gates)
# =====================================================================

def run_pre_run_gates(
    context: dict[str, Any] | None = None,
    *,
    tables: dict[str, Any] | None = None,
    exdiv_events: dict[str, Any] | None = None,
    strategy_config: Any = None,
    strict: bool = True,
) -> list[GateResult]:
    """回测执行前数据真值与配置存活门禁校验 (D-1~D-5, L-1, L-3)。

    Fail-Closed：对 ``RUN_PRE_BLOCKING_IDS``（回测内可判门禁），``FAIL`` 或
    ``INCONCLUSIVE`` 均阻断（与 ⑦ 一致，"展示与退出码不得背离"）；其余门禁仍评估记录，
    但归推送期/CI 归属，不在回测内阻断。

    Raises:
        GateBlockerError: 当任一可判门禁 FAIL/INCONCLUSIVE 且 strict=True 时立即抛出。
    """
    ctx = dict(context or {})
    results: list[GateResult] = []

    def _check_result(res: GateResult) -> None:
        results.append(res)
        if is_blocking_result(res):          # FAIL 或 INCONCLUSIVE（⑦/⑳ 统一）
            logger.error(f"[{res.gate_id}] 前置门禁阻断: {res.message}")
            if strict:
                raise GateBlockerError(res.gate_id, res.message, res.metrics)

    # 1. D-1: RawPriceJumpGate
    d1_gate = RawPriceJumpGate(max_jump_ratio=0.30)
    if "bars" in ctx:
        _check_result(d1_gate.evaluate(ctx))
    elif tables:
        d1_passed = True
        for symbol, df in tables.items():
            if df is None or df.empty:
                continue
            exdiv_dates = set()
            if exdiv_events and symbol in exdiv_events:
                for ev in exdiv_events[symbol]:
                    if getattr(ev, "date", None):
                        exdiv_dates.add(ev.date.isoformat() if hasattr(ev.date, "isoformat") else str(ev.date))
            bars = []
            prev_d_str = None
            for idx, row in enumerate(df.itertuples()):
                d_str = str(getattr(row, "date"))[:10]
                is_ex = (
                    bool(getattr(row, "is_exdiv", False))
                    or (d_str in exdiv_dates)
                    or (prev_d_str and any(prev_d_str < ed <= d_str for ed in exdiv_dates))
                    or (idx < 5)
                )
                bars.append({"date": d_str, "close": float(getattr(row, "close")), "is_exdiv": is_ex})
                prev_d_str = d_str
            res = d1_gate.evaluate({"bars": bars, "symbol": symbol})
            if res.status == GateStatus.FAIL:
                _check_result(res)
                d1_passed = False
                break
        if d1_passed:
            _check_result(GateResult(
                gate_id=d1_gate.gate_id,
                name=d1_gate.name,
                category=d1_gate.category,
                status=GateStatus.PASS,
                severity=d1_gate.severity,
                message=f"全部 {len(tables)} 只股票原始日线跳变率检验通过",
                metrics={"tables_count": len(tables)},
                threshold=d1_gate.threshold_desc,
                evidence=d1_gate.evidence,
            ))
    else:
        _check_result(d1_gate.evaluate({}))

    # 2. D-2: FloatMarketCapGate —— 归属（㉓）：需"全池（>=30）市值分布"，小样本不适用；
    #    归推送期抽样取证（pre_push 现读 >=30 只 parquet）。回测内不评估，避免小样本即阻断。

    # 3. D-3: PitDividendYieldGate
    d3_gate = PitDividendYieldGate()
    if "daily_yields" in ctx:
        _check_result(d3_gate.evaluate(ctx))
    elif tables:
        d3_evaluated = False
        for symbol, df in tables.items():
            if "dividend_yield" in df.columns and not df.empty:
                yields = df["dividend_yield"].dropna().tolist()
                if len(yields) >= 60:
                    _check_result(d3_gate.evaluate({"daily_yields": yields, "year": 2024, "symbol": symbol}))
                    d3_evaluated = True
                    break
        if not d3_evaluated:
            _check_result(d3_gate.evaluate({}))
    else:
        _check_result(d3_gate.evaluate({}))

    # 4. D-4: SuspensionVolumeGate
    d4_gate = SuspensionVolumeGate()
    if "bars" in ctx:
        _check_result(d4_gate.evaluate(ctx))
    elif tables:
        d4_dirty = False
        for symbol, df in tables.items():
            if "tradestatus" in df.columns and "volume" in df.columns and not df.empty:
                susp = df[(df["tradestatus"].astype(str) != "1") & (df["volume"].astype(float) > 0)]
                if not susp.empty:
                    bars = [{"date": str(r["date"]), "tradestatus": str(r["tradestatus"]), "volume": float(r["volume"])} for _, r in susp.iterrows()]
                    _check_result(d4_gate.evaluate({"bars": bars, "symbol": symbol}))
                    d4_dirty = True
                    break
        if not d4_dirty:
            _check_result(GateResult(
                gate_id=d4_gate.gate_id,
                name=d4_gate.name,
                category=d4_gate.category,
                status=GateStatus.PASS,
                severity=d4_gate.severity,
                message=f"全部 {len(tables)} 只股票停牌日成交量检验通过",
                threshold=d4_gate.threshold_desc,
                evidence=d4_gate.evidence,
            ))
    else:
        _check_result(d4_gate.evaluate({}))

    # 5. D-5: HighPriceLotGate —— 归属（㉓）：需"委托明细"（run 内部真相），
    #    前置阶段无委托 ⇒ 移出回测前置路径，归回测后/定时全量 CI；⛔ 不在此处评估。

    # 6. L-1: FeatureLivenessGate —— ⛔ 不再用默认特性集合代替证据
    l1_gate = FeatureLivenessGate(required_features=["DIVIDEND_TAX"])
    if "active_features" in ctx:
        l1_ctx: dict[str, Any] = {"active_features": ctx["active_features"], "is_pre_run": True}
        if "fee_summary" in ctx:
            l1_ctx["fee_summary"] = ctx["fee_summary"]
        _check_result(l1_gate.evaluate(l1_ctx))
    else:
        _check_result(_inconclusive(
            l1_gate,
            "未声明本轮回测启用特性集合（active_features），默认值不得代替证据（无证据 ≠ 通过）",
        ))

    # 7. L-3: StaticAstCallGate —— 归属（㉓）：需"运行期调用追踪"（run 内部真相），
    #    回测前置阶段无法取证 ⇒ 移出前置路径，归定时全量 CI；⛔ 不在此处评估。

    return results


# =====================================================================
# 2. 后置门禁执行器 (Post-run Gates)
# =====================================================================

def run_post_run_gates(
    context: dict[str, Any] | None = None,
    *,
    result: Any = None,
    report: Any = None,
    strategy_config: Any = None,
    strict: bool = True,
) -> list[GateResult]:
    """回测执行后撮合、对账、科学防伪与交付治理门禁审计 (E/A/S/G)。

    Fail-Closed：对 ``RUN_POST_BLOCKING_IDS``（回测内可判门禁），``FAIL`` 或
    ``INCONCLUSIVE`` 均阻断（与 ⑦ 一致）；其余门禁仍评估记录，但归推送期/CI 归属。

    Raises:
        GateBlockerError: 当任一可判门禁 FAIL/INCONCLUSIVE 且 strict=True 时立即抛出。
    """
    ctx = dict(context or {})
    results: list[GateResult] = []

    def _check_result(res: GateResult) -> None:
        results.append(res)
        if is_blocking_result(res):          # FAIL 或 INCONCLUSIVE（⑦/⑳ 统一）
            logger.error(f"[{res.gate_id}] 后置门禁阻断: {res.message}")
            if strict:
                raise GateBlockerError(res.gate_id, res.message, res.metrics)

    # 提取成交记录与账本流水
    trades = ctx.get("trades")
    if trades is None and result is not None:
        trades = getattr(result, "trades", [])
    trades = list(trades or [])

    journal_entries = ctx.get("journal_entries")
    if journal_entries is None and result is not None:
        journal_entries = getattr(result, "journal_entries", [])
    journal_entries = list(journal_entries or [])

    # 1. E-1: MustFailCasesGate —— ⛔ 绝不允许"无证据即预设 5 个用例全通过"
    e1_gate = MustFailCasesGate()
    must_fail_results = ctx.get("must_fail_results")
    if isinstance(must_fail_results, dict) and must_fail_results:
        _check_result(e1_gate.evaluate({
            "must_fail_results": must_fail_results,
            "failed_cases": ctx.get("failed_cases", []),
        }))
    else:
        _check_result(_inconclusive(
            e1_gate,
            "缺少 5 必挂极限用例（涨停买拒/跌停卖拒/停牌拒/除权连续/T+1 卖拒）的逐用例结果，"
            "无法判定撮合引擎保真性（无证据 ≠ 通过）",
        ))

    # 2. E-2: BonusSplitFifoGate —— 归属（㉓）：需"送转全量证据"（run 内部真相），
    #    归推送期探针 / 定时全量 CI；⛔ 不在此处评估（避免无证据即阻断回测）。

    # 3. E-3: SlippagePriceCapGate
    e3_gate = SlippagePriceCapGate()
    enriched_e3_trades = []
    for t in trades:
        if isinstance(t, dict):
            enriched_e3_trades.append(t)
        else:
            enriched_e3_trades.append({
                "symbol": getattr(t, "symbol", ""),
                "side": getattr(t, "side", "").value if hasattr(getattr(t, "side", ""), "value") else str(getattr(t, "side", "")),
                "price": getattr(t, "price", Decimal("0")),
                "limit_up": getattr(t, "limit_up", Decimal("0")),
                "limit_down": getattr(t, "limit_down", Decimal("0")),
            })
    _check_result(e3_gate.evaluate({"trades": enriched_e3_trades}))

    # 4. A-1: FeeSumBalanceGate
    a1_gate = FeeSumBalanceGate()
    enriched_a1_trades = []
    for t in trades:
        if isinstance(t, dict):
            enriched_a1_trades.append(t)
        else:
            fees = getattr(t, "fees", {}) or {}
            total_fee = getattr(t, "total_fee", sum(fees.values(), Decimal("0")))
            enriched_a1_trades.append({
                "trade_id": getattr(t, "trade_id", ""),
                "fees": fees,
                "total_fee": total_fee,
            })
    _check_result(a1_gate.evaluate({"trades": enriched_a1_trades}))

    # 5. A-2: DailyCashConserveGate
    a2_gate = DailyCashConserveGate()
    daily_cash_flows = ctx.get("daily_cash_flows")
    if daily_cash_flows is None:
        daily_cash_flows = _build_daily_cash_flows(journal_entries)
    _check_result(a2_gate.evaluate({"daily_cash_flows": daily_cash_flows}))

    # 6. A-3: GoldenRoundtripGate —— 归属（㉓）：需"独立黄金基准"（外部真相），
    #    归定时全量 CI（由独立 golden fixture 注入 roundtrip_total_fee）；⛔ 不在此处评估。

    # 7. A-4: SegmentRateScheduleGate
    a4_gate = SegmentRateScheduleGate()
    _check_result(a4_gate.evaluate({"trades": trades}))

    # 8. S-1: TurnoverCeilingGate —— ⛔ 不再用 0.0 兜底（否则必然通过）
    s1_gate = TurnoverCeilingGate(max_turnover=4.0)
    if "annualized_turnover" in ctx:
        _check_result(s1_gate.evaluate(ctx))
    elif report is not None and getattr(report, "annual_turnover", None) is not None:
        _check_result(s1_gate.evaluate({"annualized_turnover": float(report.annual_turnover)}))
    else:
        _check_result(_inconclusive(s1_gate, "缺少年化单边换手率数据，无法判定换手率硬顶（无证据 ≠ 通过）"))

    # 9. S-2: TimingExitSurvivalGate —— ⛔ 不再用 [] / {} 空数据喂进（门禁内部已 fail-closed）
    s2_gate = TimingExitSurvivalGate()
    s2_ctx: dict[str, Any] = {}
    if "index_below_ma200_dates" in ctx:
        s2_ctx["index_below_ma200_dates"] = ctx["index_below_ma200_dates"]
    if "daily_positions_ratio" in ctx:
        s2_ctx["daily_positions_ratio"] = ctx["daily_positions_ratio"]
    if not s2_ctx:
        _check_result(_inconclusive(s2_gate, "缺少破 MA200 日期与逐日仓位比例数据，无法判定择时空仓生存（无证据 ≠ 通过）"))
    else:
        _check_result(s2_gate.evaluate(s2_ctx))

    # 10. S-3: DynamicSlippageAdvGate —— ⛔ 不再用 b_ret*0.95（与基准同号，结构上不可能由正转负）
    s3_gate = DynamicSlippageAdvGate()
    s3_ctx: dict[str, Any] = {}
    if "orders_adv_ratio" in ctx:
        s3_ctx["orders_adv_ratio"] = ctx["orders_adv_ratio"]
    if "baseline_return" in ctx:
        s3_ctx["baseline_return"] = float(ctx["baseline_return"])
    elif report is not None and getattr(report, "total_return", None) is not None:
        s3_ctx["baseline_return"] = float(report.total_return)
    if "stress_return" in ctx:
        s3_ctx["stress_return"] = float(ctx["stress_return"])
    elif "baseline_return" in s3_ctx:
        stressed = _compute_stress_return(report, trades)
        if stressed is not None:
            s3_ctx["stress_return"] = stressed
    if "baseline_return" not in s3_ctx or "stress_return" not in s3_ctx:
        _check_result(_inconclusive(s3_gate, "缺少滑点压测基准收益率/压力情景收益率，无法判定抗压性（无证据 ≠ 通过）"))
    else:
        _check_result(s3_gate.evaluate(s3_ctx))

    # 11. S-4: DividendTaxLockGate —— 归属（㉓）：需"分红分档计税真相"（run 内部真相），
    #    归定时全量 CI；⛔ 不在此处评估（避免无分红即阻断回测）。

    # 12. S-5: AttributionEvidenceGate —— ⛔ 不再硬编码 code_evidence 默认值（证据链永真）
    s5_gate = AttributionEvidenceGate()
    total_stamp_tax = Decimal("0")
    total_comm = Decimal("0")
    if report is not None and hasattr(report, "fees_total"):
        total_stamp_tax = report.fees_total.get(FeeItem.STAMP_TAX, Decimal("0"))
        total_comm = report.fees_total.get(FeeItem.COMMISSION, Decimal("0"))
    if "total_stamp_tax" in ctx:
        total_stamp_tax = Decimal(str(ctx["total_stamp_tax"]))
    if "total_commission" in ctx:
        total_comm = Decimal(str(ctx["total_commission"]))

    trades_count = len(trades)
    if "trades_count" in ctx:
        trades_count = int(ctx["trades_count"])
    elif trades_count > 0 and total_stamp_tax <= Decimal("0"):
        # 若仅有买入而无卖出，印花税自然为 0，按 0 笔对账避免假阳性
        trades_count = sum(1 for t in trades if "SELL" in str(getattr(t, "side", "") if not isinstance(t, dict) else t.get("side", "")).upper())

    s5_ctx = {
        "trades_count": trades_count,
        "total_stamp_tax": total_stamp_tax,
        "total_commission": total_comm,
    }
    if "code_evidence" in ctx:
        s5_ctx["code_evidence"] = ctx["code_evidence"]
    # ⛔ 未提供 code_evidence 时不注入默认值：门禁将判 FAIL（证据链缺失）
    _check_result(s5_gate.evaluate(s5_ctx))

    # 13. G-1: ProvenanceTriadGate —— ⛔ 不再用常量假哈希兜底
    g1_gate = ProvenanceTriadGate()
    g1_ctx = {
        "git_commit": str(ctx.get("git_commit") or _get_git_commit()),
        "timestamp": str(ctx.get("timestamp") or datetime.datetime.now(datetime.timezone.utc).isoformat()),
    }
    if "data_hash" in ctx and ctx.get("data_hash") is not None and str(ctx["data_hash"]).strip():
        g1_ctx["data_hash"] = str(ctx["data_hash"])
        _check_result(g1_gate.evaluate(g1_ctx))
    else:
        _check_result(_inconclusive(g1_gate, "缺少 data_hash 出处哈希，无法验证出处三件套（无证据 ≠ 通过）"))

    # 14. G-2: TasksSignGate —— 归属（㉓）：tasks 勾选态属推送期/CI（回测期无 tasks 上下文），
    #    ⛔ 不在此处评估（否则无证据即阻断回测）。

    # 15. G-3: MasterFindingGate
    g3_gate = MasterFindingGate(sources_dir=ctx.get("sources_dir"))
    _check_result(g3_gate.evaluate(ctx))

    # 16. G-4: AntiTamperSignatureGate —— ⛔ 回测产物存在时必须执行（不再静默跳过）
    from .gate_g_governance import AntiTamperSignatureGate
    g4_gate = AntiTamperSignatureGate()
    record = ctx.get("run_record")
    if record is None and any(k in ctx for k in ("anti_tamper_signature", "run_id", "params_hash")):
        record = ctx
    if record is not None:
        _check_result(g4_gate.evaluate({"run_record": record}))
    else:
        _check_result(_inconclusive(g4_gate, "回测产物缺少防篡改签名上下文（run_record），无法验证密码学保真（无证据 ≠ 通过）"))

    # 17. G-MDD-1: 回撤上限门禁（P0）—— 仅针对本次回测产物，不扫描全仓
    mdd_gate = MaxDrawdownCeilingGate()
    if "run_record" in ctx:
        _check_result(mdd_gate.evaluate({"run_record": ctx["run_record"]}))
    elif "metrics" in ctx:
        _check_result(mdd_gate.evaluate({"metrics": ctx["metrics"]}))
    else:
        _check_result(_inconclusive(mdd_gate, "缺少本次回测产物（run_record/metrics），无法判定回撤上限（无证据 ≠ 通过）"))

    return results
