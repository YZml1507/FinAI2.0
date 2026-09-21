#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ExternalEvidenceAdapter：外部回测账本 → 门禁 ctx 键集合的翻译协议（E 路线通用化）。

设计出处：``docs/E_ROUTE_GATE_GENERALIZATION_SPIKE.md`` §4。核心事实：

* 门禁本体协议是鸭子类型 ``evaluate(context: dict)``——各门一律
  ``context.get(key, default)`` 取值，**不要求本仓任何业务类型**
  （Order/Trade/Ledger 均不需要）。适配器只需产出普通 dict/list/str/数值。
* 缺失键不是错误：产不出就**不填**，对应门判 INCONCLUSIVE（fail-closed 语义保留；
  ⛔ 严禁为凑 PASS 伪造键值——spike 实证 E-2 喂假 ``final_positions`` 会反被抓 FAIL）。
* ``market_rules``：外部市场的规则档（整手/高价线/必需规费科目/涨跌停/T+1），
  缺省回落 ``MarketRules()`` = A 股口径。

实现路径二选一（``audit_external`` 自动识别）：

1. **整体产 ctx**：实现 ``build_context() -> Mapping``（如 spike 玩具适配器），
   可含 ``market_rules`` 键；
2. **分键产出**：实现下列 ``trades()/orders()/...`` 方法的一个子集，
   由 :func:`assemble_context` 聚合成 ctx（并从 ``trades`` 自动推导
   ``trades_count`` 与 ``total_<科目小写>`` 聚合键）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Protocol, runtime_checkable

from .market_rules import MarketRules


@runtime_checkable
class ExternalEvidenceAdapter(Protocol):
    """外部回测 → 门禁 ctx 的翻译协议。

    所有方法均为**可选**：实现多少喂多少，缺键 ⇒ 对应门 INCONCLUSIVE。
    唯一被 ``audit_external`` 硬性要求的是二者其一：
    ``build_context()``（整体产出）或至少一个分键方法（走 ``assemble_context``）。
    """

    def build_context(self) -> Mapping[str, Any]:
        """整体产出门禁 ctx（优先路径；提供则忽略分键方法）。"""
        ...

    def market_rules(self) -> MarketRules | Mapping[str, Any] | None:
        """外部市场规则档 → 注入 ``ctx["market_rules"]``；``None`` ⇒ A 股默认。"""
        ...

    def trades(self) -> list[dict]:
        """成交明细 ``{date, symbol, side, price, volume, amount, fees{ITEM:amt}, total_fee, limit_up?, limit_down?}``
        → A-1、A-4、E-3；``fees`` 科目自动聚合出 ``total_<item 小写>``（S-5 必需规费键的出处）。"""
        ...

    def orders(self) -> list[dict]:
        """委托明细 ``{side, price, volume}`` → D-5。"""
        ...

    def daily_cash_flows(self) -> list[dict]:
        """逐日现金守恒流水 ``{date, cash_start, cash_end, trade_in, trade_out, fee_out,
        dividend_in, dividend_tax_out, other_in, other_out}`` → A-2。"""
        ...

    def ledger_entries(self) -> list[dict]:
        """账本流水 ``{entry_type, amount, fees{}}`` → L-1（另配 ``fee_summary()`` /
        ``active_features()`` 可选方法喂同名 ctx 键）。"""
        ...

    def allocation(self) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """``(target_weights, actual_values)``（≥3 标的）→ L-2。"""
        ...

    def run_records(self) -> list[dict]:
        """产物记录 ``{run_id, code_version, data_version, params_hash, status,
        metrics{}, timestamp, anti_tamper_signature?, repro_fingerprint?}`` →
        G-4、G-MDD-1、G-STRESS-1、G-REPRO-1、S-1（``metrics.annual_turnover``）。
        签名可由 ``tamper_guard.sign_run_record`` 代产（签名域外字段不影响签名）。"""
        ...

    def provenance(self) -> Mapping[str, Any]:
        """出处三件套 ``{git_commit(hex≥7), data_hash(hex≥16), timestamp}`` → G-1。"""
        ...

    def roundtrip_fee_probe(self) -> Mapping[str, Any]:
        """``{roundtrip_total_fee, expected_fee?}`` → A-3（``expected_fee`` 为逃生门）。"""
        ...

    def market_bars(self) -> Mapping[str, Any]:
        """行情段 ``{bars/frame, exdiv_dates, float_mv_list, amount_list, daily_yields, year, symbol}``
        → D-1~D-4（可选；不喂则 D 系判 INCONCLUSIVE）。"""
        ...

    def extra_context(self) -> Mapping[str, Any]:
        """逃生口：协议未列名的附加 ctx 键（如 ``code_evidence``/``annualized_turnover``
        覆盖值），**最后合并**、优先级最高——但伪造键值会被对应门如实判 FAIL。"""
        ...


