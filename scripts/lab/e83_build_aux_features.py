"""scripts/lab/e83_build_aux_features.py —— E83 v5/v6 辅助特征构建。

ann_cnt60: 截至 sig_date 前 60 个自然日内的公告条数（data/notice_meta，
           公告日期 PIT-safe）。
gdhs_qoq:  最新公告日 ≤ sig_date 的「股东户数-增减比例」(data/gdhs 季度文件)。

产出：experiments/lab/e83/aux_features.parquet
      (ts_code, sig_date, ann_cnt60, gdhs_qoq)
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments/lab/e83"


def _code_to_ts(code: str) -> str:
    code = str(code).strip()
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def load_sig_dates() -> np.ndarray:
    xs = []
    for p in ["experiments/lab/e63_Xlab4.parquet",
              "experiments/lab/e63_Xlab_2025.parquet"]:
        xs.append(pd.read_parquet(ROOT / p, columns=["sig_date"]))
    s = pd.concat(xs)["sig_date"]
    return np.array(sorted(s.unique()))


def build_ann_cnt60(sig_dates: np.ndarray) -> pd.DataFrame:
    rows = []
    for f in sorted(glob.glob(str(ROOT / "data/notice_meta/*.parquet"))):
        d = pd.read_parquet(f, columns=["代码", "公告日期"])
        d = d[d["公告日期"].notna()]
        rows.append(d)
    ann = pd.concat(rows, ignore_index=True)
    ann["ts_code"] = ann["代码"].map(_code_to_ts)
    ann["adate"] = pd.to_datetime(ann["公告日期"], errors="coerce")
    ann = ann[ann["adate"].notna()][["ts_code", "adate"]]
    counts = {c: np.sort(v.values) for c, v in
              ann.groupby("ts_code")["adate"]}
    out = []
    for sd in pd.DatetimeIndex(sig_dates):
        lo = np.datetime64(sd - pd.Timedelta(days=60))
        hi = np.datetime64(sd)
        for code, arr in counts.items():
            n = int(np.searchsorted(arr, hi, "right")
                    - np.searchsorted(arr, lo, "right"))
            if n:
                out.append((sd, code, n))
    return pd.DataFrame(out, columns=["sig_date", "ts_code", "ann_cnt60"])


def build_gdhs_qoq(sig_dates: np.ndarray) -> pd.DataFrame:
    rows = []
    for f in sorted(glob.glob(str(ROOT / "data/gdhs/*.parquet"))):
        d = pd.read_parquet(f, columns=["代码", "股东户数-增减比例", "公告日期"])
        d = d[d["公告日期"].notna()]
        rows.append(d)
    g = pd.concat(rows, ignore_index=True)
    g["ts_code"] = g["代码"].map(_code_to_ts)
    g["adate"] = pd.to_datetime(g["公告日期"], errors="coerce")
    g["qoq"] = pd.to_numeric(g["股东户数-增减比例"], errors="coerce")
    g = g[g["adate"].notna() & g["qoq"].notna()]
    g = g.sort_values("adate")[["ts_code", "adate", "qoq"]]
    recs = {c: (v["adate"].values, v["qoq"].values)
            for c, v in g.groupby("ts_code")}
    out = []
    for sd in pd.DatetimeIndex(sig_dates):
        t = np.datetime64(sd)
        for code, (dates, vals) in recs.items():
            i = int(np.searchsorted(dates, t, "right")) - 1
            if i >= 0:
                out.append((sd, code, float(vals[i])))
    return pd.DataFrame(out, columns=["sig_date", "ts_code", "gdhs_qoq"])


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sig_dates = load_sig_dates()
    print(f"sig_dates={len(sig_dates)}")
    a = build_ann_cnt60(sig_dates)
    print(f"ann_cnt60 rows={len(a)}")
    g = build_gdhs_qoq(sig_dates)
    print(f"gdhs_qoq rows={len(g)}")
    keys = a.merge(g, on=["sig_date", "ts_code"], how="outer")
    keys.to_parquet(OUT / "aux_features.parquet", index=False)
    print(f"-> {OUT/'aux_features.parquet'} rows={len(keys)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
