#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按**时间分段**取数并重组 —— `DATA_LAYER_DESIGN_PRINCIPLES.md` §1.3 第 8 步。

⭐ 本模块**只做三件事**，其余一律复用既有实现：
  ① 把一个区间切成**互不重叠、无缝隙、端点严格相等**的窗口（`segment_window`）；
  ② 让每个窗口各自去调 `capability_router.get()` —— ⛔ **不写第二条换源路径**
     （`FINDING-387`：一个事实两份实现）。换源、口径锁、覆盖面声明全由 `get()` 负责；
  ③ 按**故障域**节流与限并发（`FINDING-338`/`FINDING-558`），阈值取自
     `scripts/auto_probe_interfaces.py` 那张**已实测**的表，⛔ 不在这里另抄一份数字。

⛔⛔ **正确性优先于速度，这是本模块的排序原则而不是口号**：
  分段之所以危险，是因为它**看起来**只是把一次请求拆成几次。实测反例（同一轮）：
  `tdx::daily_bar` 对 `20260601..20260630` 只回 15 行、首日 `2026-06-09`，而血缘独立的
  腾讯腿同参数回 21 行、首日 `2026-06-01` —— 少 6 个交易日、`ok=True`、`coverage=""`
  声明全量（`FINDING-781`）。⇒ ⭐ **一次少给边界行的"提速"是数据缺陷，不是提速。**
  故本模块对每段做**区间归属断言**（`window_containment`），并把逐段实得端点交给调用方，
  ⛔ 不把缺口平均掉。

⚠ **本模块不做的事，及为什么**：
  ⛔ **不跨 schema 重组** —— `get()` 的口径锁只在同一 `schema` 内降级（那一条是刻意的），
    分段若允许各段落在不同 schema，重组出来的帧会**沿时间轴换口径**，而调用方从帧上看不出来
    （`FINDING-343` 的形状）。⇒ schema 在**第一次调用之前**就被钉死，逐段传同一个值。
  ⛔ **不做"把不同段派给不同候选"的分发** —— 那需要一条能指定"从第几个候选开始试"的
    选择逻辑，而 `get()` 恒按注册表顺序试（`capability_router.py` 的 `for i, c in
    enumerate(cands)`）⇒ 要做就得在本模块里写第二套候选选择，正是任务明令禁止的那件事。
    ⚠ 后果必须说清：**同一 schema 下所有段都会落在同一个故障域**，故本模块的并发度
    受**那一个域**的上限约束（见 `_DOMAIN_CONCURRENCY`），⛔ 不会因为"能力有 3 条腿"
    就变成 3 倍并发。
  ⛔ **不写盘** —— 无 `data/cold/`、无 catalog、无任何注册根。产物由调用方负责落到
    `artifacts/` 下。

Author: 单元 013 Phase 2（T011–T014）
FINDING: FINDING-338（sina 按 IP 限流）· FINDING-558（push2his 节流阻塞资金流轴）·
  FINDING-780（tdx 节点缓存并发不安全）· FINDING-781（tdx 静默少给历史边界行）·
  FINDING-361（覆盖面必须运行时可见）· FINDING-387（一个事实一份实现）
