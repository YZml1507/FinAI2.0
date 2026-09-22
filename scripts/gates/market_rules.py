#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""市场规则参数对象（E 路线通用化：把门体内嵌的市场断言常量抽为可注入参数）。

背景见 ``docs/E_ROUTE_GATE_GENERALIZATION_SPIKE.md`` §4.3-2：D-5（整手/高价线）、
S-5（必非零费用科目）等门的 FAIL/PASS 语义不是 ctx 键的问题，而是**断言常量**
的问题。本模块提供统一参数对象 ``MarketRules``，门体经
:func:`resolve_market_rules` 从 ctx 读取（``context["market_rules"]``），
缺省回退 A 股默认值——⛔ 硬约束：**默认值 = 现 A 股口径，本仓行为逐位不变**。

外部市场接入方式：adapter 在 ctx 中注入自己的 ``MarketRules``（或等值 dict），
例如无印花税市场把 ``required_fee_totals`` 收成仅 ``("total_commission", "佣金")``，
无整手约束市场把 ``lot_size`` 置 ``None``。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


@dataclass(frozen=True)
class MarketRules:
    """市场规则参数（默认构造 = 上交所/深交所 A 股口径）。

    Attributes:
        name: 规则档名（仅展示用，如 "A股" / 外部市场名）。
        lot_size: 买入委托整手股数；``None`` 表示该市场无整手约束（D-5）。
        max_buy_price: 高价股开仓单价上限（元）；``None`` 表示无上限（D-5）。
        stamp_tax_item: 印花税在 ``fees`` 科目 dict 中的键名（A-1/A-4 等消费）。
        commission_item: 佣金科目键名。
        required_fee_totals: 「有成交则必须非零」的聚合费用 ctx 键清单，
            ``(ctx 键名, 展示名)`` 有序对（S-5）。A 股口径要求印花税+佣金均非零；
            无印花税的外部市场只列佣金即可，**不得**靠置零科目骗过该门。
        limit_pct: 涨跌停幅度（如 0.10 = ±10%）；``None`` 表示无交易所限幅
            （供外部 adapter 合成 E-3 的 limit_up/limit_down 及后续 D-4 参数化）。
        t_plus_1: 是否 T+1 交收（A 股 ``True``；探针类门 E-1/E-2 的本仓语义，
            外部市场仅作信息披露位）。
    """

    name: str = "A股"
    lot_size: int | None = 100
    max_buy_price: Decimal | None = Decimal("300")
    stamp_tax_item: str = "STAMP_TAX"
    commission_item: str = "COMMISSION"
    required_fee_totals: tuple[tuple[str, str], ...] = (
        ("total_stamp_tax", "印花税"),
        ("total_commission", "佣金"),
    )
    limit_pct: Decimal | None = Decimal("0.10")
    t_plus_1: bool = True


#: ``MarketRules`` dict 形态注入时按字段类型强转的 Decimal 字段。
_DECIMAL_FIELDS = frozenset({"max_buy_price", "limit_pct"})


def coerce_market_rules(raw: Mapping[str, Any]) -> MarketRules:
    """把 duck-typed dict 规则强转为 ``MarketRules``（外部 JSON 入口）。

    字符串/数字字段按字段类型转型：Decimal 字段走 ``Decimal(str(v))``，
    ``required_fee_totals`` 接受 list/tuple 的 ``(键, 名)`` 对，
    ``lot_size`` 转 int，``t_plus_1`` 转 bool。未知键一律拒收（fail-closed：
    拼错的键名不许静默落到默认值上）。
    """
    valid = set(MarketRules.__dataclass_fields__)  # noqa: SLF001
    unknown = set(raw) - valid
    if unknown:
        raise TypeError(f"market_rules 含未知字段 {sorted(unknown)}，合法字段 {sorted(valid)}")
    kwargs: dict[str, Any] = dict(raw)
    for f in _DECIMAL_FIELDS:
        if kwargs.get(f) is not None:
            kwargs[f] = Decimal(str(kwargs[f]))
    if kwargs.get("required_fee_totals") is not None:
        kwargs["required_fee_totals"] = tuple(
            (str(k), str(label)) for k, label in kwargs["required_fee_totals"]
        )
    if kwargs.get("lot_size") is not None:
        kwargs["lot_size"] = int(kwargs["lot_size"])
    if kwargs.get("t_plus_1") is not None:
        kwargs["t_plus_1"] = bool(kwargs["t_plus_1"])
    return MarketRules(**kwargs)


def resolve_market_rules(context: Any) -> MarketRules:
    """从 ctx 取 ``market_rules`` 键；缺省回退 A 股默认档（本仓行为不变）。

    接受三种形态：``MarketRules`` 实例（直通）、``Mapping``（``coerce`` 强转）、
    ``None``/缺键（默认档）。其他类型直接 raise——规则对象类型错误是配置缺陷，
    按 fail-closed 让调用方显式失败而非静默用错规则。
    """
    if not context:
        return MarketRules()
    raw = context.get("market_rules") if isinstance(context, Mapping) else getattr(context, "market_rules", None)
    if raw is None:
        return MarketRules()
    if isinstance(raw, MarketRules):
        return raw
    if isinstance(raw, Mapping):
        return coerce_market_rules(raw)
    raise TypeError(
        f"ctx['market_rules'] 必须是 MarketRules | Mapping | None，收到 {type(raw).__name__}"
    )
