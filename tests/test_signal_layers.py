#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Alpha 三层信号离线单测（修池子/排雷/PEAD + 策略集成）。

全部合成数据、零打网：builder 是纯函数（DataFrame 进/出），SignalLayers
查询对象内存构造，策略侧用 MockBook/MockBroker 验证意图。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from strategy.signal_layers import (
    LandmineEvent,
    SignalLayers,
    build_landmine_frame,
    build_pead_frame,
    build_quality_veto_frame,
)
from strategy.candidates import DividendConfig, DividendStrategy
from strategy.portfolio import PortfolioConfig
from backtest.types import Bar


# ==============================================================================
# 合成数据工厂
# ==============================================================================

def _bars(n: int, start: date = date(2020, 1, 2), close: float = 10.0,
          dy: float = 0.04) -> pd.DataFrame:
    """连续 n 个自然日（测试只关心相对次序，不查交易日历）。"""
    days = [start + timedelta(days=i) for i in range(n)]
    return pd.DataFrame({
        "date": days, "open": [close] * n, "close": [close] * n,
        "preclose": [close] * n, "pctChg": [0.0] * n,
        "dividend_yield": [dy] * n, "market_cap": [1e10] * n,
    })


def _exdiv(entries: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame({
        "date": [date.fromisoformat(d) for d, _ in entries],
        "cash_dividend": [c for _, c in entries],
        "factor": [1.0] * len(entries),
    })


def _pit(rows: list[tuple[str, str, float, float]]) -> pd.DataFrame:
    """rows: (pub_date, stat_date, roe, ocfps)。"""
    return pd.DataFrame({
        "code": ["sh.T"] * len(rows),
        "pub_date": [d for d, _, _, _ in rows],
        "stat_date": [s for _, s, _, _ in rows],
        "roe": [r for _, _, r, _ in rows],
        "net_profit_yoy": [10.0] * len(rows),
        "deducted_net_profit_yoy": [10.0] * len(rows),
        "debt_to_assets": [50.0] * len(rows),
        "cash_flow_per_share": [o for _, _, _, o in rows],
        "source": ["test"] * len(rows),
    })


#: 一份"健康"基线：4 年分红 + ROE 20% + ocfps 2.0（分红 0.4/年 比例 20%）
def _healthy_inputs(n_bars: int = 400, start: date = date(2019, 1, 2)):
    bars = _bars(n_bars, start=start, dy=0.04)
    exdiv = _exdiv([(f"{y}-06-15", 0.4) for y in
                    range(start.year - 5, start.year + 4)])
    pit = _pit([
        ("2017-04-01", "2016-12-31", 20.0, 2.0),
        ("2018-04-01", "2017-12-31", 20.0, 2.0),
        ("2019-04-01", "2018-12-31", 20.0, 2.0),
        ("2020-04-01", "2019-12-31", 20.0, 2.0),
        ("2020-08-01", "2020-06-30", 10.0, 1.0),
        ("2021-04-01", "2020-12-31", 20.0, 2.0),
        ("2021-08-01", "2021-06-30", 10.0, 1.0),
        ("2022-04-01", "2021-12-31", 20.0, 2.0),
    ])
    return bars, exdiv, pit


# ==============================================================================
# ① 质量否决 build_quality_veto_frame
# ==============================================================================

class TestQualityVeto:

    def test_healthy_passes(self):
        bars, exdiv, pit = _healthy_inputs()
        out = build_quality_veto_frame(bars, exdiv, pit)
        late = out.iloc[-1]
        assert late["veto"] is False or late["veto"] == False  # noqa: E712

    def test_q1_insufficient_dividend_years(self):
        """只有 1 年分红史 → Q1 否决。"""
        bars, _, pit = _healthy_inputs()
        exdiv = _exdiv([("2020-06-15", 0.4)])
        out = build_quality_veto_frame(bars, exdiv, pit)
        last = out.iloc[-1]
        assert bool(last["veto"])
        assert "Q1" in last["reasons"]

    def test_q2_low_roe_ttm(self):
        """年报 ROE 5% → ROE-TTM<10 → Q2 否决。"""
        bars, exdiv, _ = _healthy_inputs()
        pit = _pit([
            ("2019-04-01", "2018-12-31", 5.0, 2.0),
            ("2020-04-01", "2019-12-31", 5.0, 2.0),
            ("2021-04-01", "2020-12-31", 5.0, 2.0),
        ])
        out = build_quality_veto_frame(bars, exdiv, pit)
        assert any("Q2" in r for r in out["reasons"].iloc[-1].split(";"))

    def test_q2_interim_ttm_formula(self):
        """非年报行 TTM = ytd + 上年年报 − 上年同期（TTM=10+20-5=25 通过）。"""
        bars, exdiv, _ = _healthy_inputs()
        pit = _pit([
            ("2019-04-01", "2018-12-31", 20.0, 2.0),
            ("2019-08-01", "2019-06-30", 5.0, 1.0),
            ("2020-04-01", "2019-12-31", 20.0, 2.0),
            ("2020-08-01", "2020-06-30", 10.0, 1.0),   # TTM=10+20-5=25
        ])
        out = build_quality_veto_frame(bars, exdiv, pit)
        # 2020-08-01 之后：roe_ttm=25 >10 → 无 Q2
        tail = out[pd.to_datetime(out["date"]).dt.date >= date(2020, 8, 2)]
        assert not any("Q2" in r for r in tail["reasons"])

    def test_q2_pit_invisible_future(self):
        """pub_date 在未来的报表不可见 → 沿用旧值（零前视）。"""
        bars, exdiv, _ = _healthy_inputs(n_bars=60,
                                       start=date(2020, 1, 2))
        pit = _pit([
            ("2019-04-01", "2018-12-31", 20.0, 2.0),   # 老报告 roe=20
            ("2030-04-01", "2029-12-31", 1.0, 2.0),    # 未来报告 roe=1
        ])
        out = build_quality_veto_frame(bars, exdiv, pit)
        # 2020 年段的 bar：最新可见仍是 roe=20 的老报告 → 无 Q2
        assert not any("Q2" in r for r in out["reasons"])

    def test_q3_payout_exceeds_ocf(self):
        """3 年分红 1.2 vs 3 年 ocfps 合计 1.0 → 比例 1.2>0.8 → Q3。"""
        bars, _, _ = _healthy_inputs()
        exdiv = _exdiv([(f"{y}-06-15", 0.4) for y in range(2018, 2023)])
        pit = _pit([
            ("2019-04-01", "2018-12-31", 20.0, 0.3),
            ("2020-04-01", "2019-12-31", 20.0, 0.3),
            ("2021-04-01", "2020-12-31", 20.0, 0.3),
        ])
        out = build_quality_veto_frame(bars, exdiv, pit)
        assert any("Q3" in r for r in out["reasons"].iloc[-1].split(";"))

    def test_q3_fin_sector_skipped(self):
        """金融类（fin_sector=True）跳过 Q3：ocfps≤0 仍分红不否决。"""
        bars, _, _ = _healthy_inputs()
        exdiv = _exdiv([(f"{y}-06-15", 0.4) for y in range(2018, 2023)])
        pit = _pit([
            ("2019-04-01", "2018-12-31", 20.0, -5.0),
            ("2020-04-01", "2019-12-31", 20.0, -5.0),
        ])
        out = build_quality_veto_frame(bars, exdiv, pit, fin_sector=True)
        assert not any("Q3" in r for r in out["reasons"])

    def test_q4_pseudo_yield(self):
        """股价腰斩推高股息率 → Q4 伪高股息否决。"""
        n = 300
        bars = _bars(n, start=date(2019, 1, 2), close=20.0, dy=0.03)
        # 末段：价格 10（腰斩）、dy 0.06（升幅全由跌价贡献）
        bars.loc[n - 1, "close"] = 10.0
        bars.loc[n - 1, "dividend_yield"] = 0.06
        _, exdiv, pit = _healthy_inputs(n)
        out = build_quality_veto_frame(bars, exdiv, pit)
        assert "Q4" in out["reasons"].iloc[-1]

    def test_empty_pit_veto(self):
        """无 PIT 财务证据 → 全否决（fail-closed 宁缺勿错）。"""
        bars, exdiv, _ = _healthy_inputs()
        out = build_quality_veto_frame(bars, exdiv, None)
        assert out["veto"].all()


# ==============================================================================
# ② 排雷 build_landmine_frame
# ==============================================================================

def _forecast(rows: list[tuple]) -> pd.DataFrame:
    """rows: (pub_date, end_date, type, np_min, np_max)。"""
    return pd.DataFrame({
        "code": ["sh.T"] * len(rows),
        "pub_date": [r[0] for r in rows],
        "end_date": [r[1] for r in rows],
        "type": [r[2] for r in rows],
        "p_change_min": [None] * len(rows),
        "p_change_max": [None] * len(rows),
        "net_profit_min": [r[3] for r in rows],
        "net_profit_max": [r[4] for r in rows],
        "source": ["test"] * len(rows),
    })


def _stmts(rows: list[tuple]) -> pd.DataFrame:
    """rows: (pub_date, end_date, comp_type, revenue, ar, money_cap, st_borr,
    lt_borr, total_assets, int_income)。"""
    return pd.DataFrame({
        "code": ["sh.T"] * len(rows),
        "pub_date": [r[0] for r in rows],
        "end_date": [r[1] for r in rows],
        "comp_type": [r[2] for r in rows],
        "revenue": [r[3] for r in rows],
        "total_revenue": [r[3] for r in rows],
        "n_income_attr_p": [1e8] * len(rows),
        "int_income": [r[9] for r in rows],
        "int_exp": [None] * len(rows),
        "money_cap": [r[5] for r in rows],
        "trad_asset": [None] * len(rows),
        "accounts_receiv": [r[4] for r in rows],
        "accounts_receiv_bill": [0.0] * len(rows),
        "st_borr": [r[6] for r in rows],
        "lt_borr": [r[7] for r in rows],
        "bond_payable": [0.0] * len(rows),
        "non_cur_liab_due_1y": [0.0] * len(rows),
        "cb_borr": [0.0] * len(rows),
        "total_assets": [r[8] for r in rows],
        "total_liab": [None] * len(rows),
        "update_flag": ["0"] * len(rows),
        "source": ["test"] * len(rows),
    })


class TestLandmine:

    def test_l1a_forecast_loss_types(self):
        fc = _forecast([
            ("2021-01-15", "2020-12-31", "首亏", -5e7, -3e7),
            ("2022-01-15", "2021-12-31", "预增", 5e7, 8e7),
        ])
        out = build_landmine_frame("sh.T", None, fc, None)
        assert len(out) == 1
        assert out.iloc[0]["rule"] == "L1a"
        assert out.iloc[0]["action"] == "exit_full"

    def test_l1b_slight_decrease(self):
        fc = _forecast([("2021-01-15", "2020-12-31", "略减", 4e7, 6e7)])
        out = build_landmine_frame("sh.T", None, fc, None)
        assert out.iloc[0]["action"] == "block_only"

    def test_l1c_revision_down(self):
        fc = _forecast([
            ("2021-01-15", "2020-12-31", "预增", 5e7, 8e7),
            ("2021-03-15", "2020-12-31", "预增", 2e7, 4e7),  # 下修
        ])
        out = build_landmine_frame("sh.T", None, fc, None)
        assert "L1c" in out["rule"].tolist()

    def test_l2_divergence(self):
        pit = _pit([
            ("2019-04-01", "2018-12-31", 20.0, 2.0),
        ])
        pit["net_profit_yoy"] = [50.0]
        pit["deducted_net_profit_yoy"] = [-40.0]
        out = build_landmine_frame("sh.T", pit, None, None)
        assert out.iloc[0]["rule"] == "L2"
        assert out.iloc[0]["action"] == "block_only"

    def test_l3_annual_ocf_negative(self):
        pit = _pit([
            ("2020-04-01", "2019-12-31", 20.0, -1.0),  # 年报 ocfps≤0
            ("2020-08-01", "2020-06-30", 10.0, -1.0),  # 中报不触发 L3
        ])
        out = build_landmine_frame("sh.T", pit, None, None)
        assert len(out) == 1 and out.iloc[0]["rule"] == "L3"

    def test_l4_ar_divergence(self):
        st = _stmts([
            ("2019-04-01", "2018-12-31", "1", 1e9, 1e8, 1e9, 0.0, 0.0, 5e9, 3e7),
            ("2020-04-01", "2019-12-31", "1", 1e9, 2e8, 1e9, 0.0, 0.0, 5e9, 3e7),
        ])  # 应收 +100% vs 营收 0% → gap 100pp
        out = build_landmine_frame("sh.T", None, None, st)
        assert "L4" in out["rule"].tolist()

    def test_l4_skipped_for_banks(self):
        st = _stmts([
            ("2019-04-01", "2018-12-31", "2", 1e9, 1e8, 1e9, 0.0, 0.0, 5e9, 3e7),
            ("2020-04-01", "2019-12-31", "2", 1e9, 2e8, 1e9, 0.0, 0.0, 5e9, 3e7),
        ])  # comp_type=2（银行）→ L4/L5 不适用
        out = build_landmine_frame("sh.T", None, None, st)
        assert out.empty

    def test_l5_dual_high(self):
        st = _stmts([
            ("2020-04-01", "2019-12-31", "1", 1e9, 1e8,
             2e9, 1e9, 1e9, 5e9, 1e7),   # mc/TA=40%, ibd/TA=40%, int_yield=0.5%
        ])
        out = build_landmine_frame("sh.T", None, None, st)
        assert "L5" in out["rule"].tolist()

    def test_cooldown_mapping(self):
        fc = _forecast([("2021-01-15", "2020-12-31", "首亏", -5e7, -3e7)])
        out = build_landmine_frame("sh.T", None, fc, None)
        assert out.iloc[0]["cooldown_bars"] == 120


# ==============================================================================
# ③ PEAD build_pead_frame
# ==============================================================================

def _pead_pit(dyoys: list[float], pubs: list[str], stats: list[str]):
    return pd.DataFrame({
        "code": ["sh.T"] * len(pubs),
        "pub_date": pubs, "stat_date": stats,
        "roe": [15.0] * len(pubs),
        "net_profit_yoy": dyoys,
        "deducted_net_profit_yoy": dyoys,
        "debt_to_assets": [50.0] * len(pubs),
        "cash_flow_per_share": [2.0] * len(pubs),
        "source": ["t"] * len(pubs),
    })


class TestPead:

    def _calendar(self, n=600, start=date(2019, 1, 2)):
        return [start + timedelta(days=i) for i in range(n)]

    def test_sue_and_percentile(self):
        """两只票同月公告：高意外者分位高于低意外者。"""
        pubs_a = ["2018-04-01", "2018-08-01", "2019-04-01", "2019-08-01",
                  "2020-04-10"]
        stats_a = ["2017-12-31", "2018-06-30", "2018-12-31", "2019-06-30",
                   "2019-12-31"]
        pit_a = _pead_pit([10.0, 10.0, 10.0, 10.0, 60.0], pubs_a, stats_a)
        pubs_b = pubs_a
        pit_b = _pead_pit([10.0, 10.0, 10.0, 10.0, -60.0], pubs_b, stats_a)
        bars = _bars(600, start=date(2019, 1, 2))
        ev = build_pead_frame(
            {"sh.A": pit_a, "sh.B": pit_b},
            {"sh.A": bars, "sh.B": bars},
            self._calendar(), sue_hist_min=4, pct_min=0.9)
        a = ev[(ev.symbol == "sh.A") & (ev.pub_date == date(2020, 4, 10))]
        b = ev[(ev.symbol == "sh.B") & (ev.pub_date == date(2020, 4, 10))]
        assert len(a) == 1 and len(b) == 1
        assert a.iloc[0]["pct_rank"] > b.iloc[0]["pct_rank"]
        assert bool(a.iloc[0]["eligible"])
        assert not bool(b.iloc[0]["eligible"])

    def test_demax_excludes_prerunup(self):
        """公告前 20 日内开盘跳空 ≥7% → 排除（DEMAX 条件化）。"""
        pubs = ["2018-04-01", "2018-08-01", "2019-04-01", "2019-08-01",
                "2020-04-10"]
        stats = ["2017-12-31", "2018-06-30", "2018-12-31", "2019-06-30",
                 "2019-12-31"]
        pit = _pead_pit([10.0, 10.0, 10.0, 10.0, 60.0], pubs, stats)
        bars = _bars(600, start=date(2019, 1, 2))
        # 公告前第 5 个日历日放一个 8% 开盘跳空（preclose 口径）
        gap_idx = bars.index[bars["date"] == date(2020, 4, 5)][0]
        bars.loc[gap_idx, "open"] = 10.8   # 10.8/10 - 1 = 8%
        ev = build_pead_frame({"sh.A": pit}, {"sh.A": bars},
                              self._calendar(), sue_hist_min=4,
                              pct_min=0.5, pre_gap_days=300)
        a = ev[ev.pub_date == date(2020, 4, 10)]
        assert len(a) == 1 and "pre_gap" in a.iloc[0]["excl_reason"]

    def test_limit_touch_excluded(self):
        """公告日触板（pctChg ≥ 阈值−buffer）→ 排除。"""
        pubs = ["2018-04-01", "2018-08-01", "2019-04-01", "2019-08-01",
                "2020-04-10"]
        stats = ["2017-12-31", "2018-06-30", "2018-12-31", "2019-06-30",
                 "2019-12-31"]
        pit = _pead_pit([10.0, 10.0, 10.0, 10.0, 60.0], pubs, stats)
        bars = _bars(600, start=date(2019, 1, 2))
        hit = bars.index[bars["date"] == date(2020, 4, 10)][0]
        bars.loc[hit, "pctChg"] = 9.9      # 主板 10% 档触板
        ev = build_pead_frame({"sh.600005": pit}, {"sh.600005": bars},
                              self._calendar(), sue_hist_min=4,
                              pct_min=0.5)
        ev = ev[ev.symbol == "sh.600005"]
        a = ev[ev.pub_date == date(2020, 4, 10)]
        assert len(a) == 1 and "limit_touch" in a.iloc[0]["excl_reason"]

    def test_insufficient_history_no_event(self):
        """历史不足 sue_hist_min 期 → 不产生事件（宁缺勿错）。"""
        pit = _pead_pit([10.0, 60.0], ["2020-04-01", "2020-08-01"],
                        ["2019-12-31", "2020-06-30"])
        bars = _bars(300)
        ev = build_pead_frame({"sh.A": pit}, {"sh.A": bars},
                              self._calendar(400), sue_hist_min=4)
        assert ev.empty


# ==============================================================================
# ④ SignalLayers 查询对象
# ==============================================================================

class TestSignalLayersQuery:

    def _layers(self) -> SignalLayers:
        d = date(2020, 1, 6)
        veto = {"sh.A": ((d, date(2020, 1, 7)), (True, False), ("Q2:x", ""))}
        lm = {"sh.A": (LandmineEvent("sh.A", d, "L1a", "exit_full", 120),)}
        return SignalLayers(veto=veto, landmine=lm, pead_by_day={})

    def test_veto_reason_asof(self):
        ly = self._layers()
        assert ly.veto_reason("sh.A", date(2020, 1, 6)) == "Q2:x"
        assert ly.veto_reason("sh.A", date(2020, 1, 7)) is None
        assert ly.veto_reason("sh.A", date(2020, 1, 5)) is not None  # 无前数据→否决
        assert ly.veto_reason("sh.X", date(2020, 1, 7)) is not None  # 无数据→否决

    def test_landmine_list_sorted(self):
        ly = self._layers()
        assert len(ly.landmine_list("sh.A")) == 1
        assert ly.landmine_list("sh.Z") == ()

    def test_pead_active_window(self):
        from strategy.signal_layers import PeadEvent
        ev = PeadEvent("sh.A", date(2020, 4, 10), date(2019, 12, 31),
                       50.0, 45.0, 0.9, date(2020, 4, 10), date(2020, 6, 5))
        ly = SignalLayers(pead_by_day={"2020-04-10": [ev]})
        assert ly.pead_active(date(2020, 4, 10)) == [ev]
        assert not ly.pead_active(date(2020, 4, 11))


# ==============================================================================
# ⑤ 策略集成（DividendStrategy × 三层）
# ==============================================================================

class _Pos:
    def __init__(self, vol):
        self.volume = vol


class _Book:
    def __init__(self, nav, cash=None, positions=None):
        self.total_nav = nav
        self.cash = cash if cash is not None else nav
        self.positions = positions or {}


class _Broker:
    def __init__(self):
        self.orders = []

    def submit(self, order):
        self.orders.append(order)
        return order


def _bar(symbol, dy="0.05", mc="1e10", amount="1e9", close="10",
         d=date(2020, 6, 1)):
    return Bar(date=d, symbol=symbol, open=Decimal(close),
               high=Decimal(close), low=Decimal(close), close=Decimal(close),
               preclose=Decimal(close), volume=Decimal("1000000"),
               amount=Decimal(amount), dividend_yield=Decimal(dy),
               market_cap=Decimal(mc))


def _cfg(**kw) -> DividendConfig:
    base = dict(min_dividend_yield=Decimal("0.03"), candidate_pool_size=50,
                min_positions=3, max_positions=5, default_positions=3,
                use_ma200_timing=False, warmup_bars=200, rebalance_days=1)
    base.update(kw)
    return DividendConfig(**base)


class TestStrategyLayersIntegration:

    def test_vetoed_symbol_excluded(self):
        ly = SignalLayers(veto={
            "sh.600001": ((date(2020, 6, 1),), (True,), ("Q2:roe=5",)),
            "sh.600002": ((date(2020, 6, 1),), (False,), ("",)),
        })
        cfg = _cfg(use_quality_veto=True)
        st = DividendStrategy(config=cfg, signal_layers=ly)
        bars = {
            "sh.600001": _bar("sh.600001", dy="0.06"),
            "sh.600002": _bar("sh.600002", dy="0.05"),
        }
        sigs = st._select_stocks(bars, cfg, date(2020, 6, 1))
        assert {s.symbol for s in sigs} == {"sh.600002"}

    def test_cooldown_blocks_rebuy(self):
        ev = LandmineEvent("sh.600001", date(2020, 5, 20), "L1a",
                           "exit_full", 120,
                           cooldown_until=date(2020, 11, 1))
        ly = SignalLayers(landmine={"sh.600001": (ev,)})
        cfg = _cfg(use_landmine_overlay=True)
        st = DividendStrategy(config=cfg, signal_layers=ly)
        bars = {"sh.600001": _bar("sh.600001", dy="0.06"),
                "sh.600002": _bar("sh.600002", dy="0.05")}
        sigs = st._select_stocks(bars, cfg, date(2020, 6, 1))
        assert {s.symbol for s in sigs} == {"sh.600002"}
        # 冷却窗口过后解禁
        sigs2 = st._select_stocks(bars, cfg, date(2020, 11, 5))
        assert "sh.600001" in {s.symbol for s in sigs2}

    def test_landmine_full_exit_order(self):
        ev = LandmineEvent("sh.600001", date(2020, 6, 1), "L1a",
                           "exit_full", 120)
        ly = SignalLayers(landmine={"sh.600001": (ev,)})
        cfg = _cfg(use_landmine_overlay=True)
        st = DividendStrategy(config=cfg, signal_layers=ly)
        st.watchlist = ["sh.600001"]
        book = _Book(Decimal("150000"),
                     positions={"sh.600001": _Pos(1000)})
        brk = _Broker()
        bars = {"sh.600001": _bar("sh.600001")}
        st._apply_landmine(date(2020, 6, 1), bars, book, brk)
        sells = [o for o in brk.orders if o.side.value == "SELL"]
        assert len(sells) == 1 and sells[0].volume == 1000
        assert st._lm_pending["sh.600001"]   # exit_full 留队重试至出清

    def test_landmine_half_exit(self):
        ev = LandmineEvent("sh.600001", date(2020, 6, 1), "L2",
                           "exit_half", 60)
        ly = SignalLayers(landmine={"sh.600001": (ev,)})
        cfg = _cfg(use_landmine_overlay=True)
        st = DividendStrategy(config=cfg, signal_layers=ly)
        book = _Book(Decimal("150000"),
                     positions={"sh.600001": _Pos(1000)})
        brk = _Broker()
        bars = {"sh.600001": _bar("sh.600001")}
        st._apply_landmine(date(2020, 6, 1), bars, book, brk)
        sells = [o for o in brk.orders if o.side.value == "SELL"]
        assert sells and sells[0].volume == 500

    def test_landmine_suspended_retry(self):
        """停牌日（无 bar）事件不消费，留 pending 次日重试。"""
        ev = LandmineEvent("sh.600001", date(2020, 6, 1), "L1a",
                           "exit_full", 120)
        ly = SignalLayers(landmine={"sh.600001": (ev,)})
        cfg = _cfg(use_landmine_overlay=True)
        st = DividendStrategy(config=cfg, signal_layers=ly)
        book = _Book(Decimal("150000"),
                     positions={"sh.600001": _Pos(1000)})
        brk = _Broker()
        st._apply_landmine(date(2020, 6, 1), {}, book, brk)   # 无 bar
        assert not brk.orders
        assert st._lm_pending["sh.600001"]                     # 事件留队
        bars = {"sh.600001": _bar("sh.600001", d=date(2020, 6, 2))}
        st._apply_landmine(date(2020, 6, 2), bars, book, brk)
        assert len(brk.orders) == 1

    def test_pead_requires_breadth(self):
        with pytest.raises(ValueError):
            _cfg(use_pead=True,
                 breadth_series={"2020-06-01": Decimal("0.5")})

    def test_pead_buy_only_in_attack(self):
        from strategy.signal_layers import PeadEvent
        ev = PeadEvent("sh.600005", date(2020, 5, 20),
                       date(2019, 12, 31), 50.0, 45.0, 0.9,
                       date(2020, 5, 20), date(2020, 7, 15))
        ly = SignalLayers(pead_by_day={"2020-06-01": [ev]})
        pc = PortfolioConfig(min_positions=3, max_positions=8,
                             target_count=5, hard_limit=10,
                             min_position_value=Decimal("20000"),
                             min_daily_amount=Decimal("50000000"),
                             max_participation_rate=Decimal("0.05"))
        # 宽度 0.5 ≥ attack 0.4 → 进攻档建仓（event 模式，需 reserve 池）
        cfg = _cfg(use_breadth_timing=True, use_pead=True,
                   pead_entry_mode="event",
                   breadth_series={"2020-06-01": Decimal("0.5")},
                   pead_reserve_pct=Decimal("0.40"), pead_max_slots=2,
                   portfolio=pc)
        st = DividendStrategy(config=cfg, signal_layers=ly)
        st._breadth_today = Decimal("0.5")
        book = _Book(Decimal("150000"), cash=Decimal("150000"))
        brk = _Broker()
        bars = {"sh.600005": _bar("sh.600005", amount="2e9")}
        st._apply_pead(date(2020, 6, 1), bars, book, brk)
        buys = [o for o in brk.orders if o.side.value == "BUY"]
        assert buys and buys[0].symbol == "sh.600005"
        assert "sh.600005" in st._pead_holds

        # 宽度 0.3 < attack → 不建仓
        cfg2 = _cfg(use_breadth_timing=True, use_pead=True,
                    breadth_series={"2020-06-01": Decimal("0.3")},
                    pead_reserve_pct=Decimal("0.40"), pead_max_slots=2,
                    portfolio=pc)
        st2 = DividendStrategy(config=cfg2, signal_layers=ly)
        st2._breadth_today = Decimal("0.3")
        brk2 = _Broker()
        st2._apply_pead(date(2020, 6, 1), bars, book, brk2)
        assert not [o for o in brk2.orders if o.side.value == "BUY"]

    def test_pead_expiry_exit(self):
        from strategy.signal_layers import PeadEvent
        ly = SignalLayers()
        pc = PortfolioConfig(min_daily_amount=Decimal("50000000"))
        cfg = _cfg(use_breadth_timing=True, use_pead=True,
                   breadth_series={"2020-06-01": Decimal("0.5")},
                   pead_hold_days=5, portfolio=pc)
        st = DividendStrategy(config=cfg, signal_layers=ly)
        st._breadth_today = Decimal("0.5")
        st._pead_holds["sh.600005"] = 0
        st._bar_count = 6                       # 超过 hold_days=5
        book = _Book(Decimal("150000"),
                     positions={"sh.600005": _Pos(1000)})
        brk = _Broker()
        bars = {"sh.600005": _bar("sh.600005")}
        st._apply_pead(date(2020, 6, 1), bars, book, brk)
        sells = [o for o in brk.orders if o.side.value == "SELL"]
        assert sells and sells[0].volume == 1000
        assert "sh.600005" not in st._pead_holds

    def test_pead_rebalance_mode_merges_targets(self):
        """rebalance 模式：进攻档调仓日 PEAD 事件并入候选源，不占 reserve、
        不中途追高；并入成功的目标登记 _pead_holds 起算持有期。"""
        from strategy.signal_layers import PeadEvent
        ev = PeadEvent("sh.600005", date(2020, 5, 20),
                       date(2019, 12, 31), 50.0, 45.0, 0.9,
                       date(2020, 5, 20), date(2020, 7, 15))
        ly = SignalLayers(pead_by_day={"2020-06-01": [ev]})
        pc = PortfolioConfig(min_positions=2, max_positions=5,
                             target_count=3, hard_limit=5,
                             min_position_value=Decimal("10000"),
                             min_daily_amount=Decimal("1000"),
                             max_participation_rate=Decimal("1"))
        cfg = _cfg(use_breadth_timing=True, use_pead=True,
                   pead_entry_mode="rebalance",
                   breadth_series={"2020-06-01": Decimal("0.5")},
                   rebalance_days=1,
                   pead_max_slots=1, portfolio=pc)
        st = DividendStrategy(config=cfg, signal_layers=ly)
        st._bar_count = 200               # 跳过冷启动（warmup 下限 200）
        book = _Book(Decimal("150000"))
        brk = _Broker()
        bars = {"sh.600001": _bar("sh.600001", dy="0.06"),
                "sh.600002": _bar("sh.600002", dy="0.05"),
                # PEAD 票不满足股息率门槛——只能由事件源并入
                "sh.600005": _bar("sh.600005", dy="0.0")}
        st.on_bar(date(2020, 6, 1), bars, book, brk)
        buys = [o for o in brk.orders if o.side.value == "BUY"]
        assert "sh.600005" in {o.symbol for o in buys}
        assert "sh.600005" in st._pead_holds
        assert ev.event_id in st._pead_acted

    def test_pead_rebalance_mode_zero_reserve(self):
        """rebalance 模式恒不预留现金池（软叠加核心：零闲置拖累）。"""
        from strategy.signal_layers import PeadEvent
        ev = PeadEvent("sh.600005", date(2020, 4, 20),
                       date(2019, 12, 31), 50.0, 45.0, 0.9,
                       date(2020, 4, 20), date(2020, 6, 15))
        ly = SignalLayers(pead_by_day={})
        pc = PortfolioConfig(min_positions=2, max_positions=5,
                             target_count=2, hard_limit=5,
                             min_position_value=Decimal("10000"),
                             min_daily_amount=Decimal("1000"),
                             max_participation_rate=Decimal("1"))
        cfg = _cfg(use_breadth_timing=True, use_pead=True,
                   pead_entry_mode="rebalance",
                   pead_reserve_pct=Decimal("0.40"),
                   breadth_series={"2020-04-20": Decimal("0.5")},
                   rebalance_days=1,
                   portfolio=pc)
        st = DividendStrategy(config=cfg, signal_layers=ly)
        st._bar_count = 200               # 跳过冷启动（warmup 下限 200）
        st._pead_holds["sh.600009"] = 0   # 有在册也不触发 reserve
        book = _Book(Decimal("150000"))
        brk = _Broker()
        bars = {"sh.600001": _bar("sh.600001", dy="0.06"),
                "sh.600002": _bar("sh.600002", dy="0.05")}
        st.on_bar(date(2020, 4, 20), bars, book, brk)
        buys = [o for o in brk.orders if o.side.value == "BUY"]
        # 4 月是披露密集月+有在册——event 模式会预留 40%，rebalance 不留：
        # 15 万全额按 2 目标等权 ⇒ 每票 ≥ 7 万 > min_position_value
        assert len(buys) == 2
        for o in buys:
            assert Decimal(o.volume) * Decimal("10") >= Decimal("70000")

    def test_pead_ghost_unregister_after_grace(self):
        """幽灵在册注销：登记 ≥3 bar 仍无实际持仓 → 移出 _pead_holds。
        E4 审计实证：targets 阶段登记被 plan_positions 丢弃后从未买入
        （sz.000014 空挂 34 bar），或被排雷/冰点外部出清后仍占槽位、
        并被 scores 注入买回（300443 排雷/PEAD 互搏）。"""
        pc = PortfolioConfig(min_daily_amount=Decimal("50000000"))
        cfg = _cfg(use_breadth_timing=True, use_pead=True,
                   breadth_series={"2020-06-01": Decimal("0.5")},
                   pead_hold_days=30, portfolio=pc)
        st = DividendStrategy(config=cfg, signal_layers=SignalLayers())
        st._breadth_today = Decimal("0.5")
        book = _Book(Decimal("150000"))
        brk = _Broker()
        bars = {"sh.600005": _bar("sh.600005")}

        # 宽限期内（age<3）：登记未成交不注销（T+1/拒单缓冲）
        st._pead_holds["sh.600005"] = 0
        st._bar_count = 2
        st._apply_pead(date(2020, 6, 1), bars, book, brk)
        assert "sh.600005" in st._pead_holds

        # 超过宽限仍无持仓 → 幽灵注销
        st._bar_count = 3
        st._apply_pead(date(2020, 6, 1), bars, book, brk)
        assert "sh.600005" not in st._pead_holds

    def test_pead_zombie_unregister_external_sell(self):
        """僵尸在册注销：实际持仓被外部路径（排雷/冰点/警戒）出清后，
        登记簿同步移出——不再占槽位、不再被调仓注入买回。"""
        pc = PortfolioConfig(min_daily_amount=Decimal("50000000"))
        cfg = _cfg(use_breadth_timing=True, use_pead=True,
                   breadth_series={"2020-06-01": Decimal("0.5")},
                   pead_hold_days=30, portfolio=pc)
        st = DividendStrategy(config=cfg, signal_layers=SignalLayers())
        st._breadth_today = Decimal("0.5")
        # 登记于 bar 0，曾真实持仓，bar 5 时已被外部出清（vol=0）
        st._pead_holds["sh.600005"] = 0
        st._pead_acted.add("evt-x")
        st._bar_count = 5
        book = _Book(Decimal("150000"))           # 无持仓
        brk = _Broker()
        st._apply_pead(date(2020, 6, 1), {}, book, brk)
        assert "sh.600005" not in st._pead_holds
        assert "evt-x" in st._pead_acted           # 事件保持已消费
        assert not brk.orders                      # 零持仓不发卖单

    def test_pead_held_position_not_unregistered(self):
        """有实际持仓的在册票不受对账影响（正常持有到期语义不变）。"""
        pc = PortfolioConfig(min_daily_amount=Decimal("50000000"))
        cfg = _cfg(use_breadth_timing=True, use_pead=True,
                   breadth_series={"2020-06-01": Decimal("0.5")},
                   pead_hold_days=30, portfolio=pc)
        st = DividendStrategy(config=cfg, signal_layers=SignalLayers())
        st._breadth_today = Decimal("0.5")
        st._pead_holds["sh.600005"] = 0
        st._bar_count = 10
        book = _Book(Decimal("150000"),
                     positions={"sh.600005": _Pos(1000)})
        brk = _Broker()
        st._apply_pead(date(2020, 6, 1), {}, book, brk)
        assert "sh.600005" in st._pead_holds
        assert not brk.orders                      # 未到期不卖出

    def test_layers_required_failclosed(self):
        with pytest.raises(ValueError):
            DividendStrategy(config=_cfg(use_quality_veto=True),
                             signal_layers=None)