"""
from __future__ import annotations

import datetime as _dt
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import pandas as pd

from finai.sources import capability_router as _cr
# ⭐ 复用 router 那唯一一份"任意日期写法 → YYYYMMDD"的归一化器（⛔ 不再写第二份：
#   `FINDING-387`）。它刻意只去分隔符、不做时区换算，正是端点比较需要的无损归一。
from finai.sources.capability_router import _norm_cal_date as _norm

#: ⭐⭐ **区间语义，全模块唯一一处声明。⛔ 不得在别处重述。**
#:
#: 本模块的 `Window` 是**闭区间** `[start, end]` —— 两端都**含**。
#: ⛔ 这不是随手选的，是被**下游语义**决定的：`capability_router` 的
#:   `_cal_le()`（`capability_router.py:474`）注释明写 "含右端点 —— 与服务端
#:   `end_date` 语义一致"，且 `tdx_daily_bar_adapter._filter_by_range()` 用
#:   `<= end` / `>= start`。⇒ 若本模块用半开区间，`end` 那一天会被**发出去的请求**
#:   当成含、被**分段器**当成不含 ⇒ 相邻两段都会取到那天 ⇒ 重组后该日**重复**。
#: ⇒ 故相邻段的衔接是 `next.start == prev.end + 1 天`（日历日，⛔ 不是交易日：
#:   交易日历要额外一次取数，而"多问一天休市日"服务端返回空、无害；
#:   反过来按交易日切会把"这天是不是交易日"变成分段器的前置依赖）。
WINDOW_BOUNDS = "closed-closed: [start, end], both inclusive"

_ONE_DAY = _dt.timedelta(days=1)


def _to_date(v: Any) -> _dt.date:
    """任意日期写法 → `date`。⛔ 判不出来就抛，不猜。"""
    s = _norm(v)
    if len(s) != 8 or not s.isdigit():
        raise ValueError(
            f"日期 {v!r} 归一化后是 {s!r}，不是 8 位 YYYYMMDD ⇒ ⛔ 不猜格式。"
            "分段的端点算术必须建立在可判定的日期上（边界是这类代码唯一的失效点）。")
    return _dt.date(int(s[:4]), int(s[4:6]), int(s[6:]))


@dataclass(frozen=True)
class Window:
    """一个**闭区间** `[start, end]`，两端都含（见 `WINDOW_BOUNDS`）。

    Attributes:
        start: `YYYYMMDD`，含。
        end: `YYYYMMDD`，含。
    """
    start: str
    end: str

    def __post_init__(self) -> None:
        s, e = _to_date(self.start), _to_date(self.end)
        # ⛔ 允许 start == end（单日窗口是合法的、且是最容易写错的那一种）。
        if s > e:
            raise ValueError(f"窗口 {self.start}..{self.end} 的左端点晚于右端点")
        object.__setattr__(self, "start", _norm(self.start))
        object.__setattr__(self, "end", _norm(self.end))

    @property
    def days(self) -> int:
        """本窗口**含**的日历日天数。⭐ 闭区间 ⇒ `+1`。"""
        return (_to_date(self.end) - _to_date(self.start)).days + 1

    def __str__(self) -> str:
        return f"[{self.start},{self.end}]"


def segment_window(start: Any, end: Any, *,
                   segments: int | None = None,
                   days_per_segment: int | None = None) -> list[Window]:
    """把闭区间 `[start, end]` 切成**恰好覆盖它一次**的若干闭区间。

    只能给 `segments` 或 `days_per_segment` 之一。

    Args:
        start: 区间左端点，**含**。
        end: 区间右端点，**含**。
        segments: 要切成几段（按日历日尽量均分，余数摊到**前面**几段）。
        days_per_segment: 每段最多几个**日历日**。

    Returns:
        升序、首尾端点与入参严格相等、相邻段 `next.start == prev.end + 1 天` 的窗口表。

    Raises:
        ValueError: 参数矛盾，或产出的分割**没能通过** `assert_partition()`。
            ⭐ 本函数**自己校验自己的输出** —— ⛔ 不把校验留给调用方：
              `FINDING-592` 的教训是"没人跑的判据等于没有判据"，而这里的判据
              恰好在同一次调用里就能跑完。
    """
    if (segments is None) == (days_per_segment is None):
        raise ValueError("`segments` 与 `days_per_segment` 必须给且只给一个")
    s, e = _to_date(start), _to_date(end)
    if s > e:
        raise ValueError(f"区间 {_norm(start)}..{_norm(end)} 的左端点晚于右端点")
    total = (e - s).days + 1          # ⭐ 闭区间 ⇒ +1

    if days_per_segment is not None:
        if days_per_segment < 1:
            raise ValueError(f"days_per_segment={days_per_segment} 必须 ≥1")
        sizes = [days_per_segment] * (total // days_per_segment)
        if total % days_per_segment:
            sizes.append(total % days_per_segment)
    else:
        assert segments is not None
        if segments < 1:
            raise ValueError(f"segments={segments} 必须 ≥1")
        if segments > total:
            # ⛔ 不静默缩减段数：调用方要 10 段而只有 3 天时，"给你 3 段"
            #   与"给你 10 段"在返回值上无法区分，而它影响的是并发度与节流预算。
            raise ValueError(
                f"要求 {segments} 段，但 [{_norm(start)},{_norm(end)}] 只有 {total} "
                f"个日历日 ⇒ 无法切出这么多**非空**段。⛔ 不静默缩减段数。")
        base, extra = divmod(total, segments)
        sizes = [base + 1] * extra + [base] * (segments - extra)

    out: list[Window] = []
    cur = s
    for n in sizes:
        w_end = cur + _dt.timedelta(days=n - 1)   # ⭐ 闭区间 ⇒ n 天的右端点是 start+n-1
        out.append(Window(cur.strftime("%Y%m%d"), w_end.strftime("%Y%m%d")))
        cur = w_end + _ONE_DAY                    # ⭐ 下一段从**次日**开始（不重叠）
    assert_partition(out, start, end)
    return out


def assert_partition(windows: Sequence[Window], start: Any, end: Any) -> None:
    """断言 `windows` 是 `[start, end]` 的一个**精确分割**。⛔ 违反即抛。

    逐条判据（⭐ 每一条都对应一种真实的写错方式，⛔ 不是形式主义）：

    1. 非空 —— 空表"覆盖了零天"却不会在任何逐对检查里报错。
    2. 升序 —— 乱序下"相邻"这个词没有意义。
    3. **左端点严格相等** `windows[0].start == start` —— 差一天就是丢一天的数据。
    4. **右端点严格相等** `windows[-1].end == end` —— 同上，且这一侧更常错：
       闭/半开混用时错的正是右端。
    5. 相邻**无缝隙且无重叠**：`next.start == prev.end + 1 天`。
       ⛔ `>` 是缝隙（丢数据）、`<=` 是重叠（重组后重复行）。
    6. ⭐⭐ **天数独立复核**：`sum(w.days) == (end-start).days + 1`。
       ⛔ 这一条**不是** 3–5 的推论式冗余，它是**另一条独立的腿**：3–5 全部按
       "相邻关系"逐对判定，而本条按**总量**判定。`FINDING-779` 家族的教训是
       自洽的算术没有破绽 —— 一个把 `days` 写成 `end-start`（漏 `+1`）的实现
       能同时通过 3、4、5，只在本条上暴露。
    """
    if not windows:
        raise ValueError("分割为空 ⇒ 覆盖了零天。⛔ 空表不算分割。")
    s, e = _to_date(start), _to_date(end)
    ws = list(windows)
    if any(_to_date(a.start) > _to_date(b.start) for a, b in zip(ws, ws[1:])):
        raise ValueError(f"分割未升序：{[str(w) for w in ws]}")
    if ws[0].start != _norm(start):
        raise ValueError(
            f"左端点不相等：分割从 {ws[0].start} 起，要求 {_norm(start)} "
            f"⇒ 少了 {(_to_date(ws[0].start) - s).days} 个日历日（边界丢数据）")
    if ws[-1].end != _norm(end):
        raise ValueError(
            f"右端点不相等：分割到 {ws[-1].end} 止，要求 {_norm(end)} "
            f"⇒ 差 {(e - _to_date(ws[-1].end)).days} 个日历日（闭/半开混用的典型症状）")
    for a, b in zip(ws, ws[1:]):
        want = _to_date(a.end) + _ONE_DAY
        got = _to_date(b.start)
        if got != want:
            kind = "缝隙（丢数据）" if got > want else "重叠（重组后重复行）"
            raise ValueError(
                f"{a} 与 {b} 之间有{kind}：闭区间下 next.start 必须等于 "
                f"prev.end+1天={want:%Y%m%d}，实得 {b.start}")
    covered = sum(w.days for w in ws)
    total = (e - s).days + 1
    if covered != total:
        raise ValueError(
            f"天数独立复核失败：各段 days 之和 {covered} ≠ 区间总日历日 {total}"
            f"（[{_norm(start)},{_norm(end)}] 闭区间）。"
            "⛔ 这条判据与相邻性判据互相独立 —— 逐对全过而总量不对，"
            "说明 `days` 的口径本身错了（典型：漏 `+1`）。")


#: ⭐ `capability_router.Upstream.fault_domain` → `auto_probe_interfaces` 那张**已实测**的
#: 风控域键。⛔⛔ 为什么必须显式映射、⛔ 不能直接拿 `fault_domain` 当键：
#:   两套词汇是**独立演化**的。实测（2026-08-23）`rate_limit_domain("akshare",
#:   "stock_zh_a_hist_tx")` 回 `"other"`，而 router 侧该候选的 `fault_domain` 是
#:   `"tencent"`（证据是读**已安装包源码**得到的 `proxy.finance.qq.com`）——
#:   ⇒ 直接把 `"tencent"` 当键会落进 `DOMAIN_MIN_INTERVAL` 的 `.get(dom, 默认)`
#:   分支，于是"有没有实测阈值"这件事就被一个默认值悄悄抹平了。
#: ⇒ 每一条都写**出处**；查不到出处的一律 fail-closed（见 `resolve_pacing`）。
#:
#: ⭐ `rate_key=None` 的含义是**"probe 表里没有这个域，且这是已知事实"** ——
#:   ⛔ 与"我忘了映射"必须分开：前者是结论（并已选定 `_FAIL_CLOSED_INTERVAL`），
#:   后者是疏漏（由 `assert_pacing_tables_are_live()` 判红）。
#:   ⚠ 这个区分是 `FINDING-782` 的正解：`citydata-merchant-mirror` 原先靠**疏漏**
#:     落到 fail-closed 上，结果**恰好**是对的（1.5s/并发 1），于是"最该被声明的那个域
#:     根本没被声明"这件事在产物上完全看不出来。
_FAULT_DOMAIN_TO_RATE_KEY: dict[str, tuple[str | None, str]] = {
    # fault_domain: (probe 侧风控域键 或 None, 出处)
    "eastmoney": ("eastmoney", "auto_probe_interfaces.DOMAIN_MIN_INTERVAL['eastmoney']"
                               "=1.5，出处 a-stock-data/SKILL.md 东财风控阈值（社区实测 2026-05）"),
    "ths": ("ths", "DOMAIN_MIN_INTERVAL['ths']=1.2（同花顺实测加过反爬 401）"),
    "sina": ("sina", "DOMAIN_MIN_INTERVAL['sina']=0.8；⚠ FINDING-338 实测 sina 按 IP 限流、"
                     "封禁窗口约 10 分钟量级（546s 仍红 / 607s 已绿）"),
    # ⚠ 腾讯**不在** DOMAIN_MIN_INTERVAL 表里 ⇒ 落 `other`=0.5s。
    #   出处是 auto_probe_interfaces.py:1647 的注释「通达信/腾讯据 SKILL.md 实测"不封 IP"」。
    #   ⛔ 该注释是**别处文档的转述**，不是本仓的实测 ⇒ 故仍按 `other` 的 0.5s 节流，
    #     ⛔ 不因"据说不封"就放开。
    "tencent": ("other", "不在 DOMAIN_MIN_INTERVAL 表内 ⇒ other=0.5s；"
                         "auto_probe_interfaces.py:1647 注释：腾讯据 SKILL.md 实测不封 IP"),
    # ⭐ tdx 是裸 TCP 7709，不受 HTTP 风控约束 ⇒ probe 表里的 `local`（0.0s）。
    "tdx": ("local", "DOMAIN_MIN_INTERVAL['local']=0.0（裸 TCP / 本地读取，"
                     "不受 HTTP 风控约束）；endpoint_family() 对 TDX_LIBS 亦返 'local'"),
    # ⭐⭐ 注册表里**最多**的一个域（26 个候选里 13 条走它）。probe 表无条目，
    #   而 `rate_limit_domain("citydata", …)` 实测返 `other`（0.5s）——
    #   ⛔ **刻意不采用那个 0.5s**：`FINDING-339` 记录商家侧封号风险、
    #   `citydata_source.py` 模块 docstring 明写「商家明示换代理即封号」，
    #   ⇒ 一次封号打掉的是**13 条腿**（`independence_basis` 的 mirror-only 那一族
    #   会同时掉回单点）。故显式选定最严间隔，并把这个**选择**写在这里。
    "citydata-merchant-mirror": (
        None, "probe 表无条目（rate_limit_domain 返 other=0.5s）⇒ ⛔ 刻意不用那个值："
              "FINDING-339 记录商家封号风险、citydata_source.py 明示换代理即封号，"
              "且注册表 13/26 条候选走本域 ⇒ 一次封号即多能力同时掉回单点 "
              f"⇒ 显式取最严 {1.5}s"),
    # ⚠ 交易所自营域：probe 表无条目、本仓亦无限流实测 ⇒ 如实按 fail-closed 处理，
    #   ⛔ 不因"交易所应该很稳"就放宽（那是推测，不是实测）。
    "sse": (None, "上交所自营域，probe 表无条目、本仓无限流实测 ⇒ 取最严间隔（未实测，非结论）"),
    "szse": (None, "深交所自营域，probe 表无条目、本仓无限流实测 ⇒ 取最严间隔（未实测，非结论）"),
}

#: 每个**故障域**同时允许几个分段在飞。⛔ 这一栏与 `min_interval` 不是一回事：
#:   间隔约束**请求速率**，并发上限约束**在飞请求数**。两者都得有。
#:
#: ⛔⛔ 键是 `fault_domain`（router 的词汇），⛔ **不是** probe 的 `rate_key` ——
#:   `FINDING-782`：原先按 `rate_key` 键，于是 `"citydata"` 这一项**任何真实
#:   fault_domain 都到不了**（真值是 `citydata-merchant-mirror`，且当时未映射）
#:   ⇒ 一条永远不生效的配置，正是 `FINDING-408` ①「上限结构上不可达」的形状。
#:   现由 `assert_pacing_tables_are_live()` 在 import 时判红。
#:
#: ⛔⛔ 全部为 1（串行），且这是**外部文档定的铁律不是我的保守**：
#:   `auto_probe_interfaces.py:1636` 引 `a-stock-data/SKILL.md`——
#:   「铁律是"**串行不并发** + 间隔 ≥1s + 抖动 + 复用会话 + 带 UA"」，
#:   并明写「**AI 跑批量循环逐个拉龙虎榜/资金流是被封的头号元凶**」。
#:   ⇒ 为了让"提速"这个数字好看而把它调大，就是**为过检查改标准**。
#: ⚠ `tdx` 虽然 `min_interval=0`（裸 TCP、无 HTTP 风控），并发上限**仍是 1**——
#:   理由不是风控而是 `FINDING-780`：`finai/tdx_minute5.py:773 _save_healthy_node()`
#:   是无锁 read-modify-write + 非原子 `write_text()`，本轮离线实测 8 线程 × 300 次
#:   之后 `final_file_parses=false`，且坏文件会让 `_connect_tdx_api()` 把**已连上且
#:   已通过健全性检查**的节点当失败丢掉 ⇒ 两台健康节点被丢（实测 `disconnected=2`）
#:   ⇒ 并行跑 tdx 会把这条唯一活着的 `ohlcv_daily` 腿打死。
_DOMAIN_CONCURRENCY: dict[str, int] = {
    "eastmoney": 1,
    "ths": 1,
    "sina": 1,
    "tencent": 1,
    "tdx": 1,                        # ⛔ FINDING-780：缓存竞争，不是风控
    "citydata-merchant-mirror": 1,   # ⚠ FINDING-339：商家封号风险
    "sse": 1,
    "szse": 1,
}

#: 查不到风控域时用的间隔：**全表最严的那一个**，⛔ 不是最松的。
#: ⭐ 判据方向：漏声明时势必要有个默认值，而 fail-closed 的方向是**更慢**。
#:   反过来（默认 0.5s）会让"忘了填映射"自动变成"这个域可以打得更快"。
_FAIL_CLOSED_INTERVAL = 1.5


def registered_fault_domains() -> set[str]:
    """`CAPABILITIES` 里**实际出现**的故障域。⭐ 判"表还活着吗"的分母。"""
    out = set()
    for cands in _cr.CAPABILITIES.values():
        for c in cands:
            out.add((c.upstream.fault_domain if c.upstream is not None else "")
                    or "?undeclared")
    return out


def assert_pacing_tables_are_live() -> None:
    """**import 即自检**：节流两张表既不得有死键，也不得漏掉真实存在的域。

    ⛔⛔ `FINDING-782` 的落点，判据方向与 `auto_probe_interfaces.
    assert_window_budgets_reachable()` 完全相同（那条是 `FINDING-408` ① 的落点）：
    **一条永远不生效的限流配置比没有配置更坏**，因为它在产物里看起来像有配置。

    两条判据：
      ① `_DOMAIN_CONCURRENCY` 的每个键都必须是**真实存在**的 `fault_domain`
         ⇒ 否则那一项永远读不到，是死配置。
      ② `CAPABILITIES` 里每个 `fault_domain` 都必须在 `_FAULT_DOMAIN_TO_RATE_KEY`
         里**显式**出现 ⇒ 否则它靠 `resolve_pacing` 的疏漏分支取值，而那个分支
         **恰好**给出安全的数字 ⇒ "漏声明"与"已声明为最严"在运行时同形。
    """
    live = registered_fault_domains()
    dead = sorted(set(_DOMAIN_CONCURRENCY) - live)
    assert not dead, (
        f"_DOMAIN_CONCURRENCY 有死键 {dead}：`CAPABILITIES` 里没有任何候选的 "
        f"fault_domain 等于它们 ⇒ 这些上限永远不生效（FINDING-782；"
        f"与 FINDING-408 ① 同形）。实际存在的域：{sorted(live)}")
    unmapped = sorted(live - set(_FAULT_DOMAIN_TO_RATE_KEY) - {"?undeclared"})
    assert not unmapped, (
        f"故障域 {unmapped} 未在 _FAULT_DOMAIN_TO_RATE_KEY 显式声明 ⇒ 它们会走 "
        "resolve_pacing 的疏漏分支。⚠ 那个分支给的数字**恰好**是安全的，"
        "于是'漏声明'与'已判定为最严'在运行时完全同形（FINDING-782）。"
        "⇒ 请显式声明，rate_key=None 表示'probe 表无条目、刻意取最严'。")


assert_pacing_tables_are_live()   # ⛔ import 即自检，死配置/漏声明立刻变红


@dataclass(frozen=True)
class Pacing:
    """一个故障域的节流参数 + **这些数字是怎么来的**。

    Attributes:
        fault_domain: `capability_router.Upstream.fault_domain` 原值。
        rate_key: probe 侧风控域键（`auto_probe_interfaces.rate_limit_domain()` 的值域）。
        min_interval_s: 同域两次**派发**之间的最小间隔。
        max_concurrent: 同域**在飞**分段数上限。
        window_limit: 5 分钟滑动窗口内的自设上限；`None` = 该域未设窗口预算。
        basis: 上面这几个数的出处。⛔ 空串不合法 —— 无出处的阈值等于猜。
    """
    fault_domain: str
    rate_key: str
    min_interval_s: float
    max_concurrent: int
    window_limit: int | None
    basis: str


def _probe_tables() -> tuple[dict[str, float], dict[str, int], float]:
    """读 `auto_probe_interfaces` 的三张**已实测**限流表。⛔ 不在本模块另抄数字。

    ⚠ 它在 `scripts/` 下而本模块在 `finai/` 下 ⇒ 需要把仓库根加进 `sys.path`。
      ⛔ 这个方向的依赖（`finai` → `scripts`）本身不理想，但另一个选项是**把三张表
        的数字抄一份到这里**，而那正是 `FINDING-387`（一个事实两份实现）。
      ⇒ 取"依赖方向不好看"而不取"数字分叉"：分叉的代价是限流阈值静默过期，
        而那一条会**打死数据获取能力本身**。
    """
    import sys
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    scripts = str(Path(root) / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import auto_probe_interfaces as ap  # noqa: PLC0415

    return ap.DOMAIN_MIN_INTERVAL, ap.DOMAIN_WINDOW_LIMIT, float(ap.WINDOW_SECONDS)


def resolve_pacing(fault_domain: str) -> Pacing:
    """一个故障域的节流参数。⛔ 未登记映射时 fail-closed 到最严间隔。

    ⭐⭐ 三条分支都**必须**读 `_DOMAIN_CONCURRENCY`（`FINDING-782` ③）：
      原实现的 fail-closed 分支把 `max_concurrent` **写死为 1**，于是那张表对
      未映射的域**完全无效** —— 而当时注册表里最大的那个域正好未映射
      ⇒ 我为了测调度器并行度而抬高 `_DOMAIN_CONCURRENCY`，实测速比恒为 1.00x，
        我差点把它读成"这个调度器不会并行"。⛔ 真相是那次测量**什么也没测到**。
    """
    intervals, limits, _ = _probe_tables()
    # ⭐ 并发上限**恒**从表里读（键是 fault_domain），⛔ 不在任何分支里写死。
    cap = _DOMAIN_CONCURRENCY.get(fault_domain, 1)
    mapped = _FAULT_DOMAIN_TO_RATE_KEY.get(fault_domain)
    if mapped is None:
        # ⛔ 不抛：一个没登记映射的域**仍然应该能被取数**，只是必须按最严的节奏。
        #   ⚠ 但 basis 里必须把"这是兜底、不是实测"说出来 —— 否则它读起来和
        #     有实测出处的那些**一模一样**（`FINDING-407` 反对的"编一个自信标签"）。
        #   ⚠ 正常路径上到不了这里：`assert_pacing_tables_are_live()` 在 import 时
        #     就会为任何未声明的**注册表内**域判红。它只兜住"运行时构造的新域"。
        return Pacing(
            fault_domain=fault_domain, rate_key="?unmapped",
            min_interval_s=_FAIL_CLOSED_INTERVAL, max_concurrent=cap,
            window_limit=None,
            basis=(f"⚠ fault_domain={fault_domain!r} 未登记到风控域映射 ⇒ "
                   f"fail-closed：间隔取全表最严 {_FAIL_CLOSED_INTERVAL}s。"
                   "⛔ 这不是实测阈值，是兜底。要实测请补 _FAULT_DOMAIN_TO_RATE_KEY。"))
    rate_key, src = mapped
    # ⭐ `rate_key is None` = probe 表无条目且这是**已判定的结论**（见该表 docstring）
    #   ⇒ 取最严间隔，⛔ 但 basis 写的是"为什么这么判"，不是"我忘了"。
    interval = (_FAIL_CLOSED_INTERVAL if rate_key is None
                else float(intervals.get(rate_key, _FAIL_CLOSED_INTERVAL)))
    return Pacing(
        fault_domain=fault_domain, rate_key=rate_key or "?no-probe-entry",
        min_interval_s=interval, max_concurrent=cap,
        window_limit=None if rate_key is None else limits.get(rate_key),
        basis=src)


class DomainPacer:
    """按**故障域**限并发 + 保最小派发间隔 + 5 分钟窗口退避。线程安全。

    ⭐ 与 `auto_probe_interfaces.Throttle` 的关系：阈值**全部**取自那张表
      （`resolve_pacing`），本类只补它没有的那一件事 —— **并发上限**（`Throttle`
      是单线程串行探针用的，没有信号量）。⛔ 不复制它的数字。

    ⛔ 最小间隔只在**派发**那一刻收敛，⛔ 不覆盖整个请求时长 ——
      风控约束的是**请求速率**，而不是"同时有几个连接开着"。若把间隔按
      "上一次请求结束"计，`max_concurrent>1` 就永远无效（并发变成一句空话）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sems: dict[str, threading.Semaphore] = {}
        self._next_at: dict[str, float] = {}
        self._hits: dict[str, list[float]] = {}
        #: 逐次派发留证：`(fault_domain, waited_s, reason)`。⛔ 静默 sleep 会让
        #: "这批数据是在什么速率下取的"变成不可复核的信息（`FINDING-125` 的形状）。
        self.log: list[dict[str, Any]] = []

    def _sem(self, p: Pacing) -> threading.Semaphore:
        with self._lock:
            sem = self._sems.get(p.fault_domain)
            if sem is None:
                sem = threading.Semaphore(p.max_concurrent)
                self._sems[p.fault_domain] = sem
            return sem

    def acquire(self, p: Pacing) -> None:
        """占一个该域的并发位，并等到允许派发的时刻。"""
        self._sem(p).acquire()
        _, limits, window_s = _probe_tables()
        while True:
            with self._lock:
                now = time.time()
                wait = max(0.0, self._next_at.get(p.fault_domain, 0.0) - now)
                reason = "min_interval"
                if wait <= 0 and p.window_limit:
                    hits = [t for t in self._hits.get(p.fault_domain, [])
                            if now - t < window_s]
                    if len(hits) >= p.window_limit:
                        wait = window_s - (now - hits[0]) + 1.0
                        reason = "window_limit"
                    self._hits[p.fault_domain] = hits
                if wait <= 0:
                    self._next_at[p.fault_domain] = now + p.min_interval_s
                    if p.window_limit:
                        self._hits.setdefault(p.fault_domain, []).append(now)
                    return
                self.log.append({"fault_domain": p.fault_domain,
                                 "waited_s": round(wait, 3), "reason": reason})
            time.sleep(wait)

    def release(self, p: Pacing) -> None:
        self._sem(p).release()


