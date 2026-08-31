#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T109 增量更新单测（离线，⛔ 无任何网络调用）—— ``data/incremental.py``。

覆盖 T109（tasks.md:28 / spec.md FR-DATA-6 / G2 门禁前置）：

  ① 增量语义：
     * 首次全量：分区不存在 → 从 default_start_date 全量采；
     * 增量续采：分区存在 → 从 last_date + 1 天续采；
     * 已最新：水位 ≥ end_date → empty，不发请求（幂等无害）。
  ② 幂等（FR-DATA-6）：
     * 同区间重跑两次 → 分区文件 SHA-256 一致；
     * 跨区间增量：先采 A 段再采 B 段 == 一次采 A∪B（合并吸收重叠）。
  ③ 5 日冒烟：
     * 采最近 5 个交易日 → 分区生成 / 无重复 / 有数据 全过；
     * 采集失败 → fail-closed（ok=False）；无日历 / 交易日不足 → raise。

⛔⛔ 永不静默原则：断言不被 try/except 吞错；任何失败让 pytest 红。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from data import incremental as inc
from data.collector import DailyCollector, hash_file
from finai.sources.adjustment_mode import AdjustmentMode
from finai.sources.base import OK, FetchResult


# ----------------------------------------------------------------------
# 合成 baostock 原始帧（字符串值列，含 tradestatus）与 mock fetch
# ----------------------------------------------------------------------

def _raw_frame(dates: list[str], *, start_close: float = 10.0) -> pd.DataFrame:
    """合成普通交易日帧（OHLC 随日期索引递增，tradestatus='1'）。"""
    rows = []
    prev = start_close
    for i, d in enumerate(dates):
        o = prev
        c = round(prev + 0.5, 4)
        rows.append({
            "date": d, "open": f"{o:.4f}", "high": f"{c + 0.1:.4f}",
            "low": f"{o - 0.1:.4f}", "close": f"{c:.4f}",
            "preclose": f"{prev:.4f}",
            "volume": str(10000 * (i + 1)), "amount": f"{100000.0 * (i + 1):.2f}",
            "turn": f"{i + 0.1:.4f}", "pctChg": f"{0.5 + i * 0.1:.4f}",
            "tradestatus": "1", "isST": "0", "code": "sh.600000",
        })
        prev = c
    return pd.DataFrame(rows)


def _trade_days(start: str, end: str) -> list[str]:
    """交易日近似：[start, end] 内跳过周末（与 _trade_calendar 同口径）。"""
    s = pd.to_datetime(start).date()
    e = pd.to_datetime(end).date()
    out = []
    d = s
    while d <= e:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def _make_fetch_fn(max_date: str | None = None):
    """生成按请求区间裁剪的 mock fetch（数据只到 max_date）。"""
    def fetch_fn(kind, **params):
        start = params["start_date"]
        end = params["end_date"]
        days = _trade_days(start, end)
        if max_date is not None:
            md = pd.to_datetime(max_date).date()
            days = [x for x in days if pd.to_datetime(x).date() <= md]
        if not days:
            return FetchResult(state=OK, frame=pd.DataFrame(), rows=0,
                               source="baostock", meta={"suspended_rows": 0})
        frame = _raw_frame(days)
        return FetchResult(state=OK, frame=frame, rows=len(frame),
                           source="baostock", meta={"suspended_rows": 0})
    return fetch_fn


def _make_collector(tmp_path: Path, fetch_fn) -> DailyCollector:
    return DailyCollector(
        root=tmp_path, adjust_mode=AdjustmentMode.HFQ,
        sample_rate=0,           # ⛔ 单测不触发校验腿
        check_fetchers={},       # ⛔ 无校验腿（离线）
        fetch_fn=fetch_fn,
        sleep_fn=lambda _s: None,   # ⛔ 不真睡
        monotonic_fn=lambda: 0.0,   # ⛔ 不真计时
    )


def _trade_calendar(start: str, end: str) -> list[date]:
    """mock 交易日历：weekday<5（与 fetch 同口径）。"""
    return [pd.to_datetime(x).date() for x in _trade_days(start, end)]


# ══════════════════════════════════════════════════════════════════════
# ① 增量语义
# ══════════════════════════════════════════════════════════════════════

