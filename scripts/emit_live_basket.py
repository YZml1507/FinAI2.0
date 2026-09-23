#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""实盘调仓清单发射器（Q3 落地包）。

输入最新分数 parquet + 当前日线快照，按生产构型语义输出等权目标篮：
  top-N by score → 当日可交易过滤（停牌/涨跌停）→ 20 日均成交额下限
  → 等权市值 → 100 股整手 → min_position_value 下限迭代剔除
→ 名单/股数/金额/候补表 CSV+MD。

口径与 ``strategy.score_basket`` + ``portfolio.plan_positions`` 保持一致：
  * 分数取最新 sig_date（≤ max_score_age_days 自然日）
  * 停牌（tradestatus!=1 或当日无 bar）跳过
  * 涨跌停跳过（skip_limit）：pctChg 触及板块限幅-0.3pp 内视为触板
  * 流动性 = 近 20 交易日 amount 均值 ≥ --min-amount
  * 单票目标 = capital/存活数（整手后 < --min-pos 剔除重算）

⛔ 本脚本只产清单，不下单；实盘执行按 runbook（docs/DEPLOY_LIVE.md）。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date as _date
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import pandas as pd

_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root))

from data.cleaner import board_limit_pct  # noqa: E402
from strategy.score_basket import normalize_score_code  # noqa: E402
from strategy.veto import load_veto_series  # noqa: E402

logger = logging.getLogger("emit_live_basket")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

_LOOKBACK = 20


@dataclass(frozen=True)
class Snap:
    symbol: str
    last_date: pd.Timestamp  # 最新 bar 日（陈旧 = 停牌/缺数据）
    close: Decimal
    amount20: Decimal       # 近 20 日成交额均值（元）
    trading: bool           # 最新 bar 上 tradestatus==1
    at_limit: bool          # 触及涨跌停
    is_st: bool


def _load_scores(path: Path) -> tuple[_date, dict[str, float]]:
    df = pd.read_parquet(path)
    need = {"sig_date", "ts_code", "score"}
    if not need.issubset(df.columns):
        raise ValueError(f"分数表缺列 {need - set(df.columns)}")
    df = df.dropna(subset=["score"])
    df["sig_date"] = pd.to_datetime(df["sig_date"]).dt.date
    sig_day = max(df["sig_date"])
    row = df[df["sig_date"] == sig_day]
    out: dict[str, float] = {}
    for c, s in zip(row["ts_code"], row["score"]):
        try:
            out[normalize_score_code(c)] = float(s)
        except ValueError:
            continue
    return sig_day, out


def _snap(sym_dir: Path, asof: pd.Timestamp, lookback: int) -> Snap | None:
    parts = [pd.read_parquet(p) for p in sorted(sym_dir.glob("*.parquet"))
             if p.stem.isdigit()]
    if not parts:
        return None
    df = pd.concat(parts, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["date"] <= asof].sort_values("date").tail(max(lookback, 2))
    if df.empty:
        return None
    last = df.iloc[-1]
    # 陈旧快照由调用侧按池内最新 bar 日剔除；此处只记录末行状态
    trading = str(last.get("tradestatus", "1")) == "1"
    close = Decimal(str(last["close"]))
    amt20 = Decimal(str(df["amount"].tail(lookback).mean()))
    pct = last.get("pctChg")
    is_st = bool(int(last.get("isST", 0) or 0))
    lim = Decimal("5") if is_st else Decimal(str(board_limit_pct(sym_dir.name)))
    at_limit = False
    if pct is not None and pd.notna(pct):
        at_limit = abs(Decimal(str(pct))) >= (lim - Decimal("0.3"))
    return Snap(sym_dir.name, last["date"], close, amt20,
                trading, at_limit, is_st)


