#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""同花顺涨停池适配器 —— 把 `data.10jqka.com.cn/dataapi/limit_up/limit_up_pool`
接成 `catalog_source.fetch(name, **kwargs)` 同形入口，供 `capability_router.get()` 走旁路调用。

⭐ 为什么存在这一层（实测依据，⛔ 不是推定）：

  `capability_router.get()` 的降级链**强制**走 `catalog_source.fetch(c.key)`（
  `capability_router.py:1471`，2026-08-14 实测）。而 `catalog_source.fetch()` 靠
  `_index()` 查实测台账记录—— 同花顺涨停池**不在台账索引里**（akshare 无此接口、
  a-stock-data 项目有 `ths_limit_up_pool` 实现但未收编）⇒ `KeyError`。

  ⇒ 本模块是让同花顺涨停池候选能接入 `CAPABILITIES["limit_list"]` 的唯一路径：
     不动 `catalog_source`（尊重它"只查台账"的设计），不动 `get()` 的既有任何
     候选行为（旁路分流，非侵入），把 HTTP JSON 取数 + 字段映射接成同形 `fetch()`。

⭐ 实测真相（本轮亲测，2026-08-14，⛔ 不轻信转交文档）：

  · 端点：`https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool`
    （任务文档说 `basic.10jqka.com.cn` —— ⛔ **错了**，实测真端点是
     `data.10jqka.com.cn`，`basic` 那台是一致预期 EPS 不是涨停池）
  · 参数：`page/limit/field(内部字段 ID 串)/filter=HS,GEM2STAR/order_field/order_type/date=YYYYMMDD`
  · 返回：JSON `data.info[]`，每只含
    `code/name/latest(价)/change_rate/reason_type(涨停原因题材)/limit_up_type(板型)/
     limit_up_suc_rate(封板成功率)/open_num(炸板次数)/order_amount(封单额)/
     high_days(几天几板)/first_limit_up_time(Unix 秒时间戳)/is_again_limit(是否回封)`
  · 本轮实测 `date=20260813 limit=5` ⇒ `status=200 / bytes=6154 / info count=5` ⇒ **可用**
  · 同轮主源 `akshare::stock_zt_pool_em(date=20260813)` ⇒ **59 行**（可达，字段不同 schema 分开）

⭐ schema 决策（本轮自决，依据实测）：

  `limit_list` 现有 2 候选 schema **不同**（`limit_list_d` vs `zt_pool_em`，台账明写
  "字段与 tushare 口径不同，schema 分开"）。同花顺返回字段与前两者都不同 ⇒ 建**新 schema
  `ths_limit_up`**，不复用任一现有 schema。
  ⇒ ⚠ 这意味着同花顺候选**不会与主源自动 fallback**（schema 锁不同）—— 但这正是
     `FINDING-343` 的正确做法：跨 schema 静默降级会把"涨停名单"和"涨停揭秘"两种
     不同数据混着给调用方。调用方要同花顺数据请**显式** `schema="ths_limit_up"`。
  ⇒ T2 的真独立价值不在 fallback，在 `independence_basis` 从 "mirror-only" 升到
     "verified"：当前 limit_list 2 候选里 `citydata` 是 `origin_verified=False` 镜像 ⇒
     必须算上它才够 2 域 ⇒ mirror-only。加同花顺 `origin_verified=True` 后，
     仅 verified 集就跨 ≥2 域 ⇒ verified。

⭐ 等价性判据：涨停股票集合交集率 ≥ 90%（允许同花顺与东财对"涨停"定义小幅差异，
   如东财含一字板/T 字板细分，同花顺侧重题材归因）。本轮主源 59 只 vs 同花顺实测
   5 只（limit=5 截断）⇒ 集合交集实测需 full limit=200，延后到收尾验证。

Author: Phase 2 T2
FINDING: R43 Phase 2 / FINDING-347（strict 备用源不足）/ FINDING-407-NEW-1（mirror-only vs verified）
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

import pandas as pd
import requests

from finai.sources.base import (
    EMPTY_OK,
    FetchResult,
    classify_exception,
    make_result,
)

logger = logging.getLogger(__name__)

SOURCE = "ths"  # 同花顺 fault domain（与 scripts/gen_probe_host_attribution.py:54 一致）

#: 同花顺涨停池端点（实测，2026-08-14）
_ENDPOINT = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"

#: a-stock-data 实测可用的 field 串（内部字段 ID，照抄即可）
_FIELD = ("199112,10,9001,330323,330324,330325,9002,330329,"
          "133971,133970,1968584,3475914,9003,9004")

#: `ths_limit_up` schema 契约列（实测同花顺返回映射后）
_THS_LIMIT_UP_COLS = [
    "code", "name", "price", "pct_change",
    "reason", "board_type", "seal_rate", "break_times",
    "seal_amount", "high_days", "first_time", "is_again",
]

#: 默认 User-Agent（同花顺端点实测 200，无需 Referer —— 与一致预期 EPS 那个不同）
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

