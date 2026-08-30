#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""citydata（闲鱼商家提供的 tushare 镜像）适配器 —— L0 数据源层。

⛔⛔ **两条不可违反的运营约束**（商家明示，违反会导致账号被封）：
  1. **代理不得更换**。所有请求必须经商家指定代理出网。
  2. 因此本模块**没有直连兜底** —— 缺代理时**直接报错**，
     ⛔ 绝不"退回直连"。静默直连会烧掉账号，而账号被封是不可逆的。

⭐ 协议是 **REST-per-API**（实测，`FINDING-243`）：
    POST `{base}/{api_name}`，body = `{"api_name":…, "token":…, "params":{…}, "fields":""}`
  ⛔ 与官方 tushare 不同 —— 官方所有 api 都 POST 同一个 `/`。
    照抄官方写法在本镜像上**100% 应用层 404「接口不存在」**。
    这个坑我实测踩过：5 条 smoke 全 404，直到抓包看 SDK 的真实请求才定位。

⛔ **凭据只走环境变量/`.env`，绝不落盘、绝不进 artifacts**（`FINDING-162`）。
  日志里只暴露代理的 `host:port`，不含账号口令。

实测能力与限制见 `FINDING-243`：114 个可用接口 / 18 个数据族。
⚠ **拿不到历史分钟线**：1min 仅约半年（2026-02 起），5min **完全缺失**。
  该结论已用**同源 `daily` 交叉验证**（2016–2023 抽样 14 个交易日，daily 有 K 线
  而 1min 零行）⇒ 是数据缺失，不是休市。⛔ 故本源**不得**用于 5min 需求。
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

SOURCE = "citydata"

#: 镜像基址（可覆盖，但**默认值就是商家给的那个**）
DEFAULT_URL = "https://tushare.citydata.club"

#: ⚠⚠ **2026-08-10 更正：`FINDING-243` 的"5min 完全缺失"已被实测推翻**（`FINDING-341`）。
#: 复测 `stk_mins(freq="5min")`：`2026-08-06` → **95 行**、`2026-08-07` → 43 行、
#: `2026-05-20` → 47 行 ⇒ ⭐ **5min 是有的**，⛔ 旧结论不可再引用。
#: 但**深度确实不足**：`2026-05-06`/`2026-04-20`/`2026-02-11`/`2025-03-04`/`2024-09-03` 全 0 行，
#: 且每个 0 都用**同源 `daily`** 反证过该日有 K 线（`daily=1`）⇒ 是真缺失，不是休市。
#: ⇒ 实测可用窗口约 **2026-05-20 起**（5min）。
#:
#: ⛔ **故这里不再按"粒度"整类硬拒** —— 那道旧闸门会把**真实存在的能力**挡在门外，
#: 症状是"明明取得到却报不支持"，而且理由写着一条已被推翻的结论。
#: ⭐ 改为只登记**深度下界**，由调用方按自己要的区间判断：
#: 要 2026-05 之前的历史 5min ⇒ 走 `finai.sources.tdx_ext_source`
#: （`FINDING-251` 实测港股 5min 回溯到 2015-11-30，约 10.7 年）。
MIN_INTRADAY_DATE: dict[str, str] = {
    "5min": "2026-05-20",
    "1min": "2026-02-11",
}

#: ⛔ 保留给"本源结构上没有"的能力（当前为空）。
#: ⚠ 往这里加条目前先问：**这是"端点没有"，还是"我没测到"**（`FINDING-251` 三次同形教训）。
UNSUPPORTED: dict[str, str] = {}


def _require_env() -> tuple[str, str, str]:
    """取 token / proxy / url。⛔ 缺代理直接抛错 —— 没有直连兜底。"""
    from finai.credentials import get_credential

    token = os.environ.get("CITYDATA_TS_TOKEN") or (
        get_credential("CITYDATA_TS_TOKEN", required=False, default="") or "")
    proxy = os.environ.get("CITYDATA_PROXY") or (
        get_credential("CITYDATA_PROXY", required=False, default="") or "")
    url = (os.environ.get("CITYDATA_URL")
           or get_credential("CITYDATA_URL", required=False, default="")
           or DEFAULT_URL)
    if not token or not proxy:
        raise RuntimeError(
            "citydata 需要 CITYDATA_TS_TOKEN 与 CITYDATA_PROXY（环境变量或 .env）。\n"
            "⛔ 代理是商家指定的，不得更换（换了账号会被封），"
            "也不得留空直连 —— 本适配器**故意没有**直连兜底。")
    return token, proxy, url


