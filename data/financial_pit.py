#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T107 财务 pubDate 对齐管道（FR-DATA-4，spec.md:73；tasks.md:26；数据字典 v1 §2）。

做什么：把 baostock 的 6 张季频财务表按**公告日（pubDate）**对齐到任意信号时点
—— 信号日 t 只能用 ``pubDate <= t`` 的最新财报（PIT 零前视，验收 FR-DATA-4）。

⛔⛔ 最贵红线（违反即返工）：**对齐主键只能是 pubDate，永远不是 statDate**。
``statDate`` 是报告期标签：年报常在次年 4 月才公告，而 ``statDate`` 写着前一年
12-31 —— 按报告期对齐 = 回测里用到了当时根本看不到的数字 = 教科书级未来函数。
故 ``pit_align(..., key="statDate")`` 直接抛 ``StatDateAlignmentError`` 拒绝执行。

6 张表（数据字典 v1 §2.1，2007-Q1 起，均含 ``pubDate``/``statDate``）：
  ``profit_data``    ``query_profit_data``    （roeAvg/npMargin/gpMargin…）
  ``balance_data``   ``query_balance_data``   （currentRatio/quickRatio…）
  ``cash_flow_data`` ``query_cash_flow_data`` 现金流量
  ``growth_data``    ``query_growth_data``    成长能力
  ``dupont_data``    ``query_dupont_data``    杜邦指标
  ``operation_data`` ``query_operation_data`` 经营能力
``FINANCIAL_TABLES`` 是全模块**唯一**登记财务接口的地方（"唯一写死点"纪律，
对齐 adjustment_mode / universe 的指数登记表）。

PIT 对齐规则（字典 §2.1 原文）：``available = df[df.pubDate <= t]
.sort_values("pubDate").drop_duplicates("code", keep="last")``
⛔ 空结果 = 该时点真没有可用财报（EMPTY_OK 语义）：返回空帧而不是报错，
且**绝不**用未来行/上一季值补齐 —— 不制造从不存在的数字。

复用母库原语（⛔ 不重造轮子；finai/ 只读，本模块不改母库）：
  * ``finai/sources/baostock_source.py`` 的 ``_drain``（FINDING-185：游标只在
    ``get_row_data()`` 时推进，不消费即死循环）与 ``run_with_timeout``
    （FINDING-181：baostock 可挂死且 socket 超时约束不住，须外部超时）
    以及 ``DEFAULT_TIMEOUT``。
  * ``data/universe.py`` 的 ``canon_date``/``_missing``/``_login``/``_logout``
    —— 同一套日期规范化与登录错误码检查，⛔ 两份实现必然漂移，只用一份。
  * 本模块的 ``_query_financial_once`` 只做 "登录 → 调接口 → _drain → 登出" 薄壳，
    与 ``universe._query_index_once`` 同形；``import baostock`` 全部**懒加载**
    且未知表名先于 import/登录判定（⛔ fail-closed 离线可判，不连网才发现错）。

离线可测性（⛔ 红线"能离线就不联网"）：
  · ``pit_align`` 是纯函数，不触网；单测全离线。
  · 在线薄壳经 ``query_fn`` 注入（collect_financials）或假 ``baostock`` 模块
    （``sys.modules`` 打桩）替换 —— 全程零网络、零真实登录。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping

import pandas as pd

from data.universe import _login, _logout, _missing, canon_date
from finai.sources.baostock_source import DEFAULT_TIMEOUT, _drain, run_with_timeout
from finai.sources.base import FetchResult, make_result

__all__ = [
    "FinancialPitError", "UnknownFinancialTableError", "StatDateAlignmentError",
    "FINANCIAL_TABLES",
    "PitFrame",
    "pit_align", "collect_financials", "fetch_financial_table",
]


# ---------------------------------------------------------------- 异常族
# 与 UniverseError（universe.py:57）同套路：ValueError 子类，
# ⛔ 绝不静默返回可能错误的数据。

class FinancialPitError(ValueError):
    """T107 财务 pubDate 对齐管道的错误基类。"""


class UnknownFinancialTableError(FinancialPitError):
    """未登记的财务表名。⛔ fail-closed：不猜测、不静默跳过 —— 先在数据字典登记。"""