@dataclass(frozen=True)
class Containment:
    """一段实得数据**是否落在它被要求的窗口里**，以及落在哪里。

    ⭐⭐ 本类型存在的唯一理由是 `FINDING-781`：`tdx::daily_bar` 对
      `20260601..20260630` 回 15 行、首日 `2026-06-09`（少 6 个交易日），
      `state=OK`、`coverage=""` 声明全量 ⇒ **调用方从返回值上看不出缺口**。
      ⇒ 分段路径必须逐段把实得端点摊开，⛔ 不把缺口平均到重组帧里。

    ⚠ `inside` 全中**不等于**"这段完整"：源可以在窗口内部少给行，那需要交易日历
      才能判（本模块刻意不引入那个依赖，见 `segment_window` 的日历日理由）。
      ⇒ 本类型回答的是"有没有跑到窗口外"与"实得端点在哪"，⛔ 不是"有没有缺行"。

    Attributes:
        verified: 是否真的做了检查。`False` = 调用方没声明 `date_col` ⇒
            ⭐ 如实说"没验"，⛔ 不算通过（`FINDING-592`：没跑的判据不是判据）。
        rows_before_start / rows_after_end: 跑到窗口外的行数（应恒为 0）。
        first_date / last_date: 实得端点。
        short_at_start_days / short_at_end_days: 实得端点与要求端点的日历日差
            —— ⚠ 非 0 **不一定**是缺陷（端点可能是休市日），故只报数、不判罪。
    """
    verified: bool
    rows_before_start: int = 0
    rows_after_end: int = 0
    first_date: str | None = None
    last_date: str | None = None
    short_at_start_days: int | None = None
    short_at_end_days: int | None = None

    @property
    def escaped(self) -> bool:
        """是否有行跑到窗口**外面** —— 这一条非 0 就是确定的缺陷（分段会造成重复）。"""
        return bool(self.rows_before_start or self.rows_after_end)


