#!/usr/bin/env python3
"""e103 持仓结构轴信号评估——股东户数 + 基金重仓（cninfo 季度表全史）。

数据: data/cninfo_holders/*.parquet    (股东人数: F001N=户数, F003N=环比%, ENDDATE=统计期)
      data/cninfo_fund_heavy/*.parquet  (基金重仓: F001N=重仓基金数, F002N=持股数, F003N=市值千元, ENDDATE)
PIT: 表内只有统计期 ENDDATE，无披露日——按 ENDDATE+60d 作保守可用日（季报披露通常 ≤45d）。
信号(逐股月末截面, asof 对齐):
  gdhs_lvl   = 股东户数最新值（retail crowding 水平）
  gdhs_chg   = 户数环比%（官方字段）
  hld_val    = 户均持股市值（F005N，集中度代理）
  fund_n     = 重仓基金数（机构覆盖水平）
  fund_val   = 重仓市值（F003N 千元）
评估: fwd20d 月频 Spearman IC + t + 安慰剂 ±20d。判强: |IC|≥0.04 且 |t|≥3 且安慰剂 p<0.05。
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SIG_DIR = ROOT / "experiments" / "lab" / "e103"
OUT = SIG_DIR / "eval.json"
LAG = pd.Timedelta(days=60)


def _ts(code: str) -> str:
    p = code[0]
    if p in "03":
        return f"sz.{code}"
    if p == "6":
        return f"sh.{code}"
    return f"bj.{code}"


def _load(table: str, cols: dict[str, str]) -> pd.DataFrame:
    fs = sorted(glob.glob(str(ROOT / f"data/{table}/*.parquet")))
    df = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    df = df[df["ENDDATE"].notna() & df["SECCODE"].notna()].copy()
    df["asof"] = pd.to_datetime(df["ENDDATE"], errors="coerce") + LAG
    df = df.dropna(subset=["asof"])
    df["ts_code"] = df["SECCODE"].astype(str).str.zfill(6).map(_ts)
    for src, dst in cols.items():
        df[dst] = pd.to_numeric(df[src], errors="coerce")
    return df[["ts_code", "asof"] + list(cols.values())].dropna()


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


def asof_monthly(df: pd.DataFrame, ends: pd.DatetimeIndex) -> pd.DataFrame:
    """Latest record per stock as of each month end (point-in-time as-of join)."""
    recs = []
    for e in ends:
        w = df[df["asof"] <= e]
        if w.empty:
            continue
        g = w.sort_values("asof").groupby("ts_code").tail(1).copy()
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
    holders = _load("cninfo_holders",
                    {"F001N": "gdhs_lvl", "F003N": "gdhs_chg", "F005N": "hld_val"})
    funds = _load("cninfo_fund_heavy",
                  {"F001N": "fund_n", "F003N": "fund_val"})
    print(f"holders={len(holders)} funds={len(funds)}", flush=True)
    rets = fwd20_returns()
    ends = pd.date_range("2015-01-01", "2026-06-30", freq="ME")
    sig_h = asof_monthly(holders, ends)
    sig_f = asof_monthly(funds, ends)
    out = {"lag_days": 60, "signals": {}}
    for name, sig, cols in [("holders", sig_h, ["gdhs_lvl", "gdhs_chg", "hld_val"]),
                            ("fund_heavy", sig_f, ["fund_n", "fund_val"])]:
        out[f"{name}_months"] = int(sig["date"].nunique()) if not sig.empty else 0
        for col in cols:
            res = eval_ic(sig, rets, col)
            pl = [eval_ic(sig, rets, col, s)["ic"] for s in (-20, 20)]
            pl = [p for p in pl if p is not None]
            res["placebo_ics"] = pl
            res["placebo_p"] = (
                round(float(np.mean([abs(p) >= abs(res["ic"]) for p in pl])), 3)
                if pl and res["ic"] is not None else None)
            out["signals"][col] = res
    SIG_DIR.mkdir(parents=True, exist_ok=True)
    sig_h.to_parquet(SIG_DIR / "sig_holders_monthly.parquet", index=False)
    sig_f.to_parquet(SIG_DIR / "sig_fund_monthly.parquet", index=False)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