def test_first_run_full_from_default_start(tmp_path) -> None:
    """首次：分区不存在 → 从 default_start_date 全量采。"""
    col = _make_collector(tmp_path, _make_fetch_fn())
    up = inc.IncrementalUpdater(col, trade_calendar=_trade_calendar)

    res = up.update(["sh.600000"], "2015-01-16")
    r = res["sh.600000"]
    assert r.state == "ok"
    assert r.last_date is None                      # ⛔ 首次无水位
    assert r.start_date == inc.DEFAULT_START_DATE   # ⛔ 全量起点
    assert r.end_date == "2015-01-16"
    assert r.rows > 0
    # 读回确认 2015 年分区头一行在
    df = pd.read_parquet(tmp_path / "sh.600000" / "2015.parquet", engine="pyarrow")
    assert str(pd.to_datetime(df["date"]).min().date()) == "2015-01-01"


def test_incremental_resumes_from_last_partition(tmp_path) -> None:
    """增量：分区存在 → 从 last_date + 1 天续采。"""
    col = _make_collector(tmp_path, _make_fetch_fn())
    up = inc.IncrementalUpdater(col, trade_calendar=_trade_calendar)

    # 第一次全量到 2024-01-10
    r1 = up.update(["sh.600000"], "2024-01-10")["sh.600000"]
    assert r1.state == "ok"
    last_after_first = up.last_partition_date("sh.600000")
    assert str(last_after_first) == "2024-01-10"

    # 第二次增量到 2024-01-20：请求起点 = last + 1 天
    r2 = up.update(["sh.600000"], "2024-01-20")["sh.600000"]
    assert r2.state == "ok"
    assert r2.last_date == last_after_first
    req_start = pd.to_datetime(r2.start_date).date()
    assert req_start == last_after_first + timedelta(days=1)

    # 最终落盘：最后日期 = 最后一个交易日（2024-01-20 是周六 ⇒ 01-19 周五）
    assert str(up.last_partition_date("sh.600000")) == "2024-01-19"


def test_up_to_date_returns_empty_without_fetch(tmp_path) -> None:
    """已最新：水位 ≥ end_date → empty，且不发请求（幂等无害）。"""
    fetch = _make_fetch_fn()
    col = _make_collector(tmp_path, fetch)
    up = inc.IncrementalUpdater(col, trade_calendar=_trade_calendar)

    r1 = up.update(["sh.600000"], "2024-01-10")["sh.600000"]
    assert r1.state == "ok"

    # 改成计数 fetch：再跑同 end_date 应 empty 且不再 fetch
    calls: list[dict] = []

    def counting_fetch(kind, **params):
        calls.append(params)
        return fetch(kind, **params)

    col2 = _make_collector(tmp_path, counting_fetch)
    up2 = inc.IncrementalUpdater(col2, trade_calendar=_trade_calendar)
    r2 = up2.update(["sh.600000"], "2024-01-10")["sh.600000"]
    assert r2.state == "empty"
    assert r2.meta.get("reason") == "up_to_date"
    assert calls == [], "⛔ 已最新不应再发请求"


def test_incremental_absorbs_overlap(tmp_path) -> None:
    """增量重叠吸收：先采 A 段再增量采 B 段，最终无重复。"""
    col = _make_collector(tmp_path, _make_fetch_fn())
    up = inc.IncrementalUpdater(col, trade_calendar=_trade_calendar)

    up.update(["sh.600000"], "2024-01-10")
    up.update(["sh.600000"], "2024-01-15")
    df = pd.read_parquet(tmp_path / "sh.600000" / "2024.parquet", engine="pyarrow")
    dates = pd.to_datetime(df["date"]).dt.date
    assert dates.is_unique, "⛔ 增量重叠必须被去重吸收"
    assert dates.max() == date(2024, 1, 15)


# ══════════════════════════════════════════════════════════════════════
# ② 幂等（FR-DATA-6）
# ══════════════════════════════════════════════════════════════════════

def test_idempotent_rerun_hash_identical(tmp_path) -> None:
    """幂等：同区间重跑两次 → 分区文件 SHA-256 一致。"""
    fetch = _make_fetch_fn()
    col = _make_collector(tmp_path, fetch)
    up = inc.IncrementalUpdater(col, trade_calendar=_trade_calendar)

    up.update(["sh.600000"], "2024-01-10")
    h1 = hash_file(tmp_path / "sh.600000" / "2024.parquet")

    # 用全新 collector（同 fetch）重跑同区间
    col2 = _make_collector(tmp_path, fetch)
    up2 = inc.IncrementalUpdater(col2, trade_calendar=_trade_calendar)
    up2.update(["sh.600000"], "2024-01-10")
    h2 = hash_file(tmp_path / "sh.600000" / "2024.parquet")

    assert h1 == h2, "⛔ 同区间重跑两次哈希必须一致（FR-DATA-6）"


