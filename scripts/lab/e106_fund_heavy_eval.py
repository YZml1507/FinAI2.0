#!/usr/bin/env python
"""e106 基金重仓拥挤度轴评估 — cninfo fund_heavy 表。
信号: fh_cnt(log1p持基家数)/fh_cnt_chg(家数环比)/fh_mv_chg(市值环比)
PIT: asof = ENDDATE + 45d。月频 Spearman IC + t + ±20d 安慰剂。
"""
import argparse
import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BARS_DIR = ROOT / "data" / "daily_bars"
OUT_DIR = ROOT / "experiments" / "lab" / "e106"


def _ts(code: str) -> str:
    p = str(code)[0]
    if p in "03":
        return f"sz.{code}"
    if p == "6":
        return f"sh.{code}"
    return f"bj.{code}"


def load_quarterly() -> pd.DataFrame:
    fs = sorted(glob.glob(str(ROOT / "data" / "cninfo_fund_heavy" / "*.parquet")))
    d = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    d = d.dropna(subset=["SECCODE", "ENDDATE", "F001N"])
    d["qend"] = pd.to_datetime(d["ENDDATE"])
    d["asof"] = d["qend"] + pd.Timedelta(days=45)
    d["ts_code"] = d["SECCODE"].astype(str).str.zfill(6).map(_ts)
    d = d.sort_values(["SECCODE", "qend"])
    g = d.groupby("SECCODE")
    d["fh_cnt"] = np.log1p(d["F001N"].clip(lower=0))
    d["fh_cnt_chg"] = g["F001N"].diff()
    prev_mv = g["F003N"].shift(1)
    d["fh_mv_chg"] = np.log(d["F003N"].clip(lower=1) / prev_mv.clip(lower=1))
    return d[["ts_code", "asof", "fh_cnt", "fh_cnt_chg", "fh_mv_chg"]]


def fwd20_returns() -> pd.DataFrame:
    px = []
    for f in sorted(glob.glob(str(BARS_DIR / "[sb][hzj].*/*.parquet"))):
        px.append(pd.read_parquet(f, columns=["code", "date", "close"]))
    bars = pd.concat(px, ignore_index=True)
    bars["date"] = pd.to_datetime(bars["date"])
    bars["close"] = pd.to_numeric(bars["close"], errors="coerce")
    bars = bars.sort_values(["code", "date"])
    bars["fwd20"] = bars.groupby("code")["close"].shift(-20) / bars["close"] - 1
    return bars[["code", "date", "fwd20"]].rename(columns={"code": "ts_code"})


def monthly_panel(q: pd.DataFrame, ends: pd.DatetimeIndex) -> pd.DataFrame:
    """每月末取 asof<=月末 的最新季度值（PIT 对齐）。"""
    q = q.sort_values("asof")
    recs = []
    idx = q["asof"].values
    for e in ends:
        m = idx <= np.datetime64(e)
        if not m.any():
            continue
        latest = q.iloc[: m.sum()].groupby("ts_code").tail(1)
        s = latest[["ts_code", "fh_cnt", "fh_cnt_chg", "fh_mv_chg"]].copy()
        s["date"] = e
        recs.append(s)
    return pd.concat(recs, ignore_index=True) if recs else pd.DataFrame()


def eval_ic(sig: pd.DataFrame, rets: pd.DataFrame, col: str, shift_days: int = 0) -> dict:
    d = sig[["ts_code", "date", col]].copy()
    if shift_days:
        d["date"] = d["date"] + pd.Timedelta(days=shift_days)
    d = d.merge(rets, on=["ts_code", "date"], how="inner").dropna(subset=[col, "fwd20"])
    ic = d.groupby("date").apply(
        lambda g: g[col].corr(g["fwd20"], method="spearman") if len(g) >= 20 else np.nan,
        include_groups=False)
    ic = ic.dropna()
    if len(ic) < 6:
        return {"ic": None, "t": None, "months": len(ic)}
    t = ic.mean() / (ic.std(ddof=1) / np.sqrt(len(ic))) if ic.std(ddof=1) > 0 else 0.0
    return {"ic": round(float(ic.mean()), 4), "t": round(float(t), 2), "months": int(len(ic))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.parse_args()
    q = load_quarterly()
    rets = fwd20_returns()
    ends = pd.date_range(
        max(q["asof"].min(), pd.Timestamp("2015-03-01")),
        q["asof"].max(), freq="ME")
    sig = monthly_panel(q, ends)

    out = {"n_rows": int(len(q)), "n_stock_months": int(len(sig)),
           "asof_span": [str(q["asof"].min().date()), str(q["asof"].max().date())],
           "signals": {}}
    for col in ["fh_cnt", "fh_cnt_chg", "fh_mv_chg"]:
        res = eval_ic(sig, rets, col)
        pl = [eval_ic(sig, rets, col, s)["ic"] for s in (-20, 20)]
        pl = [p for p in pl if p is not None]
        res["placebo_ics"] = pl
        res["placebo_p"] = (
            round(float(np.mean([abs(p) >= abs(res["ic"]) for p in pl])), 3)
            if pl and res["ic"] is not None else None)
        out["signals"][col] = res

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "eval.json").write_text(json.dumps(out, indent=2))
    sig.to_parquet(OUT_DIR / "sig_fund_heavy_monthly.parquet", index=False)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
