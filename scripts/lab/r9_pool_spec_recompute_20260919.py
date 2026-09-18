# -*- coding: utf-8 -*-
"""R9-A 本仓侧独立复算：候选池规格的年度成分数 + 重叠率 + 前瞻收益。

数据源（只读）：
  · R7 全 A daily_basic 分片（2431 交易日，2015-2024）
  · FinAI2.0 现池目录 data/dividend_stocks/（488 只）

口径声明：
  · 池成员 = 年末快照 dv_ratio（最近 12 月股息/总市值，%）过门槛；
  · contK = 连续 K 年（含当年）年末均过门槛；
  · 前瞻收益 = 等权买入持有次年（按收盘价，**不含分红再投**），
    另给出「价格收益 + 当年 dv_ratio 年化计息」的近似全收益口径；
  · 市值口径 total_mv（万元）用于加权对比。
"""
import json
from pathlib import Path

import pandas as pd

RAW = Path("/home/ubuntu/research-finai-latest/research-finai/.cluster/rd_r7_pool_20260917/work/daily_basic_raw")
POOL = Path("/home/ubuntu/FinAI2.0/data/dividend_stocks")
OUT = Path("/home/ubuntu/FinAI2.0/experiments/lab/r9-pool-spec")
OUT.mkdir(parents=True, exist_ok=True)

files = sorted(RAW.glob("*.parquet"))
by_year = {}
for f in files:
    by_year.setdefault(f.stem[:4], []).append(f.stem)
year_end = {y: max(ds) for y, ds in sorted(by_year.items())}

# 现池成员（目录名 = 市场.代码，如 sh.688061）
cur_pool = set()
for d in POOL.iterdir():
    if not d.is_dir() or "." not in d.name:
        continue
    mkt, code = d.name.split(".")
    if mkt in ("sh", "sz"):
        cur_pool.add(f"{code}.{mkt.upper()}")
print("现池成员数:", len(cur_pool))

COLS = ("ts_code", "dv_ratio", "close", "total_mv", "turnover_rate")


def load_day(day):
    df = pd.read_parquet(RAW / f"{day}.parquet")
    return df[[c for c in COLS if c in df.columns]].set_index("ts_code")


snap = {y: load_day(day) for y, day in year_end.items()}
for y in sorted(snap):
    d = snap[y]
    print(f"{y} ({year_end[y]}): n={len(d)} dv>=3={int((d['dv_ratio'] >= 3).sum())}")


def m_cur(y, thr=3.0):
    d = snap[y]
    return set(d.index[d["dv_ratio"].fillna(-1) >= thr])


def m_cont(y, k, thr=3.0):
    out = None
    for i in range(k):
        yy = str(int(y) - i)
        if yy not in snap:
            return set()
        s = m_cur(yy, thr)
        out = s if out is None else out & s
    return out or set()


SPECS = {
    "cur3": lambda y: m_cur(y, 3.0),
    "cur4": lambda y: m_cur(y, 4.0),
    "cur5": lambda y: m_cur(y, 5.0),
    "cont2": lambda y: m_cont(y, 2),
    "cont3": lambda y: m_cont(y, 3),
    "cont5": lambda y: m_cont(y, 5),
    "cur3_turn1": lambda y: {c for c in m_cur(y, 3.0)
                             if snap[y].loc[c, "turnover_rate"] >= 1.0},
    "cur3_mv100e4": lambda y: {c for c in m_cur(y, 3.0)
                               if snap[y].loc[c, "total_mv"] >= 1_000_000},
}

rows = []
for y in sorted(snap):
    y = int(y)
    for name, fn in SPECS.items():
        m = fn(str(y))
        rows.append({"year": y, "spec": name, "n": len(m),
                     "overlap_pool": len(m & cur_pool),
                     "overlap_pool_pct": round(100.0 * len(m & cur_pool) / max(1, len(m)), 2)})
tab = pd.DataFrame(rows)
piv = tab.pivot(index="year", columns="spec", values="n")
print()
print("== 各规格逐年成分数 ==")
print(piv.to_string())
print()
print("== 与现池重叠率(%) ==")
print(tab.pivot(index="year", columns="spec", values="overlap_pool_pct").to_string())
piv.to_csv(OUT / "spec_member_counts.csv", encoding="utf-8-sig")
tab.to_csv(OUT / "spec_member_counts_long.csv", encoding="utf-8-sig", index=False)

# ---- 前瞻收益：等权持有次年（价格口径 + 近似全收益口径）----
years = sorted(int(y) for y in snap)
ret_rows = []
for i, y in enumerate(years[:-1]):
    nxt = years[i + 1]
    d0 = snap[str(y)]["close"]
    d1 = snap[str(nxt)]["close"]
    for name, fn in SPECS.items():
        m = sorted(fn(str(y)))
        if not m:
            ret_rows.append({"year": y, "spec": name, "n": 0, "price_ret": None,
                             "tr_approx": None, "median_price_ret": None})
            continue
        px0 = d0.reindex(m)
        px1 = d1.reindex(m)
        valid = px0.notna() & px1.notna()
        if valid.sum() == 0:
            ret_rows.append({"year": y, "spec": name, "n": 0, "price_ret": None,
                             "tr_approx": None, "median_price_ret": None})
            continue
        r = (px1[valid] / px0[valid] - 1.0)
        dy = snap[str(y)]["dv_ratio"].reindex(m)[valid].fillna(0.0) / 100.0
        ret_rows.append({"year": y, "spec": name, "n": int(valid.sum()),
                         "price_ret": round(float(r.mean()), 4),
                         "tr_approx": round(float((r + dy).mean()), 4),
                         "median_price_ret": round(float(r.median()), 4)})

# 现池基准：对每日截面用现池成员近似（现池成员在各年末 dv_ratio>=3 的子集）
for i, y in enumerate(years[:-1]):
    nxt = years[i + 1]
    d0 = snap[str(y)]["close"]
    d1 = snap[str(nxt)]["close"]
    base = sorted(cur_pool & m_cur(str(y)))          # 现池中当年仍满足 dv>=3 的成员
    px0, px1 = d0.reindex(base), d1.reindex(base)
    valid = px0.notna() & px1.notna()
    r = (px1[valid] / px0[valid] - 1.0)
    dy = snap[str(y)]["dv_ratio"].reindex(base)[valid].fillna(0.0) / 100.0
    ret_rows.append({"year": y, "spec": "POOL_487_dv3_subset", "n": int(valid.sum()),
                     "price_ret": round(float(r.mean()), 4),
                     "tr_approx": round(float((r + dy).mean()), 4),
                     "median_price_ret": round(float(r.median()), 4)})

rt = pd.DataFrame(ret_rows)
rt.to_csv(OUT / "spec_forward_returns.csv", encoding="utf-8-sig", index=False)
agg = rt.groupby("spec").agg(
    years=("year", "count"),
    mean_price_ret=("price_ret", "mean"),
    mean_tr_approx=("tr_approx", "mean"),
).round(4)
print()
print("== 各规格前瞻 1 年等权收益（均值）==")
print(agg.to_string())

summary = {
    "spec_counts": piv.to_dict(),
    "forward_returns_mean": agg.to_dict(),
    "generated": "2026-09-19",
    "caveat": "price_ret 不含分红；tr_approx 用当年 dv_ratio 年化近似分红再投收益；"
              "等权、年初买入持有至年末快照，未扣除交易成本。",
}
(OUT / "spec_summary.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n产物:", OUT)
