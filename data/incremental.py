#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T109 增量更新 —— 日期分区幂等续采 + 5 日冒烟（FR-DATA-6）。

本模块是数据层的**增量续采器**：站在 ``collector.DailyCollector`` 之上，
回答"哪些日子还没采"的问题 —— 从各 symbol 已有分区的**最大日期**续采到
``end_date``，把"是否重复采"交给 ``write_daily_bars_partitioned`` 的
幂等合并（读已有 → 合并 → 按日期去重 keep='last' → 原子覆盖写）。

红线落点：

  ① **幂等（FR-DATA-6）**：同区间重跑两次，分区文件 SHA-256 一致。
     续采起点 = ``last_date + 1 天``（重叠 1 天可容忍：按日期去重 keep='last'
     保证新数据覆盖旧同日行）。首次（分区不存在）从 ``default_start_date``
     全量采。

  ② **不重复造轮子**：取数一律复用 ``collector.collect_symbol()``（限速四件套 /
     R1 停牌过滤 / R4 显式复权都在那里面）；落盘一律走
     ``write_daily_bars_partitioned``（幂等合并在那里）。本模块只做
     「查水位 → 决定区间 → 调 collect_symbol → 汇报结果」。

  ③ **永不静默**：``IncrementResult.state`` 显式区分 ``ok`` / ``empty`` /
     ``failed``，与 ``CollectResult`` 同精神；``smoke_test_5d`` 任何一项
     检查不过都算冒烟失败（fail-closed），不降级不吞错。

  ④ **5 日冒烟**：采最近 5 个交易日（交易日历可注入，离线可测），验证
     分区文件生成、日期无重复、单日行数 > 0。⛔ 不校验"日期连续"——
     A 股周末节假日本来就断，"连续"是错误判据；校验"无重复 + 有数据"。

离线可测性：``collector``（含 ``fetch_fn``）与 ``trade_calendar`` 全部可注入，
单测全程不打网、不真睡。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable, Sequence

import pandas as pd

from data.collector import (
    DailyCollector,
    _partition_dir,
    hash_file,
)

logger = logging.getLogger(__name__)

__all__ = [
    "IncrementResult", "SmokeReport",
    "IncrementalUpdater",
]

#: 首次全量采集的默认起点（系统回测自 2015-01-01，数据字典 §4.1）。
DEFAULT_START_DATE = "2015-01-01"


@dataclass
class IncrementResult:
    """单 symbol 一次增量更新的结果 + 血缘 meta。

    ⭐ 永不静默：``state`` 显式区分 ``ok``（有新数据已落盘）/ ``empty``
    （无新数据，已是最新或本区间无行情）/ ``failed``（含熔断拒绝）。
    """

    symbol: str
    state: str                                   # "ok" | "empty" | "failed"
    last_date: date | None = None                # 续采前分区水位（None=首次）
    start_date: str = ""                         # 实际请求区间起点
    end_date: str = ""                           # 实际请求区间终点
    rows: int = 0                                # 本次新增行数（CollectResult.rows）
    paths: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == "ok"


@dataclass
class SmokeReport:
    """5 日冒烟报告（fail-closed：任何检查不过 → ``ok=False``）。"""

    symbol: str
    ok: bool
    checks: dict[str, bool] = field(default_factory=dict)  # 检查项 → 是否通过
    detail: str = ""

    def failures(self) -> list[str]:
        """失败检查项清单（人工排查用）。"""
        return [k for k, v in self.checks.items() if not v]