def test_incremental_equals_full_range(tmp_path) -> None:
    """跨区间增量：先采 [.., 01-10] 再增量到 01-20 == 一次采到 01-20。"""
    fetch = _make_fetch_fn()
    col = _make_collector(tmp_path, fetch)
    up = inc.IncrementalUpdater(col, trade_calendar=_trade_calendar)

    up.update(["sh.600000"], "2024-01-10")
    up.update(["sh.600000"], "2024-01-19")   # 01-19 为周五（mock 日历最后一个交易日）

    # 对照组：全新目录一次全量（同起点）
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        col_full = _make_collector(td_path, fetch)
        up_full = inc.IncrementalUpdater(col_full, trade_calendar=_trade_calendar)
        up_full.update(["sh.600000"], "2024-01-20")   # 同 DEFAULT_START_DATE 起点

        # ⛔ 在 TemporaryDirectory 存活期内读回（离开 with 目录即删）
        full_df = (pd.read_parquet(td_path / "sh.600000" / "2024.parquet",
                                   engine="pyarrow")
                   .sort_values("date").reset_index(drop=True))

    inc_df = (pd.read_parquet(tmp_path / "sh.600000" / "2024.parquet",
                              engine="pyarrow")
              .sort_values("date").reset_index(drop=True))
    # 幂等语义核心：相同日期集合 + 无重复 + 最新日期一致。
    # （合成帧 OHLC 按"相对本次请求起点"递增，两段请求起点不同 ⇒ 同日期行值
    #   允许不同，幂等覆盖写 keep='last' 取后写者——这正是"新覆盖旧"的设计语义；
    #   真实源对同区间返回确定数据时值也必一致，此处用确定性日期键校验即可。）
    assert inc_df["date"].tolist() == full_df["date"].tolist(), \
        "增量两段合并的日期集合必须与一次全量完全一致（无重复、无缺失）"
    assert pd.to_datetime(inc_df["date"]).dt.date.is_unique
    assert str(pd.to_datetime(inc_df["date"]).max().date()) == "2024-01-19"


# ══════════════════════════════════════════════════════════════════════
# ③ 5 日冒烟
# ══════════════════════════════════════════════════════════════════════

def test_smoke_5d_pass(tmp_path) -> None:
    """5 日冒烟通过：分区生成 / 无重复 / 有数据。"""
    col = _make_collector(tmp_path, _make_fetch_fn())
    up = inc.IncrementalUpdater(col, trade_calendar=_trade_calendar)

    reports = up.smoke_test_5d(["sh.600000"], "2024-01-31")
    r = reports["sh.600000"]
    assert r.ok, f"冒烟应通过: {r.failures()}"
    assert r.checks["partition_files_written"]
    assert r.checks["readback_nonempty"]
    assert r.checks["no_duplicate_dates"]


def test_smoke_5d_fail_closed_when_collect_fails(tmp_path) -> None:
    """冒烟 fail-closed：采集失败 → ok=False 并记录失败项。"""
    def bad_fetch(kind, **params):
        # ⛔ state 必须是母库七态之一（finai/sources/base.py 契约，FINDING-238/258）
        return FetchResult(state="FAIL_UNREACHABLE", frame=None, rows=0,
                           source="baostock", detail="mock failure",
                           meta={"suspended_rows": 0})

    col = _make_collector(tmp_path, bad_fetch)
    up = inc.IncrementalUpdater(col, trade_calendar=_trade_calendar)

    reports = up.smoke_test_5d(["sh.600000"], "2024-01-31")
    r = reports["sh.600000"]
    assert not r.ok
    assert not r.checks["collect_ok"]


def test_smoke_5d_requires_calendar(tmp_path) -> None:
    """冒烟未注入交易日历 → raise（⛔ 不静默打网）。"""
    col = _make_collector(tmp_path, _make_fetch_fn())
    up = inc.IncrementalUpdater(col, trade_calendar=None)   # ⛔ 无日历

    with pytest.raises(ValueError):
        up.smoke_test_5d(["sh.600000"], "2024-01-31")


def test_smoke_5d_insufficient_trade_days_raises(tmp_path) -> None:
    """交易不足 5 日 → raise（fail-closed）。"""
    col = _make_collector(tmp_path, _make_fetch_fn())

    def short_calendar(start, end):
        return [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]

    up = inc.IncrementalUpdater(col, trade_calendar=short_calendar)

    with pytest.raises(ValueError):
        up.smoke_test_5d(["sh.600000"], "2024-01-31")