def window_containment(frame: pd.DataFrame, w: Window,
                       date_col: str | None) -> Containment:
    """这段实得的行是否都落在 `w` 内（闭区间），以及实得端点在哪。⛔ 不改帧。"""
    if not date_col or frame is None or date_col not in getattr(frame, "columns", []):
        return Containment(verified=False)
    dates = frame[date_col].map(_norm)
    if not len(dates):
        return Containment(verified=True, first_date=None, last_date=None)
    lo, hi = w.start, w.end
    # ⭐ 定长零填充的 YYYYMMDD 上，字典序 == 时间序 ⇒ 直接字符串比较
    #   （与 `capability_router._cal_ge/_cal_le` 同一条判据，⛔ 不引入 datetime 转换）。
    first, last = min(dates), max(dates)
    return Containment(
        verified=True,
        rows_before_start=int((dates < lo).sum()),
        rows_after_end=int((dates > hi).sum()),
        first_date=first, last_date=last,
        short_at_start_days=(_to_date(first) - _to_date(lo)).days,
        short_at_end_days=(_to_date(hi) - _to_date(last)).days,
    )


@dataclass
class SegmentResult:
    """一个分段的取数结果 + 归属检查 + 它实际问了谁。"""
    window: Window
    ok: bool
    rows: int
    used: str | None
    schema: str | None
    coverage: str
    attempts: list[tuple[str, str, int]]
    elapsed_s: float
    containment: Containment
    #: `session_coverage()` 的判决（`FINDING-781`）。⛔ 不得省略：`containment` 只答
    #: 「实得的行有没有跑出窗口」，答不了「窗口内该有的天数到齐没有」——tdx 实测
    #: `state=OK` 少 6 个交易日，正落在这个缺口里。
    session_coverage: dict[str, Any] | None = None
    frame: pd.DataFrame | None = None
    error: str | None = None


