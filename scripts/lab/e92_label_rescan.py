#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e92 标签视野重扫：v9(31列,d16n900) 下 h90/120/180/240 vs h150 基线。

e63 时代在旧 36 列 d6 参数上视野单调升；v9 深树+剪枝后最优视野可能迁移。
判据沿用 e83 预登记。
"""
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "lab"))
import e83_feature_sweep as S  # noqa: E402

E87_DROP = ["an_epsrev20", "max20", "ann_cnt60", "ret5", "ret20"]
LABELS = ["label90", "label120", "label180", "label240"]


def main() -> int:
    xt = pd.read_parquet(S.X_TRAIN)
    xs = pd.read_parquet(S.X_SCORE)
    xt["src"] = "train"; xs["src"] = "score"
    x_all = pd.concat([xt, xs], ignore_index=True)
    feats = [c for c in S.BASE if c not in E87_DROP]
    params = dict(S.PARAMS, max_depth=16, n_estimators=900)
    out = []
    for lab in LABELS:
        x = S._prep(x_all.copy(), feats)
        x["label150"] = pd.to_numeric(x[lab], errors="coerce")  # 列映射复用
        old_p, old_l = S.PARAMS, S.LABEL
        S.PARAMS = params
        try:
            r = S.run_arm(x, feats, f"e92_{lab}_d16n900")
        finally:
            S.PARAMS = old_p
        r["label_src"] = lab
        out.append(r)
        print(json.dumps(r), flush=True)
    (S.OUT / "results_e92.json").write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
