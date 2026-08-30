#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TDX 日线备用源适配器 —— 把 `tdx_source.fetch_bars(count=…)` 接成
`catalog_source.fetch(name, **kwargs)` 同形入口，供 `capability_router.get()` 走旁路调用。

⭐ 为什么存在这一层（实测依据，⛔ 不是推定）：

  `capability_router.get()` 的降级链**强制**走 `catalog_source.fetch(c.key)`（
  `capability_router.py:1438`，2026-08-14 实测）。而 `catalog_source.fetch()` 在
  `:329-331` 明写**拒绝**走它调 tdx —— 因为 tdx 需会话初始化 + 主机选择，那两个坑
  已由 `tdx_source.py` 封好（`_connect_tdx_api()` 健康节点缓存 / `StdQuotes()` 不 pin
  ⇒ 恒 0 行的 `FINDING-178`/`-189`）。

  ⇒ 本模块是**唯一**让 tdx 候选能接入 `CAPABILITIES["daily_bar"]` 的路径：
     不动 `catalog_source`（尊重它"tdx 不走本入口"的设计），不动 `get()` 的既有任何
     候选行为（旁路分流，非侵入），只把 tdx 的 `fetch_bars(code, *, category, count)`
     接成 `fetch(name, *, symbol, start_date, end_date)` 同形契约。

⭐ 参数换算（`count` 不是日期区间）：

  `tdx_source.fetch_bars()` 按 `count` 从最新往回取条数（TDX 协议上限），**无** start_date/
  end_date 入参。本模块把统一参数 `(symbol, start_date, end_date)` 换算成：
    ① 区间天数 ≈ `(end_date - start_date).days * 1.5`（A 股年均交易日 ~242，日历日 365，
       1.5 倍覆盖除周末/节假日后的实际交易日 + 余量，⛔ 不写死 252 —— `FINDING-139` 已实测
       A 股年均 242.88 个交易日，写死会偏低）
    ② 取数后**本地按日期过滤**到 `[start_date, end_date]`（`tdx_source` 返回含 `datetime` 列）

  ⛔ 这与 `Candidate.local_filter` 的设计意图同形 —— 服务端做不到、本层等价完成。但本模块
     不是 `Candidate`，是独立 `fetch()` 入口 ⇒ 过滤逻辑在本模块内。

⭐ schema 对齐（`ohlcv_daily`，与主源 akshare/efinance 同 schema 才能自动 fallback）：

  `tdx_source.fetch_bars()` 返回的 DataFrame 列实测（2026-08-14，`StdQuotes().bars` 返）
  为 `['open','close','high','low','vol','amount','year','month','day','hour','minute',
  'datetime','volume']`。本模块归一化成 `ohlcv_daily` 契约列：
    `['date','open','high','low','close','volume','amount']`
  （与 `akshare::stock_zh_a_hist(adjust='')` 不复权日线同列名同单位 —— 元 vs 元、
   股 vs 股，⭐ 实测 `vol == volume` 同值）。

⭐ 复权：本模块**不**做。mootdx 返回不复权价，与主源 akshare 钉死 `adjust=''` 不复权对齐
   ⇒ schema 一致、复权逻辑零新增。复权留给下游 `cross_freq_adjustment.qfq = raw * f[t] / f_epoch`
   （已有基建，`adjustment_factors` 表不存在 —— 2026-08-14 实测 `phase1/finai.db` 42 表
   无一含 adjust；复权因子走 `data/cold/parquet/tushare/adj_factor_v1/` 冷库 Parquet）。

Author: Phase 2 T1
FINDING: R43 Phase 2 / FINDING-347（strict 备用源不足）/ FINDING-402（akshare/efinance 假冗余）
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

import pandas as pd

from finai.sources import tdx_source
from finai.sources.adjustment_mode import AdjustmentMode, UnknownAdjustment
from finai.sources.base import (
    EMPTY_OK,
    FetchResult,
    classify_exception,
    make_result,
)
from finai.sources.tdx_source import CATEGORY_DAILY, MAX_BARS_PER_REQUEST, SOURCE

logger = logging.getLogger(__name__)

#: TDX 协议单次最多 800 根（`tdx_source.MAX_BARS_PER_REQUEST`），日线区间最长 ~10 年
#: ⇒ 单次足够覆盖回测常规区间。超出由 `tdx_source.fetch_bars` 自动分页。
_DEFAULT_COUNT_MULTIPLIER = 1.5  # 见模块 docstring：日历日 → 交易日换算 + 余量
_MAX_COUNT = MAX_BARS_PER_REQUEST * 5  # 上限 4000 根 (~15 年日线)，防误传巨参打爆协议

#: `ohlcv_daily` 契约列名（与 akshare::stock_zh_a_hist(adjust='') 对齐）
_OHLCV_DAILY_COLS = ["date", "open", "high", "low", "close", "volume", "amount"]


def _to_date(v: Any) -> datetime.date | None:
    """把 `YYYY-MM-DD` / `YYYYMMDD` / `datetime` / `date` 归一成 `date`。返 `None` = 无法判。"""
    if v is None:
        return None
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _count_from_range(start: Any, end: Any) -> int:
    """`(start_date, end_date)` → TDX `count`（从最新往回取的条数）。

    策略：区间日历日天数 × 1.5（覆盖实际交易日 + 余量），上限 `_MAX_COUNT`。
    ⛔ 不写死 252/年（`FINDING-139`：A 股实测年均 242.88 交易日）。
    无 `start` 时返 `_MAX_COUNT`（取尽量多，本地再按 `end` 截）。
    """
    s = _to_date(start)
    e = _to_date(end) or datetime.date.today()
    if s is None:
        return _MAX_COUNT
    days = max((e - s).days, 1)
    return min(int(days * _DEFAULT_COUNT_MULTIPLIER) + 10, _MAX_COUNT)


