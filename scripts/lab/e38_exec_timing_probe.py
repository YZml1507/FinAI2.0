#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e38 执行层时机探针：T+1 开盘价 vs 同日 VWAP 族价差分布（2026-09-21）。

问题（一阶）：信号在 T 日收盘后生成，现引擎按 FR-BT-6 于 T+1 **开盘价**成交。
若改为 T+1 当日执行——全天 VWAP / 首小时 VWAP（09:30–10:30）/ 尾盘均价
（14:30–15:00 VWAP）——买侧成本与开盘价孰优？

样本：data/minute_probe/ 下 e38_minute_fetch.py 已采的 30 股 × 最近 60 交易日
5m 数据（baostock RAW，停牌全零行已滤）。

统计口径（双侧如实给）：
  - 价差 bp = (exec_alt / open − 1) × 1e4；**买侧负=便宜**，卖侧反之。
  - pooled：全部 stock-day 池化的分布（n≈1740）；mean/std/分位数/P(alt<open)。
  - per-day：每日横截面均值的 60 点序列的 mean 与 t 值——日内相关性下
    pooled t 会虚高，per-day 序列是诚实显著性口径。
  - 质检：分钟线末日 bar 收盘 vs 仓内腾讯 RAW 日线收盘的一致性核对。

仅打印 + 落盘 data/minute_probe/exec_probe.json。无合成数据。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "data" / "minute_probe"
DSTOCKS = ROOT / "data" / "dividend_stocks"

FIRST_HOUR_END = "1030"   # 09:35–10:30 共 12 根
TAIL_START = "1430"       # 14:35–15:00 共 6 根（end-stamped）
BP = 1e4


def _vwap(df: pd.DataFrame) -> float:
    v = df["volume"].to_numpy(dtype=float)
    if v.sum() <= 0:
        return float("nan")
    return float(df["amount"].to_numpy(dtype=float).sum() / v.sum())


def load_sample() -> pd.DataFrame:
    files = sorted(PROBE.glob("*.parquet"))
    if not files:
        raise SystemExit("data/minute_probe/ 无采样 parquet，先跑 e38_minute_fetch.py")
    return pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)


def per_day_points(df: pd.DataFrame) -> pd.DataFrame:
    """每股每日四个执行时点价：open / vwap_day / vwap_h1 / vwap_tail + 参照 prev_close。"""
    recs = []
    for (sym, date), g in df.groupby(["symbol", "date"]):
        g = g.sort_values("hhmm")
        open_ = float(g.iloc[0]["open"])
        vwap_d = _vwap(g)
        h1 = g[g["hhmm"] <= FIRST_HOUR_END]
        tail = g[g["hhmm"] > TAIL_START]
        close_ = float(g.iloc[-1]["close"])
        recs.append({"symbol": sym, "date": date, "open": open_,
                     "vwap_day": vwap_d, "vwap_h1": _vwap(h1),
                     "vwap_tail": _vwap(tail), "close": close_,
                     "day_amount": float(g["amount"].sum())})
    d = pd.DataFrame(recs).sort_values(["symbol", "date"]).reset_index(drop=True)
    d["prev_close"] = d.groupby("symbol")["close"].shift(1)
    d["gap_bp"] = (d["open"] / d["prev_close"] - 1) * BP
    for c in ("vwap_day", "vwap_h1", "vwap_tail"):
        d[f"{c}_vs_open_bp"] = (d[c] / d["open"] - 1) * BP
        d[f"{c}_vs_close_bp"] = (d[c] / d["close"] - 1) * BP
    return d.dropna(subset=["prev_close"])


