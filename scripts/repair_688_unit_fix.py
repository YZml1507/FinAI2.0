#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/repair_688_unit_fix.py — 科创板 688 池内日线 volume/amount ×100 单位修复。

背景（2026-09-21 审计发现）：
- `data/dividend_stocks/sh.688*/{year}.parquet` 中全部 56 只科创板标的的早期历史段，
  `volume` 与 `amount` 相对权威源（experiments/lab/market-breadth-a/daily_bars，同为
  腾讯 RAW 口径）系统性放大 ~100 倍（采集端将「手」当「股」写入）。
- 共 49,490 行受影响；其中 24,498 行真实成交额 <5000 万但记录值 ≥5000 万，
  错误通过 `min_daily_amount` 流动性地板。
- OHLC/收盘价两源逐日完全一致，仅 volume/amount 单位异构；非 688 标的审计无此缺陷
  （±3% 以内为口径抖动）。

修复语义：
- 对每行 `pool_amount / alla_amount > 50` 的记录，以 alla（腾讯 RAW，单位=股/元）
  的 volume/amount 覆盖池文件值；其余字段不动。
- 幂等：修复后再跑审计 ratio>50 行数应为 0；重复执行本脚本为 no-op。
- 每符号修复行数/日期范围/比率统计写入 docs/data_repair_688_units_manifest.json。

用法：
    .venv/bin/python scripts/repair_688_unit_fix.py            # 修复并写清单
    .venv/bin/python scripts/repair_688_unit_fix.py --dry-run  # 只审计不写盘
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data.collector import _atomic_write_parquet  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("repair_688_units")

POOL_ROOT = REPO_ROOT / "data" / "dividend_stocks"
ALLA_ROOT = REPO_ROOT / "experiments" / "lab" / "market-breadth-a" / "daily_bars"
MANIFEST = REPO_ROOT / "docs" / "data_repair_688_units_manifest.json"
RATIO_THRESHOLD = 50.0  # 单位级错配下限（正常口径抖动 ±3%）


def repair_symbol(sym: str, dry_run: bool) -> dict:
    alla_path = ALLA_ROOT / f"{sym}.parquet"
    if not alla_path.exists():
        return {"symbol": sym, "status": "no_alla_reference"}
    alla = pd.read_parquet(alla_path, columns=["date", "volume", "amount"])
    alla_idx = alla.set_index("date")

    year_files = sorted(POOL_ROOT.joinpath(sym).glob("*.parquet"))
    if not year_files:
        return {"symbol": sym, "status": "no_pool_files"}

    fixed_rows = 0
    ratios = []
    fixed_dates = []
    for yf in year_files:
        df = pd.read_parquet(yf)
        m = df.join(alla_idx, on="date", rsuffix="_alla")
        bad = m["amount"] / m["amount_alla"] > RATIO_THRESHOLD
        n_bad = int(bad.sum())
        if n_bad:
            ratios.extend((m.loc[bad, "amount"] / m.loc[bad, "amount_alla"]).tolist())
            fixed_dates.extend(m.loc[bad, "date"].astype(str).tolist())
            if not dry_run:
                df.loc[bad, "volume"] = (
                    m.loc[bad, "volume_alla"].round().astype("int64").to_numpy()
                )
                df.loc[bad, "amount"] = m.loc[bad, "amount_alla"].to_numpy()
                _atomic_write_parquet(df, yf)
        fixed_rows += n_bad

    if fixed_rows == 0:
        return {"symbol": sym, "status": "clean"}
    return {
        "symbol": sym,
        "status": "dry_run" if dry_run else "fixed",
        "rows_fixed": fixed_rows,
        "date_min": min(fixed_dates),
        "date_max": max(fixed_dates),
        "ratio_median": round(float(pd.Series(ratios).median()), 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只审计不写盘")
    args = ap.parse_args()

    symbols = sorted(d.name for d in POOL_ROOT.iterdir() if d.is_dir() and d.name.startswith("sh.688"))
    logger.info("审计 %d 只 688 标的（threshold ratio>%.0f）", len(symbols), RATIO_THRESHOLD)

    results = [repair_symbol(s, args.dry_run) for s in symbols]
    fixed = [r for r in results if r.get("rows_fixed")]
    total = sum(r["rows_fixed"] for r in fixed)
    logger.info("受影响符号 %d 只，共 %d 行 %s", len(fixed), total, "待修复" if args.dry_run else "已修复")

    if not args.dry_run:
        manifest = {
            "repair": "688 volume/amount x100 unit fix",
            "date": "2026-09-21",
            "reference_source": "experiments/lab/market-breadth-a/daily_bars (tencent RAW)",
            "threshold": RATIO_THRESHOLD,
            "symbols": results,
            "total_rows_fixed": total,
        }
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("清单写入 %s", MANIFEST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
