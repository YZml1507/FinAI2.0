#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T201 §6 数据源 —— ``DataFeed`` 协议 + ``ParquetDailyFeed`` 实现。

行情入口只有这一处（四环境同构 SDD-1）：回测/模拟盘/实盘都拿 ``dict[str, Bar]``，
差异收敛到 Feed 实现，⛔ 引擎不许直接读 parquet、不许自造 Bar。

三条硬口径（违反即返工）：

  ① **停牌 = 缺席**（不是 ``None`` 值）：某 symbol 当日在 parquet 里**没有行**即停牌
     ⇒ 它**不出现在** ``get_bars`` 返回的 dict 里。调用方用 ``symbol in bars`` 判定
     可交易（撮合规则 #1 / settle 市值冻结都依赖这一语义）。⛔ 不许填 None、
     ⛔ 不许前向填充（R1 红线：停牌日没有真实行情，填了就是伪造数据）。
  ② **派生列由 cleaner 补**：parquet 落盘列**没有** ``limit_up`` / ``limit_down``
     / ``exdiv`` —— 读入后调 ``data.cleaner.mark_limit_flags`` 补涨跌停，
     调 ``data.cleaner.combine_exdiv_flag`` 补除权（⛔ 不在本模块重造判定规则）。
     防御：帧缺 ``preclose`` 列 ⇒ 该批 Bar 的 ``limit_up``/``limit_down`` = False
     （契约 §6 明示；缺前收无法判定触板，保守不标）。
  ③ **不静默打网**：``trade_calendar`` 未注入 ⇒ ``get_trading_dates`` raise
     ``CalendarNotInjectedError``（契约 §6）。除权事件同理 —— 只认
     ``exdiv_events`` 预注入或显式给 ``exdiv_fetch_fn``；两者都没有时 ``exdiv``
     全 False 并 warning，⛔ 绝不在读 bar 的热路径上偷偷发起网络请求。

金额一律 ``Decimal(str(v))`` 中转（⛔ 禁 ``Decimal(float)``）：parquet 里是
float64，直接喂 Decimal 会把二进制尾差（``10.1`` → ``10.0999999...``）带进账本，
净值就不可复算了。

