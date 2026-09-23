#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e85 v7特征集(36列) XGBoost 超参局部重扫。

冠军参数 (d6/lr.03/n600) 是在 BASE27 时代定的；v7 升 36 列后
沿单轴各 ±1 档复核（6 臂），判强门 = OOS25 IC 或 train IC 较
冠军臂显著改善（ΔIC_train ≥ +0.005 才考虑换参）。

用法: python scripts/lab/e85_param_rescan.py [arm ...]
"""
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.lab import e83_feature_sweep as S  # noqa: E402

OUT = S.OUT
GRID = {
    "p0_d4":  dict(max_depth=4),
    "p1_d8":  dict(max_depth=8),
    "p2_lr2": dict(learning_rate=0.02),
    "p3_lr5": dict(learning_rate=0.05),
    "p4_n400": dict(n_estimators=400),
    "p5_n900": dict(n_estimators=900),
    # 二轮：d8 优势方向细化
    "q0_d10": dict(max_depth=10),
    "q1_d8n900": dict(max_depth=8, n_estimators=900),
    "q2_d8lr5": dict(max_depth=8, learning_rate=0.05),
}


def main() -> int:
    arms = sys.argv[1:] or list(GRID)
    t0 = time.time()
    xt = pd.read_parquet(S.X_TRAIN)
    xs = pd.read_parquet(S.X_SCORE)
    xt["src"] = "train"; xs["src"] = "score"
    X = pd.concat([xt, xs], ignore_index=True)
    x = S._prep(X, S.BASE)          # v7 = BASE36（aux 已并入矩阵）
    results = {}
    for arm in arms:
        over = GRID[arm]
        params = dict(S.PARAMS); params.update(over)
        # run_arm 用模块级 PARAMS —— 临时替换
        old = S.PARAMS; S.PARAMS = params
        try:
            r = S.run_arm(x, S.BASE, f"e85_{arm}")
        finally:
            S.PARAMS = old
        r["over"] = over
        results[arm] = r
        print(json.dumps(r), flush=True)
    (OUT / "results_e85.json").write_text(json.dumps(results, indent=1))
    print(f"done {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
