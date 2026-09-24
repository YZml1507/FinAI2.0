#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e95 v9 超参微调：31 列剪枝集 + d16 下 colsample/mcw/lr 微网格。

e85 在旧 36 列上扫过参数面；剪枝集构成不同正则化需求，补扫 4 臂。
基线 = v9 p6（IC_train 0.3192/OOS25 0.2989，e87_r2 同口径）。
"""
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "lab"))
import e83_feature_sweep as S  # noqa: E402

E87_DROP = ["an_epsrev20", "max20", "ann_cnt60", "ret5", "ret20"]
GRID = {
    "cs07": {"colsample_bytree": 0.7},
    "mcw60": {"min_child_weight": 60},
    "lr02": {"learning_rate": 0.02},
    "lr05": {"learning_rate": 0.05},
}


def main() -> int:
    xt = pd.read_parquet(S.X_TRAIN)
    xs = pd.read_parquet(S.X_SCORE)
    xt["src"] = "train"; xs["src"] = "score"
    x_all = pd.concat([xt, xs], ignore_index=True)
    feats = [c for c in S.BASE if c not in E87_DROP]
    x = S._prep(x_all, feats)
    out = []
    for name, ov in GRID.items():
        params = dict(S.PARAMS, max_depth=16, n_estimators=900)
        params.update(ov)
        old = S.PARAMS; S.PARAMS = params
        try:
            r = S.run_arm(x, feats, f"e95_{name}_d16n900")
        finally:
            S.PARAMS = old
        r["override"] = ov
        out.append(r)
        print(json.dumps(r), flush=True)
    (S.OUT / "results_e95.json").write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
