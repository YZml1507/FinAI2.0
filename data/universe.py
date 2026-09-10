#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T108 股票池 / 成分回放（FR-DATA-5，spec.md:74；tasks.md:27；依赖 T104 数据字典 v1 §3）。

两件事，对应数据字典 v1 §3 的"方式 A / 方式 B"
（research-finai specs/001-a-stock-longonly-daily-quant/data_dictionary_v1.md:191-197）：

方式 A —— 历史存活池（可回放，纯函数）：
  用 baostock ``query_stock_basic`` 的 ``ipoDate/outDate/type/status`` 推算
  任意历史时点 ``as_of`` 的可交易 A 股池（``alive_universe``）：
    * ``ipoDate <= as_of`` —— 剔除当时未上市（新股前瞻）；
    * ``outDate`` 为空或 ``as_of < outDate`` —— 剔除已退市
      （字典 §3.2:203 "退市日前的数据照常可用" ⇒ 退市日当天起不再可用）；
    * ``type == '1'`` —— 仅股票（2=指数/3=其它/4=可转债/5=ETF 不入池）；
    * ⛔ **不看 ``status`` 列**：它是"当前"上市状态快照，历史回放用它会把
      "后来才退市"的股票在历史时点提前剔除 —— 教科书级幸存者偏差（未来函数）。

方式 B —— 指数成分回放（有缺口，闸门把守）：
  ``query_hs300_stocks/query_zz500_stocks/query_sz50_stocks`` 有 ``date`` 参数，
  但**历史回放未实证**（字典 §3.1:187-189 探针仅测当前快照）。
  ⛔ 缺口不许静默（T108 要求 2）：历史日请求默认抛 ``IndexReplayUnverifiedError``，
  调用方显式 ``allow_unverified=True`` 才放行（血缘标 ``replay_verified=False``）；
  CSI1000 无接口（字典 §3.1:190）⇒ 恒抛 ``IndexNotReplayableError``。

复用母库原语（⛔ 不重造轮子；finai/ 只读，本模块不改母库）：
  * ``finai/sources/baostock_source.py`` 的 ``_drain``（FINDING-185：游标只在
    ``get_row_data()`` 时推进，不消费即死循环）与 ``run_with_timeout``
    （FINDING-181：baostock 可挂死且 socket 超时约束不住，须外部超时）。
  * 本模块的 ``_query_*_once`` 只做"登录 → 调接口 → _drain → 登出"的薄壳。

离线可测：``alive_universe`` 与回放闸门是纯逻辑，不触网；单测全离线。
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

import pandas as pd

from finai.sources.baostock_source import DEFAULT_TIMEOUT, _drain, run_with_timeout

__all__ = [
    "UniverseError", "IndexReplayUnverifiedError", "IndexNotReplayableError",
    "UniverseSnapshot", "IndexSnapshot",
    "UNVERIFIED_INDEX_APIS", "NO_REPLAY_INDICES",
    "canon_date", "alive_universe", "load_stock_basic", "compute_alive_universe",
    "index_constituents",
]


# ---------------------------------------------------------------- 异常族
# 与母库 UnknownAdjustment（adjustment_mode.py:49）/ BarTruncationError 同套路：
# 均为 ValueError 子类，⛔ 绝不静默返回可能错误的数据。

class UniverseError(ValueError):
    """T108 股票池/成分回放域的错误基类。"""


class IndexReplayUnverifiedError(UniverseError):
    """指数有接口但**历史回放未实证**（数据字典 v1 §3.1:187-189）。

    ⛔ 不许把未实证的历史成分当成真实回放结果静默返回 ——
    那等于给回测喂"可能是当前快照"的成分（幸存者偏差/未来函数）。
    确要使用：显式传 ``allow_unverified=True``，血缘将标 ``replay_verified=False``。
    """


class IndexNotReplayableError(UniverseError):
    """指数**无回放接口**（如 CSI1000，数据字典 v1 §3.1:190）或指数名未登记。

    ⛔ 抛清晰异常而非静默返回错数据（T108 要求 2）。
    """


# ---------------------------------------------------------------- 指数登记

