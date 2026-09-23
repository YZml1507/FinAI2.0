"""scripts/lab/e83_feature_sweep.py —— E83 特征边际贡献扫描。

预登记 docs/E83_FEATURE_AUGMENT_PREREG.md（已冻结后跑）。
复用 e63 同构 walk-forward（月末 sig、24 月窗、EMBARGO_TD=20、
PARAMS 固定），仅变 BASE 增列；输出各臂训练期 IC/t 与 2025 OOS IC。

用法：python -m scripts.lab.e83_feature_sweep [v0 v1 ...]
产出：experiments/lab/e83/results.json + scores_e83_<arm>.parquet
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

ROOT = Path(__file__).resolve().parents[2]
X_TRAIN = ROOT / "experiments/lab/e63_Xlab4.parquet"
X_SCORE = ROOT / "experiments/lab/e63_Xlab_2025.parquet"
OUT = ROOT / "experiments/lab/e83"
AUX = OUT / "aux_features.parquet"
TYPED = OUT / "typed_features.parquet"

sys.path.insert(0, str(ROOT))
from scripts.lab.e63_score_sweep_local import (  # noqa: E402
    BASE, MIN_TRAIN_MONTHS, EMBARGO_TD, PARAMS)

LABEL = "label150"
GROUP_7 = ["fund_cov", "fund_cov_chg",
           "s1_eps_rev90", "s2_np_rev90",
           "s3_fy_slope", "s4_pe_chg", "fwd_ep"]
ARMS = {
    "v0": BASE,
    "v1": BASE + ["fund_cov", "fund_cov_chg"],
    "v2": BASE + ["s1_eps_rev90", "s2_np_rev90",
                  "s3_fy_slope", "s4_pe_chg"],
    "v3": BASE + ["fwd_ep"],
    "v4": BASE + GROUP_7,
    "v5": BASE + ["ann_cnt60"],
    "v6": BASE + ["gdhs_qoq"],
    "v7": BASE + GROUP_7 + ["ann_cnt60", "gdhs_qoq"],
    # e84: 分类型密度 on top of the promoted v7 set (BASE now = v7 set)
    "v8": BASE + ["cnt_research60", "cnt_related60",
                  "cnt_divplan60", "cnt_guar60",
                  "cnt_exec60", "cnt_pledge60"],
}
SCORE_START = pd.Timestamp("2025-01-01")
TRAIN_START = pd.Timestamp("2017-01-01")


def _prep(x: pd.DataFrame, feats: list[str]) -> pd.DataFrame:
    x = x[x["valid"]].copy()
    for c in feats:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    mu = x.groupby("sig_date")[feats].transform("mean")
    sd = x.groupby("sig_date")[feats].transform("std").replace(0, np.nan)
    x[feats] = ((x[feats] - mu) / sd).fillna(0)
    x[LABEL] = pd.to_numeric(x[LABEL], errors="coerce")
    return x


def run_arm(x: pd.DataFrame, feats: list[str], arm: str) -> dict:
    sig_days = np.array(sorted(x["sig_date"].unique()))
    months = sig_days[sig_days >= TRAIN_START]
    ics_tr, ics_oos, frames = [], [], []
    for T in months:
        tr_days = sig_days[
            sig_days < T - pd.Timedelta(days=EMBARGO_TD + 10)
        ][-MIN_TRAIN_MONTHS:]
        tr = x[x["sig_date"].isin(tr_days) & x[LABEL].notna()]
        te = x[x["sig_date"] == T]
        if len(te) < 100 or len(tr_days) < MIN_TRAIN_MONTHS:
            continue
        ytr = tr.groupby("sig_date")[LABEL].rank(pct=True)
        m = xgb.XGBRegressor(**PARAMS)
        m.fit(tr[feats], ytr)
        sc = m.predict(te[feats])
        lab = te[LABEL]
        if lab.notna().sum() > 100:
            ic = pd.Series(sc[lab.notna().values]).corr(
                pd.Series(lab[lab.notna()].values), method="spearman")
            (ics_oos if T >= SCORE_START else ics_tr).append(ic)
        frames.append(pd.DataFrame({"sig_date": T,
                                    "ts_code": te["ts_code"].values,
                                    "score": sc}))
    def _stat(s):
        s = pd.Series(s)
        return (float(s.mean()),
                float(s.mean() / (s.std() / np.sqrt(s.size)))
                if len(s) > 1 else None)
    ic_tr, t_tr = _stat(ics_tr)
    ic_oos, t_oos = _stat(ics_oos)
    pd.concat(frames, ignore_index=True).to_parquet(
        OUT / f"scores_e83_{arm}.parquet")
    return {"arm": arm, "n_feat": len(feats),
            "ic_train": ic_tr, "t_train": t_tr,
            "ic_oos25": ic_oos, "t_oos25": t_oos,
            "n_ic_train": len(ics_tr), "n_ic_oos": len(ics_oos)}


def main() -> int:
    arms = sys.argv[1:] or list(ARMS)
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    xt = pd.read_parquet(X_TRAIN)
    xs = pd.read_parquet(X_SCORE)
    xt["src"] = "train"; xs["src"] = "score"
    X = pd.concat([xt, xs], ignore_index=True)
    if AUX.exists():
        aux = pd.read_parquet(AUX)
        X = X.merge(aux, on=["sig_date", "ts_code"], how="left")
    else:
        X["ann_cnt60"] = np.nan
        X["gdhs_qoq"] = np.nan
    if TYPED.exists():
        typed = pd.read_parquet(TYPED)
        X = X.merge(typed, on=["sig_date", "ts_code"], how="left")
    results = []
    for arm in arms:
        feats = ARMS[arm]
        missing = [c for c in feats if c not in X.columns]
        if missing:
            print(f"[{arm}] missing cols {missing} — skip")
            continue
        x = _prep(X, feats)
        r = run_arm(x, feats, arm)
        results.append(r)
        print(json.dumps(r, ensure_ascii=False), flush=True)
    out = OUT / "results.json"
    out.write_text(json.dumps({"arms": results,
                               "elapsed_min": round((time.time()-t0)/60, 1)},
                              ensure_ascii=False, indent=1))
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
