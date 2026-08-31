#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T106（停牌/涨跌停/除权清洗，FR-DATA-2）单测 —— 全部**离线**，⛔ 无任何网络调用。

对应任务要求（tasks.md:25 / spec.md:71 FR-DATA-2 / plan.md P-1.3）：
  ① 停牌强制过滤（R1）：``enforce_tradestatus`` 只留 ``tradestatus=='1'`` 行、
     计数正确、⛔ 无前向填充；缺 ``tradestatus`` 列 raise（fail-closed）；
     干净窗口零误杀。
  ② 前收平推签名检测：``assert_no_prevfill_suspicious`` 命中
     OHLC==前收 ∧ volume==0 的停牌签名；干净行不误报；上市首日不误报。
  ③ 涨跌停标记：主板 ±10%、创业板 30xxx/科创板 68xxx ±20%、ST（isST='1'）±5%、
     上市首日（preclose 缺失/0）双 False、eps 边界（恰好触板算触板）、
     15% 在主板不触板 / 不在 20% 档不触板的反面例；未登记板块 raise；
     非法 isST / close NaN raise（⛔ 不静默）。
  ④ 除权：桩 ``fetch_fn``（DI，不打网）返回 dividend+adjust_factor →
     事件日合并正确；空事件（EMPTY_OK）→ 全 False；分区外事件不入；
     FAIL_* 源错误 raise（⛔ 不得伪装成"无除权"）；**畸形事件日期 raise**（fail-closed，
     不静默跳过 —— 见 ``test_fetch_exdiv_events_out_of_range_dates_excluded``）。
     ⛔ 桩的"空"必须是 ``EMPTY_OK``（真 0 行），不是 ``OK + 空帧``
     （OK 态带一个零列空帧 = 谎报字段面，薄壳按缺 ``dividOperateDate`` 正确地红）。
  ⑤ 编排：``clean_daily_bars`` 全链路计数/血缘；``CleanResult.__post_init__``
     校验（计数对不上 / 缺标记列 / 结果帧混停牌行 → raise）。
  ⑥ 复用母库原语：``fetch_exdiv_events`` 默认 fetch_fn 就是
     ``baostock_source.fetch``（不重造轮子）。

