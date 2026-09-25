#!/usr/bin/env python3
"""e104 股权质押轴信号评估——cninfo 公告质押/解押全史。

数据: data/cninfo_pledge/*.parquet
      (SECCODE, DECLAREDATE=公告日, F006N=质押万股, F012N=解押万股)
PIT: asof = DECLAREDATE + 1 天（公告披露后次日可用，保守错后）。
信号(逐股月末截面, 近90日滚动聚合):
  plg_net3m = Σ(F006N − F012N) 净质押万股, log1p
  plg_cnt3m = 质押笔数 (F006N 非空)
  plg_rel3m = 解押笔数 (F012N 非空)
评估: fwd20d 月频 Spearman IC + t + 安慰剂 ±20d。判强: |IC|≥0.04 且 |t|≥3 且安慰剂 p<0.05。
区间: 2016-01 ~ 2024-12（2025+ 永久 OOS 只观察登记）。
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SIG_DIR = ROOT / "experiments" / "lab" / "e104"
OUT = SIG_DIR / "eval.json"
LAG = pd.Timedelta(days=1)
WIN = pd.Timedelta(days=90)


def _ts(code: str) -> str:
    p = code[0]
    if p in "03":
        return f"sz.{code}"
    if p == "6":
        return f"sh.{code}"
    return f"bj.{code}"


def load_pledge() -> pd.DataFrame:
    fs = sorted(glob.glob(str(ROOT / "data/cninfo_pledge/*.parquet")))
    df = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    df = df[df["DECLAREDATE"].notna() & df["SECCODE"].notna()].copy()
    df["asof"] = pd.to_datetime(df["DECLAREDATE"], errors="coerce") + LAG
    df = df.dropna(subset=["asof"])
    df["ts_code"] = df["SECCODE"].astype(str).str.zfill(6).map(_ts)
    df["amt_in"] = pd.to_numeric(df["F006N"], errors="coerce").fillna(0)
    df["amt_out"] = pd.to_numeric(df["F012N"], errors="coerce").fillna(0)
    df["cnt_in"] = (df["F006N"].notna() & df["F006N"].astype(str).ne("")).astype(int)
    df["cnt_out"] = (df["F012N"].notna() & df["F012N"].astype(str).ne("")).astype(int)
    return df[["ts_code", "asof", "amt_in", "amt_out", "cnt_in", "cnt_out"]]


def fwd20_returns() -> pd.DataFrame:
    px = []
    for f in sorted(glob.glob(str(ROOT / "data/daily_bars/[sb][hzj].*/*.parquet"))):
        px.append(pd.read_parquet(f, columns=["code", "date", "close"]))
    bars = pd.concat(px, ignore_index=True)
    bars["date"] = pd.to_datetime(bars["date"])
    bars["close"] = pd.to_numeric(bars["close"], errors="coerce")
    bars = bars.sort_values(["code", "date"])
    bars["fwd20"] = bars.groupby("code")["close"].shift(-20) / bars["close"] - 1
    return bars[["code", "date", "fwd20"]].rename(columns={"code": "ts_code"})


def flow_monthly(df: pd.DataFrame, ends: pd.DatetimeIndex) -> pd.DataFrame:
    """Trailing-90d flow aggregates per stock as of each month end."""
    recs = []
    df = df.sort_values("asof")
    codes = df["ts_code"].unique()
    for e in ends:
        lo = e - WIN
        w = df[(df["asof"] > lo) & (df["asof"] <= e)]
        if w.empty:
            continue
        g = w.groupby("ts_code").agg(
            plg_net3m=("amt_in", lambda s: s.sum()),
            plg_cnt3m=("cnt_in", "sum"),
            plg_rel3m=("cnt_out", "sum"))
        g["amt_out_sum"] = w.groupby("ts_code")["amt_out"].sum()
        g["plg_net3m"] = g["plg_net3m"] - g["amt_out_sum"]
        g["plg_net3m"] = np.log1p(g["plg_net3m"].clip(lower=0))
        g = g.drop(columns=["amt_out_sum"]).reset_index()
        g["date"] = e
        recs.append(g)
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


def main() -> None:
    pl = load_pledge()
    print(f"pledge rows={len(pl)} stocks={pl['ts_code'].nunique()}", flush=True)
    rets = fwd20_returns()
    ends = pd.date_range("2016-01-31", "2024-12-31", freq="ME")
    sig = flow_monthly(pl, ends)
    out = {"lag": "DECLAREDATE+1d", "win_days": 90,
           "eval_window": "2016-01..2024-12", "signals": {}}
    out["months"] = int(sig["date"].nunique()) if not sig.empty else 0
    out["covered_stocks"] = int(sig["ts_code"].nunique()) if not sig.empty else 0
    for col in ["plg_net3m", "plg_cnt3m", "plg_rel3m"]:
        res = eval_ic(sig, rets, col)
        plb = [eval_ic(sig, rets, col, s)["ic"] for s in (-20, 20)]
        plb = [p for p in plb if p is not None]
        res["placebo_ics"] = plb
        res["placebo_p"] = (
            round(float(np.mean([abs(p) >= abs(res["ic"]) for p in plb])), 3)
            if plb and res["ic"] is not None else None)
        out["signals"][col] = res
    SIG_DIR.mkdir(parents=True, exist_ok=True)
    sig.to_parquet(SIG_DIR / "sig_pledge_monthly.parquet", index=False)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
