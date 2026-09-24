#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e99 东财研报原子明细全史 — 月频 IC 正式评估（预登记 E99）。

输入 data/em_reports/{prefix3}.parquet（144,718 研报 × 4,296 股, 2017→2026-09）。
信号族（全部 PIT by publishDate，月末 T 截面）：
  rev_breadth90  90d 内研报 EPS1 相对前 90d 一致均值的方向宽度 (#up-#down)/n
  rev_net90      同口径方向均值
  rating_chg90   90d 内评级上调-下调数
  dispersion     最新-每机构(120d) EPS1 std/|mean|
  eps_slope      最新-每机构 (EPS3-EPS1)/|EPS1| 均值
  n_orgs         120d 覆盖机构数
  cov_chg        n_orgs(T) - n_orgs(T-120d)
  tp_gap         最新-每机构目标价中位/close - 1
  rating_score   最新-每机构评级序数均值
  report_cnt30   30d 研报篇数

输出 experiments/lab/e99/eval.json + sig_monthly.parquet
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "data" / "em_reports"
BARS_DIR = ROOT / "data" / "daily_bars"
OUT_DIR = ROOT / "experiments" / "lab" / "e99"

RATING_ORD = {"买入": 2.0, "增持": 1.0, "持有": 0.5, "中性": 0.0,
              "减持": -1.0, "卖出": -2.0, "回避": -2.0}


def _ts_code(code: str) -> str:
    if code.startswith("6"):
        return f"sh.{code}"
    if code.startswith(("4", "8", "92")):
        return f"bj.{code}"
    return f"sz.{code}"


def load_reports() -> pd.DataFrame:
    df = pd.concat([pd.read_parquet(f) for f in sorted(SRC.glob("*.parquet"))],
                   ignore_index=True)
    df["publishDate"] = pd.to_datetime(df["publishDate"], errors="coerce")
    df = df.dropna(subset=["publishDate", "code"])
    df["ts_code"] = df["code"].astype(str).str.zfill(6).map(_ts_code)
    df["eps1"] = pd.to_numeric(df["predictThisYearEps"], errors="coerce")
    df["eps3"] = pd.to_numeric(df["predictNextTwoYearEps"], errors="coerce")
    df["aim"] = (pd.to_numeric(df["indvAimPriceL"], errors="coerce")
                 + pd.to_numeric(df["indvAimPriceT"], errors="coerce")) / 2
    df["ord_new"] = df["emRatingName"].map(RATING_ORD)
    df["ord_old"] = df["lastEmRatingName"].map(RATING_ORD)
    return df


def revision_dirs(df: pd.DataFrame) -> pd.Series:
    """每篇研报 EPS1 相对同股前 90d 一致均值的方向 (+1/-1/0)。"""
    dirs = pd.Series(0.0, index=df.index)
    for _, g in df.groupby("code", sort=False):
        g = g.sort_values("publishDate")
        dts = g["publishDate"].values.astype("datetime64[ns]")
        eps = g["eps1"].values
        j0 = 0
        run_sum, run_cnt = 0.0, 0
        w90 = np.timedelta64(90, "D")
        for i in range(len(g)):
            while dts[j0] <= dts[i] - w90:
                if not np.isnan(eps[j0]):
                    run_sum -= eps[j0]
                    run_cnt -= 1
                j0 += 1
            if not np.isnan(eps[i]):
                cons = run_sum / run_cnt if run_cnt else np.nan
                if not np.isnan(cons):
                    dirs[g.index[i]] = np.sign(eps[i] - cons)
                run_sum += eps[i]
                run_cnt += 1
    return dirs


def fwd_and_px() -> tuple[pd.DataFrame, pd.DataFrame]:
    px = []
    for f in sorted(glob.glob(str(BARS_DIR / "[sb][hzj].*/*.parquet"))):
        d = pd.read_parquet(f, columns=["code", "date", "close"])
        px.append(d)
    bars = pd.concat(px, ignore_index=True)
    bars["date"] = pd.to_datetime(bars["date"])
    bars["close"] = pd.to_numeric(bars["close"], errors="coerce")
    bars = bars.sort_values(["code", "date"])
    bars["fwd20"] = bars.groupby("code")["close"].shift(-20) / bars["close"] - 1
    return (bars[["code", "date", "fwd20"]].rename(columns={"code": "ts_code"}),
            bars[["code", "date", "close"]].rename(columns={"code": "ts_code"}))


def monthly_signals(df: pd.DataFrame, ends, px: pd.DataFrame) -> pd.DataFrame:
    recs = []
    df = df.sort_values("publishDate")
    idx_map = {c: i for i, c in enumerate(df.columns)}
    for e in ends:
        lo90, lo120 = e - pd.Timedelta(days=90), e - pd.Timedelta(days=120)
        w90 = df[(df["publishDate"] > lo90) & (df["publishDate"] <= e)]
        w120 = df[(df["publishDate"] > lo120) & (df["publishDate"] <= e)]
        if w90.empty and w120.empty:
            continue
        g90 = w90.groupby("ts_code")
        # 最新-每机构（120d 窗）
        last = (w120.sort_values("publishDate")
                .groupby(["ts_code", "orgSName"]).tail(1))
        gl = last.groupby("ts_code")
        s = pd.DataFrame({
            "rev_breadth90": g90.apply(
                lambda d: ((d["rev_dir"] > 0).sum() - (d["rev_dir"] < 0).sum())
                / max(d["rev_dir"].notna().sum(), 1), include_groups=False),
            "rev_net90": g90["rev_dir"].mean(),
            "rating_chg90": g90["rating_dir"].sum(),
            "report_cnt30": w90[w90["publishDate"] > e - pd.Timedelta(days=30)]
                .groupby("ts_code").size(),
            "dispersion": gl["eps1"].apply(
                lambda x: x.std() / abs(x.mean()) if len(x) >= 2 and abs(x.mean()) > 0 else np.nan),
            "eps_slope": gl.apply(
                lambda d: ((d["eps3"] - d["eps1"]) / d["eps1"].abs()).mean(),
                include_groups=False),
            "n_orgs": w120.groupby("ts_code")["orgSName"].nunique(),
            "tp_med": gl["aim"].median(),
            "rating_score": gl["ord_new"].mean(),
        })
        # cov_chg: n_orgs(T-120d)
        e2 = e - pd.Timedelta(days=120)
        w120b = df[(df["publishDate"] > e2 - pd.Timedelta(days=120))
                   & (df["publishDate"] <= e2)]
        norgs_prev = w120b.groupby("ts_code")["orgSName"].nunique()
        s["cov_chg"] = s["n_orgs"] - norgs_prev.reindex(s.index).fillna(0)
        s["date"] = e
        recs.append(s.reset_index())
    sig = pd.concat(recs, ignore_index=True)
    # tp_gap: tp_med / close(月末交易日) - 1
    sig = sig.merge(px, on=["ts_code", "date"], how="left")
    sig["tp_gap"] = sig["tp_med"] / sig["close"] - 1
    return sig.drop(columns=["tp_med", "close"])


def eval_ic(sig: pd.DataFrame, rets: pd.DataFrame, col: str,
            shift_days: int = 0) -> dict:
    d = sig[["ts_code", "date", col]].copy()
    if shift_days:
        d["date"] = d["date"] + pd.Timedelta(days=shift_days)
    d = d.merge(rets, on=["ts_code", "date"], how="inner").dropna(
        subset=[col, "fwd20"])
    ic = d.groupby("date").apply(
        lambda g: g[col].corr(g["fwd20"], method="spearman")
        if len(g) >= 20 else np.nan, include_groups=False)
    ic = ic.dropna()
    if len(ic) < 6:
        return {"ic": None, "t": None, "months": len(ic)}
    t = ic.mean() / (ic.std(ddof=1) / np.sqrt(len(ic))) if ic.std(ddof=1) > 0 else 0.0
    return {"ic": round(float(ic.mean()), 4), "t": round(float(t), 2),
            "months": int(len(ic))}


def main() -> int:
    ap = argparse.ArgumentParser()
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    df = load_reports()
    print(f"reports={len(df)} stocks={df.ts_code.nunique()}", flush=True)
    df["rev_dir"] = revision_dirs(df)
    df["rating_dir"] = np.sign(df["ord_new"] - df["ord_old"])
    df.loc[df["ord_new"].isna() | df["ord_old"].isna(), "rating_dir"] = np.nan

    rets, px = fwd_and_px()
    ends = pd.date_range("2017-03-31", df["publishDate"].max(), freq="ME")
    # 月末对齐到实际交易日
    td = np.sort(px["date"].unique())
    ends = pd.DatetimeIndex([td[td <= e].max() for e in ends if (td <= e).any()])
    sig = monthly_signals(df, ends, px)
    sig.to_parquet(OUT_DIR / "sig_monthly.parquet")
    print(f"sig rows={len(sig)} months={sig.date.nunique()}", flush=True)

    cols = ["rev_breadth90", "rev_net90", "rating_chg90", "dispersion",
            "eps_slope", "n_orgs", "cov_chg", "tp_gap", "rating_score",
            "report_cnt30"]
    event_cols = {"rev_breadth90", "rev_net90", "rating_chg90"}
    out = {"n_reports": int(len(df)), "n_stock_months": int(len(sig)),
           "date_span": [str(df["publishDate"].min().date()),
                         str(df["publishDate"].max().date())],
           "signals": {}}
    for col in cols:
        res = eval_ic(sig, rets, col)
        if col in event_cols:
            pl = [eval_ic(sig, rets, col, s)["ic"] for s in (-20, 20)]
            pl = [p for p in pl if p is not None]
            res["placebo_ics"] = pl
            res["placebo_p"] = (
                round(float(np.mean([abs(p) >= abs(res["ic"]) for p in pl])), 3)
                if pl and res["ic"] is not None else None)
        out["signals"][col] = res
        print(col, res, flush=True)

    have_px = rets.groupby("date")["ts_code"].nunique()
    have_sig = sig.groupby("date")["ts_code"].nunique()
    cov = (have_sig / have_px.reindex(have_sig.index)).dropna()
    out["coverage"] = {"mean": round(float(cov.mean()), 3) if len(cov) else None}
    (OUT_DIR / "eval.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out["coverage"], ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
