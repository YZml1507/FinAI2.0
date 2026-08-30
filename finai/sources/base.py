#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L0 数据源适配层 —— 统一契约与共用加固。

为什么存在（`DATA_PLATFORM_REBUILD_PLAN_20260806.md` §3.1）：
本轮实测出的取数陷阱**全是调用约定层面**的，而它们此前散落在 30+ 个脚本里，
每个脚本各自踩一遍 —— 我在 R28 中**每一类都踩过至少一次**。

封装的陷阱（每条都有台账编号与实测证据，不是推定）：
  `FINDING-178`/`-189` mootdx/StdQuotes 默认不 pin 服务器且 `bestip=False`，
      实测 daily/5min **恒返回 0 行**且不抛异常；生产代码 `if df.empty: return []` 静默吞掉
  `FINDING-185` baostock `rs.next()` **只在消费行数据时推进游标**，
      不调 `get_row_data()` 即死循环（实测 no_consume 跑到 9000 次仍不停，真实 1976 行）
  `FINDING-181` baostock 可挂死且 `socket.setdefaulttimeout` **约束不住它** → 须外部超时
  `FINDING-177` 新浪系缺 `Referer` → 403（实测带对 Referer 后 3/3 200）
  `FINDING-183` akshare 需强制 IPv4 + patch `Session.__init__`
      （⚠ 直接设类属性 `Session.trust_env=False` **无效**，`__init__` 会写实例属性覆盖）
  `FINDING-179` 代理 502 空体 → `JSONDecodeError`，与"库有 bug"**逐字同形**

⛔ 硬约束：
  1. **静默 0 行必须成态**，不得当成功、也不得当"无数据"
  2. 任何"上限/截断/兜底"触发时必须 `raise`，绝不静默返回（`FINDING-185` 的错误修法）
  3. `FAIL_UNREACHABLE`/`FAIL_GATEWAY` 不得记为"接口不可用"
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

# ---------------------------------------------------------------- 七态契约

OK = "OK"                             # 行数 > 0，永远可信
EMPTY_OK = "EMPTY_OK"                 # 调用成功但 0 行 —— ⛔ 不得读作"无数据"
FAIL_DETERMINISTIC = "FAIL_DETERMINISTIC"   # 端点给出明确语义报错，可信
FAIL_GATEWAY = "FAIL_GATEWAY"         # 502/503/504 或空体解析失败 —— 不可解释
FAIL_UNREACHABLE = "FAIL_UNREACHABLE" # 连接层/超时/挂死 —— 不可解释
FAIL_PROBE_BUG = "FAIL_PROBE_BUG"     # 调用方自己写错（AttributeError 等），与数据源无关

#: 调用成功、**确有返回值**，但返回的不是表格 ⇒ 无字段名可记。
#: ⛔ 不得归 `OK`（会谎报有 cols），也不得归 `EMPTY_OK`（会谎报 0 行，实际 rows≥1）。
#: `FINDING-238` 定的规矩是"不得伪造 cols"，本态就是那条规矩的落点；实测 20 条属此类。
OK_NO_COLS = "OK_NO_COLS"

#: ⛔ **状态白名单**。`FINDING-258` 实测：`FetchResult` 此前是裸 dataclass，
#: 任意字符串都能当状态（`state='TOTALLY_MADE_UP'` 构造成功且 `.ok` 静默为 False）
#: ⇒ 六态常量只是"建议"。危害不是多一个态，是**手误不报错**：
#: 写成 `'OK_'` 会让一次成功被静默读成失败。故此处做硬校验。
ALL_STATES = frozenset({
    OK, OK_NO_COLS, EMPTY_OK,
    FAIL_DETERMINISTIC, FAIL_GATEWAY, FAIL_UNREACHABLE, FAIL_PROBE_BUG,
})

#: 状态是否可作为"该接口没有这个能力"的证据
#: ⚠ `OK_NO_COLS` **不在**此列：它证明"调得通、有返回"，但证不出字段面。
CONCLUSIVE = frozenset({OK, FAIL_DETERMINISTIC})

_GATEWAY_MARKERS = ("502", "503", "504", "bad gateway", "gateway time")
_UNREACH_MARKERS = (
    "connectionerror", "connectionaborted", "remotedisconnected", "timeout",
    "timed out", "sslerror", "ssleoferror", "max retries", "connection closed",
    "connection reset", "nameresolution", "failed to perform", "hung",
)
_DETERMINISTIC_MARKERS = (
    "api not purchased", "没有权限", "无权限", "权限不足", "积分不足",
    "invalid", "not found", "403", "401", "抱歉", "不存在",
    "访问频率", "rate limit", "每分钟最多访问",
    # ⛔ FINDING-203 实测补：tushare 的 `您的token不对，请确认。` 此前落进
    # `FAIL_UNREACHABLE`（不可解释），而它是**端点明确回答了我们** ——
    # 网络通到了端点，问题在凭证。归 DETERMINISTIC 才能作为"需 token"的结论。
    # ⚠ B2/B3 批实测的 `active_ip_limit_exceeded`（代理并发限制）同理。
    "token", "ip_limit", "limit_exceeded", "凭证", "未购买", "试用",
)
#: ⛔ FINDING-203：`_PROBE_BUG_TYPES` 原先把整个 `KeyError`/`TypeError` 家族
#: 都判成"调用方自己写错"，而实测 10 个 `FAIL_PROBE_BUG` 里 **9 个是误标** ——
#: 它们是**库在解析意外响应时崩掉**（`KeyError('data')`：拿到响应但里面没有 `data` 键）。
#: 且 `KeyError` → PROBE_BUG 而 `IndexError` → UNREACHABLE，**成因相同结论相反**。
#: 故改为按**异常文本的形状**三分，而不是只按类型名。

#: ① 调用方误用 / 缺本地配置 —— 与数据源无关，可作结论。
#:
#: ⭐ 判据来源：`FINDING-213` 实测 86 条失败（`diagnose_failure_phase.py`，
#: 全部 `复现忠实=True`，`instrument_ok=True`）。
#: 此处只列**实测 recv=0** 的形态 —— 即「调用方根本没发出请求就崩了」。
#: recv>0 的形态由 `bytes_received` 入参在运行时裁决（`FINDING-217`）。
#:
#: ⛔ 归类判据：只有「这条错误在原理上不可能发生在收到响应之后」才能列入，
#: 不按「看起来像我的错」判断 —— 那是 `FINDING-184` 被修之前的旧思路。
_MISUSE_MARKERS = (
    "unexpected keyword argument",   # 参数名写错
    "missing required positional",   # 漏传必填参数
    "missing 1 required",
    "takes no arguments",
    "not callable",
    "no module named",               # import 名写错
    "is not defined",                # NameError
    # ⭐ FINDING-213 补：中文验证类（实测 15 条均 recv=0）
    "市场参数错误",          # mootdx ExtQuotes.* 7条：传空 market 参数
    "年度输入错误",          # tushare get_*_data 7条：合成了非法 year/quarter
    "stock code need list type",   # mootdx AssertionError 1条
    # ⭐ FINDING-213 补：缺本地配置类（实测 ≤15 条 recv=0，见下方单独说明）
    # ⚠ 这些只列**唯一标识性文本**，避免误命中正常网络错误。
    "tdxnotassignvipdocpath",       # tdxpy：缺 vipdoc 本地路径 3条
    "please provide a vipdoc path", # 同上（大小写不敏感匹配）
    "socketclientnotready",         # tdxpy：需先调 setup() 2条
    "socket client not ready",      # 同上
)

