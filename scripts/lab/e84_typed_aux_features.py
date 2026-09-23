"""scripts/lab/e84_typed_aux_features.py —— 公告分类型密度特征。

对 notice_meta 的 公告类型 白名单六类各建 60 自然日滚动计数
（PIT：公告日期 ≤ sig_date）：
  cnt_research60  调研活动（机构关注度代理）
  cnt_related60   关联交易
  cnt_divplan60   分配预案
  cnt_guar60      提供/对外担保公告
  cnt_exec60      高管人员任职变动
  cnt_pledge60    股份质押、冻结
产出：experiments/lab/e83/typed_features.parquet
      (sig_date, ts_code, cnt_*60×6)
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments/lab/e83"

TYPES = {
    "调研活动": "cnt_research60",
    "关联交易": "cnt_related60",
    "分配预案": "cnt_divplan60",
    "提供/对外担保公告": "cnt_guar60",
    "高管人员任职变动": "cnt_exec60",
    "股份质押、冻结": "cnt_pledge60",
}


def _code_to_ts(code: str) -> str:
    code = str(code).strip()
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from scripts.lab.e83_build_aux_features import load_sig_dates
    sig_dates = load_sig_dates()
    rows = []
    for f in sorted(glob.glob(str(ROOT / "data/notice_meta/*.parquet"))):
        if not Path(f).stem.isdigit():
            continue
        d = pd.read_parquet(f, columns=["代码", "公告类型", "公告日期"])
        d = d[d["公告日期"].notna() & d["公告类型"].isin(TYPES)]
        rows.append(d)
    ann = pd.concat(rows, ignore_index=True)
    ann["ts_code"] = ann["代码"].map(_code_to_ts)
    ann["adate"] = pd.to_datetime(ann["公告日期"], errors="coerce")
    ann = ann[ann["adate"].notna()][["ts_code", "adate", "公告类型"]]
    out_rows = []
    for tname, col in TYPES.items():
        sub = ann[ann["公告类型"] == tname]
        counts = {c: np.sort(v.values) for c, v in
                  sub.groupby("ts_code")["adate"]}
        for sd in pd.DatetimeIndex(sig_dates):
            lo = np.datetime64(sd - pd.Timedelta(days=60))
            hi = np.datetime64(sd)
            for code, arr in counts.items():
                n = int(np.searchsorted(arr, hi, "right")
                        - np.searchsorted(arr, lo, "right"))
                if n:
                    out_rows.append((sd, code, col, n))
    long = pd.DataFrame(out_rows, columns=["sig_date", "ts_code",
                                           "col", "n"])
    wide = long.pivot_table(index=["sig_date", "ts_code"],
                            columns="col", values="n",
                            aggfunc="sum").reset_index()
    wide.columns.name = None
    OUT.mkdir(parents=True, exist_ok=True)
    wide.to_parquet(OUT / "typed_features.parquet", index=False)
    print(f"-> {OUT/'typed_features.parquet'} rows={len(wide)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
