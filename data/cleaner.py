#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T106 清洗器 —— 停牌 / 涨跌停 / 除权标记清洗入库（FR-DATA-2，spec.md:71；tasks.md:25）。

三件事，对应 FR-DATA-2 / P-1.3（命中的验收判据是**停牌脏行过滤 100% 抽查**）：

  ① **停牌强制过滤（R1 清洗侧独立落地）**：``enforce_tradestatus`` 只保留
     ``tradestatus=='1'`` 的交易日行，⛔ 绝不前向填充。母库 ``baostock_source``
     的 ``_drop_suspended`` 已在**取数出口**滤过一遍（R1）；本函数是**入库前**
     的再校验纯函数 —— 缺 ``tradestatus`` 列的日线帧**直接 raise**（fail-closed，
     镜像母库 ``_validate_kline_params`` 的日线字段强制规则③），绝不静默放行。
     ``assert_no_prevfill_suspicious`` 检测"前收平推签名"（OHLC==前收 且
     volume==0，12 号 §9-A 实测 100% 命中），供验收按日抽样核对。

  ② **涨跌停标记（FR-BT-2 的派生层）**：``mark_limit_flags`` 纯函数加 bool 列
     ``limit_up`` / ``limit_down``。规则**可配置**（FR-EXT-6）：主板 ±10%、
     创业板（sz.30xxxx）/科创板（sh.68xxxx）±20%、ST（``isST=='1'``）±5%；
     上市首日（preclose 缺失/0）**无涨跌停**。代码前缀 → 档位字段映射是**全模块唯一
     登记点**（``BOARD_LIMIT_PCT``，前缀→``LimitFlagsConfig`` 字段名），未登记前缀
     raise ``UnknownBoardError``，⛔ 不静默按主板猜。判定用 eps（默认 1e-6 个百分点）
     吸收浮点噪声：``limit_up = pct >= +threshold - eps``、``limit_down =
     pct <= -threshold + eps``。⚠ eps 不改变档位归属（+15% 在 20% 档不触、在
     10% 档触板 = 超出阈值按触板保守处理）。

  ③ **除权标记（FR-BT-4 的派生层）**：``fetch_exdiv_events`` 薄壳调母库两个
     既有 kind（``adjust_factor`` 一次 + ``dividend`` 按年×yearType，利用两口的
     公共列 ``dividOperateDate``=除权除息日），产出逐日 ``exdiv`` 标记帧；
     ``combine_exdiv_flag`` 纯函数把标记帧按日期合并进 bars。

红线落点（对应 CLAUDE.md §3 / WORK_ORDER §3，⛔ 违反即返工）：

  ① **复用母库原语，不重造轮子**：除权事件走 ``baostock_source.fetch``
     （find 既有 kind，**不新增 kind**）；``dividend``（按年必填）与
     ``adjust_factor``（按区间）两口的除权日都叫 ``dividOperateDate``，共用同一
     解析。复权口径（R4，``adjust_mode``）与 ``source`` 列**原样透传**不触碰。

  ② **停牌只过滤、不填充（R1 红线）**：本模块没有任何前向填充路径；
     ``assert_no_prevfill_suspicious`` 只是**检测器**（给验收/对账抽样用），
     不是修补器。

  ③ **EMPTY_OK 与 FAIL_* 语义分开**（base.py 七态契约）：除权薄壳里
     ``EMPTY_OK``（真 0 行）= 该区间无除权事件，合法返回空帧（下游全 False）；
     任何 ``FAIL_*`` 一律 raise ``ExdivSourceError`` —— ⛔ 取数失败不得伪装成
     "无除权"：除权标记被回测结算消费（FR-BT-4），漏标 = 错误结算/净值跳变。

  ④ **fail-closed 家族**：缺必需列 / 未登记板块 / 非法 ``isST`` 取值 → 一律
     raise 特定 ``CleanerError`` 子类，⛔ 绝不静默降级为"没触板/非 ST"。

