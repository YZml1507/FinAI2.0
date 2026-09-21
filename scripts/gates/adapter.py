#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""外部证据适配层（E 路线第三步）：``ExternalEvidenceAdapter`` 协议 + ctx 装配。

把**与本仓 schema 无关**的外部回测账本翻译成 ``scripts/gates`` 各门消费的
ctx 键集合。协议为鸭子类型（``typing.Protocol``）：外部系统只需产出普通
``dict/list/str/Decimal`` 可转型值，⛔ 不要求导入本仓任何业务类型
（Order/Trade/Ledger 均不需要）。

Fail-Closed 语义对外部证据同样成立：

* 适配器**不实现**某方法 / 返回空 ⇒ 对应键不进 ctx ⇒ 相关门判 INCONCLUSIVE
  （无证据 ≠ 通过），不兜底、不伪造；
* 适配器不声明 ``market_rules`` ⇒ 门体回退 A 股默认规则（最严口径），
  不得静默放松；
* 本模块**不评估**门禁——评估入口在 ``audit_external.py``。

协议方法与 ctx 键的对应关系（实跑实证见
``experiments/spikes/gate_generalization/`` 与
``docs/E_ROUTE_GATE_GENERALIZATION_SPIKE.md`` §4）：
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Protocol, runtime_checkable

from .market_rules import CTX_MARKET_RULES_KEY, MarketRules

__all__ = ["ExternalEvidenceAdapter", "build_external_context"]


@runtime_checkable
class ExternalEvidenceAdapter(Protocol):
    """外部回测 → 门禁 ctx 键集合的翻译协议。

    所有方法均为**可选实现**：缺哪个方法就少哪组键，对应门如实 INCONCLUSIVE。
    返回值一律为普通 dict/list/str/数值（``Decimal`` 可转型），不得返回本仓类型。
    """

    def market_rules(self) -> MarketRules:
        """外部市场规则声明 → D-5/S-5 等参数化门读取。

        不实现/返回非 ``MarketRules`` ⇒ 回退 A 股默认（fail-closed 最严口径）。
        """
        ...

    def trades(self) -> list[dict]:
        """成交明细 → A-1/A-4/E-3。

        每条 ``{date, side, price, volume, amount, fees{科目:金额}, total_fee,
        limit_up?, limit_down?}``；``limit_*`` 由 adapter 按自身市场规则合成。
        """
        ...

    def orders(self) -> list[dict]:
        """委托明细 → D-5。每条 ``{side, price, volume}``。"""
        ...

    def daily_cash_flows(self) -> list[dict]:
        """逐日资金守恒流水 → A-2。

        每条 ``{date, cash_start, cash_end, trade_in, trade_out, fee_out,
        dividend_in, dividend_tax_out, other_in, other_out}``。
        """
        ...

    def ledger_entries(self) -> list[dict]:
        """账本流水 → L-1。每条 ``{entry_type, amount, fees{}}``。"""
        ...

    def active_features(self) -> list[str]:
        """本轮声明启用的特性名 → L-1（如 ``["COMMISSION"]``）。"""
        ...

    def fee_summary(self) -> Mapping[str, Any]:
        """科目 → 累计发生额 → L-1 增益项。"""
        ...

    def allocation(self) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """(target_weights, actual_values) → L-2（≥3 标的）。"""
        ...

    def run_records(self) -> list[dict]:
        """产物记录 → G-4/G-MDD-1/G-STRESS-1/G-REPRO-1（并抬升 metrics 到顶层键）。

        每条 ``{run_id, code_version, data_version, params_hash, status, metrics{},
        timestamp, anti_tamper_signature?, repro_fingerprint?}``。签名可由
        ``tamper_guard.sign_run_record`` 代产（签名域仅六键，证据块在域外）。
        """
        ...

    def provenance(self) -> dict:
        """出处三件套 → G-1：``{git_commit(hex≥7), data_hash(hex≥16), timestamp}``。"""
        ...

    def roundtrip_fee_probe(self) -> dict:
        """``{roundtrip_total_fee, expected_fee?}`` → A-3（expected_fee 为逃生门）。"""
        ...

    def market_bars(self) -> dict:
        """行情证据段 → D-1~D-4。

        ``{bars?, frame?, exdiv_dates?, float_mv_list?, amount_list?,
        daily_yields?, year?}``——逐键并入 ctx，缺啥门如实 INCONCLUSIVE。
        """
        ...

    def extra_context(self) -> dict:
        """逃生口：其余通用键（``code_evidence``/``orders_adv_ratio``/
        ``baseline_return``/``stress_return``/``source_code``/``required_calls``/
        ``executed_calls`` 等）。**最后并入**，可覆盖前述派生键。"""
        ...


def _maybe_call(adapter: Any, name: str) -> Any:
    """鸭子类型调用：方法不存在/不可调用 ⇒ None（缺键，交由门判 INCONCLUSIVE）。"""
    fn = getattr(adapter, name, None)
    if not callable(fn):
        return None
    return fn()


