#!/usr/bin/env python3
"""e102 研报覆盖热度(关注度轴)信号评估——cninfo load/p_info3097_inc 摘要全史。

数据: data/cninfo_report_abstracts/*.parquet
      (SECCODE, SECNAME, F001D=发布时刻, F003V=摘要全文, F004V=机构, F007V=报告类型)
信号(逐股月末截面, 滚动90自然日窗):
  cover_n    = 窗内研报篇数
  cover_d    = 窗内篇数 / 前90日窗内篇数 - 1 (热度环比)
  depth_sh   = 窗内深度报告(F007V含'深度')占比
  org_n      = 窗内覆盖机构数
评估: fwd20d 月频 Spearman IC + t + 安慰剂 ±20d。判强: IC≥0.04 且 |t|≥3 且安慰剂 p<0.05。
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SIG_DIR = ROOT / "experiments" / "lab" / "e102"
OUT = SIG_DIR / "eval.json"


def _ts(code: str) -> str:
    p = code[0]
    if p in "03":
        return f"sz.{code}"
    if p == "6":
        return f"sh.{code}"
    return f"bj.{code}"


def load_reports() -> pd.DataFrame:
    fs = sorted(glob.glob(str(ROOT / "data/cninfo_report_abstracts/*.parquet")))
    df = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    df["date"] = pd.to_datetime(df["F001D"], errors="coerce")
    df = df.dropna(subset=["date", "SECCODE"])
    df["ts_code"] = df["SECCODE"].astype(str).str.zfill(6).map(_ts)
    df["is_depth"] = df["F007V"].astype(str).str.contains("深度")
    df["org"] = df["F004V"].astype(str).str.strip()
    return df[["ts_code", "date", "is_depth", "org"]]


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


def monthly_signals(rp: pd.DataFrame, ends: pd.DatetimeIndex, win: int = 90) -> pd.DataFrame:
    rp = rp.sort_values("date")
    dates = rp["date"].values
    recs = []
    for e in ends:
        lo = e - pd.Timedelta(days=win)
        m = (dates > np.datetime64(lo)) & (dates <= np.datetime64(e))
        lo2 = lo - pd.Timedelta(days=win)
        m2 = (dates > np.datetime64(lo2)) & (dates <= np.datetime64(lo))
        if not m.any():
            continue
        w = rp.iloc[np.flatnonzero(m)]
        g = w.groupby("ts_code").agg(
            cover_n=("date", "size"),
            depth_sh=("is_depth", "mean"),
            org_n=("org", "nunique"))
        g["date"] = e
        if m2.any():
            w2 = rp.iloc[np.flatnonzero(m2)]
            prev = w2.groupby("ts_code").size().rename("prev_n")
            g = g.join(prev)
            g["cover_d"] = g["cover_n"] / g["prev_n"].clip(lower=1) - 1
        else:
            g["cover_d"] = np.nan
        recs.append(g.reset_index())
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
    rp = load_reports()
    print(f"reports={len(rp)} stocks={rp.ts_code.nunique()} "
          f"{rp.date.min().date()}..{rp.date.max().date()}", flush=True)
    rets = fwd20_returns()
    ends = pd.date_range("2017-04-01", rp["date"].max(), freq="ME")
    sig = monthly_signals(rp, ends)
    out = {"n_reports": int(len(rp)), "n_stock_months": int(len(sig)), "signals": {}}
    for col in ["cover_n", "cover_d", "depth_sh", "org_n"]:
        res = eval_ic(sig, rets, col)
        pl = [eval_ic(sig, rets, col, s)["ic"] for s in (-20, 20)]
        pl = [p for p in pl if p is not None]
        res["placebo_ics"] = pl
        res["placebo_p"] = (
            round(float(np.mean([abs(p) >= abs(res["ic"]) for p in pl])), 3)
            if pl and res["ic"] is not None else None)
        out["signals"][col] = res
    SIG_DIR.mkdir(parents=True, exist_ok=True)
    sig.to_parquet(SIG_DIR / "sig_monthly.parquet", index=False)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
