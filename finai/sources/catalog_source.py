#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按**接口名**取数的统一入口 —— 把实测可用接口从"文档里的表"变成"能调的码"。

⭐ **口径先说清**（`FINDING-257/260`，两处都曾数错。数字随打点推进会变，
  ⛔ 别把它们当常量引用 —— 每条记录自带标记，按标记过滤才不会再数错）：
    usable()              ⇐ 与门禁 CHECK 10 **同口径**（在册计划内）    实测 2026-08-09: 730
    usable(in_plan=None)  ⇐ 多的 5 条是 `off_plan_records`，              实测: 735
                            产物明令"不得计入分子" ⇒ 默认排除
    callable_here=True    ⇐ 可 `fetch()` 直调；另 58 条按设计走专用适配器  实测: 672
                            （baostock 22 须子进程隔离；mootdx 20 + tdxpy 16 须会话初始化）
  ⇒ 用 `x["in_plan"]` / `x["callable_here"]` / `x["route"]` 判断，
    ⛔ 不要靠口头约定的数字（`FINDING-262` 正是手抄派生数字翻的车）。

⛔ **这个模块补的是一个真实缺口**，不是重复劳动。此前的状态是：
  · `docs/engineering/DATA_INTERFACE_MATRIX.md` 已把可用接口逐条列出（含行数+字段），
  · `docs/DATA_SOURCE_GUIDE.md` 已按类别汇总并点名 61 条，
  · **但没有任何代码能按名字调用它们** —— 实测 `grep` 全仓无
    `fetch_by_name` / `call_interface` 之类入口，也没有任何 `.py` 读
    `auto_probe_results.json` 去取数。
  ⇒ 要用某条接口，只能人肉查表 → 手写 import → 自己踩一遍参数坑。本模块终结这一步。

⭐ **参数来自产物，不是我现编的**：每条记录的 `kwargs` 字段是打点器**实测成功**
  的那组入参（含 `FINDING-218/233/239/240/241/244/246` 等一路修出来的定点覆盖）。
  故 `fetch(name)` 默认复用实测入参 —— ⛔ 这比让调用方自己猜参数安全得多。

⛔ **六态契约照旧**：0 行归 `EMPTY_OK` 而非成功（`FINDING-178`）。
  ⛔ `EMPTY_OK` 与各 `FAIL_*` **都不能**证明"这个接口没有这个能力"。

⚠ **A 股优先**：本项目当前只做 A 股。港股/美股/期货/期权类接口**未在 1,066 分母内**
  （`EXCLUDE_CATEGORIES`，按"仅登记存在性"处理），故它们**不在**本模块的可调集合里 ——
  它们的清单见 `finai/sources/overseas_registry.py`，等扩展阶段再启用。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from finai.sources.base import (
    OK_NO_COLS,
    FetchResult,
    classify_exception,
    install_brotli_decoder_swap,
    install_aia_chain_fix,
    install_eastmoney_pool_retry,
    install_pandas1_append_shim,
    install_required_headers,
    install_tushare_referer_fix,
    make_result,
    resolve_deferred_args,
)

SOURCE = "catalog"
_ROOT = Path(__file__).resolve().parents[2]
_RESULTS = _ROOT / "artifacts/interface_matrix/auto_probe_results.json"

#: ⛔ 这些库必须子进程隔离（`FINDING-181`：baostock 能挂死主进程，
#:   且 `socket.setdefaulttimeout` 约束不住它）。命中即拒绝直调并给出正确用法。
_NEEDS_ISOLATION = {"baostock"}

#: 会话初始化/主机选择类方法的点分前缀 —— 这些**按设计**不走本入口（`FINDING-247`）。
_SESSION_PREFIXES = ("hq", "exhq", "quotes", "reader")


def _route(lib: str, dotted: str) -> str | None:
    """这条接口若不可在本入口直调，返回该走哪个适配器；可直调则返回 None。

    ⛔ 这是 `fetch()` 拒绝规则的**唯一真源** —— `usable()` 的 `callable_here`
    与 `fetch()` 的 raise 必须同源，否则 `FINDING-260` 的口径分叉会立刻复发。
    """
    if lib in _ADAPTER_LIBS:
        # ⭐ citydata 由 `fetch()` **内部转发**（见下），故这里返回 None：
        #   它对调用方是"可直调"的。⛔ 不返回 route 字符串，否则 `usable()` 会把
        #   它标成 callable_here=False，而 `fetch()` 其实能取 ⇒ 又是一次口径分叉。
        return None
    if lib in _NEEDS_ISOLATION:
        return f"finai.sources.{lib}_source"
    if "." in dotted and dotted.split(".")[0] in _SESSION_PREFIXES:
        return "finai.sources.tdx_source（A股）/ tdx_ext_source（港股·期货·期权）"
    return None


