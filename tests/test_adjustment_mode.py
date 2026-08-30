#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R4（复权口径映射表）单测 —— 全部**离线**，无任何网络调用。

对应 `REVALIDATE.md R4` / `R4_adjust_mode_design_20260830.md` 的验收判据：
  ① 映射表存在且覆盖六库（akshare/efinance/baostock/adata/mootdx/tdxpy）；
  ③ 表级守卫：`daily_bar` 的 `ohlcv_daily` 口径候选 `adjust` 字段非空，且与
     `pinned` 的字面量实参**同源一致**（⛔ 不许某条改了一个忘了另一个）；
     `to_kwargs()` 在无参库（mootdx/tdxpy）上只接受 RAW，非 RAW 必抛
     `UnknownAdjustment`（绝不静默换口径）。

⛔⛔ 永不静默原则：本模块的断言**不允许** try/except 吞错 —— 任何失败都要让
pytest 红，而不是被 except 接住。
"""
from __future__ import annotations

import pytest

from finai.sources import capability_router
from finai.sources.adjustment_mode import (
    ADJUST_KWARG_CANONICAL,
    AdjustmentMode,
    UnknownAdjustment,
    to_kwargs,
)


def test_mapping_tables_cover_six_libraries() -> None:
    """① 六库都有表（mootdx/tdxpy 允许空 dict + RAW-only 守卫）。"""
    from finai.sources import adjustment_mode as m

    for lib in ("akshare", "efinance", "baostock", "adata", "mootdx", "tdxpy"):
        assert lib in m._TABLES, f"映射表缺库 {lib}"
    # 四参库非空且三态齐备
    for table in (m.AKSHARE, m.EFINANCE, m.BAOSTOCK, m.ADATA):
        assert set(table.values()) == {
            AdjustmentMode.RAW, AdjustmentMode.QFQ, AdjustmentMode.HFQ}
    # 无参库为空 dict（RAW-only 由 to_kwargs 守卫，而非靠表里有值）
    assert m.MOOTDX == {} and m.TDXPY == {}
    # ⛔ 不设 UNKNOWN/AUTO 枚举值：不知道口径 = 不能入库
    assert {e.value for e in AdjustmentMode} == {"RAW", "QFQ", "HFQ"}


def test_to_kwargs_generates_native_kwarg() -> None:
    """to_kwargs 把统一口径翻译成该库原生实参（键名也正确）。"""
    assert to_kwargs(AdjustmentMode.RAW, "akshare") == {"adjust": ""}
    assert to_kwargs(AdjustmentMode.QFQ, "akshare") == {"adjust": "qfq"}
    assert to_kwargs(AdjustmentMode.HFQ, "akshare") == {"adjust": "hfq"}
    assert to_kwargs(AdjustmentMode.RAW, "efinance") == {"fqt": 0}
    assert to_kwargs(AdjustmentMode.QFQ, "efinance") == {"fqt": 1}
    assert to_kwargs(AdjustmentMode.HFQ, "baostock") == {"adjustflag": "3"}
    assert to_kwargs(AdjustmentMode.QFQ, "baostock") == {"adjustflag": "2"}
    assert to_kwargs(AdjustmentMode.RAW, "baostock") == {"adjustflag": "1"}
    assert to_kwargs(AdjustmentMode.QFQ, "adata") == {"adjust_type": 1}


def test_to_kwargs_rejects_non_raw_on_no_param_libs() -> None:
    """无参库（mootdx/tdxpy）只接受 RAW；非 RAW 必抛 UnknownAdjustment。"""
    # RAW 合法 → 空 dict（无实参可加）
    assert to_kwargs(AdjustmentMode.RAW, "mootdx") == {}
    assert to_kwargs(AdjustmentMode.RAW, "tdxpy") == {}
    for lib in ("mootdx", "tdxpy"):
        for mode in (AdjustmentMode.QFQ, AdjustmentMode.HFQ):
            with pytest.raises(UnknownAdjustment):
                to_kwargs(mode, lib)


def test_unknown_adjustment_is_value_error() -> None:
    """UnknownAdjustment 是 ValueError 子类（调用方可用 ValueError 兜底捕获）。"""
    assert issubclass(UnknownAdjustment, ValueError)


def test_daily_bar_ohlcv_candidates_pin_adjust_consistently() -> None:
    """③ 表级守卫：`daily_bar` 的 `ohlcv_daily` 候选 adjust 非空且与 pinned 同源。

    ⛔ 这是 R4 "禁止默认调用"的机器化：任何"新增 ohlcv_daily 候选却不给
    adjust 字段" / "adjust 与 pinned 不一致"都必须在本测试红。
    tx（`ohlcv_daily_tx`）与 citydata（`ohlcv_daily_ts`）是**不同 schema**，
    按设计 §3.3 ⛔ 不在本守卫内。
    """
    from finai.sources.adjustment_mode import _KWARG_NAMES, _TABLES

    candidates = capability_router.CAPABILITIES["daily_bar"]
    ohlcv = [c for c in candidates if c.schema == "ohlcv_daily"]
    assert ohlcv, "daily_bar 必须至少有一条 ohlcv_daily 候选"
    for c in ohlcv:
        # ① adjust 字段非空（不给 adjust 的 ohlcv_daily 候选 → 红）
        assert c.adjust is not None, (
            f"候选 {c.key}（schema=ohlcv_daily）缺 adjust 字段 —— R4 禁止默认调用")
        # ② 与 pinned 同源：把 adjust 经 to_kwargs 展开，pinned 里必须有同键同值
        lib = c.key.split("::", 1)[0]
        lib_key = {"akshare": "akshare", "efinance": "efinance",
                   "tdx": "mootdx"}[lib]  # tdx 腿 = mootdx/TDX 协议，无参库
        expected = to_kwargs(c.adjust, lib_key)
        for k, v in expected.items():
            assert _KWARG_NAMES[lib_key] == k
            assert c.pinned.get(k) == v, (
                f"候选 {c.key} 口径漂移：adjust={c.adjust} ⇒ 期望 pinned[{k!r}]={v!r}，"
                f"实际 pinned={c.pinned!r}（⛔ 改了 adjust 忘了 pinned，或反之）")
        # ③ pinned 里也确实有复权实参（无参库除外 —— 它 pinned["adjust"]="" 是契约钉档）
        if _TABLES[lib_key]:
            assert any(k in c.pinned for k in expected), (
                f"候选 {c.key} 的 pinned 未钉复权实参（FINDING-353：ohlcv_daily 必须钉档）")


def test_canonical_kwarg_name() -> None:
    """统一入参名是 'adjustment'（capability 契约层规范名）。"""
    assert ADJUST_KWARG_CANONICAL == "adjustment"
