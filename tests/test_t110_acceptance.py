#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T110 数据层验收单测（离线，⛔ 无任何网络调用）—— ``data/acceptance.py``。

覆盖 T110 三把验收尺（tasks.md:29 / spec.md FR-DATA-1/2/6 / G2 门禁）：

  ① 三源抽样比对（``validate``）：
     * 三源一致（偏差 ≤0.2%）→ report.ok；
     * 跨源分歧超限（>0.2%）→ exceed + report 非 ok；
     * 新浪可校 OHLC/volume/amount、腾讯只校 OHLC（复用 collector 登记表）；
     * 2015 年之前的行不参与偏差比较（spec「2015 年后 <0.2pp」）；
     * 主源行内缺值 → null_issues + 非 ok；
     * 主源取数非 OK（EMPTY_OK/FAIL_*）→ fetch_errors + 非 ok（⛔ 不静默跳过）；
     * 抽样确定性：sample_size 只取前 N（可复现，不用随机数）。
  ② 停牌命中 100%（``check_suspension_hit``）：
     * 已知停牌日全部被过滤 → 100% 命中；
     * 漏检（停牌日残留于过滤后输出）→ hit_rate <1.0 且 非 ok；
     * 主源取数失败 / 缺 tradestatus 列 → fetch_errors + 非 ok（fail-closed）。
  ③ 幂等哈希一致（``check_idempotency``）：
     * 同区间重跑两次分区文件 SHA-256 一致（FR-DATA-6）；
     * 第一次运行失败 → raise（⛔ 无法验收不得静默）；
     * 校验腿取数失败只记账不阻断（同 collector 精神）。

⛔⛔ 永不静默原则：断言不被 try/except 吞错；任何失败让 pytest 红。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from data import acceptance as acc
from data.acceptance import (
    FieldDeviation,
    IdempotencyReport,
    PrimaryFetchError,
    SuspensionHit,
    SuspensionReport,
    ThreeSourceReport,
    ThreeSourceValidator,
)
from data.collector import CHECK_TOL
from finai.sources.adjustment_mode import AdjustmentMode
from finai.sources.base import EMPTY_OK, FAIL_GATEWAY, FetchResult, OK


# ----------------------------------------------------------------------
# 合成帧（baostock 字符串形态 / 新浪中文列 / 腾讯英文列）
# ----------------------------------------------------------------------

def _raw_daily_frame(n: int = 4, start: str = "2024-01-02",
                     *, suspended_dates: tuple[str, ...] = ()) -> pd.DataFrame:
    """合成 baostock 原始日线帧（字符串值列）。

    ``suspended_dates``：模拟停牌脏行（tradestatus='0'、OHLC=前收平推、volume=0
    —— 12 号 §9-A 实测 100% 命中的签名）。
    """
    base = pd.to_datetime(start)
    dates = [(base + pd.Timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]
    rows = []
    prev_close = "10.0"
    for i, d in enumerate(dates):
        if d in suspended_dates:
            # 停牌行：OHLC 全部 = 前收（平推）、volume=0、tradestatus='0'
            rows.append({
                "date": d, "open": prev_close, "high": prev_close,
                "low": prev_close, "close": prev_close, "preclose": prev_close,
                "volume": "0", "amount": "0.0", "turn": "0.0", "pctChg": "0.0",
                "tradestatus": "0", "isST": "0", "code": "sh.600000",
            })
            continue
        o = f"{10.0 + i * 0.5:.4f}"
        h = f"{10.5 + i * 0.5:.4f}"
        low = f"{9.5 + i * 0.5:.4f}"
        c = f"{10.2 + i * 0.5:.4f}"
        rows.append({
            "date": d, "open": o, "high": h, "low": low, "close": c,
            "preclose": prev_close,
            "volume": f"{10000 * (i + 1)}", "amount": f"{100000.0 * (i + 1):.2f}",
            "turn": f"{i + 0.1:.4f}", "pctChg": f"{0.5 + i * 0.1:.4f}",
            "tradestatus": "1", "isST": "0", "code": "sh.600000",
        })
        prev_close = c
    return pd.DataFrame(rows)


def _ok_result(frame: pd.DataFrame, **meta_extra) -> FetchResult:
    meta = {"suspended_rows": 0}
    meta.update(meta_extra)
    return FetchResult(state=OK, frame=frame, rows=len(frame),
                       source="baostock", meta=meta)


def _make_sina_frame(dates: list[str], *, close: float | None = None) -> pd.DataFrame:
    """新浪校验腿形状（中文列名，2 位精度价格）。close=None 时与主源一致。"""
    n = len(dates)
    closes = ([10.2 + i * 0.5 for i in range(n)] if close is None
              else [close] * n)
    return pd.DataFrame({
        "日期": dates,
        "开盘": [10.0 + i * 0.5 for i in range(n)],
        "最高": [10.5 + i * 0.5 for i in range(n)],
        "最低": [9.5 + i * 0.5 for i in range(n)],
        "收盘": closes,
        "成交量": [10000 * (i + 1) for i in range(n)],
        "成交额": [100000.0 * (i + 1) for i in range(n)],
    })


def _make_tencent_frame(dates: list[str], *, close: float | None = None) -> pd.DataFrame:
    """腾讯校验腿形状（英文列名，仅 6 列；amount 实为成交量，⛔ 不校 amount）。"""
    n = len(dates)
    closes = ([10.2 + i * 0.5 for i in range(n)] if close is None
              else [close] * n)
    return pd.DataFrame({
        "date": dates,
        "open": [10.0 + i * 0.5 for i in range(n)],
        "high": [10.5 + i * 0.5 for i in range(n)],
        "low": [9.5 + i * 0.5 for i in range(n)],
        "close": closes,
        "volume": [10000 * (i + 1) for i in range(n)],
        "amount": [10000 * (i + 1) for i in range(n)],
    })


DATES4 = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]