离线可测性（⛔ 红线"能离线就不联网"）：
  · ``fetch_exdiv_events`` 注入 ``fetch_fn``（默认仍是 ``baostock_source.fetch``，
    生产可用），单测用桩全程不打网、不登录；
  · 除权薄壳**不含限速/熔断**：生产路径复用 ``data/collector.py`` 的
    ``RateLimiter``/``CircuitBreaker`` 由调用方节流，薄壳保持薄。

已知简化（记录在案，待 T110 验收复核）：
  · 涨跌停阈值按**当前**规则统一（创业板 2020-08-24 注册制改革前实为 ±10%，
    科创板 2019-07-22 前无 20% 规则）；历史时点精确回放需按日期定制，v1 不区分，
    可在 ``LimitFlagsConfig`` 中按板块整体覆盖。
  · 涨跌停 ``pct`` 按 bars 内 ``close/preclose`` 计算：RAW 口径最可信；
    HFQ/QFQ 存储时除权日附近的 pct 会受复权因子平移，标记精度由 T110 三源比对复核。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import pandas as pd

from data.universe import canon_date
from finai.sources import baostock_source
from finai.sources.base import EMPTY_OK, OK

logger = logging.getLogger(__name__)

__all__ = [
    "CleanerError", "MissingColumnError", "UnknownBoardError", "InvalidIsSTError",
    "InvalidPriceError", "ExdivSourceError",
    "LimitFlagsConfig", "CleanResult",
    "BOARD_LIMIT_PCT", "EXDIV_COL", "EXDIV_DATE_COL", "EXDIV_KINDS",
    "DIVIDEND_YEAR_TYPES", "DIVIDEND_YEARS_BACK", "DEFAULT_PREFILL_TOL",
    "enforce_tradestatus", "assert_no_prevfill_suspicious",
    "mark_limit_flags", "board_limit_pct",
    "fetch_exdiv_events", "combine_exdiv_flag", "clean_daily_bars",
]


# ---------------------------------------------------------------- 异常族
# 与 T108 UniverseError（universe.py）/ 母库 UnknownAdjustment 同套路：
# 均为 ValueError 子类，⛔ 绝不静默返回可能错误的数据（rework 底线）。

class CleanerError(ValueError):
    """T106 停牌/涨跌停/除权清洗域的错误基类。"""


class MissingColumnError(CleanerError):
    """帧缺必需列。⛔ 缺列不静默降级 —— 镜像母库"日线 fields 必须含 tradestatus"的强制。"""


class UnknownBoardError(CleanerError):
    """代码前缀未登记涨跌幅规则（如北交所 43/83/87/92）。⛔ 不静默按主板猜。"""


class InvalidIsSTError(CleanerError):
    """``isST`` 取值不在 {'0','1'} 内。⛔ 未知值不静默按非 ST 处理（那会漏判 5% 档）。"""


class InvalidPriceError(CleanerError):
    """``close`` 为 NaN/非数值 —— ⛔ 不按"未触板"静默处理（该行涨跌停不可判定）。"""


class ExdivSourceError(CleanerError):
    """除权事件源取数失败（FAIL_*）。⛔ 不得把取数失败伪装成"无除权"（FR-BT-4）。"""


# ---------------------------------------------------------------- 常量

#: 清洗输出带的除权标记列名。
EXDIV_COL = "exdiv"

#: baostock 除权信息来源两 kind 的公共除权日字段（adjust_factor 与 dividend 同列名）。
EXDIV_DATE_COL = "dividOperateDate"

#: 除权事件的两个来源 kind —— 母库 baostock_source.fetch 既有，⛔ 不新增 kind。
#: ⭐ 声明顺序 = 返回帧/血缘列序（date → exdiv → 本元组），钉死：
#:   adjust_factor 在前（区间查询，权威覆盖），dividend 在后（按年×yearType）。
#: ⛔ 空帧与有行帧列序必须一致（EXDIV_KINDS 即列序唯一写死点），否则血缘列
#:   位置随数据有无漂移。与模块 docstring 列序一致。
EXDIV_KINDS: tuple[str, ...] = ("adjust_factor", "dividend")

#: ``dividend`` 的 yearType 两个取值都查：预案/实施口径都可能携带除权除息日，
#: 只查一个会漏掉另一口径记录的事件（baostock 按单一口径查询，⛔ 不猜）。
DIVIDEND_YEAR_TYPES: tuple[str, ...] = ("report", "plan")

