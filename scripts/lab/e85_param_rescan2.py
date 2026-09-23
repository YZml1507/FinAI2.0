#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e85 三轮：depth 天花板上探（d10 胜出后）。

复用 e83_feature_sweep.run_arm，monkey-patch 参数跑 3 臂：
  r0_d12    max_depth=12
  r1_d10n900  d10 + n_estimators=900
  r2_d10lr05  d10 + learning_rate=0.05
"""
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "lab"))
import e83_feature_sweep as S  # noqa: E402

GRID = {
    "r6_d18": {"max_depth": 18},
    "r7_d20": {"max_depth": 20},
    "r8_d16n900": {"max_depth": 16, "n_estimators": 900},
}


def main() -> int:
    xt = pd.read_parquet(S.X_TRAIN)
    xs = pd.read_parquet(S.X_SCORE)
    xt["src"] = "train"; xs["src"] = "score"
    x = S._prep(pd.concat([xt, xs], ignore_index=True), S.BASE)
    out = []
    for name, over in GRID.items():
        params = dict(S.PARAMS); params.update(over)
        old = S.PARAMS; S.PARAMS = params
        try:
            r = S.run_arm(x, S.BASE, f"e85_{name}")
            r["over"] = over
            out.append(r)
        finally:
            S.PARAMS = old
        print(json.dumps(out[-1]), flush=True)
    p = S.OUT / "results_e85_r4.json"
    p.write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