# ══════════════════════════════════════════════════════════════════════
# ① 三源抽样比对（FR-DATA-1）
# ══════════════════════════════════════════════════════════════════════

def test_validate_three_sources_agree_within_tol(tmp_path) -> None:
    """三源一致：新浪/腾讯各字段偏差 ≤0.2% → report.ok，无超阈偏差。"""
    def fetch_fn(kind, **params):
        return _ok_result(_raw_daily_frame(4))

    v = ThreeSourceValidator(
        root=tmp_path, fetch_fn=fetch_fn,
        check_fetchers={
            "sina": lambda s, st, ed, m: _make_sina_frame(DATES4),
            "tencent": lambda s, st, ed, m: _make_tencent_frame(DATES4),
        })
    rep = v.validate(["sh.600000"], "2024-01-02", "2024-01-05")
    assert rep.ok, f"三源一致应通过：{rep.failures()}"
    assert rep.deviations, "应有偏差记录（含未超阈的）"
    assert not any(d.exceed for d in rep.deviations)
    # 偏差应为 0（合成数据完全一致）
    assert all(d.max_rel_dev == 0.0 for d in rep.deviations)


def test_validate_sina_drift_exceeds_threshold(tmp_path) -> None:
    """跨源分歧超限：新浪 close 偏差 >0.2% → exceed + report 非 ok。"""
    def fetch_fn(kind, **params):
        return _ok_result(_raw_daily_frame(4))

    # 新浪 close 全部抬高 5% → 相对偏差 0.05 > 0.002
    sdf = _make_sina_frame(DATES4)
    sdf["收盘"] = [c * 1.05 for c in sdf["收盘"]]
    v = ThreeSourceValidator(
        root=tmp_path, fetch_fn=fetch_fn,
        check_fetchers={
            "sina": lambda s, st, ed, m: sdf,
            "tencent": lambda s, st, ed, m: _make_tencent_frame(DATES4),
        })
    rep = v.validate(["sh.600000"], "2024-01-02", "2024-01-05")
    assert not rep.ok
    ex = [d for d in rep.deviations if d.exceed]
    assert ex, "close 偏差 5% 必须判超阈"
    close_dev = [d for d in ex if d.field == "close" and d.source == "sina"]
    assert close_dev and close_dev[0].max_rel_dev == pytest.approx(0.05, rel=1e-3)
    # 腾讯未动 ⇒ 腾讯 close 不超阈
    tencent_close = [d for d in rep.deviations
                     if d.field == "close" and d.source == "tencent"]
    assert tencent_close and not tencent_close[0].exceed