def dist_stats(s: pd.Series) -> dict:
    s = s.dropna()
    return {"n": int(len(s)), "mean": round(float(s.mean()), 2),
            "median": round(float(s.median()), 2), "std": round(float(s.std()), 2),
            "p10": round(float(s.quantile(0.10)), 2),
            "p25": round(float(s.quantile(0.25)), 2),
            "p75": round(float(s.quantile(0.75)), 2),
            "p90": round(float(s.quantile(0.90)), 2),
            "p_alt_lt_open": round(float((s < 0).mean()), 4),
            "t_pooled": round(float(s.mean() / (s.std() / math.sqrt(len(s)))), 2)}


def daily_t(d: pd.DataFrame, col: str) -> dict:
    per = d.groupby("date")[col].mean()
    return {"n_days": int(len(per)),
            "mean_of_daily_means": round(float(per.mean()), 2),
            "std_of_daily_means": round(float(per.std()), 2),
            "t_daily": round(float(per.mean() / (per.std() / math.sqrt(len(per)))), 2),
            "p_days_negative": round(float((per < 0).mean()), 4)}


def reconciliation(symbols: list[str]) -> dict:
    """分钟线末日 bar close vs 仓内腾讯 RAW 日线 close（2026 分区），逐股核对。"""
    diffs = []
    for sym in symbols[:10]:
        f = DSTOCKS / sym / "2026.parquet"
        mp = PROBE / f"{sym}.parquet"
        if not (f.exists() and mp.exists()):
            continue
        daily = pd.read_parquet(f, columns=["date", "close"])
        daily["date"] = daily["date"].astype(str)
        mins = pd.read_parquet(mp)
        last = (mins.sort_values("hhmm").groupby("date")
                .tail(1)[["date", "close"]].rename(columns={"close": "m_close"}))
        j = daily.merge(last, on="date")
        if len(j):
            diffs.extend(((j["m_close"] / j["close"]) - 1).abs().tolist())
    diffs = np.asarray(diffs)
    return {"pairs": int(len(diffs)), "max_abs_rel": float(diffs.max()),
            "mean_abs_rel": float(diffs.mean()),
            "pct_exact": float((diffs < 1e-9).mean())}


def main() -> int:
    df = load_sample()
    print(f"loaded {len(df)} bars, {df['symbol'].nunique()} syms, "
          f"{df['date'].nunique()} days")
    d = per_day_points(df)
    out: dict = {"sample_stock_days": int(len(d)),
                 "symbols": int(d["symbol"].nunique()),
                 "window": [str(d["date"].min()), str(d["date"].max())]}
    print("\n== 跳空参照（T close→T+1 open）==")
    out["gap_bp"] = dist_stats(d["gap_bp"])
    print(json.dumps(out["gap_bp"], ensure_ascii=False))

    print("\n== T+1 执行时点价差 vs T+1 open（bp，买侧负=省）==")
    for c in ("vwap_day", "vwap_h1", "vwap_tail"):
        col = f"{c}_vs_open_bp"
        out[col] = {"pooled": dist_stats(d[col]), "per_day": daily_t(d, col)}
        print(f"\n[{c}]")
        print("  pooled:", json.dumps(out[col]["pooled"], ensure_ascii=False))
        print("  per-day:", json.dumps(out[col]["per_day"], ensure_ascii=False))

    print("\n== 时点价 vs T+1 收盘（bp，供卖侧参考）==")
    for c in ("vwap_day", "vwap_h1", "vwap_tail"):
        col = f"{c}_vs_close_bp"
        out[col] = dist_stats(d[col])
        print(f"[{col}] {json.dumps(out[col], ensure_ascii=False)}")

    out["daily_amount_yuan"] = dist_stats(d["day_amount"])
    print(f"\n日均成交额分布(元): {json.dumps(out['daily_amount_yuan'], ensure_ascii=False)}")

    out["recon_minute_vs_tencent_daily"] = reconciliation(sorted(d["symbol"].unique()))
    print(f"\n质检(分钟末日bar close vs 腾讯RAW日线): "
          f"{json.dumps(out['recon_minute_vs_tencent_daily'], ensure_ascii=False)}")

    fp = PROBE / "exec_probe.json"
    fp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n落盘 {fp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