def route_for(name: str) -> str | None:
    """这个 `lib::name` 若不可 `fetch()` 直调，返回该走哪个适配器；可直调返回 `None`。

    ⭐⭐ 为什么要有这个**公开**入口（`FINDING-452`）：
      `capability_router.leg_availability()` 判"这条腿此刻走不走得到"时，
      查了**包在不在**、查了**参数对不对**，却查不到**取数入口的拒绝规则** ——
      于是 `baostock::…` 这类腿被判 `LEG_OK`、把 `upper_bound` 抬高 1，
      而运行时 `fetch()` 在**发请求之前**就抛 `RuntimeError`。
      实测（`artifacts/_r42_probe_isolation_blindspot.py`）：
        `leg_availability -> [ok]` / `upper_bound=1` / `overstated=False`
        ——⛔ 连"高估"的警报都不响，因为 declared 与 upper_bound 一致地都错了。

    ⛔ 刻意**转发** `_route()` 而不是复制它的规则：`_route` 是拒绝规则的唯一真源，
      再抄一份就是 `FINDING-260` 那类口径分叉的第 N 次复发。
    ⚠ 名字不在台账里时返回 `None`（"没有路由约束"），⛔ 不抛 —— 判"能不能取到"
      是 `fetch()` 的 `KeyError` 的事，本函数只答"有没有路由级拒绝"，
      ⛔ 两个问题不许合并成一个返回值。
    """
    rec = _index().get(name) or _citydata_index().get(name)
    if rec is None:
        return None
    return _route(rec["lib"], rec["name"])


@lru_cache(maxsize=1)
def _index() -> dict[str, dict]:
    """接口名 → 实测记录。键同时收 `lib::name` 与裸 `name`（后者唯一时）。"""
    data = json.loads(_RESULTS.read_text(encoding="utf-8"))
    idx: dict[str, dict] = {}
    bare: dict[str, list[dict]] = {}
    for r in data["results"]:
        key = f"{r['lib']}::{r['name']}"
        idx[key] = r
        bare.setdefault(r["name"], []).append(r)
    for name, rs in bare.items():
        if len(rs) == 1 and name not in idx:
            idx[name] = rs[0]
    return idx


#: citydata 商家镜像的实测产物（`FINDING-243` 两轮 + `FINDING-341` 更正）。
#: ⛔ **刻意与 `_RESULTS` 分开存**，因为它**不在 1,066 条在册计划内**：
#:   混进 `auto_probe_results.json` 就等于**动 CHECK 10 的冻结分母**，
#:   而那会用"扩分母"稀释掉 1,066 里尚未解决的 206 条缺口 —— goal 明令禁止。
#: ⭐ 但**能力必须可取**（`FINDING-339` 的教训：114 条能用的接口不在母库里 ⇒
#:   按名取不到、连我自己都找不到）。故走**独立命名空间** `citydata::`。
_CITYDATA_RESULTS = _ROOT / "artifacts/_r37_citydata_merged.json"

#: 这些库走各自的专用适配器，不在本入口直调。
_ADAPTER_LIBS = {"citydata": "finai.sources.citydata_source"}