#: ⚠ `has no attribute` **必须两段同时命中**才算"我写错了名字"。
#: 实测两类真实消息都含这个短语，但含义相反：
#:   `module 'akshare' has no attribute 'stock_dt_pool_em'`  → **我的**函数名写错（FINDING-184）
#:   `'NoneType' object has no attribute 'get'`              → 端点返回了意外形状（不可解释）
#:   `'dict' object has no attribute 'foo'`                  → 同上
#: 只写 `has no attribute` 会把后两类误判成"我的错"，从而把**端点问题藏进我的 bug 里**。
#: ⭐ 这正是 `FINDING-197` §24.5「模式匹配的统计必须先验证匹配本身」的第四次同族
#: （前三次：`sse`↔`assets`、`ggtj`↔`ggt`、`gdfx`↔`fx`）。
_MISUSE_PAIRS = (
    ("module ", "has no attribute"),
)

#: ② 环境缺陷 —— 不是我的代码，也不是数据源，而是本机 Python/OpenSSL 版本问题。
#: 仍归 `FAIL_PROBE_BUG`（与数据源无关），但**必须可辨识**，
#: 否则"修 probe bug"这个任务会无从下手（分不清该改我的代码还是升级环境）。
_ENV_DEFECT_MARKERS = (
    "op_legacy_server_connect",      # 实测：module 'ssl' has no attribute ...
    "openssl",
    "unsupported protocol",
)

#: ③ 库解析意外响应 —— 归 `FAIL_GATEWAY`（不可解释）。
#: ⭐ 这一类的判据来自 `FINDING-179` 我自己写下的规则原文：
#:   「任何解析类异常（`JSONDecodeError`/`KeyError`/`IndexError`）出现在网络调用后，
#:     **先打印原始状态码与响应体，再谈库**」
#: 成因可能是端点改版 / 返回错误页 / 服务下线 / 被限流返回空体 —— 四者无法区分。
#: ⛔ 故既不得读作"接口不可用"，也不得读作"我的 bug"。
_PARSE_FAILURE_TYPES = ("keyerror", "indexerror", "typeerror", "valueerror",
                        "jsondecodeerror", "attributeerror")


def classify_exception(exc: BaseException, bytes_received: int | None = None) -> str:
    """把异常归入五态。顺序敏感：调用方误用 > 环境缺陷 > 网关 > 解析失败 > 连接层 > 语义。

    `FINDING-184`：`AttributeError` 曾被归成 `FAIL_UNREACHABLE`，
    于是"我把函数名写错"被伪装成"接口不可用"。故调用方误用必须最先判。

    `FINDING-179`：解析类异常常是代理空体 502 伪装，归 `FAIL_GATEWAY` 而非语义错。

    ⛔ `FINDING-203`：**不得按异常类型名整族判 PROBE_BUG**。
    `KeyError('data')` 与 `AttributeError("module X has no attribute Y")`
    的类型都在旧的 `_PROBE_BUG_TYPES` 里，但前者是端点返回了意外形状（不可解释），
    后者才是我写错了（可作结论）。**区分靠文本特征，不靠类型名。**

    `FINDING-217`：**补词表有可证明的天花板**。
    同一文本形态（如 `AttributeError: 'NoneType' object has no attribute '...'`）
    在不同场景下实测阶段相反（recv=0 vs recv=1,096），纯文本判据无法区分。
    新增 `bytes_received` 入参，直接用 socket 层实测值裁决：
      · `bytes_received == 0` + 解析类异常 →
        不可能是"解析响应失败"（没有响应可解析）→ 调用方或库的本地问题 → `FAIL_PROBE_BUG`
      · `bytes_received > 0`  + 解析类异常 →
        端点**已被证明可达且回答了我们**，问题在库的解析逻辑 → `FAIL_GATEWAY`
      · `bytes_received is None` → 新观测不存在，维持原有文本判据（向后兼容）

    ⛔ `FAIL_PROBE_BUG` 的语义边界：仅指"与数据源能力无关"，不代表"必然是我写错了"；
    `bytes_received == 0` 时可能是参数不合法、缺本地配置等，统一归此态。

    Args:
        exc:            异常对象。
        bytes_received: 发出请求后实际收到的字节数（不含 TLS/HTTP 头部以外的协议开销）。
                        `None` 表示本次探测没有计量（历史结果或未安装计数器）。
    """
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    blob = f"{name} {text}"

    # ① 调用方误用 —— 唯一能证明"是我的错"的一类，最先判
    for m in _MISUSE_MARKERS:
        if m in text:
            return FAIL_PROBE_BUG
    # `has no attribute` 须两段同时命中（见 `_MISUSE_PAIRS` 的实测说明）
    for parts in _MISUSE_PAIRS:
        if all(p in text for p in parts):
            return FAIL_PROBE_BUG

    # ② 环境缺陷 —— 与数据源无关，但也不是我的代码错
    for m in _ENV_DEFECT_MARKERS:
        if m in blob:
            return FAIL_PROBE_BUG

    # ③ 网关（502/503/504）—— 请求可能根本没到端点
    for m in _GATEWAY_MARKERS:
        if m in blob:
            return FAIL_GATEWAY

    # ④ 连接层 —— 先于解析失败判，因为 `ConnectionError` 的 text 里
    #    可能含 "invalid" 等词，会被语义标记误吞
    for m in _UNREACH_MARKERS:
        if m in blob:
            return FAIL_UNREACHABLE

    # ⑤ 语义明确的端点报文（无权限 / 需 token / 频率限制）—— 可作结论
    #    ⚠ 必须早于解析失败判：`ValueError("api not purchased")` 这类
    #    既是 ValueError 又带明确报文，应归 DETERMINISTIC。
    for m in _DETERMINISTIC_MARKERS:
        if m in blob:
            return FAIL_DETERMINISTIC

    # ⑥ 库解析意外响应（FINDING-179 / FINDING-203 / FINDING-217）
    if name in _PARSE_FAILURE_TYPES or "expecting value" in text:
        # ⭐ FINDING-217：bytes_received 是决定性判据
        if bytes_received is not None:
            if bytes_received == 0:
                # 没发出请求或请求前就崩了 —— 与数据源无关
                return FAIL_PROBE_BUG
            # bytes_received > 0：端点已被证明可达且有响应，问题在库
            return FAIL_GATEWAY
        # bytes_received 未知 —— 维持保守判据（向后兼容）
        return FAIL_GATEWAY

    return FAIL_UNREACHABLE  # 默认最保守：不可解释


