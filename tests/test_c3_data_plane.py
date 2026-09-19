# -*- coding: utf-8 -*-
"""C3 数据面（scripts/lab/c3_data_plane.py）离线单测——全合成 fixture，不触真实数据。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from scripts.lab import c3_data_plane as c3


# ---------------------------------------------------------------- helpers

def _pool(tmp: Path, rows: dict[int, list[str]]) -> Path:
    df = pd.DataFrame([{"year": y, "symbols": sorted(s)} for y, s in rows.items()])
    p = tmp / "pool_yearly.parquet"
    df.to_parquet(p, index=False)
    return p


def _bars(dates: list[str], close: float = 10.0) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame({
        "date": dates, "open": [close] * n, "high": [close] * n,
        "low": [close] * n, "close": [close] * n, "preclose": [close] * n,
        "volume": [1e6] * n, "amount": [1e7] * n, "turn": [0.5] * n,
        "pctChg": [0.0] * n, "tradestatus": [1] * n, "isST": ["0"] * n,
        "code": ["sz.000001"] * n, "source": ["t"] * n,
        "adjust_mode": ["raw"] * n,
    })


def _stock_basic() -> pd.DataFrame:
    return pd.DataFrame([
        {"code": "sh.600000", "ipoDate": "2010-01-01", "outDate": "",
         "type": "1", "status": "1"},
        {"code": "sz.000001", "ipoDate": "2010-01-01", "outDate": "2015-06-30",
         "type": "1", "status": "1"},
        {"code": "sz.000002", "ipoDate": "2016-02-01", "outDate": "",
         "type": "1", "status": "1"},
        {"code": "sh.000300", "ipoDate": "2005-01-01", "outDate": "",
         "type": "2", "status": "1"},
    ])


# ---------------------------------------------------------------- pool_union

class TestPoolUnion:
    def test_union_sorted_unique(self, tmp_path):
        p = _pool(tmp_path, {2015: ["sz.000002", "sh.600000", "sh.600000"],
                             2016: ["sz.000001", "sh.600000"]})
        assert c3.pool_union(p) == ["sh.600000", "sz.000001", "sz.000002"]


# ---------------------------------------------------------------- alla_exdiv_events

class TestAllaExdivEvents:
    def test_schema_and_caliber(self, tmp_path, monkeypatch):
        alla = pd.DataFrame([
            # 实施 + 有 ex_date → 收入（cash_div_tax 优先）
            {"div_proc": "实施", "ex_date": "20230615", "cash_div": 0.91,
             "cash_div_tax": 0.91, "stk_bo_rate": None, "stk_co_rate": 0.4},
            # 预案 → 滤除
            {"div_proc": "预案", "ex_date": "20230801", "cash_div": 0.5,
             "cash_div_tax": 0.5, "stk_bo_rate": None, "stk_co_rate": None},
            # 实施但无 ex_date → 滤除
            {"div_proc": "实施", "ex_date": None, "cash_div": 0.3,
             "cash_div_tax": 0.3, "stk_bo_rate": None, "stk_co_rate": None},
            # 同日两笔实施 → 合并：cash 求和、factor 求积
            {"div_proc": "实施", "ex_date": "20220720", "cash_div": 0.10,
             "cash_div_tax": 0.10, "stk_bo_rate": 0.2, "stk_co_rate": None},
            {"div_proc": "实施", "ex_date": "20220720", "cash_div": 0.05,
             "cash_div_tax": 0.05, "stk_bo_rate": None, "stk_co_rate": 0.1},
            # cash_div_tax 缺省 → 回落 cash_div
            {"div_proc": "实施", "ex_date": "20210610", "cash_div": 0.20,
             "cash_div_tax": None, "stk_bo_rate": None, "stk_co_rate": None},
        ])
        d = tmp_path / "alla"
        d.mkdir()
        alla.to_parquet(d / "sz.000001.parquet", index=False)
        monkeypatch.setattr(c3, "DIV_ALLA", d)

        evs = c3.alla_exdiv_events("sz.000001")
        assert [e["date"] for e in evs] == ["2021-06-10", "2022-07-20",
                                            "2023-06-15"]
        assert evs[0]["cash_dividend"] == pytest.approx(0.20)
        assert evs[0]["factor"] == pytest.approx(1.0)
        assert evs[1]["cash_dividend"] == pytest.approx(0.15)
        assert evs[1]["factor"] == pytest.approx(1.2 * 1.1)
        assert evs[2]["cash_dividend"] == pytest.approx(0.91)
        assert evs[2]["factor"] == pytest.approx(1.4)

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(c3, "DIV_ALLA", tmp_path / "nope")
        assert c3.alla_exdiv_events("sz.000001") == []


# ---------------------------------------------------------------- provider

class TestYearlyPoolProvider:
    def test_year_snapshot_and_alive_filter(self, tmp_path):
        p = _pool(tmp_path, {2015: ["sh.600000", "sz.000001"],
                             2016: ["sh.600000", "sz.000002"]})
        prov = c3.make_yearly_pool_provider(p, _stock_basic())
        # 2015 年：A 在市、B 退市前正常参与
        assert prov(date(2015, 3, 1)) == ["sh.600000", "sz.000001"]
        # B outDate=2015-06-30：退市日当天起剔除
        assert prov(date(2015, 8, 1)) == ["sh.600000"]
        # 2016 年：C 上市日 2016-02-01，之前不在市
        assert prov(date(2016, 1, 15)) == ["sh.600000"]
        assert prov(date(2016, 3, 1)) == ["sh.600000", "sz.000002"]
        # 池内无该年条目 → fail-closed 空集
        assert prov(date(2017, 3, 1)) == []
        # 兼容字符串日期
        assert prov("2015-03-01") == ["sh.600000", "sz.000001"]


# ---------------------------------------------------------------- materialize

class TestMaterialize:
    def _sandbox(self, tmp_path, monkeypatch):
        """搭好 487 成员 + 新成员 + 指数 + alla 的微型仓库。"""
        div_root = tmp_path / "dividend_stocks"
        (div_root / "sh.600000").mkdir(parents=True)
        (div_root / "sh.000300").mkdir(parents=True)
        (div_root / "exdiv").mkdir(parents=True)
        member = _bars(["2015-01-05", "2015-01-06"], 12.0)
        member["market_cap"] = [9e10, 9e10]
        member["dividend_yield"] = [0.04, 0.04]
        member.to_parquet(div_root / "sh.600000/2015.parquet", index=False)
        _bars(["2015-01-05"], 3000.0).to_parquet(
            div_root / "sh.000300/2015.parquet", index=False)
        pd.DataFrame([{"date": "2015-06-01", "factor": 1.0,
                       "cash_dividend": 0.42}]).to_parquet(
            div_root / "exdiv/sh.600000.parquet", index=False)

        bars_dir = tmp_path / "daily_bars"
        bars_dir.mkdir()
        _bars(["2015-01-05", "2016-01-04"], 10.0).to_parquet(
            bars_dir / "sz.000001.parquet", index=False)

        dv_dir = tmp_path / "daily_basic"
        dv_dir.mkdir()
        pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20150105",
                       "circ_mv": 8e5},
                      {"ts_code": "600000.SH", "trade_date": "20150105",
                       "circ_mv": 9e6}]).to_parquet(
            dv_dir / "20150105.parquet", index=False)

        alla_dir = tmp_path / "alla"
        alla_dir.mkdir()
        pd.DataFrame([
            {"div_proc": "实施", "ex_date": "20150410", "cash_div": 0.5,
             "cash_div_tax": 0.5, "stk_bo_rate": None, "stk_co_rate": 0.2},
        ]).to_parquet(alla_dir / "sz.000001.parquet", index=False)

        monkeypatch.setattr(c3, "DIV_STOCKS", div_root)
        monkeypatch.setattr(c3, "BARS_ALIVE", bars_dir)
        monkeypatch.setattr(c3, "BARS_DELISTED", tmp_path / "none")
        monkeypatch.setattr(c3, "DV_DIR", dv_dir)
        monkeypatch.setattr(c3, "DIV_ALLA", alla_dir)
        monkeypatch.setattr(c3, "INDEX_SYMBOL", "sh.000300")
        monkeypatch.setattr(c3, "load_st_intervals",
                            lambda: {"sz.000001": [("20150101", "20150131")]})
        return div_root

    def test_copy_487_member_byte_identical(self, tmp_path, monkeypatch):
        div_root = self._sandbox(tmp_path, monkeypatch)
        pool = _pool(tmp_path, {2015: ["sh.600000", "sz.000001"]})
        out = tmp_path / "c3_universe"
        m = c3.materialize(pool, out, (2015, 2016))
        assert (out / "sh.600000/2015.parquet").read_bytes() == \
            (div_root / "sh.600000/2015.parquet").read_bytes()
        assert m["copied_487"] == 1 and m["built_new"] == 1

    def test_new_member_enrichment(self, tmp_path, monkeypatch):
        self._sandbox(tmp_path, monkeypatch)
        pool = _pool(tmp_path, {2015: ["sh.600000", "sz.000001"],
                                2016: ["sz.000001"]})
        out = tmp_path / "c3_universe"
        c3.materialize(pool, out, (2015, 2016))
        df15 = pd.read_parquet(out / "sz.000001/2015.parquet")
        # market_cap = circ_mv×1e4（仅分片覆盖日；2015-01-05 有值）
        row = df15[df15["date"] == "2015-01-05"].iloc[0]
        assert row["market_cap"] == pytest.approx(8e5 * 1e4)
        # isST 由 namechange 重建（2015-01 在 ST 区间 → '1'）
        assert row["isST"] == "1"
        # exdiv sidecar 由 alla 构建（factor=1+0.2）
        ex = pd.read_parquet(out / "exdiv/sz.000001.parquet")
        assert ex.iloc[0]["factor"] == pytest.approx(1.2)
        assert ex.iloc[0]["cash_dividend"] == pytest.approx(0.5)
        # 年度切分
        assert (out / "sz.000001/2016.parquet").exists()
        # 487 成员 exdiv 复制
        ex_old = pd.read_parquet(out / "exdiv/sh.600000.parquet")
        assert ex_old.iloc[0]["cash_dividend"] == pytest.approx(0.42)
        # 指数分区复制
        assert (out / "sh.000300/2015.parquet").exists()

    def test_new_member_mc_merge_date_object(self, tmp_path, monkeypatch):
        """回归：真实 daily_bars 的 date 列是 datetime.date 对象（非 'YYYY-MM-DD'
        字符串）——circ 合并按归一化字符串键，否则 market_cap 全 NaN。"""
        self._sandbox(tmp_path, monkeypatch)
        bars_p = tmp_path / "daily_bars/sz.000001.parquet"
        bars = pd.read_parquet(bars_p)
        bars["date"] = [date(2015, 1, 5), date(2016, 1, 4)]
        bars.to_parquet(bars_p, index=False)
        pool = _pool(tmp_path, {2015: ["sz.000001"], 2016: ["sz.000001"]})
        out = tmp_path / "c3_universe"
        m = c3.materialize(pool, out, (2015, 2016))
        df15 = pd.read_parquet(out / "sz.000001/2015.parquet")
        assert df15["market_cap"].iloc[0] == pytest.approx(8e5 * 1e4)
        # 2015-01-05 命中、2016-01-04 无分片 → 行覆盖 1/2
        assert m["mc_row_coverage"] == pytest.approx(0.5)

    def test_missing_bars_registered(self, tmp_path, monkeypatch):
        self._sandbox(tmp_path, monkeypatch)
        pool = _pool(tmp_path, {2015: ["sz.999999"]})
        out = tmp_path / "c3_universe"
        m = c3.materialize(pool, out, (2015, 2015))
        assert m["missing_bars"] == ["sz.999999"]

    def test_manifest_idempotent(self, tmp_path, monkeypatch):
        self._sandbox(tmp_path, monkeypatch)
        pool = _pool(tmp_path, {2015: ["sh.600000", "sz.000001"]})
        out = tmp_path / "c3_universe"
        m1 = c3.materialize(pool, out, (2015, 2016))
        m2 = c3.materialize(pool, out, (2015, 2016))
        assert m1["records"] == m2["records"]
        assert m1["union_symbols"] == m2["union_symbols"]