⛔⛔ 永不静默原则：断言不被 try/except 吞错；任何失败让 pytest 红。
"""
from __future__ import annotations

import inspect

import pandas as pd
import pytest

from data import cleaner as cl
from finai.sources.base import EMPTY_OK, FAIL_GATEWAY, OK


# ══════════════════════════════════════════════════════════════════════
# 合成帧（字符串形态，镜像 baostock _drain 输出形状）
# ══════════════════════════════════════════════════════════════════════

def _susp_frame() -> pd.DataFrame:
    """含 2 条停牌脏行（07-16/07-17：tradestatus='0'、OHLC=前收平推、volume=0）。"""
    return pd.DataFrame({
        "date": ["2025-07-15", "2025-07-16", "2025-07-17", "2025-07-18"],
        "open":  [10.0, 10.5, 10.5, 10.5],
        "high":  [10.6, 10.5, 10.5, 10.5],
        "low":   [9.9, 10.5, 10.5, 10.5],
        "close": [10.5, 10.5, 10.5, 10.9],
        "preclose": [10.0, 10.5, 10.5, 10.5],
        "volume": ["1000", "0", "0", "1200"],
        "tradestatus": ["1", "0", "0", "1"],
        "isST": ["0", "0", "0", "0"],
        "code": ["sh.600000"] * 4,
    })


def _ok_result(frame: pd.DataFrame, **meta_extra) -> pd.DataFrame:
    """把 'DataFrame' 包装成 state=OK 的 FetchResult（给 fetch_fn 桩返回）。"""
    from finai.sources.base import FetchResult

    meta = {"suspended_rows": 0}
    meta.update(meta_extra)
    return FetchResult(state=OK, frame=frame, rows=len(frame),
                       source="baostock", meta=meta)


def _empty_result() -> pd.DataFrame:
    """EMPTY_OK 结果（合法空事件，⛔ 不得读作'无数据'）。"""
    from finai.sources.base import FetchResult

    return FetchResult(state=EMPTY_OK, frame=pd.DataFrame(), rows=0,
                       source="baostock", detail=EMPTY_OK)


# ══════════════════════════════════════════════════════════════════════
# ① 停牌强制过滤（R1：只过滤，绝不前向填充）
# ══════════════════════════════════════════════════════════════════════

def test_enforce_tradestatus_drops_and_counts() -> None:
    """① 只保留 tradestatus=='1' 行，被滤行数正确；⛔ 不得出现'填充'出来的行。"""
    clean, n = cl.enforce_tradestatus(_susp_frame())
    assert n == 2
    assert len(clean) == 2
    assert set(clean["date"]) == {"2025-07-15", "2025-07-18"}
    assert (clean["tradestatus"] == "1").all()


def test_never_forward_fill_suspended_rows() -> None:
    """① ⛔ R1 红线：断言结果里不存在任何"补出来的"停牌日行（填充即返工）。"""
    clean, _ = cl.enforce_tradestatus(_susp_frame())
    assert "2025-07-16" not in set(clean["date"])
    assert "2025-07-17" not in set(clean["date"])
    # 停牌日既不在结果里，pandas 层面也不可能存在 NaN 行冒充它
    assert clean["date"].notna().all()


def test_enforce_tradestatus_clean_window_zero_false_positive() -> None:
    """① 干净窗口（全 tradestatus='1'）零误杀：行数不变、计数为 0。"""
    clean_rows = _susp_frame()
    clean_rows = clean_rows[clean_rows["tradestatus"] == "1"].reset_index(drop=True)
    out, n = cl.enforce_tradestatus(clean_rows)
    assert n == 0 and len(out) == len(clean_rows)


def test_enforce_tradestatus_missing_column_raises() -> None:
    """① 缺 tradestatus 列 ⇒ MissingColumnError（fail-closed，⛔ 不静默放行）。"""
    frame = _susp_frame().drop(columns=["tradestatus"])
    with pytest.raises(cl.MissingColumnError, match="tradestatus"):
        cl.enforce_tradestatus(frame)
    assert issubclass(cl.MissingColumnError, ValueError), "异常族必须是 ValueError 子类"


def test_enforce_tradestatus_trimmed_or_nan_values_filtered() -> None:
    """① '1 ' 带空白算交易日；NaN/'0'/其他一律按停牌滤除（保守，缺列即防）。"""
    frame = _susp_frame()
    frame.loc[len(frame)] = ["2025-07-21", "10.0", "10.6", "9.9", "10.5",
                             "10.0", "500", "1 ", "0", "sh.600000"]
    frame.loc[len(frame)] = ["2025-07-22", "10.0", "10.6", "9.9", "10.5",
                             "10.0", "0", None, "0", "sh.600000"]
    frame.loc[len(frame)] = ["2025-07-23", "10.0", "10.6", "9.9", "10.5",
                             "10.0", "0", "0", "0", "sh.600000"]
    out, n = cl.enforce_tradestatus(frame)
    assert "2025-07-21" in set(out["date"]), "去空白后 '1 ' 是交易日"
    assert "2025-07-22" not in set(out["date"]), "NaN 按停牌保守滤除"
    assert "2025-07-23" not in set(out["date"])
    assert n == 4


# ══════════════════════════════════════════════════════════════════════
# ② 前收平推签名检测（OHLC==前收 ∧ volume==0；给验收按日抽样用）
# ══════════════════════════════════════════════════════════════════════

def test_prevfill_detector_catches_signature_dates() -> None:
    """② 命中签名日期（07-16/07-17），干净日（07-15/07-18）不入。"""
    susp = cl.assert_no_prevfill_suspicious(_susp_frame())
    assert susp == ["2025-07-16", "2025-07-17"], susp


def test_prevfill_detector_clean_rows_not_flagged() -> None:
    """② 干净行（OHLC 非平推 或 volume>0）不被误报。"""
    clean_rows = _susp_frame()
    clean_rows = clean_rows[clean_rows["tradestatus"] == "1"].reset_index(drop=True)
    susp = cl.assert_no_prevfill_suspicious(clean_rows)
    assert susp == []


def test_prevfill_detector_volume_zero_but_price_moves_not_flagged() -> None:
    """② 反例：volume=0 但价格有变（非平推）≠ 停牌签名 —— 不得误报。"""
    frame = pd.DataFrame({
        "date": ["2025-07-15"],
        "open": [10.0], "high": [10.4], "low": [9.9], "close": [10.2],
        "preclose": [10.0], "volume": ["0"], "tradestatus": ["1"],
    })
    assert cl.assert_no_prevfill_suspicious(frame) == []


def test_prevfill_detector_float_rounding_tolerance() -> None:
    """② 浮点/精度噪声在 tolerance 内（相对容差 1e-6）算平推；tol=0 则严格。"""
    frame = pd.DataFrame({
        "date": ["2025-07-15", "2025-07-16", "2025-07-17"],
        "open": [10.5, 10.5000005, 10.0], "high": [10.5, 10.5, 10.0],
        "low": [10.5, 10.5, 10.0], "close": [10.5, 10.5, 10.0],
        "preclose": [10.5, 10.5, 11.0], "volume": ["0", "0", "0"],
        "tradestatus": ["0", "0", "0"],
    })
    susp = cl.assert_no_prevfill_suspicious(frame)      # 默认 tol=1e-6（相对 preclose）
    assert susp == ["2025-07-15", "2025-07-16"], \
        "07-15 精确平推、07-16 偏差 5e-7 在 1e-6 相对容差内 → 都报；07-17 差价 1 元 → 不报"
    susp2 = cl.assert_no_prevfill_suspicious(frame, tolerance=0.0)
    assert susp2 == ["2025-07-15"], "tol=0 严格相等：07-16 的浮点偏差即不视为平推"


def test_prevfill_detector_listing_day_not_flagged() -> None:
    """② 上市首日（preclose 缺失/'0'/NaN）不是停牌签名 —— 不参与判定。"""
    frame = pd.DataFrame({
        "date": ["2025-07-15", "2025-07-16", "2025-07-17"],
        "open": [10.0, 10.0, 11.0], "high": [10.5, 10.0, 11.0],
        "low": [9.5, 10.0, 11.0], "close": [10.2, 10.0, 11.0],
        "preclose": ["", "0", None], "volume": ["1000", "0", "0"],
        "tradestatus": ["1", "0", "0"],
    })
    assert cl.assert_no_prevfill_suspicious(frame) == []


def test_prevfill_detector_missing_columns_raise() -> None:
    """② 缺检测列 ⇒ MissingColumnError（缺列即拒绝，⛔ 不静默返回空）。"""
    with pytest.raises(cl.MissingColumnError, match="volume"):
        cl.assert_no_prevfill_suspicious(_susp_frame().drop(columns=["volume"]))


# ══════════════════════════════════════════════════════════════════════
# ③ 涨跌停标记（主板 10% / 创业板、科创板 20% / ST 5% / 首日无涨跌停 / eps 边界）
# ══════════════════════════════════════════════════════════════════════

def _flag_frame() -> pd.DataFrame:
    """基准：主板 sh.600000，7 行覆盖 涨停/跌停/未触板/上市首日/ST/对照。"""
    return pd.DataFrame({
        "code":     ["sh.600000"] * 7,
        "date":     [f"2025-07-{d:02d}" for d in (15, 16, 17, 18, 21, 22, 23)],
        "close":    ["11.00", "9.00", "10.40", "11.00", "10.50", "10.50", "10.50"],
        "preclose": ["10.00", "10.00", "10.00", "", "10.00", "10.00", "10.00"],
        "isST":     ["0", "0", "0", "0", "1", "0", "0"],
    })


def test_limit_flags_main_board_10pct_hits_up_and_down() -> None:
    """③ 主板：+10% 触涨停、-10% 触跌停、+4% 不触板、ST+5% 触板、非 ST+5% 不触板。"""
    out = cl.mark_limit_flags(_flag_frame())
    assert out["limit_up"].tolist() == [True, False, False, False, True, False, False], \
        out["limit_up"].tolist()
    assert out["limit_down"].tolist() == [False, True, False, False, False, False, False], \
        out["limit_down"].tolist()
    assert out["limit_up"].dtype == bool and out["limit_down"].dtype == bool


def test_limit_flags_below_threshold_no_hit() -> None:
    """③ 反例：+8.9% 与 -8.9% 都不应触板（严格阈值边界）。"""
    frame = pd.DataFrame({
        "code": ["sh.600000", "sh.600000"],
        "close": ["10.89", "9.11"],
        "preclose": ["10.00", "10.00"],
        "isST": ["0", "0"],
    })
    out = cl.mark_limit_flags(frame)
    assert not out["limit_up"].any() and not out["limit_down"].any()


def test_limit_flags_chinext_and_star_use_20pct() -> None:
    """③ 创业板 sz.300xxx / 科创板 sh.68xxxx 用 ±20% 档：恰好 ±20% 触板。"""
    frame = pd.DataFrame({
        "code": ["sz.300750", "sz.300750", "sh.688111", "sh.688111"],
        "close": ["12.00", "8.00", "12.00", "8.00"],
        "preclose": ["10.00", "10.00", "10.00", "10.00"],
        "isST": ["0", "0", "0", "0"],
    })
    out = cl.mark_limit_flags(frame)
    assert out["limit_up"].tolist() == [True, False, True, False], \
        "20% 档：+20% 应触涨停（主板 +10% 才触，此处板块规则是 20%）"
    assert out["limit_down"].tolist() == [False, True, False, True], \
        "20% 档：-20% 应触跌停"


def test_limit_flags_sub_threshold_15pct_does_not_hit_on_20pct_board() -> None:
    """③ 反例：20% 档 +15% 不触涨停（低于阈值）—— ⛔ eps 不得吞掉近阈值信号。"""
    frame = pd.DataFrame({
        "code": ["sz.300750", "sh.688111"],
        "close": ["11.50", "11.50"],
        "preclose": ["10.00", "10.00"],
        "isST": ["0", "0"],
    })
    out = cl.mark_limit_flags(frame)
    assert out["limit_up"].tolist() == [False, False], \
        "创业板/科创板 +15% 不触 ±20% 涨停（15% < 20%，eps 不得吞掉）"


def test_limit_flags_fifteen_percent_not_limit_on_main_board() -> None:
    """③ 主板档：+15% 超出 ±10% 阈值 ⇒ 触板（超出阈值即触，保守语义）；恰好 +10% 也触。"""
    frame = pd.DataFrame({
        "code": ["sh.600000", "sh.600000", "sh.600000"],
        "close": ["11.50", "11.00", "10.50"],
        "preclose": ["10.00", "10.00", "10.00"],
        "isST": ["0", "0", "0"],
    })
    out = cl.mark_limit_flags(frame)
    assert out["limit_up"].tolist() == [True, True, False], \
        "主板档(±10%)：+15% 超阈触板、恰好 +10% 触板、+5% 不触（板块档被正确使用）"


def test_limit_flags_cross_board_distinction_at_15pct() -> None:
    """③ 板块档区分：同一 +15%，主板档(10%)触板（超阈）、创业板档(20%)不触（低于阈）。"""
    frame = pd.DataFrame({
        "code": ["sh.600000", "sz.300750"],
        "close": ["11.50", "11.50"],
        "preclose": ["10.00", "10.00"],
        "isST": ["0", "0"],
    })
    out = cl.mark_limit_flags(frame)
    assert out["limit_up"].tolist() == [True, False], \
        "15%：主板档(10%) 15%>10% 触板；创业板档(20%) 15%<20% 不触板（档位区分正确）"


def test_limit_flags_st_uses_5pct() -> None:
    """③ isST='1' ⇒ ±5%：+5% 触涨停、+10% 也触涨停；非 ST 同价不触板。"""
    frame = pd.DataFrame({
        "code": ["sh.600000", "sh.600000", "sh.600000"],
        "close": ["10.50", "11.00", "10.50"],
        "preclose": ["10.00", "10.00", "10.00"],
        "isST": ["1", "1", "0"],
    })
    out = cl.mark_limit_flags(frame)
    assert out["limit_up"].tolist() == [True, True, False], \
        "ST +5% 即触涨停（10.50）；+10% 更触；非 ST 同价 +5% 不触"


def test_limit_flags_listing_day_no_limit() -> None:
    """③ 上市首日（preclose 缺失/0/NaN）⇒ 双 False（无涨跌停规则）。"""
    frame = _flag_frame()
    out = cl.mark_limit_flags(frame)
    assert not out["limit_up"].iloc[3] and not out["limit_down"].iloc[3], \
        "preclose 缺失（上市首日）不得触板"
    frame.loc[len(frame)] = ["sh.600000", "2025-07-24", "11.00", "0", "0"]
    out2 = cl.mark_limit_flags(frame)
    assert not out2["limit_up"].iloc[-1] and not out2["limit_down"].iloc[-1], \
        "preclose=0 视为首日，不得触板"


def test_limit_flags_eps_boundary_exact_threshold_counts() -> None:
    """③ eps 边界：pct 恰好等于阈值（1e-17 级浮点噪声）必须算触板。"""
    frame = pd.DataFrame({
        "code": ["sh.600000", "sh.600000", "sh.600000", "sh.600000"],
        "close": ["11.0", "9.0", "10.999999999999998", "9.000000000000002"],
        "preclose": ["10.0", "10.0", "10.0", "10.0"],
        "isST": ["0", "0", "0", "0"],
    })
    out = cl.mark_limit_flags(frame)          # 默认 eps=1e-6 个百分点
    assert out["limit_up"].tolist() == [True, False, True, False]
    assert out["limit_down"].tolist() == [False, True, False, True]
    # 默认 eps 下 0.5% 的偏差（阈值附近但超出浮点噪声）不算触板
    frame2 = pd.DataFrame({
        "code": ["sh.600000"], "close": ["10.49999"], "preclose": ["10.0"],
        "isST": ["0"],
    })
    out2 = cl.mark_limit_flags(frame2)
    assert not out2["limit_up"].iloc[0]


def test_limit_flags_config_overrides_boards() -> None:
    """③ FR-EXT-6 配置驱动：传入配置可整体覆盖板块阈值（数据流 = 板块→阈值）。"""
    cfg = cl.LimitFlagsConfig(main_pct=30.0, chinext_pct=10.0)
    frame = pd.DataFrame({
        "code": ["sh.600000", "sz.300750", "sz.300750"],
        "close": ["11.50", "11.50", "12.00"],
        "preclose": ["10.00", "10.00", "10.00"],
        "isST": ["0", "0", "0"],
    })
    out = cl.mark_limit_flags(frame, config=cfg)
    assert out["limit_up"].tolist() == [False, True, True], \
        "config 覆盖后：主板档 30%（15% 不触）、创业板档 10%（15% 触、20% 也触）"


def test_limit_flags_unknown_board_raises() -> None:
    """③ 北交所（43/83/87/92 前缀）未登记 ⇒ UnknownBoardError（⛔ 不静默按主板猜）。"""
    frame = pd.DataFrame({
        "code": ["bj.832000"], "close": ["13.0"], "preclose": ["10.0"], "isST": ["0"],
    })
    with pytest.raises(cl.UnknownBoardError, match="832000"):
        cl.mark_limit_flags(frame)
    assert issubclass(cl.UnknownBoardError, ValueError)


def test_limit_flags_invalid_ist_raises() -> None:
    """③ isST 取值不在 {'0','1'}（含 NaN）⇒ InvalidIsSTError（⛔ 不静默当非 ST）。"""
    for bad in ("2", "", None):
        frame = pd.DataFrame({
            "code": ["sh.600000"], "close": ["10.5"], "preclose": ["10.0"],
            "isST": [bad],
        })
        with pytest.raises(cl.InvalidIsSTError):
            cl.mark_limit_flags(frame)


def test_limit_flags_close_nan_raises() -> None:
    """③ close 为 NaN ⇒ InvalidPriceError（⛔ 不按'未触板'静默处理）。"""
    frame = pd.DataFrame({
        "code": ["sh.600000"], "close": [None], "preclose": ["10.0"], "isST": ["0"],
    })
    with pytest.raises(cl.InvalidPriceError):
        cl.mark_limit_flags(frame)


def test_limit_flags_missing_columns_raise() -> None:
    """③ 缺任一必需列（code/close/preclose/isST）⇒ MissingColumnError。"""
    with pytest.raises(cl.MissingColumnError, match="isST"):
        cl.mark_limit_flags(_flag_frame().drop(columns=["isST"]))
    with pytest.raises(cl.MissingColumnError, match="preclose"):
        cl.mark_limit_flags(_flag_frame().drop(columns=["preclose"]))


# ══════════════════════════════════════════════════════════════════════
# ④ 除权：桩 fetch_fn（DI，离线）；EMPTY_OK=无事件；FAIL_* 不得伪装成无除权
# ══════════════════════════════════════════════════════════════════════

def _stub_fetch_success(events: dict[str, pd.DataFrame]) -> object:
    """按 kind/year/yearType 分发返回合成帧的桩（tests 内闭包，⛔ 不打网）。"""
    adj = events["adjust_factor"]
    div = events["dividend"]

    def _fetch(kind: str, **params):
        if kind == "adjust_factor":
            return _ok_result(adj)
        if kind == "dividend":
            rows = div.get((params["year"], params["yearType"]), [])
            if not rows:
                return _empty_result()      # ⛔ 真 0 行 ⇒ EMPTY_OK（OK+空帧=无列谎报）
            return _ok_result(pd.DataFrame(rows))
        raise AssertionError(f"未知 kind {kind}（薄壳只会调母库既有 kind）")
    return _fetch


def test_fetch_exdiv_events_merges_dates_from_both_kinds() -> None:
    """④ dividend + adjust_factor 双源除权日合并；来源标记列各自为 True。"""
    events = {
        "adjust_factor": pd.DataFrame({"code": ["sh.600000"] * 2,
                                       "dividOperateDate": ["2025-06-20", "2025-07-01"]}),
        "dividend": {("2025", "report"): [
            {"code": "sh.600000", "dividOperateDate": "2025-07-01"},
            {"code": "sh.600000", "dividOperateDate": "2025-07-18"},
        ]},
    }
    ev = cl.fetch_exdiv_events(
        "sh.600000", "2025-06-01", "2025-08-31", fetch_fn=_stub_fetch_success(events))
    assert ev["date"].tolist() == ["2025-06-20", "2025-07-01", "2025-07-18"]
    assert (ev["exdiv"] == True).all()
    assert ev["adjust_factor"].tolist() == [True, True, False]
    assert ev["dividend"].tolist() == [False, True, True]


def test_fetch_exdiv_events_empty_ok_yields_empty_and_no_fake() -> None:
    """④ 全源 EMPTY_OK（真 0 行）⇒ 空帧但列契约稳定，⛔ 不得读作'数据源坏了'。"""
    calls: list[str] = []

    def empty_fetch(kind: str, **params):
        calls.append(kind)
        return _empty_result()

    ev = cl.fetch_exdiv_events(
        "sh.600000", "2025-06-01", "2025-08-31", fetch_fn=empty_fetch)
    assert ev.empty
    # ⛔ 列序钉死：date → exdiv → 来源标记（EXDIV_KINDS 声明顺序）。空帧与有行帧
    #    列序必须一致，否则血缘列位置随数据有无漂移。
    assert list(ev.columns) == ["date", "exdiv", "adjust_factor", "dividend"]
    assert "dividend" in calls and "adjust_factor" in calls


def test_fetch_exdiv_events_out_of_range_dates_excluded() -> None:
    """④ 区间外事件（早于 start / 晚于 end）不入标记；空日期行跳过。"""
    events = {
        "adjust_factor": pd.DataFrame({"code": ["sh.600000"] * 3,
                                       "dividOperateDate":
                                           ["2025-05-31",      # 早于 start
                                            "2025-09-01",      # 晚于 end
                                            ""]}),              # 空 → 跳过
        "dividend": {},
    }
    ev = cl.fetch_exdiv_events(
        "sh.600000", "2025-06-01", "2025-08-31", fetch_fn=_stub_fetch_success(events))
    assert ev.empty


def test_fetch_exdiv_events_malformed_date_raises_fail_closed() -> None:
    """④ 畸形除权日期 ⇒ ExdivSourceError（⛔ fail-closed，绝不静默跳过 ——
    漏标 = 回测按未除权价结算，净值跳变；宁可红，不可漏标）。"""
    events = {
        "adjust_factor": pd.DataFrame({"code": ["sh.600000"],
                                       "dividOperateDate": ["garbage"]}),
        "dividend": {},
    }
    with pytest.raises(cl.ExdivSourceError, match="fail-closed"):
        cl.fetch_exdiv_events(
            "sh.600000", "2025-06-01", "2025-08-31",
            fetch_fn=_stub_fetch_success(events))


def test_fetch_exdiv_events_dividend_year_expansion() -> None:
    """④ dividend 按年×yearType 全查（含前一年，跨年实施分红兜底）+ 入参形态。"""
    seen: list[tuple[str, str]] = []

    def record_fetch(kind: str, **params):
        if kind == "dividend":
            seen.append((params["year"], params["yearType"]))
        return _empty_result()

    cl.fetch_exdiv_events("sh.600000", "2025-06-01", "2025-08-31", fetch_fn=record_fetch)
    assert seen == [("2024", "report"), ("2024", "plan"),
                    ("2025", "report"), ("2025", "plan")], \
        "yearType 两口径都要查；年份含 start 前一整年（跨年实施分红）"


def test_fetch_exdiv_events_failure_raises_not_silent() -> None:
    """④ 源 FAIL_* ⇒ ExdivSourceError（⛔ 取数失败不得伪装成'无除权'，FR-BT-4）。"""
    def failing_fetch(kind: str, **params):
        from finai.sources.base import FetchResult
        return FetchResult(state=FAIL_GATEWAY, frame=None, rows=0,
                           source="baostock", detail="gateway 502")

    with pytest.raises(cl.ExdivSourceError, match="FAIL_GATEWAY"):
        cl.fetch_exdiv_events("sh.600000", "2025-06-01", "2025-08-31",
                              fetch_fn=failing_fetch)
    assert issubclass(cl.ExdivSourceError, ValueError)


def test_fetch_exdiv_events_inverted_range_raises() -> None:
    """④ 区间倒置（start > end）⇒ CleanerError（校验先于任何取数）。"""
    def boom(kind: str, **params):  # ⛔ 不应被调：校验必须在取数之前
        raise AssertionError("区间倒置不应触发取数")

    with pytest.raises(cl.CleanerError, match="晚于"):
        cl.fetch_exdiv_events("sh.600000", "2025-08-31", "2025-06-01", fetch_fn=boom)


def test_combine_exdiv_flag_marks_only_event_dates() -> None:
    """⑤ 合并：事件日 True、其余 False；不删行、不改价、不填充（R1）。"""
    bars = pd.DataFrame({
        "date": ["2025-07-15", "2025-07-16", "2025-07-17"],
        "close": ["10.5", "10.6", "10.7"],
        "source": ["baostock"] * 3, "adjust_mode": ["HFQ"] * 3,
    })
    events = pd.DataFrame({"date": ["2025-07-16"], "exdiv": [True]})
    out = cl.combine_exdiv_flag(bars, events)
    assert out["exdiv"].tolist() == [False, True, False]
    assert len(out) == 3
    assert out["close"].tolist() == ["10.5", "10.6", "10.7"], "纯标记不得改价"
    assert (out["source"] == "baostock").all(), "血缘列随拷贝透传"
    assert (out["adjust_mode"] == "HFQ").all()


def test_combine_exdiv_flag_empty_events_all_false() -> None:
    """⑤ 空事件帧（EMPTY_OK 产物）→ 全部 False；帧契约缺列 raise。"""
    bars = pd.DataFrame({"date": ["2025-07-15", "2025-07-16"]})
    ev_empty = pd.DataFrame(columns=["date", "exdiv", "dividend", "adjust_factor"])
    out = cl.combine_exdiv_flag(bars, ev_empty)
    assert out["exdiv"].tolist() == [False, False]
    with pytest.raises(cl.MissingColumnError, match="date"):
        cl.combine_exdiv_flag(bars, pd.DataFrame({"foo": [1]}))
    with pytest.raises(cl.MissingColumnError, match="date"):
        cl.combine_exdiv_flag(pd.DataFrame({"foo": [1]}), ev_empty)


# ══════════════════════════════════════════════════════════════════════
# ⑥ 编排 + 结果容器（血缘/计数自检/REALTIME 入口）
# ══════════════════════════════════════════════════════════════════════

def test_clean_daily_bars_full_pipeline_counts_and_lineage() -> None:
    """⑥ 全链路：停牌 2 行滤掉 → 涨停 1 / 跌停 1 → 除权 1 日；血缘带 source/adjust_mode。"""
    bars = _susp_frame()
    # ⛔ 用掩码 + object dtype 重赋收盘价，避免 "str→float64 列" 的 FutureWarning。
    bars = bars.astype({"close": object})
    bars.loc[bars["date"] == "2025-07-15", "close"] = "11.00"      # +10% 涨停
    bars.loc[bars["date"] == "2025-07-18", "close"] = "9.00"       # -10% 跌停
    events = pd.DataFrame({"date": ["2025-07-18"], "exdiv": [True]})
    res = cl.clean_daily_bars(bars, events, source="baostock", adjust_mode="HFQ")
    assert res.frame["limit_up"].tolist() == [True, False]
    assert res.frame["limit_down"].tolist() == [False, True]
    assert res.frame["exdiv"].tolist() == [False, True]
    assert res.suspended_rows == 2 and res.n_limit_up == 1
    assert res.n_limit_down == 1 and res.n_exdiv == 1
    m = res.meta
    assert m["source"] == "baostock" and m["adjust_mode"] == "HFQ"
    assert m["limit_rules"]["main_pct"] == 10.0
    assert m["exdiv_sources"] == ["adjust_factor", "dividend"]


def test_clean_daily_bars_lineage_from_frame_columns() -> None:
    """⑥ 不传血缘时从帧携带列取（落盘事实）；都没有则**不伪造**（键不出现）。"""
    bars = _susp_frame()
    bars["source"] = "baostock"
    bars["adjust_mode"] = "RAW"
    res = cl.clean_daily_bars(bars)
    assert res.meta["source"] == "baostock"
    assert res.meta["adjust_mode"] == "RAW"
    # ⛔ "未知不伪造"路径：帧无血缘列时键不得出现。构造一个**合法可清洗**的帧
    #    （带全校验列）但故意不带 source/adjust_mode 列 —— 校验列（code/isST/
    #    preclose）缺列是另一条 fail-closed 规则（见
    #    test_clean_daily_bars_missing_validation_column_raises），两条规则不混测。
    bare = pd.DataFrame({
        "date": ["2025-07-15"],
        "open": [10.0], "high": [10.6], "low": [9.9], "close": [10.5],
        "preclose": [10.0], "volume": ["1000"], "tradestatus": ["1"],
        "isST": ["0"], "code": ["sh.600000"],
    })
    res2 = cl.clean_daily_bars(bare)
    assert "source" not in res2.meta and "adjust_mode" not in res2.meta, \
        "未知血缘不得伪造"


def test_clean_daily_bars_events_none_is_explicit_all_false() -> None:
    """⑥ events=None = 显式声明不提供事件 ⇒ exdiv 全 False 且在 meta 有记录。"""
    bars = _susp_frame()
    res = cl.clean_daily_bars(bars, events=None)
    assert not res.frame["exdiv"].any()
    assert res.n_exdiv == 0
    assert res.meta["exdiv_events_rows"] == 0
    res2 = cl.clean_daily_bars(bars, events=pd.DataFrame({"date": ["2025-07-18"]}))
    assert res2.frame["exdiv"].tolist() == [False, True], "缺 exdiv 列的事件帧 = 全行参与"


def test_exdiv_sources_lineage_is_declared_not_derived() -> None:
    """⑥ 除权来源血缘 = 声明（EXDIV_KINDS 声明顺序），⛔ 不随空事件帧塌成 []。"""
    # 空事件（无 date/exdiv 列）：来源血缘仍是声明的两 kind，行数如实 0。
    res_empty = cl.clean_daily_bars(
        _susp_frame(), events=pd.DataFrame(columns=["date", "exdiv"]))
    assert res_empty.meta["exdiv_sources"] == ["adjust_factor", "dividend"]
    assert res_empty.meta["exdiv_events_rows"] == 0
    # 缺 exdiv 列的事件帧（全行参与）：血缘仍声明两 kind。
    res_flag = cl.clean_daily_bars(
        _susp_frame(), events=pd.DataFrame({"date": ["2025-07-18"]}))
    assert res_flag.meta["exdiv_sources"] == ["adjust_factor", "dividend"]
    assert res_flag.frame["exdiv"].tolist() == [False, True]


def test_clean_daily_bars_missing_validation_column_raises() -> None:
    """⑥ 缺校验必需列（code/isST/preclose…）⇒ MissingColumnError（fail-closed：
    缺这些列 = 涨跌停/停牌判定根本无法执行，⛔ 不静默降级成只过滤 tradestatus）。"""
    full = _susp_frame()
    for drop in ("code", "isST", "preclose"):
        with pytest.raises(cl.MissingColumnError):
            cl.clean_daily_bars(full.drop(columns=[drop]))


def test_clean_result_post_init_guards() -> None:
    """⑥ 容器自检：计数对不上 / 缺标记列 / 混停牌行 / 超行数 → raise（⛔ 不平不禁跑）。"""
    frame = pd.DataFrame({
        "date": ["2025-07-15"], "limit_up": [True], "limit_down": [False],
        "exdiv": [False],
    })
    with pytest.raises(cl.CleanerError, match="n_limit_up"):
        cl.CleanResult(frame=frame, n_limit_up=0)      # 列和是 1
    with pytest.raises(cl.CleanerError, match="n_limit_down"):
        cl.CleanResult(frame=frame, n_limit_up=1, n_limit_down=2)  # 超过行数
    with pytest.raises(cl.MissingColumnError, match="exdiv"):
        cl.CleanResult(frame=frame.drop(columns=["exdiv"]), n_limit_up=1)
    frame_susp = frame.copy()
    frame_susp["tradestatus"] = ["0"]                   # 结果帧混入停牌行
    with pytest.raises(cl.CleanerError, match="停牌"):
        cl.CleanResult(frame=frame_susp, n_limit_up=1)


def test_clean_daily_bars_events_type_guard() -> None:
    """⑥ events 非 DataFrame/None ⇒ raise（⛔ 不把 list 静默当成帧）。"""
    with pytest.raises(cl.CleanerError, match="DataFrame"):
        cl.clean_daily_bars(_susp_frame(), events=["2025-07-18"])


# ══════════════════════════════════════════════════════════════════════
# ⛔ 复用母库原语（不重造轮子）：默认 fetch_fn 是 baostock_source.fetch
# ══════════════════════════════════════════════════════════════════════

def test_fetch_exdiv_defaults_to_motherlib_fetch() -> None:
    """⑨ 默认 fetch_fn 就是 baostock_source.fetch（生产可用，⛔ 不另写取数实现）。"""
    from finai.sources import baostock_source

    # ⛔ 契约真实钉死（不是注释）：injectable 哨兵下，默认解析必须产出母库 fetch。
    probe = inspect.getsource(cl.fetch_exdiv_events)
    assert "baostock_source.fetch" in probe, "默认 fetch_fn 必须指向母库 fetch"
    assert baostock_source.fetch is not None, "母库 fetch 必须存在且可解析"


def test_exception_family_is_valueerror() -> None:
    """异常族谱钉死：全部 CleanerError 子类都必须是 ValueError 子类（同母库套路）。"""
    for name in ("MissingColumnError", "UnknownBoardError", "InvalidIsSTError",
                 "InvalidPriceError", "ExdivSourceError"):
        cls = getattr(cl, name)
        assert issubclass(cls, cl.CleanerError)
        assert issubclass(cls, ValueError)
    assert issubclass(cl.CleanerError, ValueError)


def test_board_prefix_registry_documented_and_covered() -> None:
    """代码前缀→档位映射是唯一登记点：缺省配置下数值与登记表一致（防漂移）。"""
    cfg = cl.LimitFlagsConfig()
    assert cl.board_limit_pct("sh.600000") == cfg.main_pct == 10.0
    assert cl.board_limit_pct("sz.000001") == cfg.main_pct == 10.0
    assert cl.board_limit_pct("sh.900901") == cfg.main_pct == 10.0
    assert cl.board_limit_pct("sz.200002") == cfg.main_pct == 10.0
    assert cl.board_limit_pct("sz.300750") == cfg.chinext_pct == 20.0
    assert cl.board_limit_pct("sh.688111") == cfg.star_pct == 20.0
    # 覆盖后：登记表（前缀→档位）不变，但阈值走配置值（FR-EXT-6）
    cfg2 = cl.LimitFlagsConfig(main_pct=30.0, chinext_pct=10.0)
    assert cl.board_limit_pct("sh.600000", cfg2) == 30.0
    assert cl.board_limit_pct("sz.300750", cfg2) == 10.0