@lru_cache(maxsize=1)
def _citydata_index() -> dict[str, dict]:
    """`citydata::<api>` → 实测记录。⛔ 与 `_index()` 分开，不污染 CHECK 10 口径。

    ⚠ 记录来自 `FINDING-243` 两轮探测的产物（2026-08-08 实测，113 条 `verdict=OK`
      且 `rows>0` 且有字段）。⚠ 这是"打点时可用"，不是"此刻可用"。
    ⭐ `FINDING-341` 的教训：能力类结论**必须带测量日期**，引用前先复测。
    """
    if not _CITYDATA_RESULTS.exists():
        return {}
    data = json.loads(_CITYDATA_RESULTS.read_text(encoding="utf-8"))
    idx: dict[str, dict] = {}
    for r in data.get("merged") or ():
        api = r.get("api")
        if not api or r.get("verdict") != "OK":
            continue
        if not ((r.get("rows") or 0) > 0 and (r.get("n_fields") or 0) > 0):
            continue
        idx[f"citydata::{api}"] = {
            "lib": "citydata", "name": api,
            "category": r.get("category") or "citydata/tushare镜像",
            "rows": r.get("rows"), "cols": r.get("fields") or [],
            "kwargs": r.get("params") or {},
            # ⭐ `FINDING-339`（2026-08-12）：`state` 不是凭空补的，是把**上面那道过滤**
            #   已经证明的事写下来 —— 循环开头 `verdict != "OK"` 就 `continue` 了，
            #   故能到这里的 113 条**按构造**全是 `OK`（产物里另有 `EMPTY`/`HTTP_404`
            #   两种 verdict，它们进不来）。
            #   ⛔ 不写则 `describe()` 对这 113 条返回 `state=None`，读起来是"状态未知"，
            #     而实测状态是**已知为 OK** ⇒ 那是把已有的信息丢掉，
            #     与 `FINDING-178`「空/未知冒充结论」同族。
            "state": "OK",
        }
    return idx


@lru_cache(maxsize=1)
def _off_plan() -> frozenset[str]:
    """产物自己声明的**计划外** key（`off_plan_records`）。

    ⛔ `FINDING-257`：产物原话是「保留其证据，但**不得计入「计划覆盖率」的分子**」。
      门禁 `check_10_capability_surface` 按 `plan_set` 过滤 —— 本模块此前**不过滤**
      ⇒ 比门禁多 5 条，同一个"可用"在两处指不同集合。
    ⚠ 这 5 条（胡润/福布斯榜、百度迁移规模、油品明细、QDII 溢价）**并未**被判不可用，
      它们是计划外，不是失败 —— 故此处**标注**而非删除。
    """
    data = json.loads(_RESULTS.read_text(encoding="utf-8"))
    return frozenset(data.get("off_plan_records") or ())


def usable(category: str | None = None, lib: str | None = None,
           in_plan: bool | None = True) -> list[dict]:
    """列出**实测可用**（cols 非空且 rows>0）的接口。

    Args:
        category: 按权威类别过滤
        lib: 按库过滤
        in_plan: `True`（默认）只要在册计划内的 ⇒ **与门禁 CHECK 10 同口径**；
            `False` 只要计划外的；`None` 两者都要（⛔ 此时**不可**用来对账门禁）。

    ⚠ 这是"打点时可用"，不是"此刻可用" —— 网络类结论会随时间/出口 IP 变化。
    ⚠ `callable_here=False` 的条目**不能**用 `fetch()` 直调（按设计，见 `route`）。
    """
    off = _off_plan()
    out = []
    # ⛔⛔ `FINDING-344`：citydata **不进 `usable()` 的任何分支**（含 `in_plan=None`）。
    #   我第一版把它塞进 `in_plan is not True`，实测弄红三条 identity 守卫 ——
    #   守卫是对的：`in_plan` 是个**二值**口径，其不变式为
    #       usable(None) == usable(True) | usable(False)
    #   而 `usable(False)` 的定义是"产物自己声明的 `off_plan_records`"。
    #   citydata 既不在计划内、也不在 `off_plan_records` 里 ⇒ 它是**第三类**，
    #   硬塞进来就破坏了那个不变式，也让 `in_plan=False` 不再等于"计划外"。
    #   ⭐ 故商家源走**独立函数** `usable_extra()`，⛔ 不污染这个二值口径。
    pool = dict(_index())
    for key, r in pool.items():
        if "::" not in key:
            continue
        if not (r.get("cols") and (r.get("rows") or 0) > 0):
            continue
        if category and r.get("category") != category:
            continue
        if lib and r["lib"] != lib:
            continue
        rec_in_plan = key not in off
        if in_plan is not None and rec_in_plan is not in_plan:
            continue
        route = _route(r["lib"], r["name"])
        out.append({"key": key, "lib": r["lib"], "name": r["name"],
                    "category": r.get("category"), "rows": r.get("rows"),
                    "cols": r.get("cols"), "kwargs": r.get("kwargs") or {},
                    "in_plan": rec_in_plan,
                    "callable_here": route is None, "route": route})
    return sorted(out, key=lambda x: -(x["rows"] or 0))


