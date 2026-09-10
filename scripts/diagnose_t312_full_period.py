#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T312 红利策略全周期诊断（不改引擎契约，只包一层 settle 记录）。

输出 docs/diagnosis/t312_full_period_diagnosis.md
"""
from __future__ import annotations

import logging
import sys
from datetime import date as _date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd

from backtest.broker import BacktestBroker
from backtest.engine import BacktestEngine
from backtest.feed import ParquetDailyFeed
from backtest.fees import make_fee_model, make_price_model
from backtest.ledger import JournalType, Ledger
from backtest.matching import MatchEngine
from backtest.metrics import compute_metrics
from dataclasses import replace
from scripts.run_dividend_backtest import (
    END,
    INDEX_SYMBOL,
    START,
    _group_exdiv_by_date,
    _load_exdiv_events,
    _load_index_frame,
    _load_universe_tables,
    _make_universe_provider,
    _parse_iso,
)
from strategy.candidates import DividendConfig, DividendStrategy
from strategy.portfolio import PortfolioConfig

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("diagnosis")

Q2 = Decimal("0.01")


def _q2(x: float) -> Decimal:
    return Decimal(str(x)).quantize(Q2, rounding=ROUND_HALF_UP)


class RecordingBroker(BacktestBroker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.daily_snapshot: dict[str, tuple[Decimal, Decimal, int]] = {}

    def settle(self, date, exdiv_events=None, bars=None):
        super().settle(date, exdiv_events, bars=bars)
        book = self.book
        n = sum(1 for p in book.positions.values() if p.volume)
        self.daily_snapshot[date.isoformat()] = (
            book.cash,
            book.total_market_value(),
            n,
        )


def load_etf_nav(symbol: str, data_root: Path) -> dict[str, Decimal]:
    root = data_root / symbol
    if not root.exists():
        return {}
    parts = [pd.read_parquet(p) for p in sorted(root.glob("*.parquet"))]
    if not parts:
        return {}
    df = pd.concat(parts, ignore_index=True).sort_values("date")
    if "factor" in df.columns:
        px = (df["close"].astype(float) * df["factor"].astype(float)).tolist()
    else:
        px = df["close"].astype(float).tolist()
    dates = [_parse_iso(d) for d in df["date"]]
    base = px[0]
    out: dict[str, Decimal] = {}
    for d, p in zip(dates, px):
        if d is None:
            continue
        out[d.isoformat()] = _q2(p / base)
    return out


def annual_returns(nav: dict[str, Decimal]) -> dict[int, Decimal]:
    if not nav:
        return {}
    items = sorted(nav.items())
    first: dict[int, Decimal] = {}
    last: dict[int, Decimal] = {}
    for k, v in items:
        y = int(k[:4])
        first.setdefault(y, v)
        last[y] = v
    out: dict[int, Decimal] = {}
    prev: Decimal | None = None
    for y in sorted(last):
        base = prev if prev is not None else first[y]
        end = last[y]
        if base > 0:
            out[y] = _q2(float(end / base) - 1.0)
        prev = end
    return out


def main() -> int:
    data_path = _ROOT / "data" / "dividend_stocks"
    etf_path = _ROOT / "data" / "etf_bars"
    out_dir = _ROOT / "docs" / "diagnosis"
    out_dir.mkdir(parents=True, exist_ok=True)

    portfolio_config = PortfolioConfig(
        min_positions=3,
        max_positions=8,
        target_count=5,
        hard_limit=10,
        min_position_value=Decimal("20000"),
        min_daily_amount=Decimal("50000000"),
        max_participation_rate=Decimal("0.05"),
    )
    strategy_config = DividendConfig(
        min_dividend_yield=Decimal("0.03"),
        candidate_pool_size=50,
        default_positions=5,
        use_ma200_timing=True,
        index_symbol=INDEX_SYMBOL,
        rebalance_days=20,
        warmup_bars=210,
    )

    index_frame = _load_index_frame(data_path)
    if index_frame is None:
        raise SystemExit("缺指数分区")
    universe_provider = _make_universe_provider(logger, data_path)
    tables = _load_universe_tables(data_path)
    cal_days = [d for d in (_parse_iso(x) for x in index_frame["date"]) if d and START <= d <= END]
    exdiv_events = _load_exdiv_events(data_path)
    exdiv_by_date = _group_exdiv_by_date(exdiv_events)
    exdiv_sidecars = {}
    for sym in tables:
        sp = data_path / "exdiv" / f"{sym}.parquet"
        if sp.exists():
            exdiv_sidecars[sym] = pd.read_parquet(sp)
    feed = ParquetDailyFeed(
        root=data_path,
        trade_calendar=lambda s, e: [d for d in cal_days if s <= d <= e],
        preloaded=tables,
        exdiv_events=exdiv_sidecars,
    )
    strategy = DividendStrategy(config=strategy_config, universe_provider=universe_provider)
    ledger = Ledger(initial_cash=Decimal("150000"), date=START)
    matcher = MatchEngine(fee_model=make_fee_model(), price_model=make_price_model())
    broker = RecordingBroker(matcher=matcher, ledger=ledger, feed=feed, enable_dividend_tax=True)
    engine = BacktestEngine(broker=broker, feed=feed)
    engine.exdiv_provider = lambda day: exdiv_by_date.get(day)

    logger.warning("诊断回测启动（无门禁）...")
    result = engine.run(strategy, START, END)
    report = compute_metrics(result, risk_free_annual=Decimal("0.025"))

    strat_annual = annual_returns(result.nav_curve)
    b300 = load_etf_nav("sh.510300", etf_path)
    bdiv = load_etf_nav("sh.512890", etf_path)
    b300_annual = annual_returns({k: v for k, v in b300.items() if START.isoformat() <= k <= END.isoformat()})
    bdiv_annual = annual_returns({k: v for k, v in bdiv.items() if START.isoformat() <= k <= END.isoformat()})

    n_days = len(result.nav_curve)
    empty = sum(1 for _, _, n in broker.daily_snapshot.values() if n == 0)
    cash_heavy = sum(
        1
        for k, (cash, _, _) in broker.daily_snapshot.items()
        if result.nav_curve.get(k, 0) > 0 and float(cash / result.nav_curve[k]) >= 0.95
    )
    avg_cash = sum(
        float(cash / result.nav_curve[k])
        for k, (cash, _, _) in broker.daily_snapshot.items()
        if result.nav_curve.get(k, 0) > 0
    ) / max(n_days, 1)

    pos_by_year: dict[int, list[int]] = {}
    for k, (_, _, n) in broker.daily_snapshot.items():
        pos_by_year.setdefault(int(k[:4]), []).append(n)
    avg_pos = {y: (sum(v) / len(v) if v else 0.0) for y, v in pos_by_year.items()}

    fees: dict[str, Decimal] = {}
    tax_by_year: dict[int, Decimal] = {}
    for e in result.journal_entries:
        if e.fees:
            for item, amt in e.fees.items():
                name = getattr(item, "value", str(item))
                fees[name] = fees.get(name, Decimal("0")) + amt
        if e.entry_type is JournalType.DIVIDEND_TAX:
            tax_by_year[e.date.year] = tax_by_year.get(e.date.year, Decimal("0")) + abs(e.amount)

    lines = [
        "# T312 红利策略全周期诊断报告",
        "",
        f"- 区间：{START} ~ {END}",
        f"- 初始资金：150,000 ｜ 最终净值：{report.final_nav:,.2f}",
        f"- 总收益：{float(report.total_return):.2%} ｜ CAGR：{float(report.cagr):.2%} ｜ MDD：{float(report.max_drawdown):.2%}",
        f"- 夏普：{report.sharpe_ratio} ｜ 胜率：{report.win_rate} ｜ 往返：{report.round_trips}",
        f"- 年化换手：{float(report.annual_turnover) if report.annual_turnover else 0:.2%}",
        "",
        "## 1. 分年度收益对比",
        "",
        "| 年份 | 策略 | sh.510300 | sh.512890 | 超额 vs 300 | 日均持仓数 |",
        "|---|---|---|---|---|---|",
    ]
    for y in range(START.year, END.year + 1):
        s, a, d = strat_annual.get(y), b300_annual.get(y), bdiv_annual.get(y)
        exc = _q2(float(s - a)) if s is not None and a is not None else None
        lines.append(
            f"| {y} | {f'{float(s):.2%}' if s is not None else '—'} | "
            f"{f'{float(a):.2%}' if a is not None else '—'} | "
            f"{f'{float(d):.2%}' if d is not None else '—'} | "
            f"{f'{float(exc):.2%}' if exc is not None else '—'} | "
            f"{avg_pos.get(y, 0):.2f} |"
        )
    lines += [
        "",
        "> 注：512890（红利低波）本地仅 2019–2024；510300 为 close×factor 近似（非全收益）。诊断回测 `--no-gates` 同路径。",
        "",
        "## 2. 费用与红利税",
        "",
        "| 科目 | 金额（元） |",
        "|---|---|",
    ]
    for k, v in sorted(fees.items(), key=lambda kv: -abs(kv[1])):
        lines.append(f"| {k} | {v:,.2f} |")
    lines += [f"| **合计** | **{report.fees_sum:,.2f}** |", "", "红利税按年：", "", "| 年份 | 红利税（元） |", "|---|---|"]
    for y in sorted(tax_by_year):
        lines.append(f"| {y} | {tax_by_year[y]:,.2f} |")
    div_tax = fees.get("DIVIDEND_TAX", Decimal("0"))
    if report.fees_sum:
        lines += ["", f"红利税占总费用 **{float(div_tax / report.fees_sum):.1%}**。"]
    lines += [
        "",
        "## 3. 空仓 / 现金占比",
        "",
        f"- 交易日：{n_days}",
        f"- 零持仓日：{empty}（{empty / max(n_days, 1):.1%}）",
        f"- 现金≥95% 日：{cash_heavy}（{cash_heavy / max(n_days, 1):.1%}）",
        f"- 平均现金占比：{avg_cash:.1%}",
        "",
        "## 4. 读数提示",
        "",
        "- 策略在牛市若仍跑输 300/红利低波 → 选股/换手问题，非择时。",
        "- 红利税占比过高且持股短 → 优先拉长持有、降频调仓。",
        "- 空仓占比低 → MA200 在长回撤中保护不足。",
        "",
    ]
    md = out_dir / "t312_full_period_diagnosis.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    print(md.read_text(encoding="utf-8"))
    print(f"[written] {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