def test_validate_tencent_drift_exceeds_threshold(tmp_path) -> None:
    """腾讯 OHLC 偏差超限 → exceed；且腾讯只校 OHLC（amount 漂移不产生记录）。"""
    def fetch_fn(kind, **params):
        return _ok_result(_raw_daily_frame(4))

    tdf = _make_tencent_frame(DATES4)
    tdf["close"] = [c * 0.9 for c in tdf["close"]]   # -10% 偏差
    tdf["amount"] = [999999.0] * len(tdf)            # ⛔ amount 不在腾讯校验范围
    v = ThreeSourceValidator(
        root=tmp_path, fetch_fn=fetch_fn,
        check_fetchers={
            "sina": lambda s, st, ed, m: _make_sina_frame(DATES4),
            "tencent": lambda s, st, ed, m: tdf,
        })
    rep = v.validate(["sh.600000"], "2024-01-02", "2024-01-05")
    assert not rep.ok
    fields_measured = {d.field for d in rep.deviations if d.source == "tencent"}
    assert fields_measured == {"open", "high", "low", "close"}, \
        f"腾讯只应校 OHLC，实得 {fields_measured}"
    close_dev = [d for d in rep.deviations
                 if d.field == "close" and d.source == "tencent"]
    assert close_dev and close_dev[0].exceed


def test_validate_pre_2015_rows_not_compared(tmp_path) -> None:
    """2015 年之前的行不参与偏差比较（spec「2015 年后 <0.2pp 阈值」）。"""
    old_dates = ["2014-06-02", "2014-06-03"]

    def fetch_fn(kind, **params):
        return _ok_result(_raw_daily_frame(2, start="2014-06-02"))

    # 新浪 close 偏差 50% —— 但全在 2015 年前 ⇒ 不应产生任何偏差记录
    sdf = _make_sina_frame(old_dates, close=15.0)
    v = ThreeSourceValidator(
        root=tmp_path, fetch_fn=fetch_fn,
        check_fetchers={"sina": lambda s, st, ed, m: sdf})
    rep = v.validate(["sh.600000"], "2014-06-02", "2014-06-03")
    assert rep.deviations == [], \
        "2015 年前的行不应参与偏差比较（spec FR-DATA-1 阈值适用 2015 年后）"
    assert rep.ok


def test_validate_null_fields_reported(tmp_path) -> None:
    """主源行内缺值（FR-DATA-1「行内无缺值」）→ null_issues + 非 ok。"""
    def fetch_fn(kind, **params):
        frame = _raw_daily_frame(4)
        frame.loc[1, "close"] = ""      # baostock 偶发空串 → to_numeric 成 NaN
        frame.loc[2, "volume"] = ""
        return _ok_result(frame)

    v = ThreeSourceValidator(
        root=tmp_path, fetch_fn=fetch_fn,
        check_fetchers={"sina": lambda s, st, ed, m: _make_sina_frame(DATES4)})
    rep = v.validate(["sh.600000"], "2024-01-02", "2024-01-05")
    assert not rep.ok
    nulls = {(n.field, n.null_count) for n in rep.null_issues}
    assert ("close", 1) in nulls and ("volume", 1) in nulls


def test_validate_primary_fetch_failure_recorded(tmp_path) -> None:
    """主源取数非 OK（FAIL_GATEWAY / EMPTY_OK）→ fetch_errors + 非 ok。"""
    def fail_fn(kind, **params):
        return FetchResult(state=FAIL_GATEWAY, frame=None, rows=0,
                           source="baostock", detail="502")

    def empty_fn(kind, **params):
        return FetchResult(state=EMPTY_OK, frame=None, rows=0, source="baostock")

    for fn, name in ((fail_fn, "FAIL_GATEWAY"), (empty_fn, "EMPTY_OK")):
        v = ThreeSourceValidator(
            root=tmp_path, fetch_fn=fn,
            check_fetchers={"sina": lambda s, st, ed, m: _make_sina_frame(DATES4)})
        rep = v.validate(["sh.600000"], "2024-01-02", "2024-01-05")
        assert not rep.ok, f"{name} 应使验收不通过"
        assert rep.fetch_errors, f"{name} 应记入 fetch_errors"
        assert rep.deviations == []


