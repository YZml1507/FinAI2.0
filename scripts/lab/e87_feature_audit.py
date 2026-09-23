#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e87 特征重要性审计（d16 参数，与 e85 最优臂一致）。

对每个 walk-forward 月度模型取 feature_importances_(gain)，
跨月聚合 → 均次/稳定性/末位特征，给 e87 剪枝臂依据。
输出 experiments/lab/e83/importance_e87.json + .csv
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "lab"))
import e83_feature_sweep as S  # noqa: E402

OUT = S.OUT


def main() -> int:
    xt = pd.read_parquet(S.X_TRAIN)
    xs = pd.read_parquet(S.X_SCORE)
    xt["src"] = "train"; xs["src"] = "score"
    x = S._prep(pd.concat([xt, xs], ignore_index=True), S.BASE)
    feats = S.BASE
    params = dict(S.PARAMS); params["max_depth"] = 16

    sig_days = np.array(sorted(x["sig_date"].unique()))
    months = sig_days[sig_days >= S.TRAIN_START]
    rows = []
    for T in months:
        tr_days = sig_days[
            sig_days < T - pd.Timedelta(days=S.EMBARGO_TD + 10)
        ][-S.MIN_TRAIN_MONTHS:]
        tr = x[x["sig_date"].isin(tr_days) & x[S.LABEL].notna()]
        if len(tr_days) < S.MIN_TRAIN_MONTHS:
            continue
        ytr = tr.groupby("sig_date")[S.LABEL].rank(pct=True)
        m = xgb.XGBRegressor(**params)
        m.fit(tr[feats], ytr)
        imp = m.feature_importances_
        rows.append(pd.Series(imp, index=feats, name=str(T)[:10]))
    im = pd.DataFrame(rows)
    im.to_csv(OUT / "importance_e87.csv")
    stats = pd.DataFrame({
        "mean_gain": im.mean(), "median_gain": im.median(),
        "rank_mean": im.rank(axis=1, ascending=False).mean(),
        "zero_frac": (im == 0).mean(),
    }).sort_values("mean_gain", ascending=False)
    out = {"n_months": int(len(im)),
           "params": {k: params[k] for k in ("max_depth",
                                           "n_estimators",
                                           "learning_rate")},
           "features": stats.round(5).to_dict("index")}
    (OUT / "importance_e87.json").write_text(json.dumps(out, indent=1))
    print(stats.to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