@dataclass
class SegmentedPull:
    """分段取数的完整结果。⭐ 缺口与混源在这里是**可读字段**，⛔ 不是日志里的一句话。

    Attributes:
        frame: 重组帧；任一段失败且未允许失败时为 `None`。
        segments: 逐段结果（含各自实得端点）。
        schema: 全程被锁死的口径 —— ⭐ 逐段传的是**同一个值**。
        sources_used: 各段实际用到的候选 key 集合。⚠ 势集 >1 ⇒ **沿时间轴混源**。
        coverages_seen: 各段的 `coverage` 声明集合。⛔ 势集 >1 ⇒ 时间轴上覆盖面不一致，
            这是 `FINDING-361` 在时间维度的同构形状，必须让调用方看见。
        wall_s: 墙钟耗时（含节流等待）。
        pacing: 每个故障域实际用的节流参数。
        pacer_log: 逐次派发的等待留证。
        duplicate_rows: 重组后按 `dedupe_on` 判定的重复行数（未声明 `dedupe_on` 时为 `None`）。
    """
    frame: pd.DataFrame | None
    segments: list[SegmentResult]
    schema: str | None
    sources_used: tuple[str, ...]
    coverages_seen: tuple[str, ...]
    wall_s: float
    pacing: dict[str, Pacing] = field(default_factory=dict)
    pacer_log: list[dict[str, Any]] = field(default_factory=list)
    duplicate_rows: int | None = None

    @property
    def failed(self) -> list[SegmentResult]:
        return [s for s in self.segments if not s.ok]

    @property
    def mixed_sources(self) -> bool:
        """各段是否来自**不同**候选。⚠ 不必然是缺陷，但重组帧沿时间轴换了源。"""
        return len(self.sources_used) > 1

    @property
    def escaped_windows(self) -> list[SegmentResult]:
        """有行跑到自己窗口外的段 —— 确定的缺陷（重组必然重复）。"""
        return [s for s in self.segments if s.containment.escaped]

    def warnings(self) -> list[str]:
        """可直接落日志的人话。⛔ 刻意不 raise：混源/端点短缺不必然是缺陷。"""
        out: list[str] = []
        if self.mixed_sources:
            out.append(
                f"⚠ 沿时间轴混源：{len(self.sources_used)} 个候选参与了重组"
                f"（{', '.join(self.sources_used)}）⇒ 重组帧不同时间段来自不同源。"
                "⛔ 勿当单源数据做血缘声明。")
        if len(self.coverages_seen) > 1:
            out.append(
                f"⚠ 各段 coverage 声明不一致：{self.coverages_seen} ⇒ 重组帧的覆盖面"
                "沿时间轴变化（FINDING-361 的时间维同构）。⛔ 勿当全量使用。")
        for s in self.segments:
            c = s.containment
            if c.escaped:
                out.append(
                    f"⛔ {s.window} 有行跑到窗口外："
                    f"早于左端 {c.rows_before_start} 行、晚于右端 {c.rows_after_end} 行"
                    f"（used={s.used}）⇒ 重组会产生重复行。")
            elif c.verified and c.short_at_start_days:
                out.append(
                    f"⚠ {s.window} 实得首日 {c.first_date}，比要求左端晚 "
                    f"{c.short_at_start_days} 个日历日（used={s.used}）"
                    "—— 可能是休市日，也可能是源静默少给（FINDING-781）。")
        unverified = [str(s.window) for s in self.segments if not s.containment.verified]
        if unverified:
            out.append(
                f"⚠ {len(unverified)} 段**未做**区间归属检查（未声明 `date_col`）"
                "⇒ ⛔ 这不是'检查通过'，是'没检查'。")
        return out

    def raise_if_incomplete(self) -> pd.DataFrame:
        """要么给出重组帧，要么响亮失败。⛔ 绝不返回部分帧冒充完整。"""
        if self.frame is None or self.failed:
            detail = "；".join(
                f"{s.window}→{'OK' if s.ok else (s.error or 'FAIL')}"
                f"({s.rows}行, used={s.used})" for s in self.segments)
            raise RuntimeError(
                f"分段取数不完整：{len(self.failed)}/{len(self.segments)} 段未取到数据。"
                f"逐段实测：{detail}。"
                "⛔ 未返回部分帧冒充完整（FINDING-178 的分段形状："
                "少一段就是少一整个时间区间，而重组帧上看不出来）。")
        return self.frame


def _locked_schema(capability: str, schema: str | None) -> str:
    """本次分段全程使用的口径。⛔ 一次定死，逐段传同一个值。

    ⭐ 为什么必须**显式**钉死而不是让每段自己锁：`get()` 未传 `schema` 时用
      `cands[0].schema` 作为锁（`capability_router.py:1487`）—— 那是**每次调用各自**
      锁一次。若注册表在两段之间被改动、或某段的首选候选被 `SKIP_UNHONORED` 跳过，
      各段就可能落在不同口径上，而重组帧会把两种口径拼在一条时间轴上
      （`FINDING-343` 的形状，只是缝在时间维度）。
    """
    cands = _cr.CAPABILITIES.get(capability)
    if not cands:
        raise KeyError(f"未登记的能力 {capability!r}。已登记：{sorted(_cr.CAPABILITIES)}")
    if schema:
        if not any(c.schema == schema for c in cands):
            raise KeyError(f"能力 {capability!r} 下没有 schema={schema!r} 的候选")
        return schema
    return cands[0].schema


def _domains_for(capability: str, schema: str) -> dict[str, Pacing]:
    """该 `(能力, schema)` 锁下涉及的故障域 → 节流参数。

    ⚠ 漏声明 `upstream` 的候选折叠到 `"?undeclared"` ⇒ 走 `resolve_pacing` 的
      fail-closed 分支（最严间隔 + 并发 1）。⭐ 与 `independent_fault_domains()`
      的哨兵折叠同向：漏填只会让我们**更慢**，⛔ 不会更快。
    """
    out: dict[str, Pacing] = {}
    for c in _cr.CAPABILITIES[capability]:
        if c.schema != schema:
            continue
        dom = (c.upstream.fault_domain if c.upstream is not None else "") or "?undeclared"
        if dom not in out:
            out[dom] = resolve_pacing(dom)
    return out


