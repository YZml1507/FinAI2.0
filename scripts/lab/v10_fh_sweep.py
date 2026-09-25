#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v10: 基金重仓拥挤度特征上板 — V9_FEATS + fh_* 三臂扫描。

fh 特征（cninfo fund_heavy, asof=ENDDATE+45d, 每股每 sig_date 取 ≤T 最新季度值）:
  fh_cnt    = log1p(F001N 持基家数)
  fh_cnt_chg= F001N 环比差分

运行: .venv/bin/python scripts/lab/v10_fh_sweep.py
"""
import glob
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "lab"))

import e83_feature_sweep as S  # noqa: E402
from e63_score_sweep_local import V9_FEATS, V9_PARAMS  # noqa: E402

OUT = ROOT / "experiments" / "lab" / "v10"
FH_PARQ = OUT / "fh_features.parquet"


def _code_to_ts(c) -> str:
    s = str(c).zfill(6)
    p = s[0]
    if p in "03":
        return f"{s}.SZ"
    if p == "6":
        return f"{s}.SH"
    return f"{s}.BJ"


def build_fh_features(sig_dates: np.ndarray) -> pd.DataFrame:
    fs = sorted(glob.glob(str(ROOT / "data" / "cninfo_fund_heavy" / "*.parquet")))
    d = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    d = d.dropna(subset=["SECCODE", "ENDDATE", "F001N"])
    d["asof"] = pd.to_datetime(d["ENDDATE"]) + pd.Timedelta(days=45)
    d["ts_code"] = d["SECCODE"].map(_code_to_ts)
    d = d.sort_values("asof")
    d["fh_cnt"] = np.log1p(pd.to_numeric(d["F001N"], errors="coerce").clip(lower=0))
    g = d.groupby("ts_code")
    d["fh_cnt_chg"] = g["F001N"].diff()
    d = d[["ts_code", "asof", "fh_cnt", "fh_cnt_chg"]].dropna(subset=["fh_cnt"])
    recs = {c: (v["asof"].values, v["fh_cnt"].values, v["fh_cnt_chg"].values)
            for c, v in d.groupby("ts_code")}
    out = []
    for sd in pd.DatetimeIndex(sig_dates):
        t = np.datetime64(sd)
        for code, (dates, cnt, chg) in recs.items():
            i = int(np.searchsorted(dates, t, "right")) - 1
            if i >= 0:
                out.append((sd, code, float(cnt[i]), float(chg[i])))
    return pd.DataFrame(out, columns=["sig_date", "ts_code", "fh_cnt", "fh_cnt_chg"])


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    xt = pd.read_parquet(S.X_TRAIN)
    xs = pd.read_parquet(S.X_SCORE)
    x_all = pd.concat([xt.assign(src="train"), xs.assign(src="score")], ignore_index=True)

    if FH_PARQ.exists():
        fh = pd.read_parquet(FH_PARQ)
    else:
        fh = build_fh_features(np.array(sorted(x_all["sig_date"].unique())))
        fh.to_parquet(FH_PARQ, index=False)
    print(f"fh rows={len(fh)} ({time.time()-t0:.0f}s)")

    x = x_all.merge(fh, on=["sig_date", "ts_code"], how="left")
    x = S._prep(x, list(dict.fromkeys(V9_FEATS + ["fh_cnt", "fh_cnt_chg"])))

    ARMS = {
        # v9_base 臂已存 e87_p9_drop5n2000 (同31列d16n900: IC_tr0.3133/OOS0.2885) — 直接做基线
        "v10a_fh_cnt": V9_FEATS + ["fh_cnt"],
        "v10b_fh_chg": V9_FEATS + ["fh_cnt_chg"],
        "v10c_fh_both": V9_FEATS + ["fh_cnt", "fh_cnt_chg"],
    }
    old = S.PARAMS
    S.PARAMS = V9_PARAMS
    out = []
    try:
        for name, feats in ARMS.items():
            r = S.run_arm(x, feats, f"v10_{name}")
            out.append(r)
            print(json.dumps(r), flush=True)
    finally:
        S.PARAMS = old
    (OUT / "results_v10.json").write_text(json.dumps(out, indent=1))
    print(f"DONE {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