def usable_extra(category: str | None = None) -> list[dict]:
    """列出**计划外的商家源**（当前只有 citydata）里实测可用的接口。

    ⭐ **为什么单独一个函数而不是 `usable()` 的一个分支**（`FINDING-344`）：
    `in_plan` 是**二值**口径，不变式 `usable(None) == usable(True) | usable(False)`
    由 `tests/test_catalog_source_identity.py` 三条守卫钉着，而 `usable(False)`
    的定义是产物自己声明的 `off_plan_records`。citydata 是**第三类**
    （既不在计划内、也不在 `off_plan_records`），塞进那个二值口径会破坏不变式。

    ⇒ 三个数各有其义，⛔ 不可混：
        `usable()`        = **860** ⇐ 与门禁 CHECK 10 严格同口径（⛔ 不因收编而变）
        `usable_extra()`  = **113** ⇐ 商家源，运行时可按名取
        两者之和           = 973    ⇐ "能按名取到"的能力总数（⛔ **不是** CHECK 10 分子）

    ⚠ 这是"打点时可用"（`FINDING-243`，2026-08-08 实测），不是"此刻可用"。
      ⭐ `FINDING-341` 的教训：能力类结论必须带日期，引用前先复测。
    """
    out = []
    for key, r in _citydata_index().items():
        if category and r.get("category") != category:
            continue
        out.append({"key": key, "lib": r["lib"], "name": r["name"],
                    "category": r.get("category"), "rows": r.get("rows"),
                    "cols": r.get("cols"), "kwargs": r.get("kwargs") or {},
                    "in_plan": False, "plan_status": "vendor_extra",
                    "callable_here": True, "route": None})
    return sorted(out, key=lambda x: -(x["rows"] or 0))


def describe(name: str) -> dict:
    """查一条接口的实测档案（状态/行数/字段/实测入参）。⛔ 不发网络请求。

    ⛔⛔ `FINDING-339` 残留（2026-08-12 实测并修）：本函数此前只查 `_index()`，
      而 `fetch()` 查的是 `_index()` **或** `_citydata_index()`
      ⇒ 商家源那 **113** 条接口**取得到、查不到**：
          `fetch("citydata::stk_limit", trade_date=…)` → 7,733 行
          `describe("citydata::stk_limit")`            → `KeyError: 不在实测台账里`
      ⚠ 而那句报错还把人指向 `overseas_registry`（港股/美股/期货/期权），
        对一条**在册的 A 股商家接口**是**指错方向**的诊断。
      ⭐ 这正是 `FINDING-339` 本体的复发：该条登记的缺陷**不在数据源，在可发现性** ——
        上一轮就是因为按名找不到 citydata，才把已登记两天的源当新发现重测一遍、
        还差点覆盖掉已有的 6,071 字节实现。`describe()` 是"按名查档"的入口，
        它对 113 条报"不在台账里"，就是同一个缺口换个位置继续存在。

    ⛔ 这**不是** `FINDING-344` 的反面。344 拒绝的是把 citydata 塞进 `usable()`：
      那里 `in_plan` 是**二值**口径，不变式
      `usable(None) == usable(True) | usable(False)` 由
      `tests/test_catalog_source_identity.py` 三条守卫钉着，第三类挤进去会破坏它。
      `describe()` **不划分任何集合、不参与 CHECK 10 的分子**，只是"给名字、还档案"
      ⇒ 收编它不触碰那个不变式（`test_usable_default_matches_gate_numerator` 仍钉着分子）。
      ⚠ 代价面已同时钉住：商家记录必须 `in_plan=False`
      （`test_describe_marks_vendor_records_as_out_of_plan`），⛔ 不得冒充在册能力。
    """
    r = _index().get(name)
    vendor = False
    if r is None:
        # ⛔ 与 `fetch()` 的查表**同源**，⛔ 不许在此另写一套解析 ——
        #   两个姐妹函数认得的名字集合一旦分叉，就是本条缺陷本身。
        r = _citydata_index().get(name)
        vendor = r is not None
    if r is None:
        raise KeyError(f"接口 {name!r} 不在实测台账里。"
                       f"⚠ 若它属港股/美股/期货/期权，见 overseas_registry（未实测，仅登记）。")
    if vendor:
        # 商家源走自己的适配器（`FINDING-342`），但对调用方是同一个 `fetch()` 签名
        # ⇒ `callable_here=True` / `route=None`，与 `usable_extra()` 保持一致。
        return {"lib": r["lib"], "name": r["name"], "category": r.get("category"),
                "state": r.get("state"), "rows": r.get("rows"), "cols": r.get("cols"),
                "kwargs_measured": r.get("kwargs") or {}, "error": r.get("error"),
                "in_plan": False, "plan_status": "vendor_extra",
                "callable_here": True, "route": None}
    route = _route(r["lib"], r["name"])
    return {"lib": r["lib"], "name": r["name"], "category": r.get("category"),
            "state": r.get("state"), "rows": r.get("rows"), "cols": r.get("cols"),
            "kwargs_measured": r.get("kwargs") or {}, "error": r.get("error"),
            "in_plan": f"{r['lib']}::{r['name']}" not in _off_plan(),
            "callable_here": route is None, "route": route}