def pull_segmented(
    capability: str,
    *,
    start: Any,
    end: Any,
    schema: str | None = None,
    segments: int | None = None,
    days_per_segment: int | None = None,
    windows: Sequence[Window] | None = None,
    window_params: tuple[str, str] = ("start_date", "end_date"),
    date_col: str | None = None,
    dedupe_on: Sequence[str] | None = None,
    allow_failed_segments: bool = False,
    getter: Callable[..., Any] | None = None,
    **params: Any,
) -> SegmentedPull:
    """把 `[start, end]` 分段，各段并行调 `capability_router.get()`，重组成一帧。

    ⛔⛔ **换源不在本函数里** —— 每段调的就是 `get()`，它的失败链、口径锁、
      `coverage` 运行时声明全部原样生效。本函数**没有**第二条候选选择逻辑
      （`FINDING-387`：一个事实一份实现）。

    ⚠ **并发度的真实上界**：同 schema 下 `get()` 恒按注册表顺序试候选 ⇒ 所有段都会
      先落在**同一个**候选上 ⇒ 并发受**那一个故障域**的 `max_concurrent` 约束，而
      `_DOMAIN_CONCURRENCY` 目前**全为 1**（外部铁律「串行不并发」+ `FINDING-780`）。
      ⇒ ⭐ 本函数当前在真实源上**不会**比单次请求快；它换来的是
        ① 边界可断言、② 单段失败可定位到具体区间、③ 超出源单次上限的区间可取。
      ⛔ 把 `_DOMAIN_CONCURRENCY` 调大来让"提速"这个数字好看，就是为过检查改标准。

    Args:
        capability: `CAPABILITIES` 里的能力名。
        start / end: 闭区间两端，**都含**（见 `WINDOW_BOUNDS`）。
        schema: 口径；省略则取该能力首个候选的 schema，并**逐段传同一个值**。
        segments / days_per_segment / windows: 三者给且只给一个。
            `windows` 走**调用方自备**分割，仍会被 `assert_partition()` 校验
            ⇒ ⭐ 这条路径就是变异测试注入"缝隙/重叠"的入口。
        window_params: 该能力用哪两个统一参名承载区间。默认 `("start_date","end_date")`。
        date_col: 实得帧里的日期列名；给了才做区间归属检查。
            ⛔ 不给不是"通过"，是 `Containment.verified=False`。
        dedupe_on: 重组后按这些列判重复并去重；省略则不去重、`duplicate_rows=None`。
            ⚠ 闭区间无缝分割下**本不该**有重复 ⇒ 它非 0 就是缺陷证据（源无视了窗口）。
        allow_failed_segments: 允许部分段失败并返回部分帧。默认 `False`。
        getter: 注入点，仅测试用。默认 `capability_router.get`。

    Returns:
        `SegmentedPull`。
    """
    given = [x is not None for x in (segments, days_per_segment, windows)]
    if sum(given) != 1:
        raise ValueError("`segments` / `days_per_segment` / `windows` 必须给且只给一个")
    if windows is not None:
        wins = list(windows)
        # ⭐ 自备分割**同样**要过判据 —— ⛔ "调用方自己算的"不是豁免理由。
        assert_partition(wins, start, end)
    else:
        wins = segment_window(start, end, segments=segments,
                              days_per_segment=days_per_segment)

    locked = _locked_schema(capability, schema)
    known = _cr.CAPABILITY_PARAMS.get(capability)
    if known is not None:
        bad = [p for p in window_params if p not in known]
        if bad:
            raise ValueError(
                f"能力 {capability!r} 的统一参名是 {list(known)}，"
                f"承载区间的 {bad} 不在其中 ⇒ ⛔ 传下去会被 `get()` 判 TypeError。"
                "改传正确的 `window_params`，或该能力不适合按时间分段。")

    pacing = _domains_for(capability, locked)
    # ⭐ 池大小 = 该锁下**最小**的并发上限。⛔ 不取最大：`get()` 会从首选开始试，
    #   所以最坏情况下所有段都压在同一个域上 ⇒ 按最松的域开池会越过最严那个域的上限。
    pool = max(1, min((p.max_concurrent for p in pacing.values()), default=1))
    # ⭐ 段间节流用**最严**的间隔（同一理由：不知道会落在哪个域，取更慢的那个）。
    strictest = max((p for p in pacing.values()),
                    key=lambda p: p.min_interval_s, default=None)
    pacer = DomainPacer()
    fn = getter or _cr.get

    def one(w: Window) -> SegmentResult:
        if strictest is not None:
            pacer.acquire(strictest)
        t0 = time.time()
        try:
            call = dict(params)
            call[window_params[0]] = w.start
            call[window_params[1]] = w.end
            # ⭐ `pace` 传该域的最小间隔 ⇒ 一段内部**换源**时也带节奏
            #   （`get()` 的 `pace` 是候选之间的间隔，`FINDING-338`）。
            r = fn(capability, schema=locked,
                   pace=(strictest.min_interval_s if strictest else 0.0), **call)
        except Exception as exc:  # noqa: BLE001 — 一段炸掉不该打死整条分段
            return SegmentResult(
                window=w, ok=False, rows=0, used=None, schema=locked, coverage="",
                attempts=[], elapsed_s=round(time.time() - t0, 3),
                containment=Containment(verified=False),
                error=f"{type(exc).__name__}: {exc}")
        finally:
            if strictest is not None:
                pacer.release(strictest)
        elapsed = round(time.time() - t0, 3)
        # ⚠ 调用方没声明 `date_col` 时**解析**一个（`resolve_date_col`），否则两个守卫
        #   都会静默退化成「未验」—— 实测 tdx 那条 15 行少 6 天的帧就是这样溜过去的。
        _dc = resolve_date_col(r.frame, date_col) if r.ok else None
        cont = window_containment(r.frame, w, _dc) if r.ok else Containment(False)
        # ⛔ `FINDING-781`：ok=True 且行数非 0 **不代表**窗口内交易日到齐。
        cov = session_coverage(r.frame, w, _dc) if r.ok else None
        if cov is not None:
            cov = {**cov, 'date_col_used': _dc,
                   'date_col_source': 'declared' if date_col else 'resolved'}
        return SegmentResult(
            window=w, ok=bool(r.ok), rows=int(r.rows), used=r.used, schema=r.schema,
            coverage=r.coverage, attempts=list(r.attempts), elapsed_s=elapsed,
            containment=cont, session_coverage=cov, frame=r.frame,
            error=None if r.ok else "get() 返回 ok=False")

    t_wall = time.time()
    if pool == 1:
        results = [one(w) for w in wins]
    else:
        with ThreadPoolExecutor(max_workers=pool) as ex:
            results = list(ex.map(one, wins))
    wall = round(time.time() - t_wall, 3)

    ok_frames = [s.frame for s in results if s.ok and s.frame is not None]
    used = tuple(sorted({s.used for s in results if s.used}))
    covs = tuple(sorted({s.coverage for s in results if s.ok}))
    dup: int | None = None
    frame: pd.DataFrame | None = None
    if ok_frames and (allow_failed_segments or all(s.ok for s in results)):
        # ⭐ 按**窗口顺序**拼接（`results` 与 `wins` 同序，`ex.map` 保序）。
        #   ⛔ 不 sort：重组顺序应是"分段顺序"这一可解释的顺序；要别的顺序由调用方排。
        frame = pd.concat(ok_frames, ignore_index=True)
        if dedupe_on:
            miss = [c for c in dedupe_on if c not in frame.columns]
            if miss:
                raise KeyError(
                    f"dedupe_on 里的列 {miss} 不在重组帧上（实得 {list(frame.columns)}）"
                    "⇒ ⛔ 不静默跳过去重：那会让'没重复'与'没检查'读起来一样。")
            dup = int(frame.duplicated(subset=list(dedupe_on)).sum())
            frame = frame.drop_duplicates(subset=list(dedupe_on), ignore_index=True)
    return SegmentedPull(
        frame=frame, segments=results, schema=locked, sources_used=used,
        coverages_seen=covs, wall_s=wall, pacing=pacing,
        pacer_log=pacer.log, duplicate_rows=dup)


