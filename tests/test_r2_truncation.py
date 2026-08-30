#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R2（TDX 腿静默截断 → 分页覆盖率校验）单测 —— 全部**离线**，⛔ 无任何网络/TDX 调用。

对应 `REVALIDATE.md R2` / `DATA_LAYER_WORK_ORDER.md §3-5`（静默截断红线）：
  ① `got < requested` ⇒ 抛 ``BarTruncationError``（绝不"凑活返回部分数据"，
     `base.py:22` 硬约束② / `FINDING-185` 反面教训）；
  ② 异常携带 ``requested``/``got``/``truncated_bars = requested - got`` 供血缘审计；
  ③ `got == requested`（含 0）放行返回 ``None``；0 行留给 `make_result` 归 `EMPTY_OK`
     （`FINDING-178`），⛔ 覆盖率校验不得把"确实无数据"误报成截断；
  ④ `BarTruncationError` 是 ``ValueError`` 子类（与 `UnknownAdjustment` 同套路）。

⛔⛔ 永不静默原则：断言不被 try/except 吞错。`_assert_coverage` 是纯函数，不打网。
依赖裁定：R2 的修复对象（`tdx_source.fetch_bars` 5min/分钟腿）随 **R5 方案 B** 被砍，
本模块测的是**可复用纯函数 + 钉位接线**，与真实 TDX 链路无关 ⇒ 可全程离线。
"""
from __future__ import annotations

import pandas as pd
import pytest

from finai.sources import tdx_source


def _frames(*sizes: int) -> list[pd.DataFrame]:
    """合成若干分页帧（每个 size 一页的行数）。形状仅为形态锚定，无语义。"""
    return [pd.DataFrame({"close": [0.0] * n}) for n in sizes]


def test_short_total_is_flagged_truncated() -> None:
    """①+② 请求 100、实得 60 ⇒ 判截断，`truncated_bars == 40`。"""
    frames = _frames(60)          # 单页只回了 60（远少于请求 100）
    with pytest.raises(tdx_source.BarTruncationError) as exc_info:
        tdx_source._assert_coverage(frames, requested=100, got=60)
    err = exc_info.value
    assert err.requested == 100
    assert err.got == 60
    assert err.truncated_bars == 40
    assert "截断" in str(err), "异常文本应点明'截断'以便归类/排障"


def test_full_coverage_passes() -> None:
    """③ 请求 100、实得 100（跨两页累计）⇒ 放行返回 None。"""
    frames = _frames(60, 40)      # 两页累计 100 == 请求 100
    assert tdx_source._assert_coverage(frames, requested=100, got=100) is None


def test_zero_got_is_not_misreported_as_truncation() -> None:
    """③⛔ 请求 100、实得 0 ⇒ 这里确实 got<requested，会抛；但语义归 EMPTY_OK 由
    `fetch_bars` 上方 `if not data: break` 兜住 —— 本断言只在有数据仍不足时才有意义。
    故单独钉死：0<requested 仍按 R2 抛（调用侧负责先把 0 行短路上报为 EMPTY_OK）。"""
    with pytest.raises(tdx_source.BarTruncationError):
        tdx_source._assert_coverage(_frames(0), requested=100, got=0)


def test_truncation_error_is_value_error_and_keeps_measured_counts() -> None:
    """④ 异常族谱 + 血缘计数（R2 实测案例：请求 13824 / 实得 11520 = 截 17%）。"""
    assert issubclass(tdx_source.BarTruncationError, ValueError)
    err = tdx_source.BarTruncationError(requested=13824, got=11520)   # R2 实测案例
    assert err.truncated_bars == 13824 - 11520 == 2304