def _resolve(lib: str, dotted: str) -> Any:
    """走点分路径取到可调用对象。⛔ 复用打点器的语义，不造第二套。"""
    import importlib

    obj: Any = importlib.import_module(lib)
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def fetch(name: str, /, **overrides: Any) -> FetchResult:
    """按接口名取数。默认复用**实测成功**的入参，`overrides` 可覆盖。

    Args:
        name: `lib::name` 或裸接口名（唯一时）
        **overrides: 覆盖实测入参（如换 `symbol` / 换日期区间）

    ⛔ 需要会话初始化的接口（tdxpy 的 connect、baostock 的 login）**不走本入口** ——
      它们请用 `tdx_source` / `tdx_ext_source` / `baostock_source`，
      那三个模块把连接与主机选择的坑都封好了（`FINDING-247` 是最新一例）。
    """
    rec = _index().get(name) or _citydata_index().get(name)
    if rec is None:
        raise KeyError(f"接口 {name!r} 不在实测台账里")
    lib, dotted = rec["lib"], rec["name"]

    # ⭐ `FINDING-342`：citydata 走它自己的适配器（REST-per-API + 强制代理），
    #   但对调用方保持**同一个 `fetch()` 签名与同一套 `FetchResult` 六态**。
    #   ⛔ 不在这里重实现 HTTP —— `citydata_source` 已封好"打根路径 100% 404"
    #     与"缺代理即报错、绝不静默直连（会封号）"两个坑。
    if lib == "citydata":
        from finai.sources import citydata_source

        # ⛔⛔ `FINDING-345`：**不能**把实测 kwargs 与 overrides 简单合并。
        #   tushare 系接口的过滤参数是**互斥的两族**：
        #     ① 单标的 + 区间：`ts_code` + `start_date`/`end_date`
        #     ② 全市场单日：`trade_date`
        #   实测缺陷：`stk_limit` 的存档 kwargs 是 ①（`ts_code=600000.SH` +
        #   `20220101..20220429`），调用方传 `trade_date=20260807` 后被**合并**成
        #   "600000.SH 在 2022 年区间内且交易日=2026-08-07" ⇒ 服务端合法地返 **0 行**。
        #   而直打适配器 `fetch("stk_limit", trade_date=…)` 实测 **7,733 行**。
        #   ⇒ 症状是"母库说这条能用、按名取却 0 行"，即 `FINDING-264` 那类口径分叉，
        #     且**会被误读成上游没数据**（我自己就差点把它记成"瞬时抖动"）。
        #   ⭐ 修法：调用方一旦给出 ② 族的键，就**丢掉** ① 族的存档值（反之亦然）。
        _RANGE_KEYS = ("start_date", "end_date")
        kwargs = dict(rec.get("kwargs") or {})
        if "trade_date" in overrides:
            for k in (*_RANGE_KEYS, "ts_code"):
                kwargs.pop(k, None)
        elif any(k in overrides for k in _RANGE_KEYS):
            kwargs.pop("trade_date", None)
        kwargs.update(overrides)
        return citydata_source.fetch(dotted, **kwargs)

    route = _route(lib, dotted)
    if route is not None:
        # ⛔ 拒绝规则与 `usable()['callable_here']` 同源（`_route`），故两者不可能分叉。
        why = ("能挂死主进程，且 socket.setdefaulttimeout 约束不住（FINDING-181）"
               if lib in _NEEDS_ISOLATION
               else "需要会话初始化/主机选择（FINDING-247：探针连的主机早已退役且不拒连）")
        raise RuntimeError(f"{name} 不走本通用入口 —— {why}。请用 {route}。")

    kwargs = dict(rec.get("kwargs") or {})
    kwargs.update(overrides)
    # ⭐ `FINDING-323`：产物里存的是 `@finai:` 哨兵（⛔ 凭证不写进 git 跟踪的产物、
    #    且它**会过期**），此处解析成**当次现取**的真值。
    #    ⛔ 与 `auto_probe_interfaces._worker()` **同源**解析 —— 只在探针侧解析
    #    就是 `FINDING-264`/`-319` 那个"产物声称能取、生产取不到"的形状第四次复发。
    kwargs = resolve_deferred_args(kwargs)

    # ⭐ `FINDING-264`：与 `auto_probe_interfaces._worker()` **同源**装载。
    #    探针装了而这里不装 ⇒ 产物声称 7 条基本面接口可用、本入口却仍抛
    #    `OSError: 获取失败，请检查网络.`，即口径分叉（`FINDING-260` 的形状）。
    install_pandas1_append_shim()
    # ⭐ `FINDING-311`：同上，brotli 解码器换实现也必须与探针同源装载 ——
    #    否则产物声称高压缩比接口可用、本入口却仍抛 `ContentDecodingError`。
    install_brotli_decoder_swap()
    # ⭐ `FINDING-319`：东财 push2 面的**换池垫片**同理，且这一条是**实测漏装**的
    #    第三例（`FINDING-264` 的形状第三次复发）。成对腿实测（2026-08-10）：
    #        不装 → `efinance::stock.get_realtime_quotes` = `FAIL_UNREACHABLE` / 0 行
    #               （`ConnectionError: push2.eastmoney.com:80 Max retries exceeded`）
    #        装上 → **`OK` / 5,892 行**
    #    而产物里这条声称 5,892 行 ⇒ 不装就是"产物说能取、生产取不到"的口径分叉。
    #    实测受影响的至少 7 条（原 `D_HOST_DENIED` 桶转为可用的那批）。
    install_eastmoney_pool_retry()
    # ⭐ `FINDING-327`：漏发中间证书的主机（申万 / chinascope）补链，**校验保持开启**。
    #    ⛔ 同源装载第五例：只在探针侧装 ⇒ 产物声称 5,003 行、生产入口报 `SSLError`。
    install_aia_chain_fix()
    # ⭐ `FINDING-329`：tushare 收下 `ref=` 却把 `add_header('Referer', ...)` 注释掉了。
    #    ⛔ 同源装载第六例：只在探针侧装 ⇒ 产物声称 3,809 行、本入口报"请检查网络"。
    #    ⚠ 只吞 `ImportError`（本机未装 tushare ⇒ 不该连带打死其他库的取数）。
    #      ⛔ 不吞其他异常：`FINDING-212` ② 的教训是"静默失败让『没装上』与『装上了』
    #      在产物里无法区分" —— 库内部结构变了必须炸出来。
    install_tushare_referer_fix()
    # ⭐ `FINDING-332`：逐主机必需请求头（东财基金档案的 Referer 闸门、
    #    交易商协会的 UA 闸门）。⛔ 同源装载第七例：只在探针侧装 ⇒ 产物声称
    #    40/68/40/50 行，本入口全部 404/403。
    install_required_headers()

    try:
        fn = _resolve(lib, dotted)
        value = fn(**kwargs)
        if isinstance(value, pd.DataFrame):
            frame = value
        elif isinstance(value, (list, tuple)) and value and isinstance(value[0], dict):
            frame = pd.DataFrame(list(value))
        elif value is None:
            frame = pd.DataFrame()
        else:
            # 非表格返回（标量/字符串/dict）—— 有值但无字段名可记，
            # ⛔ 按 `FINDING-238` 不得伪造 cols。该态已在 base 注册（`FINDING-258`）。
            return FetchResult(state=OK_NO_COLS, frame=None, rows=1, source=SOURCE,
                              detail=f"非表格返回 {type(value).__name__}",
                              evidence={"key": f"{lib}::{dotted}", "kwargs": kwargs,
                                        "repr": str(value)[:200]})
    except Exception as exc:  # noqa: BLE001
        return FetchResult(state=classify_exception(exc), frame=None, rows=0,
                           source=SOURCE, detail=f"{type(exc).__name__}: {exc}"[:300],
                           evidence={"key": f"{lib}::{dotted}", "kwargs": kwargs})

    return make_result(frame, source=SOURCE,
                       evidence={"key": f"{lib}::{dotted}", "kwargs": kwargs,
                                 "rows_measured_at_probe": rec.get("rows")})
