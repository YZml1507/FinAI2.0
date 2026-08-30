#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""baostock 适配器 —— 封装两个会静默污染数据的陷阱。

`FINDING-185`（最要紧）：`rs.next()` **只在消费行数据时推进游标**。
  不调 `get_row_data()` 就永远返回 True —— 那是**死循环**，不是阻塞。
  实测：`no_consume` 跑到 9,000 次仍不停（真实 1,976 行）；
  `consume` 则 1,976 行 / 0% 重复 / 6.6s 自然终止。
  ⛔ 且我的第一版"修法"（静默行数上限）**比缺陷更有害** —— 它让死循环
  吐出 20,000 行垃圾并标记为成功。故本模块的上限**触发即 raise**。
  ⛔ `rs.get_data()` 实测挂死，不可用。

`FINDING-181`：baostock 可挂死，且 `socket.setdefaulttimeout` **约束不住它**。
  故需要外部超时；本模块用后台线程 + `join(timeout)` 实现，
  ⚠ 线程无法强杀（Python 限制），故超时后进程可能残留一个僵死线程 ——
  这是**已知且未解决**的限制，批量场景请改用子进程隔离
  （`scripts/probe_data_interfaces.py::probe_one` 是子进程版本）。

`FINDING-177`：代理开启时 baostock 会挂死在取数阶段（实测代理关闭时同一调用 48 行正常）。
  ⚠ 但 `FINDING-185` 已证明"挂死"的真因是游标语义而非代理，
  故本模块**不做代理判断**，只做超时保护。
"""
from __future__ import annotations

import threading
from typing import Any

import pandas as pd

from finai.sources.base import (
    FetchResult,
    classify_exception,
    make_result,
)

SOURCE = "baostock"

#: 游标未终止的保险阈值。⛔ 触发即 raise，绝不静默截断（`FINDING-185`）。
CURSOR_LIMIT = 60_000

#: 单次取数的外部超时（秒）。`FINDING-181`：socket 超时约束不住 baostock。
DEFAULT_TIMEOUT = 90


class BaostockCursorNotTerminating(RuntimeError):
    """游标取到上限仍未结束 —— 疑似未消费行数据导致的死循环。

    ⛔ 绝不可把已取到的行当作真实数据返回：`FINDING-185` 实测那样会
    产出 20,000 行重复/垃圾行，而它们会通过任何"非空即通过"的检查。
    """


def _drain(rs: Any) -> pd.DataFrame:
    """把 ResultData 逐行消费成 DataFrame。

    ⭐ 必须调 `get_row_data()`：这是推进游标的**唯一**方式（`FINDING-185`）。
    """
    if rs.error_code != "0":
        raise RuntimeError(f"baostock error_code={rs.error_code} msg={rs.error_msg}")
    rows: list[list[str]] = []
    while rs.next():
        rows.append(rs.get_row_data())
        if len(rows) > CURSOR_LIMIT:
            raise BaostockCursorNotTerminating(
                f"cursor did not terminate after {len(rows)} rows "
                f"(fields={rs.fields}) -- refusing to return possibly-garbage data"
            )
    return pd.DataFrame(rows, columns=rs.fields) if rows else pd.DataFrame(columns=rs.fields)


def run_with_timeout(fn: Any, timeout: int) -> Any:
    """在后台线程里跑 fn，超时则抛 `TimeoutError`。

    ⚠ 已知限制：Python 无法强杀线程，超时后该线程可能继续残留。
    批量打点请用子进程版本（见模块 docstring）。

    ⭐ **公开**是刻意的（`FINDING-337`）：`phase2/fundamental_collector.py` 也需要
    给 baostock 调用加外部超时。⛔ 那边**不许另写一份** —— `socket.setdefaulttimeout`
    约束不住 baostock（`FINDING-181` 实测），所以这是仓内**唯一**有效实现；
    两份实现必然漂移，而漂移的那份会在挂死时才被发现。
    """
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"baostock call exceeded {timeout}s (thread left running)")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _query(kind: str, params: dict[str, Any]) -> pd.DataFrame:
    """登录 → 查询 → 逐行消费 → 登出。每次调用独立登录，避免会话状态串扰。"""
    import baostock as bs

    bs.login()
    try:
        if kind == "kline":
            rs = bs.query_history_k_data_plus(
                params["code"], params["fields"],
                start_date=params["start_date"], end_date=params["end_date"],
                frequency=params.get("frequency", "d"),
                adjustflag=params.get("adjustflag", "3"),
            )
        elif kind == "adjust_factor":
            rs = bs.query_adjust_factor(
                code=params["code"], start_date=params["start_date"],
                end_date=params["end_date"])
        elif kind == "dividend":
            rs = bs.query_dividend_data(
                code=params["code"], year=params["year"],
                yearType=params.get("yearType", "report"))
        elif kind == "all_stock":
            rs = bs.query_all_stock(day=params["day"])
        elif kind == "trade_dates":
            rs = bs.query_trade_dates(
                start_date=params["start_date"], end_date=params["end_date"])
        else:
            raise ValueError(f"unknown kind: {kind}")
        return _drain(rs)
    finally:
        try:
            bs.logout()
        except Exception:  # noqa: BLE001, S110
            pass


def fetch(kind: str, *, timeout: int = DEFAULT_TIMEOUT, **params: Any) -> FetchResult:
    """统一取数入口。

    kind 取值（均已在 R28 B1 批实测过，行数为实测值）：
      ``kline``          分钟/日线。5min 2026-07-24 → **48 行**；
                         daily+``tradestatus,isST`` 2010 → **10 行**；
                         ⚠ 5min 2015 → **0 行**（EMPTY_OK，⛔ 不得读作"无数据"）
      ``adjust_factor``  复权因子 2010-2015 → **4 行**
      ``dividend``       分红，**含 ``dividCashPsBeforeTax``/``AfterTax`` 两列** → 1 行
                         ⚠ 13/17 条形如 ``'0.144或0.152'`` 的字符串（差别化税率），需解析
      ``all_stock``      历史宇宙（生存者偏差用）2010-01-04 → **1,976 行**
      ``trade_dates``    交易日历

    ⛔ 返回 0 行时 state 为 ``EMPTY_OK``，**不是** OK，也**不是**"该区间无数据"的证据。
    """
    try:
        frame = run_with_timeout(lambda: _query(kind, params), timeout)
        return make_result(frame, source=SOURCE,
                           evidence={"kind": kind, "params": {k: str(v)[:40] for k, v in params.items()}})
    except BaseException as exc:  # noqa: BLE001
        return FetchResult(
            state=classify_exception(exc), frame=None, rows=0, source=SOURCE,
            detail=f"{type(exc).__name__}: {str(exc)[:200]}",
            evidence={"kind": kind, "params": {k: str(v)[:40] for k, v in params.items()}},
        )
