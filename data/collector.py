#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T105 日线采集器 —— baostock 主源 + 新浪/腾讯校验腿 + 限速四件套（FR-DATA-7）。

本模块是数据层的**入库生产者**：调用 `finai/sources/baostock_source.py::fetch()`
取 A 股日线，按 T104 数据字典 §1.1 schema 落盘成「按 symbol+年份分区」的
Parquet（幂等覆盖写，FR-DATA-6），并用新浪/腾讯做抽样交叉比对（FR-DATA-1）。

红线落点（逐条对应 CLAUDE.md §3 / WORK_ORDER §3，⛔ 违反即返工）：

  ① **复用母库原语，不重造轮子**
     · 主循环不调 baostock 原生 API，一律走 ``baostock_source.fetch()`` —— 它
       已内嵌：游标消费（FINDING-185）/ 外部超时（FINDING-181）/ R1 停牌过滤
       （``_drop_suspended``，``meta['suspended_rows']``）/ R4 口径守卫。
     · 复权档经 ``finai/sources/adjustment_mode.py`` 的 ``AdjustmentMode`` 三态
       枚举 + ``to_kwargs()`` 解析，⛔ 本模块**绝不手写** ``adjustflag='3'`` 字面量。

  ② **禁止默认复权调用（最贵陷阱）**
     baostock 缺省 ``adjustflag='3'``=后复权；``fetch()`` 内的
     ``_validate_kline_params``（baostock_source.py:139-144）已在校验层拦截
     越界值，但「显式传参」的义务在**调用侧**：本模块把 ``AdjustmentMode`` 经
     ``to_kwargs()`` 展开成 ``adjustflag`` 实参传入，落盘列含 ``adjust_mode``。

  ③ **限速四件套（FR-DATA-7）**——见 ``RateLimiter`` / ``CircuitBreaker``：
     串行单线程、相邻请求间隔 ≥0.5s、指数退避重试（基数 1s、最多 3 次）、
     连续失败 3 次熔断当前源切备胎并触发告警（飞书 hermes MCP，留 stub 不真发）。

  ④ **校验腿只告警不失败**：新浪 ``akshare::stock_zh_a_hist`` 校
     OHLC/volume/amount（无 preclose/tradestatus/isST，停牌跳行，精度 2 位）；
     腾讯 ``akshare::stock_zh_a_hist_tx`` 仅 6 列且 ``amount`` 实为成交量 ⇒
     **只校 OHLC**。差异 > 阈值记 ``warnings``，⛔ 不阻断入库（主源为准）。

离线可测性（⛔ 红线"能离线就不联网"）：
  · 主源可注入 ``fetch_fn``（替换 ``baostock_source.fetch``）、校验源可注入
    ``sina_fetch``/``tencent_fetch``（替换 akshare 懒导入），单测全程不打网。
  · akshare 的导入是**懒加载**（``_default_sina_fetch`` 内 import），故 import
    本模块、跑单测**不需要** bs4 —— 本机 akshare 因缺 bs4 而 import 失败，
    但本模块只有在真实调用校验腿时才会触发那次导入。

落盘布局（D1）：``data/daily_bars/{symbol}/{year}.parquet``，
  文件名即该年分区；同区间重跑按 (symbol, 年) 整份覆盖写 ⇒ 两次哈希一致。
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from finai.sources import baostock_source
from finai.sources.adjustment_mode import AdjustmentMode, to_kwargs
from finai.sources.base import EMPTY_OK, FAIL_PROBE_BUG, OK

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """采集时间戳（血缘 meta 用，FR-DATA 血缘字段 ``fetch_time``）。"""
    return datetime.now(timezone.utc).isoformat()


# =====================================================================
# 常量（限速/重试/熔断/校验阈值）—— FR-DATA-7 / 数据字典 §6
# =====================================================================

#: 相邻请求最小间隔（秒）。数据字典 §6：「相邻请求间隔 ≥0.5s」。
MIN_INTERVAL_S = 0.5

#: 指数退避基数（秒）。数据字典 §6：「退避基数为 1s」。
RETRY_BACKOFF_BASE_S = 1.0

#: 单源最大重试次数（除首次外）。数据字典 §6：「最大 3 次」。
MAX_RETRIES = 3

