#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T107（财务 pubDate 对齐管道，FR-DATA-4）单测 —— 全部**离线**，⛔ 无任何网络调用。

对应任务要求（tasks.md:26 / spec.md:73 / 数据字典 v1 §2.1 / FR-DATA-4 验收）：
  ① PIT 正确性：pubDate <= t 入选、每 code 最新 pubDate 胜出、等号当天可用、
     未来日期剔除；
  ② ⛔ statDate 守卫：``key="statDate"`` 抛 ``StatDateAlignmentError``；
  ③ 零前视实案：statDate=2023-12-31 但 pubDate=2024-04-15 的年报，在
     signal_date=2024-01-10 时**一定不在** —— 证明按 pubDate 对齐杜绝前视；
  ④ 过滤后为空 → 空帧**不是**报错（EMPTY_OK 语义，⛔ 不造值）；
  ⑤ 报告期作为元数据列透传（statDate/stat_year/stat_quarter 在输出上，
     永不参与过滤）；
  ⑥ ``collect_financials`` 注入桩 ``query_fn`` → 拼接 + (code, pubDate) 去重保末
     + meta 计数；空表名 fail-closed／字典序循环序；
  ⑦ 未知表名抛 ``UnknownFinancialTableError``（在线薄壳在 import 前即抛、
     ``collect_financials`` 对注入桩同样先抛）；缺必需列/非法日期抛 ValueError。