#: 分键方法 → ctx 键 的直映射（``assemble_context`` 用）。
_METHOD_KEY_MAP: tuple[tuple[str, str], ...] = (
    ("trades", "trades"),
    ("orders", "orders"),
    ("daily_cash_flows", "daily_cash_flows"),
    ("ledger_entries", "ledger_entries"),
    ("run_records", "run_records"),
    ("fee_summary", "fee_summary"),
    ("active_features", "active_features"),
)


def _derive_fee_totals(ctx: dict[str, Any]) -> None:
    """从 ``ctx["trades"][*].fees`` 聚合 ``total_<科目小写>``（S-5/A-1 消费的键）。

    仅在 adapter 未显式提供同名键时填入（``setdefault``——显式值优先）。
    """
    trades = ctx.get("trades")
    if not trades:
        return
    totals: dict[str, Decimal] = {}
    for t in trades:
        fees = t.get("fees") if isinstance(t, dict) else getattr(t, "fees", None)
        if not isinstance(fees, Mapping):
            continue
        for item, amt in fees.items():
            key = str(item)
            try:
                totals[key] = totals.get(key, Decimal("0")) + Decimal(str(amt))
            except ArithmeticError:
                continue
    for item, total in totals.items():
        ctx.setdefault(f"total_{item.lower()}", str(total))


def assemble_context(adapter: Any) -> dict[str, Any]:
    """把 adapter 翻译成门禁 ctx（两条路径自动识别）。

    * adapter 有 ``build_context()`` ⇒ 直接采用其产出（adapter 全权自装配），
      仅 ``setdefault`` 补 ``market_rules``（adapter 自供则尊重）。
    * 否则按分键协议装配：分键方法 → ``market_bars``/``allocation``/``provenance``/
      ``roundtrip_fee_probe`` → ``run_records`` 派生（首条 → ``run_record``，
      ``metrics`` 摊平 + ``round_trips``/``trading_days``/``annualized_turnover``）
      → ``trades`` 费用聚合 → ``market_rules`` → ``extra_context``（最后合并，
      显式声明 > 推导值）。
    """
    rules_fn = getattr(adapter, "market_rules", None)

    if callable(getattr(adapter, "build_context", None)):
        ctx = dict(adapter.build_context())
        if "market_rules" not in ctx and callable(rules_fn):
            rules = rules_fn()
            if rules is not None:
                ctx["market_rules"] = rules
        ctx.setdefault("market_rules", MarketRules())
        return ctx

    ctx: dict[str, Any] = {}
    for method, key in _METHOD_KEY_MAP:
        fn = getattr(adapter, method, None)
        if callable(fn):
            ctx[key] = fn()

    bars = getattr(adapter, "market_bars", None)
    if callable(bars):
        ctx.update(bars() or {})

    alloc = getattr(adapter, "allocation", None)
    if callable(alloc):
        target_weights, actual_values = alloc()
        ctx["target_weights"] = target_weights
        ctx["actual_values"] = actual_values

    prov = getattr(adapter, "provenance", None)
    if callable(prov):
        p = dict(prov() or {})
        for ctx_key in ("git_commit", "data_hash", "timestamp"):
            if ctx_key in p:
                ctx[ctx_key] = p[ctx_key]

    probe = getattr(adapter, "roundtrip_fee_probe", None)
    if callable(probe):
        ctx.update(probe() or {})

    records = ctx.get("run_records")
    if records:
        ctx.setdefault("run_record", records[0])
        metrics = records[0].get("metrics") or {}
        ctx.setdefault("metrics", metrics)
        for m_key, ctx_key in (("round_trips", "round_trips"),
                               ("trading_days", "trading_days"),
                               ("annual_turnover", "annualized_turnover")):
            if m_key in metrics:
                ctx.setdefault(ctx_key, metrics[m_key])

    _derive_fee_totals(ctx)
    # 仅在 adapter 真供了 trades 时推导成交笔数——缺证据绝不伪造「零成交」断言。
    if ctx.get("trades") is not None:
        ctx.setdefault("trades_count", len(ctx["trades"]))

    if callable(rules_fn):
        rules = rules_fn()
        if rules is not None:
            ctx.setdefault("market_rules", rules)
    ctx.setdefault("market_rules", MarketRules())

    extra = getattr(adapter, "extra_context", None)
    if callable(extra):
        ctx.update(extra() or {})

    return ctx