#: 默认取数上限（a-stock-data 用 200，单页足够覆盖全市场涨停股）
_DEFAULT_LIMIT = 200

#: 请求超时（秒）
_TIMEOUT = 15


def _to_date_str(v: Any) -> str | None:
    """`YYYY-MM-DD` / `YYYYMMDD` / `datetime` / `date` → `YYYYMMDD`（同花顺要的格式）。"""
    if v is None:
        return None
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.strftime("%Y%m%d")
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    return None


def _normalize_to_ths_limit_up(info: list[dict]) -> pd.DataFrame:
    """同花顺 `data.info[]` → `ths_limit_up` schema 契约列。

    ⚠ `first_limit_up_time` 是 **Unix 秒时间戳**（实测 `'1786585729'`），不是 HHMMSS
       ——要 `datetime.fromtimestamp` 转 `HH:MM:SS`（a-stock-data 文档明示此坑）。
    """
    if not info:
        return pd.DataFrame(columns=_THS_LIMIT_UP_COLS)

    rows = []
    for it in info:
        ft = it.get("first_limit_up_time")
        first_time = ""
        if ft:
            try:
                first_time = datetime.datetime.fromtimestamp(int(ft)).strftime("%H:%M:%S")
            except (ValueError, TypeError, OSError):
                first_time = ""
        rows.append({
            "code": it.get("code", ""),
            "name": it.get("name", ""),
            "price": it.get("latest"),
            "pct_change": it.get("change_rate"),
            "reason": it.get("reason_type", ""),
            "board_type": it.get("limit_up_type", ""),
            "seal_rate": it.get("limit_up_suc_rate"),
            "break_times": it.get("open_num") or 0,
            "seal_amount": it.get("order_amount"),
            "high_days": it.get("high_days", ""),
            "first_time": first_time,
            "is_again": it.get("is_again_limit"),
        })
    return pd.DataFrame(rows, columns=_THS_LIMIT_UP_COLS)


def fetch(name: str, /, **kwargs: Any) -> FetchResult:
    """与 `catalog_source.fetch(name, **kwargs)` 同形入口 —— 供 `capability_router.get()` 旁路调。

    Args:
        name: 必须以 `ths::` 前缀开头（由 `get()` 的旁路分流守卫，本模块不重复校验）。
        **kwargs: 统一参数 `trade_date`（与 `limit_list` 候选同契约）。

    Returns:
        `FetchResult` —— 七态判定，归一化后按 `ths_limit_up` schema 返。
        ⛔ 0 行归 `EMPTY_OK` 而非 `OK`（`FINDING-178`：静默 0 行不得当成功）。
    """
    date = kwargs.get("trade_date") or kwargs.get("date")
    date_str = _to_date_str(date)
    if not date_str:
        return FetchResult(
            state="FAIL_PROBE_BUG", source=SOURCE,
            detail="ths_limit_up_adapter.fetch: missing required `trade_date` kwarg "
                   "(or unparseable date value)",
            evidence={"name": name, "kwargs": list(kwargs)},
        )

    params = {
        "page": 1, "limit": _DEFAULT_LIMIT,
        "field": _FIELD,
        "filter": "HS,GEM2STAR",  # 沪深主板 + 创业板 + 科创板（a-stock-data 实测）
        "order_field": "330324", "order_type": "0",
        "date": date_str,
    }
    try:
        r = requests.get(_ENDPOINT, params=params,
                         headers={"User-Agent": _UA}, timeout=_TIMEOUT)
    except BaseException as exc:  # noqa: BLE001 — 一个候选炸掉不该打死整条链
        return FetchResult(
            state=classify_exception(exc), source=SOURCE,
            detail=f"ths_limit_up_adapter: {type(exc).__name__}: {str(exc)[:200]}",
            evidence={"name": name, "date": date_str, "endpoint": _ENDPOINT},
        )

    if r.status_code != 200:
        return FetchResult(
            state="FAIL_GATEWAY" if r.status_code in (502, 503, 504) else "FAIL_DETERMINISTIC",
            source=SOURCE,
            detail=f"HTTP {r.status_code}: {r.text[:200]}",
            evidence={"name": name, "date": date_str, "endpoint": _ENDPOINT,
                      "bytes_received": len(r.content)},
        )

    try:
        j = r.json()
    except Exception as exc:  # noqa: BLE001
        return FetchResult(
            state="FAIL_GATEWAY", source=SOURCE,
            detail=f"JSON parse fail: {type(exc).__name__}: {str(exc)[:200]}",
            evidence={"name": name, "date": date_str, "bytes_received": len(r.content)},
        )

    info = ((j.get("data") or {}).get("info")) or []
    frame = _normalize_to_ths_limit_up(info)
    return make_result(
        frame,
        source=SOURCE,
        evidence={
            "name": name, "trade_date": date_str, "endpoint": _ENDPOINT,
            "rows": len(frame), "schema": "ths_limit_up",
        },
    )
