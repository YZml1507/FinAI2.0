#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按**数据种类**取数 + 自动换源 —— 母库之上的编排层。

⭐ **它补的缺口**（用户 2026-08-10 提出，实测确认不存在）：
  母库 `catalog_source` 能按**接口名**取数，但**一个名字对一个实现** ——
  那条挂了就是挂了。实测过两次活证据：
    · `adata::stock.market.all_capital_flow_east` 同参数几分钟内 `OK/1032` → `FAIL_UNREACHABLE`
    · `citydata::stk_factor_pro` 同参数先 `EMPTY_OK/0` 后 `OK/81`
  ⇒ **单点取数在这个数据面上是不可靠的**，而项目要的是"要什么数据都能立刻拉到"。

⛔ **本层最重要的一条纪律：换源不得静默改口径。**
  同一种数据在不同源上字段名/单位/复权方式常常不同。
  ⇒ `get()` 只在候选**声明了同一 `schema` 契约**时才降级，
    并把**实际用了哪个源**放进返回值 —— 调用方永远知道数据来自哪里。
  ⭐ 财务数据宁可缺不可错：宁可返回失败，也不返回口径不同的数。

⚠ **不做的事**（避免踩已登记的坑）：
  ⛔ 不做全局并发 —— `FINDING-338` 实测 `quotes.sina.cn` 按 IP 限流，
    无节奏并发会把源打死（456 封禁约 10 分钟）。分段并行须显式开启并带节流。
  ⛔ 不重写任何 URL / `filter=` / Host（会静默改查询语义）。
  ⛔ 不改 citydata 的代理地址（商家明示换代理即封号）。
