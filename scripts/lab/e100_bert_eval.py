#!/usr/bin/env python3
"""e100 FinBERT 公告情绪因子族首验（docs/E100_BERT_NOTICE_PREREG.md）。

输入：data/notice_body/bert_scores/notice_bert_scores.parquet
      （art_code, code, ann_date, bert_neu, bert_pos, bert_neg）
输出：experiments/lab/e100/eval.json —— 月频 IC/t/安慰剂/覆盖度。

信号（prereg §一）：bert_pos_den/bert_neg_den/bert_posneg/bert_conf/
bert_pos_cnt；逐股近 60 自然日窗口、月末截面；fwd20d 收益。
门禁同 e62：判强 IC>=0.04 & t>=3；安慰剂 ±20d p<0.05。
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SCORES = ROOT / "data" / "notice_body" / "bert_scores" / "notice_bert_scores.parquet"
BARS_DIR = ROOT / "data" / "daily_bars"
OUT = ROOT / "experiments" / "lab" / "e100" / "eval.json"
SIG_DIR = ROOT / "experiments" / "lab" / "e100"


def _ts(code: str) -> str:
    code = str(code).zfill(6)
    p = code[0]
    if p in "03":
        return f"sz.{code}"
    if p == "6":
        return f"sh.{code}"
    return f"bj.{code}"


def load_scores() -> pd.DataFrame:
    df = pd.read_parquet(SCORES)
    df["ann_date"] = pd.to_datetime(df["ann_date"])
    df["ts_code"] = df["code"].astype(str).str.zfill(6).map(_ts)
    return df


def monthly_signals(sc: pd.DataFrame, ends: pd.DatetimeIndex, win: int = 60) -> pd.DataFrame:
    recs = []
    sc = sc.sort_values("ann_date")
    dates = sc["ann_date"].values
    for e in ends:
        lo = e - pd.Timedelta(days=win)
        m = (dates >= np.datetime64(lo)) & (dates <= np.datetime64(e))
        if not m.any():
            continue
        g = sc.iloc[np.flatnonzero(m)].groupby("ts_code")
        s = pd.DataFrame({
            "bert_pos_den": g["bert_pos"].mean(),
            "bert_neg_den": g["bert_neg"].mean(),
            "bert_posneg": g.apply(lambda d: (d["bert_pos"] - d["bert_neg"]).mean(), include_groups=False),
            "bert_conf": g.apply(lambda d: (d["bert_neu"] < 0.5).mean(), include_groups=False),
            "bert_pos_cnt": g.apply(lambda d: float((d["bert_pos"] > 0.7).sum()), include_groups=False),
        })
        s["date"] = e
        recs.append(s.reset_index())
    return pd.concat(recs, ignore_index=True) if recs else pd.DataFrame()


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
    sc = load_scores()
    rets = fwd20_returns()
    ends = pd.date_range(
        max(sc["ann_date"].min(), pd.Timestamp("2015-03-01")),
        sc["ann_date"].max(), freq="ME")
    sig = monthly_signals(sc, ends)

    out = {"n_scored": int(len(sc)), "n_stock_months": int(len(sig)),
           "date_span": [str(sc["ann_date"].min().date()), str(sc["ann_date"].max().date())],
           "signals": {}}
    for col in ["bert_pos_den", "bert_neg_den", "bert_posneg", "bert_conf", "bert_pos_cnt"]:
        res = eval_ic(sig, rets, col)
        pl = [eval_ic(sig, rets, col, s)["ic"] for s in (-20, 20)]
        pl = [p for p in pl if p is not None]
        res["placebo_ics"] = pl
        res["placebo_p"] = (
            round(float(np.mean([abs(p) >= abs(res["ic"]) for p in pl])), 3)
            if pl and res["ic"] is not None else None)
        out["signals"][col] = res

    have_px = rets.groupby("date")["ts_code"].nunique()
    have_sig = sig.groupby("date")["ts_code"].nunique()
    cov = (have_sig / have_px.reindex(have_sig.index)).dropna()
    out["coverage"] = {"mean": round(float(cov.mean()), 3) if len(cov) else None,
                       "months": int(len(cov))}

    SIG_DIR.mkdir(parents=True, exist_ok=True)
    sig.to_parquet(SIG_DIR / "sig_monthly.parquet", index=False)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
