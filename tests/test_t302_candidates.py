#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T302 候选策略 v1 端到端测试（离线，内存帧，⛔ 无网络）。

验收锚点：策略在回测里**真的换了仓**、产生了非空交易、NAV 曲线出现波动，
并且全链走通（signals → portfolio.plan → broker.submit → engine.settle → metrics）。
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
from backtest.metrics import compute_metrics
from strategy.candidates import MomentumConfig, MomentumStrategy

D = Decimal

_D0 = date(2024, 1, 2)
_DAYS = [_D0 + timedelta(days=i) for i in range(30)]     # 30 个连续日（测试粒度）
_SY_A = "sh.600101"   # 上涨标的（每天 +1% 必须全中）
_SY_B = "sh.600102"   # 下跌标的（每天 -1%）
_COLS = [
    "date", "open", "high", "low", "close", "preclose", "volume", "amount",
    "turn", "pctChg", "tradestatus", "isST", "code", "adjust_mode", "source",
]


def _row(d: date, symbol: str, base: Decimal, pct: Decimal) -> dict:
    """每天按 pct 匀速变动。base=初始 close。"""
    days_in = (d - _D0).days
    close = base * (D("1") + pct * days_in)
    open_ = close * D("0.998")
    return {
        "date": d, "open": float(open_), "high": float(close),
        "low": float(open_), "close": float(close),
        "preclose": float(close / (D("1") + pct)), "volume": 5_000_000.0,
        "amount": float(close * D("5000000")), "turn": 1.0,
        "pctChg": float(pct * D("100")), "tradestatus": "1", "isST": "0",
        "code": symbol, "adjust_mode": "hfq", "source": "baostock",
    }


def _frame_a() -> pd.DataFrame:
    return pd.DataFrame([_row(d, _SY_A, D("10"), D("0.01")) for d in _DAYS],
                        columns=_COLS)


def _frame_b() -> pd.DataFrame:
    return pd.DataFrame([_row(d, _SY_B, D("10"), D("-0.01")) for d in _DAYS],
                        columns=_COLS)


def _run(
    strategy: MomentumStrategy,
    cash: str = "200000",
    frames: dict[str, pd.DataFrame] | None = None,
) -> tuple:
    frames = frames or {_SY_A: _frame_a(), _SY_B: _frame_b()}
    feed = ParquetDailyFeed(
        preloaded=frames,
        trade_calendar=lambda s, e: [d for d in _DAYS if s <= d <= e],
    )
    ledger = Ledger(D(cash), date=_DAYS[0])
    matcher = MatchEngine(fee_model=make_fee_model())
    broker = BacktestBroker(matcher, ledger, feed)
    result = BacktestEngine(broker, feed).run(strategy, _DAYS[0], _DAYS[-1])
    return result, broker


class TestMomentumCandidate:
    def test_rebalances_and_reports(self) -> None:
        """走通全链：产生了交易、报告可计算、NAV 在动（持仓不犹豫）。"""
        strategy = MomentumStrategy(MomentumConfig(
            lookback=5, rebalance_days=5, max_holding_days=15, warmup_bars=5))
        strategy.watchlist = [_SY_A, _SY_B]
        result, _broker = _run(strategy)

        # ① 有真实交易（buy 至少一次：涨的那只；也可能 s)
        assert result.trades, "策略整段零成交 —— 链路没走通"
        assert any(t.symbol == _SY_A and t.side.value == "BUY" for t in result.trades)

        # ② NAV 曲线非水平（持仓接住了波动）
        navs = list(result.nav_curve.values())
        assert len(set(navs)) > 1, "NAV 全程恒定 = 持仓根本没动"
        assert navs[-1] > 0

        # ③ 指标可算（回测报告不空）
        report = compute_metrics(result, risk_free_annual=D("0.02"))
        assert report.final_nav == result.final_nav
        assert report.trading_days == len(_DAYS)

    def test_time_exit_forces_sell(self) -> None:
        """持仓超 max_holding_days ⇒ 即使它还在涨也必须退。"""
        strategy = MomentumStrategy(MomentumConfig(
            lookback=5, rebalance_days=5, max_holding_days=12, warmup_bars=5))
        strategy.watchlist = [_SY_A, _SY_B]
        result, _broker = _run(strategy)

        buys = [t for t in result.trades
                if t.symbol == _SY_A and t.side.value == "BUY"]
        sells = [t for t in result.trades
                 if t.symbol == _SY_A and t.side.value == "SELL"]
        assert buys and sells, f"时间退出没生效：buys={len(buys)} sells={len(sells)}"
        # 卖出日 > 买入日 + max_holding_days（自然日近似即可）
        first_buy = buys[0].date
        last_sell = sells[-1].date
        assert (last_sell - first_buy).days >= 12

    def test_no_lookahead_warmup(self) -> None:
        """warmup 之前不动手：前 warmup 个调仓日应无 BUY。"""
        strategy = MomentumStrategy(MomentumConfig(
            lookback=5, rebalance_days=1, max_holding_days=99, warmup_bars=8))
        strategy.watchlist = [_SY_A]
        result, _ = _run(strategy, frames={_SY_A: _frame_a()})
        buy_days = [t.date for t in result.trades if t.side.value == "BUY"]
        for d in buy_days:
            idx = _DAYS.index(d)
            # 组合管理器 + 时间推进延迟 ⇒ 首次可成交 ≥ warmup + 1
            assert idx >= 8, f"warmup 违约：{_DAYS[0]} 起第 {idx} 天就买入"

    def test_strategy_params_registered(self) -> None:
        """策略参数是 registry 的合法 canonical 输入（⛔ 不含 float 字段）。"""
        from reporting.registry import ExperimentRegistry
        from backtest.metrics import compute_metrics

        strategy = MomentumStrategy()
        strategy.watchlist = [_SY_A]
        result, _ = _run(strategy, frames={_SY_A: _frame_a()})
        report = compute_metrics(result, risk_free_annual=D("0.02"))

        class _Cfg:
            pass
        import strategy.candidates as m
        cfg = next(iter([s for s in [m.MomentumConfig()]].__iter__()))
        # MomentumConfig 只有 int/Decimal 字段，canonical 化应直接过
        from backtest.ledger import _canonicalize
        snapshot = _canonicalize({
            "lookback": cfg.lookback, "rebalance_days": cfg.rebalance_days,
            "max_holding_days": cfg.max_holding_days, "warmup_bars": cfg.warmup_bars,
            "target_count": cfg.portfolio.target_count,
            "min_position_value": cfg.portfolio.min_position_value,
        })
        assert snapshot["lookback"] == 20
        assert isinstance(snapshot["min_position_value"], str)