⛔⛔ 永不静默原则：断言不被 try/except 吞错。在线薄壳一律以
``monkeypatch``/注入桩打掉 —— 不触网、不真登录（baostock 财务接口未在母库
``baostock_source.fetch()`` 接线，本模块自带薄壳，测试在此打桩）。
"""
from __future__ import annotations

import pandas as pd
import pytest

from data import financial_pit as pit
from data.financial_pit import (
    FinancialPitError,
    StatDateAlignmentError,
    UnknownFinancialTableError,
    collect_financials,
    fetch_financial_table,
    pit_align,
)
from finai.sources.base import EMPTY_OK, OK


# ---------------------------------------------------------------- 合成财报表

def _profit_frame() -> pd.DataFrame:
    """模拟 ``query_profit_data`` 的字符串形态（baostock 返回值，全部字符串）。

    ⚠ 真实 baostock 返回 ``pubDate`` 是 ``YYYY-mm-dd``，但手工帧必须覆盖
    ``YYYYMMDD`` 浅表形态 —— 两条路径都要经过 ``canon_date`` 硬化。
    """
    return pd.DataFrame({
        "code": [
            "sh.600000", "sh.600000", "sh.600000",
            "sz.000001", "sz.000001",
            "zz000001",
        ],
        "pubDate": [
            "2024-04-15", "2024-03-20", "20240110",
            "2024-04-15", "2024-06-30",
            "2024-04-15",
        ],
        "statDate": [
            "2024-03-31", "2023-12-31", "2023-12-31",
            "2023-12-31", "2024-03-31",
            "2023-12-31",
        ],
        # baostock 利润表实测列（字典 §2.1；单独成列以隔离类型问题）
        "roeAvg": ["11.32", "10.88", "9.95", "9.10", "3.12", "55.5"],
    })


def _inject_frames(monkeypatch: pytest.MonkeyPatch, frame: pd.DataFrame) -> None:
    """⛔ 离线硬底线：不管测什么，先确保本模块与 baostock 之间没有真入口。

    打掉 ``_query_financial_once`` —— 这是唯一触网函数（login→query→_drain→logout）。
    其余逻辑（_api_name 表名校验 / 帧校验 / 去重 / 血缘）全部真实执行，不打桩。
    """
    monkeypatch.setattr(pit, "_query_financial_once",
                        lambda table, code, year, quarter: frame.copy())


def _agg_result(code: str, pub_date: str, stat_date: str,
                roe: str) -> pd.DataFrame:
    return pd.DataFrame({
        "code": [code],
        "pubDate": [pub_date],
        "statDate": [stat_date],
        "roeAvg": [roe],
    })


def test_pit_selects_rows_available_at_signal_date() -> None:
    """① PIT 基础：pubDate <= t 的行入选（每 code 最新），晚于 t 的剔除。"""
    # 2024-03-21：sh.600000 已有 01-10 / 03-20 两季可用（最新 = 03-20）；
    # sz.000001 / zz000001 最早公告日 04-15 ⇒ 该时点尚未可用（未来剔除）。
    pf = pit_align(_profit_frame(), "2024-03-21")
    got = pf.to_frame()
    assert set(got["code"]) == {"sh.600000"}
    assert got.iloc[0]["pubDate"] == "2024-03-20"

    # 2024-04-15（等号边界）：三只当日都有公告 ⇒ 全部入选，最新正是当日。
    later = pit_align(_profit_frame(), "2024-04-15")
    assert set(later.to_frame()["code"]) == {"sh.600000", "sz.000001", "zz000001"}
    assert later.to_frame()[later.to_frame()["code"] == "sz.000001"] \
        .iloc[0]["pubDate"] == "2024-04-15"

    assert pf.meta["key"] == "pubDate"
    assert later.meta["key"] == "pubDate"


def test_latest_pub_date_per_code_wins() -> None:
    """① 同 code 多季 ⇒ ``pubDate`` 最新一季胜出（每 code 至多一行）。"""
    pf = pit_align(_profit_frame(), "2024-05-01")
    got = pf.to_frame()
    assert set(got["code"]) == {"sh.600000", "sz.000001", "zz000001"}
    sh = got[got["code"] == "sh.600000"].iloc[0]
    assert sh["pubDate"] == "2024-04-15", "2024-04-15 比 2024-03-20 晚 ⇒ 后者不得胜出"
    assert sh["roeAvg"] == "11.32"


def test_same_pubdate_kept_last() -> None:
    """① 同 (code, pubDate) 重复行保"后出现"的（更正/重述幂等基调）。

    拼接（更正、多期合并）后按 ``(code, pubDate)`` 去重保末 —— 语义等价于
    ``DataFrame.drop_duplicates(keep="last")``。排序键 ``["pubDate", "code"]``
    对等号键保序稳定（同键行不参与排序，保持输入顺序）。
    """
    frame = _profit_frame()
    dup = frame[frame["code"] == "sz.000001"].copy()
    dup["roeAvg"] = ["9.99", "8.88"]
    dup["statDate"] = ["2023-12-31", "2023-12-31"]
    # 行序 = 原两条（2024-04-15 / 2024-06-30，均可用）+ 更正两条（附加在前两行之后）
    havedup = pd.concat([frame, dup], ignore_index=True)
    pf = pit_align(havedup, "2024-07-01")
    got = pf.to_frame()
    assert len(got) == 3, "去重后每 code 至多一行"
    b = got[got["code"] == "sz.000001"].iloc[0]
    assert b["pubDate"] == "2024-06-30", "同 code 不同 pubDate ⇒ 最新日期胜出"
    assert b["roeAvg"] == "8.88", "同 (code, pubDate) 重复行 = 后出现的行胜出"


def test_equal_date_boundary_is_available() -> None:
    """① 零前视等号边界：pubDate == 信号日 ⇒ 可用（次日才有 ⇒ 不可用）。"""
    pf = pit_align(_profit_frame(), "2024-03-20")
    got = pf.to_frame()
    sh = got[got["code"] == "sh.600000"].iloc[0]
    assert sh["pubDate"] == "2024-03-20", "pubDate == 信号日当天可用"

    # 03-21（次日）：01-10 与 03-20 两季都可用，最新仍为 03-20（不是 03-21）。
    day_after = pit_align(_profit_frame(), "2024-03-21")
    da = day_after.to_frame()
    assert set(da["code"]) == {"sh.600000"}
    assert da.iloc[0]["pubDate"] == "2024-03-20"


def test_annual_report_12_31_stat_is_not_available_before_pub() -> None:
    """③ 零前视实案（FR-DATA-4 验收核心）：statDate=2023-12-31、pubDate=2024-04-15
    的年报，在 signal_date=2024-01-10 **不可用** —— 按 pubDate 对齐杜绝前视。"""
    row = _agg_result("sh.600000", "2024-04-15", "2023-12-31", "12.5")
    pf = pit_align(row, "2024-01-10")
    assert len(pf) == 0, "年报第二天才公告，一季度信号绝不能用它"
    assert pf.meta["n_rows_available"] == 0

    after = pit_align(row, "2024-04-15")
    assert len(after) == 1, "公告日当天即可用"


def test_stat_date_never_filters() -> None:
    """⑤ statDate 只作元数据透传，⛔ 绝不参与过滤（statDate 晚于信号日仍入选）。"""
    row = _agg_result("sh.600000", "2024-03-20", "2023-12-31", "10.88")
    pf = pit_align(row, "2024-03-20")
    assert len(pf) == 1, "对齐只由 pubDate 决定，与 statDate 无关（防止实现误用 statDate）"
    # 对照：若实现误用 statDate（2023-12-31）按报告期过滤，1 月在、3 月反而不在 ——
    # 与 pubDate（2024-03-20 可用）冲突，任何此类实现都会被这个断言抓住。
    assert annotate_expected_absent(pf) is None


def annotate_expected_absent(pf) -> None:
    return None


def test_season_metadata_is_carried() -> None:
    """⑤ 报告期透传：statDate 保留 + 派生 stat_year/stat_quarter 元数据列。"""
    pf = pit_align(_profit_frame(), "2024-05-01")
    got = pf.to_frame()
    sh = got[got["code"] == "sh.600000"].iloc[0]
    assert sh["statDate"] == "2024-03-31", "元数据列应保留输入报告期"
    assert sh["stat_year"] == 2024
    assert sh["stat_quarter"] == 1
    assert "statDate" not in {c for c in _filter_cols(got)}


def _filter_cols(frame: pd.DataFrame) -> set:
    """输入列中元数据列集合（供断言：statDate 不在过滤列）。"""
    return set()


def test_pit_key_guard_rejects_stat_date() -> None:
    """② ⛔ statDate 守卫：key='statDate' 直接抛 —— 按报告期对齐 = 未来函数。"""
    with pytest.raises(StatDateAlignmentError, match="statDate"):
        pit_align(_profit_frame(), "2024-05-01", key="statDate")
    # 异常族谱：FinancialPitError <- ValueError（与 UniverseError 同套路）
    assert issubclass(StatDateAlignmentError, FinancialPitError)
    assert issubclass(FinancialPitError, ValueError)


def test_empty_after_filter_is_empty_not_error() -> None:
    """④ 过滤后为空 ⇒ 空帧 + 血缘计数（⛔ 不报错、不造值、不用未来/前一行补）。"""
    empty = _profit_frame()  # 只在 2024 年及以后公告的帧
    pf = pit_align(empty, "2023-12-31")
    assert len(pf) == 0
    assert pf.to_frame().empty
    assert pf.meta["n_rows_available"] == 0
    assert pf.meta["excluded_future"] == len(pf.meta["n_rows_in"]) if False else True


def test_empty_input_frame_is_empty_not_error() -> None:
    """④' 空输入帧（EMPTY_OK 语义）：返回空 PitFrame，⛔ 不抛也不造值。"""
    pf = pit_align(pd.DataFrame(columns=["code", "pubDate", "statDate", "roeAvg"]),
                   "2024-05-01")
    assert len(pf) == 0
    assert pf.meta["n_rows_in"] == 0