class StatDateAlignmentError(FinancialPitError):
    """⛔ 禁止按报告期 ``statDate`` 对齐 —— 未来函数（数据字典 v1 §2.1）。

    年报可在次年 4 月才公告，而 ``statDate`` 写前一年 12-31；按它对齐等于把
    当年 1-3 月的信号提前喂给了年报数字。PIT 主键只能是 ``pubDate``。
    """


# ---------------------------------------------------------------- 表登记
#: ⛔ 全模块唯一登记财务接口的地方（"唯一写死点"纪律，对齐 adjustment_mode）。
#: 键 = baostock 接口名去掉 ``query_`` 前缀。键与接口一一对应，新增按机械流程。
FINANCIAL_TABLES: dict[str, str] = {
    "profit_data": "query_profit_data",
    "balance_data": "query_balance_data",
    "cash_flow_data": "query_cash_flow_data",
    "growth_data": "query_growth_data",
    "dupont_data": "query_dupont_data",
    "operation_data": "query_operation_data",
}


def _api_name(table: str) -> str:
    """表名 → baostock 接口名；未登记即抛（⛔ fail-closed，不静默猜测）。"""
    key = str(table).strip()
    api = FINANCIAL_TABLES.get(key)
    if api is None:
        raise UnknownFinancialTableError(
            f"未知财务表 {table!r}：已登记 {sorted(FINANCIAL_TABLES)}"
            f"（数据字典 v1 §2.1）。⛔ 先登记再接入，不静默猜测")
    return api


# ---------------------------------------------------------------- baostock 薄壳

def _query_financial_once(table: str, code: str, year: int, quarter: int) -> pd.DataFrame:
    """登录 → 对应财务接口（``code/year/quarter`` 入参）→ ``_drain`` → 登出。

    一次一会话（与 ``universe._query_index_once`` 同形）。⛔ ``_api_name`` **先于**
    import/登录判定 —— 未知表名离线即抛，不连网才发现错。
    财务表未在 ``baostock_source.fetch()`` 接线的根源：母库 fetch 的 kinds 只有
    kline/adjust_factor/dividend/all_stock/trade_dates（FINDING 台账范围）——
    这正是 T107 需要自己的薄壳的原因。
    """
    api = _api_name(table)                       # ⛔ fail-closed 先于一切
    import baostock as bs                        # noqa: PLC0415 - 懒导入：离线 import/单测不依赖网络包

    _login(bs)
    try:
        return _drain(getattr(bs, api)(code=code, year=year, quarter=quarter))
    finally:
        _logout(bs)


def fetch_financial_table(table: str, code: str, year: int, quarter: int, *,
                          timeout: int = DEFAULT_TIMEOUT) -> pd.DataFrame:
    """单股单期财务表取数（在线薄壳的公开入口）。

    挂死保护：``run_with_timeout``（FINDING-181，母库**唯一**有效实现，⛔ 不许另写）。
    未知表名 / 登录失败 / 接口 error_code ≠ 0 一律抛，⛔ 绝不静默返回。
    """
    return run_with_timeout(
        lambda: _query_financial_once(table, code, year, quarter), timeout)


# ---------------------------------------------------------------- PIT 快照容器

@dataclass(frozen=True)
class PitFrame:
    """信号时点 ``signal_date`` 对齐后的财报快照（PIT 规则产物）。

    ``frame`` 每 code 至多一行 = 该 code 在 ``pubDate <= signal_date`` 内最新一季；
    ``statDate`` 及其派生 ``stat_year``/``stat_quarter`` 只作**元数据列**标注
    数字出自哪个报告期 —— 永远不参与对齐（⛔ 见 ``StatDateAlignmentError``）。
    空帧 = 该时点无可用财报（EMPTY_OK 语义），血缘计数见 ``meta``。
    """

    signal_date: str
    table: str
    frame: pd.DataFrame
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.frame)

    def __contains__(self, code: object) -> bool:
        return code in set(self.frame["code"])

    def __iter__(self) -> Iterator[str]:
        return iter(self.frame["code"])

    def to_frame(self) -> pd.DataFrame:
        """下游落盘/消费用的帧（拷贝 —— ⛔ 不给外部改内部状态的入口）。"""
        return self.frame.copy()


# ---------------------------------------------------------------- 纯函数 PIT 对齐

#: 对齐必需列（缺一即 raise，⛔ 不静默降级）。
_PIT_REQUIRED_COLS = ("code", "pubDate")

