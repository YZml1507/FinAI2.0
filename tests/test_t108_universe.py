#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T108（股票池/成分回放，FR-DATA-5）单测 —— 全部**离线**，⛔ 无任何网络调用。

对应任务要求（tasks.md:27 / spec.md:74 / 数据字典 v1 §3）：
  ① 历史存活池：mock ``query_stock_basic`` 表 → 上市前剔除、退市后剔除；
  ② 回放无前瞻：2015 历史日的池内**不含** ``ipoDate > as_of`` 或
     ``outDate <= as_of`` 的标的；⛔ ``status``（当前快照）不参与判定；
  ③ 不可回放指数（CSI1000）抛 ``IndexNotReplayableError``（``allow_unverified`` 也拦）；
  ④ 回放未实证指数（HS300/ZZ500/SZ50）历史日默认抛 ``IndexReplayUnverifiedError``；
     显式 ``allow_unverified=True`` 才放行，血缘标 ``replay_verified=False``（monkeypatch 薄壳，不打网）；
  ⑤ 当前快照（``as_of=None``）放行，血缘标 ``replay_verified=True``（monkeypatch）。

⛔⛔ 永不静默原则：断言不被 try/except 吞错。在线薄壳一律以
``monkeypatch.setattr(data.universe, "_query_index_once", ...)`` 打桩 —— 不触网。
"""
from __future__ import annotations

import pandas as pd
import pytest

from data import universe as uni


# ---------------------------------------------------------------- 合成 stock_basic 表

def _basic_table() -> pd.DataFrame:
    """模拟 ``query_stock_basic`` 的字符串形态（8 行，覆盖全部边界）。

    以 ``as_of = 2015-06-30`` 为基准设计（见每个用例的期望注释）。
    """
    return pd.DataFrame({
        "code": [
            "sh.600000",  # 老股，未退市            → 2015-06-30 存活
            "sh.600601",  # 延中实业，2015-05-21 退市 → 剔除（退市后）
            "sz.000001",  # 老股，未退市            → 存活
            "sz.300750",  # 2018-06-11 才上市       → 2015 年剔除（未上市）
            "sh.601398",  # 2006-10-27 上市         → 存活
            "sz.000002",  # 2015-06-30 当日上市     → 存活（上市首日算上市，字典 §3.2:205）
            "sh.600001",  # 2015-06-30 当日退市     → 剔除（退市日当天起不可用，字典 §3.2:203）
            "sh.000001",  # type='2' 指数           → 剔除（非股票）
        ],
        "ipoDate": [
            "1999-11-10", "1990-12-19", "1991-04-03", "2018-06-11",
            "2006-10-27", "2015-06-30", "2002-01-01", "1990-12-19",
        ],
        "outDate": [
            "", "2015-05-21", "", "", "", "", "2015-06-30", "",
        ],
        "type": ["1", "1", "1", "1", "1", "1", "1", "2"],
        # ⛔ status 是"当前"快照：sh.600601 当前已退市('0')，但 2015-06-30 之前它是活的——
        # 回放绝不能用它判定（否则把历史中仍存活的股票提前剔除 = 未来函数）。
        "status": ["1", "0", "1", "1", "1", "1", "0", "1"],
    })


AS_OF_2015 = "2015-06-30"


# ---------------------------------------------------------------- ① 历史存活池

def test_alive_universe_2015_expected_membership() -> None:
    """① 2015-06-30 存活池：老股在、退市后不在、未上市不在、非股票不在。"""
    snap = uni.alive_universe(_basic_table(), AS_OF_2015)
    codes = set(snap.codes)
    assert codes == {"sh.600000", "sz.000001", "sh.601398", "sz.000002"}, codes
    # 剔除原因对号入座
    assert "sh.600601" not in codes, "2015-05-21 已退市，2015-06-30 不得在池"
    assert "sz.300750" not in codes, "2018 才上市，2015 年不得在池"
    assert "sh.000001" not in codes, "指数（type=2）不入股票池"
    assert "sh.600001" not in codes, "退市日（2015-06-30）当天起不可用"


def test_listing_day_is_in_delisting_day_is_out() -> None:
    """① 边界语义钉死：上市首日入池（字典 §3.2:205），退市日当天起出局（§3.2:203）。"""
    snap = uni.alive_universe(_basic_table(), AS_OF_2015)
    assert "sz.000002" in snap      # ipoDate == as_of → 入池
    assert "sh.600001" not in snap  # outDate == as_of → 出局


def test_exclusion_counts_recorded_in_meta() -> None:
    """① 血缘：剔除计数可审计（未上市/已退市/非股票各就各位）。"""
    snap = uni.alive_universe(_basic_table(), AS_OF_2015)
    m = snap.meta
    assert m["as_of"] == AS_OF_2015
    assert m["excluded_not_yet_listed"] == 1    # sz.300750
    assert m["excluded_delisted"] == 2          # sh.600601 + sh.600001（退市日当天）
    assert m["excluded_non_stock"] == 1         # sh.000001（type=2）
    assert m["alive"] == len(snap) == 4
    assert "status 列未参与判定" in m["rule"]


def test_status_column_is_not_used_avoids_survivorship_bias() -> None:
    """② 反未来函数核心：status='0'（当前已退市）但 outDate 在 as_of 之后 ⇒
    历史时点仍必须存活。若实现误用 status，这里会把它从池里挤掉。"""
    table = _basic_table()
    # sh.600601 之外再造一只：当前已退市（status='0'），但退市日 2018 年
    table.loc[len(table)] = ["sh.601988", "2006-07-05", "2018-03-01", "1", "0"]
    snap = uni.alive_universe(table, AS_OF_2015)
    assert "sh.601988" in snap, "用 status 判定 = 未来函数：2018 才退市，2015 年应在池"
    assert "sh.600601" not in snap  # 对照组：真实退市日早于 as_of，剔除靠 outDate


# ---------------------------------------------------------------- ③ 回放无前瞻

def test_replay_has_no_lookahead_for_any_member() -> None:
    """③ 硬断言：池内每只股票都满足 ``ipoDate <= as_of < outDate``（逐只核对）。"""
    table = _basic_table()
    snap = uni.alive_universe(table, AS_OF_2015)
    by_code = table.set_index("code")
    for code in snap.codes:
        row = by_code.loc[code]
        assert row["ipoDate"] <= AS_OF_2015, f"{code} 上市日晚于回放日 = 前瞻"
        out = row["outDate"]
        assert out == "" or AS_OF_2015 < out, f"{code} 退市日早于/等于回放日 = 前瞻"


def test_later_as_of_admits_newly_listed() -> None:
    """③ 时点移动：2018-06-11 上市的股票，回放 2018-07-02 时必须入池（单调性）。"""
    table = _basic_table()
    snap_2015 = uni.alive_universe(table, "2015-06-30")
    snap_2018 = uni.alive_universe(table, "2018-07-02")
    assert "sz.300750" not in snap_2015
    assert "sz.300750" in snap_2018
    # 2018-07-02 时 sh.600601（2015 退市）仍不在
    assert "sh.600601" not in snap_2018


def test_universe_snapshot_helpers() -> None:
    """③ 容器契约：__contains__/__len__/__iter__/to_frame。"""
    snap = uni.alive_universe(_basic_table(), AS_OF_2015)
    assert len(snap) == len(list(snap)) == len(snap.codes)
    frame = snap.to_frame()
    assert list(frame.columns) == ["code", "as_of"]
    assert len(frame) == len(snap)
    assert (frame["as_of"] == AS_OF_2015).all()


# ---------------------------------------------------------------- 入参守卫

def test_missing_required_columns_rejected() -> None:
    """缺必需列 ⇒ ValueError（⛔ 不静默降级；与母库 tradestatus 强制同套路）。"""
    table = _basic_table().drop(columns=["outDate"])
    with pytest.raises(ValueError, match="outDate"):
        uni.alive_universe(table, AS_OF_2015)


def test_canon_date_validation() -> None:
    """日期规范化：两种合法形态 + 非法即抛。"""
    assert uni.canon_date("20150630") == "2015-06-30"
    assert uni.canon_date("2015-06-30") == "2015-06-30"
    with pytest.raises(ValueError, match="非法日期"):
        uni.canon_date("2015-13-40")
    with pytest.raises(ValueError, match="非法日期"):
        uni.canon_date("garbage")


def test_unknown_ipo_rows_excluded_and_counted() -> None:
    """``ipoDate`` 缺失 ⇒ 保守剔除并计数（宁缺勿错，防漏网的未来函数）。"""
    table = _basic_table()
    table.loc[len(table)] = ["sz.000099", "", "", "1", "1"]   # ipoDate 未知
    snap = uni.alive_universe(table, AS_OF_2015)
    assert "sz.000099" not in snap
    assert snap.meta["excluded_unknown_ipo"] == 1


# ---------------------------------------------------------------- ④/⑤ 指数回放闸门

def test_csi1000_always_raises_even_with_allow_unverified() -> None:
    """③' 不可回放指数：CSI1000 无接口 ⇒ 恒抛，``allow_unverified`` 也拦。"""
    with pytest.raises(uni.IndexNotReplayableError, match="CSI1000"):
        uni.index_constituents("csi1000", "2015-06-30")
    with pytest.raises(uni.IndexNotReplayableError):
        uni.index_constituents("csi1000", "2015-06-30", allow_unverified=True)
    with pytest.raises(uni.IndexNotReplayableError):
        uni.index_constituents("zz1000")          # 别名同理
    with pytest.raises(uni.IndexNotReplayableError, match="未知指数"):
        uni.index_constituents("sp500")           # 未登记的指数名：⛔ 不静默猜测


