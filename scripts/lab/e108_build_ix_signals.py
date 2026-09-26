"""e108 交互项信号物化：ix_<w>_<f> = z(w)*z(f)（per sig_date 截面 z）

与 e108_marginal_fast.py 的 --ix 模式同一公式：
在 Xlab 全集行上，对每个 sig_date 分别 z-normalize w（外挂信号）与
f（V9 特征或第二外挂信号），乘积写入 experiments/lab/e108/sig_ix_*.parquet。
产出列: ts_code, code6, period, ix_<w>_<f>

用法: python -m scripts.lab.e108_build_ix_signals
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "lab"))
import e83_feature_sweep as S  # noqa: E402

LAB = ROOT / "experiments" / "lab"
OUT = LAB / "e108"

SIG = {
    "gdhs_chg":    ("e103/sig_holders_monthly.parquet", "gdhs_chg"),
    "fh_cnt":      ("e106/sig_fund_heavy_monthly.parquet", "fh_cnt"),
    "fh_cnt_chg":  ("e106/sig_fund_heavy_monthly.parquet", "fh_cnt_chg"),
    "disc_late":   ("e107/sig_disc_monthly.parquet", "disc_late"),
    "disc_resched":("e107/sig_disc_monthly.parquet", "disc_resched"),
    "plg_net3m":   ("e104/sig_pledge_monthly.parquet", "plg_net3m"),
    "rating_chg90":("e99/sig_monthly.parquet", "rating_chg90"),
    "rev_net90":   ("e99/sig_monthly.parquet", "rev_net90"),
    "s1_eps_rev90": (None, "s1_eps_rev90"),  # V9 特征
    "bert_conf":   ("e100/sig_monthly.parquet", "bert_conf"),
}

# 快筛 10 对全集；已物化的两对保留同名文件
PAIRS = [
    ("fh_cnt", "turnover20", "sig_ix_fh_turnover.parquet"),
    ("fh_cnt", "fund_cov", "sig_ix_fh_fundcov.parquet"),
    ("disc_late", "ret120", "sig_ix_disc_late_ret120.parquet"),
    ("gdhs_chg", "circ_mv", "sig_ix_gdhs_circ_mv.parquet"),
    ("rating_chg90", "rev_net90", "sig_ix_rating_rev.parquet"),
    ("rating_chg90", "s1_eps_rev90", "sig_ix_rating_epsrev.parquet"),
    ("bert_conf", "turnover20", "sig_ix_bert_to.parquet"),
    ("plg_net3m", "circ_mv", "sig_ix_plg_mv.parquet"),
    ("fh_cnt_chg", "vol20", "sig_ix_fhchg_vol.parquet"),
    ("disc_late", "disc_resched", "sig_ix_disc_late_resched.parquet"),
]


def load_sig(name: str) -> pd.DataFrame:
    fn, col = SIG[name]
    d = pd.read_parquet(LAB / fn)
    d["code6"] = d["ts_code"].astype(str).str.extract(r"(\d{6})")
    dcol = "sig_date" if "sig_date" in d.columns else "date"
    d["period"] = pd.to_datetime(d[dcol]).dt.to_period("M")
    return d[["code6", "period", col]].dropna()


def zper(df: pd.DataFrame, col: str) -> pd.Series:
    v = pd.to_numeric(df[col], errors="coerce")
    mu = v.groupby(df["sig_date"]).transform("mean")
    sd = v.groupby(df["sig_date"]).transform("std").replace(0, np.nan)
    return ((v - mu) / sd).fillna(0)


def main() -> int:
    xt = pd.read_parquet(S.X_TRAIN)
    xs = pd.read_parquet(S.X_SCORE)
    x = pd.concat([xt, xs], ignore_index=True)
    x["code6"] = x["ts_code"].astype(str).str.extract(r"(\d{6})")
    x["period"] = pd.to_datetime(x["sig_date"]).dt.to_period("M")
    OUT.mkdir(parents=True, exist_ok=True)

    for wname, f, fname in PAIRS:
        out_path = OUT / fname
        ix = f"ix_{wname}_{f}"
        xw = x.merge(load_sig(wname).rename(columns={SIG[wname][1]: "w"}),
                   on=["code6", "period"], how="left")
        if f in x.columns:
            xw["f_"] = xw[f]
        else:
            xw = xw.merge(
                load_sig(f).rename(columns={SIG[f][1]: "f_"}),
                on=["code6", "period"], how="left")
        xw[ix] = zper(xw, "w") * zper(xw, "f_")
        res = xw[["ts_code", "code6", "period", ix]].dropna(subset=[ix])
        res = res[np.isfinite(res[ix])]
        res.to_parquet(out_path, index=False)
        print(f"{fname}: {len(res)} rows, cov={xw['w'].notna().mean():.3f}",
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