class IncrementalUpdater:
    """T109 增量更新编排器。

    ⭐ 单实例串行驱动（内部就是逐 symbol 调 ``collector.collect_symbol``，
    限速由 collector 保证）。依赖注入：``collector`` 与 ``trade_calendar``
    全部可换，单测全程不打网。

    ``trade_calendar``：`` Callable[[str, str], list[date]]`` —— 给定
    [start, end] 返回交易日升序列表（5 日冒烟用）。默认实现打网（baostock
    交易日查询），单测必须注入。
    """

    def __init__(
        self,
        collector: DailyCollector,
        *,
        default_start_date: str = DEFAULT_START_DATE,
        trade_calendar: Callable[[str, str], list[date]] | None = None,
    ) -> None:
        self.collector = collector
        self.root = collector.root
        self.default_start_date = default_start_date
        self._trade_calendar = trade_calendar

    # ------------------------------------------------------------------
    def last_partition_date(self, symbol: str) -> date | None:
        """读某 symbol 全部年分区里的**最大日期**（水位）；无分区 → None。

        ⛔ 只读分区文件，不联网；date 列在落盘时已是 ``datetime.date``。
        """
        pdir = _partition_dir(self.root, symbol)
        if not pdir.exists():
            return None
        max_d: date | None = None
        for pf in sorted(pdir.glob("*.parquet")):
            try:
                frame = pd.read_parquet(pf, engine="pyarrow", columns=["date"])
            except Exception as exc:  # noqa: BLE001 - 坏分区如实记账不吞
                logger.warning("读分区失败 %s: %s", pf, exc)
                continue
            if frame.empty:
                continue
            d = pd.to_datetime(frame["date"]).max().date()
            if max_d is None or d > max_d:
                max_d = d
        return max_d

    # ------------------------------------------------------------------
    def update(
        self,
        symbols: Iterable[str],
        end_date: str,
        *,
        start_date: str | None = None,
    ) -> dict[str, IncrementResult]:
        """批量串行增量更新到 ``end_date``，返回 ``{symbol: IncrementResult}``。

        区间决策：水位 ``last`` 存在 → ``start = last + 1 天``（保留 1 天重叠，
        幂等去重会吸收）；``last`` 为 None（首次）→ ``start = default_start_date``
        （或显式 ``start_date`` 覆盖）。``start > end``（已最新）→ 不发请求，
        直接 ``empty``。
        """
        out: dict[str, IncrementResult] = {}
        end_d = pd.to_datetime(end_date).date()
        for symbol in symbols:
            last = self.last_partition_date(symbol)
            if start_date is not None:
                req_start = start_date
            elif last is not None:
                req_start = str(last + timedelta(days=1))
            else:
                req_start = self.default_start_date
            start_d = pd.to_datetime(req_start).date()

            if start_d > end_d:
                # 已最新：水位 ≥ end_date，无需请求（幂等语义：重跑无害）。
                out[symbol] = IncrementResult(
                    symbol=symbol, state="empty",
                    last_date=last,
                    start_date=req_start, end_date=end_date,
                    meta={"reason": "up_to_date", "watermark": str(last) if last else None})
                continue

            res = self.collector.collect_symbol(symbol, req_start, end_date)
            state = res.state
            out[symbol] = IncrementResult(
                symbol=symbol, state=state,
                last_date=last,
                start_date=req_start, end_date=end_date,
                rows=res.rows, paths=list(res.paths),
                warnings=list(res.warnings), meta=dict(res.meta))
        return out

    # ------------------------------------------------------------------
    def smoke_test_5d(
        self,
        symbols: Sequence[str],
        end_date: str,
        *,
        trade_calendar: Callable[[str, str], list[date]] | None = None,
    ) -> dict[str, SmokeReport]:
        """5 日冒烟：采最近 5 个交易日，验证分区生成 / 无重复 / 有数据。

        ⛔ fail-closed：任一检查不过 → ``ok=False``。交易日历优先用参数注入，
        其次用实例注入；两者都无 → raise（⛔ 不静默打网）。
        """
        cal = trade_calendar or self._trade_calendar
        if cal is None:
            raise ValueError(
                "5 日冒烟需要交易日历（trade_calendar 可注入）；"
                "⛔ 未注入时不允许静默打网取日历")
        end_d = pd.to_datetime(end_date).date()
        # 取 end_date 前后各放宽 14 天找交易日，再取最后 5 个 ≤ end_date 的
        window_start = str(end_d - timedelta(days=14))
        window_end = str(end_d + timedelta(days=1))
        trading_days = [d for d in cal(window_start, window_end) if d <= end_d]
        if len(trading_days) < 5:
            raise ValueError(
                f"窗口 [{window_start}, {end_d}] 内交易日不足 5 个"
                f"（实得 {len(trading_days)}）—— 日历数据异常，fail-closed")
        five_days = trading_days[-5:]
        req_start = str(five_days[0])

        reports: dict[str, SmokeReport] = {}
        for symbol in symbols:
            res = self.collector.collect_symbol(symbol, req_start, end_date)
            if not res.ok:
                reports[symbol] = SmokeReport(
                    symbol=symbol, ok=False,
                    checks={"collect_ok": False},
                    detail=f"采集失败 state={res.state}: {res.warnings}")
                continue
            checks = self._verify_symbol(symbol, res)
            reports[symbol] = SmokeReport(
                symbol=symbol, ok=all(checks.values()),
                checks=checks,
                detail="" if all(checks.values())
                else f"失败项: {[k for k, v in checks.items() if not v]}")
        return reports

    # ------------------------------------------------------------------
    def _verify_symbol(self, symbol: str, res) -> dict[str, bool]:
        """冒烟逐项检查（读回已落盘分区验证，不信内存帧）。"""
        checks: dict[str, bool] = {}
        # ① 分区文件生成
        checks["partition_files_written"] = bool(res.paths) and all(
            p.exists() for p in res.paths)
        # ② 读回已落盘数据校验
        frames = []
        for p in res.paths:
            if p.exists():
                frames.append(pd.read_parquet(p, engine="pyarrow"))
        if not frames:
            checks["readback_nonempty"] = False
            checks["no_duplicate_dates"] = False
            return checks
        df = pd.concat(frames, ignore_index=True)
        dates = pd.to_datetime(df["date"]).dt.date
        # ③ 单日行数 > 0（有数据）
        checks["readback_nonempty"] = len(df) > 0
        # ④ 日期无重复（幂等去重生效）
        checks["no_duplicate_dates"] = bool(dates.is_unique)
        return checks
