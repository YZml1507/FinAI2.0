#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e65 ScoreBasketStrategy 端到端测试（离线内存帧，⛔ 无网络）。

验收锚点：① 分数表驱动调仓——高分标的被买入、低分不买；② 分数过期
（>max_score_age_days）不交易；③ 代码规范化映射；④ 全链走通出非空成交。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from backtest.broker import BacktestBroker
from backtest.engine import BacktestEngine
from backtest.feed import ParquetDailyFeed
from backtest.fees import make_fee_model
from backtest.ledger import Ledger
from backtest.matching import MatchEngine
from strategy.portfolio import PortfolioConfig
from strategy.score_basket import (
    ScoreBasketConfig,
    ScoreBasketStrategy,
    normalize_score_code,
)

D = Decimal

_D0 = date(2024, 1, 2)
_DAYS = [_D0 + timedelta(days=i) for i in range(40)]
_SY_A = "sh.600101"
_SY_B = "sh.600102"
_SY_C = "sz.300001"
_COLS = [
    "date", "open", "high", "low", "close", "preclose", "volume", "amount",
    "turn", "pctChg", "tradestatus", "isST", "code", "adjust_mode", "source",
]


def _row(d: date, symbol: str, base: D, pct: D) -> dict:
    days_in = (d - _D0).days
    close = base * (D("1") + pct * days_in)
    open_ = close * D("0.998")
    return {
        "date": d, "open": float(open_), "high": float(close),
        "low": float(open_), "close": float(close),
        "preclose": float(close / (D("1") + pct)), "volume": 10_000_000.0,
        "amount": float(close * D("10000000")), "turn": 1.0,
        "pctChg": float(pct * D("100")), "tradestatus": "1", "isST": "0",
        "code": symbol, "adjust_mode": "hfq", "source": "baostock",
    }


def _frame(symbol: str, pct: D) -> pd.DataFrame:
    return pd.DataFrame([_row(d, symbol, D("10"), pct) for d in _DAYS],
                        columns=_COLS)


def _cfg(target: int = 2, reb: int = 5, age: int = 45) -> ScoreBasketConfig:
    return ScoreBasketConfig(
        portfolio=PortfolioConfig(
            target_count=target, min_positions=1, max_positions=target + 2,
            hard_limit=target + 4, min_position_value=D("1000")),
        rebalance_days=reb, max_score_age_days=age, warmup_bars=2)


def _run(strategy, cash="200000") -> tuple:
    frames = {s: _frame(s, p) for s, p in
              ((_SY_A, D("0.01")), (_SY_B, D("-0.01")), (_SY_C, D("0.005")))}
    feed = ParquetDailyFeed(
        preloaded=frames,
        trade_calendar=lambda s, e: [d for d in _DAYS if s <= d <= e])
    ledger = Ledger(D(cash), date=_DAYS[0])
    broker = BacktestBroker(MatchEngine(fee_model=make_fee_model()), ledger, feed)
    result = BacktestEngine(broker, feed).run(strategy, _DAYS[0], _DAYS[-1])
    return result, broker


class TestNormalize:
    def test_suffixed(self) -> None:
        assert normalize_score_code("600000.SH") == "sh.600000"
        assert normalize_score_code("000001.SZ") == "sz.000001"
        assert normalize_score_code("430047.BJ") == "bj.430047"

    def test_bare(self) -> None:
        assert normalize_score_code("600519") == "sh.600519"
        assert normalize_score_code("300750") == "sz.300750"
        assert normalize_score_code("830799") == "bj.830799"

    def test_bad(self) -> None:
        with pytest.raises(ValueError):
            normalize_score_code("XX9999")


class TestScoreBasket:
    def test_top_scored_bought(self) -> None:
        """分数排名驱动建仓：target=2 ⇒ 买 A、C，不买 B。"""
        score_table = {
            _D0: {_SY_A: 0.9, _SY_B: 0.1, _SY_C: 0.8},
            _D0 + timedelta(days=20): {_SY_A: 0.9, _SY_B: 0.1, _SY_C: 0.8},
        }
        st = ScoreBasketStrategy(_cfg(target=2), score_table)
        result, broker = _run(st)
        trades = [t for t in broker.trades if t.symbol != ""]
        assert trades, "应有非空成交"
        bought = {t.symbol for t in trades if str(t.side).endswith("BUY")}
        assert _SY_A in bought and _SY_C in bought
        assert _SY_B not in bought

    def test_stale_scores_no_trade(self) -> None:
        """分数过期（sig 距调仓日 > max_age）⇒ 不交易。"""
        old = _D0 - timedelta(days=100)
        score_table = {old: {_SY_A: 0.9, _SY_B: 0.1, _SY_C: 0.8}}
        st = ScoreBasketStrategy(_cfg(age=30), score_table)
        result, broker = _run(st)
        assert not list(broker.trades), "过期分数不得产生成交"

    def test_rebalance_to_new_top(self) -> None:
        """第二期分数翻转 ⇒ 调仓换入新头部、卖出掉出名次的旧持仓。"""
        mid = _D0 + timedelta(days=20)
        score_table = {
            _D0: {_SY_A: 0.9, _SY_B: 0.1, _SY_C: 0.8},
            mid: {_SY_A: 0.1, _SY_B: 0.9, _SY_C: 0.2},
        }
        st = ScoreBasketStrategy(_cfg(target=2, reb=10), score_table)
        result, broker = _run(st, cash="500000")
        sells = {t.symbol for t in broker.trades if str(t.side).endswith("SELL")}
        buys = {t.symbol for t in broker.trades if str(t.side).endswith("BUY")}
        assert _SY_B in buys, "第二期头名应被买入"
        assert _SY_A in sells, "跌出头名的旧持仓应被卖出"