@dataclass
class FetchResult:
    """一次取数的结果 + 证据。**永不静默**：状态与行数始终显式携带。"""

    state: str
    frame: pd.DataFrame | None = None
    rows: int = 0
    source: str = ""
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """⛔ 状态必须在 `ALL_STATES` 内（`FINDING-258`）。

        为什么必须 `raise` 而不是警告：非法态会让 `.ok` / `.conclusive`
        **静默**变 False —— 一次成功被读成失败且无痕，正是 `FINDING-185`
        禁止的那类静默降级。
        """
        if self.state not in ALL_STATES:
            raise ValueError(
                f"非法状态 {self.state!r}；七态契约只允许 {sorted(ALL_STATES)}。"
                f"⛔ 不得手写新态：新增一态须先在 ISSUE_LEDGER 预登记语义"
                f"（`OK_NO_COLS` 是这么进来的，见 FINDING-238/258）。")

    @property
    def ok(self) -> bool:
        return self.state == OK

    @property
    def conclusive(self) -> bool:
        """能否据此断言数据源的能力（`EMPTY_OK` / `OK_NO_COLS` / 各 FAIL_* 都不能）。"""
        return self.state in CONCLUSIVE

    def require(self) -> pd.DataFrame:
        """取数据，非 OK 即抛。⛔ 用它替代 `if df.empty: return []` 这类静默降级。"""
        if self.state != OK or self.frame is None:
            raise SourceFetchError(
                f"[{self.source}] state={self.state} rows={self.rows} detail={self.detail}"
            )
        return self.frame


class SourceFetchError(RuntimeError):
    """取数失败。⛔ 调用方不得把它降级成空结果而不留痕。"""


def make_result(frame: pd.DataFrame | None, *, source: str,
                evidence: dict[str, Any] | None = None) -> FetchResult:
    """由返回帧构造结果。**0 行归 `EMPTY_OK` 而非 OK** —— 这是 `FINDING-178` 的教训：
    静默 0 行与"确实无数据"在返回值上无法区分，故必须单独成态。"""
    n = 0 if frame is None else len(frame)
    return FetchResult(
        state=OK if n > 0 else EMPTY_OK,
        frame=frame, rows=n, source=source,
        detail="" if n > 0 else "returned 0 rows -- NOT evidence of 'no data'",
        evidence=evidence or {},
    )


def harden_requests_session() -> None:
    """令新建的 `requests.Session` 不信任环境代理。

    `FINDING-183` 实测：直接设类属性 `Session.trust_env = False` **无效**，
    因为 `Session.__init__` 里 `self.trust_env = True` 会写实例属性覆盖类属性。
    必须 patch `__init__` 本身，且在其执行**之后**赋值。
    """
    import requests

    if getattr(requests.sessions.Session, "_finai_hardened", False):
        return
    _orig = requests.sessions.Session.__init__

    def _patched(self, *a, **k):  # noqa: ANN001, ANN202
        _orig(self, *a, **k)
        self.trust_env = False

    requests.sessions.Session.__init__ = _patched
    requests.sessions.Session._finai_hardened = True


#: 「缺某个请求头就整体拒答」的主机 → 需要补的头。⛔ 逐主机白名单，不全局改默认头。
#: 每条都必须有**单变量隔离实测**（只切这一个头，其余全同）才准进表。
_REQUIRED_HEADERS: dict[str, dict[str, str]] = {
    # `FINDING-332` 实测（2026-08-10，同一 URL/同一 params，只切一个头）：
    #   裸请求 → **404 / 1,163 字节**；只加 `User-Agent` → 仍 **404**；
    #   只加 `Referer: https://fundf10.eastmoney.com/` → **200 / 20,603 字节**；
    #   给个**错的** Referer（`https://example.com/`）→ 回到 **404**
    #   ⇒ Referer 是唯一决定变量，且**其取值被服务端校验**。
    "fundf10.eastmoney.com": {"Referer": "https://fundf10.eastmoney.com/"},
    # `FINDING-332` 实测：交易商协会 WAF 按 UA 拦截。
    #   默认 `python-requests/2.33.0` → **403 / 6,688 字节** HTML 拦截页
    #   （正文标题「您的访问请求可能对网站造成安全威胁，请求已被阻断」）；
    #   浏览器 UA → **200 / 17,634 字节**。⇒ UA 是唯一决定变量。
    "zhuce.nafmii.org.cn": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    # `FINDING-336` 实测（2026-08-10，同一 URL 一字不动，只切 UA）：
    #   默认 `python-requests/2.33.0` → **HTTP 510 / 780 字节**、`<table>` 标签 **0 个**
    #   （库随即 `findAll("table")[0]` ⇒ `IndexError: list index out of range`）；
    #   浏览器 UA → **200 / 71,912 字节**、1 个 table / 505 个 `<tr>`。
    #   按库自身解析逻辑复算 ⇒ **500 行 / 7 列**。⇒ UA 是唯一决定变量。
    #   ⚠ 510 是个**罕见状态码**（HTTP "Not Extended"），比 403 更容易被读成"服务端坏了"，
    #     实测它只是这台 WAF 拒 UA 的表达方式 —— 与 `zhuce.nafmii.org.cn` 同一形状。
    "stats.areppim.com": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
}


def _default_user_agent() -> str | None:
    """`requests` 自己的样板 UA（如 `python-requests/2.33.0`）。

    ⛔ 不写死版本号：从库里现取，否则库一升级这个例外就静默失效。
    """
    try:
        from requests.utils import default_user_agent
        return default_user_agent()
    except Exception:  # noqa: BLE001
        return None


def install_required_headers(
    table: dict[str, dict[str, str]] | None = None) -> bool:
    """为「缺某个请求头就整体拒答」的主机补上那个头。**逐主机白名单。**

    ⭐ **为什么这不是本轮禁止的"读侧劫持"**：禁令针对的是**改写 URL / 重写
      `filter=`** —— 那等于替库重新实现查询语义，端点一变就静默返回错数据。
      本函数**不碰 URL、不碰任何查询参数、不碰响应体**，只补一个 HTTP 头；
      头决定的是**准不准我读**，不决定**读到哪些记录** ⇒ 数据正确性不受影响
      （⭐「财务数据宁可缺不可错」这条红线在这里不被触碰）。
    ⭐ 仓内已有同型先例：`SINA_HEADERS`/`EASTMONEY_HEADERS`（`FINDING-177`
      实测「带对 Referer 后 3/3 200」）—— 那是我们自己发请求时带；本函数把
      同一件事补到**库替我们发请求**的路径上。

    ⛔ **只加缺失的头，不覆盖已有值**：库若自己带了 Referer/UA，以库的为准
      （`FINDING-329` 的教训是"把库自己算好的值发出去"，⛔ 不是"用我猜的值顶掉它"）。
    ⛔ 不按 URL 前缀模糊匹配，按 **hostname 精确相等**：`in url` 那种写法会被
      `?redirect=fundf10.eastmoney.com` 之类的查询串骗到，把头发给第三方主机。
    """
    import requests
    from urllib.parse import urlsplit

    tbl = _REQUIRED_HEADERS if table is None else table
    if getattr(requests.sessions.Session, "_finai_req_headers", False):
        return True
    _orig = requests.sessions.Session.request

    def _patched(self, method, url, **kw):  # noqa: ANN001, ANN202
        host = (urlsplit(str(url)).hostname or "").lower()
        need = tbl.get(host)
        if need:
            hdrs = dict(kw.get("headers") or {})
            present = {k.lower(): v for k, v in (self.headers or {}).items()}
            present.update({k.lower(): v for k, v in hdrs.items()})
            # ⚠ `requests` 自己在 `Session.headers` 里塞了 `python-requests/x.y`
            #   这个**样板** UA。它不是调用方的选择，故不算"库已指定" ——
            #   否则 UA 类白名单永远命中不了（实测：不做这个例外，nafmii 那条
            #   仍是 403/6,688 字节，因为 `present` 里恒有一个 UA）。
            boilerplate = _default_user_agent()
            for k, v in need.items():
                cur = present.get(k.lower())
                if cur is None or (k.lower() == "user-agent" and cur == boilerplate):
                    hdrs[k] = v
            kw["headers"] = hdrs
        return _orig(self, method, url, **kw)

    requests.sessions.Session.request = _patched
    requests.sessions.Session._finai_req_headers = True
    return True


