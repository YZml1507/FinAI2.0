"""e108 边际贡献快筛（留出法，非 walk-forward）

全 walk-forward 每臂 ~4-7h 不可行。本脚本做单次 fit 留出法快筛：
  train: sig_date < 2022-01-01
  test_in: 2022-01-01 <= sig_date <= 2024-12-31  (样本内留出)
  oos25: 2025-01-01 起（e63_Xlab_2025, 永久 OOS）
每臂 = base(31 feats) vs base+w(32 feats) 同参数同数据各一次 fit，
比较月度 Spearman IC(test_in) 与 IC(oos25)。双口径均改善 → 进慢速复验。
winner 名单追加到实验目录 e108/winners.json。

用法: python e108_marginal_fast.py --arms base,gdhs_chg,... --tag s1
"""
import argparse, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "lab"))
import pandas as pd
import numpy as np
import e83_feature_sweep as S
from e63_score_sweep_local import V9_FEATS
from scipy.stats import spearmanr
from xgboost import XGBRegressor

LAB = ROOT / "experiments" / "lab"
OUT = LAB / "e108"

# 快筛参数：与 V9 同深度但树数减半；胜出者回 V9_PARAMS 全 walk-forward 复验
FAST_PARAMS = dict(S.PARAMS, n_estimators=300, n_jobs=4)

CANDIDATES = {
    "gdhs_chg":    ("e103/sig_holders_monthly.parquet", "gdhs_chg"),
    "fh_cnt":      ("e106/sig_fund_heavy_monthly.parquet", "fh_cnt"),
    "disc_late":   ("e107/sig_disc_monthly.parquet", "disc_late"),
    "disc_resched":("e107/sig_disc_monthly.parquet", "disc_resched"),
    "plg_net3m":   ("e104/sig_pledge_monthly.parquet", "plg_net3m"),
    "fh_cnt_chg":  ("e106/sig_fund_heavy_monthly.parquet", "fh_cnt_chg"),
    "fh_mv_chg":   ("e106/sig_fund_heavy_monthly.parquet", "fh_mv_chg"),
    "e101_disp_cv":("e101/sig_monthly.parquet", "disp_cv"),
    "cover_d":     ("e102/sig_monthly.parquet", "cover_d"),
    "depth_sh":    ("e102/sig_monthly.parquet", "depth_sh"),
    "rating_chg90":("e99/sig_monthly.parquet", "rating_chg90"),
    "rating_score":("e99/sig_monthly.parquet", "rating_score"),
    "rev_net90":   ("e99/sig_monthly.parquet", "rev_net90"),
    "bert_conf":   ("e100/sig_monthly.parquet", "bert_conf"),
}

TRAIN_END = pd.Timestamp("2022-01-01")
TEST_END = pd.Timestamp("2025-01-01")


def load_signal(name):
    fn, col = CANDIDATES[name]
    p = LAB / fn
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    df["code6"] = df["ts_code"].astype(str).str.extract(r"(\d{6})")
    dcol = "sig_date" if "sig_date" in df.columns else "date"
    df["period"] = pd.to_datetime(df[dcol]).dt.to_period("M")
    return df[["code6", "period", col]].rename(columns={col: f"w_{name}"})


def monthly_ic(scored):
    """scored: df with sig_date, pred, label -> list of monthly spearman"""
    ics = []
    for d, g in scored.groupby("sig_date"):
        if g["label"].notna().sum() < 50:
            continue
        r, _ = spearmanr(g["pred"], g["label"], nan_policy="omit")
        if np.isfinite(r):
            ics.append(r)
    return np.array(ics)


def znorm(x, feats):
    mu = x.groupby("sig_date")[feats].transform("mean")
    sd = x.groupby("sig_date")[feats].transform("std").replace(0, np.nan)
    x[feats] = ((x[feats] - mu) / sd).fillna(0)
    return x