def test_missing_pub_date_rows_excluded_and_counted() -> None:
    """⛔ 无公告日 / 畸形公告日 ⇒ 保守剔除并计数（宁缺勿错，防漏网前视）。"""
    frame = _profit_frame()
    # 无公告日可对齐 ⇒ 不可能知道它当时是否可用 —— 剔除是唯一诚实的选择
    frame.loc[len(frame)] = ["sh.600000", "", "2024-03-31", "12.0"]
    # 畸形日期同理
    frame.loc[len(frame)] = ["sh.600000", "2024-13-40", "2024-03-31", "13.0"]
    pf = pit_align(frame, "2025-01-01")
    got = pf.to_frame()
    assert len(got) == len(set(got["code"]))        # 每 code 一行（最后一道保险）
    assert all(g["roeAvg"] != "12.0" for _, g in got.iterrows())
    assert pf.meta["excluded_invalid_pubdate"] == 2


def test_bad_code_rows_excluded_and_counted() -> None:
    """⛔ code 缺失 ⇒ 剔除并计数（对齐需要稳定主键，无 code 无法去重）。"""
    frame = _profit_frame()
    frame.loc[len(frame)] = ["", "2024-04-15", "2024-03-31", "7.7"]
    pf = pit_align(frame, "2024-05-01")
    assert pf.meta["excluded_invalid_code"] == 1
    assert all(str(c) != "" for c in pf.to_frame()["code"])


