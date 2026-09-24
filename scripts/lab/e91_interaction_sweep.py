#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e91 交互项臂：v9(31列,d16n900) + 高重要度特征交叉积。

交叉项在原始列上构造后随 _prep 按 sig_date 截面 z-score。
对照 = v9 p6 臂（e87 results：IC_train 0.2867 / OOS 0.2190）。
门禁沿用 e83 预登记：IC_train ≥ 基线+0.010 且 OOS ≥ 基线-0.01 才有晋级资格。
"""
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "lab"))
import e83_feature_sweep as S  # noqa: E402

E87_DROP = ["an_epsrev20", "max20", "ann_cnt60", "ret5", "ret20"]

# 原始列交叉（数值乘积；缺失自然传播为 NaN 由 _prep 兜底）
IX = {
    "ix_turn_pb": ("turnover20", "pb"),
    "ix_turn_vol": ("turnover20", "vol20"),
    "ix_size_pb": ("log_circ_mv", "pb"),
    "ix_turn_ret60": ("turnover20", "ret60"),
    "ix_fund_size": ("fund_cov", "log_circ_mv"),
}


def main() -> int:
    xt = pd.read_parquet(S.X_TRAIN)
    xs = pd.read_parquet(S.X_SCORE)
    xt["src"] = "train"; xs["src"] = "score"
    x_all = pd.concat([xt, xs], ignore_index=True)
    for name, (a, b) in IX.items():
        x_all[name] = pd.to_numeric(x_all[a], errors="coerce") * \
            pd.to_numeric(x_all[b], errors="coerce")
    feats = [c for c in S.BASE if c not in E87_DROP] + list(IX)
    x = S._prep(x_all, feats)
    params = dict(S.PARAMS, max_depth=16, n_estimators=900)
    old = S.PARAMS; S.PARAMS = params
    try:
        r = S.run_arm(x, feats, "e91_ix5_d16n900")
    finally:
        S.PARAMS = old
    r["added"] = list(IX)
    print(json.dumps(r), flush=True)
    (S.OUT / "results_e91.json").write_text(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