def test_validate_check_leg_failure_recorded_not_raised(tmp_path) -> None:
    """校验腿取数失败只记账不 raise（同 collector：主源为准，校验失败不阻断）。"""
    def fetch_fn(kind, **params):
        return _ok_result(_raw_daily_frame(4))

    def broken_leg(symbol, start, end, mode):
        raise ConnectionError("新浪 403")

    v = ThreeSourceValidator(
        root=tmp_path, fetch_fn=fetch_fn,
        check_fetchers={"sina": broken_leg,
                        "tencent": lambda s, st, ed, m: _make_tencent_frame(DATES4)})
    rep = v.validate(["sh.600000"], "2024-01-02", "2024-01-05")  # 不应 raise
    assert any("sina" in e for e in rep.fetch_errors)
    # 腾讯正常 ⇒ 腾讯的偏差记录照常存在
    assert any(d.source == "tencent" for d in rep.deviations)
    # ⚠ 校验腿失败使 report.ok=False —— 验收裁判要求三源齐备才可判「一致」
    assert not rep.ok


def test_validate_sampling_deterministic_first_n(tmp_path) -> None:
    """抽样确定性：sample_size=N 只取前 N 只（⛔ 不用随机数 ⇒ 可复现）。"""
    def fetch_fn(kind, **params):
        return _ok_result(_raw_daily_frame(4))

    v = ThreeSourceValidator(
        root=tmp_path, fetch_fn=fetch_fn,
        check_fetchers={"sina": lambda s, st, ed, m: _make_sina_frame(DATES4)})
    symbols = [f"sh.60000{i}" for i in range(20)]
    rep = v.validate(symbols, "2024-01-02", "2024-01-05", sample_size=5)
    assert rep.total_symbols == 20 and rep.sampled_symbols == 5
    assert {d.symbol for d in rep.deviations} == set(symbols[:5])


# ══════════════════════════════════════════════════════════════════════
# ② 停牌命中 100%（FR-DATA-2 / R1）
# ══════════════════════════════════════════════════════════════════════

def test_suspension_all_known_dates_filtered(tmp_path) -> None:
    """已知停牌日全部被过滤 → 100% 命中（hit_rate=1.0, ok=True）。"""
    susp_dates = ("2024-01-03", "2024-01-05")

    def fetch_fn(kind, **params):
        return _ok_result(_raw_daily_frame(5, suspended_dates=susp_dates),
                          suspended_rows=2)

    v = ThreeSourceValidator(root=tmp_path, fetch_fn=fetch_fn, check_fetchers={})
    rep = v.check_suspension_hit(
        ["sh.600000"], "2024-01-02", "2024-01-06",
        known_suspensions={"sh.600000": list(susp_dates)})
    assert rep.ok, f"已知停牌日应全部被滤：{[h.date for h in rep.missed]}"
    assert rep.hit_rate == 1.0 and rep.total_known == 2
    assert all(h.filtered for h in rep.hits)


def test_suspension_missed_when_dirty_row_survives(tmp_path) -> None:
    """漏检：停牌脏行（tradestatus='0'）未被过滤残留在输出 → hit_rate <1.0、非 ok。"""
    susp_dates = ("2024-01-03", "2024-01-05")

    def fetch_fn(kind, **params):
        # ⚠ 模拟「主源未做 R1 过滤」的坏帧：fetch 直接返回含停牌行且 state=OK。
        # _build_bars 不做停牌过滤（那是 fetch 的职责）⇒ 停牌行残留在 bars 里，
        # enforce_tradestatus 兜底过滤后不该有残留 —— 为制造漏检，把坏帧的
        # tradestatus 改成 '1'（伪装成交易日行）。
        frame = _raw_daily_frame(5, suspended_dates=susp_dates)
        mask = frame["date"].isin(susp_dates)
        frame.loc[mask, "tradestatus"] = "1"     # 停牌行被伪装成正常交易行
        return _ok_result(frame, suspended_rows=0)

    v = ThreeSourceValidator(root=tmp_path, fetch_fn=fetch_fn, check_fetchers={})
    rep = v.check_suspension_hit(
        ["sh.600000"], "2024-01-02", "2024-01-06",
        known_suspensions={"sh.600000": list(susp_dates)})
    assert not rep.ok, "伪装成交易日的停牌行应判漏检"
    missed_dates = {h.date for h in rep.missed}
    assert missed_dates == set(susp_dates)
    assert rep.hit_rate == 0.0 and rep.total_known == 2