#: 有 baostock 接口、但**历史回放未实证**的指数（字典 §3.1:187-189）。
#: 值 = baostock 接口名。⚠ ``sz50`` 在 baostock 命名里是**上证 50**（不是深证 50）。
#: ⛔ 本表是全模块唯一登记指数接口的地方（对齐 adjustment_mode 的"唯一写死点"纪律）。
UNVERIFIED_INDEX_APIS: dict[str, str] = {
    "hs300": "query_hs300_stocks",
    "zz500": "query_zz500_stocks",
    "sz50": "query_sz50_stocks",
}

#: 无回放接口的指数（字典 §3.1:190）：恒抛 ``IndexNotReplayableError``，
#: ``allow_unverified=True`` 也不放行（没有接口可以"放行"）。
NO_REPLAY_INDICES: dict[str, str] = {
    "csi1000": ("CSI1000（中证1000）：baostock 无对应接口，不可回放"
                "（数据字典 v1 §3.1:190）；需要时在下一版数据字典登记新源"),
    "zz1000": "同 csi1000：中证1000 无回放接口（数据字典 v1 §3.1:190）",
}


# ---------------------------------------------------------------- 日期工具

def canon_date(value: str) -> str:
    """``'YYYY-MM-DD'`` 或 ``'YYYYMMDD'`` → ``'YYYY-MM-DD'``；畸形即抛 ``ValueError``。

    ISO 格式字符串可直接字典序比较，故回放比较全部用规范化后的字符串。
    """
    s = str(value).strip()
    if len(s) == 8 and s.isdigit():
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    try:
        _dt.date.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"非法日期 {value!r}（应为 YYYY-MM-DD 或 YYYYMMDD）") from exc
    return s


def _missing(value: Any) -> bool:
    """空串/None/NaN/'nan' 一律视为缺失（``_drain`` 出来是字符串，手工帧可能带 NaN）。"""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    s = str(value).strip()
    return s == "" or s.lower() == "nan"


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 快照容器

@dataclass(frozen=True)
class UniverseSnapshot:
    """某一历史时点的可交易 A 股池（方式 A 的产物）。"""

    as_of: str
    codes: tuple[str, ...]
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __contains__(self, code: object) -> bool:
        return code in self.codes

    def __len__(self) -> int:
        return len(self.codes)

    def __iter__(self) -> Iterator[str]:
        return iter(self.codes)

    def to_frame(self) -> pd.DataFrame:
        """落盘/下游用的长表（``code`` + ``as_of``）；血缘见 ``meta``。"""
        return pd.DataFrame({"code": list(self.codes), "as_of": self.as_of})


@dataclass(frozen=True)
class IndexSnapshot:
    """某一时点的指数成分（方式 B 的产物）。``as_of=None`` 表示当前快照。"""

    index: str
    as_of: str | None
    codes: tuple[str, ...]
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __contains__(self, code: object) -> bool:
        return code in self.codes

    def __len__(self) -> int:
        return len(self.codes)

    def __iter__(self) -> Iterator[str]:
        return iter(self.codes)

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame({"code": list(self.codes), "index": self.index,
                             "as_of": self.as_of})


# ---------------------------------------------------------------- 方式 A：存活池

#: baostock query_stock_basic 的 type 取值里只有 '1' 是股票（2=指数/3=其它/4=可转债/5=ETF）。
_STOCK_TYPE = "1"

#: 回放必需的列（缺一即 raise，⛔ 不静默降级）。
_REQUIRED_COLS = ("code", "ipoDate", "outDate", "type", "status")


