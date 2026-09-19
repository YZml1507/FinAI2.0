#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/lab/build_crowding_series.py — D7 拥挤度序列构建（e19 预登记 §一）。

信号定义（冻结口径，与 /tmp/d7_probe/d7_shadow.py 同构）：
- 池 dv 面板：data/dividend_stocks/{sym}/{year}.parquet 的 dividend_yield
  列（PIT 滚动口径，单位=小数；×100 转 %）。
- 利差 a1（主口径）：spread[T] = mean(dv%[T] | dv%[T] ≥ 3.0) − yield10[T]；
  a2（稳健口径）：spread[T] = median(dv%[T] | 全池) − yield10[T]。
- 拥挤度 crowd = −spread；crowd_pct[T] = crowd 在 [T−756, T] 窗内百分位，
  min_periods=504（roll3y ≈ 2 年暖机，2015-2016 结构盲区）。
- PIT：crowd_pct[T] 仅用 ≤T 收盘数据，与 breadth20_daily 同约定
  （T 日决策、T+1 成交 ⇒ 无前视）。

产物：data/macro/crowding_roll3y_daily.parquet（a1）与
      data/macro/crowding_roll3y_daily_a2.parquet（a2），
      列为 (date, crowd_pct)；NaN 段（暖机期）不落行——引擎查不到=中性。

幂等：同输入重跑逐值一致（构建参数全在文件头常量，无随机性）。
"""
from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_crowding")

POOL_ROOT = REPO_ROOT / "data" / "dividend_stocks"
TSY_F = REPO_ROOT / "data" / "macro" / "treasury_yield_10y.parquet"
OUT_A1 = REPO_ROOT / "data" / "macro" / "crowding_roll3y_daily.parquet"
OUT_A2 = REPO_ROOT / "data" / "macro" / "crowding_roll3y_daily_a2.parquet"

ROLL_WIN = 756      # 滚动 3 年交易日
ROLL_MIN = 504      # min_periods = 2 年（暖机下限）
DV_FLOOR_PCT = 3.0  # a1 口径候选池下限（与策略 min_dividend_yield 同语义）


def load_pool_dv() -> pd.DataFrame:
    """池内全部标的 dividend_yield 面板：index=date, columns=symbol。"""
    frames = []
    for sym_dir in sorted(POOL_ROOT.iterdir()):
        if not sym_dir.is_dir() or sym_dir.name.startswith(("exdiv", "_")):
            continue
        files = sorted(sym_dir.glob("*.parquet"))
        if not files:
            continue
        df = pd.concat(
            [pd.read_parquet(f, columns=["date", "dividend_yield"]) for f in files],
            ignore_index=True)
        df = df.dropna(subset=["dividend_yield"])
        frames.append(df.assign(code=sym_dir.name))
    big = pd.concat(frames, ignore_index=True)
    big["date"] = pd.to_datetime(big["date"])
    return big.pivot_table(index="date", columns="code",
                           values="dividend_yield", aggfunc="last").sort_index()


def roll_pct(s: pd.Series) -> pd.Series:
    """窗内百分位（含当日）：rank of last value / window count。"""
    return s.rolling(ROLL_WIN, min_periods=ROLL_MIN).apply(
        lambda w: (w <= w[-1]).mean(), raw=True)


def build(dv_pct: pd.DataFrame, y10: pd.Series) -> dict[str, pd.Series]:
    y10_al = y10.reindex(dv_pct.index).ffill()
    spread_a1 = dv_pct[dv_pct >= DV_FLOOR_PCT].mean(axis=1) - y10_al
    spread_a2 = dv_pct.median(axis=1) - y10_al
    return {"a1": roll_pct(-spread_a1), "a2": roll_pct(-spread_a2)}


def main() -> int:
    logger.info("加载池 dv 面板 ...")
    dv = load_pool_dv() * 100.0
    logger.info("面板 %s", dv.shape)

    tsy = pd.read_parquet(TSY_F)
    tsy["date"] = pd.to_datetime(tsy["date"])
    y10 = tsy.set_index("date")["yield_10y"].sort_index()

    pcts = build(dv, y10)
    for tag, (pct, out) in {"a1": (pcts["a1"], OUT_A1),
                            "a2": (pcts["a2"], OUT_A2)}.items():
        df = pct.dropna().rename("crowd_pct").reset_index()
        df["date"] = df["date"].dt.strftime("%Y-%m-%d")
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out, index=False)
        h = hashlib.sha256(out.read_bytes()).hexdigest()[:12]
        logger.info("[%s] %s 行 %s→%s  sha256=%s",
                    tag, len(df), df["date"].iloc[0], df["date"].iloc[-1], h)
        logger.info("[%s] pct describe: %s", tag,
                    pct.describe().round(3).loc[["mean", "50%", "max"]].tolist())
    return 0


if __name__ == "__main__":
    sys.exit(main())