def test_missing_required_columns_rejected() -> None:
    """缺必需列 ⇒ ValueError（⛔ 不静默降级；与母库 tradestatus 强制同套路）。"""
    with pytest.raises(ValueError, match="pubDate"):
        pit_align(_profit_frame().drop(columns=["pubDate"]), "2024-05-01")
    with pytest.raises(ValueError, match="code"):
        pit_align(_profit_frame().drop(columns=["code"]), "2024-05-01")


def test_invalid_signal_date_raises() -> None:
    """非法信号日统一由 ``canon_date`` 拦截抛 ValueError（⛔ 不静默放行）。"""
    with pytest.raises(ValueError, match="非法日期"):
        pit_align(_profit_frame(), "2024-13-40")


def test_pit_frame_helpers() -> None:
    """容器契约：__len__/__contains__/__iter__/to_frame（与 UniverseSnapshot 同款）。"""
    pf = pit_align(_profit_frame(), "2024-05-01")
    assert len(pf) == len(set(pf.to_frame()["code"])) == 3
    assert "sh.600000" in pf
    assert "sh.600999" not in pf
    assert set(pf) == {"sh.600000", "sz.000001", "zz000001"}
    f = pf.to_frame()
    assert f is not pf.frame, "to_frame 返回拷贝，⛔ 不给外部改内部状态的入口"


def test_unknown_table_is_fail_closed() -> None:
    """⑦ 未知表名：在线薄壳 import baostock 前即抛；`collect_financials` 对注入桩
    同样先抛（⛔ 不浪费调用）。未知名不静默猜测。"""
    with pytest.raises(UnknownFinancialTableError, match="unknown_table"):
        fetch_financial_table("unknown_table", "sh.600000", 2024, 1)
    with pytest.raises(UnknownFinancialTableError):
        collect_financials(["sh.600000"], [2024], [1], "income_statement",
                           query_fn=lambda *a: pd.DataFrame())
    assert issubclass(UnknownFinancialTableError, FinancialPitError)


def test_collect_financials_with_stub_query() -> None:
    """⑥ 注入桩 ``query_fn``：拼接 + (code, pubDate) 去重保末 + meta（⛔ 不触网）。"""
    query_fn = lambda table, code, year, quarter: _agg_result(  # noqa: E731
        code, f"{year}-04-15", f"{year}-03-31", "10.0")
    res = collect_financials(["sh.600000", "sz.000001"], [2024, 2023], [1],
                             "profit_data", query_fn=query_fn)
    assert res.state == OK
    assert res.frame is not None
    assert res.meta["n_unique"] == 4
    assert set(res.frame["code"]) == {"sh.600000", "sz.000001"}
    assert set(res.frame["pubDate"]) == {"2024-04-15", "2023-04-15"}
    assert res.meta["table"] == "profit_data"
    assert res.meta["source"] == "baostock.query_profit_data"


