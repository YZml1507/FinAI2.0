"""e63 OOS-2025 打分登记（只观察，不参与晋升决策）。

训练行取 e63_Xlab4（2016→2024 in-sample 全窗），打分日取
e63_Xlab_2025（2025-01→最新）。walk-forward 口径与
e63_score_sweep_local / kernel68 完全一致：每月 T 用此前 24 个月
（embargo 30d）训练 XGBRegressor，对 T 截面打分。

输出:
  experiments/lab/e63/scores_label150_2025.parquet
  experiments/lab/e63/results_label150_2025.json（IC 仅登记，不判强弱）
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
OUT = ROOT / "experiments/lab/e63"

sys.path.insert(0, str(ROOT))
from scripts.lab.e63_score_sweep_local import (  # noqa: E402
    V9_FEATS as BASE, MIN_TRAIN_MONTHS, EMBARGO_TD,
    V9_PARAMS as PARAMS)

LABEL = "label150"
SCORE_START = pd.Timestamp("2025-01-01")


def main() -> int:
    t0 = time.time()
    xt = pd.read_parquet(X_TRAIN)
    xs = pd.read_parquet(X_SCORE)
    xt["src"] = "train"; xs["src"] = "score"
    X = pd.concat([xt, xs], ignore_index=True)
    X = X[X["valid"]].copy()
    for c in BASE:
        X[c] = pd.to_numeric(X[c], errors="coerce")
    mu = X.groupby("sig_date")[BASE].transform("mean")
    sd = X.groupby("sig_date")[BASE].transform("std").replace(0, np.nan)
    X[BASE] = ((X[BASE] - mu) / sd).fillna(0)
    X[LABEL] = pd.to_numeric(X[LABEL], errors="coerce")

    sig_days = np.array(sorted(X["sig_date"].unique()))
    score_days = [d for d in sig_days if d >= SCORE_START]
    frames, ics = [], []
    for T in score_days:
        tr_days = sig_days[
            sig_days < T - pd.Timedelta(days=EMBARGO_TD + 10)
        ][-MIN_TRAIN_MONTHS:]
        tr = X[X["sig_date"].isin(tr_days) & X[LABEL].notna()]
        te = X[(X["sig_date"] == T) & (X["src"] == "score")]
        if len(te) < 100 or len(tr_days) < MIN_TRAIN_MONTHS:
            print(f"[skip] {T} tr_months={len(tr_days)} te={len(te)}")
            continue
        ytr = tr.groupby("sig_date")[LABEL].rank(pct=True)
        m = xgb.XGBRegressor(**PARAMS)
        m.fit(tr[BASE], ytr)
        sc = m.predict(te[BASE])
        lab = te[LABEL]
        n_lab = int(lab.notna().sum())
        if n_lab > 100:
            ics.append(pd.Series(sc[lab.notna().values]).corr(
                pd.Series(lab[lab.notna()].values), method="spearman"))
        frames.append(pd.DataFrame({"sig_date": T,
                                    "ts_code": te["ts_code"].values,
                                    "score": sc}))
        print(f"[score] {pd.Timestamp(T).date()} rows={len(te)} "
              f"labeled={n_lab}", flush=True)
    s = pd.Series(ics)
    res = {"label": LABEL, "n_months_labeled": int(s.size),
           "ic_mean": float(s.mean()) if s.size else None,
           "t": float(s.mean() / (s.std() / np.sqrt(s.size)))
           if s.size > 1 else None,
           "n_sig_scored": len(frames),
           "score_start": str(SCORE_START.date()),
           "note": "OOS-2025 observe-only; IC 不作晋升判据",
           "elapsed_min": round((time.time() - t0) / 60, 1)}
    out = OUT / "scores_label150_2025.parquet"
    pd.concat(frames, ignore_index=True).to_parquet(out)
    (OUT / "results_label150_2025.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1))
    print(json.dumps(res, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
