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
SCREEN_PARAMS = dict(S.PARAMS, max_depth=6, n_estimators=250)

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
    "ix_gdhs_mv":  ("e108/sig_ix_gdhs_circ_mv.parquet", "ix_gdhs_chg_circ_mv"),
    "ix_fh_to":    ("e108/sig_ix_fh_turnover.parquet", "ix_fh_cnt_turnover20"),
    "ix_fh_fundcov": ("e108/sig_ix_fh_fundcov.parquet", "ix_fh_cnt_fund_cov"),
    "ix_disc_ret": ("e108/sig_ix_disc_late_ret120.parquet", "ix_disc_late_ret120"),
    "ix_rating_rev": ("e108/sig_ix_rating_rev.parquet", "ix_rating_chg90_rev_net90"),
    "ix_rating_eps": ("e108/sig_ix_rating_epsrev.parquet", "ix_rating_chg90_s1_eps_rev90"),
    "ix_bert_to":  ("e108/sig_ix_bert_to.parquet", "ix_bert_conf_turnover20"),
    "ix_plg_mv":   ("e108/sig_ix_plg_mv.parquet", "ix_plg_net3m_circ_mv"),
    "ix_fhchg_vol": ("e108/sig_ix_fhchg_vol.parquet", "ix_fh_cnt_chg_vol20"),
    "ix_disc_lr":  ("e108/sig_ix_disc_late_resched.parquet", "ix_disc_late_disc_resched"),
}


def load_signal(fn: str, col: str) -> pd.DataFrame:
    d = pd.read_parquet(LAB / fn)
    d["code6"] = d["ts_code"].astype(str).str.extract(r"(\d{6})")
    if "period" not in d.columns:
        d["period"] = pd.to_datetime(d["date"]).dt.to_period("M")
    return d[["code6", "period", col]].dropna()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="base," + ",".join(CANDIDATES))
    ap.add_argument("--tag", default="all")
    ap.add_argument("--v9params", action="store_true",
                    help="全参复验模式 (V9_PARAMS, 生产口径)")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    xt = pd.read_parquet(S.X_TRAIN)
    xs = pd.read_parquet(S.X_SCORE)
    x = pd.concat([xt.assign(src="train"), xs.assign(src="score")], ignore_index=True)
    x["code6"] = x["ts_code"].str.extract(r"(\d{6})")
    x["period"] = pd.to_datetime(x["sig_date"]).dt.to_period("M")

    arms = [s.strip() for s in a.arms.split(",") if s.strip()]
    from e63_score_sweep_local import V9_PARAMS
    old = S.PARAMS
    S.PARAMS = V9_PARAMS if a.v9params else SCREEN_PARAMS
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
