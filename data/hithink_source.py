#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""hithink（同花顺 fuyao.aicubes.cn）数据源适配器 —— T312 第五源。

母库红线合规：⛔ 不放 ``finai/sources/``（只读区），本模块是数据层新源适配，
模式对齐母库（七态契约 ``FetchResult`` + 挂死保护）。

端点（实测 2026-09-03 全通）：
  - ``search``          标的检索（/api/meta/tickers/search）
  - ``kline``           历史 K 线（/api/a-share/prices/historical，
                        adjust=none|forward|backward；窗口 ≤10 年）
  - ``adjust_factors``  复权因子事件流（/api/a-share/corporate-actions/...）：
                        分红/送股/配股事件，含 dividend_per_share 逐股派息
  - ``trading_days``    交易日历（/api/a-share/calendar/trading-days，近一年）

凭据：``.env`` 的 ``HITHINK_FINANCE_API_KEY``（⛔ 不入 git、不打印值）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from finai.sources.base import FetchResult, make_result

SOURCE = "hithink"
_BASE = "https://fuyao.aicubes.cn"
_TIMEOUT = 30


def _api_key() -> str:
    """从 .env 读 key（优先）→ 环境变量兜底。缺失 raise（⛔ 不静默匿名调用）。"""
    key = os.environ.get("HITHINK_FINANCE_API_KEY", "")
    if not key:
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("HITHINK_FINANCE_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    if not key:
        raise RuntimeError(
            "HITHINK_FINANCE_API_KEY 未配置（.env 或环境变量）—— "
            "⛔ 不静默匿名调用（fuyao.aicubes.cn/admin 获取）")
    return key


def _get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET + 错误信封检查（HTTP 200 且 code==0 才算成功）。"""
    resp = requests.get(
        f"{_BASE}{path}", params=params,
        headers={"X-api-key": _api_key()}, timeout=_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"hithink code={body.get('code')} msg={body.get('message')}")
    return body


def fetch(kind: str, **params: Any) -> FetchResult:
    """统一取数入口（形态对齐 ``baostock_source.fetch``）。

    kind: ``search`` / ``kline`` / ``adjust_factors`` / ``trading_days``
    """
    try:
        if kind == "search":
            body = _get("/api/meta/tickers/search", params)
            items = (body.get("data") or {}).get("item") or []
            frame = pd.DataFrame(items)
        elif kind == "kline":
            items = (body := _get(
                "/api/a-share/prices/historical", params)).get("data", {}).get("item") or []
            frame = pd.DataFrame([{
                "date": pd.Timestamp(i["date_ms"], unit="ms", tz="Asia/Shanghai")
                          .strftime("%Y-%m-%d"),
                "open": i["open_price"], "high": i["high_price"],
                "low": i["low_price"], "close": i["close_price"],
                "volume": i["volume"], "amount": i["turnover"],
            } for i in items])
        elif kind == "adjust_factors":
            items = _get(
                "/api/a-share/corporate-actions/adjustment-factors",
                params).get("data", {}).get("item") or []
            frame = pd.DataFrame([{
                "date": pd.Timestamp(i["ex_date_ms"], unit="ms", tz="Asia/Shanghai")
                          .strftime("%Y-%m-%d"),
                "cash_dividend": i.get("dividend_per_share") or 0,
                "factor": 1 + (i.get("per_share_bonus") or 0),
            } for i in items])
        elif kind == "trading_days":
            items = _get(
                "/api/a-share/calendar/trading-days", params).get("data", {}).get("item") or []
            frame = pd.DataFrame([{"date": i["date"], "is_trading_day": "1"}
                                  for i in items])
        else:
            raise ValueError(f"unknown kind: {kind}")
        return make_result(frame, source=SOURCE,
                           evidence={"kind": kind,
                                     "params": {k: str(v)[:40] for k, v in params.items()}})
    except BaseException as exc:  # noqa: BLE001
        return FetchResult(
            state="FAIL_UNREACHABLE" if isinstance(
                exc, (requests.RequestException, TimeoutError)) else "FAIL_DETERMINISTIC",
            frame=None, rows=0, source=SOURCE,
            detail=f"{type(exc).__name__}: {str(exc)[:200]}",
            evidence={"kind": kind, "params": {k: str(v)[:40] for k, v in params.items()}},
        )