def _normalize_to_ohlcv_daily(frame: pd.DataFrame, code: str) -> pd.DataFrame:
    """`tdx_source` 返回帧 → `ohlcv_daily` 契约列。

    实测 tdx bars 列：`open/close/high/low/vol/amount/year/month/day/hour/minute/datetime/volume`。
    ⚠ `vol` 与 `volume` 同值（实测 632950.0 == 632950.0）⇒ 取 `volume`（更契约化）。
    """
    if frame is None or frame.empty:
        return pd.DataFrame(columns=_OHLCV_DAILY_COLS)

    out = pd.DataFrame()
    # date：从 `datetime` 列截到 YYYY-MM-DD（实测 `datetime` 是 '2026-08-14 15:00' 字符串）
    dt_col = frame["datetime"].astype(str) if "datetime" in frame.columns else None
    out["date"] = dt_col.str[:10] if dt_col is not None else None
    for col in ("open", "high", "low", "close"):
        out[col] = frame[col] if col in frame.columns else pd.NaT
    # volume：优先 volume 列（与 vol 同值），回落 vol
    out["volume"] = frame["volume"] if "volume" in frame.columns else (
        frame["vol"] if "vol" in frame.columns else pd.NaT
    )
    out["amount"] = frame["amount"] if "amount" in frame.columns else pd.NaT
    return out


def _filter_by_range(frame: pd.DataFrame, start: Any, end: Any) -> pd.DataFrame:
    """本地按 `[start_date, end_date]` 过滤（tdx 服务端只按 count 取，不按区间）。"""
    if frame.empty or "date" not in frame.columns:
        return frame
    s = _to_date(start)
    e = _to_date(end)
    mask = pd.Series([True] * len(frame), index=frame.index)
    if s is not None:
        mask &= frame["date"] >= s.isoformat()
    if e is not None:
        mask &= frame["date"] <= e.isoformat()
    return frame[mask]


def fetch(name: str, /, **kwargs: Any) -> FetchResult:
    """与 `catalog_source.fetch(name, **kwargs)` 同形入口 —— 供 `capability_router.get()` 旁路调。

    Args:
        name: 必须以 `tdx::` 前缀开头（由 `get()` 的旁路分流守卫，本模块不重复校验）。
        **kwargs: 统一参数 `symbol` / `start_date` / `end_date`（与 `ohlcv_daily` 候选同契约）。

    Returns:
        `FetchResult` —— 复用 `tdx_source.fetch_bars()` 的七态判定，归一化后按区间过滤。
        ⛔ 0 行归 `EMPTY_OK` 而非 `OK`（`FINDING-178` 的教训：静默 0 行不得当成功）。
    """
    symbol = kwargs.get("symbol") or kwargs.get("code")
    if not symbol:
        return FetchResult(
            state="FAIL_PROBE_BUG", source=SOURCE,
            detail="tdx_daily_bar_adapter.fetch: missing required `symbol` kwarg",
            evidence={"name": name, "kwargs": list(kwargs)},
        )
    # ⭐ R4 §3.5：把"TDX 无复权参数 = RAW"从隐式变显式 —— 任何非 RAW 口径请求
    #   在此被拒（⛔ 不许装作支持），防止"调用方以为拿到前复权、实际是不复权"的静默漂移。
    if kwargs.get("adjustment") not in (None, AdjustmentMode.RAW):
        raise UnknownAdjustment(
            f"tdx::daily_bar 走 TDX 协议**无复权参数**，只能 RAW（"
            f"adjustment={kwargs.get('adjustment')!r} 无法映射，R4 §3.5）")
    start = kwargs.get("start_date")
    end = kwargs.get("end_date")
    count = _count_from_range(start, end)

    try:
        res = tdx_source.fetch_bars(
            code=str(symbol),
            category=CATEGORY_DAILY,
            count=count,
            client=None,  # 让适配器内部自管连接（复用 _connect_tdx_api 健康节点缓存）
        )
    except BaseException as exc:  # noqa: BLE001 — 一个候选炸掉不该打死整条链
        return FetchResult(
            state=classify_exception(exc), source=SOURCE,
            detail=f"tdx_daily_bar_adapter: {type(exc).__name__}: {str(exc)[:200]}",
            evidence={"name": name, "symbol": symbol, "count": count},
        )

    if res.state != "OK" or res.frame is None or res.frame.empty:
        # 透传 tdx_source 的判定（EMPTY_OK / FAIL_* 等），只补 evidence
        return FetchResult(
            state=res.state, source=SOURCE,
            detail=res.detail,
            evidence={**(res.evidence or {}), "name": name, "symbol": symbol, "count": count},
        )

    frame = _normalize_to_ohlcv_daily(res.frame, str(symbol))
    frame = _filter_by_range(frame, start, end)
    # ⭐ 过滤后可能为空 —— 与 `capability_router.get()` 的 `EMPTY_AFTER_LOCAL_FILTER` 同性质：
    #   0 行不算成功，让 `get()` 继续试下一个候选。
    return make_result(
        frame,
        source=SOURCE,
        evidence={
            "name": name, "symbol": symbol, "start_date": start, "end_date": end,
            "count_requested": count, "rows_before_filter": len(res.frame),
            "interval_measured": "daily",
        },
    )
