#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""POOL-2：PEAD 信号面预研（只读统计，不开策略代码）

产出（experiments/lab/）：
  1. pead_event_counts.csv     年度触发次数（归母净利同比 >= 30% 的公告事件）
  2. pead_post_event_ret.csv   触发后 5/10/20 个交易日平均超额收益（有价格数据时）
数据底座：data/financial_pit/（487只×16891行、pub_date 零前视、归母口径）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "lab"
OUT.mkdir(parents=True, exist_ok=True)

def main() -> int:
    import pandas as pd

    fin_dir = ROOT / "data" / "financial_pit"
    files = sorted(fin_dir.glob("*.parquet")) or sorted(fin_dir.glob("*.csv"))
    if not files:
        print(f"SKIP: 未找到财务数据文件于 {fin_dir}")
        return 0

    frames = []
    for f in files:
        df = pd.read_parquet(f) if f.suffix == ".parquet" else pd.read_csv(f)
        frames.append(df)
    fin = pd.concat(frames, ignore_index=True)
    print(f"财务数据: {len(fin)} 行, 列={list(fin.columns)[:12]}")

    colmap = {c.lower(): c for c in fin.columns}
    pub = next((colmap[c] for c in colmap if "pub" in c and "date" in c), None)
    yoy = next((colmap[c] for c in colmap if "yoy" in c and "profit" in c), None) \
        or next((colmap[c] for c in colmap if "yoy" in c), None)
    code = next((colmap[c] for c in colmap if c in ("code", "symbol", "ts_code", "stock")), None)
    if not (pub and yoy and code):
        print(f"SKIP: 关键列缺失 pub={pub} yoy={yoy} code={code}")
        return 0

    fin[pub] = pd.to_datetime(fin[pub], errors="coerce")
    fin[yoy] = pd.to_numeric(fin[yoy], errors="coerce")
    fin = fin.dropna(subset=[pub, yoy])

    events = fin[fin[yoy] >= 30.0].copy()
    events["year"] = events[pub].dt.year
    counts = events.groupby("year").size().rename("event_count").reset_index()
    counts.to_csv(OUT / "pead_event_counts.csv", index=False)
    print("年度触发次数:")
    print(counts.to_string(index=False))

    # 超额收益段：尝试加载价格数据，不可用则明确降级
    price_files = sorted((ROOT / "data").glob("**/*daily*.parquet"))[:1]
    if not price_files:
        print("NOTE: 未找到日线价格数据，触发后超额收益段本次跳过（仅完成事件统计）")
        (OUT / "pead_post_event_ret.csv").write_text(
            "# 价格数据不可用，本段留待补齐日线后重跑\n", encoding="utf-8")
        return 0

    px = pd.read_parquet(price_files[0])
    pcols = {c.lower(): c for c in px.columns}
    pdate = next((pcols[c] for c in pcols if "date" in c), None)
    pclose = next((pcols[c] for c in pcols if c in ("close", "close_qfq", "adj_close")), None)
    pcode = next((pcols[c] for c in pcols if c in ("code", "symbol", "ts_code", "stock")), None)
    if not (pdate and pclose and pcode):
        print("NOTE: 价格数据列无法识别，超额收益段跳过")
        return 0
    px[pdate] = pd.to_datetime(px[pdate], errors="coerce")
    px = px.sort_values([pcode, pdate])
    rets = {}
    for horizon in (5, 10, 20):
        acc = []
        for cd, grp in px.groupby(pcode):
            g = grp.reset_index(drop=True)
            ev_dates = events.loc[events[code] == cd, pub].tolist()
            if not ev_dates:
                continue
            idx = {d: i for i, d in enumerate(g[pdate])}
            for d in ev_dates:
                cand = g.index[g[pdate] >= d]
                if len(cand) == 0:
                    continue
                i0 = cand[0]
                i1 = i0 + horizon
                if i1 >= len(g):
                    continue
                acc.append(float(g[pclose].iloc[i1]) / float(g[pclose].iloc[i0]) - 1.0)
        rets[horizon] = acc
    rows = [
        {"horizon_days": h, "n": len(v), "avg_ret": (sum(v) / len(v)) if v else None}
        for h, v in rets.items()
    ]
    pd.DataFrame(rows).to_csv(OUT / "pead_post_event_ret.csv", index=False)
    print("触发后平均收益（未扣基准）:")
    print(pd.DataFrame(rows).to_string(index=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
