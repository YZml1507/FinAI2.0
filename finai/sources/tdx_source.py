#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TDX（通达信）适配器 —— **包装项目已有的连接层，不造平行实现**。

⭐ 关键决定（实测依据）：项目里已有一套正确的 TDX 连接实现
`finai/tdx_minute5.py::_connect_tdx_api()`，R28 实测可用 ——
返回 **48 根**、首根 `09:35`、末根 `15:00`（栅格完全合规），且已具备：
  · 健康节点缓存（`data/tdx_healthy_nodes.json`，TTL 1 小时，实测存在）
  · `get_security_count > 0` 健全性检查
  · 空响应视为失败并换下一节点
  · 10 台内置服务器（实测 **3/10 可连**：218.75.126.9 / 115.238.56.198 / 60.12.136.250）
故本模块**只做封装与状态归类**，⛔ 不重写服务器选择逻辑。

封装的陷阱：
`FINDING-178`/`FINDING-189`：mootdx 的 `StdQuotes()` 默认 `bestip=False`，
  既不 pin 服务器也不探测最佳 IP，实测 daily 与 5min **恒返回 0 行且不抛异常**；
  而生产代码 `phase1/data_collector/mootdx.py:120` 的 `if df.empty: return []`
  会把它**静默吞掉**（该兜底通道因此从未真正生效）。
  → 本模块一律走 `_connect_tdx_api()`，并把 0 行归 `EMPTY_OK` 而非成功。

⭐ category 编码为**实测值**（2026-08-05，在 60.12.136.250 上逐个验证间隔）：
    0 → 5min      1 → 15min     7 → 1min      8 → 1min      9 → daily
  ⚠ 与 mootdx 文档声称的 "8=5min" **不符**，与 `finai/tdx_minute5.py:846` 的
  代码注释（0=5min/7=1min/9=daily）一致。⛔ 不要按文档改，按实测。
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from finai.sources.base import (
    FetchResult,
    classify_exception,
    make_result,
)

SOURCE = "tdx"

#: 实测的 category → bar 间隔映射（2026-08-05 在实盘服务器上逐个验证）
CATEGORY_5MIN = 0
CATEGORY_15MIN = 1
CATEGORY_1MIN = 7
CATEGORY_DAILY = 9

_CATEGORY_MEASURED = {
    CATEGORY_5MIN: "5min",
    CATEGORY_15MIN: "15min",
    CATEGORY_1MIN: "1min",
    8: "1min",
    CATEGORY_DAILY: "daily",
}

#: 单次请求最多 800 根（TDX 协议上限）
MAX_BARS_PER_REQUEST = 800


def _market_id(code: str) -> int:
    """沪深北市场编号。复用生产实现，避免第 N 份代码身份归一化（`FINDING-20` 教训）。"""
    from finai.tdx_minute5 import _market_for_code

    return _market_for_code(code.split(".")[0])


def connect() -> Any:
    """取一个已验证可用的 TDX 连接。⛔ 不要自己 new TdxHq_API 或用 StdQuotes()。"""
    from finai.tdx_minute5 import _connect_tdx_api

    return _connect_tdx_api()


def fetch_bars(
    code: str,
    *,
    category: int = CATEGORY_5MIN,
    count: int = 48,
    client: Any | None = None,
) -> FetchResult:
    """取 K 线。

    Args:
        code: `000001` 或 `000001.SZ` 均可。
        category: 用模块常量，**不要写字面量** —— 实测 0=5min / 1=15min /
            7=1min / 8=1min / 9=daily，与 mootdx 文档不符（见模块 docstring）。
        count: bar 条数（不是日期区间）。单次上限 800，超出会自动分页。

    实测基线（2026-08-05）：`category=0, count=48` → **48 行**，
    首根 `09:35`、末根 `15:00`；`category=9, count=20` → **20 行**。

    ⛔ 0 行返回 `EMPTY_OK` 而非成功 —— `FINDING-178`/`-189` 的核心教训：
    静默 0 行与"确实无数据"在返回值上无法区分。
    """
    if category not in _CATEGORY_MEASURED:
        return FetchResult(
            state="FAIL_PROBE_BUG", source=SOURCE,
            detail=(f"category={category} 未经实测。已验证的取值: "
                    f"{sorted(_CATEGORY_MEASURED)} → {_CATEGORY_MEASURED}"),
        )
    own = client is None
    api = None
    try:
        api = client or connect()
        pure = code.split(".")[0]
        market = _market_id(code)
        frames: list[pd.DataFrame] = []
        remaining = count
        start = 0
        while remaining > 0:
            take = min(remaining, MAX_BARS_PER_REQUEST)
            data = api.get_security_bars(category, market, pure, start, take)
            if not data:
                break  # 无更多数据；总量由下方 make_result 判 EMPTY_OK
            frames.append(pd.DataFrame(data))
            got = len(data)
            start += got
            remaining -= got
            if got < take:
                break
        frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        return make_result(frame, source=SOURCE, evidence={
            "code": code, "category": category,
            "interval_measured": _CATEGORY_MEASURED[category],
            "requested": count,
        })
    except BaseException as exc:  # noqa: BLE001
        return FetchResult(
            state=classify_exception(exc), source=SOURCE,
            detail=f"{type(exc).__name__}: {str(exc)[:200]}",
            evidence={"code": code, "category": category},
        )
    finally:
        if own and api is not None:
            try:
                api.disconnect()
            except Exception:  # noqa: BLE001, S110
                pass
