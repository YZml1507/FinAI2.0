"""_derive_is_resumption 单测：两种停牌形态 + 文件层标记的 OR 语义。

背景：runner 旧实现用缺口推导无条件覆写 is_resumption 列——把文件层
（采集端）按 tradestatus 0→1 标好的复牌行清成 False，导致 D-1 对
"有占位行"形态复牌跳变误伤（sh.600190 57.1% 案）。
"""
from __future__ import annotations

import pandas as pd

from scripts.run_score_basket_backtest import _derive_is_resumption


def _cal(dates):
    return pd.to_datetime(pd.Series(dates)).values.astype("datetime64[D]")


def _frame(dates, statuses, marks=None):
    df = pd.DataFrame({
        "date": pd.to_datetime(pd.Series(dates)).dt.date,
        "close": [10.0] * len(dates),
        "tradestatus": [str(s) for s in statuses],
    })
    if marks is not None:
        df["is_resumption"] = marks
    return df


CAL = _cal(["2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30",
            "2026-07-01"])


def test_placeholder_flip_marks_resumption():
    # 停牌占位行 tradestatus=0 → 复牌行 0→1 翻转 ⇒ True（57.1% 跳变豁免情形）
    df = _frame(
        ["2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30"],
        [1, 0, 0, 1])
    res = _derive_is_resumption(df, CAL)
    assert list(res) == [False, False, False, True]


def test_gap_marks_resumption():
    # 无占位行：06-26 后直接跳到 06-30（指数日历缺口 ≥2 日）⇒ 缺口后首行 True
    df = _frame(["2026-06-26", "2026-06-30", "2026-07-01"], [1, 1, 1])
    res = _derive_is_resumption(df, CAL)
    assert list(res) == [False, True, False]


def test_existing_file_marks_preserved():
    # 文件层标记 OR 保留——不可被缺口推导覆写清掉
    df = _frame(
        ["2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30"],
        [1, 0, 0, 1],
        marks=[False, False, False, True])
    res = _derive_is_resumption(df, CAL)
    assert list(res) == [False, False, False, True]


def test_normal_days_all_false():
    df = _frame(
        ["2026-06-25", "2026-06-26", "2026-06-29"], [1, 1, 1])
    res = _derive_is_resumption(df, CAL)
    assert not res.any()


def test_no_columns_defaults_zero():
    df = pd.DataFrame({
        "date": pd.to_datetime(pd.Series(["2026-06-26"])).dt.date,
        "close": [10.0],
    })
    res = _derive_is_resumption(df, CAL)
    assert not res.any()
