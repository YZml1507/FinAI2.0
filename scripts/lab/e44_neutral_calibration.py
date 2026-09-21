#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e44 暴露配平斜坡标定器（linear_neutral 的 a' 求解）。

口径（docs/E44_NEUTRAL_BAND_PREREG.md 冻结）：
- E[hard] = P(b ≥ attack)（champion mid_cap=0 → 台阶函数）；
- 斜坡 h(b) = clip((b−defense)/(a'−defense), 0, 1)；
- 在标定窗（默认 ≤2020-12-31 训练段，⛔ 禁含评估窗）上二分
  解 a'∈(defense, attack] 使 E[h]=E[hard]；
- 退化门：a'−defense < 0.02 → 斜坡退化为台阶，如实报告。
a' 是宽度历史的确定性函数，非策略内搜索自由度。
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.lab.run_experiment import _load_breadth_series  # noqa: E402

BREADTH_PATH = ROOT / "experiments" / "lab" / "market-breadth-a" / \
    "breadth20_daily.parquet"


def exposure_hard(b: Decimal, attack: Decimal) -> Decimal:
    return Decimal("1") if b >= attack else Decimal("0")


def exposure_ramp(b: Decimal, defense: Decimal, na: Decimal,
                  mid_cap: Decimal) -> Decimal:
    if b >= na:
        return Decimal("1")
    if b < defense:
        return Decimal("0")
    frac = (b - defense) / (na - defense)
    return mid_cap + (Decimal("1") - mid_cap) * frac


def solve_na(series: dict[str, Decimal], defense: Decimal,
             attack: Decimal, mid_cap: Decimal,
             calib_end: str) -> dict:
    train = {d: b for d, b in series.items() if d <= calib_end}
    if len(train) < 100:
        raise SystemExit(f"标定窗样本不足 {len(train)} 日（fail-closed）")
    e_hard = sum(exposure_hard(b, attack) for b in train.values()) / len(train)

    def e_ramp(na: Decimal) -> Decimal:
        return sum(exposure_ramp(b, defense, na, mid_cap)
                   for b in train.values()) / len(train)

    # E[ramp] 随 na 单调减（斜坡越宽期望暴露越低）；
    # na→defense+ 退化为 step@defense：E→P(b≥defense) ≥ e_hard；
    # na→1.0 时 E≈E[frac(b)] 通常 << e_hard。方向由数据定。
    lo, hi = defense + Decimal("0.0001"), Decimal("1.00")
    e_lo, e_hi = e_ramp(lo), e_ramp(hi)
    if not (e_hi <= e_hard <= e_lo):
        raise SystemExit(f"标定不可解：e_hard={e_hard} 不在 "
                         f"[{e_hi},{e_lo}] 内（fail-closed）")
    for _ in range(60):
        mid = (lo + hi) / 2
        if e_ramp(mid) > e_hard:
            lo = mid
        else:
            hi = mid
    na = hi
    return {
        "na": str(na.quantize(Decimal("0.0000001"))),
        "train_days": len(train),
        "calib_end": calib_end,
        "e_hard": str(e_hard),
        "e_ramp_at_na": str(e_ramp(na)),
        "e_ramp_at_attack": str(e_ramp(attack)),
        "degenerate": bool(na - defense < Decimal("0.02")),
        "ramp_width": str(na - defense),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calib-end", default="2020-12-31")
    ap.add_argument("--defense", default="0.25")
    ap.add_argument("--attack", default="0.35")
    ap.add_argument("--mid-cap", default="0.0")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    series = _load_breadth_series(BREADTH_PATH)
    res = solve_na(series, Decimal(args.defense), Decimal(args.attack),
                   Decimal(args.mid_cap), args.calib_end)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
