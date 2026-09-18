# -*- coding: utf-8 -*-
"""FinAI2.0 数据口径审计（TASK_TRACKER 接续队列 ②）。

校验项：
1. data/macro/index_H20955/H00922 行序与 pct_chg 口径（R8 告警：R7 CSV 降序）；
2. index_000300 / benchmark_510880 行序；
3. etf_bars 512890 factor 跳变（2021-10-22 1:2 分拆）与其他 ETF factor 异常；
4. gc001_daily 序列单调性/重复/缺失。
"""
import glob
import json

import numpy as np
import pandas as pd

report = {}

# ---------- 1. 全收益指数 parquet ----------
for name, f in [
    ("H20955", "data/macro/index_H20955_total_return.parquet"),
    ("H00922", "data/macro/index_H00922_total_return.parquet"),
]:
    df = pd.read_parquet(f)
    df["d"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    ascending = bool((df["d"].diff().dropna() > pd.Timedelta(0)).all())
    dfs = df.sort_values("d").reset_index(drop=True)
    stored = dfs["pct_chg"].astype(float)
    rec = dfs["close"].pct_change() * 100.0
    mask = rec.notna() & stored.notna()
    corr_sorted = float(np.corrcoef(stored[mask], rec[mask])[0, 1])
    desc_sim = dfs["close"] / dfs["close"].shift(-1) - 1.0
    corr_desc = float(np.corrcoef(stored[mask], (desc_sim * 100)[mask])[0, 1])
    cagr = (dfs["close"].iloc[-1] / dfs["close"].iloc[0]) ** (252.0 / (len(dfs) - 1)) - 1
    report[name] = {
        "rows": int(len(df)),
        "date_min": str(dfs["d"].iloc[0].date()),
        "date_max": str(dfs["d"].iloc[-1].date()),
        "file_row_order_ascending": ascending,
        "corr_stored_pct_chg_vs_close_asc": round(corr_sorted, 6),
        "corr_stored_pct_chg_vs_desc_sim": round(corr_desc, 6),
        "cagr_from_close_pct": round(cagr * 100, 2),
        "dup_dates": int(dfs["d"].duplicated().sum()),
        "close_null": int(dfs["close"].isna().sum()),
    }

# ---------- 2. 000300 / 510880 ----------
for name, f in [
    ("idx_000300", "data/macro/index_000300.parquet"),
    ("bench_510880", "data/macro/benchmark_510880.parquet"),
]:
    df = pd.read_parquet(f)
    df["d"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    asc = bool((df["d"].diff().dropna() > pd.Timedelta(0)).all())
    dfs = df.sort_values("d").reset_index(drop=True)
    stored = dfs["pct_chg"].astype(float)
    rec = dfs["close"].pct_change() * 100.0
    mask = rec.notna() & stored.notna()
    report[name] = {
        "rows": int(len(df)),
        "ascending": asc,
        "corr_pct_chg_vs_close_asc": round(float(np.corrcoef(stored[mask], rec[mask])[0, 1]), 6),
        "date_min": str(dfs["d"].iloc[0].date()),
        "date_max": str(dfs["d"].iloc[-1].date()),
    }

# ---------- 3. ETF factor 检查 ----------
etf_issues = {}
for sym in ["sh.512890", "sh.510300", "sh.510500", "sh.511990", "sh.513100", "sz.159915"]:
    frames = []
    for p in sorted(glob.glob("data/etf_bars/%s/*.parquet" % sym)):
        frames.append(pd.read_parquet(p))
    if not frames:
        continue
    e = pd.concat(frames, ignore_index=True)
    dcol = "date" if "date" in e.columns else "trade_date"
    e[dcol] = pd.to_datetime(e[dcol])
    e = e.sort_values(dcol).reset_index(drop=True)
    if "factor" in e.columns:
        fac = e["factor"].astype(float)
        jumps = fac / fac.shift(1)
        big = e[(jumps > 1.5) | (jumps < 0.67)]
        issues = []
        for idx in big.index:
            issues.append({
                "date": str(e.loc[idx, dcol].date()),
                "factor": float(fac.loc[idx]),
                "prev_factor": float(fac.shift(1).loc[idx]) if idx > 0 else None,
                "close": float(e.loc[idx, "close"]),
                "prev_close": float(e["close"].shift(1).loc[idx]) if idx > 0 else None,
            })
        etf_issues[sym] = {
            "rows": int(len(e)),
            "factor_range": [float(fac.min()), float(fac.max())],
            "n_factor_jumps_gt50pct": len(issues),
            "jumps": issues[:10],
        }
    else:
        etf_issues[sym] = {"rows": int(len(e)), "cols": e.columns.tolist()}
report["etf"] = etf_issues

# ---------- 4. GC001 ----------
g = pd.read_parquet("data/rates/gc001_daily.parquet")
g["date"] = pd.to_datetime(g["date"])
report["gc001"] = {
    "rows": int(len(g)),
    "ascending": bool((g["date"].diff().dropna() > pd.Timedelta(0)).all()),
    "date_min": str(g["date"].min().date()),
    "date_max": str(g["date"].max().date()),
    "rate_range": [float(g["rate_annual"].min()), float(g["rate_annual"].max())],
    "null": int(g["rate_annual"].isna().sum()),
    "dup_dates": int(g["date"].duplicated().sum()),
    "negative_rates": int((g["rate_annual"] < 0).sum()),
}

print(json.dumps(report, ensure_ascii=False, indent=2))