"""
from __future__ import annotations

import difflib
import importlib.util
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import pandas as pd

from finai import security_ids as _security_ids
from finai.sources.adjustment_mode import AdjustmentMode
from finai.sources.base import FetchResult

#: 六态里算"拿到了数据"的状态。⛔ `EMPTY_OK` **不算** —— 0 行不是成功（`FINDING-178`），
#: 但也**不代表**这个源没有这个能力，故它只触发换源、不判定源失效。
_GOT_DATA = ("OK",)


@dataclass(frozen=True)
class Upstream:
    """一个候选**真正问谁要数据**，以及这个判断的证据出处。

    ⛔⛔ `FINDING-407` 的正解。为什么必须是**显式声明 + 留证**、⛔ 不能推导：
      · 包名前缀（`key.split("::")[0]`）已被 `FINDING-402` **源码实测证伪**：
        `akshare::stock_zh_a_hist` 与 `efinance::stock.get_quote_history`
        请求**逐字相同**的 `https://push2his.eastmoney.com/api/qt/stock/kline/get`
        ⇒ 两个包名、一个上游、一个故障域。
      · `scripts/auto_probe_interfaces.py::endpoint_family()` 同样不行：实测它把
        上面那条 akshare 腿标成 `fam=other`（它是为**限流**服务的名字后缀启发式，
        其 docstring 自己预言了这个错标）⇒ 换一个被证伪的代理而已。
      · 方向反过来也不成立：`akshare` **一个包**下实测同时存在 eastmoney /
        sse / szse / sina / tencent 五个上游 ⇒ 包名与上游是**多对多**。
    ⇒ 故这里的每个值都由人读**当前安装的库源码**得到，并把出处写进 `evidence`。

    Attributes:
        fault_domain: **谁挂掉这条腿就断**。⭐ 判独立性只看这一栏 ——
            它是"发布者/运营方"粒度，⛔ 不是主机名粒度：`push2his.eastmoney.com`
            与 `push2.eastmoney.com` 是两个主机、但**同一个** Eastmoney
            （封 IP / 改字段定义 / 整体故障会同时打掉两者）⇒ 同 `fault_domain`。
        endpoint: 实测请求的端点（URL 或"主机 + api 名"）。⭐ 留它是为了让后人
            能**原地复核**，⛔ 不参与独立性判定（同域不同端点仍是同一故障域）。
        evidence: 这个判断是从哪读出来的（源码文件::函数、或适配器位置）。
            ⛔ 空串不合法 —— 无证据的声明等于猜（`tests` 里有表级守卫钉住）。
        origin_verified: 该 `fault_domain` 是否就是**数据的原始出处**。
            ⚠ `False` 表示"我只能证明这条腿**问谁要**，⛔ 证不出这家自己
            又是从哪拿的" —— 商家镜像（citydata）就是这种情况：能实测它
            POST 到 `tushare.citydata.club`，⛔ 但那台机器背后是不是又转手了
            交易所/东财，从**安装的代码里读不出来**。
            ⭐ 刻意把这个不确定性**编码进类型**，⛔ 不写成一个自信的标签：
              两条 `origin_verified=False` 的腿"故障域不同"只在**镜像运营方**
              这一层成立；⚠ 若它们背后其实是同一家，独立性会比声明的更弱。
              ⇒ 这一栏让读者看得见这个上界，⛔ 而不是被一个漂亮标签骗过去。
    """
    fault_domain: str
    endpoint: str
    evidence: str
    origin_verified: bool = True


def independent_fault_domains(cands: Iterable["Candidate"]) -> int:
    """这组候选**实际横跨几个故障域**。⭐ "有没有真备胎"的唯一判据。

    ⛔ 未声明 `upstream` 的候选**全部折叠到同一个哨兵域**，⚠ 刻意如此：
      漏声明时势集**不会**因此变大 ⇒ 守卫 fail-closed（宁可误红，不可误绿）。
      ⛔ 反过来（给每条漏声明的腿算一个独立域）会让"忘了填"自动变成"有备胎"。
    """
    return len({(c.upstream.fault_domain if c.upstream is not None else "")
                or "?undeclared" for c in cands})


def independence_basis(cands: Iterable["Candidate"]) -> str:
    """这组候选的"有备胎"是**建立在什么之上**：`verified` / `mirror-only` / `single`。

    ⛔⛔ `FINDING-407-NEW-1` 的正解。`independent_fault_domains()` 只回答"几个域"，
    而实测（2026-08-11）5 个达到 2 个域的能力，**第 2 条腿全是同一条**
    citydata 镜像（全仓 13 处 `origin_verified=False` 的 `fault_domain`
    无一例外是 `citydata-merchant-mirror`）⇒ 只数可证实来源的腿，10 个能力**全为 1**。
    ⇒ 一个只回答"2"的函数，会让"靠一条未验证镜像凑够 2"与"两个可证实来源"
    在读者眼里**完全一样**。这一栏就是把那个区别说出来。

    ⚠ `mirror-only` **不**等于"独立性是假的"：镜像与 `sina`/`sse`/`eastmoney`
    在**可用性**层面确实是不同运营方（不同主机、不同公司），这点没被证伪。
    未被证实的是**来源**层面 —— 镜像背后是否又转手了同一家。
    ⭐ 另有一个**聚合**含义，逐对读"2"时看不见：那 5 个能力的第 2 条腿是**同一条**，
      故单次商家账号封禁（`FINDING-339` 记录的现实风险）会让它们**同时**掉回单点。

    Returns:
        - ``"single"``：`independent_fault_domains() < 2` ⇒ 锁下就是单点。
        - ``"verified"``：**仅**用 `origin_verified=True` 的腿就已跨 ≥2 个域。
        - ``"mirror-only"``：够 2 个域，但**必须**算上来源未证实的腿才够。
    """
    cands = list(cands)
    if independent_fault_domains(cands) < 2:
        return "single"
    verified = [c for c in cands
                if getattr(c, "upstream", None) is not None
                and c.upstream.origin_verified]
    # ⛔ 这里**不能**复用 independent_fault_domains(verified)：它对空/漏声明的输入
    #    会返回 1（哨兵域），于是"零条可证实的腿"会读成"1 个域"。语义上没错，
    #    但下面只关心"≥2"，故直接数真实域名，避免哨兵混进可证实集合。
    return "verified" if len({c.upstream.fault_domain for c in verified}) >= 2 \
        else "mirror-only"


#: 一条腿**此刻**能不能被 `get()` 走到，以及判不出来时为什么判不出来。
#: ⭐ 刻意是四个取值而不是 `bool`：`FINDING-407-NEW-1` 的教训是"一个只答 2 的函数
#:   会让两种完全不同的处境读起来一样"，`bool` 在这里会犯同一个错 ——
#:   "证明走不到"与"无法证明走得到"必须分开，前者是结论，后者是**上界**。
LEG_OK = "ok"                                # 参数对得上签名、包在本机
LEG_BLOCKED_PARAMS = "blocked_params"        # ⛔ 已证明走不到：参数兑现不了 ⇒ 必然跳过
LEG_BLOCKED_PACKAGE = "blocked_package"      # ⛔ 已证明走不到：包不在本机 ⇒ 必然 ImportError
#: ⛔⛔ `FINDING-452`：**取数入口的路由规则**判它不可直调（如 `_NEEDS_ISOLATION`
#:   里的 baostock：能挂死主进程 ⇒ 必须子进程隔离）⇒ `fetch()` 在**发请求之前**
#:   就抛 `RuntimeError`。⭐ 这一态**必须与前两态分开**：前两态的修法是装包/改参名，
#:   这一态的修法是**走适配器**（`route` 字符串就是该走哪个），混成一个状态
#:   会让读者拿错工具箱。
LEG_BLOCKED_ROUTE = "blocked_route"          # ⛔ 已证明走不到：fetch() 结构性拒绝直调
LEG_UNKNOWN = "unknown"                      # ⚠ 判不出来（无签名可查）⇒ 只能算进上界

#: ⭐ "已证明走不到"的状态集合 —— `availability()` 靠它扣减 `upper_bound`。
#: ⛔⛔ 新增任何 `LEG_BLOCKED_*` 态**必须**同时加进这里，否则那一态只会
#:   出现在 `legs` 的展示里，而 `upper_bound` 照旧为一条必失败的腿 +1
#:   ⇒ 等于加了个不干活的状态（`FINDING-389` 的教训：把功劳记在不干活的字段上）。
#: ⛔ 刻意收成常量而不是在三处散写字面量：`FINDING-452` 就是"判据没同步"造出来的。
_BLOCKED_STATES = frozenset({LEG_BLOCKED_PARAMS, LEG_BLOCKED_PACKAGE,
                             LEG_BLOCKED_ROUTE})


def leg_availability(c: "Candidate", params: dict[str, Any]) -> tuple[str, str]:
    """这条腿在**这组统一参数**下的可达性 → `(状态, 人话原因)`。⛔ 不联网。

    ⛔⛔ 为什么"包在不在"必须**单独**判，⛔ 不能靠 `_unhonored()` 兜：
      `_honored_params()` 在 `ImportError` 时返回 `None`（`:264`），而 `_unhonored()`
      对 `None` 返回 `[]` ⇒ ⭐ **包不在的腿会报"零个参数被吞"，读起来完全像可达**。
      实测（`FINDING-444`）：`_unhonored(Candidate("nosuchlib::foo", …), {"symbol":"x"})
      == []`。⇒ 缺包与"参数完全对得上"在这一层**字节级同形**。

    ⚠ `find_spec()` 只证"能找到"，⛔ 不证"import 得动"（版本不兼容 / C 扩展缺失
      仍会 `find_spec` 成功而 import 失败）⇒ 故本函数给的是**上界**，见
      `availability()` 的返回类型。这一点 `FINDING-435` 已明确写过，⛔ 不要在这里
      悄悄把它当成充分判据。

    ⛔⛔ 判据**有顺序**，且顺序本身是被实测教会的（`FINDING-452`）：
      包在不在 → **路由准不准** → 参数对不对。我写那条的探针时给探针传了一个
      签名里没有的参数，判据在 `blocked_params` 就停住了，**根本没测到**路由那道闸
      ⇒ 打出 `upper_bound=0` 的假阴性。⭐ 要暴露后一道闸，必须先让前一道闸放行。
    """
    lib = c.key.partition("::")[0]
    # citydata 是 REST-per-API：参数由商家侧解释，本地既无签名也无包可查
    # ⇒ ⚠ 判不出来就**说判不出来**，⛔ 不猜"可用"也不猜"不可用"。
    if lib != "citydata":
        try:
            present = importlib.util.find_spec(lib) is not None
        except (ImportError, ValueError):
            present = False
        if not present:
            return LEG_BLOCKED_PACKAGE, f"包 {lib!r} 不在本机 ⇒ 取数时必 ImportError"
    # ⛔⛔ `FINDING-452`：问**取数入口自己**这条腿可不可直调 —— ⛔ 不在这里
    #   重写一份 `_NEEDS_ISOLATION`/`_SESSION_PREFIXES` 规则：`route_for()` 转发的
    #   `_route()` 是拒绝规则的唯一真源，抄一份就是 `FINDING-260` 的口径分叉复发。
    # ⚠ 本判据放在**参数判据之前**：路由拒绝是结构性的（改参名救不了），
    #   而参数不匹配是可修的 ⇒ 先报不可修的那个，读者才不会去修错地方。
    try:
        from finai.sources import catalog_source as _cs

        route = _cs.route_for(c.key)
    except Exception as exc:  # noqa: BLE001
        # ⛔⛔ 判据本身不可用时**必须**降到 `unknown`，⛔ 不许 `route = None` 往下走 ——
        #   那会让"问不出路由规则"**读起来完全等于**"没有路由约束"，于是这条腿可能
        #   一路走到 `LEG_OK`、把 `upper_bound` 抬高 1。⚠ 我第一版就是那么写的，
        #   而同一段注释写着"不许冒充可达" ⇒ ⭐ 注释与代码分叉，正是本条要修的病
        #   （`FINDING-444`「缺包与参数全中同形」的同族：两种处境压成一个读法）。
        return (LEG_UNKNOWN,
                f"路由判据不可用（{type(exc).__name__}）⇒ ⛔ 无法证明走得到，只计入上界")
    if route is not None:
        return (LEG_BLOCKED_ROUTE,
                f"`fetch()` 结构性拒绝直调（须走 {route}）⇒ 取数时必 RuntimeError")
    if (dropped := _unhonored(c, params)):
        return (LEG_BLOCKED_PARAMS,
                f"参数 {dropped} 映射后对不上签名 ⇒ `get()` 必跳过整条腿")
    if lib == "citydata" or _honored_params(c.key) is None:
        return LEG_UNKNOWN, "无本地签名可内省（服务端解释参数）⇒ 只能算进上界"
    return LEG_OK, "参数对得上签名，且包在本机"


@dataclass(frozen=True)
class Availability:
    """"此刻这个 `(能力, schema)` 有几个**能用**的故障域" —— `FINDING-435` 关闭条件①。

    ⛔⛔ 为什么返回结构体而不是一个 `int`：`declared` 与 `upper_bound` 的**差**才是
      本类型存在的理由。一个裸 `int` 会重演 `FINDING-407-NEW-1`（"只答 2 的函数
      让靠镜像凑数与两个可证实来源读起来一样"），那条的正解也正是**加一栏说清楚**。

    Attributes:
        declared: `independent_fault_domains()` 说有几个 —— 即**声明**。
        upper_bound: 扣掉**已证明走不到**的腿之后还剩几个域。⚠ 是**上界**：
            `unknown` 的腿算在里面（判不出来不等于可用），且 `find_spec` 成功
            不等于 import 得动 ⇒ ⛔ **不要**把它当"保证有这么多"。
        blocked: `(key, 状态, 原因)` —— 已证明走不到的腿，逐条留证。
        overstated: `upper_bound < declared` ⇒ ⭐ 声明比可用**多**，
            这正是 `FINDING-435`/`FINDING-441`/`FINDING-444` 共同的伤害面。
    """
    declared: int
    upper_bound: int
    blocked: tuple[tuple[str, str, str], ...] = ()
    legs: tuple[tuple[str, str, str], ...] = ()

    @property
    def overstated(self) -> bool:
        """声明的域数是否**多于**可用上界。⭐ 判"以为有备胎"只看这一个属性。"""
        return self.upper_bound < self.declared

    def warn_if_overstated(self) -> str | None:
        """高估时返回**可直接记日志**的人话；未高估返回 `None`。

        ⭐ 与 `RouteResult.warn_if_partial()` 同形：⛔ 刻意不 raise ——
          高估本身不产生错数据，但它**不该静默**（`FINDING-361` 的纪律）。
        """
        if not self.overstated:
            return None
        detail = "；".join(f"{k}：{why}" for k, _, why in self.blocked)
        return (f"⚠ 声明 {self.declared} 个独立故障域，但此刻只有至多 "
                f"{self.upper_bound} 个走得到。已证明走不到的腿：{detail}。"
                "⛔ 勿按声明值假定有备胎（FINDING-435 / FINDING-444）。")


def availability(cands: Iterable["Candidate"],
                 params: dict[str, Any] | None = None) -> Availability:
    """这组同 schema 候选**此刻**的可用故障域上界。⛔ 不联网、⛔ 不执行取数。

    ⭐ 为什么与 `independent_fault_domains()` **并存**而不是改它：
      后者的契约是"数**声明**"，全仓有守卫按这个语义钉住了具体数字
      （如 `daily_bar` 恒为 1）。⛔ 把可用性混进去会让那些断言在换一台机器
      时给出不同答案 —— 一个随环境漂移的"声明"就不再是声明了。
      ⇒ 两个函数各答一个问题，`Availability.overstated` 负责把差别说出来。
    """
    cands = list(cands)
    params = params or {}
    legs = tuple((c.key, *leg_availability(c, params)) for c in cands)
    # ⛔ `FINDING-452`：新增的 `LEG_BLOCKED_ROUTE` **必须**进这个集合 ——
    #   加了状态却不加进"已证明走不到"，`upper_bound` 照旧为假备胎 +1，
    #   等于白加一态。⭐ 故这里刻意用一个**具名常量**收口，⛔ 不散写三处字面量。
    blocked = tuple(l for l in legs if l[1] in _BLOCKED_STATES)
    reachable = [c for c, l in zip(cands, legs) if l[1] not in _BLOCKED_STATES]
    # ⛔ 走不到一条腿时 `upper_bound` 必须是 0，⛔ 不是 1 ——
    #   `independent_fault_domains([])` 会返回 0（空集），这里刻意直接复用它，
    #   ⚠ 但**不**复用它的哨兵折叠：漏声明 upstream 的腿在那边折叠成 1 个哨兵域，
    #     在这里同样折叠 ⇒ 与 declared 的口径保持一致，差值才有意义。
    return Availability(
        declared=independent_fault_domains(cands),
        upper_bound=independent_fault_domains(reachable) if reachable else 0,
        blocked=blocked,
        legs=legs,
    )


def capability_availability(
        capability: str,
        params: dict[str, Any] | None = None) -> dict[str, Availability]:
    """一个能力**按 schema 分组**的可用域数 —— `FINDING-435` 的"两种口径不一致"。

    ⭐ 为什么必须按 schema 分组：`get()` 的口径锁只在**同 schema 内**降级
      （`:1231`）⇒ 把整个能力的腿池在一起数，会把"跨 schema 的腿"算成备胎，
      而它们**永远不会**互相接管。池化口径与 per-schema 口径的差就是这么来的。

    ⚠ `params` 省略时用 `capability_params(capability)` 造探针 ——
      即"按该能力**声明的**调用契约"判可达性。⛔ 不是"随便一组参数"。
    """
    cands = CAPABILITIES.get(capability)
    if not cands:
        raise KeyError(
            f"未登记的能力 {capability!r}。已登记：{sorted(CAPABILITIES)}")
    if params is None:
        params = {p: "x" for p in capability_params(capability)}
    groups: dict[str, list[Candidate]] = {}
    for c in cands:
        groups.setdefault(c.schema, []).append(c)
    return {s: availability(g, params) for s, g in groups.items()}


@dataclass(frozen=True)
class Candidate:
    """一种数据的一个候选实现。

    Attributes:
        key: 母库里的接口名（`lib::name`），交给 `catalog_source.fetch()`。
        schema: 口径契约名。⭐ **只有 schema 相同的候选之间才允许自动降级** ——
            这是"换源不得静默改口径"的执行点。
        arg_map: 把统一参数名映射到该接口的实参名（如 `code` → `ts_code`）。
            ⛔ **只改名字、不改值** —— 这是它的能力上限，也是 `FINDING-350` 的成因：
            我曾以为写上 `arg_map={"symbol": "symbol"}` 就能让裸码变成 `sz000001`，
            那是恒等映射，什么也没做。要改**值**请用 `value_map`。
        value_map: 统一参数值 → 该接口要的值。⭐ `FINDING-350` 的正解：
            `stock_zh_a_hist_tx` 要 `sz000001`，`stock_zh_a_hist` 要 `000001`，
            两者**同一个能力、同一个 schema**，差别只在值的写法。
        pinned: **写死**的实参，调用方传什么都不覆盖。⭐ `FINDING-353` 的正解：
            同一个 `ohlcv_daily` 契约必须钉住**复权档**——实测 `efinance` 默认
            `fqt=1`（前复权）而 `stock_zh_a_hist` 默认不复权，300750 差到 328 元。
            钉住后 10 个共有列 4797/4797 逐元素相等。
        required: 该接口**必须真正生效**的统一参数名。⛔ `FINDING-352` 的正解：
            `efinance.stock.get_quote_history` 带 `**kwargs`，传错参名**不报错**，
            实测把 28 天的请求静默变成 1991-04-03~2026-08-10 的 8,454 行。
            ⇒ 凡是调用方传了却无法映射到真实签名的参数，本层**拒绝调用**该候选。
        transform: 取到 frame 后的规范化钩子（重命名列/单位换算）。⛔ 不做数值修正。
        local_filter: 该候选**服务端做不到、但本层可在 frame 上等价完成**的过滤。
            统一参数名 → `(frame, value) -> frame`。⭐ `FINDING-359` 的正解：
            `akshare::tool_trade_date_hist_sina` 签名为空 ⇒ 传 `start_date` 会被
            `_unhonored()` 判为"不生效"而**跳过整个候选** ⇒ 实测"主源一挂、
            带日期的调用就没有备胎了"，而带日期才是回测的**常规**调用形态。
            ⛔ 但**不能**因此放宽 `FINDING-352` 的守门（那条守的是"参数被静默吞掉"）——
            正解是让候选**声明**它把这个参数放在客户端做，并由 `get()` **真的执行**。
            ⇒ 于是"调用方的参数一定被兑现"这个不变式仍然成立，只是兑现的**位置**不同。
            ⚠ 只有当该源返回的是**超集**（如全历史日历）时这才等价；
              ⛔ 对"服务端分页/截断"的源不成立，那种情况下本地过滤会丢数据。
        coverage: 该候选**覆盖面的声明**。空串 = 全量（与主源同覆盖面）；
            非空 = **实测确认的部分覆盖**（如 `"SH"` 表示只含沪市）。
            ⛔⛔ `FINDING-361` 的正解，也是我**自己在 `FINDING-360` 里写出的真缺陷**：
            覆盖面差异是本层**唯一无法用 `schema` 表达**的口径差 —— 列名一样、
            数值也全对（相关 1.000000），调用方从 frame 上**看不出少了半个市场**。
            实测症状：注入主源 `FAIL_UNREACHABLE` ⇒ `ok=True` / 1,994 行 /
            `used=akshare::stock_margin_detail_sse`，而主源同日是 **4,424** 行
            ⇒ **2,430 只（54.9%）标的静默消失**，`RouteResult` 上**无任何标记**。
            ⚠ 我当时以为"写进 `note` + 一条守卫钉住"就够了 —— ⛔ 不够：
              `note` 是给**读代码的人**看的，运行时的调用方读不到它。
              ⇒ ⭐ 声明必须在**运行时**可见，否则等于没声明。
        upstream: 该候选**真正问谁要数据** + 证据出处（见 `Upstream`）。
            ⭐ "这条能力有没有真备胎"由它的 `fault_domain` 势集判定
            （`independent_fault_domains()`），⛔ **不是**由包名前缀判定 ——
            那个代理已被 `FINDING-402` 源码实测证伪（`FINDING-407`）。
            ⚠ 默认 `None` 只为不破坏既有测试夹具的构造式；⛔ `CAPABILITIES`
              里的每一条**必须**填，由 `test_every_candidate_declares_an_
              evidence_backed_upstream` 表级钉住；且漏填会让势集折叠到
              哨兵域 ⇒ 备胎守卫**变红**而不是变绿（fail-closed）。
        note: 实测备注（深度下界、已知限制），会出现在失败报告里。
            ⛔⛔ `FINDING-388` ③：这句话曾经**是假的** —— `RouteResult` 上没有
            任何位置承载 note，`raise_if_failed()` 也只格式化 `(key,state,rows)`
            ⇒ 无论何种失败 note 都不可能出现。现由 `RouteResult.notes` 兑现，
            并被 `test_failure_report_carries_the_candidate_note` 钉住。
            ⚠ 只在**失败**结果上出现：成功时调用方拿到的是数据，
              把"已知限制"混进成功路径会被读成"本次数据有问题"。
        adjust: 该候选**钉死的复权口径**（`AdjustmentMode`，R4 §3.3）。
            ⭐ 这是口径的**符号化**表示 —— 与 `pinned` 里的字面量值
            （`{"adjust": ""}` / `{"fqt": 0}`）**同源**：pinned 是给被调函数的实参，
            `adjust` 是"这条腿是什么口径"的机器可读声明，两者由 `to_kwargs()` 同源
            断言一致（⛔ 不许某条改了一个忘了另一个）。默认 ``None`` 只为不破坏
            既有测试夹具的构造式；`ohlcv_daily` 口径的 `daily_bar` 候选**必须**填。
    """
    key: str
    schema: str
    arg_map: dict[str, str] = field(default_factory=dict)
    value_map: dict[str, Callable[[Any], Any]] = field(default_factory=dict)
    pinned: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    transform: Callable[[pd.DataFrame], pd.DataFrame] | None = None
    local_filter: dict[str, Callable[[pd.DataFrame, Any], pd.DataFrame]] = field(
        default_factory=dict)
    coverage: str = ""
    upstream: Upstream | None = None
    note: str = ""
    #: ⭐ R4 §3.3：钉死的复权口径（符号化，与 `pinned` 同源）。默认 ``None`` 见上。
    adjust: "AdjustmentMode | None" = None


def _prefix_symbol(code: Any) -> str:
    """裸股票代码 → 带交易所前缀（`000001` → `sz000001`）。

    ⭐ `FINDING-350`：`akshare::stock_zh_a_hist_tx` 只认带前缀的写法，
      裸码会在它内部炸成 `ValueError: invalid literal for int() with base 10: 'i'`
      （因为它把返回的错误页当数据解析）。
    ⚠ 已带前缀的原样返回 ⇒ 幂等，调用方两种写法都能用。

    ⛔ `FINDING-355`：这里**不得**自己写 `startswith("9") -> sh` 这类前缀判交易所
      的规则。我第一版就是那么写的，而 `9` 同时覆盖沪 B（900xxx）与北交所
      （920xxx），于是 920xxx 被判成上海 —— 这正是
      `FINDING-11/-12/-13/-20/-21/-22` 那一族缺陷（判错交易所 ⇒ 接口返回空
      ⇒ 每个调用方都读成"这天没数据"）。交易所归属只有
      `finai.security_ids` 一个来源，它对不可判定的段（如 `510300`）**拒绝猜**。
      被 `tests/test_security_id_single_source.py` 看死。
    """
    s = str(code).strip().lower()
    if s[:2] in _security_ids.MARKETS:
        return s
    # 判不出来（如 5xxxxx 基金：沪深都有这一段）就原样传下去：让接口自己报错，
    # 好过在这里猜一个交易所、拿回一张空表、被上层当成"无数据"。
    try:
        market = _security_ids.market_for_security_code(s)
    except _security_ids.SecurityCodeError:
        return s
    return f"{market}{_security_ids.bare_security_code(s)}"


def _plain_symbol(code: Any) -> str:
    """带前缀 → 裸码（`sz000001` → `000001`）。⭐ 与 `_prefix_symbol` 互为逆。"""
    return _security_ids.bare_security_code(str(code).strip().lower())


def _norm_cal_date(v: Any) -> str:
    """任意日期写法 → `YYYYMMDD` 字符串。⛔ 只去分隔符，**不做时区/偏移换算**。

    ⭐ `FINDING-358` 的等价性实测要求：两源必须在**同一个可比表示**上比较。
      `citydata::trade_cal` 给 `'20240110'`（`str`），
      `akshare::tool_trade_date_hist_sina` 给 `datetime.date(1990, 12, 19)`。
    ⛔ 不用 `pd.to_datetime` —— 它会对 `'20240110'` 猜格式，且引入 tz-naive/aware 分歧；
      这里只要"把分隔符去掉"，是**无损**的字符串归一。
    """
    s = str(v).strip()
    if " " in s:
        s = s.split(" ", 1)[0]
    if "T" in s:
        s = s.split("T", 1)[0]
    return s.replace("-", "").replace("/", "")


def _honored_params(key: str) -> set[str] | None:
    """该接口**真正会读**的实参名；无法内省时返回 `None`。

    ⛔ `FINDING-352`：带 `**kwargs` 的接口对错误参名**不报错也不生效**。
      实测 `efinance.stock.get_quote_history(symbol=…, start_date=…)` 返
      `OK/8454`、区间 1991-04-03~2026-08-10 —— 调用方要的是 28 天。
      ⇒ `**kwargs` **不算**"支持这个参数"，故这里只收具名参数。
    """
    lib, _, dotted = key.partition("::")
    if lib == "citydata":
        return None          # REST-per-API：参数由商家侧解释，本地无签名可查
    try:
        mod = __import__(lib)
    except ImportError:
        return None
    obj: Any = mod
    for part in dotted.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return None
    return {
        name for name, p in sig.parameters.items()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                      inspect.Parameter.KEYWORD_ONLY)
    }


#: ⭐ 交易日历统一契约：**只有一列** `cal_date`（`YYYYMMDD` 字符串、升序、只含开市日）。
#: ⛔ 刻意**不保留** `exchange`/`pretrade_date`/`is_open` —— 那三列 akshare 侧没有，
#:   保留它们就等于声明一个只有一个源能满足的契约，`FINDING-351` 的形状（假等价）。
#: ⭐ 契约窄 = 两源都能真正满足 = 备胎是真的。要那三列请显式 schema="trade_cal_ts"。
_CAL_CONTRACT = ("cal_date",)


def _cal_ge(f: pd.DataFrame, v: Any) -> pd.DataFrame:
    """`cal_date >= start_date`。⭐ 归一到 `YYYYMMDD` 后**字符串比较即日期比较**
    （定长零填充 ⇒ 字典序与时间序一致），⛔ 故不需要 datetime 转换。"""
    return f[f["cal_date"] >= _norm_cal_date(v)]


def _cal_le(f: pd.DataFrame, v: Any) -> pd.DataFrame:
    """`cal_date <= end_date`（含右端点 —— 与服务端 `end_date` 语义一致）。"""
    return f[f["cal_date"] <= _norm_cal_date(v)]


def _cal_from_tushare(f: pd.DataFrame) -> pd.DataFrame:
    """tushare 口径日历 → 统一契约。⭐ **必须**按 `is_open` 过滤。

    ⛔⛔ 这是本转换里唯一有"改变数据含义"风险的一步：`citydata::trade_cal`
      的 `cal_date` 列**同时含休市日**（`is_open=0`），而 akshare 那条只给**开市日**。
      不过滤就会把休市日当交易日交给调用方 —— 回测里等于凭空多出交易日。
    ⚠ 实测该接口带 `start_date/end_date` 时返回的 242 行**恰好全是** `is_open=1`，
      所以"忘了过滤"在那次抽样里**看不出来** ⇒ 这正是必须写不变量守卫而非样本守卫的理由
      （`LESSONS §31`）。故此处**无条件过滤**，⛔ 不依赖"实测那批刚好都开市"。
    """
    if "is_open" in f.columns:
        keep = f["is_open"].astype(str).str.strip().isin(("1", "1.0", "True", "true"))
        f = f[keep]
    col = "cal_date" if "cal_date" in f.columns else f.columns[0]
    out = pd.DataFrame({"cal_date": [_norm_cal_date(v) for v in f[col]]})
    return out.drop_duplicates().sort_values("cal_date", ignore_index=True)


def _cal_from_akshare(f: pd.DataFrame) -> pd.DataFrame:
    """新浪全历史日历 → 统一契约。⚠ 它**只给开市日**，故无 `is_open` 可过滤。

    ⛔ 它**不接受区间参数**（`FINDING-352` 实测签名为空）⇒ 一次返回 8,797 天全历史。
      ⭐ 切区间是**调用方**的事，本层不切：切了就等于替调用方猜它要哪一段，
        而 `get()` 拿到的 `params` 里也可能根本没有日期（那时切什么都是错的）。
    """
    col = "trade_date" if "trade_date" in f.columns else f.columns[0]
    out = pd.DataFrame({"cal_date": [_norm_cal_date(v) for v in f[col]]})
    return out.drop_duplicates().sort_values("cal_date", ignore_index=True)


#: 上交所披露列 → tushare `margin_detail` 契约列。⭐ 每一条都经数值反证
#: （`scripts/_r38_equiv_margin.py`：6/6 相关 1.000000、中位相对差 ≤0.0001%）。
#: ⛔⛔ `融券余量` → **`rqyl`**（数量）⛔ 不是 `rqye`（金额）——
#:   把它对到 `rqye` 实测中位相对差 **1388%**，那是元/股的量纲比不是噪声。
_SSE_MARGIN_MAP = {
    "信用交易日期": "trade_date",
    "标的证券代码": "ts_code",
    "融资余额": "rzye",        # 金额
    "融资买入额": "rzmre",      # 金额
    "融资偿还额": "rzche",      # 金额
    "融券余量": "rqyl",        # ⭐ 数量（⛔ 非 rqye）
    "融券卖出量": "rqmcl",      # 数量
    "融券偿还量": "rqchl",      # 数量
}


#: 深交所披露列 → tushare `margin_detail` 契约列（`FINDING-361`）。
#: ⭐ 深市这张表**比沪市那张多两列**（`融券余额`=金额 / `融资融券余额`=金额）
#:   ⇒ 它能**实测拿到** `FINDING-360` 里"⛔ 不合成"的 `rqye`/`rzrqye` 两个量。
#: ⛔⛔ `融券余量`(数量)→`rqyl` 与 `融券余额`(金额)→`rqye` **必须分清** ——
#:   `FINDING-360` 就是在这上面踩过 1388% 的坑（两者量纲比约等于股价）。
_SZSE_MARGIN_MAP = {
    "证券代码": "ts_code",
    "融资买入额": "rzmre",      # 金额
    "融资余额": "rzye",        # 金额
    "融券卖出量": "rqmcl",      # 数量
    "融券余量": "rqyl",        # ⭐ 数量
    "融券余额": "rqye",        # ⭐ 金额（⛔ 与上一行不是同一个量）
    "融资融券余额": "rzrqye",    # 金额
}


#: 新浪财务分析指标列 → citydata `fina_indicator` 契约列（`FINDING-372`）。
#: ⛔⛔ 这张映射表是**逐期打表实测**出来的，⛔ 不是按名字像不像配的 ——
#:   同一个概念在新浪那侧有**多个口径变体**，配错变体会让"完全相等"看起来像"差 4%"：
#:     · `roe` ← **`加权净资产收益率`**（⛔ 不是 `净资产收益率`：后者未加权，
#:       20191231 实测 city 33.1177 vs 未加权 30.3 vs **加权 33.09**）
#:     · `bps` ← **`每股净资产_调整后`**（⛔ 不是 `_调整前`：
#:       20191231 实测 city 108.2714 vs 调整前 112.9411 vs **调整后 108.2714**）
#: ⭐ 这是 `FINDING-365` 那类"分母不同"教训的**另一种形状**：不是两家算法不同，
#:   而是**同一家给了多个变体**，选错就误判为不等价。
_SINA_FINA_MAP = {
    "加权净资产收益率(%)": "roe",
    "销售毛利率(%)": "grossprofit_margin",
    "流动比率": "current_ratio",
    "速动比率": "quick_ratio",
    "每股净资产_调整后(元)": "bps",
}


def _fina_from_sina(f: pd.DataFrame) -> pd.DataFrame:
    """新浪财务分析指标 → `fina_indicator` 契约（子集）。⛔ 只改名/改日期格式，不改数值。

    ⚠ 它只覆盖契约 110 列里的 **5 列**（见 `coverage` 声明）⇒ ⛔ 不是全量替代品，
      而是"主源挂了还能拿到最核心几个指标"的腿。⭐ 覆盖面在**运行时**可见（`FINDING-361`）。
    ⚠ 实测 `销售毛利率` 该源**逐期全为 NaN**（10/10 期）⇒ 名义上有列、实际取不到值。
      ⛔ 不因此把它从映射里删掉：列存在是事实，值缺失也是事实，两者都该被调用方看到。
    """
    out = f.rename(columns=_SINA_FINA_MAP)
    keep = [c for c in _SINA_FINA_MAP.values() if c in out.columns]
    if "日期" in out.columns:
        out["end_date"] = (pd.to_datetime(out["日期"], errors="coerce")
                           .dt.strftime("%Y%m%d"))
        keep = ["end_date"] + keep
    return out[keep].reset_index(drop=True)


#: akshare 东财"个股资金流排行"（`indicator='今日'`）→ `moneyflow_dc` 契约。
#: ⛔ 列名带**档位前缀**（`今日…` / `5日…`）⇒ 这张映射表**只对 `indicator='今日'` 成立**，
#:   故该候选把 `indicator` 写进 `pinned`（⛔ 不许调用方改档，改了列名就对不上）。
_AK_MONEYFLOW_DC_MAP = {
    "今日主力净流入-净额": "net_amount",
    "今日主力净流入-净占比": "net_amount_rate",
    "今日超大单净流入-净额": "buy_elg_amount",
    "今日超大单净流入-净占比": "buy_elg_amount_rate",
    "今日大单净流入-净额": "buy_lg_amount",
    "今日大单净流入-净占比": "buy_lg_amount_rate",
    "今日中单净流入-净额": "buy_md_amount",
    "今日中单净流入-净占比": "buy_md_amount_rate",
    "今日小单净流入-净额": "buy_sm_amount",
    "今日小单净流入-净占比": "buy_sm_amount_rate",
    "今日涨跌幅": "pct_change",
    "最新价": "close",
    "名称": "name",
}

#: ⭐ 金额列的单位换算因子：akshare 侧是**元**，契约（citydata 东财）侧是**万元**。
#: ⛔ 这是本仓唯一一处 `transform` 里做**数值**运算的地方，故把判据写在这里：
#:   实测 5,283 只共同标的上 `akshare / citydata` 的比值中位数 **恰好 10000.0000**，
#:   p1~p99 落在 9996.58~10004.71（⭐ 该离散**完全由 citydata 侧只保留 2 位小数
#:   的舍入造成**，不是口径差）；换算后相对差中位数 **0.0004%** ≤ 1% 判据。
#:   ⇒ 这不是"数值修正"，是**单位统一**；⛔ 若哪天比值不再是 1e4，守卫会红。
_AK_MONEYFLOW_YUAN_TO_WAN = 1e4
_AK_MONEYFLOW_AMOUNT_COLS = (
    "net_amount", "buy_elg_amount", "buy_lg_amount",
    "buy_md_amount", "buy_sm_amount",
)

#: 中文行情源里表示"无数据"的常规写法。
#: ⭐⭐ `FINDING-405` 的判据分界线就在这里：**源侧缺值**（本条集合内）与
#:   **有值但解析不了**是两件完全不同的事 ——
#:     · 源侧缺值 ⇒ 允许落成 NaN（源就是没给，缺就是缺）
#:     · 解析失败 ⇒ **必须抛**（那是"上游数据坏了"或"我映射错了"，
#:       而 `errors="coerce"` 会把它静默变成 NaN ⇒ 无人知晓）
#: ⛔ 不许把两者合并成一个"NaN 率上限"：那个阈值没有实测出处，
#:   且会在正常缺值上误红 —— 而误红的守卫下一轮就会被关掉。
_AK_MONEYFLOW_NULLISH = frozenset(
    {"", "-", "--", "—", "None", "none", "nan", "NaN", "null", "NULL"})


def _moneyflow_from_akshare(f: pd.DataFrame) -> pd.DataFrame:
    """akshare 东财个股资金流排行 → `moneyflow_dc` 契约。⭐ 只改名 + **单位换算**。

    ⛔⛔ **这个候选没有日期参数** ⇒ 它永远返回**当日实时快照**。
      ⇒ 由 `akshare::` 前缀的签名内省（实测 `{'indicator'}`）自动把任何带
        `trade_date` 的调用判为 `SKIP_UNHONORED` 而**跳过**，
        ⛔ 绝不能让它拿今天的数去应答"某个历史交易日"的请求 —— 那是
        `FINDING-343` 型静默换口径，且**比换列更隐蔽**（列名数值都对，只有日期错）。
      ⚠ 我第一版探针正是踩了这个坑：拿它比 20260807，判据③ 相关 **−0.0906**；
        把两边对齐到同一天后，同一对量 **1.000000 / 完全相等 100.0%**。
      ⭐ 故本函数**不回填** `trade_date`：那等于"把入参当数据"
        （与 `_margin_from_szse` 同一条纪律 —— 缺就是缺）。

    ⚠ 覆盖面：实测 akshare 5,292 只 ⊂ citydata 5,661 只（citydata 独有 369 只、
      akshare 独有 0 只）⇒ 已在 `coverage` 里声明，⛔ 不假装全量。
    """
    out = f.rename(columns=_AK_MONEYFLOW_DC_MAP)
    # ⛔⛔ `FINDING-405`：这里原本是
    #     `keep = [c for c in _AK_MONEYFLOW_DC_MAP.values() if c in out.columns]`
    #   —— 那个 `if c in out.columns` **就是静默丢弃**：13 个映射目标列少几个都
    #   不报错，行数照旧 >0，而 `base.py:276-278` 只按行数定状态 ⇒ 整体判 `OK`。
    #   ⇒ **「上游改列名」永远不会变红**，只会变成一张少列的表；下游按名取列
    #     `KeyError`，或 `reindex` 得到一整列 NaN。
    #   ⭐ 金额口径契约上"错得看起来像对"比"缺"危险得多 ⇒ 缺列即抛。
    #   ⚠ 抛异常在这里是**安全**的，前提是 `FINDING-390` 已把 `transform`
    #     纳入 `get()` 的异常边界 ⇒ 留 `EXC_TRANSFORM:` 痕迹后让位给同口径备胎。
    #     ⛔ 顺序不可颠倒：若没有 390，本断言会把静默降级升级成整条链崩溃。
    missing = [f"{dst}（源列 {src!r}）"
               for src, dst in _AK_MONEYFLOW_DC_MAP.items()
               if dst not in out.columns]
    if missing:
        raise ValueError(
            f"akshare 资金流排行缺少映射目标列 {missing}；"
            f"实得列 {list(f.columns)}。"
            f"⛔ 不静默丢弃：少列会让下游拿到一张'行数正常但字段不全'的表，"
            f"而状态仍是 OK（FINDING-405）")
    keep = list(_AK_MONEYFLOW_DC_MAP.values())
    out = out[keep].copy()
    # ⛔ 实测该源数值列**是字符串**（`str - str` 曾让探针 TypeError）⇒ 必须先数值化，
    #   否则下面的单位换算会静默变成字符串重复拼接。
    for col in keep:
        if col in ("name",):
            continue
        raw = out[col]
        # ⭐ 先把"源侧本来就没给"标出来，剩下的解析失败才是真缺陷（见 `_AK_MONEYFLOW_NULLISH`）。
        blank = raw.isna() | raw.astype(str).str.strip().isin(_AK_MONEYFLOW_NULLISH)
        conv = pd.to_numeric(raw, errors="coerce")
        unparseable = conv.isna() & ~blank
        if unparseable.any():
            bad = raw[unparseable].astype(str).unique()[:5].tolist()
            raise ValueError(
                f"akshare 资金流排行的 {col!r} 列有 {int(unparseable.sum())} 个值"
                f"无法数值化，样例 {bad}。"
                f"⛔ 不用 errors='coerce' 静默转 NaN：那会让'上游数据坏了'"
                f"变成一张带洞但状态 OK 的表（FINDING-405）")
        out[col] = conv
    for col in _AK_MONEYFLOW_AMOUNT_COLS:
        if col in out.columns:
            out[col] = out[col] / _AK_MONEYFLOW_YUAN_TO_WAN
    # ⛔ 同族第三处（⚠ **不在** `FINDING-405` 原文的 13 列判据里，是修它时在本函数
    #   看到的同形状缺陷，故一并修并在台账补记）：原本是 `if "代码" in f.columns:`
    #   ⇒ 缺 `代码` 时**连 `ts_code` 都不建**，产出帧**没有任何标识列**，
    #     调用方拿到一堆金额却不知道属于谁 —— 比缺一个金额列更严重。
    if "代码" not in f.columns:
        raise ValueError(
            f"akshare 资金流排行缺少 '代码' 列 ⇒ 无法构造 ts_code；"
            f"实得列 {list(f.columns)}。"
            f"⛔ 不静默产出无标识帧（FINDING-405 同族）")
    # ⭐⭐ 只给 6 位码 ⇒ 后缀必须由**全仓唯一那张**交易所表判定。
    #   ⛔ `FINDING-355` 的教训正是"路由层私写了第二份前缀表"（把北交所判成上海）
    #   ⇒ 这里**复用** `finai.security_ids.suffixed_security_code`，
    #     ⛔ 不在本模块里再写一次 `startswith` 分支。
    out.insert(0, "ts_code",
               [_security_ids.suffixed_security_code(str(c))
                for c in f["代码"]])
    return out.reset_index(drop=True)


def _limit_from_bid_ask(f: pd.DataFrame) -> pd.DataFrame:
    """东财逐票盘口（36 行**键值表**）→ `stk_limit` 契约（`up_limit`/`down_limit`）。

    ⭐ 该源返回的是 `item/value` 两列的**竖表**，⛔ 不是常规的一行多列 ——
      故本函数要先转置。⚠ 这也是它容易被漏看的原因：`涨停`/`跌停` 是**两行**，
      在列名清单里根本看不见（母库记录只写"36 行"）。
    ⛔ **不回填 `ts_code`/`trade_date`**：该表两者都不含（代码在请求参数里、
      日期隐含为"今天"）⇒ 与 `_margin_from_szse` 同一条纪律：
      把入参当数据是在制造看起来可信的假值。⭐ 缺就是缺，由调用方自己知道它问了谁。
    ⚠ 覆盖面：**一次只回答一只**（无全市场截面），且**永远是当日**
      ⇒ 已在 `coverage` 里声明，⛔ 不假装能替代主源那 7,734 行的截面。
    """
    if f.shape[1] < 2:
        return pd.DataFrame(columns=["up_limit", "down_limit"])
    kv = dict(zip(f.iloc[:, 0].astype(str), f.iloc[:, 1]))
    out = pd.DataFrame([{
        "up_limit": pd.to_numeric(kv.get("涨停"), errors="coerce"),
        "down_limit": pd.to_numeric(kv.get("跌停"), errors="coerce"),
    }])
    # ⛔ 两个值都缺 ⇒ 返回空表而**不是**一行 NaN：一行 NaN 会被 `get()` 当成
    #   "拿到数据了"（`len(frame)==1`）⇒ 那是用空值冒充成功（`FINDING-178` 的形状）。
    if out[["up_limit", "down_limit"]].isna().all(axis=None):
        return out.iloc[0:0]
    return out


def _margin_from_szse(f: pd.DataFrame) -> pd.DataFrame:
    """深交所融资融券明细 → tushare `margin_detail` 契约。⛔ 只改名/补后缀，不改数值。

    ⚠ 该表**不含日期列**（深交所按日出表，日期在请求参数里）⇒ 契约里的 `trade_date`
      **给不出来**。⛔ 不从请求参数回填：那是"把入参当数据"，一旦调用方与服务端对
      日期的理解不同（如非交易日顺延），回填的值就是**假的**。缺就是缺。
    ⭐ 后缀恒为 `.SZ`（该表只含深市）⇒ ⛔ 不猜交易所（`FINDING-355`）。
    """
    out = f.rename(columns=_SZSE_MARGIN_MAP)
    keep = [c for c in _SZSE_MARGIN_MAP.values() if c in out.columns]
    out = out[keep].copy()
    if "ts_code" in out.columns:
        out["ts_code"] = (out["ts_code"].astype(str).str.strip().str.zfill(6)
                          + ".SZ")
    return out.reset_index(drop=True)


def _margin_from_sse(f: pd.DataFrame) -> pd.DataFrame:
    """上交所融资融券明细 → tushare `margin_detail` 契约。⛔ 只改名/补后缀，不改数值。

    ⛔ **不合成** `rqye`（融券余额=金额）与 `rzrqye`（融资融券余额合计）：
      上交所这张表里**没有**这两个量，凭 `rqyl × 价格` 去算就是**制造数据**。
      ⇒ 缺就是缺（`FINDING-347` 原则：财务数据宁可缺不可错）。
      ⚠ 故降级后调用方拿到的列**少于**主源 —— 这是**声明过的**覆盖面差异，
        由 `test_margin_backup_declares_its_narrower_coverage` 钉住。
    ⭐ `ts_code` 补 `.SH` 后缀：上交所给裸 6 位码，而契约是 tushare 的 `600000.SH` 写法。
      ⛔ 不用 `_prefix_symbol()` —— 那给的是 `sh600000`（另一套写法，见 `FINDING-355`）。
      ⚠ 这张表**只含沪市**，故后缀恒为 `.SH`，⛔ 不需要（也不得）猜交易所。
    """
    out = f.rename(columns=_SSE_MARGIN_MAP)
    keep = [c for c in _SSE_MARGIN_MAP.values() if c in out.columns]
    out = out[keep].copy()
    if "ts_code" in out.columns:
        out["ts_code"] = (out["ts_code"].astype(str).str.strip().str.zfill(6)
                          + ".SH")
    if "trade_date" in out.columns:
        out["trade_date"] = out["trade_date"].astype(str).str.strip()
    return out.reset_index(drop=True)


def _build_kwargs(c: Candidate, params: dict[str, Any]) -> dict[str, Any]:
    """统一参数 → 该候选的实参。顺序：改名 → 改值 → 钉死。

    ⛔ `pinned` **最后**写入且不可被调用方覆盖 —— 它承载的是 schema 契约
      （如复权档），若能被覆盖，`FINDING-353` 的静默换口径就会复活。
    """
    kwargs: dict[str, Any] = {}
    for k, v in params.items():
        # ⛔⛔ `local_filter` 声明的参数**不得**发到线上：该源的服务端**不认识**它。
        #   实测（`FINDING-359`）：把 `start_date` 传给签名为空的
        #   `tool_trade_date_hist_sina` ⇒ `TypeError` ⇒ 母库判 `FAIL_PROBE_BUG`
        #   ⇒ 备胎又一次没能启用，只是失败原因从 SKIP 变成了 EXC。
        #   ⭐ 它在**本地**兑现（`_apply_local`），故此处跳过。
        if k in c.local_filter:
            continue
        kwargs[c.arg_map.get(k, k)] = c.value_map[k](v) if k in c.value_map else v
    kwargs.update(c.pinned)
    return kwargs


def _unhonored(c: Candidate, params: dict[str, Any]) -> list[str]:
    """调用方传了、但该候选**不会真正生效**的参数（映射后对不上签名）。

    ⭐ `local_filter` 里声明的参数**算被兑现** —— 但兑现地点是客户端（`_apply_local`）。
      ⛔ 这**不是**放宽 `FINDING-352`：那条的危害是"参数被静默吞掉、调用方拿到
        另一个区间的数据还以为对"。这里的参数**一定会被执行**，
        且由 `test_local_filter_is_actually_applied_not_just_declared` 看死
        （⛔ 只"声明"不"执行"就红）。
    """
    honored = _honored_params(c.key)
    if honored is None:
        return []
    return sorted(
        k for k in params
        if c.arg_map.get(k, k) not in honored
        and c.arg_map.get(k, k) not in c.pinned
        and k not in c.local_filter
    )


def _apply_local(c: Candidate, frame: pd.DataFrame,
                 params: dict[str, Any]) -> pd.DataFrame:
    """执行 `local_filter` 里声明的客户端过滤。⭐ 声明了就**必须**真的做。

    ⛔ 顺序在 `transform` **之后** —— 过滤器按**统一契约**的列名写
      （如 `cal_date`），而原始列名各源不同（`trade_date` / `cal_date`）。
    """
    for k, fn in c.local_filter.items():
        if k in params:
            frame = fn(frame, params[k])
    return frame


@dataclass
class RouteResult:
    """取数结果 + **来源可追溯性**（⭐ 调用方必须能知道数据来自哪个源）。"""
    ok: bool
    frame: pd.DataFrame | None
    used: str | None
    schema: str | None
    rows: int
    attempts: list[tuple[str, str, int]]  # (key, state, rows)
    #: ⭐ 实际启用的候选的覆盖面声明。空串 = 全量；非空 = **部分覆盖**（如 `"SH"`）。
    #: ⛔⛔ `FINDING-361`：这一栏存在的唯一理由是"降级可能静默少给半个市场"——
    #:   实测主源挂掉时备胎给 1,994 行而主源同日 4,424 行，列名/数值全对，
    #:   调用方从 frame 上**看不出来**。⇒ 覆盖面必须在**运行时**可读。
    #: ⚠ 默认空串是安全的：全量候选无需声明，⛔ 但部分覆盖的候选**漏声明就会**
    #:   被 `test_every_partial_coverage_candidate_declares_it` 判红。
    coverage: str = ""
    #: ⭐ 本次失败是否**全部**因为"参数在候选身上不生效"而跳过（`FINDING-386` 实测 A）。
    #: ⛔ 存在的唯一理由：失败的 `RouteResult` 与"源真的没数据"**长得一模一样**
    #:   （`ok=False / rows=0 / frame=None`）⇒ 调用方会把自己的调用问题
    #:   读成"数据源不可用"，往错误方向排查。
    #: ⚠ 真实 registry 上可达：`moneyflow` 的 `moneyflow_market` schema 只有
    #:   `akshare::stock_market_fund_flow` 一个候选且签名为空 ⇒ 连合法的
    #:   `trade_date` 都会被它吞掉 ⇒ 该 schema 下 100% 全跳过。
    #: ⛔ 刻意**不抛异常**：那是行为变更（现在返回空 `RouteResult`），
    #:   可能有调用方依赖"不抛"⇒ 只加一个只读位，不动返回形状。
    all_skipped_unhonored: bool = False
    #: ⭐ **失败时**每个被考察过的候选自己的 `note`，形如 `[(key, note), …]`。
    #: ⛔⛔ `FINDING-388` ③：`Candidate.note` 的 docstring 公开声明它"会出现在
    #:   失败报告里"，而实测**数据流从未连接** —— `RouteResult` 上没有任何位置
    #:   承载它，`raise_if_failed()` 只格式化 `(key,state,rows)`
    #:   ⇒ 无论何种失败 note 都不可能出现。这不是某个候选漏填，是声明与
    #:   运行时分叉（`FINDING-361` 的形状：声明只有运行时可见才算契约）。
    #: ⭐ 为什么是 `(key, note)` 序列而不是塞进 `attempts`：全仓约 12 处按
    #:   `(k, s, n)` **三元组解包** `attempts` ⇒ 改它的宽度会炸掉那些调用方。
    #: ⚠ 成功时**恒为空**：成功路径上调用方拿到的是数据，把"已知限制"混进去
    #:   会被读成"本次数据有问题"；覆盖面缺口另有 `coverage` 在运行时承载。
    notes: list[tuple[str, str]] = field(default_factory=list)
    #: ⭐⭐ **失败时**本次口径锁下的可用故障域实测（`FINDING-435` 关闭条件②）。
    #: ⛔ 存在的唯一理由：`attempts` 只说"试了哪些腿"，⛔ 说不出**为什么某条腿
    #:   压根不可能被试到**。实测（`FINDING-444`）三个 `(能力, schema)` 声明
    #:   `domains=2` 而第二条腿在声明的调用契约下**必然**被跳过 ⇒ 调用方读
    #:   `attempts` 会以为"备胎也挂了"（去找运维），真相是"备胎从来没上过场"
    #:   （该改注册表）。⇒ 两种处境的修法相反，故必须在运行时可分。
    #: ⚠ 成功时为 `None`：成功路径的返回形状刻意不动（与 `notes` 同一条纪律）。
    availability: "Availability | None" = None

    @property
    def is_partial(self) -> bool:
        """本次取数是否**只覆盖了一部分市场/标的**。⭐ 调用方判缺口只看这一个属性。

        ⛔ 不要用 `used` 去反查覆盖面 —— 那要求每个调用方都记住哪个接口只含沪市，
          正是 `FINDING-11` 那一族"知识散落在调用方"缺陷的成因。
        """
        return bool(self.coverage)

    def warn_if_partial(self) -> str | None:
        """部分覆盖时返回**可直接记日志**的人话；全量时返回 `None`。

        ⭐ 刻意**不 raise**：降级本身是本层的**正常功能**（主源挂了还有腿），
          ⛔ 但它不该**静默**发生。⇒ 给调用方一句能落到日志里的话。
        """
        if not self.coverage:
            return None
        return (f"⚠ 数据来自备胎 {self.used}，其覆盖面为 {self.coverage!r}"
                f"（非全市场）⇒ 本次 {self.rows} 行**不含**该范围外的标的。"
                "⛔ 勿当全量使用（FINDING-361）。")

    def note_report(self) -> str:
        """失败候选各自的 `note` 渲染成一段可直接读的文本；无 note 时返回空串。

        ⛔⛔ `FINDING-388` ③ 的落点。⭐ 为什么它必须进**异常文本**而不只是字段：
          `raise_if_failed()` 的使用者**只看得到异常文本**（读不到 `RouteResult`
          的任何栏）—— 与 `all_skipped_unhonored` 当初的处境完全相同
          （`FINDING-386` 已在这一点上踩过一次）。
        ⚠ note 里常写着"深度只到 X"/"不接受该参数过滤"/"仅沪市" ——
          那正是失败时调用方要的答案，把它留在源码里等于没写。
        """
        pairs = [(k, n) for k, n in self.notes if n]
        if not pairs:
            return ""
        return "；".join(f"{k} 自述：{n}" for k, n in pairs)

    def raise_if_failed(self) -> pd.DataFrame:
        """要么给出数据，要么**响亮失败**。⛔ 绝不返回空表冒充成功。"""
        if not self.ok or self.frame is None:
            detail = " | ".join(f"{k}→{s}({n})" for k, s, n in self.attempts)
            # ⛔ `FINDING-388` ③：候选的 `note` 必须进**这段文本** —— 见 `note_report()`。
            if (notes := self.note_report()):
                detail = f"{detail}。候选自述的已知限制：{notes}"
            # ⛔ `FINDING-386`：全跳过时说"未取到数据"**是假的** —— 一个请求都没发出去。
            #   ⭐ "没问过"（改调用/补 local_filter）与"问了没有"（找运维/换源）
            #     指向完全不同的排查方向，而 `raise_if_failed()` 的使用者
            #     只看得到这段文本，读不到 `all_skipped_unhonored`。
            # ⭐⭐ `FINDING-444`：可用域高估必须进**异常文本** —— 与 note 同理，
            #   `raise_if_failed()` 的使用者读不到 `RouteResult` 的任何栏。
            if self.availability is not None and \
                    (warn := self.availability.warn_if_overstated()):
                detail = f"{detail}。{warn}"
            if self.all_skipped_unhonored:
                raise RuntimeError(
                    f"**一个请求都没发出去**：全部候选都因参数在其签名上不生效而被跳过。"
                    f"逐条实测：{detail}。"
                    "⛔ 这不是'源没数据'，是本次调用的参数落不到任何候选身上 ⇒ "
                    "要么改调用参名，要么给候选补 arg_map/local_filter（FINDING-386）。"
                )
            raise RuntimeError(
                f"全部候选均未取到数据。逐条实测：{detail}。"
                "⛔ 未返回空表冒充成功（FINDING-178）。"
            )
        return self.frame


#: ⭐ 每个能力的**统一参名**（调用方会传的那一组），按能力分组。
#: ⛔⛔ `FINDING-389`：这张表原本**手抄了两份**（`tests/test_capability_router.py` 里
#:   一份三条的 `unified`、`scripts/_r37_f352_argmap_audit.py` 里一份十条的 `UNIFIED`）
#:   ⇒ registry 加一个能力时**两份判据都不会红**，静态体检会拿**错的参名**去探测它，
#:   于是"全表体检通过"这句话可以在实际有缺口时照样为真。
#:   ⇒ 现在这里是唯一出处，那两处改为消费它（`capability_params()`）。
#: ⚠ 这里写的是"调用方**会**传什么"，⛔ **不是**"底层签名收什么" ——
#:   后者由 `_honored_params()` 内省得到，两者的**差集**才是 `FINDING-352` 要抓的东西。
CAPABILITY_PARAMS: dict[str, tuple[str, ...]] = {
    "daily_bar": ("symbol", "start_date", "end_date"),
    "moneyflow": ("trade_date",),
    "chip_distribution": ("trade_date",),
    "factor_panel": ("trade_date",),
    "limit_price": ("trade_date",),
    "limit_list": ("trade_date",),
    "auction": ("trade_date",),
    "margin": ("trade_date",),
    "fina_indicator": ("period",),
    "trade_cal": ("start_date", "end_date"),
}


def capability_params(cap: str) -> tuple[str, ...]:
    """该能力的统一参名。⛔ 未登记就**抛**，不给默认值。

    ⭐ 这是 `FINDING-362` 的形状（守卫盲区）：若漏登记时静默退回
      `("trade_date",)`，一个**其实收 symbol/日期**的新能力会被用错的参数探测，
      静态体检便形同虚设却仍报绿。⇒ 宁可抛错，让加能力的人**必须**登记。
    """
    try:
        return CAPABILITY_PARAMS[cap]
    except KeyError:
        raise KeyError(
            f"能力 {cap!r} 未在 CAPABILITY_PARAMS 登记统一参名 ⇒ 静态体检无法探测它。"
            "⛔ 不许靠默认值兜底（FINDING-389）"
        ) from None


#: ⭐ 能力表。每个能力的候选**按实测可靠性排序**，⛔ 不按"我觉得哪个好"。
#: ⚠ 每条 `note` 都是实测结论且**带日期** —— `FINDING-341` 的教训：
#:   能力类结论不带日期就会被后人当成永久真理（我自己犯过）。
def _CITYDATA(api: str) -> Upstream:
    """`citydata::<api>` 的上游声明。⚠ **`origin_verified=False`** —— 这是本表里
    唯一一处我**证不出数据原始出处**的地方，故把不确定性写进类型而不是编个标签。

    ⭐ 能实测到的（`finai/sources/citydata_source.py:40`/`:182`）：本适配器
      `POST {CITYDATA_URL or https://tushare.citydata.club}/{api_name}`，
      body 带 `token/params`，且**必须**经商家指定代理出网（无直连兜底）。
      ⇒ "这条腿问谁要数据"是确定的：那台商家主机（+ 那条代理）。
    ⛔ 证不出的：那台主机背后是不是又转手了官方 tushare / 交易所 / 东财。
      模块 docstring 自述它是"闲鱼商家提供的 tushare 镜像"——⚠ 那是**人话描述**，
      ⛔ 不是我能从安装代码里读出的事实，故**不**据此写 `fault_domain="tushare"`。
    ⇒ 因此本表里两条 `origin_verified=False` 的腿之间"故障域不同"这一结论，
      只在**镜像运营方**这一层成立；⚠ 若其背后其实同源，独立性弱于声明。
      ⭐ 这个上界必须可见 —— 那正是 `FINDING-407` 反对"编一个自信标签"的理由。
    """
    return Upstream(
        "citydata-merchant-mirror",
        f"POST https://tushare.citydata.club/{api}（经商家指定代理）",
        "finai/sources/citydata_source.py:40 DEFAULT_URL + :182 requests.post("
        "f'{url}/{api_name}',…, proxies=proxies)；⚠ 镜像背后的原始出处"
        "无法从安装代码证实 ⇒ origin_verified=False",
        origin_verified=False,
    )


CAPABILITIES: dict[str, list[Candidate]] = {
    # ── 日频行情 ─────────────────────────────────────────────────────
    # ⭐⭐ 本能力是全表**唯一**真有同口径备胎的一条，故三条 `Candidate` 的等价性
    #   是被逐元素实测过的，⛔ 不是"看列名像"就写上的（`FINDING-351` 的教训）：
    #     · `stock_zh_a_hist(adjust='')` vs `efinance(fqt=0)`：
    #       10 个共有列 **4797/4797 全等**（3 只股 × 1599 交易日，含除权日）。
    #     · 不钉复权档则 300750 的开盘价差到 **328 元**（`efinance` 默认前复权）
    #       ⇒ `pinned` 就是为这个存在的。
    "daily_bar": [
        # ⭐⭐ 排序原则（`FINDING-354` 的教训）：**默认链的首选必须是"有备胎的那个"**。
        #   我一度把最轻的 `..._hist_tx` 放第一位，它却是全表唯一一个**独占 schema**
        #   的候选 ⇒ 口径锁一上，默认链就只剩它一条腿，`daily_bar` 反而**失去了备胎** ——
        #   而"坏了立刻换下一个"正是本层存在的唯一理由。⛔ 快 ≠ 该当首选。
        # ⛔⛔ `FINDING-402`/`FINDING-407`：下面两条**包名不同、上游相同** ——
        #   源码实测请求逐字相同的 push2his K 线端点 ⇒ 它们**不是**互相独立的备胎。
        #   这一对现在是 `test_same_upstream_different_packages_must_fail_the_
        #   vendor_guard` 的**反例夹具**：任何"备胎必须跨厂商"的判据都必须在
        #   这一对上变红。⛔ 不删任何 truthful 候选来改数字（wrapper 级冗余仍在：
        #   解析器/包级崩溃互不影响），⛔ 但也不许再把它冒充成跨上游冗余。
        Candidate("akshare::stock_zh_a_hist", "ohlcv_daily",
                  value_map={"symbol": _plain_symbol},
                  pinned={"adjust": "", "period": "daily"},
                  required=("symbol", "start_date", "end_date"),
                  upstream=Upstream(
                      "eastmoney",
                      "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                      "akshare/stock_feature/stock_hist_em.py:952 "
                      "stock_zh_a_hist() 内 requests.get(url,…)"),
                  # ⭐ R4 §3.3：口径符号化。pinned["adjust"]="" ⇔ RAW（AKSHARE 表）。
                  adjust=AdjustmentMode.RAW,
                  note="2026-08-10 实测 1599 行/6.5年·12 列；⭐ adjust 钉死为不复权"),
        Candidate("efinance::stock.get_quote_history", "ohlcv_daily",
                  arg_map={"symbol": "stock_codes",
                           "start_date": "beg", "end_date": "end"},
                  value_map={"symbol": _plain_symbol},
                  pinned={"fqt": 0},
                  required=("symbol", "start_date", "end_date"),
                  transform=lambda f: f.drop(columns=["股票名称"], errors="ignore"),
                  upstream=Upstream(
                      "eastmoney",
                      "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                      "efinance/common/getter.py:109 get_quote_history_single() "
                      "内 session.get(url,…)；调用链 stock.get_quote_history → "
                      "get_quote_history_for_stock → get_quote_history_single"),
                  # ⭐ R4 §3.3：口径符号化。pinned["fqt"]=0 ⇔ RAW（EFINANCE 表）。
                  adjust=AdjustmentMode.RAW,
                  note="2026-08-10 实测与上条 4797/4797 逐元素全等（fqt=0 时）；"
                       "⛔ 它带 **kwargs，参名写错会静默返回 35 年全量（FINDING-352）；"
                       "⛔⛔ 与上条**同上游**（push2his，FINDING-402）⇒ 不是独立备胎"),
        # ⚠ 它**最轻**（1 页 5.4s）但**最薄**：只有 6 列、无成交额/换手率，
        #   且 `amount` 是**成交量**不是成交额（与 `成交量` 中位比值 1.0，
        #   相对误差 ≤3.3e-4 的取整抖动）⇒ 自己一个 schema，
        #   ⛔ 不与上面两条自动互换。要它请**显式** schema="ohlcv_daily_tx"。
        Candidate("akshare::stock_zh_a_hist_tx", "ohlcv_daily_tx",
                  value_map={"symbol": _prefix_symbol},
                  required=("symbol", "start_date", "end_date"),
                  # ⭐ 同一个 `akshare` 包，上游却是**腾讯**不是东财 ——
                  #   这条就是"包名推不出上游"的现成反例（`FINDING-407`）。
                  upstream=Upstream(
                      "tencent",
                      "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/"
                      "newfqkline/get",
                      "akshare/stock_feature/stock_hist_tx.py:19 "
                      "stock_zh_a_hist_tx() 内 requests.get(url,…)"),
                  note="2026-08-10 实测 sz000001/28 行/5.4s（⛔ 必须 sz/sh 前缀，"
                       "裸码报 ValueError；⛔ 6 列·无成交额，amount 才是成交量）"),
        Candidate("citydata::daily", "ohlcv_daily_ts",
                  arg_map={"symbol": "ts_code"},
                  upstream=_CITYDATA("daily"),
                  note="tushare 口径（ts_code/trade_date）⇒ schema 与上面**不同**，"
                       "⛔ 不与它们自动互换"),
        # ⭐⭐ R43 Phase 2 T1（2026-08-14）：**真独立备用源** —— mootdx TCP 7709。
        #   实测真相（本轮亲测，⛔ 不轻信转交文档）：
        #     · `akshare`/`efinance` 那两条 `ohlcv_daily` 腿走**同一个** push2his HTTP
        #       ⇒ `independent_fault_domains("daily_bar", schema="ohlcv_daily")` = **1**。
        #     · mootdx 走 **TCP 二进制协议**到通达信服务器（10 台，实测 3/10 可连：
        #       218.75.126.9 / 115.238.56.198 / 60.12.136.250，见 `tdx_source.py`
        #       docstring），与 eastmoney HTTP 完全独立 ⇒ 真独立故障域。
        #     · 本轮实测 `StdQuotes().bars('000001', frequency=9, offset=2000)`
        #       0.1s 返 800 根，过滤到 2026-07-01..2026-08-14 得 33 行 ⇒ **可用**。
        #     · 同轮 akshare/efinance `push2his` 三次重试全 `ProxyError: RemoteDisconnected`
        #       ⇒ 主源当前不可达，本候选恰是它挂时的真备胎。
        # ⛔ **等价性判据延后**（诚实登记，不造假）：本轮环境东财系不可达，
        #   mootdx vs akshare 的 Pearson/中位相对差**无法实测**。仓内已有证据
        #   `efinance vs akshare` 在 20260701..20260710 上 OHLCV **逐位相等**（n=28，
        #   不等=0，台账 26057-26060 行）⇒ 间接佐证 mootdx 与同 schema 候选对齐
        #   的可能性，但⛔不是直接等价性实测。判据留 OPEN，见台账 FINDING-475。
        # ⚠ `key="tdx::daily_bar"` 是**伪 lib::name 格式** —— 不走 catalog
        #   （`catalog_source.fetch()` 第 329-331 行明写 tdx 不入该入口）。
        #   `get()` 里按 `tdx::` 前缀分流到 `tdx_daily_bar_adapter.fetch()`，
        #   ⛔ 不让其他 agent/测试误以为它走 catalog（24 个既有候选无一这种格式）。
        Candidate("tdx::daily_bar", "ohlcv_daily",
                  pinned={"adjust": ""},  # ⭐ FINDING-353：ohlcv_daily 每个候选必须钉复权档
                          #   与主源 akshare `adjust=""` 不复权对齐（mootdx 返 raw 价）
                  required=("symbol", "start_date", "end_date"),
                  upstream=Upstream(
                      "tdx",
                      "通达信 TCP 7709（10 台内置服务器，实测 3/10 可连）",
                      "finai/sources/tdx_source.py::fetch_bars "
                      "（封装 finai/tdx_minute5.py::_connect_tdx_api，"
                      "复用其健康节点缓存 data/tdx_healthy_nodes.json）"
                  ),
                  # ⭐ R4 §3.3：口径符号化。pinned["adjust"]="" ⇔ RAW（TDX 无复权参数）。
                  adjust=AdjustmentMode.RAW,
                  note="⛔ R5：TDX 腿已砍（v1 仅日线，data_catalog 未搬入）——"
                       "connect() 触发 ModuleNotFoundError ⇒ 本候选恒失败、永不返回数据；"
                       "如未来需要分钟线再行恢复（R5 方案 A，搬入最小裁剪 data_catalog）。"
                       "⛔ 不走 catalog（tdx 需会话初始化，见 tdx_daily_bar_adapter）；"
                       "⭐ 同 schema=ohlcv_daily ⇒ 主源 push2his 挂时本应 fallback 到本腿"
                       "（现已砍）"),
    ],
    # ── 资金流：实测 citydata 的 moneyflow_dc 6,038 行最厚 ──────────────
    "moneyflow": [
        Candidate("citydata::moneyflow_dc", "moneyflow_dc",
                  upstream=_CITYDATA("moneyflow_dc"),
                  note="2026-08-10 实测 6,038 行"),
        Candidate("citydata::moneyflow_ths", "moneyflow_ths",
                  upstream=_CITYDATA("moneyflow_ths"),
                  note="2026-08-10 实测 3,532 行；⛔ 与 _dc 字段不同，schema 分开"),
        # ⛔ `FINDING-352`：它的签名是**空的**（无任何参数）⇒ 调用方传 `trade_date`
        #   会得到 `TypeError`（母库归为 `FAIL_PROBE_BUG`）。它返回的是**全历史大盘**
        #   汇总，不接受任何过滤 ⇒ 现在由 `_unhonored()` 的**签名内省**自动跳过，
        #   ⛔ 不再让它以"候选"的身份制造"有备胎"的错觉。
        # ⛔ `FINDING-389` 更正：本注释原写"由 `required=()` + 签名检查自动跳过" ——
        #   `required=()` 在这里**什么也没做**（它是默认值，且当时全仓无任何消费方）。
        #   真正跳过它的只有 `_unhonored()` 的内省。把功劳记在不干活的字段上，
        #   会让后人以为"删掉签名检查也没事，反正有 required 兜着"。
        Candidate("akshare::stock_market_fund_flow", "moneyflow_market",
                  upstream=Upstream(
                      "eastmoney",
                      "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
                      "akshare/stock/stock_fund_em.py:347 "
                      "stock_market_fund_flow() 内 requests.get(url,…)"),
                  note="2026-08-10 实测 120 行（大盘级·非个股·⛔ 不接受 trade_date 过滤）"),
        # ⭐⭐ `FINDING-373`：`moneyflow` 曾被 `FINDING-367` 判为**单点**，那是**漏测**。
        #   母库 `category='资金流'` 有 27 条 `state=OK`，那条结论只测了
        #   `stock_market_fund_flow`（大盘级 120 行）就写下"另外两个候选也不成"，
        #   而 `stock_individual_fund_flow_rank` 实测是 **5,292 行个股截面**，
        #   与 `moneyflow_dc` 是**同一个问题** ⇒ 与 `FINDING-365`/`-369`/`-372` 同一错误形状。
        # ⭐ 实测（20260810，5,283 只共同标的）：10 个契约列**全部** 相关 1.000000；
        #   5 个金额列比值中位数恰好 1e4（单位 元 vs 万元），换算后相对差中位数 0.0004%；
        #   5 个占比列**无需换算**即 0.0000%；判据③ 对照量 `pct_change` 完全相等 100.0%
        #   ⇒ 满足 `FINDING-347` 冻结判据 ⇒ **同 schema**（与 `_ths` 那条不同，那条是真不等价）。
        # ⛔⛔ 它**没有日期参数**（签名内省实测 `{'indicator'}`）⇒ 任何带 `trade_date`
        #   的调用都会被 `_unhonored()` 判 `SKIP_UNHONORED` 而跳过，
        #   ⛔ 故它**只能**服务"当日快照"型调用，绝不会拿今天的数冒充历史某日。
        #   ⚠ 这也意味着**回测路径拿不到它** —— 覆盖面里如实声明，⛔ 不假装是全能备胎。
        Candidate("akshare::stock_individual_fund_flow_rank", "moneyflow_dc",
                  pinned={"indicator": "今日"},
                  transform=_moneyflow_from_akshare,
                  upstream=Upstream(
                      "eastmoney",
                      "https://push2.eastmoney.com/api/qt/clist/get",
                      "akshare/stock/stock_fund_em.py:122 "
                      "stock_individual_fund_flow_rank() 内 requests.get(url,…)"),
                  coverage="仅当日快照(无日期参数)·5292只⊂主源5661只",
                  note="2026-08-10 实测 5,292 行个股截面；金额列单位为**元**，"
                       "已在 transform 里 /1e4 归到契约的**万元**口径 "
                       "（比值中位数实测恰好 10000.0000）。"
                       "⛔ `indicator` 钉死为 '今日'：列名带档位前缀，改档即对不上映射表"),
    ],
    # ── 筹码分布：FINDING-117 点名"磁盘上有却从未进面板"的材料之一 ────────
    "chip_distribution": [
        Candidate("citydata::cyq_perf", "cyq_perf",
                  upstream=_CITYDATA("cyq_perf"),
                  note="2026-08-10 实测 77~156 行"),
        Candidate("citydata::cyq_chips", "cyq_chips",
                  upstream=_CITYDATA("cyq_chips"),
                  note="⛔ 与 cyq_perf 字段不同"),
    ],
    # ── 技术因子面：⭐ FINDING-117 的直接解（面板只有 11 列 → 261 列）─────
    "factor_panel": [
        Candidate("citydata::stk_factor_pro", "stk_factor_pro",
                  upstream=_CITYDATA("stk_factor_pro"),
                  note="2026-08-10 实测 261 列。⚠ 曾观测一次 EMPTY_OK/0 后立即复现 OK/81 "
                       "⇒ 商家侧抖动，正是本层存在的理由"),
    ],
    # ── 涨跌停：执行状态判定要用（FINDING-113 家族）──────────────────────
    # ⚠ 两个 schema 各自独立：`stk_limit` 是**涨跌停价格**（4 列），
    #   `limit_list_d` 是**涨跌停名单**（19 列）—— ⛔ 不是一回事，故 schema 分开，
    #   `get("limit_price")` 默认只在**价格**口径内降级（`FINDING-343`）。
    "limit_price": [
        Candidate("citydata::stk_limit", "stk_limit",
                  upstream=_CITYDATA("stk_limit"),
                  note="2026-08-10 实测 7,733/7,727/7,725/7,716 行（四个交易日）"),
        # ⛔ **本能力目前只有一个候选，这是已知缺口，⛔ 不假装有备胎。**
        #   我曾想拿 `citydata::bak_daily` 充当同口径备胎，理由是"它带 up_limit/down_limit"
        #   —— **实测证伪**：它的 31 列里**没有任何 limit 字段**
        #   （`ts_code…pct_change,close,…,interval_3,interval_6`）。
        #   ⇒ 若照那个假设写进来，`stk_limit` 失败时会降级到一个**没有涨跌停价的表**，
        #     而调用方以为拿到了涨跌停价 —— 正是 `FINDING-343` 那类静默换口径。
        #   ⭐ 宁可留一个"已知单点"，也不要一个假备胎（财务数据宁可缺不可错）。
        #   ⚠ 实测 `stk_limit` 本身很稳（四个交易日 7,733/7,727/7,725/7,716 行），
        #     那次 0 行是瞬时抖动，同参数几分钟后即恢复。
        # ⭐⭐ `FINDING-375`：上面那段"只有一个候选"的结论**在 2026-08-10 被实测推翻**。
        #   它只排除了 `citydata::bak_daily`，⛔ 而母库 `category='行情-实时/快照'`
        #   有 58 条 `state=OK`，其中 `akshare::stock_bid_ask_em` 的 **36 行键值表里
        #   就有 `涨停`/`跌停` 两行** ——⚠ 因为它是**竖表**，列名清单里看不见，
        #   于是被漏看了（`FINDING-365`/`-369`/`-372`/`-373` 的同一错误形状，第 5 次）。
        # ⭐ 实测（20260810，10 只、覆盖 10%/20%/30% 三档涨跌幅）：
        #   涨停 **10/10**、跌停 **10/10** 与主源**到分完全相等**；
        #   判据③ 用 `昨收` 复算幅度档，实得 {10.0, 20.0, 30.0} 三档整数 ⇒ 对照量相符。
        #   ⇒ 满足 `FINDING-347` 冻结判据 ⇒ 同 schema。
        # ⛔⛔ 但它的口径窄得多，且这是**运行时必须可见**的（`FINDING-361`）：
        #   ① **一次只回答一只**（无全市场截面）⇒ 要覆盖全市场需 5,000+ 次调用；
        #   ② **无日期参数** ⇒ 永远是当日 ⇒ 带 `trade_date` 的调用由签名内省
        #      （实测 `honored={'symbol'}`）判 `SKIP_UNHONORED` 而跳过，
        #      ⛔ 绝不会拿今天的涨跌停价冒充历史某日。
        Candidate("akshare::stock_bid_ask_em", "stk_limit",
                  arg_map={"ts_code": "symbol"},
                  value_map={"ts_code": _plain_symbol},
                  transform=_limit_from_bid_ask,
                  upstream=Upstream(
                      "eastmoney",
                      "https://push2.eastmoney.com/api/qt/stock/get",
                      "akshare/stock/stock_ask_bid_em.py:13 "
                      "stock_bid_ask_em() 内 requests.get(url,…)"),
                  coverage="单只逐票·仅当日(无日期参数)·⛔无全市场截面",
                  note="2026-08-10 实测 36 行**键值竖表**，`涨停`/`跌停` 各占一行；"
                       "10 只样本（含 920xxx 北交所 30% 档）与主源到分 10/10 相等。"
                       "⛔ 不回填 ts_code/trade_date（表里没有，回填即造假值）"),
    ],
    #: ⛔ 名单口径**单独成一个能力**，⛔ 不与价格混在同一条链里。
    "limit_list": [
        Candidate("citydata::limit_list_d", "limit_list_d",
                  upstream=_CITYDATA("limit_list_d"),
                  note="2026-08-10 实测 104 行；⛔ 是「涨跌停名单」，与价格不同"),
        # ⭐ `FINDING-352`：它的实参名是 `date` 不是 `trade_date`。原先 `arg_map` 为空
        #   ⇒ 传 `trade_date` 直接 `TypeError`。实测改名后 `OK/74`（20260807）。
        Candidate("akshare::stock_zt_pool_em", "zt_pool_em",
                  arg_map={"trade_date": "date"},
                  upstream=Upstream(
                      "eastmoney",
                      "https://push2ex.eastmoney.com/getTopicZTPool",
                      "akshare/stock_feature/stock_ztb_em.py:24 "
                      "stock_zt_pool_em() 内 requests.get(url,…)"),
                  note="2026-08-10 实测 74 行（date=20260807）；"
                       "⛔ 字段与 tushare 口径不同，schema 分开"),
        # ⭐⭐ R43 Phase 2 T2（2026-08-14）：**真独立备用源** —— 同花顺涨停池。
        #   实测真相（本轮亲测，⛔ 不轻信转交文档）：
        #     · 端点 `https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool`
        #       （任务文档说 `basic.10jqka.com.cn` —— ⛔ **错了**，实测真端点是
        #        `data.10jqka.com.cn`，`basic` 那台是一致预期 EPS 不是涨停池）
        #     · 本轮实测 `date=20260813 limit=5` ⇒ `status=200 / bytes=6154 / info=5`
        #       ⇒ **可用**。主源 `akshare::stock_zt_pool_em(date=20260813)` 同轮 59 行可达。
        #     · `independence_basis` 当前 "mirror-only"：citydata 那条 `origin_verified=False`
        #       镜像 ⇒ 必须算它才够 2 域。加本候选 `origin_verified=True` 后，
        #       仅 verified 集就跨 ≥2 域（eastmoney + ths）⇒ **升 "verified"**。
        # ⚠ schema 决策（本轮自决）：建**新 schema `ths_limit_up`**，不复用任一现有 schema。
        #   理由：同花顺返回字段（`code/name/latest/change_rate/reason_type/limit_up_type/
        #   limit_up_suc_rate/open_num/order_amount/high_days/first_limit_up_time/is_again_limit`）
        #   与 `limit_list_d`/`zt_pool_em` 都不同 ⇒ 跨 schema 静默降级会把"涨停揭秘"
        #   和"涨停名单"两种不同数据混着给调用方（FINDING-343 教训）。
        #   ⇒ 调用方要同花顺数据请**显式** `schema="ths_limit_up"`。
        # ⚠ 故本候选**不会与主源自动 fallback**（schema 锁不同）—— T2 的真独立价值
        #   不在 fallback，在 `independence_basis` 从 mirror-only 升 verified。
        Candidate("ths::limit_up_pool", "ths_limit_up",
                  required=("trade_date",),
                  upstream=Upstream(
                      "ths",
                      "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool",
                      "finai/sources/ths_limit_up_adapter.py::fetch "
                      "（HTTP JSON，实测 2026-08-14 status=200/info=5）",
                      origin_verified=True,
                  ),
                  note="2026-08-14 实测 data.10jqka.com.cn status=200/bytes=6154·"
                       "date=20260813 limit=5 返 5 只涨停股；"
                       "⛔ 不走 catalog（akshare 无此接口、未收编台账）；"
                       "⭐ origin_verified=True ⇒ 把 independence_basis 从 mirror-only 升 verified"),
    ],
    # ── 集合竞价：MEASURE-20260804-4-NEW-1 实测"我们在开盘竞价成交" ───────
    "auction": [
        Candidate("citydata::stk_auction", "stk_auction",
                  upstream=_CITYDATA("stk_auction"),
                  note="2026-08-10 实测 5,513~7,885 行/日"),
    ],
    # ── 融资融券 ────────────────────────────────────────────────────
    # ⭐⭐ 全表**第三个**真备胎、**第二个跨厂商**的能力（`FINDING-360`，2026-08-10 实测）。
    #   按 `FINDING-347` 冻结判据逐条测过（`scripts/_r38_equiv_margin.py`，trade_date=20260807）：
    #     · 6 个量**全部**相关 = 1.000000、中位相对差 ≤ 0.0001%、>1% 占比 0.00%
    #     · 标的集合**完全一致**：沪市 1,994 只内连 1,994，两侧差集**均为空**
    #     · 判据③第三方对照量：证券简称与**独立第三方** `akshare::stock_info_a_code_name`
    #       核对 **5/5 相符**（⛔ 刻意不用 citydata 自己的 stock_basic —— 那是循环论证）
    #     · 残差实测 max 相对差 **4.9e-6**（float32 存储精度量级）⇒ 是取整而非口径差
    # ⛔⛔ 这一对差点被我自己写错的列映射判成"不等价"：我最初把 `rqye` 对到「融券余量」，
    #   实测相关 0.9234 / 中位相对差 **1388%**。真因不是数据不一致，而是
    #   `rqye` = 融券余**额**（金额/元）而 `rqyl` = 融券余**量**（数量/股），
    #   上交所披露的「融券余量」是**数量** ⇒ 该对 `rqyl`（中位比值 14.88 正是元/股量纲比）。
    #   ⇒ ⭐ 与 `FINDING-351`（tx 的 `amount` 其实是成交量）**完全同型**：按名字猜口径会错。
    # ⚠ 覆盖面**不同**且这是**已知且必须声明的限制**：citydata 是全市场（沪+深+北，4,424 行），
    #   上交所披露**只有沪市**（1,994 行）⇒ 降级到备胎时**深市/北交所标的会缺失**。
    #   ⛔ 故它排在**第二位**（只在主源挂掉时启用），且 note 里写明缺口 ——
    #   ⛔ 不假装两者覆盖面相同（那会是"静默少给一半市场"，比响亮失败危险）。
    "margin": [
        Candidate("citydata::margin_detail", "margin_detail",
                  upstream=_CITYDATA("margin_detail"),
                  note="2026-08-10 实测 2,517~4,424 行（全市场：沪+深+北）"),
        Candidate("akshare::stock_margin_detail_sse", "margin_detail",
                  arg_map={"trade_date": "date"},
                  transform=_margin_from_sse,
                  # ⭐ 上交所**自营域**：请求打 `query.sse.com.cn`（Referer 是
                  #   `www.sse.com.cn`，⛔ 那不是数据端点，别把它当证据）。
                  #   ⇒ 与 citydata 镜像在**运营方**层面不同 ⇒ 这条能力的
                  #   `independent_fault_domains()` 才真的 ≥2。
                  upstream=Upstream(
                      "sse",
                      "https://query.sse.com.cn/marketdata/tradedata/queryMargin.do",
                      "akshare/stock_feature/stock_margin_sse.py:137 "
                      "stock_margin_detail_sse() 内 requests.get(url, params=…)"),
                  # ⭐⭐ `FINDING-361`：覆盖面**运行时**声明。⛔ 不能只写在 note 里 ——
                  #   实测降级后 `ok=True`/1,994 行而主源 4,424 行，调用方无从察觉。
                  coverage="SH",
                  note="⭐ 上交所**自己披露**（非 tushare 镜像）⇒ 真跨厂商备胎。"
                       "2026-08-10 实测 1,994 行，6 个量与主源相关全 1.000000、"
                       "中位相对差 ≤0.0001%、标的集合完全一致；"
                       "⚠ **仅沪市** ⇒ 降级时深市/北交所缺失（已在 FINDING-360 登记）"),
        # ⭐⭐ 深交所自披露（`FINDING-361`）。⛔⛔ **刻意不与上面两条同 schema** ——
        #   它和上交所那条**互相替不了对方**，实测两条独立理由：
        #     ① 市场不相交：SSE 只含沪（1,994 只）、SZSE 只含深（2,100 只），
        #        ⇒ 拿深市数据去"顶替"沪市请求，是**换了一批标的**还装作降级成功。
        #     ② 列集合**互斥**：SSE 独有 `{rqchl, rzche, trade_date}`、
        #        SZSE 独有 `{rqye, rzrqye}` ⇒ 正是 `FINDING-351` 的假等价形状，
        #        被 `test_every_transform_output_stays_within_its_declared_schema_family`
        #        当场判红（⭐ 我自己的守卫先一步否掉了我自己的方案）。
        #   ⇒ 故给它**独立 schema**：要深市请**显式** schema="margin_detail_sz"（知情选择），
        #     ⛔ 绝不让口径锁把它当沪市的备胎自动换上。
        #   ⚠ 它**只是让深市数据可取**（用户目标"需要什么数据都能立马拉到"），
        #     ⛔ 并**不**声称补上了主源挂掉时的全市场缺口 —— 那个缺口仍在，见 note。
        Candidate("akshare::stock_margin_detail_szse", "margin_detail_sz",
                  arg_map={"trade_date": "date"},
                  transform=_margin_from_szse,
                  upstream=Upstream(
                      "szse",
                      "https://www.szse.cn/api/report/ShowReport",
                      "akshare/stock_feature/stock_margin_szse.py:93 "
                      "stock_margin_detail_szse() 内 requests.get(url, params=…)"),
                  coverage="SZ",
                  note="⭐ 深交所**自己披露** ⇒ 又一家独立厂商。2026-08-10 实测 2,100 行，"
                       "6 个量与主源相关全 1.000000、中位相对差 ≤0.0001%、"
                       "标的集合 2,100/2,100 完全一致；⭐ 且**给得出**沪市那条缺的 "
                       "`rqye`/`rzrqye`。⚠ **仅深市**、⚠ **无 trade_date 列**"
                       "（该表按日出、日期在入参里，⛔ 不回填：那是把入参当数据）"),
    ],
    # ── 财务指标：110 列 ────────────────────────────────────────────
    # ⭐⭐ 全表**第四个**真有跨厂商备胎的能力（`FINDING-372`，2026-08-10 实测）。
    #   ⛔⛔ 而我在 `FINDING-365` 里曾把它判成"单点·候选不等价" —— 那个结论**是错的**，
    #     错因有两层，都在本轮实测暴露：
    #       ① 我只测了**一个**候选（东财系）就下了全称结论，而母库里 `category='财务指标'`
    #          实测有 **16 个** `state=OK` 的接口 ⇒ 与 `FINDING-369` 同一个错误形状。
    #       ② 换到新浪系后首跑仍显示 4.17%/3.92% 不等价 —— 那是**我配错了口径变体**
    #          （`净资产收益率` 未加权 / `每股净资产_调整前`），逐期打表才看出
    #          真正逐期相等的是 `加权净资产收益率` 与 `每股净资产_调整后`。
    #   ⇒ ⭐ 教训：`FINDING-347` 的判据只能证伪"这一对列"，⛔ 不能证伪"这两家"。
    "fina_indicator": [
        Candidate("citydata::fina_indicator", "fina_indicator",
                  upstream=_CITYDATA("fina_indicator"),
                  note="2026-08-10 实测 110 列"),
        # ⭐ 新浪系（与东财系独立的另一家）⇒ citydata 整体挂掉时仍拿得到核心 5 个指标。
        #   ⛔ 刻意声明 `coverage` ——它只覆盖 110 列里的 5 列，⛔ 不是全量替代品。
        Candidate("akshare::stock_financial_analysis_indicator", "fina_indicator",
                  arg_map={"ts_code": "symbol"},
                  value_map={"ts_code": _plain_symbol},
                  transform=_fina_from_sina,
                  # ⚠ 新浪系两个**不同主机**（`money.finance` vs `finance`）在这里
                  #   归同一个 `fault_domain="sina"` —— ⭐ 判据是"谁挂了这条腿就断"，
                  #   ⛔ 不是主机名：同一运营方封 IP/改页面会同时打掉两者。
                  upstream=Upstream(
                      "sina",
                      "https://money.finance.sina.com.cn/corp/go.php/"
                      "vFD_FinancialGuideLine/…（按 stockid/年份拼路径）",
                      "akshare/stock_fundamental/stock_finance_sina.py:228 "
                      "stock_financial_analysis_indicator() 内 requests.get(url)"),
                  coverage="5列子集(roe/毛利率/流动比率/速动比率/bps)",
                  note="⛔ **吞掉 `period`**（该签名只有 `symbol`/`start_year`，"
                       "⇒ 按 `FINDING-352` 守门，带 `period` 的调用会**跳过本候选**）。"
                       "⚠ ⛔ 刻意**不**用 `local_filter` 兑现它：`local_filter` 只在该源返回"
                       "**超集**时才等价（`FINDING-359`），而实测本源按年**逐年发请求**"
                       "（`start_year='1900'` 返 **0 行且无日期列**，非全历史超集）"
                       "⇒ 声称超集就是假的。⇒ 缺就是缺。"
                       "⭐ 新浪系独立厂商。2026-08-10 逐期实测 10 期共同区间"
                       "（20191231..20220331）：`加权净资产收益率`↔`roe` 相关 0.998820/"
                       "中位差 0.07%、`流动比率`/`速动比率` 相关 **1.000000**/0.00%、"
                       "对照量 `每股净资产_调整后`↔`bps` **1.000000**/0.00% ⇒ 4/4 满足冻结判据。"
                       "复现：`scripts/_r38_equiv_fina_sina.py`（EXIT=0）。"
                       "⚠ `销售毛利率` 该源**逐期全 NaN**（10/10）⇒ 名义有列、实际无值；"
                       "⚠ 只 5 列 ⇒ 降级后其余 105 列缺失，已由 coverage 运行时声明"),
    ],
    # ── 交易日历 ────────────────────────────────────────────────────
    # ⭐⭐ 全表**第二个**真有同口径备胎的能力（`FINDING-358`，2026-08-10 实测），
    #   且是**第一个跨两家厂商**的（citydata 商家镜像 vs 新浪）⇒ 一家整体挂掉时仍有腿。
    #   ⛔ 它不是"看着像就合并"的：按 `FINDING-347` 冻结的准入判据逐条测过 ——
    #     · 判据①等价：2015–2025 **11 年 2,674 个交易日，Jaccard = 1.00000000**，
    #       逐年 11/11 全等，差集两侧均为空（日期集合上，"相关≥0.99"的等价物是 Jaccard=1）。
    #     · 判据②中位相对差 ≤1%：日期集合上即"差集为空" ⇒ 实测 0 条。
    #     · 判据③第三方对照量：5 个已知休市日（元旦/春节/劳动节/国庆）**两源都无**，
    #       5 个已知交易日**两源都有** ⇒ 10/10 相符 ⇒ 证明两源确实对齐到同一日历语义，
    #       ⛔ 而不是"两个都错得一样"。复现：`scripts/_r38_equiv_trade_cal.py`
    # ⚠ 两源的**原始形状不同**（4 列 vs 1 列 · `str` vs `datetime.date` · 降序 vs 升序
    #   · 带区间 vs 全历史），故等价性**在 `transform` 之后**成立 ——
    #   ⭐ 这正是 `Candidate.transform` 存在的意义：规范化列/类型，⛔ 不做数值修正。
    "trade_cal": [
        Candidate("citydata::trade_cal", "trade_cal",
                  transform=_cal_from_tushare,
                  upstream=_CITYDATA("trade_cal"),
                  note="2026-08-10 实测 242 行/年；⭐ transform 后与下条 11 年逐日全等"),
        Candidate("akshare::tool_trade_date_hist_sina", "trade_cal",
                  transform=_cal_from_akshare,
                  local_filter={"start_date": _cal_ge, "end_date": _cal_le},
                  upstream=Upstream(
                      "sina",
                      "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt",
                      "akshare/tool/trade_date_hist.py:19 "
                      "tool_trade_date_hist_sina() 内 requests.get(url)"),
                  note="2026-08-10 实测 8,797 天全历史（1990-12-19~2026-12-31）；"
                       "⛔ 签名为空、服务端不接受区间 ⇒ 区间由 local_filter 在**本地**兑现"
                       "（`FINDING-359`：否则带日期的调用会跳过它 ⇒ 主源一挂就没备胎了）"),
        # ⭐ 原始 tushare 四列口径仍以**独立 schema** 保留：要 `exchange`/`pretrade_date`
        #   /`is_open`（含休市日）请显式 `schema="trade_cal_ts"` —— 知情选择。
        #   ⛔ 不把这三列塞进统一契约：akshare 侧给不出 ⇒ 那会是假等价（`FINDING-351`）。
        Candidate("citydata::trade_cal", "trade_cal_ts",
                  upstream=_CITYDATA("trade_cal"),
                  note="原始四列（exchange/cal_date/is_open/pretrade_date，**含休市日**）；"
                       "⛔ 单点，仅此一源能满足 ⇒ 要备胎请用 schema='trade_cal'"),
    ],
}


def capabilities() -> dict[str, int]:
    """有哪些能力、各有几个候选。"""
    return {k: len(v) for k, v in CAPABILITIES.items()}


def get(capability: str, *, schema: str | None = None,
        pace: float = 0.0, **params: Any) -> RouteResult:
    """按**数据种类**取数，逐个候选试到成功。

    Args:
        capability: `CAPABILITIES` 里的能力名。
        schema: 若指定，**只用**该口径的候选 ⇒ ⭐ 这是"我要的字段口径不能变"的开关。
            ⛔ 不指定时，仍**只在同一 schema 内部**降级（第一个成功候选的 schema 锁定后续）。
        pace: 候选之间的间隔秒数。⚠ 对限流源（sina 系）应设 >0（`FINDING-338`）。
        **params: 统一参数名，按候选的 `arg_map` 映射。

    Returns:
        `RouteResult`，含 `used`（实际用了哪个源）与 `attempts`（逐条实测轨迹）。
    """
    from finai.sources import catalog_source

    cands = CAPABILITIES.get(capability)
    if not cands:
        raise KeyError(
            f"未登记的能力 {capability!r}。已登记：{sorted(CAPABILITIES)}"
        )
    if schema:
        cands = [c for c in cands if c.schema == schema]
        if not cands:
            raise KeyError(f"能力 {capability!r} 下没有 schema={schema!r} 的候选")

    # ⛔⛔ `FINDING-386` 实测 B（**正确性**腿，不是可用性腿）：
    #   `get("daily_bar", symbol=…, start_date=…, end="20260810")` —— `end` 是
    #   **efinance 后端原生名**、不是统一 API 名。旧行为实测：
    #     attempts=[('akshare::stock_zh_a_hist','SKIP_UNHONORED:end',0),
    #               ('efinance::stock.get_quote_history','OK',8)]
    #     ⇒ `ok=True / used=efinance`，下发 kwargs 为 beg/end/fqt/stock_codes。
    #   ⇒ **路由选择依赖后端词汇**：同一个笔误在别的能力上会换出另一条腿，
    #     而调用方从 `RouteResult` 上看不出自己传错了 —— 更糟的是它**成功了**，
    #     连 `attempts` 都没人会去看。
    #   ⭐ 根因是缺少**能力级统一参数契约**，⛔ 不是 `_unhonored()` 的逐候选内省失灵。
    #     ⇒ 有了 `CAPABILITY_PARAMS`（`FINDING-389`）才修得动这一条。
    #   ⛔ 这里**抛**而不是留痕跳过：传了不存在的参名是**调用方缺陷**，
    #     不是数据条件；而"成功"这条腿上没有任何位置可以承载这个诊断。
    #   ⚠ 只校验"名字存不存在"，⛔ **不**要求全传 —— `:173/:188/:221` 三处既有契约
    #     只传 `symbol`，把可选区间一刀切成必填是 `FINDING-389` 里判为破坏性、
    #     刻意没做的那个改法。
    known = CAPABILITY_PARAMS.get(capability)
    if known is not None and (unknown := sorted(set(params) - set(known))):
        hints = []
        for u in unknown:
            near = difflib.get_close_matches(u, known, n=1, cutoff=0.5)
            hints.append(f"{u}（是不是想传 {near[0]}？）" if near else u)
        raise TypeError(
            f"能力 {capability!r} 不认识这些统一参数名：{'、'.join(hints)}。"
            f"它接受的是 {list(known)}。"
            "⛔ 未按此名传参不会被静默忽略，也不会被'碰巧接受它的那个后端'兜住 ——"
            "那会让选源依赖后端词汇（FINDING-386）。"
        )

    attempts: list[tuple[str, str, int]] = []
    # ⛔⛔ `FINDING-388` ③：被**考察过**的候选各自的 `note`，用于失败报告。
    #   ⭐ 只收"过了口径锁"的那些 —— 被 schema 锁挡掉的候选压根不是本次的腿，
    #     把它们的 note 混进来会让调用方以为那也是一条试过的路。
    #   ⚠ 与 `attempts` **并列**收集而不是塞进它：全仓约 12 处按三元组解包 `attempts`。
    considered_notes: list[tuple[str, str]] = []
    # ⭐⭐ 口径锁：**第一个被尝试的候选**就锁定 schema，⛔ 之后不再跨 schema 降级。
    #   ⚠ 这里曾有一个**我自己写出来的真缺陷**（`FINDING-343` 实测）：
    #     原实现只在调用方显式传 `schema=` 时才设 `locked`，未传时 `locked=None`
    #     ⇒ 那个 `if locked is not None` 判断**永远为假** ⇒ 跨 schema 静默降级。
    #     实测症状：`get("limit_price")` 的首选 `stk_limit`（4 列·涨跌停**价格**）
    #     返 `EMPTY_OK` 后，它自动给了 `limit_list_d`（19 列·涨跌停**名单**）——
    #     **两种完全不同的数据**，而调用方拿到的 frame 看不出被换过。
    #   ⇒ 这正是本模块声称要防的事，却被我写反了。⛔ 修正为下面这行。
    locked: str | None = schema if schema else cands[0].schema
    for i, c in enumerate(cands):
        if c.schema != locked:
            # ⛔ 跨口径不降级。要另一种口径请**显式**传 `schema=`（知情选择）。
            continue
        considered_notes.append((c.key, c.note))
        # ⛔⛔ `FINDING-352`：先查"调用方传的参数在这个候选身上会不会被静默吞掉"。
        #   带 `**kwargs` 的接口（实测 `efinance.stock.get_quote_history`）对错误参名
        #   **不报错**，只是当没传 ⇒ 28 天的请求返回 35 年全历史，state 还是 `OK`。
        #   ⇒ 这种候选必须**跳过**并留痕，⛔ 绝不能让它冒充成功。
        if (dropped := _unhonored(c, params)):
            attempts.append((c.key, f"SKIP_UNHONORED:{','.join(dropped)}", 0))
            continue
        if i and pace:
            time.sleep(pace)
        kwargs = _build_kwargs(c, params)
        try:
            # ⭐ R43 Phase 2 T1/T2（2026-08-14）：`tdx::`/`ths::` 前缀的候选**不走** catalog
            #   —— `catalog_source.fetch()` 第 329-331 行明写 tdx 需会话初始化、不入该入口；
            #   同花顺涨停池未收编台账索引 ⇒ 两者都走旁路适配器（tdx_daily_bar_adapter /
            #   ths_limit_up_adapter），与 catalog 同形 `FetchResult` 契约。
            #   ⛔ 不动既有任何候选行为 —— 仅 `tdx::`/`ths::` 前缀分流，余 24 候选照走 catalog。
            if c.key.startswith("tdx::"):
                from finai.sources import tdx_daily_bar_adapter
                res: FetchResult = tdx_daily_bar_adapter.fetch(c.key, **kwargs)
            elif c.key.startswith("ths::"):
                from finai.sources import ths_limit_up_adapter
                res: FetchResult = ths_limit_up_adapter.fetch(c.key, **kwargs)
            else:
                res: FetchResult = catalog_source.fetch(c.key, **kwargs)
        except Exception as exc:  # noqa: BLE001 — 一个候选炸掉不该打死整条链
            attempts.append((c.key, f"EXC_FETCH:{type(exc).__name__}", 0))
            continue
        attempts.append((c.key, res.state, res.rows or 0))
        if res.state in _GOT_DATA and res.frame is not None and len(res.frame):
            frame = res.frame
            # ⛔⛔ `FINDING-390`：`transform` 与 `_apply_local` 曾在**保护区外面** ——
            #   上面那个 `try` 在 `fetch()` 返回的一刻就结束了。
            #   实测症状（确定性、不联网）：同 schema 两候选、fetch 都返 1 行，
            #   首选 transform 读不存在的列 ⇒ `get()` 直接抛 `KeyError('missing')`，
            #   第二候选**从未被调用**，调用方连 `attempts` 都拿不到
            #   ⇒ fallback 与可诊断性两项核心契约同时失效。
            #   ⭐ 失败位置在**候选归一化边界**，不是源不可达 —— 故留痕必须**分阶段**，
            #     否则事后无法区分"源挂了"与"我的归一化器写错了"。
            #   ⛔ 修法不是"吞掉异常返回空表"：那会与下面的
            #     `EMPTY_AFTER_LOCAL_FILTER`（真实 0 行）混为一谈，
            #     把**代码缺陷**读成**市场事实**。
            if c.transform is not None:
                try:
                    frame = c.transform(frame)
                except Exception as exc:  # noqa: BLE001
                    attempts[-1] = (
                        c.key, f"{res.state}->EXC_TRANSFORM:{type(exc).__name__}", 0)
                    continue
            # ⭐ `FINDING-359`：客户端过滤在 `transform` **之后**执行（按统一契约列名），
            #   且**必须**执行 —— `_unhonored()` 已凭它放行了该参数。
            try:
                frame = _apply_local(c, frame, params)
            except Exception as exc:  # noqa: BLE001
                attempts[-1] = (
                    c.key, f"{res.state}->EXC_LOCAL_FILTER:{type(exc).__name__}", 0)
                continue
            if not len(frame):
                # ⚠ 过滤后为空 ⇒ 与 `EMPTY_OK` 同性质：0 行不算成功（`FINDING-178`），
                #   继续试下一个候选，⛔ 不返回空表冒充成功。
                attempts[-1] = (c.key, f"{res.state}->EMPTY_AFTER_LOCAL_FILTER", 0)
                continue
            # ⭐ `FINDING-361`：把**实际启用的**候选的覆盖面带回给调用方 ——
            #   ⛔ 不是把能力表里第一个候选的覆盖面带回去（那样降级就看不出来了）。
            return RouteResult(True, frame, c.key, c.schema, len(frame), attempts,
                               coverage=c.coverage)
        # ⚠ `EMPTY_OK` 不判定源失效，只继续试下一个（0 行可能才是正确答案）。
    # ⭐ `FINDING-386` 实测 A：把"全因参数不生效而跳过"与"源真的没数据"分开。
    # ⛔ `attempts` 必须先判非空 —— `all([])` 是 `True`，直接写 `all(...)` 会把
    #   **零次尝试**（schema 锁挡掉全部候选）也标成参数问题。
    all_skipped = bool(attempts) and all(
        s.startswith("SKIP_UNHONORED") for _, s, _ in attempts)
    # ⭐ `FINDING-388` ③：note 只随**失败**返回 —— 成功路径的形状刻意不动
    #   （判据明写"成功返回形状不变"，且成功时把已知限制混进来会被读成
    #    "本次数据有问题"；覆盖面缺口由 `coverage` 单独承载）。
    # ⭐⭐ `FINDING-435` 关闭条件②/`FINDING-444`：失败时把"这个锁下**本来**
    #   有几个域、此刻有几个走得到"一并交给调用方 —— ⛔ 否则"两条腿都试过了"
    #   与"第二条腿从来就走不到"在 `RouteResult` 上**长得一样**，
    #   而这两种处境的修法完全不同（找运维 / 改注册表）。
    locked_cands = [c for c in cands if c.schema == locked]
    return RouteResult(False, None, None, locked, 0, attempts,
                       all_skipped_unhonored=all_skipped,
                       notes=considered_notes,
                       availability=availability(locked_cands, params))
