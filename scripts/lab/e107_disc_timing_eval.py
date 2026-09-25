#!/usr/bin/env python
"""e107 披露时点选择轴评估 — cninfo disclosure_schedule 表。
信号: disc_late(报告期→披露日间隔)/disc_resched(预约→实际推迟)
PIT: asof = F006D。月频 Spearman IC + t + ±20d 安慰剂。
"""
import argparse
import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BARS_DIR = ROOT / "data" / "daily_bars"
OUT_DIR = ROOT / "experiments" / "lab" / "e107"


def _ts(code: str) -> str:
    p = str(code)[0]
    if p in "03":
        return f"sz.{code}"
    if p == "6":
        return f"sh.{code}"
    return f"bj.{code}"


def load_events() -> pd.DataFrame:
    fs = sorted(glob.glob(str(ROOT / "data" / "cninfo_disclosure_schedule" / "*.parquet")))
    d = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    d = d.dropna(subset=["SECCODE", "F001D", "F006D"])
    d["qend"] = pd.to_datetime(d["F001D"], errors="coerce")
    d["asof"] = pd.to_datetime(d["F006D"], errors="coerce")
    d["first_sched"] = pd.to_datetime(d["F002D"], errors="coerce")
    d["ts_code"] = d["SECCODE"].astype(str).str.zfill(6).map(_ts)
    d["disc_late"] = (d["asof"] - d["qend"]).dt.days
    d["disc_resched"] = (d["asof"] - d["first_sched"]).dt.days
    d = d[(d["disc_late"] > 0) & (d["disc_late"] < 200)]
    return d[["ts_code", "asof", "disc_late", "disc_resched"]]


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


def monthly_panel(ev: pd.DataFrame, ends: pd.DatetimeIndex) -> pd.DataFrame:
    """当月有披露事件的股：当月最近一次事件值。"""
    ev = ev.sort_values("asof")
    recs = []
    idx = ev["asof"].values
    for e in ends:
        lo = e - pd.Timedelta(days=31)
        m = (idx > np.datetime64(lo)) & (idx <= np.datetime64(e))
        if not m.any():
            continue
        latest = ev.iloc[np.flatnonzero(m)].groupby("ts_code").tail(1)
        s = latest[["ts_code", "disc_late", "disc_resched"]].copy()
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
    ev = load_events()
    rets = fwd20_returns()
    ends = pd.date_range(
        max(ev["asof"].min(), pd.Timestamp("2015-03-01")),
        ev["asof"].max(), freq="ME")
    sig = monthly_panel(ev, ends)

    out = {"n_rows": int(len(ev)), "n_stock_months": int(len(sig)),
           "asof_span": [str(ev["asof"].min().date()), str(ev["asof"].max().date())],
           "signals": {}}
    for col in ["disc_late", "disc_resched"]:
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
    sig.to_parquet(OUT_DIR / "sig_disc_monthly.parquet", index=False)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
