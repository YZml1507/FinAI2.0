#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R1（baostock 停牌脏行过滤）单测 —— 全部**离线**，⛔ 无任何网络调用。

对应 `REVALIDATE.md R1` / `R1_suspension_fix_20260830.md` 的验收判据：
  ① 停牌日（`tradestatus != '1'`，OHLC=前收平推）不在结果集；
  ② 被过滤行数写入 `meta['suspended_rows']` 与 `evidence['suspended_rows']`；
  ③ 无 `tradestatus` 的**日线**调用被拒（raise ValueError，⛔ 不静默 append）；
  ⑤ 干净时段零误杀（全 `tradestatus='1'` 时行数不变、suspended_rows==0）。

⛔⛔ 永不静默原则：断言不被 try/except 吞错。`_query` 走 monkeypatch（不打 baostock 网）。
"""
from __future__ import annotations

import pandas as pd
import pytest

from finai.sources import baostock_source
from finai.sources.base import EMPTY_OK, OK
from finai.sources.adjustment_mode import UnknownAdjustment


def _daily_frame() -> pd.DataFrame:
    """合成一个含 2 条停牌脏行的日线帧（12 号 §9-A-4 实测形状）。

    停牌日：`tradestatus='0'`、`volume=0`、OHLC 全等于前收盘价（平推）。
    正常日：`tradestatus='1'`、`volume>0`。
    """
    return pd.DataFrame({
        "date": ["2025-07-15", "2025-07-16", "2025-07-17", "2025-07-18"],
        "open":  [10.0, 10.5, 10.5, 10.5],
        "high":  [10.6, 10.5, 10.5, 10.5],
        "low":   [9.9,  10.5, 10.5, 10.5],
        "close": [10.5, 10.5, 10.5, 10.9],
        "volume": ["1000", "0", "0", "1200"],
        "tradestatus": ["1", "0", "0", "1"],   # 07-16 / 07-17 停牌（OHLC=前收 10.5 平推）
    })


def _patch_query(monkeypatch: pytest.MonkeyPatch, frame: pd.DataFrame) -> None:
    """把 `_query` 换成返回合成帧的桩 —— ⛔ 离线，绝不真连 baostock。"""
    monkeypatch.setattr(
        baostock_source, "_query", lambda kind, params: frame.copy())


def test_suspended_rows_are_dropped_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """①+② 停牌日被剔除，计数写进 evidence 与 meta。"""
    _patch_query(monkeypatch, _daily_frame())
    res = baostock_source.fetch(
        "kline", code="sh.600710",
        fields="date,open,high,low,close,volume,tradestatus",
        start_date="2025-07-15", end_date="2025-07-18",
        frequency="d", adjustflag="3")

    assert res.state == OK, f"过滤后仍有真实行 ⇒ state 应为 OK，实际 {res.state}"
    assert res.rows == 2, f"4 行剔除 2 条停牌 ⇒ 应剩 2 行，实际 {res.rows}"
    # ① 结果集里不允许再有非 '1' 的 tradestatus
    assert (res.frame["tradestatus"] == "1").all(), "结果集仍含停牌脏行"
    assert "2025-07-16" not in set(res.frame["date"])
    assert "2025-07-17" not in set(res.frame["date"])
    # ② 计数：evidence 与 meta 双写
    assert res.evidence["suspended_rows"] == 2
    assert res.evidence["suspended_warn"] is True
    assert res.meta["suspended_rows"] == 2


def test_clean_window_zero_false_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """⑤ 干净时段（无停牌）零误杀：行数不变、suspended_rows==0、meta 不写键。"""
    clean = _daily_frame()
    clean = clean[clean["tradestatus"] == "1"].reset_index(drop=True)
    _patch_query(monkeypatch, clean)
    res = baostock_source.fetch(
        "kline", code="sh.600000",
        fields="date,open,high,low,close,volume,tradestatus",
        start_date="2025-07-15", end_date="2025-07-18",
        frequency="d", adjustflag="3")

    assert res.state == OK
    assert res.rows == len(clean), "干净窗口被误杀"
    assert res.evidence["suspended_rows"] == 0
    assert res.evidence["suspended_warn"] is False
    # R1 §1-③：n_susp==0 时**不**写 meta 键（保持"有停牌才记"的语义）
    assert "suspended_rows" not in res.meta


def test_daily_without_tradestatus_is_rejected() -> None:
    """③ 日线 fields 缺 `tradestatus` → ValueError（⛔ 不静默 append；离线，不登录）。"""
    with pytest.raises(ValueError, match="tradestatus"):
        baostock_source._validate_kline_params(
            "kline",
            {"code": "sh.600000", "fields": "date,close",
             "start_date": "2025-07-15", "end_date": "2025-07-18",
             "frequency": "d", "adjustflag": "3"})


def test_adjustflag_out_of_table_is_rejected() -> None:
    """R4 §3.4：adjustflag 越界（不在 BAOSTOCK 表内）→ UnknownAdjustment（离线）。"""
    with pytest.raises(UnknownAdjustment):
        baostock_source._validate_kline_params(
            "kline",
            {"code": "sh.600000", "fields": "date,close,tradestatus",
             "start_date": "2025-07-15", "end_date": "2025-07-18",
             "frequency": "d", "adjustflag": "9"})   # '9' 不在 {1,2,3,''} 内


def test_minute_bar_not_suspension_filtered() -> None:
    """R1 §4-1：`tradestatus` 只在日线有效 —— 分钟线不做停牌过滤（直过）。"""
    minute = _daily_frame()  # 形状借用，分钟线无 tradestatus 概念，但这里测过滤不触发
    # 分钟线（frequency='5'）不应进入 tradestatus 强制 —— _drop_suspended 只对 kind=='kline'
    # 但 kind 仍是 'kline'。真正区分在 frequency：分钟线 fields 不含 tradestatus，
    # 故 `_drop_suspended` 因无 tradestatus 列而原样放行。
    out, n = baostock_source._drop_suspended(minute.drop(columns=["tradestatus"]),
                                             kind="kline")
    assert n == 0 and len(out) == len(minute), "无 tradestatus 列时应原样放行"
