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

from finai.sources.adjustment_mode import (
    BAOSTOCK as _ADJUSTFLAG_TABLE,
    UnknownAdjustment,
)
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

#: ⭐ R1 §1-①：日线查询必须显式要的字段（用于停牌脏行过滤）。
#:   12 号文档 §9-A-4 实测：baostock 停牌日**返回数据行**且 `tradestatus='0'`、
#:   `volume=0`、OHLC 四项全部等于前收盘价（100% 命中）。缺 `tradestatus` 则无法
#:   在入口挡掉脏行，故缺它直接 raise（⛔ 不静默 append，诚实性先于便利）。
_DAILY_FIELDS_REQUIRED: tuple[str, ...] = ("tradestatus",)


def _drop_suspended(frame: pd.DataFrame, *, kind: str) -> tuple[pd.DataFrame, int]:
    """把停牌脏行（`tradestatus != '1'`，OHLC=前收平推）挡在适配层出口。

    12 号文档 §9-A-4 实测 100% 命中；⛔ 不得用前收平推填补缺失（R1 红线）。
    返回 ``(过滤后的帧, 被过滤的行数)``。非 kline 或无 `tradestatus` 列时原样放行。
    """
    if kind != "kline" or "tradestatus" not in frame.columns:
        return frame, 0
    pre = len(frame)
    out = frame[frame["tradestatus"] == "1"]
    return out, pre - len(out)


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


def _validate_kline_params(kind: str, params: dict[str, Any]) -> None:
    """kline 入参的离线校验（R1 §1-① 字段强制 + R4 §3.4 口径校验）。

    ⭐ 必须**先于** `bs.login()`：⛔ 不许先连网登录才发现参数错（既慢又留会话）。
    """
    if kind != "kline":
        return
    frequency = params.get("frequency", "d")
    # ⭐ R1 §1-①：日线必须显式要 `tradestatus`，否则无法过滤停牌脏行。
    if frequency == "d":
        missing = [f for f in _DAILY_FIELDS_REQUIRED
                   if f not in params.get("fields", "").split(",")]
        if missing:
            raise ValueError(
                f"baostock 日线 fields 必须含 {missing} —— 否则无法过滤停牌"
                f"脏行（R1，12 号 §9-A-4 实测：停牌日 OHLC=前收平推）")
    # ⭐ R4 §3.4：adjustflag 显式化 + 用映射表校验，⛔ 越界不静默透传。
    adjustflag = params.get("adjustflag", "3")
    if adjustflag not in _ADJUSTFLAG_TABLE:
        raise UnknownAdjustment(
            f"baostock adjustflag={adjustflag!r} 不在映射表 "
            f"{sorted(_ADJUSTFLAG_TABLE)} 内（R4：⛔ 不许静默换口径）")


def _query(kind: str, params: dict[str, Any]) -> pd.DataFrame:
    """登录 → 查询 → 逐行消费 → 登出。每次调用独立登录，避免会话状态串扰。"""
    _validate_kline_params(kind, params)   # ⭐ 先于登录（离线校验，⛔ 不连网才发现错）
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

    ⭐ R1 §1-②③：日线在 `_query` 返回后、`make_result` 前过滤停牌脏行
    （`tradestatus != '1'`，OHLC=前收平推），被过滤行数写入
    `evidence["suspended_rows"]` 与 `meta["suspended_rows"]`。语义纪律：过滤后
    仍有行 → state 仍 ``OK``（数据真实取到，只是按口径剔除停牌日）；全区间停牌 →
    ``EMPTY_OK``（0 行，代表"该区间无真实行情"，⛔ 与"源故障"分开读）。
    """
    try:
        _validate_kline_params(kind, params)   # ⭐ 离线校验先于一切（⛔ 不连网才发现错）
        frame = run_with_timeout(lambda: _query(kind, params), timeout)
        frame, n_susp = _drop_suspended(frame, kind=kind)
        evidence: dict[str, Any] = {
            "kind": kind, "params": {k: str(v)[:40] for k, v in params.items()},
            "suspended_rows": n_susp, "suspended_warn": n_susp > 0,
        }
        res = make_result(frame, source=SOURCE, evidence=evidence)
        if n_susp:
            res.meta["suspended_rows"] = n_susp
        return res
    except BaseException as exc:  # noqa: BLE001
        return FetchResult(
            state=classify_exception(exc), frame=None, rows=0, source=SOURCE,
            detail=f"{type(exc).__name__}: {str(exc)[:200]}",
            evidence={"kind": kind, "params": {k: str(v)[:40] for k, v in params.items()}},
        )
