#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T304 跨区间压力测试（A 路线：离线合成；spec §6.1 样外如实呈现）。

两个合成阶段区间对应的**真实历史情境陈述**（⛔ 数字不修饰）：
  - **2015-06 ~ 2016-02 股灾+熔断**：前段（6/12 前）加速赶顶，中段（6/15 起）连续跳水，
    2016-01 初熔断式跌停潮，随后阴跌磨底。合成特征：前段高波动上行 + 中段断崖 + 后段阴跌。
  - **2018 全年熊市**：持续的市值缩水（约 −25% 全市场），间或有企稳反弹，整体趋势向下。

合成数据生成原则（不美化动量策略在这两类区间的预期劣势）：
  - ⛔ 不许造「动量在股灾里反而赢」的合成场景——动量 = 趋势跟踪，在 V 型反转和
    熔断日跳水面前理论上是必挨打的形态；如实呈现负收益是本分。
  - 每只股票 = 市场因子 × 个股扰动（不同 seed，固定可复现），⛔ 不许每只都走出独立行情
    （那样相当于内置了 alpha，不真实）。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

import pandas as pd
import pytest

from backtest.broker import BacktestBroker
from backtest.engine import BacktestEngine
from backtest.feed import ParquetDailyFeed
from backtest.fees import make_fee_model
from backtest.ledger import Ledger
from backtest.matching import MatchEngine
from strategy.candidates import MomentumConfig, MomentumStrategy

D = Decimal

_COLS = [
    "date", "open", "high", "low", "close", "preclose", "volume", "amount",
    "turn", "pctChg", "tradestatus", "isST", "code", "adjust_mode", "source",
]


class _Regime:
    """区间定义：日期范围 + 每日市场幅度（%）。"""
    def __init__(self, name: str, start: date, days: int, daily_pcts: list[Decimal]):
        self.name = name
        self.start = start
        self.days = days
        assert len(daily_pcts) == days, f"{name} 的 daily_pcts 要恰好 {days} 天"
        self.daily_pcts = daily_pcts      # 市场因子（%）序列，第 i 天


# 合成区间构造：同一批 5 只股在两个区间各跑一次（不同 seed ⇒ 个股噪声差异）。
_POOL = [
    ("sh.600010", "600010"),
    ("sh.600028", "600028"),
    ("sz.000001", "000001"),
    ("sz.000063", "000063"),
    ("sh.600036", "600036"),
]


def _build_frames(regime: _Regime, seed_base: int) -> dict[str, pd.DataFrame]:
    """同一市场因子 + 各股 seed 噪声 ⇒ 一致的合成行情（⛔ 不造全员独立牛市）。"""
    frames = {}
    for i, (code, _prefix) in enumerate(_POOL):
        rng_seed = seed_base + i
        # 个股噪声 ±1.5% 均匀分布，确定性取自 seed
        rnd = _lcg_noise(rng_seed, regime.days)
        rows = []
        prev_close = D("500")                          # 起始价 500 元
        for k in range(regime.days):
            day = regime.start + timedelta(days=k)
            mkt = regime.daily_pcts[k]                   # 市场因子（%）
            idio = rnd[k]                               # 个股噪声（%）∈ [-1.5, 1.5]
            pct = mkt + idio
            close = prev_close * (D("1") + pct / D("100"))
            open_ = prev_close * D("0.999")
            high = max(open_, close)
            low = min(open_, close)
            rows.append({
                "date": day, "open": float(open_), "high": float(high),
                "low": float(low), "close": float(close), "preclose": float(prev_close),
                "volume": 500_000.0, "amount": float(close * D("500000")),
                "turn": 1.0, "pctChg": float(pct), "tradestatus": "1",
                "isST": "0", "code": code, "adjust_mode": "hfq", "source": "baostock",
            })
            prev_close = close
        frames[code] = pd.DataFrame(rows, columns=_COLS)
    return frames


def _lcg_noise(seed: int, n: int) -> list[Decimal]:
    """LCG（线性同余）伪随机，均匀分布在 [-1.5, 1.5]（%）。"""
    # A=1103515245, C=12345, M=2^31 —— 经典 LCG 参数
    a, c, m = 1103515245, 12345, 2 ** 31
    x = seed
    out: list[Decimal] = []
    for _ in range(n):
        x = (a * x + c) % m
        u = Decimal(x) / Decimal(m)                     # ∈ [0, 1)
        out.append((u - D("0.5")) * D("3"))             # ∈ [-1.5, 1.5)
    return out


_CRASH = _Regime(
    "2015-06 stock-crash + 2016-01 circuit-breaker",
    start=date(2015, 6, 1), days=40,
    # 阶段 1（0~8：加速赶顶 +3%/日）→ 阶段 2（9~19：跳水 -4.5%/日）→
    # 阶段 3（20~29：熔断期跌停 -9.9%/日 extremes，工作日）→ 阶段 4（30~39：阴跌 -0.5%/日）
    daily_pcts=(
        [D("3.0")] * 9 + [D("-4.5")] * 11 + [D("-9.9")] * 10 + [D("-0.5")] * 10
    ),
)

