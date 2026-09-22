#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e65 外部分数宽篮回测 runner —— 把离线分数表灌进真引擎跑含费/含撮合语义的全真回测。

与 ``run_dividend_backtest.py`` 同宗：ParquetDailyFeed + Ledger + MatchEngine +
BacktestBroker + 前后置门禁 + registry 出处三件套。差异：

* 数据源：``data/daily_bars``（全 A，baostock RAW）而非 ``data/dividend_stocks``；
* 策略：``ScoreBasketStrategy``（外部分数表驱动，默认月调 top-N）；
* 无除权 sidecar——RAW 价在除权日有真实跳空，P&L 口径偏**保守**（少记股息
  收益），已在 data_version/manifest 声明。

用法::

    .venv/bin/python scripts/run_score_basket_backtest.py \
        --scores experiments/lab/e63/scores_label60.parquet \
        --start 2017-01-01 --end 2024-12-31 \
        --capital 3000000 --target-count 100 --hard-limit 110 \
        --rebalance-days 20
"""
from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date, datetime as _datetime, timezone as _timezone
from decimal import Decimal
from pathlib import Path
from dataclasses import asdict as _asdict
from typing import Any

import pandas as pd

_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

from backtest.broker import BacktestBroker
from backtest.engine import BacktestEngine
from backtest.feed import ParquetDailyFeed
from backtest.fees import make_fee_model, make_price_model
from backtest.ledger import Ledger
from backtest.matching import MatchEngine
from backtest.metrics import compute_metrics
from reporting.registry import ExperimentRegistry
from reporting.provenance import hash_path_manifest, hash_sequence
from strategy.portfolio import PortfolioConfig
from strategy.score_basket import (
    ScoreBasketConfig,
    ScoreBasketStrategy,
    normalize_score_code,
)
from scripts.gates import run_post_run_gates, run_pre_run_gates

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

INDEX_SYMBOL = "sh.000300"


def _parse_iso(value: Any) -> _date | None:
    try:
        return _date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _load_index_frame(data_path: Path) -> pd.DataFrame | None:
    idx_dir = data_path / INDEX_SYMBOL
    parts = [pd.read_parquet(p) for p in sorted(idx_dir.glob("*.parquet"))
             if p.stem.isdigit()]
    if not parts:
        return None
    return pd.concat(parts, ignore_index=True).sort_values("date").reset_index(drop=True)


def _load_universe_tables(data_path: Path) -> dict[str, pd.DataFrame]:
    def _load_one(sym_dir: Path):
        parts = [pd.read_parquet(p) for p in sorted(sym_dir.glob("*.parquet"))
                 if p.stem.isdigit()]
        return sym_dir.name, (pd.concat(parts, ignore_index=True) if parts else None)

    dirs = [d for d in sorted(data_path.iterdir())
            if d.is_dir() and (d.name.startswith(("sh.", "sz.", "bj.")))]
    tables: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for name, frame in pool.map(_load_one, dirs):
            if frame is not None:
                tables[name] = frame
    return tables


def _load_score_table(scores_path: Path) -> dict[_date, dict[str, float]]:
    df = pd.read_parquet(scores_path)
    need = {"sig_date", "ts_code", "score"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"分数表缺列 {missing}: {scores_path}")
    df = df.dropna(subset=["score"])
    df["sig_date"] = pd.to_datetime(df["sig_date"]).dt.date
    df["sym"] = df["ts_code"].astype(str).map(normalize_score_code)
    table: dict[_date, dict[str, float]] = {}
    for d, g in df.groupby("sig_date"):
        table[d] = dict(zip(g["sym"], g["score"].astype(float)))
    logger.info(f"分数表: {len(table)} 期 × 覆盖 {df['sym'].nunique()} 标的 "
                f"({min(table)} ~ {max(table)})")
    return table


def main() -> int:
    ap = argparse.ArgumentParser(description="e65 外部分数宽篮回测")
    ap.add_argument("--scores", required=True, type=Path)
    ap.add_argument("--data-path", type=Path, default=_root / "data" / "daily_bars")
    ap.add_argument("--index-path", type=Path,
                    default=_root / "data" / "dividend_stocks")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--capital", type=Decimal, default=Decimal("3000000"))
    ap.add_argument("--target-count", type=int, default=100)
    ap.add_argument("--min-positions", type=int, default=None)
    ap.add_argument("--max-positions", type=int, default=None)
    ap.add_argument("--hard-limit", type=int, default=None)
    ap.add_argument("--min-position-value", type=Decimal,
                    default=Decimal("20000"))
    ap.add_argument("--rebalance-days", type=int, default=20)
    ap.add_argument("--max-score-age-days", type=int, default=45)
    ap.add_argument("--risk-free-annual", type=Decimal, default=Decimal("0.015"))
    ap.add_argument("--registry-root", type=Path, default=None)
    ap.add_argument("--no-gates", action="store_true")
    ap.add_argument("--gate-strict", action="store_true")
    args = ap.parse_args()

    start = _date.fromisoformat(args.start)
    end = _date.fromisoformat(args.end)
    target = args.target_count
    min_pos = args.min_positions if args.min_positions is not None else max(1, target - 10)
    max_pos = args.max_positions if args.max_positions is not None else target + 10
    hard = args.hard_limit if args.hard_limit is not None else max_pos + 10

    portfolio_config = PortfolioConfig(
        target_count=target, min_positions=min_pos, max_positions=max_pos,
        hard_limit=hard, min_position_value=args.min_position_value)
    strategy_config = ScoreBasketConfig(
        portfolio=portfolio_config,
        rebalance_days=args.rebalance_days,
        max_score_age_days=args.max_score_age_days,
    )

    # 日历：复用 dividend_stocks 的指数分区（全市场日历基准）
    index_frame = _load_index_frame(args.index_path)
    if index_frame is None:
        raise RuntimeError(f"指数 {INDEX_SYMBOL} 分区缺失于 {args.index_path}")
    cal_days = [_parse_iso(d) for d in index_frame["date"]]
    cal_days = [d for d in cal_days if d and start <= d <= end]
    if not cal_days:
        raise RuntimeError("日历窗口内无交易日")

    logger.info(f"加载全 A 数据: {args.data_path}")
    tables = _load_universe_tables(args.data_path)
    if not tables:
        raise FileNotFoundError(f"{args.data_path} 下没有可用 symbol 分区")
    logger.info(f"  {len(tables)} 只标的分区加载完成")

    score_table = _load_score_table(args.scores)
    covered = {s for m in score_table.values() for s in m}
    missing = covered - set(tables)
    if missing:
        logger.warning(f"分数覆盖域中 {len(missing)} 只标的无 bar 分区（未采集/已退市外）")

    feed = ParquetDailyFeed(
        root=args.data_path,
        trade_calendar=lambda s, e: [d for d in cal_days if s <= d <= e],
        preloaded=tables,
        exdiv_events={},
    )

    strategy = ScoreBasketStrategy(
        config=strategy_config,
        score_table=score_table,
        universe_provider=None,          # watchlist = 分数覆盖域 ∩ 当日有 bar
    )

    ledger = Ledger(initial_cash=args.capital, date=start)
    matcher = MatchEngine(fee_model=make_fee_model(), price_model=make_price_model())
    broker = BacktestBroker(matcher=matcher, ledger=ledger, feed=feed,
                            enable_dividend_tax=True)
    engine = BacktestEngine(broker=broker, feed=feed)

    gate_statuses: dict[str, Any] = {}
    if not args.no_gates:
        logger.info("执行回测前置门禁审计 ...")
        from scripts.run_dividend_backtest import _gate_status_map
        pre_results = run_pre_run_gates(
            context={"active_features": ["DIVIDEND_TAX"]},
            tables=tables,
            exdiv_events={},
            strategy_config=strategy_config,
            strict=args.gate_strict,
        )
        gate_statuses.update(_gate_status_map(pre_results))
    else:
        logger.warning("--no-gates 生效")

    logger.info(f"开始回测 {start} ~ {end}  top{target} reb={args.rebalance_days}d")
    result = engine.run(strategy, start, end)

    logger.info("计算绩效指标...")
    report = compute_metrics(result, risk_free_annual=args.risk_free_annual)

    if not args.no_gates:
        logger.info("执行回测后置门禁审计 ...")
        from scripts.run_dividend_backtest import (
            _build_post_run_gate_context, _gate_status_map)
        gate_ctx = _build_post_run_gate_context(
            data_path=args.data_path,
            result=result,
            report=report,
            strategy_config=strategy_config,
            index_frame=index_frame,
            cal_days=cal_days,
        )
        post_results = run_post_run_gates(
            context=gate_ctx,
            result=result,
            report=report,
            strategy_config=strategy_config,
            strict=args.gate_strict,
        )
        gate_statuses.update(_gate_status_map(post_results))

    universe_codes = sorted(covered)
    data_hash = hash_path_manifest(args.data_path)
    registry = ExperimentRegistry(
        root=args.registry_root or (_root / "experiments"),
        code_version="e65-score-basket-v1",
        data_version="daily-bars-alla-2014-2024+raw-noexdiv",
        code_hash=None,
        data_hash=data_hash,
        calendar_hash=hash_sequence(cal_days, label="cal"),
        universe_hash=hash_sequence(universe_codes, label="universe"),
    )

    from reporting.evidence import build_run_evidence
    ev_extras: dict[str, Any] = {
        "active_features": ["DIVIDEND_TAX"],
        "fee_summary": {
            (k.value if hasattr(k, "value") else str(k)): str(v)
            for k, v in (getattr(report, "fees_total", {}) or {}).items()},
        "baseline_return": float(report.total_return),
        "run_calendar_bounds": [cal_days[0].isoformat(), cal_days[-1].isoformat()],
        "scores_path": str(args.scores),
    }
    evidence = build_run_evidence(result, tables=tables, extras=ev_extras)

    run_id = registry.record_run(
        params=_asdict(strategy_config),
        report=report,
        seed=None,
        status="FINISHED",
        gate_statuses=gate_statuses or None,
        evidence=evidence,
    )

    print("\n" + "=" * 60)
    print(f"e65 分数宽篮回测 {start} ~ {end}  top{target}/月调{args.rebalance_days}d")
    print("=" * 60)
    print(f"初始资金:     {args.capital:>14,.2f} 元")
    print(f"终值净值:     {result.nav_series[-1].nav:>14,.2f} 元")
    print(f"总收益:       {float(report.total_return) * 100:>13.2f} %")
    print(f"CAGR:         {float(report.cagr) * 100:>13.2f} %")
    print(f"最大回撤:     {float(report.max_drawdown) * 100:>13.2f} %")
    print(f"年化换手:     {float(report.turnover_annual):>13.1f} %")
    print(f"费用合计:     {sum(Decimal(str(v)) for v in (getattr(report, 'fees_total', {}) or {}).values()):>14,.2f} 元")
    print(f"run_id: {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
