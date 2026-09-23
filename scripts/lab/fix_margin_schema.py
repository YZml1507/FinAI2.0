"""margin_detail 2025+ 中文原始列 → 旧 canonical schema。

旧 schema: ts_code, sec_name, mkt, fin_balance, fin_buy, fin_repay,
           short_qty, short_sell, short_repay
sse 行: 标的证券代码/融资余额/融资买入额/融资偿还额/融券余量/融券卖出量/融券偿还量
szse 行: 证券代码/融资余额/融资买入额/-/融券余量/融券卖出量/-
        (深交所不披露偿还额, 置 NaN)
幂等: 已是 canonical 的文件跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_root = Path(__file__).resolve().parents[2]
D = _root / "data" / "margin_detail"
CANON = ["ts_code", "sec_name", "mkt", "fin_balance", "fin_buy",
         "fin_repay", "short_qty", "short_sell", "short_repay"]


def convert(fp: Path) -> bool:
    df = pd.read_parquet(fp)
    if "ts_code" in df.columns:
        return False
    rows = []
    for src, code_c, name_c, fin_r, short_r in (
            ("sse", "标的证券代码", "标的证券简称", "融资偿还额",
             "融券偿还量"),
            ("szse", "证券代码", "证券简称", None, None)):
        sub = df[df["_src"] == src]
        if sub.empty:
            continue
        suffix = ".SH" if src == "sse" else ".SZ"
        rows.append(pd.DataFrame({
            "ts_code": sub[code_c].astype(str) + suffix,
            "sec_name": sub[name_c].astype(str),
            "mkt": "SH" if src == "sse" else "SZ",
            "fin_balance": pd.to_numeric(sub["融资余额"],
                                         errors="coerce"),
            "fin_buy": pd.to_numeric(sub["融资买入额"],
                                     errors="coerce"),
            "fin_repay": (pd.to_numeric(sub[fin_r], errors="coerce")
                          if fin_r else pd.NA),
            "short_qty": pd.to_numeric(sub["融券余量"],
                                       errors="coerce"),
            "short_sell": pd.to_numeric(sub["融券卖出量"],
                                        errors="coerce"),
            "short_repay": (pd.to_numeric(sub[short_r], errors="coerce")
                            if short_r else pd.NA),
        }))
    out = pd.concat(rows, ignore_index=True)[CANON]
    out.to_parquet(fp, index=False)
    return True


def main() -> int:
    fs = sorted(D.glob("20*.parquet"))
    n = 0
    for fp in fs:
        try:
            if convert(fp):
                n += 1
        except Exception as exc:
            print(f"{fp.name} FAIL {exc}", flush=True)
    print(f"DONE converted={n}/{len(fs)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
