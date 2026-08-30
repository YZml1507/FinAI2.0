#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""复权口径统一枚举（R4）——REVALIDATE.md R4 / 设计稿 R4_adjust_mode_design_20260830.md §1-§2。

实测定论（12 号文档 §9-A / REVALIDATE.md R4，⛔ 不是文档约定）：
  akshare adjust='' → 不复权；adjust='qfq'/'hfq' → 前/后复权
  efinance fqt=0 → 不复权；fqt=1 → 前复权；fqt=2 → 后复权
  mootdx/tdxpy 无参数 → 不复权（TDX 协议读本地/行情档，系数仅 =0.01 单位换算）
  baostock adjustflag='3'/''  → 后复权（baostock 默认=后复权）；
           adjustflag='2'     → 前复权；adjustflag='1' → 不复权
  adata adjust_type=0 → 不复权；1 → 前复权；2 → 后复权（默认 1）

⛔⛔ 本表是全仓**唯一**允许写死复权取值的地方：别处一律不许手写
`"fqt": 1` 之类的字面量，必须经 `to_kwargs()` 从这里解析。
 ⛔ 不设 ``UNKNOWN``/``AUTO`` 枚举值：不知道口径 = 不能入库（宁可缺不可错）。

inspect 实测签名证据链（本机 2026-08-30，装库版本）：
  akshare 1.18.64：``stock_zh_a_hist(..., adjust: str = '')``
  efinance 0.5.9：``stock.get_quote_history(..., fqt: int = 1)``
  mootdx 0.11.7：``StdReader.daily(symbol=None, **kwargs)`` —— **无复权参数**（读本地 vipdoc）
  tdxpy 0.2.7：``TdxDailyBarReader.get_df_*`` / ``hq.get_security_bars(...)`` ——
      **无复权参数**（系数 ``SECURITY_COEFFICIENT[SH_A_STOCK]=[0.01,0.01]``，仅单位换算，非复权）
  adata 2.9.5：``get_market(..., adjust_type: int = 1)``，docstring 明示
      ``0.不复权 / 1.前复权 / 2.后复权``（默认前复权！）
  baostock 0.9.2：``query_history_k_data_plus(..., adjustflag: str='3')``
      （历史上传 ``''`` 也按后复权；'1'=不复权 待实测确认，见设计稿 §6 风险 1）
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class AdjustmentMode(str, Enum):
    """复权口径三态。⛔ 只有这三态成立才允许入库（R4 §1）。"""

    RAW = "RAW"   # 不复权
    QFQ = "QFQ"   # 前复权
    HFQ = "HFQ"   # 后复权


#: 模块级别名（`AdjustmentMode.RAW` 全限定冗长，表与守卫里用短名）。
#: ⛔ 这是别名不是新口径；新口径只能加进上面的枚举。
RAW = AdjustmentMode.RAW
QFQ = AdjustmentMode.QFQ
HFQ = AdjustmentMode.HFQ


class UnknownAdjustment(ValueError):
    """映射表里没有的复权取值。⛔ 拒绝静默继续 —— R4 的目的就是不放过任何一次口径漂移。"""


#: 统一入参名（capability 契约层）。各库原生名（adjust/fqt/adjustflag/adjust_type）
#: 只允许在**本模块映射表与 to_kwargs()** 出现，编排层一律用这个规范名。
ADJUST_KWARG_CANONICAL = "adjustment"


# ⛔ 只允许出现在这里；别处一律不许写死复权值——而是从这条表解析出来。
# （枚举成员不自动出模块级别名，全限定 `AdjustmentMode.RAW`，⛔ 不许裸 `RAW`。）
AKSHARE: dict[str, AdjustmentMode] = {
    "": AdjustmentMode.RAW, "qfq": AdjustmentMode.QFQ, "hfq": AdjustmentMode.HFQ}
EFINANCE: dict[int, AdjustmentMode] = {
    0: AdjustmentMode.RAW, 1: AdjustmentMode.QFQ, 2: AdjustmentMode.HFQ}
BAOSTOCK: dict[str, AdjustmentMode] = {
    "1": AdjustmentMode.RAW, "2": AdjustmentMode.QFQ,
    "3": AdjustmentMode.HFQ, "": AdjustmentMode.HFQ}
#            ↑ baostock adjustflag 默认 '3'=后复权，且不传同默认为 '3'
#              ⛔ 按"禁止默认调用"原则，调用侧最终一律显式传，
#              空串分支只留作兼容警告路径（设计稿 §2）。
ADATA: dict[int, AdjustmentMode] = {
    0: AdjustmentMode.RAW, 1: AdjustmentMode.QFQ, 2: AdjustmentMode.HFQ}
MOOTDX: dict[str, AdjustmentMode] = {}    # 无参数 → 只有 RAW，见 to_kwargs 的 RAW-only 守卫
TDXPY: dict[str, AdjustmentMode] = {}     # 无参数 → 只有 RAW

_TABLES: dict[str, dict[Any, AdjustmentMode]] = {
    "akshare": AKSHARE,
    "efinance": EFINANCE,
    "baostock": BAOSTOCK,
    "adata": ADATA,
    "mootdx": MOOTDX,
    "tdxpy": TDXPY,
}

#: 每库复权实参的**关键字名**（to_kwargs 由此生成实参的键）。
_KWARG_NAMES: dict[str, str] = {
    "akshare": "adjust",
    "efinance": "fqt",
    "baostock": "adjustflag",
    "adata": "adjust_type",
    "mootdx": "adjust",       # 无参库：键不会被用到（RAW-only 返空 dict）
    "tdxpy": "adjust",
}

#: 口径 → 人话标签（血缘/落盘 meta 用；⛔ 不用 int 编码，避免 0/1/2 撞库差异）。
_LABELS: dict[AdjustmentMode, str] = {
    AdjustmentMode.RAW: "不复权",
    AdjustmentMode.QFQ: "前复权",
    AdjustmentMode.HFQ: "后复权",
}


def to_kwargs(mode: AdjustmentMode, lib: str) -> dict[str, Any]:
    """把统一口径变成该库要的实参；无参库只接受 ``RAW``（返空 dict）。

    ⛔ 映射表里找不到对应取值时抛 ``UnknownAdjustment``，绝不静默换口径。
    """
    table = _TABLES[lib]
    if not table:
        if mode is not AdjustmentMode.RAW:   # ⛔ 无参库只接受 RAW
            raise UnknownAdjustment(f"{lib} 无复权参数，只能 RAW")
        return {}
    for raw, m in table.items():
        if m is mode:
            return {_KWARG_NAMES[lib]: raw}
    raise UnknownAdjustment(f"口径 {mode} 无法映射到 {lib}")


def to_label(mode: AdjustmentMode) -> str:
    """口径 → 人话标签（日志/血缘/evidence 用）。"""
    return _LABELS[mode]