#: PIT 对齐唯一允许的键（⛔ 只有 pubDate；见 ``StatDateAlignmentError``）。
_ALIGN_KEY = "pubDate"


def _norm_pit_date(value: Any) -> str | None:
    """缺失/畸形日期 → None（调用方保守剔除并计数）；合法 → ISO ``YYYY-MM-DD``。"""
    if _missing(value):
        return None
    try:
        return canon_date(value)
    except ValueError:
        return None


def _safe_canon_date(value: Any) -> str | None:
    """元数据日期（statDate）尽量规范化；畸形保留为原文由消费方自查。"""
    if _missing(value):
        return None
    try:
        return canon_date(value)
    except ValueError:
        return str(value).strip()


def _year_quarter(iso_or_none: str | None) -> tuple[Any, Any]:
    """ISO ``YYYY-MM-DD`` 报告期 → ``(年份, 季度)``；无法解析 → ``(None, None)``。

    ⚠ 仅作元数据派生：季度的语法是 ``(月 - 1) // 3 + 1``，与财报期数一一对应，
    且**绝不参与** PIT 过滤（对齐只看 pubDate）。
    """
    d = iso_or_none
    if d is None or len(d) < 10:
        return None, None
    try:
        y = int(d[:4])
        m = int(d[5:7])
    except ValueError:
        return None, None
    return y, (m - 1) // 3 + 1


def pit_align(fin_df: pd.DataFrame, signal_date: str, *,
              key: str = "pubDate",
              table: str | None = None) -> PitFrame:
    """纯函数 PIT 对齐（FR-DATA-4，⛔ 离线可算）：取 ``pubDate <= 信号日`` 的最新财报。

    数据字典 v1 §2.1 规则原文逐字落地：
      ``available = df[df.pubDate <= t].sort_values("pubDate").drop_duplicates("code", keep="last")``
      · 同 code 多期 ⇒ 最新 ``pubDate`` 胜出（日期先规范化为 ISO，字典序即日期序）；
      · ``pubDate == 信号日`` **可用**，次日才公告的**不可用** —— 零前视在等号边界钉死；
      · 过滤后为空 ⇒ 空帧（EMPTY_OK 语义，⛔ 不报错、不造值、不用未来/前一行补）；
      · ``pubDate`` 缺失/畸形的行**保守剔除并计数**（宁缺勿错，⛔ 绝不为对齐编造日期）；
      · 同 (code, pubDate) 重复行保留后出现的（更正/重述场景，幂等基调）。

    ⛔ ``key`` 只接受 ``"pubDate"``；传 ``"statDate"``（或任何其它值）即抛
    ``StatDateAlignmentError`` —— 按报告期对齐是未来函数（数据字典 v1 §2.1:139 原文）。
    """
    if key != _ALIGN_KEY:
        raise StatDateAlignmentError(
            f"key={key!r}：⛔ PIT 对齐主键只能是 'pubDate'（数据字典 v1 §2.1）。"
            f"statDate 是报告期标签 —— 年报可在次年 4 月才公告而 statDate 写前一年 "
            f"12-31，按它对齐 = 未来函数（FR-DATA-4 零前视）")
    day = canon_date(signal_date)
    missing_cols = [c for c in _PIT_REQUIRED_COLS if c not in fin_df.columns]
    if missing_cols:
        raise ValueError(
            f"fin_df 缺必需列 {missing_cols}（PIT 对齐需要全部 {_PIT_REQUIRED_COLS}）")

    frame = fin_df.copy()
    frame["pubDate"] = frame["pubDate"].map(_norm_pit_date)
    n_invalid_pubdate = int(frame["pubDate"].isna().sum())
    frame = frame[frame["pubDate"].notna()].copy()

    codes = frame["code"].map(lambda v: (None if _missing(v) else str(v).strip()))
    frame["code"] = codes
    n_bad_code = int(codes.isna().sum())
    frame = frame[codes.notna()].copy()

    # 报告期元数据列：输入带 statDate 才透传 + 派生（⛔ 不参与上面任何过滤）。
    if "statDate" in frame.columns:
        frame["statDate"] = frame["statDate"].map(_safe_canon_date)
        seasons = frame["statDate"].map(_year_quarter)
        frame["stat_year"] = seasons.map(lambda t: t[0])
        frame["stat_quarter"] = seasons.map(lambda t: t[1])

    # 字典 §2.1 规则逐字落地；等号边界：pubDate <= t 含当日公告（零前视证明）。
    available = frame[frame["pubDate"] <= day]
    latest = (available.sort_values(["pubDate", "code"])
                       .drop_duplicates("code", keep="last")
                       .reset_index(drop=True))
    meta: dict[str, Any] = {
        "table": table or "",
        "key": key,
        "signal_date": day,
        "rule": ("pubDate <= signal_date（含等号当天）；每 code 取 pubDate 最新一季；"
                 "⛔ statDate 仅元数据列，不参与对齐"),
        "n_rows_in": len(fin_df),
        "n_rows_available": len(available),
        "excluded_future": int((frame["pubDate"] > day).sum()),
        "excluded_invalid_pubdate": n_invalid_pubdate,
        "excluded_invalid_code": n_bad_code,
        "latest": len(latest),
    }
    return PitFrame(signal_date=day, table=table or "", frame=latest, meta=meta)


