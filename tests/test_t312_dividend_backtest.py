#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T312 红利策略回测测试套件（7 例）。

① 数据完整性 2 例 + ② 字段校验 2 例依赖 data/dividend_stocks（未采集 skip）；
③ 回测 3 例离线合成数据全跑（永不 skip）—— 纠上一窗口遗留：引擎组装错误
（initial_capital 进 Engine 构造器）+ warmup_bars=50 违反 >=200 校验。
"""
from __future__ import annotations

import json
from datetime import date as _date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from backtest.broker import BacktestBroker
from backtest.engine import BacktestEngine
from backtest.feed import ParquetDailyFeed
from backtest.fees import make_fee_model, make_price_model
from backtest.ledger import Ledger
from backtest.matching import MatchEngine
from backtest.metrics import compute_metrics
from strategy.candidates import DividendConfig, DividendStrategy
from strategy.portfolio import PortfolioConfig

DATA_ROOT = Path("data/dividend_stocks")


def _sample_symbols(n: int) -> list[str]:
    return sorted(
        (d.name for d in DATA_ROOT.iterdir()
         if d.is_dir() and d.name.startswith(("sh.", "sz."))
         and d.name != "sh.000300"),
    )[:n]


def _skip_if_no_data():
    if not (DATA_ROOT / "meta.json").exists():
        pytest.skip("数据未采集（运行 scripts/collect_dividend_stocks.py）")
    # ⛔ CI 小样守卫（2026-09-11）：CI 会把 tests/fixtures/ci_min_data 物化到 data/，
    #    使数据路径真实存在（G-REF-1 需要）。但该小样只有 30 只 × 2024 一年，
    #    ⛔ 不是全量真实数据 ⇒ 依赖全量的断言（>=300 只、含 2015/2016 分区）在此必须 skip，
    #    ⛔ 不得让它伪装成"全量校验通过"。全量校验仍须在本地 data/ 上跑同一条命令。
    if (DATA_ROOT / "fixture_manifest.json").exists():
        pytest.skip(
            "数据区是 CI 最小数据样本（30 只 × 2024 一年，非全量）⇒ "
            "全量断言（>=300 只 / 2015-2016 分区深度）不适用；"
            "⛔ 这不是通过，全量校验须在本地 data/ 上运行"
        )


# ===================================================================
# ① 数据完整性（依赖采集产物，未采集 skip）
# ===================================================================

def test_dividend_stocks_meta_contains_300_plus():
    """meta.json >=300 只（避免幸存者偏差）。"""
    _skip_if_no_data()
    with open(DATA_ROOT / "meta.json", encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["total_symbols"] >= 300, f"红利股数量 {meta['total_symbols']} < 300"
    assert meta["successful"] >= 250, f"成功采集 {meta['successful']} < 250"


def test_dividend_stocks_partitions_complete():
    """抽样股票含 2015/2016 年分区（历史深度检查）。"""
    _skip_if_no_data()
    symbols = _sample_symbols(10)
    if len(symbols) < 5:
        pytest.skip("可用 symbol 不足（< 5）")
    for symbol in symbols:
        years = {int(f.stem.isdigit() and f.stem) for f in (DATA_ROOT / symbol).glob("*.parquet")}
        assert 2015 in years or 2016 in years, f"{symbol} 缺 2015/2016 分区"


# ===================================================================
# ② 字段校验（依赖采集产物，未采集 skip）
# ===================================================================

def test_dividend_yield_market_cap_fields_present():
    """抽样 10 只股票 dividend_yield / market_cap 列齐备（覆盖率 >=50%）。"""
    _skip_if_no_data()
    symbols = _sample_symbols(10)
    if len(symbols) < 5:
        pytest.skip("可用 symbol 不足")
    checked, missing = 0, 0
    for symbol in symbols:
        files = [p for p in (DATA_ROOT / symbol).glob("*.parquet") if p.stem.isdigit()]
        if not files:
            continue
        df = pd.read_parquet(max(files, key=lambda p: int(p.stem)))
        checked += 1
        if "dividend_yield" not in df.columns or "market_cap" not in df.columns:
            missing += 1
    if checked == 0:
        pytest.skip("未找到可检查分区")
    assert (checked - missing) / checked >= 0.50, (
        f"字段覆盖率 {(checked-missing)/checked:.1%} < 50%")


def test_dividend_yield_range_reasonable():
    """股息率在 [0, 20%]。"""
    _skip_if_no_data()
    symbols = _sample_symbols(5)
    if len(symbols) < 3:
        pytest.skip("可用 symbol 不足")
    found_any = False
    for symbol in symbols:
        files = [p for p in (DATA_ROOT / symbol).glob("*.parquet") if p.stem.isdigit()]
        if not files:
            continue
        df = pd.read_parquet(max(files, key=lambda p: int(p.stem)))
        if "dividend_yield" not in df.columns:
            continue
        yields = pd.to_numeric(df["dividend_yield"], errors="coerce").dropna()
        if len(yields) == 0:
            continue
        found_any = True
        assert yields.min() >= 0, f"{symbol} 负股息率: {yields.min()}"
        assert yields.max() <= 0.20, f"{symbol} 股息率 >20%: {yields.max()}"
    if not found_any:
        pytest.skip("抽样股票均无有效股息率数据")


# ===================================================================
# ③ 回测运行（离线合成数据，永不 skip）
# ===================================================================

START = _date(2024, 1, 2)


def _make_bars_frame(dates, price0, div_yield, mcap, code) -> pd.DataFrame:
    """合成日线帧（缓涨 + 扩展列），形状同采集器落盘。"""
    return pd.DataFrame({
        "date": [d.isoformat() for d in dates],
        "open": [price0 + i * 0.01 for i in range(len(dates))],
        "high": [price0 + i * 0.01 + 0.5 for i in range(len(dates))],
        "low": [price0 + i * 0.01 - 0.5 for i in range(len(dates))],
        "close": [price0 + i * 0.01 for i in range(len(dates))],
        "preclose": [price0 + (i - 1) * 0.01 for i in range(len(dates))],
        "volume": [1_000_000 for _ in dates],
        "amount": [80_000_000.0 for _ in dates],
        "turn": [0.5] * len(dates),
        "pctChg": [0.0] * len(dates),
        "tradestatus": ["1"] * len(dates),
        "isST": ["0"] * len(dates),
        "code": [code] * len(dates),
        "dividend_yield": [div_yield] * len(dates),
        "market_cap": [mcap] * len(dates),
    })


def _make_synthetic_fixture(tmp_path: Path):
    """两只高息股 + 一只低息股，合成 260 根 bar（warmup 210 + 缓冲）。"""
    dates = list(pd.date_range("2024-01-02", periods=260, freq="B").date)
    frames = {
        "sh.600000": _make_bars_frame(dates, 10.0, 0.05, 5e9, "sh.600000"),
        "sz.000001": _make_bars_frame(dates, 8.0, 0.04, 4e9, "sz.000001"),
        "sz.000002": _make_bars_frame(dates, 6.0, 0.01, 3e9, "sz.000002"),
    }
    for sym, frame in frames.items():
        for year, chunk in frame.groupby(pd.to_datetime(frame["date"]).dt.year):
            d = tmp_path / sym
            d.mkdir(parents=True, exist_ok=True)
            chunk = chunk.copy()
            chunk["source"] = "sine"
            chunk["adjust_mode"] = "RAW"
            chunk.to_parquet(d / f"{year}.parquet", index=False)

    feed = ParquetDailyFeed(
        root=tmp_path,
        trade_calendar=lambda s, e: [d for d in dates if s <= d <= e],
        preloaded=frames,
    )
    return feed, dates


def _build_engine(feed, capital: Decimal) -> BacktestEngine:
    """正确组装（T201 契约）：资金进 Ledger，Engine 只收 broker+feed。"""
    ledger = Ledger(initial_cash=capital, date=START)
    matcher = MatchEngine(fee_model=make_fee_model(), price_model=make_price_model())
    broker = BacktestBroker(matcher=matcher, ledger=ledger, feed=feed)
    return BacktestEngine(broker=broker, feed=feed)


def _config(min_yield: str) -> DividendConfig:
    return DividendConfig(
        min_dividend_yield=Decimal(min_yield),
        candidate_pool_size=10,
        min_positions=2,
        max_positions=5,
        default_positions=3,
        use_ma200_timing=False,
        rebalance_days=20,
        warmup_bars=210,
        portfolio=PortfolioConfig(
            min_positions=2, max_positions=5, target_count=3,
            max_price=None),
    )


def test_dividend_strategy_backtest_runs_without_crash(tmp_path):
    """离线合成数据端到端回测不崩溃，NAV 曲线完整。"""
    feed, dates = _make_synthetic_fixture(tmp_path)
    strategy = DividendStrategy(config=_config("0.03"), universe_provider=None)
    engine = _build_engine(feed, Decimal("100000"))
    result = engine.run(strategy, dates[0], dates[-1])
    assert result.nav_curve, "NAV 曲线为空"
    assert len(result.nav_curve) == len(dates), "NAV 曲线跳日"
    assert result.final_nav > 0


def test_performance_report_fields_complete(tmp_path):
    """compute_metrics 在合成数据上产出齐全的 PerformanceReport。"""
    feed, dates = _make_synthetic_fixture(tmp_path)
    strategy = DividendStrategy(config=_config("0.02"), universe_provider=None)
    engine = _build_engine(feed, Decimal("100000"))
    result = engine.run(strategy, dates[0], dates[-1])
    report = compute_metrics(result, risk_free_annual= Decimal("0.025"))
    for field in ("cagr", "sharpe_ratio", "max_drawdown", "annual_turnover",
                  "win_rate", "fees_total", "final_nav"):
        assert hasattr(report, field), f"PerformanceReport 缺 {field}"
    assert isinstance(report.cagr, Decimal)
    assert 0 <= report.max_drawdown <= 1


def test_engine_exdiv_settlement_on_raw_prices(tmp_path):
    """RAW 价 + sidecar 除权：派现后 NAV 无跳变（FR-BT-4 端到端）。"""
    from backtest.settle import ExdivEvent
    feed, dates = _make_synthetic_fixture(tmp_path)
    strategy = DividendStrategy(config=_config("0.03"), universe_provider=None)
    engine = _build_engine(feed, Decimal("100000"))
    exdiv_day = dates[250]
    engine.exdiv_provider = lambda day: (
        {"sh.600000": ExdivEvent(symbol="sh.600000", factor=Decimal("2"),
                                 cash_dividend=Decimal("0.10"), date=exdiv_day)}
        if day == exdiv_day else None)
    result = engine.run(strategy, dates[0], dates[-1])
    idx = dates.index(exdiv_day)
    navs = list(result.nav_curve.values())
    prev, post = navs[idx - 1], navs[idx]
    assert abs(post - prev) < Decimal("3"), (
        f"除权日 NAV 跳变 {prev}->{post}（RAW 价漏结算）")
