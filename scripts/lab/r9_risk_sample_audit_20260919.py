# -*- coding: utf-8 -*-
"""R9 数据层风险样本与 ST 标识缺陷审计（复跑脚本，只读）。

包含三部分：
  ① 风险样本分桶：各年末 dv≥3 且存活票 → ST / 此后退市 / 正常 的前瞻收益画像；
  ② 现池风险样本覆盖：退市票、ST 名称票、dv≥7% 极端高息尾部覆盖；
  ③ isST 缺陷暴露度：现池 ST 名称票的 5% 档触板日 vs 真 ±10% 档触板日。

⚠ 口径声明（与文档一致）：等权前瞻影子口径、未扣成本、非 PIT ST、
票池为 2026-09 快照；结果不得用于准入/报备。
"""
import glob
from pathlib import Path

import pandas as pd

ROOT = Path("/home/ubuntu/FinAI2.0")
RAW = Path("/home/ubuntu/research-finai-latest/research-finai/.cluster/rd_r7_pool_20260917/work/daily_basic_raw")
OUT = ROOT / "experiments/lab/r9-pool-spec"
OUT.mkdir(parents=True, exist_ok=True)
CACHE = OUT / "shadow_px_cache.parquet"

YE = {"2015": "20151231", "2016": "20161230", "2017": "20171229", "2018": "20181228",
      "2019": "20191231", "2020": "20201231", "2021": "20211231", "2022": "20221230",
      "2023": "20231229", "2024": "20241231"}

sb = pd.read_parquet(ROOT / "data/stock_basic_cache.parquet")
stocks = sb[sb["type"].astype(str).str.strip() == "1"].copy()
stocks["code"] = stocks["code"].astype(str).str.strip()
stocks["name"] = stocks["code_name"].astype(str)
stocks["ipo"] = stocks["ipoDate"].astype(str).str[:10]
stocks["out"] = stocks["outDate"].astype(str).str[:10]
is_st = set(stocks[stocks["name"].str.upper().str.contains("ST")]["code"])
delisted = set(stocks[stocks["out"].str.len() >= 8]["code"])

pool = []
for d in (ROOT / "data/dividend_stocks").iterdir():
    if d.is_dir() and "." in d.name:
        mkt, code = d.name.split(".")
        if mkt in ("sh", "sz"):
            pool.append(f"{mkt}.{code}")
pool = sorted(pool)
poolset = set(pool)
print("股票表 %d 行｜ST 名称 %d｜已退市 %d｜现池 %d 只" % (len(stocks), len(is_st), len(delisted), len(pool)))


def alive_at(as_of):
    return set(stocks[(stocks["ipo"] <= as_of) &
                      ((stocks["out"].str.len() < 8) | (stocks["out"] > as_of))]["code"])


snaps = {}
for y, day in YE.items():
    df = pd.read_parquet(RAW / f"{day}.parquet")[["ts_code", "dv_ratio", "turnover_rate", "total_mv"]]
    df["code"] = df["ts_code"].str[-2:].str.lower() + "." + df["ts_code"].str[:6]
    snaps[y] = df.set_index("code")

# ---------- ① 风险样本分桶 ----------
pxdf = pd.read_parquet(CACHE)
lookup = {(c, d): v for c, d, v in zip(pxdf["code"], pxdf["date"], pxdf["close"])}
years = sorted(YE)
rows = []
for i, y in enumerate(years[:-1]):
    day0, day1 = YE[y], YE[years[i + 1]]
    d = snaps[y]
    alive = alive_at(f"{y}-12-31")
    for c in [c for c in d.index if d.at[c, "dv_ratio"] >= 3 and c in alive]:
        p0 = lookup.get((c, day0))
        if p0 is None:
            continue
        p1 = lookup.get((c, day1))
        if p1 is None:
            sub = pxdf[(pxdf["code"] == c) & (pxdf["date"] > day0)]
            if sub.empty:
                continue
            p1 = float(sub.sort_values("date")["close"].iloc[-1])
        bucket = "ST" if c in is_st else ("delisted" if c in delisted else "normal")
        rows.append({"year": y, "code": c, "bucket": bucket, "ret": p1 / p0 - 1.0,
                     "turnover": d.at[c, "turnover_rate"], "total_mv": d.at[c, "total_mv"],
                     "dv": d.at[c, "dv_ratio"]})
r = pd.DataFrame(rows)
agg = r.groupby("bucket").agg(n_points=("ret", "size"), mean_ret=("ret", "mean"),
                              median_ret=("ret", "median"),
                              turnover_median=("turnover", "median"),
                              mv_median_wan=("total_mv", "median"),
                              dv_median=("dv", "median")).round(4)
print("\n== ① 风险样本分桶（各年样本点合并）==")
print(agg.to_string())
agg.to_csv(OUT / "tradability_bucket_summary.csv", encoding="utf-8-sig")

# ---------- ② 现池风险样本覆盖 ----------
names = stocks.set_index("code")["name"]
pool_st = [c for c in pool if c in is_st]
print("\n== ② 现池覆盖 ==")
print("已退市 %d｜ST 名称 %d（%.1f%%；市场 %.1f%%）"
      % (len(poolset & delisted), len(pool_st), 100.0 * len(pool_st) / len(pool),
         100.0 * len(is_st) / len(stocks)))
cov = []
for y, day in sorted(YE.items()):
    d = snaps[y]
    hi = d[d["dv_ratio"] >= 7]
    cov.append({"year": int(y), "n_dv_ge7": len(hi),
                "in_pool": sum(1 for c in hi.index if c in poolset),
                "is_ST_now": sum(1 for c in hi.index if c in is_st),
                "later_delisted": sum(1 for c in hi.index if c in delisted)})
covdf = pd.DataFrame(cov)
print(covdf.to_string(index=False))
print("累计 dv≥7 样本 %d，进池 %d（%.1f%%）" % (covdf["n_dv_ge7"].sum(), covdf["in_pool"].sum(),
                                             100.0 * covdf["in_pool"].sum() / covdf["n_dv_ge7"].sum()))
covdf.to_csv(OUT / "risk_sample_coverage.csv", encoding="utf-8-sig", index=False)

# ---------- ③ isST 缺陷暴露度 ----------
print("\n== ③ isST 缺陷暴露度（现池 ST 名称票，pctChg 为比率口径）==")
rows3, tot5, tot10, tot_rows = [], 0, 0, 0
for sym in pool_st:
    fs = sorted(glob.glob(str(ROOT / f"data/dividend_stocks/{sym}/*.parquet")))
    if not fs:
        continue
    d = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    if "isST" in d.columns:
        uniq = sorted(set(d["isST"].astype(str)))
    else:
        uniq = ["<缺列>"]
    p = pd.to_numeric(d["pctChg"], errors="coerce").abs()
    n5 = int(((p >= 0.048) & (p <= 0.052)).sum())
    n10 = int((p >= 0.098).sum())
    tot5 += n5
    tot10 += n10
    tot_rows += len(d)
    rows3.append({"symbol": sym, "rows": len(d), "isST_values": ",".join(uniq),
                  "days_5pct_band": n5, "days_ge_9p8pct": n10})
t3 = pd.DataFrame(rows3)
print(t3.head(20).to_string(index=False))
print("合计 %d 行；5%% 档（未标记）%d 日；真 ±10%% 档 %d 日" % (tot_rows, tot5, tot10))
t3.to_csv(OUT / "st_flag_exposure.csv", encoding="utf-8-sig", index=False)
print("\n产物目录:", OUT)