def install_pandas1_append_shim() -> bool:
    """恢复 pandas 2.0 移除的 `DataFrame.append` / `Series.append`。

    ⭐ `FINDING-264`（2026-08-09 实测，`scripts/_r31_legs.py` 成对腿）：
    老 tushare（本机 1.4.29）的 7 条基本面接口在 pandas 2.3.2 上**谎报网络故障**：
    库内 `except Exception: pass` 吞掉真异常
    `AttributeError: 'DataFrame' object has no attribute 'append'`，
    再统一抛 `OSError: 获取失败，请检查网络.`。
    实测两侧腿（同一入参、只改「有无本垫片」这一个变量）：

        get_profit_data      旧 OSError网络 → 新 rows=5205 cols=9
        get_report_data      旧 OSError网络 → 新 rows=5205 cols=11
        get_cashflow_data    旧 OSError网络 → 新 rows=5205 cols=7
        get_growth_data      旧 OSError网络 → 新 rows=5205 cols=8
        get_operation_data   旧 OSError网络 → 新 rows=5205 cols=8
        get_debtpaying_data  旧 OSError网络 → 新 rows=5205 cols=8
        get_today_all        旧 AttributeError append → 新 rows=5638 cols=15

    **为什么这不是"放宽判据"**：判据（`cols` 非空 ∧ `rows>0`）一字未动，
    分母未动。改的是**我方运行时**——库要的方法本机没有。数据一直在上游，
    是我们这侧取不到。

    ⚠ 语义等价性：pandas 官方迁移口径就是
    `df.append(other)` ≡ `concat([df, other])`；`ignore_index=False` 时
    两者都保留原索引。⛔ 这里**不做**任何列对齐/去重/排序的"顺手改进"——
    垫片一旦偏离原语义，探针记下的行列就不是接口的真实结论了。

    ⛔ 装在哪里很关键：必须在**探针子进程**与**生产取数路径**都装，否则
    产物会声称能取到、而管道取不到（这正是本项目反复被咬的"管道事实 vs
    市场事实"，见 `FINDING-18`）。故本函数住在 `base.py`：
    `auto_probe_interfaces._worker()` 与 `catalog_source.fetch()` 同源调用。

    Returns:
        True 表示本次真的装上了；False 表示 pandas 自带（<2.0）或已装过。
    """
    import pandas as pd

    if getattr(pd.DataFrame, "_finai_append_shim", False):
        return False
    if hasattr(pd.DataFrame, "append"):
        return False  # pandas 1.x 自带，⛔ 不覆盖原生实现

    def _df_append(self, other, ignore_index=False,  # noqa: ANN001, ANN202
                   verify_integrity=False, sort=False):
        if isinstance(other, dict):
            other = pd.DataFrame([other])
        elif isinstance(other, pd.Series):
            other = other.to_frame().T
        elif isinstance(other, list) and other and isinstance(
                other[0], (dict, pd.Series)):
            other = pd.DataFrame(other)
        return pd.concat([self, other], ignore_index=ignore_index,
                         verify_integrity=verify_integrity, sort=sort)

    def _s_append(self, to_append, ignore_index=False,  # noqa: ANN001, ANN202
                  verify_integrity=False):
        return pd.concat([self, to_append], ignore_index=ignore_index,
                         verify_integrity=verify_integrity)

    pd.DataFrame.append = _df_append          # type: ignore[attr-defined]
    pd.Series.append = _s_append              # type: ignore[attr-defined]
    pd.DataFrame._finai_append_shim = True    # type: ignore[attr-defined]
    return True


def install_brotli_decoder_swap() -> bool:
    """把 urllib3 的 brotli 解码实现从 `brotlicffi` 换成 CPython `brotli`。

    ⭐ `FINDING-311`（2026-08-09 实测）：**高压缩比响应会解码失败**，
    报 `ContentDecodingError: Received response with content-encoding: br,
    but failed to decode it`。⛔ 这**不是**"上游返回坏数据"，也**不是**网络问题 ——
    同一批字节用整块解码是**完好的**。

    实测机制（`https://www.cnindex.com.cn/index/indexList`，`rows` 是唯一变量）：

        rows=50    响应 28,952B    → 成功
        rows=200   响应 116,811B   → 成功
        rows=500   响应 298,026B   → 成功
        rows=2000  压缩体 121,804B → **失败**（解压后 899,860B，压缩比 7.4×）

    对**同一段失败字节**做四种解码：

        brotli.decompress(整块)                    → OK 899,860B
        brotlicffi 一次性 decompress(整块)         → OK 899,860B
        brotlicffi 分块喂（10KB/次）               → **FAIL**
        CPython brotli 分块喂（10KB/次）           → OK 899,860B

    ⇒ 真因在 `brotlicffi._api.Decompressor.decompress`：它有 **输出缓冲上限**，
    喂到 51,200B 输出后 `can_accept_more_data()` 转 False，要求调用方先
    `decompress(b"")` 排空。而 `urllib3.response.BrotliDecoder` 是
    **无条件继续喂**的（见其 `__init__`：有 `decompress` 就直接绑上去），
    于是第二块数据撞上 `decoder process called with data when
    'can_accept_more_data()' is False`。
    ⚠ 只有**压缩比高**的响应才会在一块之内就把输出缓冲顶满 ⇒ 故障看着像
      "随机/偶发"，实测是**确定性**的（同一入参 3/3 复现）。

    为什么换 CPython `brotli` 就好：urllib3 优先 `import brotlicffi as brotli`，
    仅在其缺失时退回 CPython `brotli`。两者接口不同 ——
    `brotlicffi.Decompressor` 有 `.decompress`（带缓冲上限），
    CPython `brotli.Decompressor` **只有** `.process`（无上限）。
    urllib3 的 `hasattr(self._obj, "decompress")` 分支因此选中了有上限的那条路。
    ⛔ 本函数**不改 urllib3 的代码**、不改 site-packages，只在运行期把
       `urllib3.response.brotli` 指向 CPython 实现 ——
       之后 urllib3 自己新建的 `BrotliDecoder` 就会走 `.process` 分支。

    ⚠ 必须同时把 `brotli.error` 追加进 `HTTPResponse.DECODER_ERROR_CLASSES`：
      那个元组在类定义时按 `brotlicffi.error` 固化，不补的话 CPython 实现
      抛的错会**穿透** urllib3 的包装层，变成裸 `brotli.error`
      而不是 `ContentDecodingError` ⇒ 上层的 except 分支会漏接。

    **为什么这不是"放宽判据"**：判据（`cols` 非空 ∧ `rows>0`）一字未动，
    分母未动，没有条目进豁免集。改的是**我方运行时的解码器选择** ——
    数据一直在上游、字节一直是完好的，是我们这侧解不开。

    实测效果（`akshare`，成对腿，只改「有无本垫片」这一个变量）：

        index_hist_sw            旧 ContentDecodingError → 新 rows=6429 cols=8
        stock_report_disclosure  旧 ContentDecodingError → 新 rows=4584 cols=7
        index_all_cni            旧 ContentDecodingError → 新 `ValueError:
                                 Length mismatch: Expected axis has 26
                                 elements, new values have 25` ⇒ 解码已通，
                                 ⛔ 但**仍不可用**：那是 akshare 自己的列名表
                                 与上游字段数不符（另一层缺陷，不由本垫片修）。

    ⛔ 变异反证腿（`FINDING-197`）：不装垫片、同一进程同一调用顺序，
       上面两条**必须**回落到 `ContentDecodingError`——实测确实回落（2/2）。
       故"是垫片起的作用"是被证明的，不是同期网络好转的巧合。

    ⚠ 装在哪里很关键：与 `install_pandas1_append_shim` 同一条理由 ——
      探针子进程与生产取数路径必须同源，否则产物声称能取、管道取不到
      （`FINDING-18`）。

    Returns:
        True 表示本次真的换上了；False 表示无需换（没装 brotlicffi、
        或 CPython `brotli` 不可用、或已换过）。
    """
    try:
        import urllib3.response as _u3resp
    except Exception:  # noqa: BLE001  # urllib3 不可用 ⇒ 无事可做
        return False

    if getattr(_u3resp, "_finai_brotli_swapped", False):
        return False

    cur = getattr(_u3resp, "brotli", None)
    if cur is None:
        return False        # urllib3 根本没有 brotli 支持 ⇒ 不是本缺陷的形状
    # ⛔ 只在当前实现**确实是带缓冲上限的 brotlicffi** 时才换。
    #    若 urllib3 已经在用 CPython brotli（只有 .process），本垫片是空转 ——
    #    直接返回 False，⛔ 不假装"装上了"（`FINDING-157`：改了却不会变的空转）。
    if getattr(cur, "__name__", "") != "brotlicffi":
        return False

    try:
        import brotli as _brotli_cpy      # CPython 实现（只有 .process）
    except Exception:  # noqa: BLE001
        return False                      # 没有替代实现 ⇒ 保持原状，不劣化
    if hasattr(_brotli_cpy.Decompressor(), "decompress"):
        # 装的其实还是 brotlicffi（同名 shadow）⇒ 换了也没用，⛔ 不空转声称成功
        return False

    _u3resp.brotli = _brotli_cpy
    err = getattr(_brotli_cpy, "error", None)
    if err is not None:
        existing = tuple(_u3resp.HTTPResponse.DECODER_ERROR_CLASSES)
        if err not in existing:
            _u3resp.HTTPResponse.DECODER_ERROR_CLASSES = existing + (err,)
    _u3resp._finai_brotli_swapped = True   # type: ignore[attr-defined]
    return True