数据布局（T105 落盘契约，见 ``data/collector.py::write_daily_bars_partitioned``）：
``{root}/{symbol}/{year}.parquet``，``symbol`` 原样即目录名（``sh.600000``）；
落盘列 = ``date open high low close preclose volume amount turn pctChg
tradestatus isST code adjust_mode source``，``date`` 列是 ``datetime.date``。
"""
from __future__ import annotations

import logging
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable

import pandas as pd

from backtest.types import Bar
from data.cleaner import (  # ⛔ 全路径导入（data/__init__.py 为空，T201 §0-6）
    EXDIV_COL,
    LimitFlagsConfig,
    combine_exdiv_flag,
    enforce_tradestatus,
    fetch_exdiv_events,
    mark_limit_flags,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FeedError",
    "CalendarNotInjectedError",
    "DataFeed",
    "ParquetDailyFeed",
    "DEFAULT_BARS_ROOT",
    "DEFAULT_ADJUST_MODE",
]

#: 默认 parquet 根目录（T105 落盘约定）。
DEFAULT_BARS_ROOT = Path("data/daily_bars")

#: ``adjust_mode`` 列缺失时的兜底口径（与 ``types.Bar`` 默认值一致，R4：口径随数据同行）。
DEFAULT_ADJUST_MODE = "hfq"

#: 交易日历注入点签名：``(start, end) -> Iterable[date]``。
TradeCalendarFn = Callable[[_date, _date], Iterable[Any]]


class FeedError(RuntimeError):
    """T201 feed 域错误基类。"""


class CalendarNotInjectedError(FeedError):
    """未注入交易日历就问交易日 —— ⛔ 不静默打网、不静默按自然日糊弄（契约 §6）。"""


# ----------------------------------------------------------------------
# 协议（契约 §6 逐字）
# ----------------------------------------------------------------------

@runtime_checkable
class DataFeed(Protocol):
    """行情源协议（四环境同构：回测读 parquet、实盘读券商推送，接口一致）。

    ``runtime_checkable`` 只校验**方法存在**（Protocol 的运行时能力上限），
    仍以本 docstring 的语义契约为准 —— 尤其 ``get_bars`` 的"停牌 = 键缺席"。
    """

    def get_bars(self, symbols: list[str], date: _date) -> dict[str, Bar]:
        """取当日行情。停牌（当日无行）的 symbol **不在**返回 dict 中。"""
        ...

    def get_trading_dates(self, start: _date, end: _date) -> list[_date]:
        """区间内交易日升序列表（含端点）。"""
        ...

    def current_date(self) -> _date:
        """最近一次 ``get_bars`` 的交易日（引擎推进游标用）。"""
        ...


# ----------------------------------------------------------------------
# Parquet 实现
# ----------------------------------------------------------------------

def _to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    """数值 → ``Decimal``，**必经 ``str``**（⛔ 禁 ``Decimal(float)`` 的尾差污染）。

    None / NaN / 空串 → ``default``（缺列或首日 preclose 缺失时用 0，⛔ 不用 NaN
    ——``Decimal("NaN")`` 参与账本运算会静默毒化整条净值曲线）。
    """
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):      # 非标量（不会发生，纯防御）
        return default
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return default
    return Decimal(text)


def _opt_decimal(value: Any) -> Decimal | None:
    """可选数值 → ``Decimal`` 或 ``None``（T312 扩展列 dividend_yield/market_cap）。

    与 ``_to_decimal`` 同纪律（必经 ``str``，⛔ 禁 ``Decimal(float)``），但**缺失 /
    NaN 返回 ``None`` 而非 0**——``None`` 是"该 symbol 无红利数据"的显式信号，策略层
    据此 fail-closed 跳过；若回 0 会被误读成"股息率=0"而静默入选失败原因。
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return Decimal(text)


def _is_st_value(value: Any) -> bool:
    """``isST`` 列 → bool。baostock 约定 ``'1'`` = ST（落盘是字符串）。"""
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        return False
    return str(value).strip() == "1"


def _canon_dates(series: pd.Series) -> pd.Series:
    """任意日期表示（``date`` / ``Timestamp`` / ``'YYYY-MM-DD'``）→ ``datetime.date`` 列。"""
    return pd.to_datetime(series).dt.date