def test_suspension_missing_tradestatus_fail_closed(tmp_path) -> None:
    """主源帧缺 tradestatus 列 → enforce_tradestatus raise（fail-closed）→
    记入 fetch_errors、report 非 ok（⛔ 无法验收不得静默跳过）。"""
    def fetch_fn(kind, **params):
        frame = _raw_daily_frame(4).drop(columns=["tradestatus"])
        return _ok_result(frame)

    v = ThreeSourceValidator(root=tmp_path, fetch_fn=fetch_fn, check_fetchers={})
    rep = v.check_suspension_hit(
        ["sh.600000"], "2024-01-02", "2024-01-05",
        known_suspensions={"sh.600000": ["2024-01-03"]})
    assert not rep.ok
    assert rep.fetch_errors and "tradestatus" in rep.fetch_errors[0]
    # 无结论 ≠ 命中：hits 为空、total_known 为 0
    assert rep.hits == [] and rep.total_known == 0


def test_suspension_partial_hit(tmp_path) -> None:
    """部分命中：2 个已知停牌日滤掉 1 个 → hit_rate=0.5、非 ok。"""
    def fetch_fn(kind, **params):
        frame = _raw_daily_frame(5, suspended_dates=("2024-01-03", "2024-01-05"))
        # 只把 01-03 伪装成交易日行；01-05 保持 tradestatus='0' 会被滤掉
        frame.loc[frame["date"] == "2024-01-03", "tradestatus"] = "1"
        return _ok_result(frame)

    v = ThreeSourceValidator(root=tmp_path, fetch_fn=fetch_fn, check_fetchers={})
    rep = v.check_suspension_hit(
        ["sh.600000"], "2024-01-02", "2024-01-06",
        known_suspensions={"sh.600000": ["2024-01-03", "2024-01-05"]})
    assert not rep.ok
    assert rep.hit_rate == pytest.approx(0.5)
    assert {h.date for h in rep.missed} == {"2024-01-03"}


def test_suspension_symbol_not_in_symbols_ignored(tmp_path) -> None:
    """真值里有、但 symbols 未包含的 symbol → 该真值不参与本次验收。"""
    def fetch_fn(kind, **params):
        return _ok_result(_raw_daily_frame(4))

    v = ThreeSourceValidator(root=tmp_path, fetch_fn=fetch_fn, check_fetchers={})
    rep = v.check_suspension_hit(
        ["sh.600000"], "2024-01-02", "2024-01-05",
        known_suspensions={"sz.000001": ["2024-01-03"]})
    assert rep.ok and rep.total_known == 0 and rep.hits == []


# ══════════════════════════════════════════════════════════════════════
# ③ 幂等哈希一致（FR-DATA-6）
# ══════════════════════════════════════════════════════════════════════

def test_idempotency_same_range_rerun_hash_identical(tmp_path) -> None:
    """同区间重跑两次 → 分区文件 hash_file() SHA-256 一致（FR-DATA-6 验收）。"""
    def fetch_fn(kind, **params):
        return _ok_result(_raw_daily_frame(4))

    v = ThreeSourceValidator(root=tmp_path, fetch_fn=fetch_fn, check_fetchers={})
    rep = v.check_idempotency("sh.600000", "2024-01-02", "2024-01-05")
    assert rep.identical, \
        f"同区间重跑哈希必须一致：{rep.first_hash} != {rep.second_hash}"
    assert rep.path.exists() and rep.path.parent == tmp_path / "sh.600000"


def test_idempotency_first_run_failure_raises(tmp_path) -> None:
    """第一次运行失败（state=failed）→ raise PrimaryFetchError（⛔ 不静默）。"""
    def fetch_fn(kind, **params):
        return FetchResult(state=FAIL_GATEWAY, frame=None, rows=0,
                           source="baostock", detail="502")

    v = ThreeSourceValidator(root=tmp_path, fetch_fn=fetch_fn, check_fetchers={})
    with pytest.raises(PrimaryFetchError):
        v.check_idempotency("sh.600000", "2024-01-02", "2024-01-05")


