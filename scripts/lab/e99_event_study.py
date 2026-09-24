#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e99 补查：研报修评事件的事件日精度（事件研究，非月频）。

E99 月频门判弱后，验证 e49 式事件口径是否在本数据集上复现：
按 rev_dir（上修/下修/无修正）分组，算 publishDate 后 fwd5/fwd20
超额收益（个股收益 − 同日截面中位）。若下修组显著负超额，
说明原子通道的事件日信息存在，只是月频聚合稀释。

输出 experiments/lab/e99/event_study.json
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "data" / "em_reports"
BARS_DIR = ROOT / "data" / "daily_bars"
OUT = ROOT / "experiments" / "lab" / "e99" / "event_study.json"


def load_reports() -> pd.DataFrame:
    df = pd.concat([pd.read_parquet(f) for f in sorted(SRC.glob("*.parquet"))],
                   ignore_index=True)
    df["publishDate"] = pd.to_datetime(df["publishDate"], errors="coerce")
    df = df.dropna(subset=["publishDate", "code"])
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["eps1"] = pd.to_numeric(df["predictThisYearEps"], errors="coerce")
    return df


def revision_dirs(df: pd.DataFrame) -> pd.Series:
    dirs = pd.Series(0.0, index=df.index)
    for _, g in df.groupby("code", sort=False):
        g = g.sort_values("publishDate")
        dts = g["publishDate"].values.astype("datetime64[ns]")
        eps = g["eps1"].values
        j0, run_sum, run_cnt = 0, 0.0, 0
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


def load_bars() -> pd.DataFrame:
    px = []
    for f in sorted(glob.glob(str(BARS_DIR / "[sb][hzj].*/*.parquet"))):
        px.append(pd.read_parquet(f, columns=["code", "date", "close"]))
    b = pd.concat(px, ignore_index=True)
    b["date"] = pd.to_datetime(b["date"])
    b["close"] = pd.to_numeric(b["close"], errors="coerce")
    b["code6"] = b["code"].astype(str).str[-6:]
    b = b.sort_values(["code6", "date"])
    for h in (5, 20):
        b[f"fwd{h}"] = b.groupby("code6")["close"].shift(-h) / b["close"] - 1
    return b


def main() -> int:
    df = load_reports()
    df["rev_dir"] = revision_dirs(df)
    bars = load_bars()
    med5 = bars.groupby("date")["fwd5"].median().rename("med5")
    med20 = bars.groupby("date")["fwd20"].median().rename("med20")
    ev = df.merge(bars[["code6", "date", "fwd5", "fwd20"]],
                  left_on=["code", "publishDate"],
                  right_on=["code6", "date"], how="left")
    ev = ev.merge(med5, on="date").merge(med20, on="date")
    ev["abn5"] = ev["fwd5"] - ev["med5"]
    ev["abn20"] = ev["fwd20"] - ev["med20"]
    ev = ev.dropna(subset=["abn20"])

    out = {"n_events": int(len(ev)), "groups": {}}
    for name, sub in [("up", ev[ev.rev_dir > 0]), ("flat", ev[ev.rev_dir == 0]),
                      ("down", ev[ev.rev_dir < 0]),
                      ("new_cov", ev[ev.rev_dir.isna()])]:
        if len(sub) < 30:
            continue
        out["groups"][name] = {
            "n": int(len(sub)),
            "abn5_mean": round(float(sub.abn5.mean()), 5),
            "abn5_t": round(float(sub.abn5.mean() / (sub.abn5.std() / np.sqrt(len(sub)))), 2),
            "abn20_mean": round(float(sub.abn20.mean()), 5),
            "abn20_t": round(float(sub.abn20.mean() / (sub.abn20.std() / np.sqrt(len(sub)))), 2),
            "hit_pos": round(float((sub.abn20 > 0).mean()), 3),
        }
    # 下修 vs 上修差
    up, dn = ev[ev.rev_dir > 0].abn20, ev[ev.rev_dir < 0].abn20
    if len(up) and len(dn):
        diff = up.mean() - dn.mean()
        se = np.sqrt(up.var() / len(up) + dn.var() / len(dn))
        out["up_minus_down"] = {"diff": round(float(diff), 5),
                                "t": round(float(diff / se), 2)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