def alive_universe(stock_basic: pd.DataFrame, as_of: str) -> UniverseSnapshot:
    """纯函数：由 ``query_stock_basic`` 表推算 ``as_of`` 当日的可交易 A 股池。⛔ 离线可算。

    前瞻防线（T108 要求 3）：
      * 剔除 ``ipoDate > as_of``（当时未上市）；
      * 剔除 ``outDate <= as_of``（当时已退市；退市日当天起不可用，字典 §3.2:203）；
      * ⛔ ``status`` 列**不参与**判定：它是当前快照，用了即未来函数
        （"2018 年才退市"的股票在 2015 年必须仍在池内）。

    参数 ``stock_basic`` 需含列 ``code/ipoDate/outDate/type/status``
    （``load_stock_basic()`` 的返回形态；``ipoDate`` 缺失的行保守剔除并计数）。
    """
    day = canon_date(as_of)
    missing_cols = [c for c in _REQUIRED_COLS if c not in stock_basic.columns]
    if missing_cols:
        raise ValueError(
            f"stock_basic 缺必需列 {missing_cols}（T108 回放需要全部 {_REQUIRED_COLS}）")

    total = len(stock_basic)
    stocks = stock_basic[stock_basic["type"].astype(str).str.strip() == _STOCK_TYPE]
    n_non_stock = total - len(stocks)

    ipo = stocks["ipoDate"].map(lambda v: None if _missing(v) else canon_date(v))
    known_idx = ipo[ipo.notna()].index
    n_unknown_ipo = len(stocks) - len(known_idx)

    # 第一道前瞻防线：上市日 <= as_of（上市首日算已上市，字典 §3.2:205）
    listed_mask = ipo.loc[known_idx] <= day
    n_not_yet = int((~listed_mask).sum())
    candidates = stocks.loc[known_idx][listed_mask]

    # 第二道前瞻防线：退市日 > as_of（退市日当天起不可用，字典 §3.2:203）
    # ⚠ pandas Arrow/新版本会把 map 出来的 None 变成 float nan：`v is not None`
    #   对 nan 为 True，随后 `nan <= day` 在 CI 上炸 TypeError。统一走 `_missing`。
    out = candidates["outDate"].map(lambda v: None if _missing(v) else canon_date(v))
    delisted_mask = out.map(lambda v: (not _missing(v)) and (str(v) <= day))
    n_delisted = int(delisted_mask.fillna(False).sum())
    alive = candidates[~delisted_mask.fillna(False)]

    codes = tuple(sorted(alive["code"].astype(str).str.strip()))
    meta: dict[str, Any] = {
        "as_of": day,
        "source": "baostock.query_stock_basic",
        "total_rows": total,
        "excluded_non_stock": n_non_stock,
        "excluded_not_yet_listed": n_not_yet,
        "excluded_delisted": n_delisted,
        "excluded_unknown_ipo": n_unknown_ipo,
        "alive": len(codes),
        "rule": ("ipoDate <= as_of < outDate（outDate 空=未退市；退市日当天起剔除）；"
                 "⛔ status 列未参与判定（当前快照，防幸存者偏差/未来函数）"),
    }
    return UniverseSnapshot(as_of=day, codes=codes, meta=meta)


# ---------------------------------------------------------------- baostock 薄壳

def _login(bs: Any) -> None:
    """登录并检查错误码。⛔ 登录失败不许静默继续（返回的 ResultData 带 error_code）。"""
    rs = bs.login()
    if getattr(rs, "error_code", "0") != "0":
        raise RuntimeError(
            f"baostock login failed: error_code={rs.error_code} "
            f"msg={getattr(rs, 'error_msg', '?')}")


def _logout(bs: Any) -> None:
    try:
        bs.logout()
    except Exception:  # noqa: BLE001, S110 —— 登出失败不掩盖查询结果/原因
        pass


def _query_stock_basic_once() -> pd.DataFrame:
    """登录 → query_stock_basic（全量）→ ``_drain``（FINDING-185）→ 登出。一次一会话。"""
    import baostock as bs
    _login(bs)
    try:
        return _drain(bs.query_stock_basic())
    finally:
        _logout(bs)


def _query_index_once(key: str, day: str | None) -> pd.DataFrame:
    """登录 → 指数成分接口（``date`` 入参；空串=最新）→ ``_drain`` → 登出。"""
    import baostock as bs
    api = UNVERIFIED_INDEX_APIS[key]
    _login(bs)
    try:
        return _drain(getattr(bs, api)(date=day or ""))
    finally:
        _logout(bs)


