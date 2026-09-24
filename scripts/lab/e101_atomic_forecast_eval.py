#!/usr/bin/env python3
"""e101 原子分析师预测（CSMAR 明细 2001-2024, 214.8万行）信号族评估。

数据：data/analyst_atomic/forecasts.parquet
      (code, rpt_date, decl_date, fend, reportid, analyst, brokern, feps)
用途：e99 轴在权威原子明细上的复测——上修/下修宽度、离散度、覆盖度、
      一致预期变化。若同样判弱则分析师预测轴四证收束。

信号（逐股月末截面，滚动 60 自然日窗）：
  rev_breadth  = (同分析师同预测期内新值−旧值>0 家数 − <0 家数) / 修评家数
  disp_cv      = 窗内最新 Feps 分析师间 std/|mean|（近端预测期=Fenddt 年末）
  cover_n      = 窗内发布新预测的机构数
  cons_chg     = 当期一致预期均值(Feps, 最近预测年) / 60d 前均值 − 1
评估：fwd20d 月频 Spearman IC + t + 安慰剂 ±20d；门禁同 e62/e99。
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
FC = ROOT / "data" / "analyst_atomic" / "forecasts.parquet"
BARS_DIR = ROOT / "data" / "daily_bars"
OUT = ROOT / "experiments" / "lab" / "e101" / "eval.json"
SIG_DIR = ROOT / "experiments" / "lab" / "e101"


def _ts(code: str) -> str:
    p = code[0]
    if p in "03":
        return f"sz.{code}"
    if p == "6":
        return f"sh.{code}"
    return f"bj.{code}"


def load_forecasts() -> pd.DataFrame:
    df = pd.read_parquet(FC)
    df = df[df.rpt_date.dt.year.between(2001, 2024)]
    # 近端预测期：Fenddt 年份 = 报告日年份或次年（剔除远期预测噪声）
    fy = df.fend.dt.year
    df = df[(fy == df.rpt_date.dt.year) | (fy == df.rpt_date.dt.year + 1)]
    df["ts_code"] = df["code"].map(_ts)
    return df


def window_frame(fc: pd.DataFrame, e: pd.Timestamp, win: int = 60) -> pd.DataFrame:
    lo = e - pd.Timedelta(days=win)
    return fc[(fc.rpt_date > lo) & (fc.rpt_date <= e)]


def monthly_signals(fc: pd.DataFrame, ends: pd.DatetimeIndex, win: int = 60) -> pd.DataFrame:
    recs = []
    fc = fc.sort_values("rpt_date")
    dates = fc["rpt_date"].values
    for e in ends:
        lo = e - pd.Timedelta(days=win)
        m = (dates > np.datetime64(lo)) & (dates <= np.datetime64(e))
        if not m.any():
            continue
        w = fc.iloc[np.flatnonzero(m)]
        # 同股同期同年分析师只留最新一条
        w = (w.sort_values("rpt_date")
               .drop_duplicates(subset=["code", "analyst", "fend"], keep="last"))
        g = w.groupby("ts_code")
        rows = []
        for code, d in g:
            mu = d["feps"].mean()
            rows.append({
                "ts_code": code,
                "disp_cv": d["feps"].std(ddof=1) / abs(mu) if abs(mu) > 1e-9 and len(d) >= 3 else np.nan,
                "cover_n": d["brokern"].nunique(),
                "cons_mean": mu,
            })
        s = pd.DataFrame(rows).set_index("ts_code")
        s["date"] = e
        recs.append(s.reset_index())
    sig = pd.concat(recs, ignore_index=True) if recs else pd.DataFrame()
    # cons_chg：一致预期均值环比上月
    sig = sig.sort_values(["ts_code", "date"])
    sig["cons_chg"] = sig.groupby("ts_code")["cons_mean"].pct_change()
    # rev_breadth：对每条预测取同股同分析师同预测期的前一条为基准
    f2 = fc.sort_values(["code", "analyst", "fend", "rpt_date"]).copy()
    f2["prev"] = f2.groupby(["code", "analyst", "fend"])["feps"].shift(1)
    rev = f2.dropna(subset=["prev"])
    rev["dir"] = np.sign(rev["feps"] - rev["prev"])
    rev = rev[rev["dir"] != 0]
    rr = []
    for e in ends:
        lo = e - pd.Timedelta(days=win)
        m = (rev["rpt_date"] > lo) & (rev["rpt_date"] <= e)
        if m.any():
            rr.append(rev[m].groupby("ts_code")["dir"].mean().rename(e))
    if rr:
        rbf = pd.concat(rr, axis=1).T
        rbf.index.name = "date"
        rbf.columns.name = "ts_code"
        sig = sig.merge(rbf.reset_index().melt(id_vars="date", var_name="ts_code",
                                               value_name="rev_breadth"),
                        on=["date", "ts_code"], how="left")
    else:
        sig["rev_breadth"] = np.nan
    return sig.drop(columns=["cons_mean"])


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
    fc = load_forecasts()
    rets = fwd20_returns()
    ends = pd.date_range(
        max(fc["rpt_date"].min(), pd.Timestamp("2001-06-01")),
        fc["rpt_date"].max(), freq="ME")
    sig = monthly_signals(fc, ends)

    out = {"n_forecasts": int(len(fc)), "n_stock_months": int(len(sig)),
           "signals": {}}
    for col in ["rev_breadth", "disp_cv", "cover_n", "cons_chg"]:
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