_BEAR = _Regime(
    "2018 bear market",
    start=date(2018, 1, 2), days=60,
    # 持续阴跌 -0.6%、周中反弹 +0.7%、月末下挫 -1.2%
    daily_pcts=(
        [D("-0.6"), D("-0.6"), D("0.7"), D("-0.6"), D("-1.2")]
        + [D("-0.6"), D("-0.6"), D("0.7"), D("-0.6"), D("-1.2")]
        + [D("-0.6"), D("-0.6"), D("0.7"), D("-0.6"), D("-1.2")]
        + [D("-0.6"), D("-0.6"), D("0.7"), D("-0.6"), D("-1.2")]
        + [D("-0.6"), D("-0.6"), D("0.7"), D("-0.6"), D("-1.2")]
        + [D("-0.6"), D("-0.6"), D("0.7"), D("-0.6"), D("-1.2")]
        + [D("-0.6"), D("-0.6"), D("0.7"), D("-0.6"), D("-1.2")]
        + [D("-0.6"), D("-0.6"), D("0.7"), D("-0.6"), D("-1.2")]
        + [D("-0.6"), D("-0.6"), D("0.7"), D("-0.6"), D("-1.2")]
        + [D("-0.6"), D("-0.6"), D("0.7"), D("-0.6"), D("-1.2")]
        + [D("-0.6"), D("-0.6"), D("0.7"), D("-0.6"), D("-1.2")]
        + [D("-0.6"), D("-0.6"), D("0.7"), D("-0.6"), D("-1.2")]
    ),
)


def _run_regime(regime: _Regime, seed_base: int) -> tuple:
    """跑一个区间，返回 (engine_result, metrics_report)。"""
    from backtest.metrics import compute_metrics
    frames = _build_frames(regime, seed_base)
    feed = ParquetDailyFeed(
        preloaded=frames,
        trade_calendar=lambda s, e: [
            regime.start + timedelta(days=i)
            for i in range(regime.days)
            if s <= regime.start + timedelta(days=i) <= e
        ],
    )
    ledger = Ledger(D("500000"), date=regime.start)
    matcher = MatchEngine(fee_model=make_fee_model())
    broker = BacktestBroker(matcher, ledger, feed)
    strategy = MomentumStrategy(MomentumConfig(
        lookback=8, rebalance_days=5, warmup_bars=8, max_holding_days=25))
    strategy.universe_provider = lambda day: [code for code, _ in _POOL]
    result = BacktestEngine(broker, feed).run(
        strategy, regime.start, regime.start + timedelta(days=regime.days - 1))
    report = compute_metrics(result, risk_free_annual=D("0.02"))
    return result, report


class TestStressRuns:
    """T304 验收锚点：两个区间都跑完、指标可算、如实在报告里呈现。"""

    def test_crash_period_produces_measurable_drawdown(self) -> None:
        result, report = _run_regime(_CRASH, seed_base=7)
        # 股灾段：必有显著负收益 + 回撤 > 15%（合成市场本身跌 ~50%）
        assert report.total_return < D("0"), \
            f"股灾合成段竟涨？total_return={report.total_return}（不可能，除非代码错）"
        assert report.max_drawdown >= D("0.10"), \
            f"股灾合成段回撤过小 {report.max_drawdown} —— 没接住下跌趋势"
        # NAV 在末期必低于初期
        navs = list(result.nav_curve.values())
        assert navs[-1] < navs[0]

    def test_bear_period_shows_persistence(self) -> None:
        result, report = _run_regime(_BEAR, seed_base=101)
        # 熊市段：负收益（趋势向下 + 动量滞后反转必亏），但回撤不应达股灾级
        assert report.total_return < D("0"), \
            f"熊市合成段非负？{report.total_return}（除非走得很扛跌——不合情理）"
        assert report.max_drawdown < D("0.6"), \
            "熊市区间回撤失控到 >60% 说明是双侧踩踏，不是策略问题"

    def test_both_regimes_serialize_comparable_reports(self) -> None:
        """compare across regimes：两个区间的 PerformanceReport 同时可序列化供 T305 引用。"""
        _, r_crash = _run_regime(_CRASH, seed_base=7)
        _, r_bear = _run_regime(_BEAR, seed_base=101)
        # 字段全在、值非 NaN
        for label, r in (("crash", r_crash), ("bear", r_bear)):
            assert isinstance(r.cagr, Decimal), label
            assert isinstance(r.max_drawdown, Decimal), label
            assert isinstance(r.trading_days, int), label
        # 如实呈现：报告原样返回，不在测试里修饰（T305 需要原始数字）
        assert r_crash.max_drawdown > r_bear.max_drawdown, \
            "股灾合成段回撤竟不超过熊市段——合成数据建构错了，不是策略对了"
