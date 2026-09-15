#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""方案 D 核心：全 A 市场宽度（Market Breadth）序列计算器。

口径（19 号报告 §2.1，V5-ALPHA-02）：
  Breadth20(t) = 全 A 中『收盘价 > MA20』的股票数 / 当日有交易的全 A 股票数
  停牌股当日无 K 线自动剔除（腾讯链路特性）。

⚠ 口径局限（如实标注）：新浪日线不含 ST 标记，本序列**含 ST 股**。
  与 19 号报告『剔除 ST』存在偏差，属已知口径差异，产物中显式声明。
⚠ 口径边界：MA20 需 20 根收盘才能算，前 19 日宽度恒 0（失真），
  被策略 warmup（210 根）天然覆盖，不影响交易期判据。

产物隔离（⛔ 不碰权威目录）：
  输入 → experiments/lab/market-breadth-a/daily_bars/*.parquet
  输出 → experiments/lab/market-breadth-a/breadth20_daily.parquet（含出处三件套）
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path("/home/ubuntu/FinAI2.0")
LAB = ROOT / "experiments" / "lab" / "market-breadth-a"
BAR_DIR = LAB / "daily_bars"
OUT = LAB / "breadth20_daily.parquet"
META = LAB / "BREADTH_META.json"
MA_WIN = 20
MIN_UNIVERSE = 100  # 当日可交易票少于此值视为数据异常，宽度记 NaN


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def src_hash() -> str:
    h = hashlib.sha256()
    for p in sorted(BAR_DIR.glob("*.parquet")):
        h.update(p.name.encode())
        h.update(str(p.stat().st_size).encode())
    return h.hexdigest()[:16]


def load_all() -> pd.DataFrame:
    frames = []
    files = sorted(BAR_DIR.glob("*.parquet"))
    print(f"[load] 读取 {len(files)} 只票日线…", flush=True)
    for i, f in enumerate(files, 1):
        try:
            df = pd.read_parquet(f, columns=["date", "close", "code"])
            frames.append(df)
        except Exception as e:
            print(f"  [skip] {f.name}: {str(e)[:60]}", flush=True)
        if i % 1000 == 0:
            print(f"  [load] {i}/{len(files)}", flush=True)
    big = pd.concat(frames, ignore_index=True)
    big["date"] = pd.to_datetime(big["date"])
    return big


def compute_breadth(big: pd.DataFrame) -> pd.DataFrame:
    print(f"[compute] 总行数 {len(big)}，开始按票算 MA20…", flush=True)
    big = big.sort_values(["code", "date"])
    big["ma20"] = big.groupby("code")["close"].transform(
        lambda s: s.rolling(MA_WIN, min_periods=MA_WIN).mean())
    big["above"] = (big["close"] > big["ma20"]).astype("float")
    # 当日可交易票 = 当日有 K 线；停牌票无 K 线自动剔除
    daily = big.groupby("date").agg(
        universe=("code", "count"),
        above_cnt=("above", "sum"),
    ).reset_index()
    daily["breadth20"] = daily["above_cnt"] / daily["universe"]
    daily.loc[daily["universe"] < MIN_UNIVERSE, "breadth20"] = float("nan")
    return daily.sort_values("date").reset_index(drop=True)


def main() -> int:
    if not BAR_DIR.exists():
        print("[FATAL] 全 A 日线目录不存在，先跑 collect_market_breadth_a.py")
        return 1
    big = load_all()
    if big.empty:
        print("[FATAL] 无可用日线数据")
        return 1
    daily = compute_breadth(big)
    valid = daily.dropna(subset=["breadth20"])
    print(f"[result] 交易日 {len(daily)} 天，有效宽度 {len(valid)} 天，"
          f"范围 {valid['date'].min().date()} -> {valid['date'].max().date()}")
    print(f"[result] 宽度分位: min={valid['breadth20'].min():.3f} "
          f"p25={valid['breadth20'].quantile(.25):.3f} "
          f"median={valid['breadth20'].median():.3f} "
          f"p75={valid['breadth20'].quantile(.75):.3f} "
          f"max={valid['breadth20'].max():.3f}")
    daily.to_parquet(OUT, index=False)
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": git_sha(),
        "source_data_hash": src_hash(),
        "ma_window": MA_WIN,
        "min_universe": MIN_UNIVERSE,
        "trading_days": int(len(daily)),
        "valid_days": int(len(valid)),
        "first_valid": str(valid["date"].min().date()) if len(valid) else None,
        "last_valid": str(valid["date"].max().date()) if len(valid) else None,
        "caliber_note": "含 ST 股（新浪日线无 ST 标记）；停牌股当日无K线自动剔除；前19日宽度恒0失真（MA20未成型，被策略warmup覆盖）",
        "source": "sina daily bars via akshare stock_zh_a_daily (collect_market_breadth_a.py)",
    }
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] 宽度序列 → {OUT}")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