def _plan(targets: list[Snap], capital: Decimal, min_pos: Decimal,
          lot: int = 100) -> tuple[list[tuple[Snap, int, Decimal]], Decimal]:
    """等权目标 → 整手股数 → 低于 min_pos 剔除重算（迭代至稳定）。"""
    alive = list(targets)
    while True:
        if not alive:
            return [], capital
        per = capital / Decimal(len(alive))
        rows = []
        dropped = []
        used = Decimal("0")
        for s in alive:
            shares = int((per / s.close).to_integral_value(rounding=ROUND_DOWN)
                         // lot) * lot
            value = s.close * shares
            if value < min_pos:
                dropped.append(s)
            else:
                rows.append((s, shares, value))
                used += value
        if not dropped:
            return rows, capital - used
        alive = [s for s in alive if s not in dropped]


def _veto_banned_at(veto: dict, asof: _date) -> frozenset[str]:
    """asof 当日（或最近 ≤asof 交易日）生效的买入否决集；无覆盖→空集。"""
    days = [d for d in veto if d <= asof]
    return veto[max(days)] if days else frozenset()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scores", type=Path, required=True)
    ap.add_argument("--topn", type=int, default=20)
    ap.add_argument("--capital", type=Decimal, default=Decimal("150000"))
    ap.add_argument("--min-amount", type=Decimal, default=Decimal("5000000"))
    ap.add_argument("--min-pos", type=Decimal, default=Decimal("5000"))
    ap.add_argument("--max-score-age", type=int, default=45)
    ap.add_argument("--data-path", type=Path,
                    default=_root / "data" / "daily_bars")
    ap.add_argument("--bench", type=int, default=10, help="候补名单长度")
    ap.add_argument("--veto-path", type=Path, action="append", default=None,
                    help="e37 否决序列 parquet（可重复传，多文件按日期合并；缺省不启用——生产构型应启用）")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    sig_day, scores = _load_scores(args.scores)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    # 快照评估：top 队列 + 候补（多取 bench+若干冗余应对剔除）
    pool = ranked[: args.topn + args.bench + 15]
    # 有效交易日 = 被评票自身最新 bar 日众数（指数目录不齐备时用群体口径，
    # 避免盘前无当日 bar 把全体判成停牌）
    snaps: dict[str, Snap] = {}
    for sym, _ in pool:
        d = args.data_path / sym
        if not d.is_dir():
            continue
        snap = _snap(d, pd.Timestamp("2262-01-01"), _LOOKBACK)
        if snap is not None:
            snaps[sym] = snap
    if not snaps:
        raise SystemExit("无可用行情快照——检查 --data-path")
    asof = max(s.last_date for s in snaps.values()).date()
    age = (asof - sig_day).days

    banned: frozenset[str] = frozenset()
    if args.veto_path:
        veto: dict = {}
        for vp in args.veto_path:
            veto.update(load_veto_series(vp))
        banned = _veto_banned_at(veto, asof)
        if banned:
            logger.info(f"否决序列生效（asof {asof} 最近可用期）：{len(banned)} 只禁买")
        else:
            logger.warning(f"否决序列无 ≤{asof} 的日期——本清单未应用否决")
    logger.info(f"最新分数期 {sig_day}（{len(scores)} 标的，距数据端 {asof} "
                f"{age} 天）")
    if age > args.max_score_age:
        logger.warning(f"分数期超过 {args.max_score_age} 天，已过期——"
                       "先产出新分数再用本清单")

    chosen: list[Snap] = []
    bench: list[Snap] = []
    for sym, _ in pool:
        s = snaps.get(sym)
        if s is None or s.last_date.date() != asof or not s.trading \
                or s.at_limit or s.amount20 < args.min_amount \
                or sym in banned:
            continue
        if len(chosen) < args.topn:
            chosen.append(s)
        elif len(bench) < args.bench:
            bench.append(s)
        if len(chosen) >= args.topn and len(bench) >= args.bench:
            break

    rows, cash_left = _plan(chosen, args.capital, args.min_pos)
    if not rows:
        raise SystemExit("无存活标的——检查资金/下限或分数期")

    out = args.out or (_root / "experiments" / "live" /
                       f"basket_{asof.isoformat()}_top{args.topn}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    recs = []
    for i, (s, shares, value) in enumerate(rows, 1):
        recs.append({
            "rank": i, "symbol": s.symbol, "shares": shares,
            "close": str(s.close), "target_value": str(value.quantize(Decimal("1"))),
            "amount20_m": str((s.amount20 / Decimal("10000")).quantize(Decimal("0.1"))),
            "is_st": s.is_st,
        })
    pd.DataFrame(recs).to_csv(out, index=False)
    bench_recs = [{"symbol": s.symbol, "close": str(s.close),
                   "amount20_m": str((s.amount20 / Decimal("10000")).quantize(Decimal("0.1"))),
                   "is_st": s.is_st} for s in bench]
    meta = {
        "sig_date": sig_day.isoformat(), "asof": asof.isoformat(),
        "topn": args.topn, "capital": str(args.capital),
        "min_amount": str(args.min_amount), "min_pos": str(args.min_pos),
        "cash_left": str(cash_left.quantize(Decimal("1"))),
        "n_rows": len(rows), "bench": bench_recs,
        "scores_path": str(args.scores),
        "veto_paths": [str(v) for v in (args.veto_path or [])],
        "veto_n": len(banned),
    }
    (out.parent / (out.stem + "_meta.json")).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2))

    print(f"\n=== 调仓清单 {asof}（分数期 {sig_day}，top{len(rows)}）===")
    print(f"{'#':>3} {'代码':<10} {'股数':>6} {'最新价':>8} {'目标市值':>9} "
          f"{'20日均额(万)':>11} ST")
    for i, (s, shares, value) in enumerate(rows, 1):
        print(f"{i:>3} {s.symbol:<10} {shares:>6} {s.close:>8} "
              f"{value.quantize(Decimal('1')):>9} "
              f"{(s.amount20/Decimal('10000')).quantize(Decimal('0.1')):>11} "
              f"{'ST' if s.is_st else ''}")
    print(f"现金余量 ≈ {cash_left.quantize(Decimal('1'))} 元")
    if bench_recs:
        print("\n候补（剔除/停牌替补，按序）：",
              ", ".join(b['symbol'] for b in bench_recs))
    print(f"\n已写 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