def fit_pred(xtr, feats, xte):
    ytr = xtr.groupby("sig_date")["label"].rank(pct=True)
    m = XGBRegressor(**FAST_PARAMS)
    m.fit(xtr[feats].values, ytr.values)
    out = xte[["ts_code", "sig_date", "label"]].copy()
    out["pred"] = m.predict(xte[feats].values)
    return out, m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="base," + ",".join(CANDIDATES))
    ap.add_argument("--tag", required=True)
    ap.add_argument("--ix", action="store_true",
                    help="交互项模式: arms 形如 w_name:v9feat，生成 z(w)*z(f) 乘积列")
    a = ap.parse_args()
    arms = [s.strip() for s in a.arms.split(",") if s.strip()]
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    x_all = pd.read_parquet(S.X_TRAIN)
    x_all["period"] = x_all["sig_date"].dt.to_period("M")
    x_all["code6"] = x_all["ts_code"].astype(str).str.extract(r"(\d{6})")
    x_in = x_all[x_all["sig_date"] < TEST_END].copy()
    xtr = x_in[x_in["sig_date"] < TRAIN_END]
    xte = x_in[(x_in["sig_date"] >= TRAIN_END)]

    x_oos = pd.read_parquet(S.X_SCORE)
    x_oos["period"] = x_oos["sig_date"].dt.to_period("M")
    x_oos["code6"] = x_oos["ts_code"].astype(str).str.extract(r"(\d{6})")

    feats_base = list(V9_FEATS)
    results = []

    base_state = {}

    for name in arms:
        if name == "base":
            feats, xtr_u, xte_u, xo_u = feats_base, xtr, xte, x_oos
        elif a.ix and ":" in name:
            wname, f = name.split(":", 1)
            sig = load_signal(wname)
            if sig is None:
                results.append({"arm": name, "error": "bad ix spec"})
                print(json.dumps(results[-1]), flush=True)
                continue
            ix = f"ix_{wname}_{f}"
            feats = feats_base + [ix]
            xtr_u = xtr.merge(sig, on=["code6", "period"], how="left")
            xte_u = xte.merge(sig, on=["code6", "period"], how="left")
            xo_u = x_oos.merge(sig, on=["code6", "period"], how="left")
            wcol = f"w_{wname}"
            if f not in xtr.columns:  # 弱×弱：第二项也按信号加载
                sig2 = load_signal(f)
                if sig2 is None:
                    results.append({"arm": name, "error": "bad ix spec2"})
                    print(json.dumps(results[-1]), flush=True)
                    continue
                fcol2 = f"w2_{f}"
                sig2 = sig2.rename(columns={f"w_{f}": fcol2})
                xtr_u = xtr_u.merge(sig2, on=["code6", "period"], how="left")
                xte_u = xte_u.merge(sig2, on=["code6", "period"], how="left")
                xo_u = xo_u.merge(sig2, on=["code6", "period"], how="left")
            else:
                fcol2 = f
            for df in (xtr_u, xte_u, xo_u):
                df[wcol] = pd.to_numeric(df[wcol], errors="coerce")
                m_ = df.groupby("sig_date")[wcol].transform("mean")
                s_ = df.groupby("sig_date")[wcol].transform("std").replace(0, np.nan)
                zw = ((df[wcol] - m_) / s_).fillna(0)
                df[fcol2] = pd.to_numeric(df[fcol2], errors="coerce")
                zf = (df[fcol2] - df.groupby("sig_date")[fcol2].transform("mean")) / df.groupby("sig_date")[fcol2].transform("std").replace(0, np.nan)
                df[ix] = (zw * zf.fillna(0))
            cov = xtr_u[wcol].notna().mean()
            results.append({"arm": name, "merge_cov_train": round(float(cov), 4)})
            print(json.dumps(results[-1]), flush=True)
        else:
            sig = load_signal(name)
            if sig is None:
                results.append({"arm": name, "error": "no signal file"})
                print(json.dumps(results[-1]), flush=True)
                continue
            feats = feats_base + [f"w_{name}"]
            xtr_u = xtr.merge(sig, on=["code6", "period"], how="left")
            xte_u = xte.merge(sig, on=["code6", "period"], how="left")
            xo_u = x_oos.merge(sig, on=["code6", "period"], how="left")
            cov = xtr_u[f"w_{name}"].notna().mean()
            results.append({"arm": name, "merge_cov_train": round(float(cov), 4)})
            print(json.dumps(results[-1]), flush=True)

        for df in (xtr_u, xte_u, xo_u):
            for f in feats:
                df[f] = pd.to_numeric(df[f], errors="coerce")
            znorm(df, feats)

        pred_in, m = fit_pred(xtr_u, feats, xte_u)
        pred_oos, _ = fit_pred(xtr_u, feats, xo_u)
        ic_in = monthly_ic(pred_in)
        ic_oos = monthly_ic(pred_oos)
        imp = {k: round(float(v), 4) for k, v in
               sorted(zip(feats, m.feature_importances_), key=lambda kv: -kv[1])[:8]}
        r = {
            "arm": name, "n_feat": len(feats),
            "ic_in": round(float(ic_in.mean()), 5), "t_in": round(float(ic_in.mean() / (ic_in.std(ddof=1) / np.sqrt(len(ic_in)))), 2),
            "ic_oos25": round(float(ic_oos.mean()), 5), "t_oos25": round(float(ic_oos.mean() / (ic_oos.std(ddof=1) / np.sqrt(len(ic_oos)))), 2),
            "n_in": len(ic_in), "n_oos": len(ic_oos), "top_feats": imp,
        }
        if name == "base":
            base_state.update(r)
        results.append(r)
        print(json.dumps(r), flush=True)
        (OUT / f"fast_{a.tag}.json").write_text(json.dumps(results, indent=1))
    print(f"DONE {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