@dataclass
class Equivalence:
    """分段重组帧 与 单次取数帧 的逐项比对结果。

    ⭐ 判据是**同行、同 dtype、同值**（确定性排序后），⛔ 不是"行数一样"。
      一次少给边界行的"提速"是数据缺陷（`FINDING-781` 就是这个形状）。

    Attributes:
        equal: 三项判据是否全过。⭐ 只有它为 `True` 才叫等价。
        rows_serial / rows_segmented: 两侧行数。
        rows_only_in_serial / rows_only_in_segmented: 单侧独有行数（按全列比对）。
        dtype_diffs: `{列: (serial dtype, segmented dtype)}`。
            ⚠ 拼接可能改 dtype（某段整列 NaN ⇒ `object` 变 `float64`）
            ⇒ 这一项**单独报**，⛔ 不与值差异混为一谈：两者修法不同。
        cols_only_in_serial / cols_only_in_segmented: 列集合差异。
        value_mismatches: 同键同列但值不同的处数（仅在两侧行集合相同时有意义）。
        sort_keys: 实际用于确定性排序的列。
        sort_keys_unique_in_serial: 那组键在单次帧上是否唯一。
            ⛔ 不唯一 ⇒ 排序不确定 ⇒ 逐行比对的结论**不可信**，故此时 `equal` 强制为
            `False` 并在 `caveats` 里说明。⚠ 这不是"比对失败"，是"这次比对无效"。
        caveats: 让结论的**效力边界**可读。
    """
    equal: bool
    rows_serial: int
    rows_segmented: int
    rows_only_in_serial: int
    rows_only_in_segmented: int
    dtype_diffs: dict[str, tuple[str, str]]
    cols_only_in_serial: tuple[str, ...]
    cols_only_in_segmented: tuple[str, ...]
    value_mismatches: int
    sort_keys: tuple[str, ...]
    sort_keys_unique_in_serial: bool
    caveats: list[str] = field(default_factory=list)
    #: `pandas.testing.assert_frame_equal` 的独立裁决（`None` = 通过）。
    #: ⭐ 为什么要它：本类自己算的那几项是**我写的**判据，而 pandas 那条是**别人写的**。
    #:   两者都过才排除"我的比对器有洞"这种可能（`FINDING-338` ④ 那条纪律：
    #:   先证明自己的探针有效）。
    pandas_verdict: str | None = None


def _canon(f: pd.DataFrame, sort_keys: Sequence[str]) -> pd.DataFrame:
    """确定性排序 + 重置索引。⛔ 不改值、不改 dtype。"""
    out = f.sort_values(list(sort_keys), kind="mergesort").reset_index(drop=True)
    return out[sorted(out.columns)]


def equivalence_report(serial: pd.DataFrame, segmented: pd.DataFrame, *,
                       sort_keys: Sequence[str]) -> Equivalence:
    """分段重组帧是否与单次取数帧**逐元素相同**（确定性排序后）。

    Args:
        serial: 单次（未分段）取数的帧。
        segmented: 分段重组帧。
        sort_keys: 用于确定性排序的列。⛔ 必填、⛔ 无默认值 ——
            "随便排一下"会让比对结论随 pandas 版本漂移。

    Returns:
        `Equivalence`。⭐ `equal=True` 才叫等价。
    """
    caveats: list[str] = []
    cs, cg = set(serial.columns), set(segmented.columns)
    only_s, only_g = tuple(sorted(cs - cg)), tuple(sorted(cg - cs))
    missing = [k for k in sort_keys if k not in cs or k not in cg]
    if missing:
        raise KeyError(
            f"排序键 {missing} 不在两侧的公共列里（serial={sorted(cs)}, "
            f"segmented={sorted(cg)}）⇒ ⛔ 无法做确定性比对。")

    uniq = not serial.duplicated(subset=list(sort_keys)).any()
    if not uniq:
        caveats.append(
            f"⛔ 排序键 {tuple(sort_keys)} 在单次帧上**不唯一** ⇒ 同键行之间的顺序"
            "由 pandas 内部决定，逐行比对的结论不可信。⇒ 本次比对判为**无效**"
            "（`equal=False`），⛔ 不是判为不等价。请改用能唯一定位一行的键。")

    a, b = _canon(serial, sort_keys), _canon(segmented, sort_keys)
    dtype_diffs = {c: (str(a[c].dtype), str(b[c].dtype))
                   for c in sorted(cs & cg) if str(a[c].dtype) != str(b[c].dtype)}

    # 行集合差异：按**公共列全列**做多重集合比对（⛔ 不只比排序键：
    # 键相同而值不同的行必须算"值不同"，不能算"行相同"）。
    common = sorted(cs & cg)
    ta = a[common].astype(str).agg("\x1f".join, axis=1) if len(a) else pd.Series(dtype=str)
    tb = b[common].astype(str).agg("\x1f".join, axis=1) if len(b) else pd.Series(dtype=str)
    from collections import Counter
    ca, cb = Counter(ta), Counter(tb)
    only_rows_s = sum((ca - cb).values())
    only_rows_g = sum((cb - ca).values())

    mismatches = 0
    if len(a) == len(b) and not only_s and not only_g:
        for c in common:
            va, vb = a[c], b[c]
            # ⭐ NaN == NaN 在这里必须算相同（两侧都缺就是一致），故用 `.ne()` + 双非空。
            neq = va.ne(vb) & ~(va.isna() & vb.isna())
            mismatches += int(neq.sum())

    verdict: str | None = None
    try:
        pd.testing.assert_frame_equal(a, b, check_dtype=True, check_like=False)
    except AssertionError as exc:
        verdict = str(exc).splitlines()[0][:300]
    except Exception as exc:  # noqa: BLE001
        verdict = f"{type(exc).__name__}: {exc}"[:300]

    equal = (uniq and not only_s and not only_g and not dtype_diffs
             and only_rows_s == 0 and only_rows_g == 0 and mismatches == 0
             and verdict is None)
    return Equivalence(
        equal=equal, rows_serial=len(serial), rows_segmented=len(segmented),
        rows_only_in_serial=only_rows_s, rows_only_in_segmented=only_rows_g,
        dtype_diffs=dtype_diffs, cols_only_in_serial=only_s,
        cols_only_in_segmented=only_g, value_mismatches=mismatches,
        sort_keys=tuple(sort_keys), sort_keys_unique_in_serial=uniq,
        caveats=caveats, pandas_verdict=verdict)