def proxy_id() -> str:
    """返回代理的 `host:port`，用于日志/产物。⛔ 不含账号口令。"""
    _, proxy, _ = _require_env()
    return proxy.rsplit("@", 1)[-1] if "@" in proxy else proxy


def _envelope_table(body: Any) -> pd.DataFrame:
    """校验 `{code,msg,data:{fields,items}}` 信封并取出表。**全程 fail-closed**。

    ⭐ `FINDING-404`：旧写法 `if body.get("code") not in (0, None): raise`
    有**三个放行漏洞**，每一个都让「200 + 非契约信封」走完整条成功路径：

      ① `code` **字段整体缺失** → `.get()` 返回 `None`，而 `None` 就在放行集合里；
      ② `code: false` → Python 里 `False == 0` ⇒ `False not in (0, None)` 为**假**；
      ③ `code: 0.0` → 同理 `0.0 == 0`。

    实测后果（改前，`tests/test_citydata_envelope.py` 的 RED 输出）：

        {"msg":"","data":null}                      → EMPTY_OK   ← "这天没有数据"
        {"msg":"","data":{fields,items}} 带 1 行     → **OK**     ← 当成可信数据用下去
        {"ok":false,"error":"upstream unavailable"}  → EMPTY_OK
        {"code":false,...}                          → EMPTY_OK
        {"code":0,"data":{"items":[2 行]}} 无 fields → EMPTY_OK   ← 真有 2 行却报 0 行
        {"code":0,"data":[]}                        → EMPTY_OK

    ⛔ 危害不是"多取一条错数据"，而是**失败相位被判成功相位**：`EMPTY_OK` 的既有
    语义是「端点答了、确实没有行」（`FINDING-178`），于是「商家换了信封格式 /
    网关回了个 200 非契约 JSON」被下游读成「这天没有数据」——
    正是 `FINDING-178` 要拆开的那两件事又被合并一次，只是这回合并在信封层。

    ⚠ 判据改为「`code` **是** `int` 且 **== 0**」。`bool` 必须单独排除：
    它是 `int` 的子类，只写 `isinstance(code, int)` 挡不住 `code: false`。

    ⭐ **唯一**可信的"无行"是 `code == 0` 且 `data` 为空 —— 那是端点明说
    "调用成功"之后的空表。⛔ 缺 `code` 时的空表不可信，差别就在这一位上。

    Raises:
        ValueError: 信封形状破损。⇒ `classify_exception` + `bytes_received>0`
            归 `FAIL_GATEWAY`（不可解释）—— 商家改版 / 网关错页 / 服务降级
            三者无法区分，故**不得**记成"接口没有这个能力"。
        RuntimeError: `code` 非 0，端点明确报错。
    """
    if not isinstance(body, dict):
        raise ValueError(
            f"citydata 信封破损：响应体是 {type(body).__name__} 而非 JSON 对象")
    if "code" not in body:
        raise ValueError(
            "citydata 信封破损：契约必备字段 `code` 缺失"
            f"（顶层键 {sorted(map(str, body))[:8]}）。"
            "⛔ 缺 code 不等于成功 —— 判成 EMPTY_OK 会让下游把"
            "「信封变了 / 网关回了 200 错页」读成「这天没有数据」（FINDING-404）")
    code = body["code"]
    if isinstance(code, bool) or not isinstance(code, int):
        raise ValueError(
            f"citydata 信封破损：`code` 是 {type(code).__name__}（值 {code!r}）"
            "而非整数 —— ⛔ 不按 `== 0` 宽松比较，false/0.0 都不是成功")
    if code != 0:
        raise RuntimeError(
            f"citydata 应用层错误 code={code} msg={body.get('msg')}")

    data = body.get("data")
    if data is None:
        # `code == 0` 已由端点明说成功 ⇒ 空 data 是可信的"无行"
        return pd.DataFrame()
    if not isinstance(data, dict):
        raise ValueError(
            f"citydata 信封破损：`data` 根是 {type(data).__name__} 而非 "
            "{fields,items} 对象 —— ⛔ 旧写法 `body.get(\"data\") or {}` 会把它"
            "静默换成空 dict，于是破损信封变成一张 0 行表")
    items, fields = data.get("items") or [], data.get("fields") or []
    if items and not fields:
        raise ValueError(
            f"citydata 信封破损：`data.items` 有 {len(items)} 行但 `fields` 缺失"
            " —— 无列名可用，⛔ 不得静默丢掉整批行再报 0 行")
    return pd.DataFrame(items, columns=fields) if fields else pd.DataFrame()