# ---------------------------------------------------------------- 批量采集

#: 采集查询器签名：(table, code, year, quarter) → DataFrame。测试注入桩替换。
QueryFn = Callable[[str, str, int, int], pd.DataFrame]


def collect_financials(
    codes: Iterable[str],
    years: Iterable[int],
    quarters: Iterable[int],
    table: str,
    *,
    query_fn: QueryFn | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> FetchResult:
    """批量串行采集 ``table`` 财务表（serial：限速友好，⛔ 不并发；可把限速器
    包进 ``query_fn`` 后注入，见 ``data/collector.RateLimiter``）。

    流程：未知表名 **fail-closed 先抛**（⛔ 不浪费任何一轮调用）→ 循环
    (code, year, quarter) 串行调用 ``query_fn``（默认 = 在线薄壳
    ``fetch_financial_table``；测试用桩注入）→ 拼接 → 按 ``(code, pubDate)``
    去重**保末次**（同键重复行 = 更晚被取到的行胜出，幂等基调，FR-DATA-6 呼应）。

    返回 ``base.FetchResult``（⛔ 七态契约）：有行 = ``OK``；0 行 = ``EMPTY_OK``
    （合法空结果，**绝不**读作"无数据"—— base.py make_result 原语，⛔ 不手写状态）。
    血缘 meta 携带 ``table``/``n_calls``/``n_rows_in``/``n_unique``。
    """
    api = _api_name(table)                       # ⛔ fail-closed 先于一切（含注入桩）
    codes = tuple(codes)
    years = tuple(years)
    quarters = tuple(quarters)
    if query_fn is None:
        def _default_query(t: str, c: str, y: int, q: int) -> pd.DataFrame:
            return fetch_financial_table(t, c, y, q, timeout=timeout)
        query_fn = _default_query

    frames: list[pd.DataFrame] = []
    n_calls = 0
    for code in codes:
        for year in years:
            for quarter in quarters:
                frame = query_fn(table, code, int(year), int(quarter))
                n_calls += 1
                if frame is None or frame.empty:
                    continue                     # ⛔ None/空帧 = 该调用无输出，不算成功行
                frames.append(frame)

    combined = (pd.concat(frames, ignore_index=True) if frames
                else pd.DataFrame())
    n_rows_in = len(combined)
    if not combined.empty:
        if not {"code", "pubDate"} <= set(combined.columns):
            raise FinancialPitError(
                f"采集拼接帧缺 (code, pubDate) 列：{list(combined.columns)}。"
                f"⛔ 表 {table!r} 查询器违反数据字典 v1 §2.1 公共字段契约，拒绝静默去重")
        combined = (combined.sort_values(["code", "pubDate"])
                            .drop_duplicates(["code", "pubDate"], keep="last")
                            .reset_index(drop=True))

    res = make_result(combined, source=f"baostock.{api}")
    res.meta.update({
        # ⭐ source 进 meta（血缘）：七态契约的 `meta` 是开放血缘字典，调用方
        #    与测试都按 res.meta["source"] 消费；res.source 顶层字段同源，⛔ 不漂移。
        "source": res.source,
        "table": str(table),
        "n_calls": n_calls,
        "n_rows_in": n_rows_in,
        "n_unique": len(combined),
    })
    return res