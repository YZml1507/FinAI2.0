"""e108 GPU 全臂复验 — V9_PARAMS walk-forward, device='cuda'.

逐臂 = e83 run_arm 同构：月末 sig、24 月窗、EMBARGO_TD=20、
train=116 月滚动 + oos25 月频 Spearman IC。
arms: 单信号 merge w_{name}; ix 臂 merge 预物化 ix 列。
产出: /kaggle/working/results.jsonl 每臂一行。
"""
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

DS = "/kaggle/input/datasets/mengxinyz/finai-e108-xlab"
LABEL = "label150"
MIN_TRAIN_MONTHS, EMBARGO_TD = 24, 20
TRAIN_START = pd.Timestamp("2017-01-01")
SCORE_START = pd.Timestamp("2025-01-01")

BASE = ['ret5', 'ret20', 'ret60', 'ret120', 'vol20', 'max20',
        'turnover20', 'amihud20', 'pe', 'pb', 'dv_ttm', 'circ_mv',
        'log_circ_mv', 'fin_bal_chg20', 'short_qty_chg20',
        'ev_letter', 'ev_resumption', 'ev_fc_pos', 'ev_fc_neg',
        'ev_incentive', 'ev_lhb', 'ev_insider_sell', 'ev_bt_inst_sell',
        'ev_reduce', 'ev_frozen', 'an_rating_dir20', 'an_epsrev20',
        'fund_cov', 'fund_cov_chg',
        's1_eps_rev90', 's2_np_rev90', 's3_fy_slope', 's4_pe_chg',
        'fwd_ep', 'ann_cnt60', 'gdhs_qoq']
E87_DROP = ["an_epsrev20", "max20", "ann_cnt60", "ret5", "ret20"]
V9 = [c for c in BASE if c not in E87_DROP]
PARAMS = dict(max_depth=16, learning_rate=0.03, n_estimators=900,
              min_child_weight=80, subsample=0.9, colsample_bytree=0.8,
              tree_method='hist', device='cuda', n_jobs=8)

# arm -> (file, raw column)
ARMS = {
    "ix_fh_to":      ("sig_ix_fh_turnover.parquet", "ix_fh_cnt_turnover20"),
    "ix_fh_fundcov": ("sig_ix_fh_fundcov.parquet", "ix_fh_cnt_fund_cov"),
    "ix_disc_ret":   ("sig_ix_disc_late_ret120.parquet", "ix_disc_late_ret120"),
    "ix_gdhs_mv":    ("sig_ix_gdhs_circ_mv.parquet", "ix_gdhs_chg_circ_mv"),
    "ix_rating_rev": ("sig_ix_rating_rev.parquet", "ix_rating_chg90_rev_net90"),
    "ix_rating_eps": ("sig_ix_rating_epsrev.parquet", "ix_rating_chg90_s1_eps_rev90"),
    "ix_bert_to":    ("sig_ix_bert_to.parquet", "ix_bert_conf_turnover20"),
    "ix_plg_mv":     ("sig_ix_plg_mv.parquet", "ix_plg_net3m_circ_mv"),
    "ix_fhchg_vol":  ("sig_ix_fhchg_vol.parquet", "ix_fh_cnt_chg_vol20"),
    "ix_disc_lr":    ("sig_ix_disc_late_resched.parquet", "ix_disc_late_disc_resched"),
}


def _prep(x, feats):
    x = x[x["valid"]].copy()
    for c in feats:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    mu = x.groupby("sig_date")[feats].transform("mean")
    sd = x.groupby("sig_date")[feats].transform("std").replace(0, np.nan)
    x[feats] = ((x[feats] - mu) / sd).fillna(0)
    x[LABEL] = pd.to_numeric(x[LABEL], errors="coerce")
    return x


def load_signal(fn, col):
    d = pd.read_parquet(f"{DS}/{fn}")
    d["code6"] = d["ts_code"].astype(str).str.extract(r"(\d{6})")
    if "period" not in d.columns:
        dcol = "sig_date" if "sig_date" in d.columns else "date"
        d["period"] = pd.to_datetime(d[dcol]).dt.to_period("M")
    return d[["code6", "period", col]].dropna()


def _stat(s):
    s = pd.Series(s)
    return (float(s.mean()),
            float(s.mean() / (s.std() / np.sqrt(s.size)))
            if len(s) > 1 else None)


def run_arm(x, feats, arm):
    sig_days = np.array(sorted(x["sig_date"].unique()))
    months = sig_days[sig_days >= TRAIN_START]
    ics_tr, ics_oos = [], []
    tt = time.time()
    for i, T in enumerate(months):
        tr_days = sig_days[
            sig_days < T - pd.Timedelta(days=EMBARGO_TD + 10)
        ][-MIN_TRAIN_MONTHS:]
        tr = x[x["sig_date"].isin(tr_days) & x[LABEL].notna()]
        te = x[x["sig_date"] == T]
        if len(te) < 100 or len(tr_days) < MIN_TRAIN_MONTHS:
            continue
        ytr = tr.groupby("sig_date")[LABEL].rank(pct=True)
        m = xgb.XGBRegressor(**PARAMS)
        m.fit(tr[feats], ytr)
        sc = m.predict(te[feats])
        lab = te[LABEL]
        if lab.notna().sum() > 100:
            ic = pd.Series(sc[lab.notna().values]).corr(
                pd.Series(lab[lab.notna()].values), method="spearman")
            (ics_oos if T >= SCORE_START else ics_tr).append(ic)
        if (i + 1) % 20 == 0:
            print(f"  {arm} [{i+1}/{len(months)}] {time.time()-tt:.0f}s",
                  flush=True)
    ic_tr, t_tr = _stat(ics_tr)
    ic_oos, t_oos = _stat(ics_oos)
    return {"arm": arm, "n_feat": len(feats),
            "ic_train": ic_tr, "t_train": t_tr,
            "ic_oos25": ic_oos, "t_oos25": t_oos,
            "n_ic_train": len(ics_tr), "n_ic_oos": len(ics_oos),
            "wall_min": round((time.time() - tt) / 60, 1)}


t0 = time.time()
xt = pd.read_parquet(f"{DS}/e63_Xlab4.parquet")
xs = pd.read_parquet(f"{DS}/e63_Xlab_2025.parquet")
x0 = pd.concat([xt, xs], ignore_index=True)
x0["code6"] = x0["ts_code"].str.extract(r"(\d{6})")
x0["period"] = pd.to_datetime(x0["sig_date"]).dt.to_period("M")
print(f"data {x0.shape} {time.time()-t0:.0f}s", flush=True)

out_path = Path("/kaggle/working/results.jsonl")
for arm, (fn, col) in ARMS.items():
    feats = V9 + [f"w_{arm}"]
    sig = load_signal(fn, col).rename(columns={col: f"w_{arm}"})
    x = x0.merge(sig, on=["code6", "period"], how="left")
    cov = x[f"w_{arm}"].notna().mean()
    print(f"{arm}: cov={cov:.3f}", flush=True)
    x = _prep(x, feats)
    r = run_arm(x, feats, arm)
    r["cov"] = round(float(cov), 4)
    print(json.dumps(r), flush=True)
    with open(out_path, "a") as fp:
        fp.write(json.dumps(r) + "\n")
print(f"ALL DONE {(time.time()-t0)/60:.0f}min", flush=True)