class ParquetDailyFeed:
    """从 ``{root}/{symbol}/{year}.parquet`` 读日线，补齐派生列后产出 ``Bar``。

    参数
    ----
    root
        分区根目录（默认 ``data/daily_bars``）。
    trade_calendar
        交易日历注入点 ``(start, end) -> Iterable[date]``；``None`` ⇒
        ``get_trading_dates`` raise（契约 §6：⛔ 不静默打网）。
    limit_config
        ``data.cleaner.LimitFlagsConfig``；``None`` 走 cleaner 缺省（主板 ±10% /
        创业板科创板 ±20% / ST ±5%，FR-EXT-6 可配）。
    exdiv_events
        ``{symbol: 除权事件帧}`` 预注入（帧含 ``date`` 列，可选 ``exdiv`` 列；
        形状同 ``data.cleaner.fetch_exdiv_events`` 的返回）。离线测试与
        "先批量取一次除权、再逐日回放"的生产路径都走这里。
    preloaded
        ``{symbol: bars 帧}`` 预加载（跳过磁盘读；测试与内存回放用）。
        命中的 symbol **完全不读 parquet**，帧可跨任意年份。
    exdiv_fetch_fn
        显式打网取除权事件的取数器（签名同 ``baostock_source.fetch``）。
        只有**既没有** ``exdiv_events`` 命中**又**给了本参数时才会发起取数。

    缓存：按 ``(symbol, year)`` 缓存**补完派生列后**的帧，读一次多日复用
    （契约 §6-4）；``preloaded`` 的 symbol 按 ``(symbol, None)`` 缓存一份。
    """

    def __init__(
        self,
        root: Path | str = DEFAULT_BARS_ROOT,
        *,
        trade_calendar: TradeCalendarFn | None = None,
        limit_config: LimitFlagsConfig | None = None,
        exdiv_events: Mapping[str, pd.DataFrame] | None = None,
        preloaded: Mapping[str, pd.DataFrame] | None = None,
        exdiv_fetch_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.root = Path(root)
        self.trade_calendar = trade_calendar
        self.limit_config = limit_config
        self.exdiv_events: dict[str, pd.DataFrame] = dict(exdiv_events or {})
        self.preloaded: dict[str, pd.DataFrame] = {
            s: f.copy() for s, f in (preloaded or {}).items()
        }
        self.exdiv_fetch_fn = exdiv_fetch_fn
        #: (symbol, year|None) → 补完派生列的帧；None 值 = 该分区不存在（负缓存）。
        self._cache: dict[tuple[str, int | None], pd.DataFrame | None] = {}
        #: 被 ``enforce_tradestatus`` 拦下的停牌脏行累计数（R1 血缘，供验收抽查）。
        self.suspended_rows: int = 0
        #: 既无预注入事件、又无取数器 ⇒ 已 warning 过的 symbol（只吼一次）。
        self._exdiv_warned: set[str] = set()
        self._current_date: _date | None = None

    # ------------------------------------------------------------------ 公开 API

    def get_bars(self, symbols: list[str], date: _date) -> dict[str, Bar]:
        """取 ``date`` 当日各 symbol 的 ``Bar``。

        ⭐ **停牌 = 键缺席**：某 symbol 当日无行（停牌 / 未上市 / 已退市 / 分区
        不存在）⇒ 它不在返回 dict 里。⛔ 不填 None、⛔ 不前向填充。
        同日重复行（理论上不该有）取**最后一行**，与落盘去重口径 ``keep='last'`` 一致。
        """
        target = self._as_date(date)
        out: dict[str, Bar] = {}
        for symbol in symbols:
            frame = self._frame_for_range(symbol, target, target)
            if frame is None or frame.empty:
                continue                       # 停牌 = 缺席
            row = frame.iloc[-1]
            out[symbol] = self._row_to_bar(symbol, row)
        self._current_date = target
        return out

    def get_trading_dates(self, start: _date, end: _date) -> list[_date]:
        """区间交易日升序去重列表（含端点），委托注入的日历。

        ⛔ ``trade_calendar`` 未注入 → raise ``CalendarNotInjectedError``
        （契约 §6：不静默打网、不静默按自然日糊弄）。
        日历返回值统一规整成 ``datetime.date`` 并裁到 ``[start, end]``（防御：
        注入实现可能给出多余端点）。
        """
        if self.trade_calendar is None:
            raise CalendarNotInjectedError(
                "trade_calendar 未注入 —— ⛔ 不静默打网取交易日历，也不按自然日"
                "近似（T201 §6）。请构造 ParquetDailyFeed 时传 "
                "trade_calendar=(start, end) -> list[date]")
        s, e = self._as_date(start), self._as_date(end)
        if s > e:
            raise FeedError(f"start {s} 晚于 end {e}（区间倒置，拒绝）")
        raw = self.trade_calendar(s, e)
        dates = {self._as_date(d) for d in raw}
        return sorted(d for d in dates if s <= d <= e)

    def current_date(self) -> _date:
        """最近一次 ``get_bars`` 的交易日；一次都没取过 → raise（⛔ 不返回 today）。"""
        if self._current_date is None:
            raise FeedError(
                "尚未调用过 get_bars，current_date 无定义 —— ⛔ 不静默返回今天")
        return self._current_date

    def clear_cache(self) -> None:
        """丢弃分区缓存（数据被重新落盘后调用）。"""
        self._cache.clear()

    # ------------------------------------------------------------------ 内部：取帧

    @staticmethod
    def _as_date(value: Any) -> _date:
        """统一成 ``datetime.date``（接受 ``date`` / ``Timestamp`` / ``'YYYY-MM-DD'``）。"""
        if isinstance(value, _date) and not isinstance(value, pd.Timestamp):
            return value
        ts = pd.Timestamp(value)
        if pd.isna(ts):
            raise FeedError(f"无法解析日期 {value!r}")
        return ts.date()

    def _frame_for_range(
        self, symbol: str, start: _date, end: _date,
    ) -> pd.DataFrame | None:
        """取 ``symbol`` 在 ``[start, end]`` 内的（已补派生列的）行；无行返回 ``None``。

        跨年自动拼多个 ``{year}.parquet`` 分区（契约 §6-5）：按年逐个取缓存帧，
        concat 后按日期过滤 —— 缺某年分区不算错（那几年没数据而已）。
        """
        parts: list[pd.DataFrame] = []
        if symbol in self.preloaded:
            enriched = self._enriched(symbol, None)
            if enriched is not None:
                parts.append(enriched)
        else:
            for year in range(start.year, end.year + 1):
                enriched = self._enriched(symbol, year)
                if enriched is not None:
                    parts.append(enriched)
        if not parts:
            return None
        frame = parts[0] if len(parts) == 1 else pd.concat(parts, ignore_index=True)
        mask = (frame["_date"] >= start) & (frame["_date"] <= end)
        sub = frame.loc[mask]
        if sub.empty:
            return None
        return sub.sort_values("_date")

    def _enriched(self, symbol: str, year: int | None) -> pd.DataFrame | None:
        """``(symbol, year)`` 的补派生列帧（带缓存；``None`` = 该分区不存在）。"""
        key = (symbol, year)
        if key in self._cache:
            return self._cache[key]
        raw = self._read_raw(symbol, year)
        frame = None if raw is None else self._derive(symbol, raw)
        self._cache[key] = frame
        return frame

    def _read_raw(self, symbol: str, year: int | None) -> pd.DataFrame | None:
        """读原始帧：``preloaded`` 命中直接用（year=None），否则读该年分区文件。"""
        if year is None:
            preset = self.preloaded.get(symbol)
            return None if preset is None else preset.copy()
        path = self.root / symbol / f"{year}.parquet"
        if not path.exists():
            return None                        # 缺分区 ≠ 出错：那年就是没数据
        return pd.read_parquet(path, engine="pyarrow")

    # ------------------------------------------------------------------ 内部：派生列

    def _derive(self, symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
        """补 ``limit_up`` / ``limit_down`` / ``exdiv`` + 归一 ``_date`` 排序列。

        顺序：停牌脏行防御过滤（R1）→ 涨跌停标记 → 除权标记。
        ⛔ 不做任何价格填充/修补 —— 本模块只读、只标记。
        """
        if "date" not in frame.columns:
            raise FeedError(
                f"{symbol} 的 bars 帧缺 date 列（实际列：{list(frame.columns)}）"
                f"—— ⛔ 缺列即拒绝，不猜索引当日期")
        out = frame.copy()
        # ① R1 防御：落盘时已按 tradestatus=='1' 滤过，这里是纵深防御（幂等）。
        #    ⛔ 有列才滤 —— 缺列不伪造 tradestatus（伪造 = 把停牌日当交易日）。
        if "tradestatus" in out.columns:
            out, dropped = enforce_tradestatus(out)
            self.suspended_rows += int(dropped)
        # ② 涨跌停（cleaner 唯一判定点，⛔ 不在 feed 重造规则）。
        if "code" not in out.columns:
            out["code"] = symbol               # 分区目录名即 code，不算伪造
        if "preclose" in out.columns:
            out = mark_limit_flags(out, self.limit_config)
        else:
            # 契约 §6 明示的防御分支：缺前收 ⇒ 触板不可判定 ⇒ 保守双 False。
            logger.warning(
                "%s 的 bars 帧缺 preclose 列，limit_up/limit_down 全部置 False"
                "（涨跌停不可判定）", symbol)
            out["limit_up"] = False
            out["limit_down"] = False
        # ③ 除权（FR-BT-4：结算依赖，⛔ 取数失败绝不伪装成"无除权"—— 由 cleaner raise）。
        out = self._apply_exdiv(symbol, out)
        out["_date"] = _canon_dates(out["date"])
        return out.sort_values("_date").reset_index(drop=True)

    def _apply_exdiv(self, symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
        """把除权事件合并进帧（``exdiv`` bool 列）。

        取事件的优先级：``exdiv_events[symbol]`` 预注入 → ``exdiv_fetch_fn`` 显式
        取数 → 都没有则全 False + 一次性 warning。⛔ 默认不打网（与
        ``trade_calendar`` 同纪律）；取数器给了但失败 ⇒ cleaner 会 raise
        ``ExdivSourceError``，本层**不吞**。
        """
        events = self.exdiv_events.get(symbol)
        if events is None and self.exdiv_fetch_fn is not None:
            dates = _canon_dates(frame["date"])
            if not dates.empty:
                events = fetch_exdiv_events(
                    symbol,
                    dates.min().isoformat(),
                    dates.max().isoformat(),
                    fetch_fn=self.exdiv_fetch_fn,
                )
                self.exdiv_events[symbol] = events      # 同 symbol 只取一次
        if events is None:
            if symbol not in self._exdiv_warned:
                self._exdiv_warned.add(symbol)
                logger.warning(
                    "%s 无除权事件数据（未预注入 exdiv_events、未给 exdiv_fetch_fn）"
                    "⇒ exdiv 全部 False。除权日结算（FR-BT-4）需要事件数据，"
                    "回测前请注入。", symbol)
            out = frame.copy()
            out[EXDIV_COL] = False
            return out
        return combine_exdiv_flag(frame, events)

    # ------------------------------------------------------------------ 内部：建 Bar

    def _row_to_bar(self, symbol: str, row: pd.Series) -> Bar:
        """一行帧 → ``Bar``：数值走 ``Decimal(str(v))``，标记落 Python ``bool``。"""
        def _flag(name: str) -> bool:
            value = row.get(name, False)
            try:
                if pd.isna(value):
                    return False
            except (TypeError, ValueError):
                return False
            return bool(value)

        adjust_mode = row.get("adjust_mode", DEFAULT_ADJUST_MODE)
        try:
            if pd.isna(adjust_mode):
                adjust_mode = DEFAULT_ADJUST_MODE
        except (TypeError, ValueError):
            adjust_mode = DEFAULT_ADJUST_MODE

        return Bar(
            date=self._as_date(row["_date"] if "_date" in row else row["date"]),
            symbol=symbol,
            open=_to_decimal(row.get("open")),
            high=_to_decimal(row.get("high")),
            low=_to_decimal(row.get("low")),
            close=_to_decimal(row.get("close")),
            preclose=_to_decimal(row.get("preclose")),
            volume=_to_decimal(row.get("volume")),
            amount=_to_decimal(row.get("amount")),
            limit_up=_flag("limit_up"),
            limit_down=_flag("limit_down"),
            exdiv=_flag(EXDIV_COL),
            is_st=_is_st_value(row.get("isST")),
            adjust_mode=str(adjust_mode),
            # ⭐ T312：parquet 原生扩展列直通（红利策略选股域）。缺列 → None
            # （策略层 fail-closed 跳过该 symbol，见 candidates._select_stocks）。
            dividend_yield=_opt_decimal(row.get("dividend_yield")),
            market_cap=_opt_decimal(row.get("market_cap")),
        )
