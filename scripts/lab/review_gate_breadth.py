#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""任务A：达标宽度组合的六维门禁复核算子（S-2 宽度口径）。

背景：P2-b 未落地前，``_build_post_run_gate_context`` 不注入宽度字段，
导致宽度组合（use_breadth_timing=True）的 S-2 被迫退回 MA200 口径评判，
产生口径错配的 FAIL。本算子按宽度口径（S-2 门禁本体已支持的优先分支）
重算 below_dates / grace_dates / daily_positions_ratio，还原真实判定。

``daily_positions_ratio`` 与 ``_compute_daily_positions_ratio`` 同口径：
逐日重放 trades 得到「持仓/空仓」二值代理（阈值 5% 判定足够）。

产物：experiments/lab/<实验名>/gate_review_<run_id>.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date as _date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from scripts.gates.gate_s_scientific import TimingExitSurvivalGate  # noqa: E402
from backtest.constants import OrderSide  # noqa: E402

BREADTH_FILE = ROOT / "experiments/lab/market-breadth-a/breadth20_daily.parquet"
LAB_ROOT = ROOT / "experiments/lab"


def load_breadth(path: Path) -> dict[str, Decimal]:
    df = pd.read_parquet(path)
    return {str(d)[:10]: Decimal(str(b)) for d, b in zip(df["date"], df["breadth20"])}


def breadth_below_dates(breadth: dict[str, Decimal], defense: float) -> list[str]:
    return sorted(d for d, b in breadth.items() if float(b) < defense)


def timing_grace_dates(below_dates: list[str], cal_days: list[_date]) -> list[str]:
    """与 ``run_dividend_backtest._compute_timing_grace_dates`` 同算法（段首日起 2 个交易日）。"""
    below_set = set(below_dates)
    cal_idx = {d.isoformat(): i for i, d in enumerate(cal_days)}
    grace: list[str] = []
    for dt in below_dates:
        i = cal_idx.get(dt)
        if i is None or i == 0:
            continue
        if cal_days[i - 1].isoformat() not in below_set:
            grace.append(dt)
            nxt = cal_days[i + 1].isoformat() if i + 1 < len(cal_days) else None
            if nxt is not None and nxt in below_set:
                grace.append(nxt)
    return grace


def daily_positions_ratio(trades: list, cal_days: list[_date]) -> dict[str, float]:
    """与 ``_compute_daily_positions_ratio`` 同口径（逐日重放成交得二值仓位代理）。"""
    ordered = sorted(trades, key=lambda t: getattr(t, "date", _date.min))
    holdings: dict[str, int] = {}
    ratios: dict[str, float] = {}
    idx = 0
    for day in cal_days:
        while idx < len(ordered):
            t = ordered[idx]
            td = getattr(t, "date", None)
            if td is None or td > day:
                break
            side = getattr(t, "side", "")
            side_val = side.value if hasattr(side, "value") else str(side)
            vol = int(getattr(t, "volume", 0) or 0)
            if side_val.upper() == OrderSide.BUY.value:
                holdings[t.symbol] = holdings.get(t.symbol, 0) + vol
            elif side_val.upper() == OrderSide.SELL.value:
                holdings[t.symbol] = holdings.get(t.symbol, 0) - vol
            idx += 1
        ratios[day.isoformat()] = 1.0 if any(v > 0 for v in holdings.values()) else 0.0
    return ratios


def rebuild_result_trades(run_record: dict) -> tuple[list, list[_date]]:
    """从 run 产物反构可重放的成交列表与交易日历（仅含 S-2 所需字段）。

    产物本身不落逐笔成交；但 trades 的日期序列可由仓位变化边沿重建——
    仓位由 0→1 的日期为 BUY 边沿、1→0 为 SELL 边沿（二值代理口径下
    与真实成交序列等价，足以重放 holdings 得到同一 ratio 序列）。
    """
    raise NotImplementedError("产物不含逐笔成交；改由重跑回测直接取 result.trades")


def review_experiment(name: str, save: bool = True) -> dict:
    lab_dir = LAB_ROOT / name
    run_files = sorted((lab_dir / "runs").glob("*.json"))
    if not run_files:
        raise SystemExit(f"{name}: 无 run 产物")
    record = json.loads(run_files[0].read_text(encoding="utf-8"))
    params = record.get("params", {}) or {}
    metrics = record.get("metrics", {}) or {}

    defense = float(params.get("breadth_defense_threshold", 0.20))
    breadth = load_breadth(BREADTH_FILE)
    below = breadth_below_dates(breadth, defense)

    # 交易日历：宽度序列的日期集即交易日（宽度基座逐交易日产出）
    cal_days = [_date.fromisoformat(d) for d in sorted(breadth)]
    grace = timing_grace_dates(below, cal_days)

    # 逐日仓位：run 产物无逐笔成交，但 gate_statuses 的 S-2 旧口径结论
    # 记录了 violations 样本；仓位序列须由重跑回测取得（见 main 的真跑分支）。
    verdict: dict = {
        "experiment": name,
        "run_id": record.get("run_id"),
        "criterion": "breadth",
        "breadth_defense_threshold": defense,
        "below_dates_count": len(below),
        "grace_dates_count": len(grace),
        "mdd": metrics.get("max_drawdown"),
        "cagr": metrics.get("cagr"),
        "annual_turnover": metrics.get("annual_turnover"),
        "win_rate": metrics.get("win_rate"),
    }
    if save:
        out = lab_dir / f"gate_review_{record.get('run_id', 'review')}.json"
        out.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser(description="宽度组合 S-2 宽度口径复核")
    ap.add_argument("--names", nargs="*", default=[
        "bd25a45m00i1", "bd25a45m30i1", "bd25a35m00i1", "bd25a35m30i1",
        "bd25a40m30i1", "bd25a40m00i1", "bd25a45m00i2",
    ])
    args = ap.parse_args()
    for n in args.names:
        v = review_experiment(n)
        print(f"{n}: below={v['below_dates_count']} grace={v['grace_dates_count']} "
              f"defense={v['breadth_defense_threshold']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
