"""补齐 daily_bars 2025-01→2026-09 缺口（1472 只未续 + 21 只半续）。

与既有分片同构：{sym}/{year}.parquet，列
  date,open,high,low,close,preclose,volume,amount,turn,pctChg,
  tradestatus,isST,code,source='baostock',adjust_mode='RAW'
幂等：2026.parquet 存在且 max(date)>=end-2 则跳过。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import baostock as bs
import pandas as pd

_root = Path(__file__).resolve().parents[2]
BARS = _root / "data" / "daily_bars"
FIELDS = ("date,open,high,low,close,preclose,volume,amount,turn,"
          "pctChg,tradestatus,isST")
COLS = ["date", "open", "high", "low", "close", "preclose", "volume",
        "amount", "turn", "pctChg", "tradestatus", "isST", "code",
        "source", "adjust_mode"]


def _needs(symdir: Path, end: str) -> bool:
    f26 = symdir / "2026.parquet"
    if not f26.exists():
        return True
    try:
        mx = pd.read_parquet(f26, columns=["date"])["date"].max()
        # 容 2 天：末交易日 vs --end 之间可能有周末/假期
        watermark = str(pd.Timestamp(end) - pd.Timedelta(days=2))[:10]
        return str(mx) < watermark
    except Exception:
        return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-09-22")
    ap.add_argument("--sleep", type=float, default=0.15)
    args = ap.parse_args()

    syms = sorted(d for d in BARS.iterdir()
                  if d.is_dir() and d.name.startswith(("sh.", "sz.")))
    todo = [d for d in syms if _needs(d, args.end)]
    print(f"total={len(syms)} todo={len(todo)}", flush=True)
    bs.login()
    done = 0
    for i, symdir in enumerate(todo):
        code = symdir.name
        try:
            rs = bs.query_history_k_data_plus(
                code, FIELDS, start_date=args.start, end_date=args.end,
                frequency="d", adjustflag="3")
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            df = pd.DataFrame(rows, columns=rs.fields)
            if df.empty:
                continue
            for c in ["open", "high", "low", "close", "preclose",
                      "volume", "amount", "turn", "pctChg"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df["volume"] = df["volume"].fillna(0).astype("int64")
            df["code"] = code
            df["source"] = "baostock"
            df["adjust_mode"] = "RAW"
            df["tradestatus"] = df["tradestatus"].astype(str)
            df["isST"] = df["isST"].astype(str)
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df = df[COLS].sort_values("date", kind="stable").reset_index(drop=True)
            # D-1 豁免列：停牌占位行(tradestatus==0)→复牌行(tradestatus==1)
            # 的首个交易日记 is_resumption=True（复牌结构性跳变属真实行情）
            _ts = df["tradestatus"].fillna("1").astype(int)
            df["is_resumption"] = (_ts == 1) & (_ts.shift(1) == 0)
            df["is_resumption"].iloc[0] = False
            for yr, g in df.groupby(pd.to_datetime(df["date"]).dt.year):
                fp = symdir / f"{yr}.parquet"
                if fp.exists():
                    old = pd.read_parquet(fp)
                    g = (pd.concat([old[~old["date"].isin(g["date"])], g])
                           .sort_values("date", kind="stable"))
                tmp = fp.with_suffix(".parquet.tmp")
                g.reset_index(drop=True).to_parquet(tmp, index=False)
                os.replace(tmp, fp)
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(todo)} @{code}", flush=True)
            time.sleep(args.sleep)
        except Exception as exc:
            print(f"  {code} FAIL {type(exc).__name__}: {exc}", flush=True)
    bs.logout()
    print(f"DONE {done}/{len(todo)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
