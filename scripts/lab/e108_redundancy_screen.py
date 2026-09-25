#!/usr/bin/env python3
"""e108 组合信号深度调研 · 第①步：冗余度解剖。

问题（用户假设）：弱信号之间、弱信号与强信号组合能否产生强信号？
先行证据一正一反：
- 正：e106 fh_cnt 单信号判弱（IC −0.028/t−2.18），挂入 v9 特征板后
  v10a 臂 IC_train +0.0067 / OOS25 +0.0104 —— 正交弱信号有边际价值；
- 反：e87 删掉 ann_cnt60/ret5/ret20/max20/an_epsrev20 后模型改善 ——
  冗余"弱"信号是噪声不是信息。

本步把全部已评弱信号与 v9 31 特征做 Spearman 相关矩阵：
与现有特征 max|ρ| 低的信号才是正交候选；高相关的先出局。
输出 experiments/lab/e108/redundancy.json + corr_matrix.parquet。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "lab"))
from e63_score_sweep_local import V9_FEATS  # noqa: E402

LAB = ROOT / "experiments" / "lab"
OUT = LAB / "e108"
OUT.mkdir(exist_ok=True)

V9_DROP = {"an_epsrev20", "max20", "ann_cnt60", "ret5", "ret20"}

SIG_SPECS = {
    "e100": ("sig_monthly.parquet", ["bert_pos_den", "bert_neg_den", "bert_posneg", "bert_conf", "bert_pos_cnt"]),
    "e101": ("sig_monthly.parquet", ["rev_breadth", "disp_cv", "cover_n", "cons_chg"]),
    "e102": ("sig_monthly.parquet", ["cover_n", "cover_d", "depth_sh", "org_n"]),
    "e103f": ("e103/sig_fund_monthly.parquet", ["fund_n", "fund_val"]),
    "e103": ("sig_holders_monthly.parquet", ["gdhs_lvl", "gdhs_chg", "hld_val"]),
    "e104": ("sig_pledge_monthly.parquet", ["plg_net3m", "plg_cnt3m", "plg_rel3m"]),
    "e106": ("sig_fund_heavy_monthly.parquet", ["fh_cnt", "fh_cnt_chg", "fh_mv_chg"]),
    "e107": ("sig_disc_monthly.parquet", ["disc_late", "disc_resched"]),
    "e99": ("sig_monthly.parquet", ["rev_breadth90", "rev_net90", "rating_chg90", "report_cnt30", "dispersion", "eps_slope", "n_orgs", "rating_score", "cov_chg", "tp_gap"]),
}


def load_x():
    x = pd.read_parquet(LAB / "e63_Xlab4.parquet")
    feats = [c for c in V9_FEATS if c in x.columns]
    return x, feats


def main() -> None:
    x, feats = load_x()
    print(f"X rows={len(x)} v9_feats={len(feats)}")
    x["period"] = pd.to_datetime(x["sig_date"]).dt.to_period("M")
    x["code6"] = x["ts_code"].str.extract(r"(\d{6})")

    # assemble weak-signal panel
    weak = {}
    for lab, (fn, cols) in SIG_SPECS.items():
        d = pd.read_parquet(LAB / fn) if "/" in fn else pd.read_parquet(LAB / lab / fn)
        d["code6"] = d["ts_code"].astype(str).str.extract(r"(\d{6})")
        d["period"] = pd.to_datetime(d["date"]).dt.to_period("M")
        for c in cols:
            key = f"{lab}_{c}"
            sub = d[["code6", "period", c]].dropna()
            weak[key] = sub.rename(columns={c: key})
    wp = None
    for k, sub in weak.items():
        wp = sub if wp is None else wp.merge(sub, on=["code6", "period"], how="outer")

    m = x.merge(wp, on=["code6", "period"], how="left")
    print(f"merged rows={len(m)}")

    weak_cols = list(weak.keys())
    corr = pd.DataFrame(index=weak_cols, columns=feats, dtype=float)
    cover = {}
    for w in weak_cols:
        ok = m[w].notna()
        cover[w] = float(ok.mean())
        if ok.sum() < 5000:
            continue
        for f in feats:
            corr.loc[w, f] = m.loc[ok, w].corr(m.loc[ok, f], method="spearman")

    corr.to_parquet(OUT / "corr_matrix.parquet")
    summary = []
    for w in weak_cols:
        row = corr.loc[w].dropna().abs()
        summary.append({
            "signal": w,
            "panel_coverage": round(cover[w], 4),
            "max_abs_rho": round(float(row.max()), 4) if len(row) else None,
            "max_rho_feat": row.idxmax() if len(row) else None,
            "mean_abs_rho": round(float(row.mean()), 4) if len(row) else None,
        })
    summary.sort(key=lambda r: (r["max_abs_rho"] is None, -(r["max_abs_rho"] or 0)))
    res = {"n_v9_feats": len(feats), "panel_rows": len(m), "signals": summary}
    (OUT / "redundancy.json").write_text(json.dumps(res, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