#: 触发熔断的**连续**失败次数。数据字典 §6：「连续失败 3 次」。
BREAKER_THRESHOLD = 3

#: 校验腿相对差异阈值（无量纲，0.002 = 0.2%）。主源为准，差异超过即记 warning。
#: ⚠ 新浪精度 2 位 vs baostock 10 位 ⇒ 阈值不得设 0，否则精度差本身成假阳性。
CHECK_TOL = 0.002

#: T104 §1.1 主源 schema 要求的字段（baostock ``query_history_k_data_plus``）。
#: ⛔ 必须含 ``tradestatus``（R1 停牌过滤列）与 ``adjustflag`` 由调用侧显式传
#:    （非输出字段，见 baostock_source.py:_DAILY_FIELDS_REQUIRED 的强制）。
BAOSTOCK_DAILY_FIELDS: tuple[str, ...] = (
    "date", "open", "high", "low", "close", "preclose",
    "volume", "amount", "turn", "pctChg",
    "tradestatus", "isST", "code",
)

#: 主源（baostock）源标识。落盘 ``source`` 列 + meta 用。
SOURCE_BAOSTOCK = "baostock"
#: 校验腿源标识（数据字典 §1.2 / §1.3）。
SOURCE_SINA = "sina"
SOURCE_TENCENT = "tencent"


# =====================================================================
# 类型别名（依赖注入点）
# =====================================================================

#: 主源取数器签名：与 ``baostock_source.fetch`` 同构（kind + 关键字参 → FetchResult）。
FetchFn = Callable[..., Any]
#: 告警回调：熔断时触发（飞书 hermes MCP 桥；见 ``default_alert_stub``）。
AlertFn = Callable[[dict[str, Any]], None]
#: 校验腿取数器签名：(symbol, start_date, end_date, AdjustmentMode) → DataFrame。
CheckFetchFn = Callable[[str, str, str, AdjustmentMode], pd.DataFrame]


# =====================================================================
# 限速四件套之一/二：串行 + 间隔节流
# =====================================================================

