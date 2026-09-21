#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""市场规则参数对象（E 路线第三步：29 门通用化产品化）。

门体内的市场规则字面量（整手股数、高价股线、必非零费率科目等）统一收敛到本模块的
``MarketRules`` 参数对象——门体从规则对象读参数，而非内嵌 A 股字面量。

口径纪律：
* **默认值 = 现 A 股口径**（上交所交易规则 3.4.2 整手 100 股、高价股线 300 元、
  印花税+佣金必非零、主板涨跌停 ±10%、T+1 交收）——本仓不传参时行为逐位不变；
* 外部市场由 adapter 显式声明另一套 ``MarketRules``（如玩具市场 ``board_lot_size=1``、
  ``required_fee_subjects=(("total_commission", "佣金"),)``）；
* ⛔ 未声明规则不等于无规则：门体取不到规则时一律回退 **A 股默认**（fail-closed，
  宁可误拦不可误放）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["MarketRules", "resolve_market_rules", "CTX_MARKET_RULES_KEY"]

#: ctx 中承载 MarketRules 的键名（context_builder 注入 / adapter 产 ctx 共用）。
CTX_MARKET_RULES_KEY = "market_rules"


@dataclass(frozen=True)
class MarketRules:
    """市场规则参数对象。

    全部为**标量/元组**字段，可跨进程序列化（``to_dict``/``from_dict``）。

    字段语义：
    * ``market_id`` —— 市场标识（自述口径，报告/日志可见）。
    * ``board_lot_size`` —— 买入整手股数（D-5）；``None`` 或 ``<=1`` 表示无整手约束。
    * ``high_price_limit`` —— 开仓标的单价上限（D-5）；``None`` 表示无高价线约束。
    * ``price_limit_pct`` —— 主板涨跌停幅度（信息性声明：E-3 的逐笔限界由 adapter
      按自身市场规则显式供给 ``limit_up``/``limit_down``，门禁不改判据）。
    * ``t_plus_1`` —— T+1 交收（信息性声明：T+1/FIFO 探针门 E-1/E-2 属本仓特异层，
      不随通用层走）。
    * ``required_fee_subjects`` —— S-5 关税作弊自查的**必非零费率科目集**：
      ``(ctx 键名, 展示名)`` 元组序列，如 ``("total_stamp_tax", "印花税")``。
      ⛔ 必非空——空集等于声明"本市场无任何必须发生的手续费科目"，会掏空
      S-5 的关税置零断言，构造期即拒。
    """

    market_id: str = "CN_ASHARE"
    board_lot_size: int | None = 100
    high_price_limit: float | None = 300.0
    price_limit_pct: float = 0.10
    t_plus_1: bool = True
    required_fee_subjects: tuple[tuple[str, str], ...] = field(
        default=(("total_stamp_tax", "印花税"), ("total_commission", "佣金"))
    )

    def __post_init__(self) -> None:
        # fail-closed 形状校验（仅校验形状/合法性，不约束取值——外部市场规则不同不是错）。
        if self.board_lot_size is not None and self.board_lot_size < 1:
            raise ValueError(f"board_lot_size 必须 >=1 或 None，实得 {self.board_lot_size!r}")
        if self.high_price_limit is not None and self.high_price_limit <= 0:
            raise ValueError(f"high_price_limit 必须 >0 或 None，实得 {self.high_price_limit!r}")
        if self.price_limit_pct < 0:
            raise ValueError(f"price_limit_pct 不得为负，实得 {self.price_limit_pct!r}")
        subjects = tuple(self.required_fee_subjects or ())
        if not subjects:
            raise ValueError("required_fee_subjects 必非空（空集会使 S-5 关税置零断言失效）")
        for item in subjects:
            if not (isinstance(item, (tuple, list)) and len(item) == 2 and all(isinstance(x, str) and x for x in item)):
                raise ValueError(f"required_fee_subjects 元素必须为 (ctx 键名, 展示名) 非空字符串对，实得 {item!r}")
        object.__setattr__(self, "required_fee_subjects", tuple((str(k), str(v)) for k, v in subjects))

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "board_lot_size": self.board_lot_size,
            "high_price_limit": self.high_price_limit,
            "price_limit_pct": self.price_limit_pct,
            "t_plus_1": self.t_plus_1,
            "required_fee_subjects": [list(x) for x in self.required_fee_subjects],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MarketRules":
        if not isinstance(d, dict):
            raise TypeError(f"MarketRules.from_dict 需要 dict，实得 {type(d).__name__}")
        return cls(
            market_id=str(d.get("market_id", "CN_ASHARE")),
            board_lot_size=d.get("board_lot_size", 100),
            high_price_limit=d.get("high_price_limit", 300.0),
            price_limit_pct=float(d.get("price_limit_pct", 0.10)),
            t_plus_1=bool(d.get("t_plus_1", True)),
            required_fee_subjects=tuple(
                (str(k), str(v)) for k, v in d.get(
                    "required_fee_subjects",
                    (("total_stamp_tax", "印花税"), ("total_commission", "佣金")),
                )
            ),
        )


def resolve_market_rules(context: Any, explicit: MarketRules | None = None) -> MarketRules:
    """解析生效的市场规则：构造参显式注入 > ctx['market_rules'] > A 股默认。

    ⛔ 缺证回退 A 股默认而非"无约束"——fail-closed：外部 ctx 忘了声明规则时，
    宁可按最严口径误拦，不得静默放行。
    """
    if explicit is not None:
        return explicit
    candidate = None
    if isinstance(context, dict):
        candidate = context.get(CTX_MARKET_RULES_KEY)
    elif context is not None:
        candidate = getattr(context, CTX_MARKET_RULES_KEY, None)
    if isinstance(candidate, MarketRules):
        return candidate
    if isinstance(candidate, dict):                      # JSON 落盘回读（--ctx-json 路径）
        return MarketRules.from_dict(candidate)
    return MarketRules()
