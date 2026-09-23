#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e86 原子预测明细 — 滚动窗内截面信号首验（日频 IC）。

PREDICTDETAIL = 每机构最新预测快照（每(股,机构)对窗口内一行）。
按 as-of 重建：对评估日 T 取 PUBLISH_DATE≤T 的每机构最新行，
构造截面信号:
  dispersion : 各机构 EPS2 std/mean（分歧度，假设负向）
  eps_slope  : 各机构 (EPS3-EPS2)/|EPS2| 均值（期限结构，假设正向）
  n_orgs     : 覆盖机构数（关注度，假设正向）
  pct_buy    : 评级含"买"占比（假设正向）
判据（窗口 ~6M）：日 IC 均值 |IC|≥0.02 且 |t|≥2 = 信号候选。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
ATOMIC = ROOT / "data" / "analyst_atomic_em"
BARS = ROOT / "data" / "daily_bars"
OUT = ROOT / "experiments" / "lab" / "e86"


def _ts(secucode: str) -> str:
    code, mkt = secucode.split(".")
    p = {"SH": "sh", "SS": "sh", "XSHG": "sh",
         "SZ": "sz", "SZSE": "sz", "BJ": "bj"}.get(mkt, mkt.lower()[:2])
    return f"{p}.{code}"


def load_atomic() -> pd.DataFrame:
    df = pd.read_parquet(sorted(ATOMIC.glob("*.parquet"))[-1])
    df["PUBLISH_DATE"] = pd.to_datetime(df["PUBLISH_DATE"])
    df["ts_code"] = df["SECUCODE"].map(_ts)
    return df


def load_fwd(syms, start, end, horizon=20):
    out = []
    for sym in syms:
        p = BARS / sym
        if not p.is_dir():
            continue
        fs = sorted(p.glob("*.parquet"))
        if not fs:
            continue
        b = pd.concat([pd.read_parquet(f) for f in fs],
                      ignore_index=True)
        b["date"] = pd.to_datetime(b["date"])
        b = b[(b["date"] >= start) & (b["date"] <= end)].sort_values("date")
        if b.empty:
            continue
        b["close"] = pd.to_numeric(b["close"], errors="coerce")
        b["fwd"] = b["close"].shift(-horizon) / b["close"] - 1
        out.append(b[["date", "fwd"]].assign(ts_code=sym))
    if not out:
        return pd.DataFrame(columns=["date", "fwd", "ts_code"])
    return pd.concat(out, ignore_index=True)


def asof_signals(at: pd.DataFrame, T: pd.Timestamp) -> pd.DataFrame:
    w = at[at["PUBLISH_DATE"] <= T]
    if w.empty:
        return w.iloc[0:0]
    last = (w.sort_values("PUBLISH_DATE")
              .groupby(["ts_code", "ORG_CODE"], as_index=False).tail(1))
    def _f(g):
        e2 = g["EPS2"].astype(float)
        e3 = g["EPS3"].astype(float)
        mu = e2.mean()
        rat = g["RATING"].astype(str)
        return pd.Series({
            "dispersion": float(e2.std() / mu) if mu and len(e2) > 2
            else np.nan,
            "eps_slope": float(((e3 - e2) / e2.abs()).mean()) if mu
            else np.nan,
            "n_orgs": int(len(g)),
            "pct_buy": float(rat.str.contains("买").mean()),
        })
    sig = last.groupby("ts_code").apply(_f).reset_index()
    sig["date"] = T
    return sig


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    at = load_atomic()
    start, end = at.PUBLISH_DATE.min(), at.PUBLISH_DATE.max()
    fwd = load_fwd(sorted(at.ts_code.unique()), start,
                   end + pd.Timedelta(days=30))
    eval_dates = pd.bdate_range(start + pd.Timedelta(days=20), end)
    sig = pd.concat([asof_signals(at, T) for T in eval_dates],
                    ignore_index=True)
    m = sig.merge(fwd, on=["ts_code", "date"], how="inner").dropna()
    res = {"n_obs": int(len(m)), "n_days": int(m.date.nunique())}
    for col in ("dispersion", "eps_slope", "n_orgs", "pct_buy"):
        ics = (m.dropna(subset=[col]).groupby("date")
                .apply(lambda g: g[col].corr(g["fwd"], method="spearman")
                       if len(g) > 20 else np.nan).dropna())
        res[col] = {"ic": float(ics.mean()) if len(ics) else None,
                    "t": float(ics.mean() / (ics.std() /
                        np.sqrt(len(ics)))) if len(ics) > 2 else None,
                    "n": int(len(ics))}
    print(json.dumps(res, indent=1))
    (OUT / "firstlook.json").write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