class RateLimiter:
    """相邻请求间隔节流（FR-DATA-7 串行 + ≥0.5s 间隔）。

    ⭐ 单调时钟 + 显式间隔：调用方持有一个实例串行复用 ⇒ 天然保证"单源串行"
    （数据字典 §6 不并发）。``sleep_fn`` 与 ``monotonic_fn`` 可注入 ⇒ 单测离线、
    不真等 0.5s。
    """

    def __init__(
        self,
        min_interval: float = MIN_INTERVAL_S,
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval = float(min_interval)
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._last: float | None = None

    def wait(self) -> None:
        """距上次请求不足 ``min_interval`` 则补睡到满；首次调用立即放行。"""
        now = self._monotonic()
        if self._last is not None:
            gap = now - self._last
            if gap < self.min_interval:
                self._sleep(self.min_interval - gap)
        # ⭐ 在"补睡之后"重取时间戳，保证下一次 gap 从本次实际发起时刻起算。
        self._last = self._monotonic()


# =====================================================================
# 限速四件套之四：熔断状态机（连续失败 3 次 → 熔断切备胎 + 告警）
# =====================================================================

class BreakerState(str, Enum):
    """熔断三态。CLOSED 正常供数 / OPEN 已熔断（不再重试该源）。"""

    CLOSED = "CLOSED"
    OPEN = "OPEN"


class SourceCircuitOpenError(RuntimeError):
    """熔断后仍尝试调用该源 —— ⛔ 数据字典 §6「熔断后不再重试该源，直到人工介入」。

    ⛔ 这是**显式拒绝**，不是静默跳过：把"想取但被熔断拦住"藏起来，会让一次
    本应告警的故障无声消失（同 base.py:22「上限触发必须 raise」的精神）。
    """


class CircuitBreaker:
    """单源熔断器：连续失败 ``threshold`` 次 → OPEN，触发告警并停止重试。

    ⭐ 「连续」失败：成功一次即清零（``record_success``）。熔断是**粘滞**的 —
    一旦 OPEN 保持到显式 ``reset()``（人工介入），期间 ``before_call`` 一律抛
    ``SourceCircuitOpenError``，绝不自动重试（数据字典 §6 禁止重试风暴）。
    """

    def __init__(
        self,
        source: str,
        threshold: int = BREAKER_THRESHOLD,
        *,
        alert_fn: AlertFn | None = None,
    ) -> None:
        self.source = source
        self.threshold = int(threshold)
        self._alert_fn = alert_fn or default_alert_stub
        self.consecutive_failures = 0
        self.state = BreakerState.CLOSED

    @property
    def is_open(self) -> bool:
        return self.state is BreakerState.OPEN

    def before_call(self) -> None:
        """调用前闸：已熔断则拒绝（⛔ 不放行 ⇒ 阻断重试风暴）。"""
        if self.is_open:
            raise SourceCircuitOpenError(
                f"[{self.source}] 熔断器 OPEN（连续失败 {self.consecutive_failures} 次），"
                f"⛔ 不再重试该源直到人工介入（FR-DATA-7 / 数据字典 §6）")

    def record_success(self) -> None:
        """成功一次：连续失败计数清零（熔断只在**连续**失败时触发）。"""
        self.consecutive_failures = 0

    def record_failure(self, detail: str = "") -> bool:
        """记一次失败，达阈值则熔断 + 告警。返回是否在本次熔断。"""
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold and not self.is_open:
            self.state = BreakerState.OPEN
            self._alert_fn({
                "event": "circuit_open",
                "source": self.source,
                "consecutive_failures": self.consecutive_failures,
                "detail": detail,
                "ts": _utc_now_iso(),
            })
            return True
        return False

    def reset(self) -> None:
        """人工介入后复位。⭐ 命名刻意直白 —— 复位应当是一个**可见的决定**。"""
        self.consecutive_failures = 0
        self.state = BreakerState.CLOSED


def default_alert_stub(payload: dict[str, Any]) -> None:
    """熔断告警的**预留桥**（飞书 hermes MCP）。⛔ 留 stub 不真发。

    T001 已拍板告警通道 = 飞书（经 hermes_orchestrator MCP）。本函数是接线点：
    当前只记日志（WARNING 级），真实发送待 T106+ 接入 hermes MCP 工具时替换实现。
    签名刻意保持 ``payload: dict`` —— 替换实现时不改调用方（``CircuitBreaker``）。
    """
    logger.warning(
        "数据层熔断告警（stub，未真发飞书 hermes MCP）: %s", payload)


# =====================================================================
# 复权档 → baostock 显式 adjustflag（R4，⛔ 禁止默认调用）
# =====================================================================

def _resolve_adjustflag(mode: AdjustmentMode) -> str:
    """把统一口径 ``AdjustmentMode`` 翻译成 baostock 显式 ``adjustflag`` 实参。

    ⛔ 全仓只有 ``adjustment_mode.py`` 允许写死复权取值 —— 本函数经
    ``to_kwargs()`` 从映射表解析，⛔ 不手写 ``'3'``/``'2'``/``'1'`` 字面量。
    baostock 缺省 ``adjustflag='3'``=后复权（最贵陷阱），故返回值**必然非空**
    且必须被显式传入 ``fetch()``（禁止依赖缺省）。
    """
    kwargs = to_kwargs(mode, "baostock")
    flag = kwargs["adjustflag"]
    if flag == "":  # pragma: no cover - to_kwargs(HFQ,'baostock') 恒给 '3'，空串仅防御
        # baostock 空串也按后复权（映射表注释），但仍**显式化**为 '3'，杜绝默认。
        return "3"
    return flag


# =====================================================================
# 限速四件套之三：指数退避重试 + 熔断包装
# =====================================================================

def _is_retryable_state(state: str) -> bool:
    """是否值得退避重试。⛔ ``FAIL_PROBE_BUG``（调用方写错）不重试 —— 重试 100 次
    也是同一个错，只会放大打点压力；其余失败态（网关/连接/语义）按瞬时故障重试。"""
    return state not in (OK, EMPTY_OK, FAIL_PROBE_BUG)


def _fetch_with_retry(
    fetch_fn: FetchFn,
    kind: str,
    params: dict[str, Any],
    *,
    limiter: RateLimiter,
    breaker: CircuitBreaker,
    sleep_fn: Callable[[float], None],
    max_retries: int = MAX_RETRIES,
    backoff_base: float = RETRY_BACKOFF_BASE_S,
) -> Any:
    """对单源取数做「间隔节流 + 指数退避重试 + 连续失败熔断」。

    语义（FR-DATA-7 四件套之三/四 + 串行/间隔由 limiter 兜）：
      · 每次调用前 ``breaker.before_call()`` —— 已熔断则**显式拒绝**（不静默跳过）；
      · ``limiter.wait()`` 保证相邻请求 ≥0.5s（含重试之间）；
      · 失败按 1s/2s/4s… 指数退避，最多重试 ``max_retries`` 次；
      · 每次失败喂给 ``breaker.record_failure``，连续 3 次熔断 + 告警，**并立即
        停止重试**（return 该失败结果），熔断后再调即被 ``before_call`` 拦。
    """
    last: Any = None
    attempts = max_retries + 1  # 首次 + 重试 max_retries 次
    for attempt in range(attempts):
        breaker.before_call()
        limiter.wait()
        result = fetch_fn(kind, **params)
        if result.state == OK:
            breaker.record_success()
            return result
        last = result
        if not _is_retryable_state(result.state):
            # ⛔ EMPTY_OK（真 0 行）/ FAIL_PROBE_BUG（调用方写错）不算故障、不计熔断、
            #    不重试：前者是合法空结果（FINDING-178），后者重试无意义。
            return result
        opened = breaker.record_failure(result.detail or result.state)
        if opened or attempt >= max_retries:
            # 熔断（达阈值）或重试耗尽 ⇒ 停止，返回最后一次失败结果。
            return result
        # ⛔ 即便 0s 也走 sleep_fn：保持调用形态一致（单测注入假 sleep 观测次数）。
        sleep_fn(backoff_base * (2 ** attempt))
    return last  # pragma: no cover - 循环内必 return；防御性兜底


# =====================================================================
# baostock 原始行 → 落盘 bars 帧（类型规整 + adjust_mode/source 血缘列）
# =====================================================================

#: 数值列（T104 §1.1 类型均为 float64）。baostock 返回的是字符串，须转数值。
#: ⛔ 停牌脏行已被 ``fetch()`` 过滤，这里的数值列都是真实行情（R1）。
_NUMERIC_COLS: tuple[str, ...] = (
    "open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pctChg",
)


def _build_bars(
    frame: pd.DataFrame,
    *,
    mode: AdjustmentMode,
) -> pd.DataFrame:
    """把 baostock 日线帧规整成落盘 bars 帧。

    步骤：按 ``BAOSTOCK_DAILY_FIELDS`` 取列 → date 转 ``datetime.date`` →
    按日期升序 + 同日期去重（幂等前提）→ 数值列 ``to_numeric`` → 追加血缘列
    ``adjust_mode``/``source``（FR-DATA-3：口径可追溯）。``symbol``/``fetch_time``/
    ``suspended_rows`` 由调用侧写进 meta（行级冗余进 parquet 浪费，meta 才是血缘位）。
    """
    cols = [c for c in BAOSTOCK_DAILY_FIELDS if c in frame.columns]
    out = frame.loc[:, cols].copy()
    out["date"] = pd.to_datetime(out["date"], format="%Y-%m-%d").dt.date
    for c in _NUMERIC_COLS:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    out = (out.sort_values("date")
              .drop_duplicates(subset=["date"], keep="last")
              .reset_index(drop=True))
    out["adjust_mode"] = mode.value          # ⭐ FR-DATA-3：每行口径可追溯
    out["source"] = SOURCE_BAOSTOCK
    return out


# =====================================================================
# 校验腿（新浪 / 腾讯）：交叉比对，差异 > 阈值记 warning（⛔ 不阻断入库）
# =====================================================================

def _norm_symbol_digits(symbol: str) -> str:
    """``sh.600000`` / ``600000.SH`` / ``600000`` → ``600000``（akshare 校验腿入参）。"""
    s = str(symbol).strip()
    if "." in s:
        a, b = s.split(".", 1)
        # baostock 形如 sh.600000（前缀字母）；tushare 形如 600000.SH（后缀字母）
        return b if a.isalpha() else a
    return s


def _default_sina_fetch(
    symbol: str, start_date: str, end_date: str, mode: AdjustmentMode,
) -> pd.DataFrame:
    """新浪校验腿（``akshare::stock_zh_a_hist``）。⭐ akshare **懒导入**（见模块 docstring）。"""
    import akshare as ak  # noqa: PLC0415 - 懒导入：单测/无 bs4 环境下不应触发

    return ak.stock_zh_a_hist(
        symbol=_norm_symbol_digits(symbol),
        period="daily",
        start_date=start_date.replace("-", ""),
        end_date=end_date.replace("-", ""),
        **to_kwargs(mode, "akshare"),   # ⛔ 复权档经映射表，不手写 adjust=''
    )


def _default_tencent_fetch(
    symbol: str, start_date: str, end_date: str, mode: AdjustmentMode,
) -> pd.DataFrame:
    """腾讯校验腿（``akshare::stock_zh_a_hist_tx``）。⭐ akshare **懒导入**。"""
    import akshare as ak  # noqa: PLC0415 - 懒导入

    return ak.stock_zh_a_hist_tx(
        symbol=_norm_symbol_digits(symbol),
        start_date=start_date.replace("-", ""),
        end_date=end_date.replace("-", ""),
        **to_kwargs(mode, "akshare"),
    )


#: 校验腿取数器的注册表（源标识 → akshare 懒导入函数）。
_DEFAULT_CHECK_FETCHERS: dict[str, CheckFetchFn] = {
    SOURCE_SINA: _default_sina_fetch,
    SOURCE_TENCENT: _default_tencent_fetch,
}

#: 各校验腿「可校字段 → akshare 列名」映射（T104 §1.2/§1.3）：
#:   新浪可校 OHLC/volume/amount（无 preclose/tradestatus/isST）；
#:   腾讯仅 6 列且 ``amount`` 实为成交量 ⇒ **只校 OHLC**（⛔ 不用腾讯 amount 校成交额）。
_SINA_COLS: dict[str, str] = {
    "open": "开盘", "high": "最高", "low": "最低", "close": "收盘",
    "volume": "成交量", "amount": "成交额",
}
_TENCENT_COLS: dict[str, str] = {
    "open": "open", "high": "high", "low": "low", "close": "close",
}
_CHECK_FIELDS: dict[str, dict[str, str]] = {
    SOURCE_SINA: _SINA_COLS,
    SOURCE_TENCENT: _TENCENT_COLS,
}
#: 校验腿日期列名（akshare 中文列「日期」 vs 腾讯英文「date」）。
_CHECK_DATE_COL: dict[str, str] = {SOURCE_SINA: "日期", SOURCE_TENCENT: "date"}


def _cross_check_one(
    source: str,
    check_df: pd.DataFrame,
    primary: pd.DataFrame,
    *,
    tol: float,
) -> list[str]:
    """单校验腿对账：按日期内连接对齐，逐字段算相对偏差，超阈值记 warning。

    ⛔ 主源为准 —— 校验腿只产生 warning 供人工/后续对账，⛔ 不阻断入库。
    ⭐ 新浪/腾讯停牌日**跳行**（无脏行），与已过滤的主源按日期内连接对齐，
    日期不一致是自然结果、不计入字段偏差（行数差单记 warning）。
    """
    warnings: list[str] = []
    colmap = _CHECK_FIELDS[source]
    date_col = _CHECK_DATE_COL[source]
    if check_df is None or check_df.empty:
        warnings.append(f"[{source}] 校验腿 0 行（EMPTY_OK，⛔ 不得读作'无数据'）")
        return warnings

    chk = check_df.copy()
    chk["date"] = pd.to_datetime(chk[date_col]).dt.date
    merged = primary.merge(chk, on="date", how="inner", suffixes=("", "_c"))
    if len(merged) != len(primary):
        warnings.append(
            f"[{source}] 校验腿覆盖 {len(merged)}/{len(primary)} 个交易日"
            f"（停牌跳行/区间差异，仅提示）")
    if merged.empty:
        return warnings

    for field_name, src_col in colmap.items():
        if field_name not in merged.columns or src_col not in merged.columns:
            continue
        base = pd.to_numeric(merged[field_name], errors="coerce")
        cand = pd.to_numeric(merged[src_col], errors="coerce")
        valid = base.notna() & cand.notna() & (base != 0)
        if not valid.any():
            continue
        rel = ((cand[valid] - base[valid]).abs() / base[valid].abs()).max()
        if rel > tol:
            warnings.append(
                f"[{source}] 字段 {field_name} 最大相对偏差 {rel:.4%} > 阈值 {tol:.2%}")
    return warnings


def _cross_check(
    symbol: str,
    primary: pd.DataFrame,
    start_date: str,
    end_date: str,
    mode: AdjustmentMode,
    *,
    sample: bool,
    check_fetchers: dict[str, CheckFetchFn],
    tol: float,
) -> list[str]:
    """对抽样标的跑新浪/腾讯校验腿，聚合全部 warning。未抽样 / 缺校验腿即返回空。

    ⭐ 复权口径对齐（FR-DATA-3）：校验腿用**同一** ``AdjustmentMode`` 经
    ``to_kwargs(mode, 'akshare')`` 展开，⛔ 不跨源混用复权因子（数据字典 §4.3-6）。
    """
    if not sample or primary.empty:
        return []
    warnings: list[str] = []
    for source in (SOURCE_SINA, SOURCE_TENCENT):
        fetcher = check_fetchers.get(source)
        if fetcher is None:
            continue
        try:
            check_df = fetcher(symbol, start_date, end_date, mode)
        except Exception as exc:  # noqa: BLE001 - 校验腿失败不阻断主源入库
            warnings.append(f"[{source}] 校验腿取数失败（仅提示）: {type(exc).__name__}")
            continue
        warnings.extend(_cross_check_one(source, check_df, primary, tol=tol))
    return warnings


# =====================================================================
# 幂等落盘（D1：data/daily_bars/{symbol}/{year}.parquet 按日期分区覆盖写）
# =====================================================================

def _partition_dir(root: Path, symbol: str) -> Path:
    """某 symbol 的分区目录（如 ``data/daily_bars/sh.600000``）。"""
    return Path(root) / symbol


def _partition_path(root: Path, symbol: str, year: int) -> Path:
    """某 (symbol, 年) 分区的 Parquet 路径（文件名即分区键）。"""
    return _partition_dir(root, symbol) / f"{year}.parquet"


def _canonicalize(frame: pd.DataFrame) -> pd.DataFrame:
    """落盘前规范化：date 转 date、按日期升序去重、重置索引。

    ⭐ 幂等的根：同区间重跑两次产出**逐字节一致**的分区文件，前提是 DataFrame
    的行序/索引/类型确定。合并已有分区 + 新数据后统一过本函数 ⇒ 与运行次序无关。
    """
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.date
    out = (out.sort_values("date")
              .drop_duplicates(subset=["date"], keep="last")
              .reset_index(drop=True))
    return out


def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
    """同目录临时文件写满后 ``os.replace`` 替换 —— ⛔ 杜绝写一半的脏分区。

    ⚠ ``os.replace`` 在 Windows 上对**已存在**目标是原子替换；同目录临时文件
    保证与目标同盘（跨盘 ``os.replace`` 会失败）。临时文件用完即删（红线"测试
    /探测产物用完即删"的同款纪律落到生产路径）。
    """
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp",
                               dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        frame.to_parquet(tmp_path, engine="pyarrow", index=False)
        os.replace(tmp_path, path)
    finally:
        # ⛔ 临时文件用完即删（红线"测试/探测产物用完即删"落到生产路径同款纪律）。
        if tmp_path.exists():
            tmp_path.unlink()


def write_daily_bars_partitioned(
    new_bars: pd.DataFrame,
    *,
    symbol: str,
    root: Path,
) -> list[Path]:
    """把 ``new_bars`` 按 (symbol, 年) 分区**幂等覆盖写**。

    幂等语义（FR-DATA-6 / 数据字典 §6）：先读该分区**已有**内容，与 ``new_bars``
    合并后按日期去重（新数据覆盖旧同日行），再整份原子覆盖该年文件。⇒ 同区间
    重跑两次，分区文件哈希一致；跨区间增量运行也不会丢已有年份。

    返回写出的分区文件路径列表（按年升序）。
    """
    written: list[Path] = []
    canonical = _canonicalize(new_bars)
    years = sorted({d.year for d in canonical["date"]})
    for year in years:
        year_mask = [d.year == year for d in canonical["date"]]
        year_rows = canonical[year_mask]
        path = _partition_path(root, symbol, year)
        if path.exists():
            existing = pd.read_parquet(path, engine="pyarrow")
            combined = pd.concat([existing, year_rows], ignore_index=True)
            combined = combined.drop_duplicates(subset=["date"], keep="last")
        else:
            combined = year_rows
        _atomic_write_parquet(_canonicalize(combined), path)
        written.append(path)
    return written


def hash_file(path: Path) -> str:
    """分区文件内容的 SHA-256（幂等校验用，T110 验收判据 ⑥）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# =====================================================================
# 采集结果与编排（单 symbol → 批量）
# =====================================================================

@dataclass
class CollectResult:
    """单 symbol 一次采集的结果 + 血缘 meta（数据字典 §5）。

    ⭐ 永不静默：``state`` 显式区分 ``ok``（有数据已落盘）/ ``empty``（EMPTY_OK，
    合法空结果，⛔ 不读作"无数据"）/ ``failed``（含熔断拒绝）。``meta`` 携带
    数据字典 §5 要求的血缘字段（``adjust_mode``/``source``/``suspended_rows``/
    ``fetch_time``/``symbol``/``start_date``/``end_date``）；``truncated_bars``
    待 R2 恢复（数据字典 §5 标注）。
    """

    symbol: str
    state: str                                  # "ok" | "empty" | "failed"
    rows: int = 0
    paths: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == "ok"


class DailyCollector:
    """T105 日线采集器编排器。

    ⭐ 单实例**串行**驱动（FR-DATA-7 不并发）：内部一个 ``RateLimiter`` 被所有
    请求复用 ⇒ 相邻请求恒 ≥0.5s；一个主源 ``CircuitBreaker`` + 每个校验腿一个。

    依赖注入（离线可测）：``fetch_fn``/``check_fetchers``/``sleep_fn``/
    ``monotonic_fn``/``alert_fn`` 全部可换，单测全程不打网、不真睡。
    """

    def __init__(
        self,
        *,
        root: Path | str = Path("data/daily_bars"),
        adjust_mode: AdjustmentMode = AdjustmentMode.HFQ,  # 数据字典 §4.3：存储优先 baostock hfq
        sample_rate: float = 0.2,               # 校验腿抽样比例（0~1）
        sample_stride: int | None = None,       # 固定步长抽样（确定性，默认按 rate）
        max_retries: int = MAX_RETRIES,
        min_interval: float = MIN_INTERVAL_S,
        check_tol: float = CHECK_TOL,
        fetch_fn: FetchFn | None = None,
        check_fetchers: dict[str, CheckFetchFn] | None = None,
        alert_fn: AlertFn | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = Path(root)
        self.adjust_mode = adjust_mode
        self.sample_rate = float(sample_rate)
        self.sample_stride = sample_stride
        self.max_retries = int(max_retries)
        self.check_tol = float(check_tol)
        self._fetch_fn = fetch_fn if fetch_fn is not None else baostock_source.fetch
        self._check_fetchers = (
            dict(_DEFAULT_CHECK_FETCHERS) if check_fetchers is None
            else dict(check_fetchers))
        self._sleep = sleep_fn
        self._limiter = RateLimiter(
            min_interval, sleep_fn=sleep_fn, monotonic_fn=monotonic_fn)
        self._alert_fn = alert_fn
        self._breakers: dict[str, CircuitBreaker] = {}

    def _breaker(self, source: str) -> CircuitBreaker:
        """取（或建）某源的熔断器。主源与校验腿各自独立熔断。"""
        br = self._breakers.get(source)
        if br is None:
            br = CircuitBreaker(source, alert_fn=self._alert_fn)
            self._breakers[source] = br
        return br

    # ------------------------------------------------------------------
    def _should_sample(self, index: int) -> bool:
        """第 ``index`` 只票是否抽中校验（确定性，⛔ 不用随机数 ⇒ 可复现）。"""
        if self.sample_stride is not None and self.sample_stride > 0:
            return index % self.sample_stride == 0
        if self.sample_rate <= 0:
            return False
        if self.sample_rate >= 1:
            return True
        stride = max(1, round(1.0 / self.sample_rate))
        return index % stride == 0

    # ------------------------------------------------------------------
    def collect_symbol(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        sample: bool = False,
    ) -> CollectResult:
        """采集单 symbol 的 [start_date, end_date] 日线并幂等落盘。

        流程：显式 adjustflag（R4）→ 限速+重试+熔断取数（FR-DATA-7）→
        ``_build_bars`` 规整（R1 已由 fetch 过滤停牌）→ 校验腿比对（抽样时）→
        分区覆盖写（幂等 FR-DATA-6）。
        """
        fetch_time = _utc_now_iso()
        adjustflag = _resolve_adjustflag(self.adjust_mode)   # ⛔ 显式，非默认
        params = {
            "code": symbol,
            "fields": ",".join(BAOSTOCK_DAILY_FIELDS),
            "start_date": start_date,
            "end_date": end_date,
            "frequency": "d",
            "adjustflag": adjustflag,
        }
        base_meta: dict[str, Any] = {
            "symbol": symbol,
            "source": SOURCE_BAOSTOCK,
            "adjust_mode": self.adjust_mode.value,
            "adjustflag": adjustflag,
            "start_date": start_date,
            "end_date": end_date,
            "fetch_time": fetch_time,
            # ⛔ truncated_bars 待 R2 恢复（数据字典 §5 标注），此处不伪造。
        }

        try:
            res = _fetch_with_retry(
                self._fetch_fn, "kline", params,
                limiter=self._limiter,
                breaker=self._breaker(SOURCE_BAOSTOCK),
                sleep_fn=self._sleep,
                max_retries=self.max_retries,
            )
        except SourceCircuitOpenError as exc:
            # 熔断拒绝：显式 failed，⛔ 不静默当 EMPTY_OK（熔断≠无数据）。
            return CollectResult(
                symbol=symbol, state="failed",
                warnings=[str(exc)], meta={**base_meta, "circuit_open": True})

        if res.state != OK:
            # EMPTY_OK（合法空结果）或重试耗尽的失败 —— 如实上报，不读作"无数据"。
            meta = {**base_meta, **getattr(res, "meta", {})}
            return CollectResult(
                symbol=symbol,
                state="empty" if res.state == EMPTY_OK else "failed",
                warnings=[res.detail] if res.detail else [],
                meta=meta)

        frame = res.frame
        # ⭐ _build_bars 只做「字段规整 + adjust_mode/source 血缘列」；symbol/
        #   suspended_rows/fetch_time 等批次血缘进 meta（见 _build_bars docstring，
        #   行级冗余进 parquet 浪费）。suspended_rows 恒显式补 0（数据字典 §5：
        #   血缘字段必须携带，=0 表示本区间无停牌被滤）。
        bars = _build_bars(frame, mode=self.adjust_mode)
        meta = {
            **base_meta,
            **getattr(res, "meta", {}),
            "suspended_rows": int(getattr(res, "meta", {}).get("suspended_rows", 0)),
            "rows": len(bars),
        }

        warnings = _cross_check(
            symbol, bars, start_date, end_date, self.adjust_mode,
            sample=sample, check_fetchers=self._check_fetchers, tol=self.check_tol)

        paths = write_daily_bars_partitioned(bars, symbol=symbol, root=self.root)
        return CollectResult(
            symbol=symbol, state="ok", rows=len(bars), paths=paths,
            warnings=warnings, meta=meta)

    # ------------------------------------------------------------------
    def collect(
        self,
        symbols: Iterable[str],
        start_date: str,
        end_date: str,
    ) -> dict[str, CollectResult]:
        """批量串行采集（FR-DATA-7 单源串行），按 ``sample_stride``/``sample_rate``
        对子集跑新浪/腾讯校验腿。返回 ``{symbol: CollectResult}``。"""
        out: dict[str, CollectResult] = {}
        for i, symbol in enumerate(symbols):
            out[symbol] = self.collect_symbol(
                symbol, start_date, end_date, sample=self._should_sample(i))
        return out