def test_idempotency_multi_year_partitions_stable(tmp_path) -> None:
    """跨年区间：两个分区各自同区间重跑哈希一致（幂等不限于单年）。"""
    def fetch_fn(kind, **params):
        f24 = _raw_daily_frame(4, start="2024-12-30")
        f25 = _raw_daily_frame(3, start="2025-01-02")
        combined = pd.concat([f24, f25], ignore_index=True)
        return _ok_result(combined)

    v = ThreeSourceValidator(root=tmp_path, fetch_fn=fetch_fn, check_fetchers={})
    from data import collector as col
    r1 = col.DailyCollector(root=tmp_path, fetch_fn=fetch_fn).collect_symbol(
        "sh.600000", "2024-12-30", "2025-01-04")
    assert r1.ok and len(r1.paths) == 2
    h24a = col.hash_file(tmp_path / "sh.600000" / "2024.parquet")
    h25a = col.hash_file(tmp_path / "sh.600000" / "2025.parquet")

    rep = v.check_idempotency("sh.600000", "2024-12-30", "2025-01-04")
    assert rep.identical
    assert col.hash_file(tmp_path / "sh.600000" / "2024.parquet") == h24a
    assert col.hash_file(tmp_path / "sh.600000" / "2025.parquet") == h25a


# ══════════════════════════════════════════════════════════════════════
# ④ 结构/常量红线
# ══════════════════════════════════════════════════════════════════════

def test_check_tol_matches_collector_threshold() -> None:
    """阈值 0.2% 与 collector.CHECK_TOL 同源（⛔ 不得另起炉灶改阈值）。"""
    assert acc.DEFAULT_CHECK_TOL == CHECK_TOL == 0.002
    v = ThreeSourceValidator()
    assert v.check_tol == 0.002


def test_validator_default_fetch_fn_is_baostock_primitive() -> None:
    """⛔ 复用母库原语：默认主源取数器就是 baostock_source.fetch。"""
    from finai.sources import baostock_source

    assert ThreeSourceValidator()._fetch_fn is baostock_source.fetch


def test_validator_default_check_fetchers_reuse_collector_registry() -> None:
    """默认校验腿复用 collector 登记表（akshare 懒导入，不重造轮子）。"""
    from data import collector as col

    v = ThreeSourceValidator()
    assert set(v._check_fetchers) == set(col._DEFAULT_CHECK_FETCHERS)


def test_source_fields_registry_matches_collector() -> None:
    """字段登记表与 collector 一致：新浪 OHLC/volume/amount、腾讯只 OHLC。"""
    assert set(acc.SOURCE_FIELDS["sina"]) == {"open", "high", "low", "close",
                                              "volume", "amount"}
    assert set(acc.SOURCE_FIELDS["tencent"]) == {"open", "high", "low", "close"}


def test_field_deviation_exceed_boundary() -> None:
    """超阈判定边界：恰等于阈值不算超（> 才算）。"""
    d_at = FieldDeviation("sh.600000", "sina", "close", 0.002, 0.002)
    d_over = FieldDeviation("sh.600000", "sina", "close", 0.0021, 0.002)
    assert not d_at.exceed and d_over.exceed


def test_report_failures_listing(tmp_path) -> None:
    """报告 failures() 把超阈偏差/缺值/取数失败逐条列出（供上报）。"""
    rep = ThreeSourceReport(
        deviations=[FieldDeviation("sh.600000", "sina", "close", 0.05, 0.002)],
        null_issues=[acc.NullFieldIssue("sh.600000", "volume", 2)],
        fetch_errors=["x [sina] 校验腿取数失败"])
    assert not rep.ok
    lines = rep.failures()
    assert len(lines) == 3
    assert any("0.20%" in ln for ln in lines)
    assert any("volume" in ln for ln in lines)


