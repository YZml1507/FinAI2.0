#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TDX **扩展市场**（港股 / 期货 / 期权 / 基金 / 宏观）适配器 —— L0 数据源层。

与 `tdx_source`（沪深北 A 股，走 `hq` 协议 7709）的分工：
本模块走 **`exhq` 协议**，覆盖 A 股之外的 17+ 个市场。

⭐ 实测能力（`FINDING-250`）：目录自报 **82,879 个标的 / 31 个市场**，
  免 token、免代理。已确认有合约的市场（本次翻到 60,000 条样本）：
    开放式基金 30,186 ┆ 临时股 17,682 ┆ 股份转让 4,062 ┆ 香港主板 2,703
    货币型基金 1,099 ┆ 港股通 961 ┆ 个股/中金所/深圳期权 752/726/494
    上海期货 352 ┆ 郑州商品 292 ┆ 大连商品 278 ┆ B股转H股 204
    宏观指标 99 ┆ 中金所期货 65 ┆ 香港指数 41
  ⚠ `get_markets` 另报 **74 美国股票**、40 中国概念股、62 中证指数 等，
    但它们**未出现**在本次样本里（可能在未翻到的后 22,879 条）
    ⇒ ⛔ **未证实也未否证**，不得当作已有能力宣传。

⭐ **5min 深度实测**（`FINDING-251`）：港股 5min 可回溯到 **2015-11-30**
  （倍增探底 + 二分：最深 offset 172,800，约 17.28 万根 ≈ 10.7 年）。
  ⚠ 期货/期权受**合约生命周期**限制（`IC2608` 仅 1,632 根），这是正确行为而非缺陷。

⛔⛔ **三个曾把我骗过的坑，写在这里以免下一个人重踩**：
  1. **主机换了**（`FINDING-247`）：旧 `112.74.214.43:7727` **连得上但没服务** ——
     `connect()` 成功、每个方法 0 行。mootdx 源码 `consts.py:90-103` 把 9 个
     7727 主机**全注释掉了**，活主机在 **7720**。
     ⭐ 半死的端点比死透的端点更危险：它给出"合法的空"，
       而六态契约会把"合法的空"当成关于**数据**的结论，不是关于**连接**的结论。
  2. **market 与 symbol 必须配对**（`FINDING-246`）：两个值各自合法、组合非法
     （`market=1` 临时股 + `000001` A 股代码）⇒ 静默 0 行。
     标的必须取自**同源目录接口** `get_instrument_info`，不得手工猜。
  3. **库的免责 warning 不是可用性证据**：mootdx 在 `ExtQuotes.__init__` 里
     **无条件**打印"目前扩展市场行情接口已经失效"。我据此判过"整族失效"，
     **是错的** —— 无条件打印意味着它对成功调用也打，故不携带任何可用性信息。
     ⭐ 判"上游已死"只能用**逐方法实测**，且入参本身要先被证明合法。

⛔ **未验正确性**：本模块只证"取得到、有行数字段"。**没有**与已有源交叉对账，
  **没有**做 PIT 检查。按 `FINDING-232`（330 只 BJ 标的被误映射为 `.SH`）的教训，
  入库前必须先双源对账。CLAUDE.md：**A green gate does not mean the data is correct.**
