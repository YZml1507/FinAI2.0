#!/usr/bin/env python3
"""e108 组合信号深度调研 · 第②步：边际贡献扫描。

每个已评弱信号单独挂入 V9_FEATS 走 walk-forward（同 e83 run_arm），
降参筛选版（max_depth=8, n_estimators=300, ~4x 加速）——先看同参数
v9 基线再逐信号比较；双口径（ic_train + ic_oos25）同向改善才算胜。
胜出信号回 V9_PARAMS 全参复验（v10 流程）。

用法: python e108_marginal_sweep.py --arms <逗号分隔> --tag <shard名>
输出: experiments/lab/e108/marginal_{tag}.json + scores parquets (e83 dir)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "lab"))

import e83_feature_sweep as S  # noqa: E402
from e63_score_sweep_local import V9_FEATS  # noqa: E402

LAB = ROOT / "experiments" / "lab"
OUT = LAB / "e108"

# 筛选参数（快 4x）：胜出者回 V9_PARAMS 全参复验
SCREEN_PARAMS = dict(S.PARAMS, max_depth=8, n_estimators=300)

# 每个候选: (信号文件, 信号列)。与 e108 冗余度筛同一批正交+对照信号
CANDIDATES = {
    "gdhs_chg":    ("e103/sig_holders_monthly.parquet", "gdhs_chg"),
    "gdhs_lvl":    ("e103/sig_holders_monthly.parquet", "gdhs_lvl"),
    "fh_cnt":      ("e106/sig_fund_heavy_monthly.parquet", "fh_cnt"),
    "fh_cnt_chg":  ("e106/sig_fund_heavy_monthly.parquet", "fh_cnt_chg"),
    "fh_mv_chg":   ("e106/sig_fund_heavy_monthly.parquet", "fh_mv_chg"),
    "disc_late":   ("e107/sig_disc_monthly.parquet", "disc_late"),
    "disc_resched":("e107/sig_disc_monthly.parquet", "disc_resched"),
    "plg_net3m":   ("e104/sig_pledge_monthly.parquet", "plg_net3m"),
    "plg_cnt3m":   ("e104/sig_pledge_monthly.parquet", "plg_cnt3m"),
    "cover_n":     ("e102/sig_monthly.parquet", "cover_n"),
    "cover_d":     ("e102/sig_monthly.parquet", "cover_d"),
    "depth_sh":    ("e102/sig_monthly.parquet", "depth_sh"),
    "org_n":       ("e102/sig_monthly.parquet", "org_n"),
    "rev_breadth90":("e99/sig_monthly.parquet", "rev_breadth90"),
    "rev_net90":   ("e99/sig_monthly.parquet", "rev_net90"),
    "rating_chg90":("e99/sig_monthly.parquet", "rating_chg90"),
    "rating_score":("e99/sig_monthly.parquet", "rating_score"),
    "n_orgs":      ("e99/sig_monthly.parquet", "n_orgs"),
    "bert_pos_den":("e100/sig_monthly.parquet", "bert_pos_den"),
    "bert_neg_den":("e100/sig_monthly.parquet", "bert_neg_den"),
    "bert_conf":   ("e100/sig_monthly.parquet", "bert_conf"),
}


def load_signal(fn: str, col: str) -> pd.DataFrame:
    d = pd.read_parquet(LAB / fn)
    d["code6"] = d["ts_code"].astype(str).str.extract(r"(\d{6})")
    d["period"] = pd.to_datetime(d["date"]).dt.to_period("M")
    return d[["code6", "period", col]].dropna()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="base," + ",".join(CANDIDATES))
    ap.add_argument("--tag", default="all")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    xt = pd.read_parquet(S.X_TRAIN)
    xs = pd.read_parquet(S.X_SCORE)
    x = pd.concat([xt.assign(src="train"), xs.assign(src="score")], ignore_index=True)
    x["code6"] = x["ts_code"].str.extract(r"(\d{6})")
    x["period"] = pd.to_datetime(x["sig_date"]).dt.to_period("M")

    arms = [s.strip() for s in a.arms.split(",") if s.strip()]
    old = S.PARAMS
    S.PARAMS = SCREEN_PARAMS
    results = []
    for name in arms:
        if name == "base":
            feats = list(V9_FEATS)
        else:
            fn, col = CANDIDATES[name]
            sig = load_signal(fn, col)
            x = x.merge(sig.rename(columns={col: f"w_{name}"}),
                        on=["code6", "period"], how="left")
            feats = list(V9_FEATS) + [f"w_{name}"]
        xp = S._prep(x.copy(), feats)
        r = S.run_arm(xp, feats, f"e108_{name}")
        results.append(r)
        print(json.dumps(r), flush=True)
        (OUT / f"marginal_{a.tag}.json").write_text(
            json.dumps(results, indent=1))
    print(f"DONE {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