def test_fetch_primary_bars_raises_on_non_ok(tmp_path) -> None:
    """_fetch_primary_bars 对非 OK 主源显式 raise（⛔ 不静默当无数据）。"""
    def fetch_fn(kind, **params):
        return FetchResult(state=EMPTY_OK, frame=None, rows=0, source="baostock")

    v = ThreeSourceValidator(root=tmp_path, fetch_fn=fetch_fn, check_fetchers={})
    with pytest.raises(PrimaryFetchError):
        v._fetch_primary_bars("sh.600000", "2024-01-02", "2024-01-05")


def test_fetch_primary_bars_passes_explicit_adjustflag(tmp_path) -> None:
    """⛔ 禁止默认复权调用：发往主源的 params 必须带显式 adjustflag（HFQ→'3'）。"""
    seen: dict = {}

    def fetch_fn(kind, **params):
        seen.update(params)
        return _ok_result(_raw_daily_frame(4))

    v = ThreeSourceValidator(root=tmp_path, fetch_fn=fetch_fn, check_fetchers={})
    v._fetch_primary_bars("sh.600000", "2024-01-02", "2024-01-05")
    assert seen["adjustflag"] == "3"
    assert "tradestatus" in seen["fields"]
    assert seen["frequency"] == "d"


# ══════════════════════════════════════════════════════════════════════
# ⑤ G2 门禁聚合视角：三把尺子一起跑（smoke，全离线）
# ══════════════════════════════════════════════════════════════════════

def test_g2_acceptance_full_pass(tmp_path) -> None:
    """G2 冒烟：三把尺（比对/停牌/幂等）全绿 → 三个 report 全 ok。

    停牌日（2024-01-03）被主源 fetch 的 R1 过滤 ⇒ 主源 bars 只剩 4 个交易日；
    校验腿帧按「同 4 个交易日、字段值与主源一致」构造（新浪/腾讯停牌日跳行
    与主源过滤后的日期天然对齐）。
    """
    susp_dates = ("2024-01-03",)
    trade_dates = ["2024-01-02", "2024-01-04", "2024-01-05", "2024-01-06"]

    def fetch_fn(kind, **params):
        return _ok_result(_raw_daily_frame(5, suspended_dates=susp_dates),
                          suspended_rows=1)

    # 校验腿值须与主源过滤后的 bars 对齐：主源第 i 个交易日的 OHLC =
    # (10.0 + j*0.5, 10.5 + j*0.5, 9.5 + j*0.5, 10.2 + j*0.5)，j 为**原始行号**
    # （停牌行不占 j 递增位），故过滤后为 j = 0, 2, 3, 4。
    js = [0, 2, 3, 4]
    def _sina_leg() -> pd.DataFrame:
        return pd.DataFrame({
            "日期": trade_dates,
            "开盘": [10.0 + j * 0.5 for j in js],
            "最高": [10.5 + j * 0.5 for j in js],
            "最低": [9.5 + j * 0.5 for j in js],
            "收盘": [10.2 + j * 0.5 for j in js],
            "成交量": [10000 * (j + 1) for j in js],
            "成交额": [100000.0 * (j + 1) for j in js],
        })

    def _tencent_leg() -> pd.DataFrame:
        return pd.DataFrame({
            "date": trade_dates,
            "open": [10.0 + j * 0.5 for j in js],
            "high": [10.5 + j * 0.5 for j in js],
            "low": [9.5 + j * 0.5 for j in js],
            "close": [10.2 + j * 0.5 for j in js],
            "volume": [10000 * (j + 1) for j in js],
            "amount": [10000 * (j + 1) for j in js],
        })

    v = ThreeSourceValidator(
        root=tmp_path, fetch_fn=fetch_fn,
        check_fetchers={"sina": lambda s, st, ed, m: _sina_leg(),
                        "tencent": lambda s, st, ed, m: _tencent_leg()})
    # ① 三源比对
    rep = v.validate(["sh.600000"], "2024-01-02", "2024-01-06")
    assert rep.ok, f"三源比对应全绿：{rep.failures()}"
    # ② 停牌命中 100%
    susp = v.check_suspension_hit(
        ["sh.600000"], "2024-01-02", "2024-01-06",
        known_suspensions={"sh.600000": list(susp_dates)})
    assert susp.ok and susp.hit_rate == 1.0
    # ③ 幂等哈希一致
    idem = v.check_idempotency("sh.600000", "2024-01-02", "2024-01-06")
    assert idem.identical