#: 向外多查分红数据的年数：上年预案的年度分红常在次年 1-6 月实施（跨年），
#: 只查 [start.year, end.year] 会漏掉落在区间头部的上年度分红除权日。
DIVIDEND_YEARS_BACK = 1

#: 停牌脏行"前收平推签名"检测的相对容差（无量纲，相对 ``preclose``）。
DEFAULT_PREFILL_TOL = 1e-6

#: ⛔ 全模块**唯一**登记"代码数字前缀 → 涨跌幅档位"的地方
#: （对齐 T108 UNVERIFIED_INDEX_APIS 的"唯一写死点"纪律）。前缀取 6 位代码前两位，
#: 值 = ``LimitFlagsConfig`` 的**字段名**（``main_pct``/``chinext_pct``/``star_pct``，
#: 按档位聚合，数值在配置里）：
#:   60 = 沪市主板 A，90 = 沪市 B，00 = 深市主板 A，20 = 深市 B —— 均走 main_pct（默认 ±10%）
#:   30 = 创业板（注册制改革后）—— chinext_pct（默认 ±20%）
#:   68 = 科创板 —— star_pct（默认 ±20%）
#: ⭐ 存**字段名**而非字面阈值，是为了让 ``LimitFlagsConfig`` 覆盖（FR-EXT-6）对
#: 这些前缀真正生效：否则表内写死数值会把配置旁路（测试已抓到该 bug）。
#: 北交所（43/83/87/92，±30%）未登记 ⇒ 触 ``UnknownBoardError``，需要用须显式登记。
#: ⛔ 缺省 ``LimitFlagsConfig()`` 必须等于默认规则（main=10/chinext=20/star=20），
#: 否则登记表与缺省阈值漂移 —— 有测试钉死这一点。
BOARD_LIMIT_PCT: Mapping[str, str] = {
    "60": "main_pct", "00": "main_pct", "90": "main_pct", "20": "main_pct",
    "30": "chinext_pct",
    "68": "star_pct",
}

#: 涨跌停判定默认容差（百分点单位）：pct 与阈值差在 eps 内仍算触板
#: （吸收 (close-preclose)/preclose*100 的浮点舍入噪声）。
_LIMIT_EPS = 1e-6


# ---------------------------------------------------------------- 规则配置

@dataclass(frozen=True)
class LimitFlagsConfig:
    """涨跌停规则配置（FR-EXT-6：规则可配置，配置驱动单测）。

    阈值单位 = 百分点（10.0 即 ±10%）。``st_pct`` 只在小盘股规则的沿用：
    ⚠ 已知简化 —— 创业板/科创板注册制后 ST 实为 ±20% 与普通股相同，
    v1 统一按 ``st_pct`` 覆盖（见模块 docstring 已知简化），需要时按板块定制。
    ``eps`` 为判定容差，默认 1e-6 个百分点。
    """

    main_pct: float = 10.0      # 主板（60/00/90/20 前缀）±10%
    chinext_pct: float = 20.0   # 创业板（30 前缀）±20%
    star_pct: float = 20.0      # 科创板（68 前缀）±20%
    st_pct: float = 5.0         # ST（isST=='1'）±5%
    eps: float = _LIMIT_EPS


_DEFAULT_LIMIT_CONFIG = LimitFlagsConfig()


# ---------------------------------------------------------------- ① 停牌过滤（R1）

