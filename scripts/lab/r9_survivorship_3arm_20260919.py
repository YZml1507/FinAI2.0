# -*- coding: utf-8 -*-
"""幸存者偏差三臂对照（同选股规则，隔离生存条件化）。

三臂（同一规则：当时存活 & dv_ratio>=3 → 股息率降序取前 8 只，等权持有 1 年）：
  A_allA      = 候选集不限（含后来退市）           → 无生存条件化
  B_survive   = 候选集仅限「至今未退市」票          → 生存条件化（偏差本体）
  C_pool      = 候选集仅限现池 488 成员（回测实际可持有）→ 当前系统口径
A−B = 纯幸存者偏差量级；A−C = 池覆盖缺口；C−B = 现池相对「存活全 A」的净影响。
退市/缺失票按最后一根可用收盘价平仓（保守中性假设）。
"""
from pathlib import Path

import pandas as pd

ROOT = Path("/home/ubuntu/FinAI2.0")
RAW = Path("/home/ubuntu/research-finai-latest/research-finai/.cluster/rd_r7_pool_20260917/work/daily_basic_raw")
OUT = ROOT / "experiments/lab/r9-pool-spec"
OUT.mkdir(parents=True, exist_ok=True)
PX_CACHE = OUT / "shadow_px_cache.parquet"

YE = {"2015": "20151231", "2016": "20161230", "2017": "20171229", "2018": "20181228",
      "2019": "20191231", "2020": "20201231", "2021": "20211231", "2022": "20221230",
      "2023": "20231229", "2024": "20241231"}

sb = pd.read_parquet(ROOT / "data/stock_basic_cache.parquet")
stocks = sb[sb["type"].astype(str).str.strip() == "1"].copy()
stocks["code"] = stocks["code"].astype(str).str.strip()
stocks["ipo"] = stocks["ipoDate"].astype(str).str[:10]
stocks["out"] = stocks["outDate"].astype(str).str[:10]
survivors = set(stocks[stocks["out"].str.len() < 8]["code"])
pool = set()
for d in (ROOT / "data/dividend_stocks").iterdir():
    if d.is_dir() and "." in d.name:
        mkt, code = d.name.split(".")
        if mkt in ("sh", "sz"):
            pool.add(f"{mkt}.{code}")


def alive_at(as_of):
    return set(stocks[(stocks["ipo"] <= as_of) &
                      ((stocks["out"].str.len() < 8) | (stocks["out"] > as_of))]["code"])


dvmap = {}
for y, day in YE.items():
    df = pd.read_parquet(RAW / f"{day}.parquet")[["ts_code", "dv_ratio"]]
    df["code"] = df["ts_code"].str[-2:].str.lower() + "." + df["ts_code"].str[:6]
    dvmap[y] = df.set_index("code")["dv_ratio"]

years = sorted(YE)
sel_rows = []
for i, y in enumerate(years[:-1]):
    dv = dvmap[y]
    a_alive = alive_at(f"{y}-12-31")
    cand = [c for c in dv.index if dv[c] >= 3 and c in a_alive]
    cand.sort(key=lambda c: -dv[c])
    topA = cand[:8]
    topB = [c for c in cand if c in survivors][:8]
    topC = [c for c in cand if c in pool][:8]
    sel_rows.append({"year": y, "n_cand": len(cand),
                     "n_cand_surv": sum(1 for c in cand if c in survivors),
                     "topA": "|".join(topA), "topB": "|".join(topB), "topC": "|".join(topC),
                     "A_surv_diff": len(set(topA) - set(topB))})
sel = pd.DataFrame(sel_rows)
print("== 三臂选取 ==")
print(sel[["year", "n_cand", "n_cand_surv", "A_surv_diff"]].to_string(index=False))

targets = set()
for col in ("topA", "topB", "topC"):
    for v in sel[col]:
        targets |= set(v.split("|"))

# 价格扫描（缓存复用）
if PX_CACHE.exists():
    pxdf = pd.read_parquet(PX_CACHE)
    print("复用价格缓存:", len(pxdf))
else:
    parts = []
    files = sorted(RAW.glob("*.parquet"))
    for k, f in enumerate(files):
        df = pd.read_parquet(f)[["ts_code", "close"]]
        df["code"] = df["ts_code"].str[-2:].str.lower() + "." + df["ts_code"].str[:6]
        sub = df[df["code"].isin(targets) & df["close"].notna()]
        if len(sub):
            parts.append(pd.DataFrame({"code": sub["code"], "date": f.stem,
                                       "close": sub["close"].astype(float)}))
        if k % 800 == 0:
            print("  扫描", k, "/", len(files), flush=True)
    pxdf = pd.concat(parts, ignore_index=True)
    pxdf.to_parquet(PX_CACHE)
    print("价格缓存已落盘:", len(pxdf))
first = pxdf.sort_values(["code", "date"]).groupby("code").first()
last = pxdf.sort_values(["code", "date"]).groupby("code").last()
lookup = {(c, d): v for c, d, v in zip(pxdf["code"], pxdf["date"], pxdf["close"])}

rows = []
for i, y in enumerate(years[:-1]):
    nxt = years[i + 1]
    day0, day1 = YE[y], YE[nxt]
    rec = sel.iloc[i]
    for arm, key in (("A_allA", "topA"), ("B_survive", "topB"), ("C_pool", "topC")):
        syms = [s for s in rec[key].split("|") if s]
        rets, cens = [], 0
        for c in syms:
            p0 = lookup.get((c, day0))
            if p0 is None:
                continue
            p1 = lookup.get((c, day1))
            if p1 is None:
                # 取 day0 后最后一根可用价（退市/长期停牌）
                sub = pxdf[(pxdf["code"] == c) & (pxdf["date"] > day0)]
                if sub.empty:
                    continue
                p1 = float(sub.sort_values("date")["close"].iloc[-1])
                cens += 1
            rets.append(p1 / p0 - 1.0)
        rows.append({"year": y, "arm": arm, "n": len(rets), "censored": cens,
                     "mean_ret": round(sum(rets) / len(rets), 4) if rets else None})

res = pd.DataFrame(rows)
piv = res.pivot(index="year", columns="arm", values="mean_ret")
piv["A_minus_B"] = piv["A_allA"] - piv["B_survive"]
piv["A_minus_C"] = piv["A_allA"] - piv["C_pool"]
piv["C_minus_B"] = piv["C_pool"] - piv["B_survive"]
print()
print("== 前瞻 1 年等权收益（三臂）==")
print((piv * 100).round(2).to_string())
stats = {
    "A_allA_mean_pct": round(float(piv["A_allA"].mean()) * 100, 2),
    "B_survive_mean_pct": round(float(piv["B_survive"].mean()) * 100, 2),
    "C_pool_mean_pct": round(float(piv["C_pool"].mean()) * 100, 2),
    "A_minus_B_pp": round(float(piv["A_minus_B"].mean()) * 100, 2),
    "A_minus_C_pp": round(float(piv["A_minus_C"].mean()) * 100, 2),
    "C_minus_B_pp": round(float(piv["C_minus_B"].mean()) * 100, 2),
    "n_years": len(piv),
}
print()
print("== 全期均值 ==")
for k, v in stats.items():
    print(f"  {k}: {v}")
res.to_csv(OUT / "survivorship_three_arm.csv", encoding="utf-8-sig", index=False)
sel.to_csv(OUT / "survivorship_three_arm_selections.csv", encoding="utf-8-sig", index=False)
import json
(OUT / "survivorship_three_arm_summary.json").write_text(
    json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
print("产物:", OUT)