def load_stock_basic(timeout: int = DEFAULT_TIMEOUT) -> pd.DataFrame:
    """取全量股票基础表（字典 §3.1:186 探针实测 8,878 行，随时间只增）。

    挂死保护：``run_with_timeout``（FINDING-181；母库唯一有效实现，⛔ 不许另写）。
    """
    return run_with_timeout(_query_stock_basic_once, timeout)


def compute_alive_universe(as_of: str, timeout: int = DEFAULT_TIMEOUT) -> UniverseSnapshot:
    """在线一键：拉 ``stock_basic`` → 纯函数 ``alive_universe`` 推算。血缘补 ``fetch_time``。"""
    frame = load_stock_basic(timeout=timeout)
    snap = alive_universe(frame, as_of)
    meta = dict(snap.meta)
    meta["fetch_time"] = _utcnow_iso()
    return UniverseSnapshot(as_of=snap.as_of, codes=snap.codes, meta=meta)


# ---------------------------------------------------------------- 方式 B：指数成分

def index_constituents(index: str, as_of: str | None = None, *,
                       allow_unverified: bool = False,
                       timeout: int = DEFAULT_TIMEOUT) -> IndexSnapshot:
    """指数成分查询（方式 B）。⛔ 缺口闸门纪律（T108 要求 2/3）：

    * ``as_of=None`` ⇒ **当前快照**（探针实证过，字典 §3.1:187-189）→ 直接放行；
    * ``as_of=历史日`` ⇒ 历史回放**未实证**（``date`` 参数存在但探针仅测当前）⇒
      默认抛 ``IndexReplayUnverifiedError``（⛔ 静默返回的可能是当前快照 = 未来函数）；
      显式 ``allow_unverified=True`` 才放行，血缘标 ``replay_verified=False`` + ``gap``；
    * CSI1000/中证1000 ⇒ **无接口**（字典 §3.1:190）⇒ 恒抛 ``IndexNotReplayableError``，
      ``allow_unverified`` 也不放行；未登记的指数名同样抛（⛔ 不静默猜测）。
    """
    key = str(index).strip().lower()
    if key in NO_REPLAY_INDICES:
        raise IndexNotReplayableError(NO_REPLAY_INDICES[key])
    if key not in UNVERIFIED_INDEX_APIS:
        raise IndexNotReplayableError(
            f"未知指数 {index!r}：无已登记的成分接口。已登记（回放未实证）："
            f"{sorted(UNVERIFIED_INDEX_APIS)}；确认不可回放：{sorted(NO_REPLAY_INDICES)}。"
            f"⛔ 不静默猜测，先在数据字典登记再接入")

    day = None if as_of is None else canon_date(as_of)
    verified = day is None   # ⭐ 仅"当前快照"是探针实证过的路径（字典 §3.1:187-189）
    if not verified and not allow_unverified:
        raise IndexReplayUnverifiedError(
            f"{key} 历史成分回放未实证（数据字典 v1 §3.1：{UNVERIFIED_INDEX_APIS[key]} "
            f"的 date 参数存在，但探针仅测当前快照，不保证返回历史真实成分）。"
            f"⛔ 拒绝静默返回可能是当前快照的数据；如确需，显式传 "
            f"allow_unverified=True（血缘将标 replay_verified=False）")

    frame = run_with_timeout(lambda: _query_index_once(key, day), timeout)
    if "code" not in frame.columns:
        raise RuntimeError(
            f"baostock {UNVERIFIED_INDEX_APIS[key]} 返回缺 'code' 列：{list(frame.columns)}")

    codes = tuple(sorted(set(frame["code"].astype(str).str.strip())))
    meta: dict[str, Any] = {
        "index": key,
        "as_of": day,
        "source": f"baostock.{UNVERIFIED_INDEX_APIS[key]}",
        "replay_verified": verified,
        "rows": len(frame),
        "fetch_time": _utcnow_iso(),
    }
    if not verified:
        meta["gap"] = "历史成分回放未实证（数据字典 v1 §3.1）；结果可能是当前快照，用者自担"
    return IndexSnapshot(index=key, as_of=day, codes=codes, meta=meta)
