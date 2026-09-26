#!/usr/bin/env python3
"""e109 内部人交易流量轴信号评估——cninfo 董监高持股变动全史。

数据: data/cninfo_insider_all/*.parquet
      (SECCODE, DECLAREDATE=公告日, F006N=带符号变动股数,
       F010V=交易方式, F002V=职务)
PIT: asof = DECLAREDATE + 1 天（公告披露后次日可用，保守错后）。
过滤: F010V ∈ {竞价交易, 二级市场买卖, 大宗交易}（真实自主交易）。
信号(逐股月末截面, 近90日滚动聚合):
  ins_net3m  = ΣF006N 净变动股数, log1p(|x|)*sign(x)
  ins_buyn90 = F006N>0 增持笔数
  ins_dir3m  = (增−减)/(增+减+1) 方向平衡
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
SIG_DIR = ROOT / "experiments" / "lab" / "e109"
OUT = SIG_DIR / "eval.json"
LAG = pd.Timedelta(days=1)
WIN = pd.Timedelta(days=90)
MKT_METHODS = {"竞价交易", "二级市场买卖", "大宗交易"}


def _ts(code: str) -> str:
    p = code[0]
    if p in "03":
        return f"sz.{code}"
    if p == "6":
        return f"sh.{code}"
    return f"bj.{code}"


def load_insider() -> pd.DataFrame:
    fs = sorted(glob.glob(str(ROOT / "data/cninfo_insider_all/*.parquet")))
    df = pd.concat([pd.read_parquet(f, columns=["SECCODE", "DECLAREDATE",
                                              "F006N", "F010V"])
                    for f in fs], ignore_index=True)
    df = df[df["DECLAREDATE"].notna() & df["SECCODE"].notna()].copy()
    df = df[df["F010V"].isin(MKT_METHODS)]
    df["asof"] = pd.to_datetime(df["DECLAREDATE"], errors="coerce") + LAG
    df = df.dropna(subset=["asof"])
    df["ts_code"] = df["SECCODE"].astype(str).str.zfill(6).map(_ts)
    df["chg"] = pd.to_numeric(df["F006N"], errors="coerce").fillna(0)
    df["buy"] = (df["chg"] > 0).astype(int)
    df["sell"] = (df["chg"] < 0).astype(int)
    return df[["ts_code", "asof", "chg", "buy", "sell"]]


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
    recs = []
    df = df.sort_values("asof")
    for e in ends:
        lo = e - WIN
        w = df[(df["asof"] > lo) & (df["asof"] <= e)]
        if w.empty:
            continue
        g = w.groupby("ts_code").agg(
            net=("chg", "sum"), buy_n=("buy", "sum"), sell_n=("sell", "sum"))
        g["ins_net3m"] = np.log1p(g["net"].abs()) * np.sign(g["net"])
        g["ins_buyn90"] = g["buy_n"]
        g["ins_dir3m"] = (g["buy_n"] - g["sell_n"]) / (g["buy_n"] + g["sell_n"] + 1)
        g = g.drop(columns=["net", "buy_n", "sell_n"]).reset_index()
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
    ins = load_insider()
    print(f"insider rows={len(ins)} stocks={ins['ts_code'].nunique()}", flush=True)
    rets = fwd20_returns()
    ends = pd.date_range("2016-01-31", "2024-12-31", freq="ME")
    sig = flow_monthly(ins, ends)
    out = {"lag": "DECLAREDATE+1d", "win_days": 90,
           "eval_window": "2016-01..2024-12",
           "filter": "F010V in market methods", "signals": {}}
    out["months"] = int(sig["date"].nunique()) if not sig.empty else 0
    out["covered_stocks"] = int(sig["ts_code"].nunique()) if not sig.empty else 0
    for col in ["ins_net3m", "ins_buyn90", "ins_dir3m"]:
        res = eval_ic(sig, rets, col)
        plb = [eval_ic(sig, rets, col, s)["ic"] for s in (-20, 20)]
        plb = [p for p in plb if p is not None]
        res["placebo_ics"] = plb
        res["placebo_p"] = (
            round(float(np.mean([abs(p) >= abs(res["ic"]) for p in plb])), 3)
            if plb and res["ic"] is not None else None)
        out["signals"][col] = res
    SIG_DIR.mkdir(parents=True, exist_ok=True)
    sig.to_parquet(SIG_DIR / "sig_insider_monthly.parquet", index=False)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