def fetch(api_name: str, /, **params: Any) -> FetchResult:
    """调 citydata 上的一个 tushare api，返回统一 `FetchResult`。

    Args:
        api_name: tushare api 名（如 `daily` / `cyq_perf` / `moneyflow`）
        **params: 该 api 的入参（如 `ts_code=`, `start_date=`）

    ⚠ 空表归 `EMPTY_OK` 而非成功 —— 同 `tdx_source` 的理由（`FINDING-178`）：
      把"没取到"与"取到了"同判成功，会让下游把空面板当成真相。
    ⛔ 但 `EMPTY_OK` **只发给** `code == 0` 的真空表。信封破损（缺 `code`、
      `data` 根非法、有 `items` 无 `fields`）一律走失败相位（`FINDING-404`）——
      详见 `_envelope_table`。
    """
    import requests

    token, proxy, url = _require_env()
    proxies = {"http": proxy, "https": proxy}
    payload = {"api_name": api_name, "token": token,
               "params": dict(params), "fields": ""}

    bytes_received = 0
    try:
        resp = requests.post(f"{url}/{api_name}", json=payload,
                             proxies=proxies, timeout=60)
        bytes_received = len(resp.content or b"")
        resp.raise_for_status()
        body = resp.json()
        # 镜像沿用 tushare 的 {code,msg,data:{fields,items}} 信封。
        # ⛔ 校验必须 fail-closed：`FINDING-404` 的三个放行漏洞见 `_envelope_table`。
        frame = _envelope_table(body)
    except Exception as exc:  # noqa: BLE001
        # ⛔ 失败路径必须走 `classify_exception` + 带 `bytes_received`：
        #   `FINDING-217` —— bytes>0 说明请求已发出且端点回了，失败在解析而非网络。
        #   这是六态契约的判据输入，缺了它下游无法区分失败相位。
        return FetchResult(
            state=classify_exception(exc, bytes_received),
            frame=None, rows=0, source=SOURCE,
            detail=f"{type(exc).__name__}: {exc}"[:300],
            evidence={"api_name": api_name, "proxy": proxy_id(),
                      "bytes_received": bytes_received},
        )

    return make_result(frame, source=SOURCE,
                       evidence={"api_name": api_name, "proxy": proxy_id(),
                                 "bytes_received": bytes_received})


def assert_supported(need: str, *, start_date: str | None = None) -> None:
    """调用方声明需求；命中 `UNSUPPORTED` 即拒绝并给出替代源。

    ⚠ `FINDING-341`：**不再按粒度整类拒绝** —— `5min`/`1min` 实测是有的，
    旧闸门会误拒真实能力。只在**明确要早于实测下界的历史**时才拒。

    Args:
        need: 能力名（如 `"5min"`）。
        start_date: 需求区间起点（`YYYY-MM-DD` 或 `YYYYMMDD`）。⭐ 传了才做深度判断；
            ⛔ 不传时**不拒绝** —— 增量日更是这个源的正当用法。
    """
    if need in UNSUPPORTED:
        raise RuntimeError(f"citydata 不支持 {need}：{UNSUPPORTED[need]}")
    floor = MIN_INTRADAY_DATE.get(need)
    if floor and start_date:
        want = start_date.replace("-", "")[:8]
        if want < floor.replace("-", ""):
            raise RuntimeError(
                f"citydata 的 {need} 实测只回溯到 {floor}（`FINDING-341`：更早的日期"
                f"逐日实测 0 行，且已用同源 daily 反证该日有 K 线 ⇒ 真缺失）。"
                f"你要的 {start_date} 早于此 ⇒ 请走 finai.sources.tdx_ext_source"
                f"（`FINDING-251` 实测 5min 回溯至 2015-11-30）。"
            )