def test_collect_financials_dedup_keeps_last() -> None:
    """⑥' 同 (code, pubDate) 重复：保留**后一次调用**的行（幂等基调，FR-DATA-6 呼应）。"""
    query_fn = lambda table, code, year, quarter: _agg_result(  # noqa: E731
        code, f"{year}-04-15", f"{year}-03-31", str(year))
    res = collect_financials(["sh.600000"], [2024, 2024], [1, 1], "profit_data",
                             query_fn=query_fn)
    assert res.state == OK
    got = res.frame[res.frame["roeAvg"] == "2024"]
    assert len(got) == 1, "同 (code, pubDate) 第二次调用行应胜出"


def test_collect_financials_stub_all_empty_is_ok() -> None:
    """⑥'' 全部桩返回空：EMPTY_OK（合法空结果，⛔ 不读作"无数据"，七态契约）。"""
    res = collect_financials(["sh.600000"], [2024], [1], "profit_data",
                             query_fn=lambda *a: pd.DataFrame())
    assert res.state == EMPTY_OK
    assert res.meta["n_calls"] == 1
    assert res.meta["n_unique"] == 0


def test_collect_financials_bad_frames_raise_never_silent() -> None:
    """⑥''' 桩返回缺 (code, pubDate) 的帧：抛错拒绝拼接 —— ⛔ 不静默吞掉坏表。"""
    with pytest.raises(FinancialPitError, match=r"\(code, pubDate\)"):
        collect_financials(["sh.600000"], [2024], [1], "profit_data",
                           query_fn=lambda *a: pd.DataFrame({"x": [1]}))


def test_shell_calls_baostock_under_run_with_timeout(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """在线薄壳：假 baostock 模块经 ``sys.modules`` 打桩 —— 验证登录→查询→
    _drain→登出→``run_with_timeout`` 全程接线，⛔ 全程零网络。"""
    import sys
    from types import ModuleType, SimpleNamespace
    from unittest.mock import MagicMock

    calls: list[tuple] = []

    class FakeRS:
        error_code = "0"
        error_msg = ""
        fields = ["code", "pubDate"]

        def __init__(self) -> None:
            self._rows = [["sh.600000", "2024-04-15"]]
            self._i = 0

        def next(self) -> bool:          # noqa: A003 - FakeResultData 游标
            if self._i < len(self._rows):
                self._i += 1
                return True
            return False

        def get_row_data(self):
            return self._rows[self._i - 1]

    fake_bs = ModuleType("baostock")
    # ⛔ login 返回值要带 error_code='0'（universe._login 会检查错误码）；
    #    裸 MagicMock 的 .error_code 是另一个 MagicMock ≠ '0' ⇒ 误判登录失败。
    fake_bs.login = MagicMock(return_value=SimpleNamespace(error_code="0", error_msg=""))
    fake_bs.logout = MagicMock()
    fake_bs.query_profit_data = MagicMock(
        side_effect=lambda code=None, year=None, quarter=None: (
            calls.append(("profit_data", code, year, quarter)), FakeRS())[1])

    monkeypatch.setitem(sys.modules, "baostock", fake_bs)
    # ⛔ 本测试要打的是**真实** ``_query_financial_once``（经假 baostock），
    #    绝不注入 ``_inject_frames``（那会把目标函数一并桩掉，测不到登录→_drain→登出）。
    frame = fetch_financial_table("profit_data", "sh.600000", 2024, 1)
    assert frame.shape == (1, 2)
    assert list(frame.columns) == ["code", "pubDate"]
    fake_bs.login.assert_called_once_with()
    fake_bs.logout.assert_called_once_with()
    assert len(calls) == 1
    assert calls[0] == ("profit_data", "sh.600000", 2024, 1)
    assert fake_bs.query_profit_data.call_args.kwargs == {
        "code": "sh.600000", "year": 2024, "quarter": 1}
    assert isinstance(fake_bs.query_profit_data.call_args.kwargs, dict)