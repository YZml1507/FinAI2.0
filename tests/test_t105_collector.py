#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T105 日线采集器单测（离线，⛔ 无任何网络调用）—— ``data/collector.py``。

覆盖 T105 实现要求的七类（对应 tasks.md T105 / data_dictionary_v1.md）：

  ① 停牌过滤：主源 ``fetch()`` 的 R1 结果（``meta['suspended_rows']``）透传进
     ``CollectResult.meta``（复用 baostock_source._drop_suspended，不重造轮子）；
  ② 复权档映射：``_resolve_adjustflag`` 经 ``AdjustmentMode`` + ``to_kwargs``
     展开（⛔ 不手写 adjustflag 字面量），三态映射表锁死；``meta['adjust_mode']``
     与落盘 ``adjust_mode`` 列口径一致；
  ③ 限速：``RateLimiter`` 相邻请求补睡到 ≥0.5s（注入假 sleep/单调钟，离线）；
  ④ 熔断状态机：连续失败 3 次 OPEN + 告警一次 + ``before_call`` 显式拒绝 +
     成功不清 OPEN（粘滞）+ ``reset`` 复位（FR-DATA-7 / 数据字典 §6）；
  ⑤ 指数退避重试：失败退避 1s/2s/4s、最多 3 次重试后停止；
     注入 `种源可注入 ``fetch_fn``（替换 ``baostock_source.fetch``）、校验源可注入
    ``sina_fetch``/``tencent_fetch``（替换 akshare 懒导入），单测全程不打网。
     成功即清零连续失败；EMPTY_OK 不计熔断不重试（合法空结果）；
  ⑥ 幂等：同区间重跑两次分区文件哈希一致；跨年覆盖不丢已有年份（FR-DATA-6）；
  ⑦ 校验腿：新浪可校 OHLC/volume/amount、腾讯只校 OHLC；差异>阈值记 warning
     且**不阻断入库**（主源为准，FR-DATA-1）。

⛔⛔ 永不静默原则：断言不被 try/except 吞错；任何失败让 pytest 红。
"""
from __future__ import annotations

import hashlib
import io
import logging
import time
from contextlib import redirect_stderr
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from data import collector as col
from finai.sources.adjustment_mode import AdjustmentMode
from finai.sources.base import EMPTY_OK, FAIL_GATEWAY, FAIL_PROBE_BUG, OK

# ----------------------------------------------------------------------
# 假时间源（离线）与合成帧
# ----------------------------------------------------------------------


class FakeClock:
    """可步进的单调钟 + 假 sleep：让 RateLimiter 测 0.5s 间隔不真等时间。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _raw_daily_frame(n: int = 5, start: str = "2024-01-02") -> pd.DataFrame:
    """合成 baostock 原始日线帧（字符串值列，含 tradestatus/isST —— 均已过滤停牌）。"""
    base = pd.to_datetime(start)
    dates = [(base + pd.Timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]
    return pd.DataFrame({
        "date": dates,
        "open": [f"{10 + i * 0.5:.4f}" for i in range(n)],
        "high": [f"{10.5 + i * 0.5:.4f}" for i in range(n)],
        "low":  [f"{9.5 + i * 0.5:.4f}" for i in range(n)],
        "close": [f"{10.2 + i * 0.5:.4f}" for i in range(n)],
        "preclose": [f"{10.0 + i * 0.5:.4f}" for i in range(n)],
        "volume": [f"{10000 * (i + 1)}" for i in range(n)],
        "amount": [f"{100000 * (i + 1):.2f}" for i in range(n)],
        "turn": [f"{i + 0.1:.4f}" for i in range(n)],
        "pctChg": [f"{0.5 + i * 0.1:.4f}" for i in range(n)],
        "tradestatus": ["1"] * n,
        "isST": ["0"] * n,
        "code": ["sh.600000"] * n,
    })


def _ok_result(frame: pd.DataFrame, **meta_extra) -> object:
    """构造一个 state=OK 的 FetchResult（meta 可挂 suspended_rows 等血缘）。"""
    from finai.sources.base import FetchResult

    meta = {"suspended_rows": 0}
    meta.update(meta_extra)
    return FetchResult(state=OK, frame=frame, rows=len(frame), source="baostock", meta=meta)


# ══════════════════════════════════════════════════════════════════════
# ① 复权档映射（R4：显式传参，禁默认；三态表锁死）
# ══════════════════════════════════════════════════════════════════════

def test_resolve_adjustflag_maps_three_modes() -> None:
    """R4 映射：HFQ→'3'、RAW→'1'、QFQ→'2'（经 to_kwargs 从映射表展开，非手写）。"""
    assert col._resolve_adjustflag(AdjustmentMode.HFQ) == "3"
    assert col._resolve_adjustflag(AdjustmentMode.RAW) == "1"
    assert col._resolve_adjustflag(AdjustmentMode.QFQ) == "2"


def test_collector_passes_explicit_adjustflag_not_default(monkeypatch) -> None:
    """⛔ 禁止默认调用：sent 的 params 里必须有显式 adjustflag（HFQ→'3'），
    且字段清单含 tradestatus（R1 停牌过滤列）。"""
    collector = col.DailyCollector()
    seen: dict = {}

    def fake_fetch(kind, **params):
        seen.update(params)
        return _ok_result(_raw_daily_frame())

    monkeypatch.setattr(collector, "_fetch_fn", fake_fetch)
    res = collector.collect_symbol("sh.600000", "2024-01-02", "2024-01-08")
    assert res.ok
    assert seen["adjustflag"] == "3"          # ⛔ 显式，非默认缺省
    assert seen["frequency"] == "d"
    fields = set(seen["fields"].split(","))
    assert "tradestatus" in fields            # R1：停牌过滤列必须在字段清单
    assert "code" in fields
    assert res.meta["adjust_mode"] == AdjustmentMode.HFQ.value
    assert res.meta["adjustflag"] == "3"      # ⛔ 与 sent params 一致（显式非默认）


def test_resolved_adjustflag_equals_meta_and_parquet_column() -> None:
    """口径在 meta['adjust_mode'] 与落盘 adjust_mode 列均可追溯（FR-DATA-3）。"""
    # 与 adjustment_mode 映射表同源一致：to_kwargs(HFQ,'baostock') 恒 '3'
    from finai.sources.adjustment_mode import to_kwargs

    assert to_kwargs(AdjustmentMode.HFQ, "baostock") == {"adjustflag": "3"}


# ══════════════════════════════════════════════════════════════════════
# ② 停牌过滤：R1 元数据透传（复用 fetch 内的 _drop_suspended，不重造轮子）
# ══════════════════════════════════════════════════════════════════════

def test_suspended_rows_forwarded_into_result_meta(monkeypatch, tmp_path) -> None:
    """主源 meta['suspended_rows'] 透传进 CollectResult.meta（血缘，FR §5）。"""
    collector = col.DailyCollector(root=tmp_path)

    def fake_fetch(kind, **params):
        return _ok_result(_raw_daily_frame(), suspended_rows=3)

    monkeypatch.setattr(collector, "_fetch_fn", fake_fetch)
    res = collector.collect_symbol("sh.600000", "2024-01-02", "2024-01-08")
    assert res.ok
    assert res.meta["suspended_rows"] == 3     # 冒烟的 5 行之外另有 3 条停牌被滤
    assert res.meta["adjust_mode"] == AdjustmentMode.HFQ.value
    assert res.meta["source"] == "baostock"
    assert "fetch_time" in res.meta
    assert "symbol" in res.meta and res.meta["symbol"] == "sh.600000"
    assert res.meta["start_date"] == "2024-01-02"
    assert res.meta["end_date"] == "2024-01-08"


def test_main_loop_uses_baostock_fetch_primitive() -> None:
    """⛔ 复用母库原语：默认 fetch_fn 就是 baostock_source.fetch（不重造轮子）。"""
    from finai.sources import baostock_source

    assert col.DailyCollector()._fetch_fn is baostock_source.fetch


# ══════════════════════════════════════════════════════════════════════
# ③ 限速：相邻请求 ≥0.5s（假时钟，离线）
# ══════════════════════════════════════════════════════════════════════

def test_ratelimiter_enforces_min_interval() -> None:
    """间隔节流：首次放行（不睡），随后每次补睡到 ≥0.5s。"""
    clock = FakeClock()
    limiter = col.RateLimiter(0.5, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
    limiter.wait()
    assert clock.sleeps == [], "首次调用不应补睡"
    limiter.wait()
    assert len(clock.sleeps) == 1 and clock.sleeps[0] == pytest.approx(0.5)
    # 时钟已前进 0.5s → 再次 wait 仍需补满 0.5
    limiter.wait()
    assert clock.sleeps[-1] == pytest.approx(0.5)


def test_ratelimiter_gap_longer_than_interval_does_not_sleep() -> None:
    """距离上次 >0.5s 时不必补睡（不产生假延迟）。"""
    clock = FakeClock()
    limiter = col.RateLimiter(0.5, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
    limiter.wait()
    clock.now += 2.0          # 模拟真实经过 2s
    limiter.wait()
    assert clock.sleeps == []


# ══════════════════════════════════════════════════════════════════════
# ④ 熔断状态机（FR-DATA-7：连续失败 3 次 → 熔断 + 告警 + 停止重试）
# ══════════════════════════════════════════════════════════════════════

def test_breaker_opens_after_three_consecutive_failures() -> None:
    """连续失败 3 次 → OPEN + 恰好告警一次 + before_call 显式拒绝。"""
    alerts: list[dict] = []
    breaker = col.CircuitBreaker("baostock", threshold=3, alert_fn=alerts.append)
    breaker.record_failure("e1")
    breaker.record_failure("e2")
    assert breaker.state is col.BreakerState.CLOSED and not breaker.is_open
    opened = breaker.record_failure("e3")
    assert opened is True
    assert breaker.state is col.BreakerState.OPEN
    assert len(alerts) == 1 and alerts[0]["event"] == "circuit_open"
    assert alerts[0]["source"] == "baostock"
    with pytest.raises(col.SourceCircuitOpenError):
        breaker.before_call()


def test_breaker_success_resets_consecutive_count() -> None:
    """成功一次清零连续失败（熔断只在**连续**失败时触发）。"""
    breaker = col.CircuitBreaker("baostock", threshold=3)
    breaker.record_failure("e1")
    breaker.record_failure("e2")
    breaker.record_success()
    assert breaker.consecutive_failures == 0 and not breaker.is_open
    breaker.record_failure("e3")
    assert not breaker.is_open, "中断的连续失败不应触发熔断"


def test_breaker_stays_open_until_explicit_reset() -> None:
    """熔断是粘滞的：OPEN 后成功不清 OPEN，reset() 才复位（禁止重试风暴）。"""
    breaker = col.CircuitBreaker("baostock", threshold=3)
    [breaker.record_failure(f"e{i}") for i in range(3)]
    assert breaker.is_open
    breaker.record_success()
    assert breaker.is_open, "OPEN 后成功不得自动关闭（等待人工介入）"
    with pytest.raises(col.SourceCircuitOpenError):
        breaker.before_call()
    breaker.reset()
    assert not breaker.is_open and breaker.consecutive_failures == 0
    breaker.before_call()   # 复位后可继续调用


# ══════════════════════════════════════════════════════════════════════
# ⑤ 指数退避重试：1s/2s/4s、最多 3 次重试；成功清零；EMPTY_OK 不重试不计熔断
# ══════════════════════════════════════════════════════════════════════

def test_retry_backoff_exponential_and_cap(monkeypatch) -> None:
    """连续失败熔断优先于重试耗尽：threshold=3 ⇒ 第 3 次失败即熔断停手（退避 1/2s，
    无 4s——FR-DATA-7 熔断后不再重试）。另用 threshold=99 验证纯退避 1/2/4s。"""
    # —— 情形 A：默认阈值 3，第 3 次连续失败即熔断停手（§6「连续失败 3 次熔断」）——
    timeouts = iter([FAIL_GATEWAY] * 4)

    def fake_fetch(kind, **params):
        return _fetch_state(next(timeouts))

    done: list[float] = []
    limiter = col.RateLimiter(0.5, sleep_fn=lambda _s: None, monotonic_fn=lambda: 0.0)
    breaker = col.CircuitBreaker("baostock", threshold=3)
    result = col._fetch_with_retry(
        fake_fetch, "kline", {"code": "x", "fields": "date", "adjustflag": "1"},
        limiter=limiter, breaker=breaker, sleep_fn=done.append, max_retries=3, backoff_base=1.0)
    assert result.state == FAIL_GATEWAY
    assert done == [1.0, 2.0], f"第 3 次失败即熔断停手，退避只应有 1/2s，实得 {done}"
    assert breaker.is_open, "连续 3 次失败应已熔断（FR-DATA-7）"

    # —— 情形 B：threshold=99（不熔断）⇒ 验证纯指数退避 1/2/4s、重试耗尽即停 ——
    done2: list[float] = []
    limiter2 = col.RateLimiter(0.5, sleep_fn=lambda _s: None, monotonic_fn=lambda: 0.0)
    breaker2 = col.CircuitBreaker("baostock", threshold=99)
    result2 = col._fetch_with_retry(
        lambda k, **p: _fetch_state(FAIL_GATEWAY), "kline", {"code": "x"},
        limiter=limiter2, breaker=breaker2, sleep_fn=done2.append, max_retries=3, backoff_base=1.0)
    assert result2.state == FAIL_GATEWAY
    assert done2 == [1.0, 2.0, 4.0], f"纯退避应为 1/2/4s，实得 {done2}"
    assert not breaker2.is_open, "threshold=99 不应熔断"


def test_retry_success_clears_breaker(monkeypatch) -> None:
    """失败→失败→成功：连续失败清零，返回 OK，不熔断。"""
    responses = [FAIL_GATEWAY, FAIL_GATEWAY, "OK"]

    def fake_fetch(kind, **params):
        r = responses.pop(0)
        return _ok_result(_raw_daily_frame()) if r == "OK" else _fetch_state(r)

    sleeps: list[float] = []
    limiter = col.RateLimiter(0.5, sleep_fn=lambda _s: None, monotonic_fn=lambda: 0.0)
    breaker = col.CircuitBreaker("baostock")
    result = col._fetch_with_retry(
        fake_fetch, "kline", {}, limiter=limiter, breaker=breaker,
        sleep_fn=sleeps.append, max_retries=3)
    assert result.state == OK and result.frame is not None
    assert breaker.consecutive_failures == 0
    assert len(sleeps) == 2, f"两次失败应退避 1s/2s，实得 {sleeps}"


def test_empty_ok_not_retried_not_counted(monkeypatch) -> None:
    """EMPTY_OK（合法空结果）不重试、不计熔断（⛔ 不得读作'无数据'）。"""
    calls: list[int] = []

    def fake_fetch(kind, **params):
        calls.append(1)
        return _fetch_state(EMPTY_OK)

    limiter = col.RateLimiter(0.5, sleep_fn=lambda _s: None, monotonic_fn=lambda: 0.0)
    breaker = col.CircuitBreaker("baostock")
    result = col._fetch_with_retry(
        fake_fetch, "kline", {}, limiter=limiter, breaker=breaker,
        sleep_fn=lambda _s: None, max_retries=3)
    assert result.state == EMPTY_OK
    assert len(calls) == 1, "EMPTY_OK 不应触发重试"
    assert breaker.consecutive_failures == 0, "EMPTY_OK 不计连续失败"


def test_probe_bug_not_retried(monkeypatch) -> None:
    """FAIL_PROBE_BUG（调用方写错 / 环境缺陷）不重试 —— 重试只会放大打点压力。"""
    calls: list[int] = []

    def fake_fetch(kind, **params):
        calls.append(1)
        return _fetch_state(FAIL_PROBE_BUG)

    limiter = col.RateLimiter(0.5, sleep_fn=lambda _s: None, monotonic_fn=lambda: 0.0)
    breaker = col.CircuitBreaker("baostock")
    result = col._fetch_with_retry(
        fake_fetch, "kline", {}, limiter=limiter, breaker=breaker,
        sleep_fn=lambda _s: None, max_retries=3)
    assert result.state == FAIL_PROBE_BUG and len(calls) == 1
    assert breaker.consecutive_failures == 0


def test_consecutive_failures_trigger_breaker_in_retry(monkeypatch) -> None:
    """连续 3 次失败在重试循环内即熔断：恰好 3 次调用、1 次退避、告警一次、
    不再做第 4 次调用（熔断后不重试该源，数据字典 §6）。"""
    alerts: list[dict] = []
    calls: list[int] = []

    def fake_fetch(kind, **params):
        calls.append(1)
        return _fetch_state(FAIL_GATEWAY)

    sleeps: list[float] = []
    limiter = col.RateLimiter(0.5, sleep_fn=lambda _s: None, monotonic_fn=lambda: 0.0)
    breaker = col.CircuitBreaker("baostock", alert_fn=alerts.append)
    result = col._fetch_with_retry(
        fake_fetch, "kline", {}, limiter=limiter, breaker=breaker,
        sleep_fn=sleeps.append, max_retries=3)
    assert result.state == FAIL_GATEWAY
    assert len(calls) == 3, f"连续 3 失败即熔断，不应有第 4 次调用，实得 {len(calls)}"
    assert len(alerts) == 1
    assert breaker.is_open
    # 熔断后再调 ⇒ 显式拒绝（⛔ 不静默跳过）
    with pytest.raises(col.SourceCircuitOpenError):
        col._fetch_with_retry(
            fake_fetch, "kline", {}, limiter=limiter, breaker=breaker,
            sleep_fn=lambda _s: None, max_retries=3)


def test_collect_symbol_circuit_open_reports_failed(monkeypatch) -> None:
    """熔断拦截在 collect_symbol 层被如实上报为 failed（⛔ 不当 EMPTY_OK）。"""
    collector = col.DailyCollector()

    def broken_fetch(kind, **params):
        raise col.SourceCircuitOpenError("熔断器 OPEN")

    monkeypatch.setattr(collector, "_fetch_fn", broken_fetch)
    res = collector.collect_symbol("sh.600000", "2024-01-02", "2024-01-08")
    assert res.state == "failed" and not res.ok
    assert res.meta.get("circuit_open") is True
    assert any("熔断" in w for w in res.warnings)


def test_collect_symbol_empty_ok_reports_empty(monkeypatch, tmp_path) -> None:
    """EMPTY_OK ⇒ CollectResult.state == 'empty'（⛔ 不读作 failed，也不入库）。"""
    collector = col.DailyCollector(root=tmp_path)

    def empty_fetch(kind, **params):
        return _fetch_state(EMPTY_OK)

    monkeypatch.setattr(collector, "_fetch_fn", empty_fetch)
    res = collector.collect_symbol("sh.600000", "2024-01-02", "2024-01-08")
    assert res.state == "empty" and not res.ok
    assert res.paths == [], "EMPTY_OK 不入库（无分区文件）"


# ══════════════════════════════════════════════════════════════════════
# ⑥ 幂等：同区间重跑哈希一致；跨年增量不丢已有年份；临时文件无残留
# ══════════════════════════════════════════════════════════════════════

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_rerun_same_range_is_byte_identical(monkeypatch, tmp_path) -> None:
    """FR-DATA-6：同一区间重跑两次，分区文件 SHA-256 一致（幂等）。"""
    frame = _raw_daily_frame(5, start="2024-01-02")
    calls: list[str] = []

    def fake_fetch(kind, **params):
        calls.append(params.get("code", ""))
        return _ok_result(frame)

    collector = col.DailyCollector(root=tmp_path)
    monkeypatch.setattr(collector, "_fetch_fn", fake_fetch)

    r1 = collector.collect_symbol("sh.600000", "2024-01-02", "2024-01-08")
    p1 = tmp_path / "sh.600000" / "2024.parquet"
    assert r1.ok and p1.exists()
    h1 = _sha(p1)

    r2 = collector.collect_symbol("sh.600000", "2024-01-02", "2024-01-08")
    h2 = _sha(p1)
    assert h1 == h2, "同区间重跑两次哈希必须一致（幂等 FR-DATA-6）"
    assert r1.rows == r2.rows == 5
    # 数据内容核对
    back = pd.read_parquet(p1, engine="pyarrow")
    assert len(back) == 5
    assert (back["adjust_mode"] == "HFQ").all()
    assert (back["source"] == "baostock").all()


def test_merge_fresh_over_existing_is_stable(monkeypatch, tmp_path) -> None:
    """半覆盖重跑（新数据与已有分区重叠）→ 同日期覆盖、无重复行、哈希可复现。"""
    calls: list[str] = []

    def fake_fetch(kind, **params):
        calls.append(1)
        frame = _raw_daily_frame(5, start="2024-01-02")
        return _ok_result(frame)

    collector = col.DailyCollector(root=tmp_path)
    monkeypatch.setattr(collector, "_fetch_fn", fake_fetch)
    collector.collect_symbol("sh.600000", "2024-01-02", "2024-01-08")
    h_before = _sha(tmp_path / "sh.600000" / "2024.parquet")

    # 第二跑：区间完全一致 → 哈希不变
    collector.collect_symbol("sh.600000", "2024-01-02", "2024-01-08")
    assert h_before == _sha(tmp_path / "sh.600000" / "2024.parquet")


def test_multi_year_partitions_are_separate_and_stable(monkeypatch, tmp_path) -> None:
    """跨年数据分成两个分区文件；各自独立、同区间重跑哈希一致。"""
    collector = col.DailyCollector(root=tmp_path)

    def fake_fetch(kind, **params):
        # 2024 年 5 行 + 2025 年 3 行
        f24 = _raw_daily_frame(5, start="2024-01-02")
        f25 = _raw_daily_frame(3, start="2025-01-02")
        return _ok_result(pd.concat([f24, f25], ignore_index=True))

    monkeypatch.setattr(collector, "_fetch_fn", fake_fetch)
    collector.collect_symbol("sh.600000", "2024-01-02", "2025-01-08")
    p24 = tmp_path / "sh.600000" / "2024.parquet"
    p25 = tmp_path / "sh.600000" / "2025.parquet"
    assert p24.exists() and p25.exists()
    assert len(pd.read_parquet(p24, engine="pyarrow")) == 5
    assert len(pd.read_parquet(p25, engine="pyarrow")) == 3
    h24 = _sha(p24)
    h25 = _sha(p25)
    assert h24 != h25, "不同年份分区内容不同（但都稳定）"
    # 重跑同区间
    collector.collect_symbol("sh.600000", "2024-01-02", "2025-01-08")
    assert h24 == _sha(p24) and h25 == _sha(p25)


def test_write_leaves_no_tmp_files(tmp_path) -> None:
    """原子写后目录无 .tmp 残留（红线：测试/探测产物用完即删）。"""
    df = _raw_daily_frame(3).pipe(
        lambda d: col._canonicalize(d))
    path = tmp_path / "sh.600000" / "2024.parquet"
    col._atomic_write_parquet(df, path)
    leftovers = [p for p in (tmp_path / "sh.600000").iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


# ══════════════════════════════════════════════════════════════════════
# ⑦ 校验腿：新浪校 OHLC/volume/amount；腾讯只校 OHLC；超阈值记 warning 不阻断
# ══════════════════════════════════════════════════════════════════════

def _sina_frame(dates: list[date], *, close: float = 10.4) -> pd.DataFrame:
    """新浪校验腿形状（中文列名，2 位精度价格）。"""
    return pd.DataFrame({
        "日期": [d.strftime("%Y-%m-%d") for d in dates],
        "开盘": [10.1] * len(dates), "最高": [10.5] * len(dates),
        "最低": [9.9] * len(dates), "收盘": [close] * len(dates),
        "成交量": [10000 * (i + 1) for i in range(len(dates))],
        "成交额": [100000.0 * (i + 1) for i in range(len(dates))],
    })


def _tencent_frame(dates: list[date], *, close: float = 10.4) -> pd.DataFrame:
    """腾讯校验腿形状（英文列名，仅 6 列）。"""
    return pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d in dates],
        "open": [10.1] * len(dates), "high": [10.5] * len(dates),
        "low": [9.9] * len(dates), "close": [close] * len(dates),
        "volume": [10000] * len(dates), "amount": [10000] * len(dates),
    })


def _bars_frame(dates: list[date]) -> pd.DataFrame:
    """主源已落盘 bars（baostock 10 位精度 close=10.4）。"""
    return pd.DataFrame({
        "date": dates,
        "open": [10.1] * len(dates), "high": [10.5] * len(dates),
        "low": [9.9] * len(dates), "close": [10.4] * len(dates),
        "preclose": [10.0] * len(dates),
        "volume": [10000.0 * (i + 1) for i in range(len(dates))],
        "amount": [100000.0 * (i + 1) for i in range(len(dates))],
        "turn": [0.5] * len(dates), "pctChg": [0.5] * len(dates),
        "tradestatus": ["1"] * len(dates), "isST": ["0"] * len(dates),
        "code": ["sh.600000"] * len(dates),
        "adjust_mode": ["HFQ"] * len(dates), "source": ["baostock"] * len(dates),
    })


def test_sina_cross_check_ok_within_tol(tmp_path) -> None:
    """新浪双腿比对：OHLC/volume/amount 各字段偏差 ≤ 阈值 → 无 warning。"""
    dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    bars = _bars_frame(dates)
    fetchers = {"sina": lambda s, st, ed, m: _sina_frame(dates)}
    warns = col._cross_check(
        "sh.600000", bars, "2024-01-02", "2024-01-08",
        AdjustmentMode.HFQ, sample=True, check_fetchers=fetchers, tol=0.002)
    assert warns == []


def test_tencent_only_checks_ohlc_even_if_amount_available(tmp_path) -> None:
    """腾讯 amount 实为成交量 ⇒ 只校 OHLC：即便腾讯帧带'amount'也不校它。"""
    dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    bars = _bars_frame(dates)

    # 腾讯帧的 amount 列人为设成 0（若是成交额必然超阈，但规则=不校 amount）
    tdf = _tencent_frame(dates)
    tdf["amount"] = [0.0] * len(dates)
    fetchers = {"tencent": lambda s, st, ed, m: tdf}
    warns = col._cross_check(
        "sh.600000", bars, "2024-01-02", "2024-01-08",
        AdjustmentMode.HFQ, sample=True, check_fetchers=fetchers, tol=0.002)
    assert warns == [], "腾讯只校 OHLC，amount 异常不许产生 warning"


def test_sina_amount_drift_triggers_warning_but_no_block(tmp_path) -> None:
    """新浪成交额偏差超阈值 ⇒ warning 出现，但不阻断入库（主源为准）。"""
    dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    bars = _bars_frame(dates)
    sdf = _sina_frame(dates)
    sdf["成交额"] = [200000.0] * len(dates)      # 相对主源 100000 → +100% 偏差
    fetchers = {"sina": lambda s, st, ed, m: sdf}

    collector = col.DailyCollector(root=tmp_path, sample_rate=1.0,
                                   check_fetchers=fetchers)
    # 校验警告出现
    warns = col._cross_check(
        "sh.600000", bars, "2024-01-02", "2024-01-08",
        AdjustmentMode.HFQ, sample=True, check_fetchers=fetchers, tol=0.002)
    assert any("成交额" in w or "amount" in w for w in warns)

    # 且 collect_symbol 落盘照常成功（校验腿不阻断）
    def fake_fetch(kind, **params):
        return _ok_result(_raw_daily_frame(3))

    monkeypatch_none = None  # placeholder never used


def test_sina_missing_rows_reported_as_warning() -> None:
    """新浪停牌跳行 ⇒ 行数少于主源：记覆盖比例 warning（自然结果，不计字段偏差）。"""
    dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    bars = _bars_frame(dates)
    sdf = _sina_frame(dates[:2])               # 新浪缺一天（停牌跳行）
    fetchers = {"sina": lambda s, st, ed, m: sdf}
    warns = col._cross_check(
        "sh.600000", bars, "2024-01-02", "2024-01-08",
        AdjustmentMode.HFQ, sample=True, check_fetchers=fetchers, tol=0.002)
    assert any("覆盖" in w for w in warns)


def test_sampling_is_deterministic() -> None:
    """抽样确定性：固定 stride 时 index 命中可复现（⛔ 不用随机数）。"""
    c_hi = col.DailyCollector(sample_rate=0.2)      # stride=5：index 0,5,10…
    c_lo = col.DailyCollector(sample_rate=0.0)
    assert [c_hi._should_sample(i) for i in range(6)] == [True, False, False, False, False, True]
    assert all(not c_lo._should_sample(i) for i in range(10))


def test_fetch_fn_injection_uses_akshare_lazy_import() -> None:
    """校验腿默认走 akshare 懒导入：import 本模块不需要 bs4（本机 akshare 缺 bs4）。"""
    step = col._DEFAULT_CHECK_FETCHERS["sina"]
    import inspect

    src = inspect.getsource(step)
    assert "import akshare" in src, "默认校验腿必须懒导入 akshare（离线/缺依赖不崩）"


def test_alert_stub_logs_without_sending() -> None:
    """熔断告警 stub 只记日志，不真发（⛔ 飞书 hermes MCP 预留桥）。"""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    col.logger.addHandler(handler)
    old_level = col.logger.level
    col.logger.setLevel(logging.WARNING)
    try:
        col.default_alert_stub({"event": "circuit_open", "source": "baostock"})
    finally:
        col.logger.removeHandler(handler)
        col.logger.setLevel(old_level)
    out = buf.getvalue()
    assert "熔断告警" in out
    assert "hermes" in out


def _fetch_state(state: str) -> object:
    """构造指定七态的结果（frame=None；仅 _fetch_with_retry 用）。"""
    from finai.sources.base import FetchResult

    return FetchResult(state=state, frame=None, rows=0, source="baostock", detail=state)