def force_ipv4() -> None:
    """强制 IPv4 解析。

    ⚠ `FINDING-182`：这**不是** `push2his` 失败的根因 —— 我曾据一次不可复现的
    HTTP 200 下过那个结论，重复 6 次是 0/6，已撤回。此处保留仅因它在
    部分端点上无害且能排除 IPv6 优先带来的额外变量，**不承诺任何修复效果**。
    """
    if getattr(socket, "_finai_v4_only", False):
        return
    _orig = socket.getaddrinfo

    def _v4(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002, ANN001, ANN202
        return _orig(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _v4
    socket._finai_v4_only = True  # type: ignore[attr-defined]


#: 东财 push2 面「连上即关」的 host 前缀（`FINDING-299` 实测）。
_POOL_RETRY_HOSTS = ("push2.eastmoney.com", "push2his.eastmoney.com",
                     "push2delay.eastmoney.com")

#: 每个 host 已实测「能供数」的池成员，运行期缓存（host → ip）。
_POOL_GOOD: dict[str, str] = {}
#: 已实测「连上即关」的池成员，避免反复撞同一个坏后端。
_POOL_BAD: dict[str, set[str]] = {}

#: ⭐ `FINDING-301`：系统 DNS 当下**每个 host 只返回 1 条 A 记录**，且会轮换到坏成员
#: （实测 `push2` 先后拿到 `47.112.165.11` / `14.103.191.91` / `101.226.30.206`，全 0/N）。
#: ⇒ 只靠 `getaddrinfo` 就**没有池可换**，垫片会"结构正确但挑不到成员"。
#: 故补一份**实测候选集**作为兜底来源，与系统 DNS 结果**合并**后逐个实测挑选。
#: ⛔ 这不是"硬编码可用 IP"：每个候选**必须现场通过 `_serves()` 实测**才会被使用，
#:    坏的进黑名单。候选表只影响"试哪些"，⛔ 不影响"判定谁可用"。
#: ⚠ 候选会失效（`FINDING-247`：主机换过 IP 和端口）⇒ 全坏时行为退化为原生解析，
#:    不会比不打垫片更差。可用 `FINAI_EASTMONEY_POOL`（逗号分隔）覆盖。
_POOL_SEED_DEFAULT = ("101.226.30.136", "119.3.232.150", "101.226.30.206",
                      "101.226.30.221", "43.144.251.121", "117.184.38.143",
                      "61.129.129.196", "47.112.165.11")

#: 准入所需的**连续**成功探测次数（k-of-k）。⭐ `FINDING-430`（2026-08-12 实测）。
#:
#: ⛔ 这**不是**"多探几次更稳妥"这种美学主张，也**不是**在调一个旋钮 ——
#: 它修的是一个**前提错误**：`_serves()` 原先按 n=1 抽样，且结果写进 `_POOL_GOOD`
#: 后**永久生效**（实测 `_POOL_GOOD.pop|clear|del` 全仓 0 命中）。
#: ⚠ 而实测**同一个固定 IP 上存在连接级间歇性**：`61.129.129.199` 在配对实验里
#: IPv4 腿 4/6，在随后的逐 IP 扫描里同一 IP 0/3。
#: ⇒ 成员不是"要么好要么坏"，而更像一枚有偏硬币。在一枚硬币上抽 1 次并把结果
#:   **不可逆地**固化，等于按抛硬币结果决定整个进程能否取数。
#:
#: ⭐ 取 3 的理由（⛔ 不是拍脑袋）：设某坏成员单次探测的假通过率为 p，
#: 则 k-of-k 的误准入率是 p^k。实测那个间歇成员 p≈4/6≈0.67 ⇒
#: k=1 误准入 67%、k=3 降到约 30%、k=5 约 13%。⚠ 代价是每个候选最多 k 次 TLS 握手
#: （失败即短路，故坏成员仍只花 1 次）。3 是"显著降低误准入"与"挑成员延迟"的折中。
#: ⛔ **k 再大也不能替代失效机制** —— p^k 永远 >0，且成员会**在使用中**变坏
#:   （准入时是好的，之后才坏）。故 `_pool_invalidate()` 是**独立必需**的另一半。
#: ⭐ 提高 k 是**提高**门槛，⛔ 不是降标准：判据（HTTP 200）一字未动。
_POOL_ADMIT_K = 3


def _pool_admit(
    host: str,
    candidates: list[str],
    probe: Callable[[str, str], bool],
    bad: set[str],
    k: int = _POOL_ADMIT_K,
) -> str | None:
    """按 **k-of-k** 判据从 `candidates` 里挑一个成员；挑不到返回 `None`。

    ⭐ 本函数**刻意提为模块级并把探测器作为参数注入** —— 原实现里
    `_serves`/`_pick` 是闭包内定义，**从外部完全不可测**，于是
    「准入判据是 n=1」这个缺陷没有任何测试能钉住它（`FINDING-430`）。
    ⚠ 注入的是**判定预言机**，⛔ **不是**用 mock 伪造网络条件：
    守卫喂进去的是一个"已知行为的成员"（如"第 1 次成，之后都败"），
    测的是**策略在该行为下做什么决定**，⛔ 不声称复现了任何真实网络现象。

    ⛔ 副作用刻意留在调用方：本函数只**读** `bad`，把失败成员**加入** `bad`，
    但**不写** `_POOL_GOOD` —— 写缓存这件事必须与"谁负责失效"在同一处，
    否则又会出现"有人写、没人删"的老问题。
    """
    for ip in candidates:
        if ip in bad:
            continue
        ok = 0
        for _ in range(k):
            if not probe(ip, host):
                break          # ⭐ 一次失败即淘汰：坏成员只花 1 次探测
            ok += 1
        if ok == k:
            return ip
        bad.add(ip)
    return None


def _pool_invalidate(host: str) -> str | None:
    """把 `host` 当前缓存的成员**踢出**缓存并拉黑，返回被踢掉的 IP（无则 `None`）。

    ⭐ 这是 `FINDING-430` 的**第二个洞**，与 k-of-k 无关且不可互相替代：
    成员可能**准入时是好的、使用中才变坏**（实测同一固定 IP 4/6 → 0/3）。
    原实现里 `_POOL_GOOD` 一旦写入就**没有任何路径**能删除它
    ⇒ 一次幸运探测污染整个进程生命周期。
    """
    ip = _POOL_GOOD.pop(host, None)
    if ip is not None:
        _POOL_BAD.setdefault(host, set()).add(ip)
    return ip


def install_eastmoney_pool_retry(probe_timeout: float = 6.0) -> bool:
    """令 push2 面在「连上即关」时**换池成员重试**，而不是直接失败。

    ⭐ `FINDING-299`（2026-08-09 实测，`scripts/_r34_pinned_ip_test.py` 成对腿）：
    push2 面是**地址池部分损坏** —— 同一 host 的多个 A 记录里只有一部分供数，
    其余在 **TLS 握手之后、HTTP 响应之前**关闭连接。实测逐 IP 各打 4 次：

        push2.eastmoney.com    47.112.165.11    0/4  连上即关
        push2his.eastmoney.com 101.226.30.221   0/4  连上即关
        82.push2.eastmoney.com 101.226.30.206   4/4  HTTP 200 + 真数据
        push2.eastmoney.com    101.226.30.136   OK   HTTP 200 + 真数据

    **为什么 `requests`/`urllib3` 自己救不了**：`socket.create_connection` 只在
    **连接失败**时才遍历下一个地址；这里 TCP 与 TLS 都成功，失败发生在 HTTP 阶段
    ⇒ 地址遍历用不上，于是每次都卡在同一个坏后端 ⇒ 表现为 `RemoteDisconnected`。
    这就是 65 条同签名失败的机制（推翻 `FINDING-291` 的「复测零收益」前提：
    那个结论的前提是 `network_gate` 东财 3/3 通过，但门测的是
    `datacenter-web.eastmoney.com` —— **另一个 host**，从未测过 push2）。

    实测两侧腿（同一入参、只改「有无本垫片」这一个变量，8 条真实 akshare 接口）：

        stock_zh_a_spot_em               旧 RemoteDisconnected → 新 rows=5892 cols=23
        stock_bid_ask_em                 旧 RemoteDisconnected → 新 rows=36   cols=2
        stock_zh_a_hist                  旧 RemoteDisconnected → 新 rows=4    cols=12
        index_zh_a_hist                  旧 RemoteDisconnected → 新 rows=4    cols=11
        stock_board_industry_name_em     旧 RemoteDisconnected → 新 rows=496  cols=12
        stock_individual_fund_flow_rank  旧 RemoteDisconnected → 新 rows=5292 cols=15
        fund_etf_hist_em                 旧 RemoteDisconnected → 新 rows=4    cols=11
        stock_sector_fund_flow_rank      旧 RemoteDisconnected → 新 rows=496  cols=14

    ⇒ leg A 0/8、leg B 8/8。

    **为什么这不是「放宽判据」**：判据（`cols` 非空 ∧ `rows>0`）一字未动，分母未动，
    没有任何豁免名单。改的是**我方运行时的连接选择**——数据一直在上游，
    是我们这侧固定撞在坏后端上。与 `install_pandas1_append_shim` 同性质。

    ⛔ **不硬编码 IP**：池成员会轮换（实测多轮 DNS 返回的 A 记录不同），
    写死等于埋一颗定时炸弹（`FINDING-247` 就是主机换了 IP 和端口）。
    做法是每个 host 首次使用时**实测挑一个能供数的成员**并缓存，坏成员记入黑名单。
    ⚠ 因此本函数**必然产生网络包**（挑成员时），⛔ 不得在"零网络"的静态归属流程里调用。
    """
    import urllib3.util.connection as _u3

    if getattr(_u3, "_finai_pool_retry", False):
        return False

    _orig_create = _u3.create_connection

    def _candidates(host: str) -> list[str]:
        """系统 DNS 结果 **+** 实测候选集，去重后按序返回。

        ⭐ 顺序是故意的：先试系统 DNS 给的（若它恰好给了好成员，零额外探测），
        再试候选集。⛔ 每一个都要过 `_serves()` 实测，候选集不代表"可用"。
        """
        out: list[str] = []
        seen: set[str] = set()
        try:
            for info in socket.getaddrinfo(host, 443, socket.AF_INET,
                                           socket.SOCK_STREAM):
                ip = info[4][0]
                if ip not in seen:
                    seen.add(ip)
                    out.append(ip)
        except OSError:
            pass
        env = os.environ.get("FINAI_EASTMONEY_POOL", "")
        seed = ([x.strip() for x in env.split(",") if x.strip()]
                if env else list(_POOL_SEED_DEFAULT))
        for ip in seed:
            if ip not in seen:
                seen.add(ip)
                out.append(ip)
        return out

    def _serves(ip: str, host: str) -> bool:
        """实测该成员是否在 HTTP 阶段真的回数据（而不是连上即关）。"""
        import ssl as _ssl
        path = ("/api/qt/stock/get?secid=0.000001"
                "&ut=fa5fd1943c7b386f172d6893dbfba10b")
        raw = None
        try:
            raw = socket.create_connection((ip, 443), timeout=probe_timeout)
            ctx = _ssl.create_default_context()
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                tls.sendall(
                    f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                    "User-Agent: Mozilla/5.0\r\nAccept: */*\r\n"
                    "Accept-Encoding: identity\r\n"
                    "Referer: https://quote.eastmoney.com/\r\n"
                    "Connection: close\r\n\r\n".encode())
                buf = tls.recv(2048)
            return bool(buf) and b"200" in buf.split(b"\r\n", 1)[0]
        except OSError:
            return False
        finally:
            if raw is not None:
                try:
                    raw.close()
                except OSError:
                    pass

    def _pick(host: str) -> str | None:
        """挑一个成员并缓存。⭐ 准入判据委托给模块级 `_pool_admit`（k-of-k）。

        ⛔ 判据**不得**在这里内联重写：`FINDING-430` 的成因正是它藏在闭包里、
        没有任何测试能钉住"探几次才算通过"。
        """
        cached = _POOL_GOOD.get(host)
        if cached:
            return cached
        bad = _POOL_BAD.setdefault(host, set())
        ip = _pool_admit(host, _candidates(host), _serves, bad)
        if ip is not None:
            _POOL_GOOD[host] = ip
        return ip

    def _patched(address, timeout=None, source_address=None,
                 socket_options=None):  # noqa: ANN001, ANN202
        host = str(address[0])
        if any(host == h or host.endswith("." + h) for h in _POOL_RETRY_HOSTS):
            ip = _pick(host)
            if ip:
                return _orig_create((ip, address[1]), timeout, source_address,
                                    socket_options)
        return _orig_create(address, timeout, source_address, socket_options)

    _u3.create_connection = _patched
    _u3._finai_pool_retry = True  # type: ignore[attr-defined]

    # ── 第二半：**用中失败**要能把成员踢出缓存并换一个重试一次 ──────────────
    # ⭐ `FINDING-430`：上面的 `_patched` 只在**建连接时**替换 IP，函数名里的
    #    "retry" 因此只发生在**挑成员**阶段。成员在**使用中**变坏后（实测同一固定
    #    IP 4/6 → 0/3），没有任何路径能把它踢出 `_POOL_GOOD` ⇒ 整个进程持续取零数据。
    # ⛔ k-of-k 修不了这一半：准入时它确实是好的。
    import requests as _rq
    from urllib.parse import urlsplit as _urlsplit

    _orig_request = _rq.sessions.Session.request

    def _patched_request(self, method, url, **kw):  # noqa: ANN001, ANN202
        try:
            return _orig_request(self, method, url, **kw)
        except _rq.exceptions.ProxyError:
            # ⛔ 代理坏 ≠ 池成员坏。踢成员是错误归因，直接上抛。
            raise
        except _rq.exceptions.ConnectionError:
            # ⚠ 覆盖实测到的两种表象：`RemoteDisconnected`→ConnectionError、
            #   `SSLEOFError(UNEXPECTED_EOF_WHILE_READING)`→SSLError（其子类）。
            #   `ConnectTimeout` 也在其中 —— 连不上同样是坏成员，踢掉是对的。
            #   ⛔ 刻意**不**捕 `ReadTimeout`（非 ConnectionError 子类）：读超时更可能
            #   是慢查询而非坏后端，踢掉是错误归因。
            if str(method).upper() not in ("GET", "HEAD"):
                raise      # ⛔ 只重试幂等方法，⛔ 不替调用方决定重放副作用
            host = _urlsplit(str(url)).hostname or ""
            if not any(host == h or host.endswith("." + h)
                       for h in _POOL_RETRY_HOSTS):
                raise
            if _pool_invalidate(host) is None:
                # 没有缓存成员可踢 ⇒ 重试必然走同一条路，⛔ 不做无意义的重放
                raise
            # ⭐ 只重试**一次**：被踢的成员已进黑名单，故这次 `_pick` 必然拿到
            #    **另一个**成员。⛔ 不做循环重试 —— 那会在整池皆坏时放大打点压力。
            return _orig_request(self, method, url, **kw)

    _rq.sessions.Session.request = _patched_request
    _rq.sessions.Session._finai_pool_retry_in_use = True
    return True


#: 已知「只发叶证书、不发中间证书」的主机。⛔ **不是**豁免白名单：
#: 校验**全程开启**，我们只是把服务器本该自己发的那一环从它证书里的
#: AIA `caIssuers` 地址取回来补进信任捆 —— 与"跳过校验"是相反的操作。
_AIA_HOSTS = ("www.swsresearch.com", "www.chinascope.com.cn")

#: 补好的信任捆路径（进程内缓存，⛔ 不写进仓库、不落 git 跟踪目录）
_AIA_BUNDLE: dict[str, str] = {}


def install_aia_chain_fix(hosts: tuple[str, ...] = _AIA_HOSTS) -> bool:
    """为「服务器漏发中间证书」的主机补齐证书链，**校验保持开启**。

    ⭐ `FINDING-327`（2026-08-10 实测）：3 条申万接口报 `SSLError`
    `unable to get local issuer certificate`。⛔ 这**不是**本机证书库坏、
    **不是**域名死、**更不是**该跳过校验的理由。逐层取证：

        openssl s_client -connect www.swsresearch.com:443 -showcerts
            → 服务器**只发 1 张**证书：`0 s:CN=*.swsresearch.com`
              `i:CN=GeoTrust G2 TLS CN RSA4096 SHA256 2022 CA1`
            → `Verify return code: 21 (unable to verify the first certificate)`
        certifi 捆里 GeoTrust 条目数 = **0**
        但该中间证书的签发者 `DigiCert Global Root G2` **在**捆内 ⇒ 只缺中间一环
        叶证书的 AIA caIssuers = http://cacerts.digicert.cn/GeoTrustG2TLSCNRSA4096SHA2562022CA1.crt
            → 实测 HTTP 200 / 1,482 字节
        并入捆后 `requests.get(..., verify=<捆>)`（**校验开启**）→ TLS 握手通过

    ⛔ **绝不** `verify=False`、⛔ 不设 `CERT_NONE`、⛔ 不改 site-packages：
    那三种都是把"我们无法验证对方身份"改成"我们不在乎对方是谁" ——
    对财务数据源而言等于接受中间人（⭐ 宁可缺不可错）。
    ⭐ 中间证书是**现取**的：写死 PEM 会在轮换时静默失效（同 `FINDING-323` ②）。
    """
    import requests

    if getattr(requests.sessions.Session, "_finai_aia_fixed", False):
        return True
    try:
        bundle = _build_aia_bundle(hosts)
    except Exception:  # noqa: BLE001
        # ⛔ 不抛：本垫片是**增量**能力（补 2 个主机），装不上不该让整批探测失败。
        #    但也**不静默降级成 verify=False** —— 那条路根本不存在。
        return False
    if bundle is None:
        return False

    _orig = requests.sessions.Session.request

    def _patched(self, method, url, **kw):  # noqa: ANN001, ANN202
        if kw.get("verify", True) is True and any(h in str(url) for h in hosts):
            kw["verify"] = bundle
        return _orig(self, method, url, **kw)

    requests.sessions.Session.request = _patched
    requests.sessions.Session._finai_aia_fixed = True
    return True


def _build_aia_bundle(hosts: tuple[str, ...]) -> str | None:
    """取回各 host 缺失的中间证书，与 certifi 捆合并成一个临时捆，返回其路径。"""
    import socket
    import ssl
    import tempfile

    import certifi
    import requests

    extra: list[str] = []
    for host in hosts:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # 仅为**读取**对方叶证书，不用于取数
            with socket.create_connection((host, 443), timeout=15) as s:
                with ctx.wrap_socket(s, server_hostname=host) as ss:
                    der = ss.getpeercert(binary_form=True)
            from cryptography import x509

            cert = x509.load_der_x509_certificate(der)
            aia = cert.extensions.get_extension_for_class(
                x509.AuthorityInformationAccess).value
            for desc in aia:
                if desc.access_method.dotted_string != "1.3.6.1.5.5.7.48.2":
                    continue
                r = requests.get(desc.access_location.value, timeout=30)
                if r.ok and r.content:
                    extra.append(ssl.DER_cert_to_PEM_cert(r.content))
                    break
        except Exception:  # noqa: BLE001, PERF203
            continue
    if not extra:
        return None
    fh = tempfile.NamedTemporaryFile("w", suffix="-finai-ca.pem",
                                     delete=False, encoding="utf-8")
    fh.write(Path(certifi.where()).read_text(encoding="utf-8"))
    for pem in extra:
        fh.write("\n" + pem)
    fh.close()
    return fh.name


#: 「调用时才求值」的入参哨兵。⛔ 覆盖表里**不得**写死凭证类值：
#:   ① 产物 `auto_probe_results.json` 是 **git 跟踪**的，写死等于把凭证提交进仓库；
#:   ② 这类值**会过期** —— akshare 内置的 `xq_a_token` 就是这么死的
#:      （`FINDING-323` 实测：库自带 token 打 quote.json 返 400 `error_code 400016`）。
#: ⇒ 覆盖表存**哨兵字符串**，探针与生产在**调用前**各自解析成当次的真值。
#: ⭐ 必须与 `catalog_source.fetch()` 同源解析（`FINDING-264`/`-319` 的形状：
#:   只在一侧解析 ⇒ 产物声称能取、生产取不到）。
DEFERRED_ARG_PREFIX = "@finai:"


def resolve_deferred_args(kwargs: dict) -> dict:
    """把 kwargs 里的哨兵值换成**当次现取**的真值。非哨兵原样返回。

    ⛔ 不缓存到磁盘、不写回产物：每次运行现取（见上面 ② 过期理由）。
    """
    out = {}
    for k, v in kwargs.items():
        if isinstance(v, str) and v.startswith(DEFERRED_ARG_PREFIX):
            out[k] = _resolve_one_deferred(v[len(DEFERRED_ARG_PREFIX):])
        else:
            out[k] = v
    return out


def _resolve_one_deferred(spec: str):
    if spec == "xueqiu_anon_token":
        return fetch_xueqiu_anon_token()
    raise ValueError(f"未知的延迟入参哨兵 {spec!r} —— 覆盖表与解析器已漂移，拒绝静默继续")


def fetch_xueqiu_anon_token(timeout: float = 30.0) -> str:
    """现向雪球索取一个**匿名访客** `xq_a_token`。

    ⭐ `FINDING-323` 实测取证链：
      ① akshare 内置 token（`akshare.stock.cons`，40 字符）打
         `stock.xueqiu.com/v5/stock/quote.json` → HTTP 400，
         体 `{"error_code":"400016","error_description":"遇到错误，请刷新页面…"}`
         ⇒ **内置 token 已失效**（不是反爬、不是端点坏）。
      ② 完全不带 cookie → 同样 400/400016 ⇒ 确是**缺凭证**。
      ③ 匿名 GET 个股页 `xueqiu.com/snowman/S/<SYM>/detail` 的响应头即
         `Set-Cookie: xq_a_token=<40字符>`，同一响应把 `xq_is_login`/`remember`
         显式置 1970 过期 ⇒ 这是站点发给**匿名访客**的公开 token，**非账号态**。
         ⚠ 首页 `xueqiu.com/` 只发 `acw_tc`、**不发** token —— 必须打个股页。
      ④ `token=` 是 akshare 那两个函数的**既有形参**，故传值是走库自己的既定入口。
    ⛔ 不伪造 cookie、不硬编码任何 token、不改 site-packages、不劫持 URL 重写。
    """
    import requests

    s = requests.Session()
    s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    s.get("https://xueqiu.com/snowman/S/SH601127/detail", timeout=timeout)
    token = s.cookies.get("xq_a_token")
    if not token:
        # ⛔ 不静默退回"不传 token" —— 那会把"取不到凭证"伪装成"接口不可用"
        raise RuntimeError(
            "雪球未下发匿名 xq_a_token —— 站点行为已变，拒绝在无凭证下继续")
    return token


def install_tushare_referer_fix() -> bool:
    """把 tushare 自己算好、却被**注释掉没发出去**的 `Referer` 头补发出去。

    ⭐ `FINDING-329` 实测取证链（上交所行情接口，2026-08-10）：
      ① `tushare/util/netbase.py:Client._setOpener` 第 12 行是
         `#         request.add_header('Referer', self._ref)` ——
         **构造函数收下了 `ref=` 参数，却从不使用它**。
      ② 头矩阵实测（同一 URL、同一 Cookie、同一 UA，只切 Referer 一项）：
         | 腿 | 实测 |
         |---|---|
         | 不带 Referer | **119 字节**：`{"success":"false","error":"系统繁忙…","errorType":"ExceptionInter…}` |
         | 带 `http://www.sse.com.cn/market/dealingdata/overview/margin/` | **3,433 字节**正常 jsonp |
         ⇒ Referer 是**唯一**决定变量。
      ③ 装上后端到端实测：`sh_margins` → **6 行 / 7 列**，
         `sh_margin_details` → **3,809 行 / 9 列**（均需与 pandas1 append 垫片同装）。

    ⛔ **不是**替库重新实现语义（那是本轮明令禁止的读侧 URL 改写）：
      本函数**不构造任何 URL、不猜任何 Referer 值**，只把 `self._ref`
      —— 库在调用点自己用 `rv.MAR_SH_HZ_REF_URL % (...)` 算出来并传进来的值 ——
      按 HTTP 语义发出去。端点若改版，`self._ref` 随库变，我们这里不需要跟着改。
    ⛔ 不改 site-packages：只在**本进程内**换 `_setOpener` 的实现。

    ⚠ 影响面实测：整个 tushare 里 `Client(...)` 有 **16 个调用点，只有 3 个传 `ref=`**
      （`reference.py:604 _sh_hz` / `:694 _sh_mx` / `:895 moneyflow_hsgt`）。
      其余 13 处 `self._ref is None` ⇒ 本垫片对它们**完全不动**。
      第 3 处 `moneyflow_hsgt` 成对腿实测：不装/装上**都是** `URLError: timed out`
      （其主机本机不可达，与本垫片无关）⇒ 不引入回归。

    Returns:
        True 已装好；False **仅**表示本机没装 tushare（此时无可修对象）。
        ⛔ 其他任何异常都**照原样抛出** —— `FINDING-212` ② 的教训是
        静默失败会让"没装上"与"装上了"在产物里无法区分。
    """
    try:
        import tushare.util.netbase as _nb
    except ImportError:
        return False

    if getattr(_nb.Client, "_finai_referer_fixed", False):
        return True
    _orig = _nb.Client._setOpener

    def _patched(self):
        _orig(self)
        ref = getattr(self, "_ref", None)
        # ⛔ 只在库**自己算好了** ref 时补发；⛔ 不为 None 的情况编造一个值。
        if ref:
            self._request.add_header("Referer", ref)

    _nb.Client._setOpener = _patched
    _nb.Client._finai_referer_fixed = True
    return True


#: 新浪系接口缺 Referer 即 403（`FINDING-177` 实测：带对 Referer 后 3/3 200）
SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://finance.sina.com.cn/",
}
EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://finance.eastmoney.com/",
}