def enforce_tradestatus(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """纯函数：只保留 ``tradestatus=='1'`` 的交易日行，返回 (干净帧, 被滤行数)。

    ⛔ 三条规定（R1 / WORK_ORDER §3-4，违反即返工）：
      * 只**过滤**，⛔ 绝不前向填充（停牌日没有真实行情，填了就是伪造数据）；
      * 缺 ``tradestatus`` 列的日线帧 → raise ``MissingColumnError``
        （fail-closed，镜像母库 `_validate_kline_params` 的日线字段强制规则③：
        "字段必含 tradestatus，否则无法过滤停牌脏行"）；
      * ``tradestatus`` 取值非 '1'（含 NaN → 'nan'）一律按停牌滤除（保守）。
    """
    if "tradestatus" not in frame.columns:
        raise MissingColumnError(
            "日线帧必须含 tradestatus 列（R1：停牌脏行须显式过滤，缺列即拒绝；"
            "同 baostock_source._validate_kline_params 的日线字段强制）")
    pre = len(frame)
    mask = frame["tradestatus"].astype(str).str.strip() == "1"
    return frame.loc[mask].copy().reset_index(drop=True), pre - int(mask.sum())


def assert_no_prevfill_suspicious(
    frame: pd.DataFrame, tolerance: float = DEFAULT_PREFILL_TOL,
) -> list[str]:
    """检测停牌脏行"前收平推签名"（12 号 §9-A 实测 100% 命中），供验收按日抽样。

    签名：**OHLC 四项全部 == 前收** 且 **volume == 0**。`tolerance` 为相对
    ``preclose`` 的容差（无量纲，默认 1e-6；字符串/浮点/精度噪声都由它吸收）。

    返回可疑日期列表（``YYYY-MM-DD``，按帧内顺序）；**空列表 = 干净**。
    ⭐ 名字里的 assert 只表示"验收抽查用"，本函数**不 raise** — 它是检测器不是
    修补器（⛔ 检测到脏行后由调用方决定处理，本模块不做任何前向填充）。
    上市首日（``preclose`` 缺失/0）不参与判定 —— 那是首日无涨停规则，不是停牌签名。
    """
    required = ("date", "open", "high", "low", "close", "preclose", "volume")
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise MissingColumnError(
            f"前收平推签名检测需要列 {required}，缺 {missing}（缺列即拒绝）")
    out = frame.copy()
    for c in required:
        if c != "date":
            out[c] = pd.to_numeric(out[c], errors="coerce")
    pre = out["preclose"]
    valid = pre.notna() & (pre != 0)          # 上市首日（preclose 缺失/0）跳过
    lim = pre.abs() * tolerance
    flat = (out["open"] - pre).abs() <= lim
    for c in ("high", "low", "close"):
        flat = flat & ((out[c] - pre).abs() <= lim)
    susp = valid & flat & (out["volume"] == 0)   # volume NaN != 0 ⇒ 不误报
    dates = pd.to_datetime(out.loc[susp, "date"]).dt.strftime("%Y-%m-%d")
    return list(dates)


# ---------------------------------------------------------------- ② 涨跌停标记

def _is_st(value: Any) -> bool:
    """baostock 约定 ``isST`` 取值 '1'=ST / '0'=非 ST；⛔ 其他取值一律 raise。"""
    if isinstance(value, float) and math.isnan(value):
        raise InvalidIsSTError(
            "isST 为 NaN —— ⛔ 不静默按非 ST 处理（漏判 5% 档 = 错误触板）")
    s = str(value).strip()
    if s == "1":
        return True
    if s == "0":
        return False
    raise InvalidIsSTError(
        f"isST 取值 {value!r} 不在 {{'0','1'}} 内（baostock 约定；"
        f"⛔ 未知值不静默当非 ST）")


def _code_digits(code: Any) -> str:
    """``sh.600000`` / ``600000.SH`` / ``600000`` → ``600000``（纯 6 位数字）。"""
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    if len(digits) < 6:
        raise UnknownBoardError(
            f"无法从代码 {code!r} 解析 6 位数字代码 —— ⛔ 不静默猜测板块")
    return digits[:6]


def board_limit_pct(code: Any, config: LimitFlagsConfig | None = None) -> float:
    """代码 → 涨跌幅阈值（%）。映射表 = ``BOARD_LIMIT_PCT``（前缀→配置字段）∪ 配置值。

    ⭐ 前缀先查登记表拿到**档位字段名**，再从 ``config`` 取该字段的当前值 ——
    这样 ``LimitFlagsConfig`` 覆盖（FR-EXT-6）对每个板块真正生效，⛔ 不被表内
    写死的数值旁路。未登记前缀 → ``UnknownBoardError``（⛔ 不静默按主板猜）。
    """
    cfg = _DEFAULT_LIMIT_CONFIG if config is None else config
    prefix = _code_digits(code)[:2]
    field_name = BOARD_LIMIT_PCT.get(prefix)
    if field_name is None:
        raise UnknownBoardError(
            f"代码 {code!r} 前缀 {prefix} 未登记涨跌幅规则"
            f"（登记表 {sorted(BOARD_LIMIT_PCT)}；北交所等未纳入须显式登记，"
            f"⛔ 不静默按主板猜）")
    return float(getattr(cfg, field_name))


def mark_limit_flags(
    frame: pd.DataFrame, config: LimitFlagsConfig | None = None,
) -> pd.DataFrame:
    """纯函数：加 bool 列 ``limit_up`` / ``limit_down``（涨跌停派生层，FR-BT-2）。

    规则（FR-EXT-6 配置驱动）：主板 ±10%、创业板 30xxx/科创板 68xxx ±20%、
    ST（``isST=='1'``）±5%；上市首日（``preclose`` 缺失/0）→ 双 False（无涨跌停）。

    判定：``pct = (close - preclose) / preclose * 100``；
      ``limit_up   = pct >= +threshold - eps``
      ``limit_down = pct <= -threshold + eps``
    ``eps`` 默认 1e-6 个百分点，吸收浮点舍入（恰好触板必须算触板）。
    ⚠ eps 不改变档位归属：+15% 在 20% 档（创业板）不触板、在 10% 档（主板）
    **触板**（超出阈值 = 触板或更远，保守按涨停处理 —— 回测侧"涨停买入不成交"
    对超限异常数据取保守语义）。

    必需列：``code`` / ``close`` / ``preclose`` / ``isST`` —— 缺一即 raise
    （⛔ 不静默降级）；``close`` 为 NaN → raise（那行的触板状态不可判定）。
    """
    cfg = _DEFAULT_LIMIT_CONFIG if config is None else config
    required = ("code", "close", "preclose", "isST")
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise MissingColumnError(
            f"涨跌停标记需要列 {required}，缺 {missing}——⛔ 不静默降级")
    out = frame.copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    if close.isna().any():
        raise InvalidPriceError(
            "close 存在 NaN/非数值 —— ⛔ 不按'未触板'静默处理（该行涨跌停不可判定）")
    pre = pd.to_numeric(out["preclose"], errors="coerce")
    st = out["isST"].map(_is_st)
    board_thr = out["code"].map(lambda c: board_limit_pct(c, cfg))
    thr = pd.Series(
        [cfg.st_pct if s else b for s, b in zip(st, board_thr)],
        index=out.index)
    pct = (close - pre) / pre * 100.0
    tradable = pre.notna() & (pre != 0)          # 上市首日 → 无涨跌停
    out["limit_up"] = (tradable & (pct >= thr - cfg.eps)).astype(bool)
    out["limit_down"] = (tradable & (pct <= -thr + cfg.eps)).astype(bool)
    return out


# ---------------------------------------------------------------- ③ 除权标记

#: 取数器签名：与 ``baostock_source.fetch`` 同构（kind + 关键字参 → FetchResult）。
FetchExdivFn = Callable[..., Any]


def _is_empty(v: Any) -> bool:
    """空串/None/NaN/'nan' 一律视为缺失（``_drain`` 出来是字符串，手工帧可能带 NaN）。"""
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    s = str(v).strip()
    return s == "" or s.lower() == "nan"


def fetch_exdiv_events(
    code: str,
    start: str,
    end: str,
    *,
    fetch_fn: FetchExdivFn | None = None,
    years_back: int = DIVIDEND_YEARS_BACK,
) -> pd.DataFrame:
    """除权事件薄壳：母库两 kind → 区间内除权除息日逐日标记帧。

    调用形态（⛔ 不复重造轮子，kind 全是母库既有）：
      * ``fetch("adjust_factor", code, start_date, end_date)`` —— 一次覆盖区间；
      * ``fetch("dividend", code, year, yearType)`` —— 按 [start.year-1, end.year]
        逐年、yearType∈{report, plan} 两口径都查（跨年实施 + 预案/实施口径都要兜住）。
    两 kind 的公共列 ``dividOperateDate`` = 除权除息日；非空且在 [start, end] 内的
    行记为该日有除权事件。同日多源出现 → 单行，来源标记列各自为 True。

    七态契约（FINDING-178 纪律）：
      * ``EMPTY_OK``（真 0 行）= 该区间无除权事件 ⇒ 合法返回空帧（⛔ 不读作"无数据"）；
      * 任何 ``FAIL_*`` ⇒ raise ``ExdivSourceError`` —— ⛔ 取数失败不得伪装成
        "无除权"（除权标记被回测结算消费 FR-BT-4，漏标 = 错误结算）。

    返回帧列：``date``（YYYY-MM-DD）+ ``exdiv``（恒 True）+ 来源标记列
    ``dividend`` / ``adjust_factor``。空区间也带这 4 列（契约稳定）。

    ⛔ 薄壳不含限速/熔断：生产路径由调用方串行节流（复用 ``data/collector.py``
    的 ``RateLimiter``/``CircuitBreaker``），报此薄壳保持薄。
    """
    fetch = fetch_fn if fetch_fn is not None else baostock_source.fetch
    start_c, end_c = canon_date(start), canon_date(end)
    if start_c > end_c:
        raise CleanerError(f"start {start_c} 晚于 end {end_c}（区间倒置，拒绝）")
    records: dict[str, set[str]] = {}

    def _absorb(kind: str, res: Any) -> None:
        if res.state == EMPTY_OK:
            return
        if res.state != OK:
            raise ExdivSourceError(
                f"除权事件源 {kind} 取数失败 state={res.state} "
                f"detail={res.detail} —— ⛔ 不得伪装成'无除权'（FR-BT-4 结算依赖）")
        frame = res.frame
        if frame is None:           # OK 但无帧：母库 make_result 不会产出，仅防御
            return
        if EXDIV_DATE_COL not in frame.columns:
            raise ExdivSourceError(
                f"{kind} 返回缺 {EXDIV_DATE_COL} 列（实际列：{list(frame.columns)}）")
        for raw in frame[EXDIV_DATE_COL]:
            if _is_empty(raw):
                continue
            try:
                d = canon_date(str(raw))
            except ValueError:
                raise ExdivSourceError(
                    f"{kind} 除权日期无法解析（⛔ fail-closed，绝不静默跳过）: {raw!r} "
                    f"—— 脏日期不丢弃，宁可红，不可漏标记（FR-BT-4 结算依赖）") from None
            if start_c <= d <= end_c:
                records.setdefault(d, set()).add(kind)

    _absorb("adjust_factor",
            fetch("adjust_factor", code=code,
                  start_date=start_c, end_date=end_c))
    for year in range(int(start_c[:4]) - years_back, int(end_c[:4]) + 1):
        for year_type in DIVIDEND_YEAR_TYPES:
            _absorb("dividend",
                    fetch("dividend", code=code,
                          year=str(year), yearType=year_type))

    cols = ("date", EXDIV_COL, *EXDIV_KINDS)
    if not records:
        return pd.DataFrame(columns=list(cols))
    rows = [
        {"date": d, EXDIV_COL: True,
         "dividend": "dividend" in kinds,
         "adjust_factor": "adjust_factor" in kinds}
        for d, kinds in sorted(records.items())
    ]
    out = pd.DataFrame(rows, columns=list(cols))
    # ⛔ 契约列序钉死：date → exdiv → 来源标记（按 EXDIV_KINDS 声明顺序，⛔ 不随
    #    记录产生先后漂移；空帧与有行帧列序必须一致，否则血缘列位置随数据变）。
    out = out[["date", EXDIV_COL, *EXDIV_KINDS]]
    return out


def combine_exdiv_flag(
    bars_frame: pd.DataFrame, events_frame: pd.DataFrame,
) -> pd.DataFrame:
    """纯函数：除权事件帧按日期合并进 bars，加 bool 列 ``exdiv``。

    ``bars_frame`` 必含 ``date``；``events_frame`` 必含 ``date``（缺列 raise，
    ⛔ 不静默当"无事件"—— 那会把"没喂事件数据"伪装成"没除权"）。
    events 帧若带 ``exdiv`` 列则以该列过滤参与标记的日期（默认全部参与）。
    合并是**纯标记**：不删行、不改价格、不填充（R1 红线）——
    血缘列（``source``/``adjust_mode`` 等）随拷贝原样透传。
    """
    if "date" not in bars_frame.columns:
        raise MissingColumnError(
            f"bars 帧必须含 date 列（实际列：{list(bars_frame.columns)}）")
    if "date" not in events_frame.columns:
        raise MissingColumnError(
            f"除权事件帧必须含 date 列（实际列：{list(events_frame.columns)}）"
            f"—— ⛔ 不静默当'无事件'")
    out = bars_frame.copy()
    bar_dates = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    ev = events_frame.copy()
    ev["_date_c"] = pd.to_datetime(ev["date"]).dt.strftime("%Y-%m-%d")
    if EXDIV_COL in ev.columns:
        flagged = ev.loc[ev[EXDIV_COL].astype(bool), "_date_c"]
    else:
        flagged = ev["_date_c"]
    out[EXDIV_COL] = bar_dates.isin(set(flagged)).astype(bool)
    return out


# ---------------------------------------------------------------- 结果容器

@dataclass(frozen=True)
class CleanResult:
    """T106 清洗结果：干净 bars + 各标记行数 + 完整血缘 meta。

    ``__post_init__`` 校验（同 T108 快照套路，⛔ 不平不禁跑）：
      必须带全标记列（``date``/``limit_up``/``limit_down``/``exdiv``）；
      帧内不得再混非交易日行；各计数非负、≤ 帧行数、且与列内和**逐项一致**
      —— 计数对不上即 raise，⛔ 绝不静默返回。

    ``meta`` 携带血缘：``source``/``adjust_mode`` 透传（FR-DATA-3）、
    ``suspended_rows``/``n_limit_up``/``n_limit_down``/``n_exdiv`` 计数、
    涨跌停规则快照与除权来源清单。
    """

    frame: pd.DataFrame
    suspended_rows: int = 0
    n_limit_up: int = 0
    n_limit_down: int = 0
    n_exdiv: int = 0
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for col in ("date", "limit_up", "limit_down", EXDIV_COL):
            if col not in self.frame.columns:
                raise MissingColumnError(
                    f"清洗结果帧缺必需列 {col!r}（必须带全标记列）")
        if "tradestatus" in self.frame.columns and not (
                self.frame["tradestatus"].astype(str).str.strip() == "1").all():
            raise CleanerError(
                "清洗结果帧内仍混有非交易日行（tradestatus != '1'）—— 结果必须已过滤停牌")
        n = len(self.frame)
        for name, value in (
            ("suspended_rows", self.suspended_rows),
            ("n_limit_up", self.n_limit_up),
            ("n_limit_down", self.n_limit_down),
            ("n_exdiv", self.n_exdiv),
        ):
            if value < 0:
                raise CleanerError(f"{name}={value} 为负，计数必须非负")
            if value > n:
                raise CleanerError(f"{name}={value} 超过帧行数 {n}（对不上）")
        if self.n_limit_up != int(self.frame["limit_up"].sum()):
            raise CleanerError(
                f"n_limit_up={self.n_limit_up} ≠ 帧内 limit_up 列和 "
                f"{int(self.frame['limit_up'].sum())}（计数对不上，拒绝）")
        if self.n_limit_down != int(self.frame["limit_down"].sum()):
            raise CleanerError(
                f"n_limit_down={self.n_limit_down} ≠ 帧内 limit_down 列和 "
                f"{int(self.frame['limit_down'].sum())}（计数对不上，拒绝）")
        if self.n_exdiv != int(self.frame[EXDIV_COL].sum()):
            raise CleanerError(
                f"n_exdiv={self.n_exdiv} ≠ 帧内 exdiv 列和 "
                f"{int(self.frame[EXDIV_COL].sum())}（计数对不上，拒绝）")


# ---------------------------------------------------------------- 编排

def _resolve_lineage(out: pd.DataFrame, name: str, arg: str | None) -> str | None:
    """血缘取值：显式入参（调用方拍板）> 帧携带列（落盘事实）> 未知（⛔ 不伪造）。"""
    if arg is not None:
        return arg
    if name in out.columns and len(out) > 0:
        return str(out[name].iloc[0])
    return None


def clean_daily_bars(
    bars: pd.DataFrame,
    events: pd.DataFrame | None = None,
    *,
    source: str | None = None,
    adjust_mode: str | None = None,
    limit_config: LimitFlagsConfig | None = None,
    meta_extra: Mapping[str, Any] | None = None,
) -> CleanResult:
    """清洗编排：停牌过滤 → 涨跌停标记 → 除权标记 → ``CleanResult``。

    ``bars`` 为日线帧（T104 §1.1 schema，含 ``tradestatus``/``isST``/``preclose``
    等）；``events`` 为除权事件帧（``fetch_exdiv_events`` 的产物；``None`` =
    调用方显式声明不提供事件，此时 ``exdiv`` 全 False 且 meta 有记录 —— 显式选择
    不是静默）。

    血缘（FR-DATA-3）：``source``/``adjust_mode`` 按"入参 > 帧携带列 > 未知
    不伪造"解析进 meta；帧内血缘列原样透传。

    ⛔ **校验列（code/isST/preclose）若缺则 raise**（fail-closed：这些列是
    涨跌停/停牌判定的依据，缺列 = 该段清洗根本无法执行，⛔ 不静默降级成
    "只过滤 tradestatus"。血缘列 source/adjust_mode 则**允许缺**（未知不伪造）。
    """
    if events is not None and not isinstance(events, pd.DataFrame):
        raise CleanerError(
            f"events 必须是 DataFrame 或 None，实得 {type(events).__name__}")

    # ⛔ 编排自身的 fail-closed：涨跌停判定必需列必须在喂给 mark_limit_flags 前齐。
    _validation_required = ("code", "close", "preclose", "isST", "tradestatus")
    missing_required = [c for c in _validation_required if c not in bars.columns]
    if missing_required:
        raise MissingColumnError(
            f"日线帧缺校验必需列 {missing_required}（需 {_validation_required}；"
            f"⛔ 缺校验列 = 无法做涨跌停/停牌判定，不静默降级）")

    clean, n_dropped = enforce_tradestatus(bars)
    flagged = mark_limit_flags(clean, limit_config)
    if events is None:
        events = pd.DataFrame(columns=["date", EXDIV_COL, *EXDIV_KINDS])
    out = combine_exdiv_flag(flagged, events)

    n_up = int(out["limit_up"].sum())
    n_down = int(out["limit_down"].sum())
    n_ex = int(out[EXDIV_COL].sum())
    cfg = _DEFAULT_LIMIT_CONFIG if limit_config is None else limit_config

    meta: dict[str, Any] = {
        "suspended_rows": n_dropped,
        "n_limit_up": n_up,
        "n_limit_down": n_down,
        "n_exdiv": n_ex,
        "limit_rules": {
            "main_pct": cfg.main_pct,
            "chinext_pct": cfg.chinext_pct,
            "star_pct": cfg.star_pct,
            "st_pct": cfg.st_pct,
            "eps": cfg.eps,
            # ⭐ 登记表（前缀→配置字段名）原样入血缘；数值以 main/chinext/star_pct 为准。
            "board_prefix_pct": dict(BOARD_LIMIT_PCT),
        },
        # 血缘 = 声明的来源类型（EXDIV_KINDS 的既有顺序），⛔ 不按帧列动态推
        # （空事件帧或列缺失会让动态推导塌成 []，谎报"没查过任何源"）。
        "exdiv_sources": list(EXDIV_KINDS),
        "exdiv_events_rows": len(events),
    }
    for name, arg in (("source", source), ("adjust_mode", adjust_mode)):
        resolved = _resolve_lineage(out, name, arg)
        if resolved is not None:
            meta[name] = resolved
    if meta_extra:
        meta.update(dict(meta_extra))

    return CleanResult(
        frame=out, suspended_rows=n_dropped,
        n_limit_up=n_up, n_limit_down=n_down, n_exdiv=n_ex, meta=meta)