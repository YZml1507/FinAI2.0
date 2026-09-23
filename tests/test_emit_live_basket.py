"""emit_live_basket._plan：整手迭代剔除 + min_pos 语义测试（纯函数零 IO）。"""
from datetime import date as _date
from decimal import Decimal as D

import pandas as pd

from scripts.emit_live_basket import Snap, _plan, _veto_banned_at
from strategy.veto import load_veto_series


def _snap(symbol: str, close: str, trading: bool = True,
          at_limit: bool = False, is_st: bool = False) -> Snap:
    return Snap(symbol=symbol, last_date=pd.Timestamp("2026-09-22"),
                close=D(close), amount20=D("10000000"),
                trading=trading, at_limit=at_limit, is_st=is_st)


class TestPlan:
    def test_equal_weight_lot_rounding(self) -> None:
        # 10 万 / 2 票 → 每票 5 万；close=60 → 833.33→800 股（整手向下）
        rows, left = _plan([_snap("a", "60"), _snap("b", "60")],
                           D("100000"), D("5000"))
        assert [r[1] for r in rows] == [800, 800]
        assert left == D("100000") - D("48000") * 2

    def test_min_pos_drop_and_recompute(self) -> None:
        # 3 票各 1 万目标；c 票 close=150 → 66.6→0 股不合规剔除
        # → 剩 2 票各 1.5 万
        rows, _ = _plan(
            [_snap("a", "10"), _snap("b", "10"), _snap("c", "150")],
            D("30000"), D("5000"))
        assert [r[0].symbol for r in rows] == ["a", "b"]
        assert all(v >= D("5000") for _, _, v in rows)

    def test_all_below_min_pos_empty(self) -> None:
        rows, left = _plan([_snap("a", "900")], D("3000"), D("5000"))
        assert rows == [] and left == D("3000")

    def test_min_pos_boundary_inclusive(self) -> None:
        # 5000/5000 恰达线：close=50 → 100 股 = 5000 == min_pos 保留
        rows, _ = _plan([_snap("a", "50")], D("5000"), D("5000"))
        assert rows and rows[0][1] == 100


class TestVeto:
    def test_load_veto_series_normalizes(self, tmp_path) -> None:
        p = tmp_path / "v.parquet"
        pd.DataFrame({"date": ["2026-09-21", "2026-09-22"],
                      "symbols": [["000001.SZ", "600000.SH"], ["300750.SZ"]]}
                     ).to_parquet(p)
        v = load_veto_series(p)
        assert v[_date(2026, 9, 21)] == frozenset({"sz.000001", "sh.600000"})
        assert v[_date(2026, 9, 22)] == frozenset({"sz.300750"})

    def test_banned_at_picks_latest_le_asof(self) -> None:
        v = {_date(2026, 9, 18): frozenset({"sz.000001"}),
             _date(2026, 9, 21): frozenset({"sz.300750"}),
             _date(2026, 9, 25): frozenset({"sh.600000"})}
        assert _veto_banned_at(v, _date(2026, 9, 22)) == frozenset({"sz.300750"})
        assert _veto_banned_at(v, _date(2026, 9, 21)) == frozenset({"sz.300750"})

    def test_banned_at_empty_before_first(self) -> None:
        v = {_date(2026, 9, 21): frozenset({"sz.300750"})}
        assert _veto_banned_at(v, _date(2026, 9, 18)) == frozenset()