"""
from __future__ import annotations

import os
from typing import Any

import pandas as pd

from finai.sources.base import (
    FetchResult,
    classify_exception,
    make_result,
)

SOURCE = "tdx_ext"

#: 活主机（`FINDING-247` 逐台实测：仅此一台 `get_markets` 返回 31 行）。
#: ⭐ 留环境变量覆盖 —— 反证腿必须能跨子进程边界（同 `FINDING-189` 的理由）。
DEFAULT_HOST = os.environ.get("FINAI_TDX_EXHQ_HOST_OVERRIDE") or "47.112.95.207"
DEFAULT_PORT = int(os.environ.get("FINAI_TDX_EXHQ_PORT_OVERRIDE") or 7720)

#: TDX 周期编码 —— 与 `tdx_source` **同一套**（实测，⛔ 不要按 mootdx 文档改）。
CATEGORY_5MIN = 0
CATEGORY_15MIN = 1
CATEGORY_DAILY = 9

#: 单次请求上限 700（实测：请求 800 恒返回 700）。
#: ⛔ 这是**响应上限**，不是历史深度 —— 我曾把首页 700 根读成"只有 2 周历史"，
#:   显式请求 off=11200 才发现仍是满页。翻页不足即下结论，本轮犯了三次。
MAX_BARS_PER_REQUEST = 700

#: 已实测有合约的 market（`FINDING-250`）。⛔ 不在此表里的 market **未经实测**，
#: 传入不报错但可能 0 行 —— 那是"未测"，不是"无数据"。
MARKETS_MEASURED = {
    1: "临时股", 7: "中金所期权", 8: "个股期权", 9: "深圳期权",
    27: "香港指数", 28: "郑州商品", 29: "大连商品", 30: "上海期货",
    31: "香港主板", 33: "开放式基金", 34: "货币型基金", 38: "宏观指标",
    43: "B股转H股", 44: "股份转让", 47: "中金所期货", 71: "港股通",
}


def connect() -> Any:
    """连活主机。⛔ 不要自己写主机池 —— 用 `DEFAULT_HOST`，它是逐台实测出来的。"""
    from tdxpy.exhq import TdxExHq_API

    api = TdxExHq_API()
    return api, api.connect(DEFAULT_HOST, DEFAULT_PORT)


def list_markets() -> FetchResult:
    """市场目录（实测 31 行）。"""
    try:
        from tdxpy.exhq import TdxExHq_API

        api = TdxExHq_API()
        with api.connect(DEFAULT_HOST, DEFAULT_PORT):
            rows = api.get_markets() or []
        frame = pd.DataFrame(rows)
    except Exception as exc:  # noqa: BLE001
        return FetchResult(state=classify_exception(exc), source=SOURCE,
                           detail=f"{type(exc).__name__}: {exc}"[:300],
                           evidence={"host": f"{DEFAULT_HOST}:{DEFAULT_PORT}"})
    return make_result(frame, source=SOURCE,
                       evidence={"host": f"{DEFAULT_HOST}:{DEFAULT_PORT}"})


def list_instruments(start: int = 0, count: int = 1000) -> FetchResult:
    """合约目录分页。⭐ **标的必须从这里取**（`FINDING-246`），不得手工猜代码。"""
    try:
        from tdxpy.exhq import TdxExHq_API

        api = TdxExHq_API()
        with api.connect(DEFAULT_HOST, DEFAULT_PORT):
            rows = api.get_instrument_info(start, count) or []
        frame = pd.DataFrame(rows)
    except Exception as exc:  # noqa: BLE001
        return FetchResult(state=classify_exception(exc), source=SOURCE,
                           detail=f"{type(exc).__name__}: {exc}"[:300],
                           evidence={"start": start, "count": count})
    return make_result(frame, source=SOURCE,
                       evidence={"start": start, "count": count})


def fetch_bars(market: int, code: str, *, category: int = CATEGORY_5MIN,
               start: int = 0, count: int = MAX_BARS_PER_REQUEST) -> FetchResult:
    """取 K 线。`market` 与 `code` **必须配对**（`FINDING-246`）。

    ⚠ `start` 是**记录偏移量**，不是日期（`FINDING-239` 的形状）。
      offset 越大 = 越早的历史。
    """
    if category not in (CATEGORY_5MIN, CATEGORY_15MIN, CATEGORY_DAILY):
        raise ValueError(f"category={category} 未在本源实测过；"
                         f"已实测：0=5min / 1=15min / 9=daily")
    if market not in MARKETS_MEASURED:
        # ⛔ 不抛错：未实测 ≠ 不可用。但必须让调用方知道结论不可采信。
        pass
    try:
        from tdxpy.exhq import TdxExHq_API

        api = TdxExHq_API()
        with api.connect(DEFAULT_HOST, DEFAULT_PORT):
            rows = api.get_instrument_bars(category, market, code, start, count) or []
        frame = pd.DataFrame(rows)
    except Exception as exc:  # noqa: BLE001
        return FetchResult(state=classify_exception(exc), source=SOURCE,
                           detail=f"{type(exc).__name__}: {exc}"[:300],
                           evidence={"market": market, "code": code,
                                     "category": category, "start": start})
    return make_result(frame, source=SOURCE,
                       evidence={"market": market, "code": code,
                                 "category": category, "start": start,
                                 "market_name": MARKETS_MEASURED.get(market, "未实测"),
                                 "measured_market": market in MARKETS_MEASURED})