def prove_equivalence(
    capability: str,
    *,
    start: Any,
    end: Any,
    sort_keys: Sequence[str],
    schema: str | None = None,
    segments: int = 3,
    date_col: str | None = None,
    window_params: tuple[str, str] = ("start_date", "end_date"),
    getter: Callable[..., Any] | None = None,
    **params: Any,
) -> dict[str, Any]:
    """跑一次「单次 vs 分段」对照并给出可归档的结论。⛔ 不写盘（由调用方落 artifacts）。

    ⭐ **顺序刻意是先单次后分段**：单次那一跑同时充当 tdx 健康节点缓存的预热
      （`FINDING-780`：并发写那个缓存会打坏它）⇒ 分段跑的时候缓存已是热的。

    ⚠ **只有 `sources_used` 势集为 1 且与单次的 `used` 相同时，本对照才在测"分段"**。
      否则它在测"两个源等不等价"，那是另一件事（`FINDING-347` 的判据），
      故这种情况下把它写进 `caveats` 而**不**当成分段的结论。
    """
    fn = getter or _cr.get
    locked = _locked_schema(capability, schema)
    pacing = _domains_for(capability, locked)
    strictest = max(pacing.values(), key=lambda p: p.min_interval_s, default=None)

    call = dict(params)
    call[window_params[0]] = _norm(start)
    call[window_params[1]] = _norm(end)
    t0 = time.time()
    ser = fn(capability, schema=locked,
             pace=(strictest.min_interval_s if strictest else 0.0), **call)
    serial_s = round(time.time() - t0, 3)

    seg = pull_segmented(capability, start=start, end=end, schema=locked,
                         segments=segments, window_params=window_params,
                         date_col=date_col, getter=getter, **params)

    out: dict[str, Any] = {
        "capability": capability, "schema": locked,
        "window": {"start": _norm(start), "end": _norm(end),
                   "bounds": WINDOW_BOUNDS},
        "segments": [str(w.window) for w in seg.segments],
        "serial": {"ok": bool(ser.ok), "rows": int(ser.rows), "used": ser.used,
                   "coverage": ser.coverage, "elapsed_s": serial_s,
                   "attempts": list(ser.attempts)},
        "segmented": {
            "ok": seg.frame is not None and not seg.failed,
            "rows": 0 if seg.frame is None else len(seg.frame),
            "sources_used": list(seg.sources_used),
            "coverages_seen": list(seg.coverages_seen),
            "wall_s": seg.wall_s,
            "per_segment": [
                {"window": str(s.window), "ok": s.ok, "rows": s.rows,
                 "used": s.used, "elapsed_s": s.elapsed_s,
                 "first_date": s.containment.first_date,
                 "last_date": s.containment.last_date,
                 "escaped_window": s.containment.escaped,
                 "containment_verified": s.containment.verified,
                 "attempts": s.attempts, "error": s.error}
                for s in seg.segments],
        },
        "pacing": {d: {"rate_key": p.rate_key,
                       "min_interval_s": p.min_interval_s,
                       "max_concurrent": p.max_concurrent,
                       "window_limit": p.window_limit,
                       "basis": p.basis} for d, p in pacing.items()},
        "pacer_waits": seg.pacer_log,
        "warnings": seg.warnings(),
    }
    # ⭐ 速比只有在两侧都成功时才有意义；⛔ 失败的一侧"很快"不是提速。
    if ser.ok and seg.frame is not None and seg.wall_s > 0:
        out["speedup_x"] = round(serial_s / seg.wall_s, 3)
    else:
        out["speedup_x"] = None

    if not ser.ok or seg.frame is None:
        out["equivalence"] = {
            "equal": False,
            "reason": ("单次取数失败" if not ser.ok else "分段取数未产出重组帧")
                      + " ⇒ ⛔ 无法比对（这不是'不等价'，是'没测到'）",
        }
        return out

    eq = equivalence_report(ser.frame, seg.frame, sort_keys=sort_keys)
    if len(seg.sources_used) != 1 or (ser.used and seg.sources_used != (ser.used,)):
        eq.caveats.append(
            f"⚠ 本次对照的两侧**不是同一个源**：单次 used={ser.used}，"
            f"分段 sources_used={list(seg.sources_used)} ⇒ 它测的是"
            "**源之间**是否等价（FINDING-347 的判据），⛔ 不是分段机制是否等价。")
    out["equivalence"] = {
        "equal": eq.equal, "rows_serial": eq.rows_serial,
        "rows_segmented": eq.rows_segmented,
        "rows_only_in_serial": eq.rows_only_in_serial,
        "rows_only_in_segmented": eq.rows_only_in_segmented,
        "dtype_diffs": eq.dtype_diffs,
        "cols_only_in_serial": list(eq.cols_only_in_serial),
        "cols_only_in_segmented": list(eq.cols_only_in_segmented),
        "value_mismatches": eq.value_mismatches,
        "sort_keys": list(eq.sort_keys),
        "sort_keys_unique_in_serial": eq.sort_keys_unique_in_serial,
        "pandas_verdict": eq.pandas_verdict,
        "caveats": eq.caveats,
    }
    return out



# ── 会话覆盖率判决（`FINDING-781` 处置方向 ③） ─────────────────────────────
#
# `Containment` 刻意只报 `short_at_start_days` 而不判罪 —— 那是对的，日历日差非 0 可能
# 只是端点休市。缺的那一步是：拿**真实交易日历**把"少了几天"变成判决。
#
# ⛔ 为什么必须有这一步：`FINDING-781` 实测 `tdx::daily_bar` 对 `20260601..20260630`
#    回 **15** 行、`state=OK`，而该区间实测 **21** 个交易日 —— 少 6 天且**前段被削**。
#    0 行有 `EMPTY_OK` 兜住（`FINDING-178`），**15 行带 OK 没有任何东西兜**。
#    ⇒ 判据必须落在**覆盖率**上，与 `FINDING-740`/`741`/`742` 是同一条判据的第四个实例。
#
# ⚠ 本函数**不**猜日历：拿不到日历就如实返回 `verdict="UNKNOWN"`，⛔ 不退化成按
#    `days*5/7` 估（那正是 `FINDING-770` 记下的、我本轮犯过的错）。

#: 调用方未声明 `date_col` 时的解析优先序。**最细粒度优先**。
#:
#: ⛔ `FINDING-682` 的教训是反的方向：那次探针偏好 `trade_date` 而非 `trade_time`，
#:    于是把 5 分钟根里 204 万行**正常**日内数据报成「200 万重复」。⇒ 顺序必须细→粗。
#: ⚠ 这是**解析**而不是**推断口径**：只在实得帧的列名里挑一个已知的日期列，挑不到就
#:    返回 None 让判决保持 UNKNOWN。⛔ 不臆造列、不改帧。
_DATE_COL_PREFERENCE = ("trade_time", "datetime", "trade_date", "date")


def resolve_date_col(frame: Any, declared: str | None = None) -> str | None:
    """`declared` 优先；否则按 `_DATE_COL_PREFERENCE` 在实得列里挑。挑不到返 None。"""
    # ⚠ 不可写 `getattr(...) or []`：pandas Index 的真值判定会抛
    #   `ValueError: truth value of a Index is ambiguous`（本轮实测踩到）。
    raw = getattr(frame, "columns", None)
    cols = [] if raw is None else list(raw)
    if declared:
        return declared if declared in cols else None
    for cand in _DATE_COL_PREFERENCE:
        if cand in cols:
            return cand
    return None


_SESSION_SQL = (
    "SELECT COUNT(*) FROM trading_calendar "
    "WHERE date >= ? AND date <= ? AND is_trading_day = 1"
)


def expected_sessions(w: "Window", *, runtime_db: Any = None) -> int | None:
    """`w` 闭区间内的交易日数，取自运行期日历。拿不到返回 `None`。

    ⛔ 无回退估算。`FINDING-770`：`days*5/7` 是推算，写进表格后与实测无从分辨。
    """
    import sqlite3
    from pathlib import Path as _P
    db = _P(runtime_db) if runtime_db else (
        _P(__file__).resolve().parents[2] / "phase1" / "finai.db")
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        # ⚠ `Window.start/end` 是 `YYYYMMDD` 字符串，而 `trading_calendar.date` 是
        #   `YYYY-MM-DD` ⇒ 必须经 `_to_date` 归一，否则字符串比较恒不匹配（静默 0）。
        lo, hi = _to_date(w.start).isoformat(), _to_date(w.end).isoformat()
        row = conn.execute(_SESSION_SQL, (lo, hi)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return int(row[0]) if row and row[0] is not None else None


def session_coverage(frame: Any, w: "Window", date_col: str | None, *,
                     runtime_db: Any = None) -> dict[str, Any]:
    """实得 distinct 日期数 vs 区间应有交易日数。

    Returns a dict with ``verdict`` in:
      * ``COMPLETE``   — 实得 ≥ 应有
      * ``SHORT``      — 实得 < 应有 ⇒ ⛔ 调用方**不得**当成功（这是 `FINDING-781` 的形状）
      * ``UNKNOWN``    — 无日历或无日期列，⭐ 如实说没验（`FINDING-592`）
    """
    exp = expected_sessions(w, runtime_db=runtime_db)
    got = None
    if date_col and frame is not None and date_col in getattr(frame, "columns", []):
        got = int(frame[date_col].astype(str).str[:10].nunique())
    if exp is None or got is None:
        return {"verdict": "UNKNOWN", "expected_sessions": exp,
                "observed_sessions": got,
                "why": "no runtime calendar" if exp is None else "no date column"}
    return {
        "verdict": "COMPLETE" if got >= exp else "SHORT",
        "expected_sessions": exp,
        "observed_sessions": got,
        "missing_sessions": max(exp - got, 0),
        # ⚠ 只报事实，⛔ 不在此处抛 —— 抛不抛由调用方按用途决定（回补 vs 探针）。
        "note": ("实得少于区间应有交易日数 ⇒ 该腿对本窗口不可信，参见 FINDING-781"
                 if got < exp else ""),
    }