def test_historical_replay_blocked_without_explicit_consent() -> None:
    """④ HS300/ZZ500/SZ50 历史日默认抛（回放未实证），不许静默返回当前快照。"""
    for idx in ("hs300", "zz500", "sz50"):
        with pytest.raises(uni.IndexReplayUnverifiedError, match="未实证"):
            uni.index_constituents(idx, AS_OF_2015)
        with pytest.raises(uni.IndexReplayUnverifiedError):
            uni.index_constituents(idx, "20150630")   # YYYYMMDD 形态同样拦
    # 异常族谱：ValueError 子类（与母库 UnknownAdjustment 同套路）
    assert issubclass(uni.IndexReplayUnverifiedError, ValueError)
    assert issubclass(uni.IndexNotReplayableError, ValueError)


def _patch_index(monkeypatch: pytest.MonkeyPatch, frame: pd.DataFrame) -> None:
    """把在线薄壳 ``_query_index_once`` 换成桩 —— ⛔ 离线，绝不连 baostock。"""
    monkeypatch.setattr(
        uni, "_query_index_once", lambda key, day: frame.copy())


def test_allow_unverified_passes_and_flags_lineage(monkeypatch: pytest.MonkeyPatch) -> None:
    """④ 显式放行后：返回成分 + 血缘 ``replay_verified=False`` + ``gap`` 缺口标注。"""
    _patch_index(monkeypatch, pd.DataFrame({
        "code": ["sh.600000", "sz.000001", "sh.600000"],   # 含重复，应去重
        "code_name": ["浦发银行", "平安银行", "浦发银行"],
    }))
    snap = uni.index_constituents("hs300", AS_OF_2015, allow_unverified=True)
    assert snap.codes == ("sh.600000", "sz.000001")        # 去重 + 排序
    assert snap.index == "hs300" and snap.as_of == AS_OF_2015
    assert snap.meta["replay_verified"] is False
    assert "未实证" in snap.meta["gap"]
    assert snap.meta["source"] == "baostock.query_hs300_stocks"
    assert len(snap) == 2


def test_current_snapshot_is_allowed_and_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """⑤ ``as_of=None``（当前快照）= 探针实证路径 ⇒ 放行且 ``replay_verified=True``。"""
    _patch_index(monkeypatch, pd.DataFrame({
        "code": ["sh.600519", "sz.000858"], "code_name": ["贵州茅台", "五粮液"]}))
    snap = uni.index_constituents("hs300")
    assert snap.as_of is None
    assert snap.meta["replay_verified"] is True
    assert "gap" not in snap.meta
    assert snap.codes == ("sh.600519", "sz.000858")
    frame = snap.to_frame()
    assert (frame["index"] == "hs300").all()


def test_index_missing_code_column_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回帧缺 ``code`` 列 ⇒ RuntimeError（⛔ 不伪造空成分冒充成功）。"""
    _patch_index(monkeypatch, pd.DataFrame({"updateDate": ["2026-01-01"]}))
    with pytest.raises(RuntimeError, match="code"):
        uni.index_constituents("sz50")