def build_external_context(adapter: Any) -> dict[str, Any]:
    """把 adapter 产出装配成门禁 ctx（纯 dict，无副作用）。

    装配次序（后者覆盖前者）：
    1. ``provenance()`` 三件套；
    2. ``run_records()``（含 ``metrics`` 抬升 + 出处字段回填）；
    3. ``trades()``/``orders()``/``daily_cash_flows()``/``ledger_entries()``/
       ``active_features()``/``fee_summary()``/``allocation()``；
    4. ``roundtrip_fee_probe()``、``market_bars()`` 段并入；
    5. ``market_rules`` 注入（未声明 ⇒ A 股默认，fail-closed）；
    6. ``extra_context()`` 逃生口最后并入。

    ⛔ 手续费总额派生：``trades[].fees{科目:额}`` 按科目加总成
    ``total_<科目小写>``（``STAMP_TAX→total_stamp_tax``、``COMMISSION→total_commission``）——
    科目缺席即键缺席（缺失 ≠ 0，S-5 依旧可判"置零作弊"）。
    """
    ctx: dict[str, Any] = {}

    # 1. 出处三件套
    prov = _maybe_call(adapter, "provenance")
    if isinstance(prov, Mapping):
        for k in ("git_commit", "data_hash", "timestamp"):
            if prov.get(k) is not None:
                ctx[k] = str(prov[k])

    # 2. 产物记录 + metrics 抬升（口径对齐 context_builder：run_record 取最新一条，
    #    metrics 顶层抬升仅取存在字段，⛔ 不编造缺键）
    records = _maybe_call(adapter, "run_records")
    if records:
        recs = list(records)
        ctx["run_records"] = recs
        record = recs[-1]
        ctx["run_record"] = record
        metrics = record.get("metrics") if isinstance(record, Mapping) else None
        if isinstance(metrics, Mapping):
            ctx["metrics"] = dict(metrics)
            if metrics.get("annual_turnover") is not None:
                ctx["annualized_turnover"] = metrics["annual_turnover"]
            if metrics.get("round_trips") is not None:
                ctx["round_trips"] = metrics["round_trips"]
            if metrics.get("trading_days") is not None:
                ctx["trading_days"] = metrics["trading_days"]
            if metrics.get("total_return") is not None:
                ctx["total_return"] = metrics["total_return"]
        if isinstance(record, Mapping):
            # 出处回填：provenance() 未给全时，run_record 自带字段兜底（不覆盖已给值）
            ctx.setdefault("git_commit", str(record.get("code_version", "")) or None)
            ctx.setdefault("timestamp", str(record.get("timestamp", "")) or None)
            data_hash = record.get("data_hash") or record.get("data_version")
            if data_hash is not None:
                ctx.setdefault("data_hash", str(data_hash))
        # 剔除兜底回填产生的 None（setdefault 不应引入空键）
        for k in ("git_commit", "timestamp"):
            if ctx.get(k) is None:
                ctx.pop(k, None)

    # 3. 成交/委托/现金流/账本/特性/分配
    trades = _maybe_call(adapter, "trades")
    if trades is not None:
        ctx["trades"] = list(trades)
        ctx["trades_count"] = len(ctx["trades"])
        fee_totals: dict[str, Any] = {}
        for t in ctx["trades"]:
            fees = t.get("fees") if isinstance(t, Mapping) else getattr(t, "fees", None)
            if isinstance(fees, Mapping):
                for item, amt in fees.items():
                    key = f"total_{str(item.value if hasattr(item, 'value') else item).lower()}"
                    fee_totals[key] = fee_totals.get(key, Decimal("0")) + Decimal(str(amt))
        ctx.update({k: str(v) for k, v in fee_totals.items()})

    orders = _maybe_call(adapter, "orders")
    if orders is not None:
        ctx["orders"] = list(orders)

    flows = _maybe_call(adapter, "daily_cash_flows")
    if flows is not None:
        ctx["daily_cash_flows"] = list(flows)

    entries = _maybe_call(adapter, "ledger_entries")
    if entries is not None:
        ctx["ledger_entries"] = list(entries)

    feats = _maybe_call(adapter, "active_features")
    if feats is not None:
        ctx["active_features"] = list(feats)

    fee_summary = _maybe_call(adapter, "fee_summary")
    if fee_summary is not None:
        ctx["fee_summary"] = dict(fee_summary)

    alloc = _maybe_call(adapter, "allocation")
    if alloc is not None:
        tw, av = alloc
        ctx["target_weights"] = dict(tw)
        ctx["actual_values"] = dict(av)

    # 4. 探测/行情段
    probe = _maybe_call(adapter, "roundtrip_fee_probe")
    if isinstance(probe, Mapping):
        ctx.update(probe)

    bars = _maybe_call(adapter, "market_bars")
    if isinstance(bars, Mapping):
        ctx.update(bars)

    # 5. 市场规则：未声明 ⇒ A 股默认（fail-closed）
    rules = _maybe_call(adapter, "market_rules")
    ctx[CTX_MARKET_RULES_KEY] = rules if isinstance(rules, MarketRules) else MarketRules()

    # 6. 逃生口（最后并入，允许显式覆盖派生键——如 code_evidence）
    extra = _maybe_call(adapter, "extra_context")
    if isinstance(extra, Mapping):
        ctx.update(extra)

    return ctx
