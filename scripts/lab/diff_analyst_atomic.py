#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e86b 原子预测快照差分 → 修订事件表。

PREDICTDETAIL 每快照=每(股,机构)最新预测。相邻两日快照 diff：
同(SECUCODE, ORG_CODE, YEARk) 下 EPSk/NPk/RATING 变化即一次修订事件。
输出 data/analyst_atomic_events/<new_date>.parquet：
secucode, ts_code, org_code, researcher, year_k(1-4),
eps_prev, eps_new, eps_chg_pct, np_prev, np_new, rating_prev, rating_new,
prev_publish, new_publish, event_date(=new snapshot date)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SNAP = ROOT / "data" / "analyst_atomic_em"
OUT = ROOT / "data" / "analyst_atomic_events"


def _ts(secucode: str) -> str:
    code, mkt = secucode.split(".")
    p = {"SH": "sh", "SS": "sh", "XSHG": "sh",
         "SZ": "sz", "SZSE": "sz", "BJ": "bj"}.get(mkt, mkt.lower()[:2])
    return f"{p}.{code}"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    snaps = sorted(SNAP.glob("*.parquet"))
    if len(snaps) < 2:
        print("need >=2 snapshots")
        return 0
    old = pd.read_parquet(snaps[-2])
    new = pd.read_parquet(snaps[-1])
    key = ["SECUCODE", "ORG_CODE"]
    cols = ["SECUCODE", "ORG_CODE", "RESEARCHER", "PUBLISH_DATE",
            "YEAR1", "YEAR2", "EPS2", "EPS3",
            "PARENT_NETPROFIT2", "RATING"]
    o = old[[c for c in cols if c in old.columns]].set_index(key)
    n = new[[c for c in cols if c in new.columns]].set_index(key)
    j = n.join(o, how="inner", lsuffix="_new", rsuffix="_prev").reset_index()
    ev = j[(j["EPS2_new"].astype(float) != j["EPS2_prev"].astype(float))
           | (j["RATING_new"] != j["RATING_prev"])].copy()
    if ev.empty:
        print("no revisions")
        return 0
    e2p = ev["EPS2_prev"].astype(float)
    e2n = ev["EPS2_new"].astype(float)
    out = pd.DataFrame({
        "secucode": ev["SECUCODE"],
        "ts_code": ev["SECUCODE"].map(_ts),
        "org_code": ev["ORG_CODE"],
        "researcher": ev["RESEARCHER_new"],
        "eps2_prev": e2p, "eps2_new": e2n,
        "eps2_chg_pct": np.where(e2p.abs() > 1e-9,
                                 (e2n - e2p) / e2p.abs(), np.nan),
        "rating_prev": ev["RATING_prev"],
        "rating_new": ev["RATING_new"],
        "prev_publish": ev["PUBLISH_DATE_prev"],
        "new_publish": ev["PUBLISH_DATE_new"],
        "event_date": pd.Timestamp.now().normalize(),
    })
    fn = OUT / f"{pd.Timestamp.now():%Y%m%d}.parquet"
    out.to_parquet(fn, index=False)
    print(f"{fn} rows={len(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
