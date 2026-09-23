#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e87 剪枝臂：d16 参数下删末位特征的 IC 对照。

p1: BASE - an_epsrev20        (35列)
p2: BASE - 末5位(an_epsrev20,max20,ann_cnt60,ret5,ret20)  (31列)
p3: BASE - an_epsrev20 + fwd_ep删? 对照组 = d16 全列已由 e85 提供
"""
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "lab"))
import e83_feature_sweep as S  # noqa: E402

DROP5 = ["an_epsrev20", "max20", "ann_cnt60", "ret5", "ret20"]
DROP10 = DROP5 + ["s4_pe_chg", "s2_np_rev90", "s1_eps_rev90",
                  "fin_bal_chg20", "gdhs_qoq"]
GRID = {
    "p9_drop5n2000": DROP5,
    "p10_drop5n2500": DROP5,
}
PARM_OVERRIDES = {"p6_drop5n900": {"n_estimators": 900},
                  "p7_drop5n1200": {"n_estimators": 1200},
                  "p8_drop5n1500": {"n_estimators": 1500},
                  "p9_drop5n2000": {"n_estimators": 2000},
                  "p10_drop5n2500": {"n_estimators": 2500}}


def main() -> int:
    xt = pd.read_parquet(S.X_TRAIN)
    xs = pd.read_parquet(S.X_SCORE)
    xt["src"] = "train"; xs["src"] = "score"
    out = []
    for name, drops in GRID.items():
        feats = [c for c in S.BASE if c not in drops]
        x = S._prep(pd.concat([xt.copy(), xs.copy()],
                              ignore_index=True), feats)
        params = dict(S.PARAMS); params["max_depth"] = 16
        params.update(PARM_OVERRIDES.get(name, {}))
        old = S.PARAMS; S.PARAMS = params
        try:
            r = S.run_arm(x, feats, f"e87_{name}")
        finally:
            S.PARAMS = old
        r["dropped"] = drops
        out.append(r)
        print(json.dumps(r), flush=True)
    (S.OUT / "results_e87_r4.json").write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
