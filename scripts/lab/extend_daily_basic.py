"""重建 daily_basic 2025+（tushare 断供 → baostock+本地组装）。

字段对齐 data/daily_basic_alla/{YYYYMMDD}.parquet:
  ts_code, trade_date, close, dv_ratio, dv_ttm, total_mv, circ_mv,
  free_share, turnover_rate, pe, pb

来源:
  pe/pb        ← baostock k-data peTTM/pbMRQ（每股一次查询覆盖全区间）
  close/turn   ← 本地 data/daily_bars/{sym}/{year}.parquet（turn=换手率%）
  circ_mv      ← volume/(turn/100) × close / 1e4   （万元，同 tushare 口径）
  free_share   ← volume/(turn/100) / 1e4            （万股）
  total_mv     ← NaN（baostock 无总股本；e63 特征不引用，留 NaN 明示）
  dv_ttm       ← data/daily_bars/exdiv/{sym}.parquet 的 cash_dividend
                 按除权日滚动 365 天求和 / close ×100（%口径近似）
  dv_ratio     ← 同 dv_ttm（年度股息率近似，登记为近似口径）

水位幂等: 分片已存在跳过。
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
OUT_DIR = _root / "data" / "daily_basic_alla"
BARS = _root / "data" / "daily_bars"
EXDIV = BARS / "exdiv"
FIELDS = "date,close,peTTM,pbMRQ,turn"


def _ts_code(dirname: str) -> str:
    ex, num = dirname.split(".")
    return f"{num}.{ex.upper()}"


def _load_bars(symdir: Path) -> pd.DataFrame:
    frames = [pd.read_parquet(f, columns=["date", "close", "volume", "turn"])
              for f in sorted(symdir.glob("*.parquet"))]
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _dv_ttm(exdir_sym: Path, days: pd.DatetimeIndex,
            closes: pd.Series) -> pd.Series:
    out = pd.Series(0.0, index=days)
    if exdir_sym.exists():
        ev = pd.read_parquet(exdir_sym)
        ev["date"] = pd.to_datetime(ev["date"])
        for _, r in ev.iterrows():
            mask = (days >= r["date"]) & (days < r["date"] + pd.Timedelta(days=365))
            out.loc[mask] += float(r["cash_dividend"])
    return (out / closes.reindex(days).ffill() * 100).where(
        closes.reindex(days).notna())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-09-22")
    ap.add_argument("--sleep", type=float, default=0.15)
    ap.add_argument("--only", type=Path, default=None,
                    help="文件清单：每行一个 sh./sz. 符号目录名，只跑这些")
    args = ap.parse_args()

    bs.login()
    if args.only:
        syms = [BARS / s.strip() for s in args.only.read_text().split()
                if s.strip() and (BARS / s.strip()).is_dir()]
    else:
        syms = sorted(d for d in BARS.iterdir()
                      if d.is_dir() and d.name.startswith(("sh.", "sz.")))
    print(f"symbols={len(syms)}", flush=True)
    done = 0
    for symdir in syms:
        code = symdir.name
        ts = _ts_code(code)
        try:
            bars = _load_bars(symdir)
            bars = bars[(bars["date"] >= args.start) &
                        (bars["date"] <= args.end)]
            if bars.empty:
                continue
            rs = bs.query_history_k_data_plus(
                code, FIELDS, start_date=args.start, end_date=args.end,
                frequency="d", adjustflag="3")
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            k = pd.DataFrame(rows, columns=rs.fields)
            if k.empty:
                continue
            k["date"] = pd.to_datetime(k["date"])
            m = bars.merge(k[["date", "peTTM", "pbMRQ"]], on="date", how="left")
            turn = pd.to_numeric(m["turn"], errors="coerce")
            vol = pd.to_numeric(m["volume"], errors="coerce")
            close = pd.to_numeric(m["close"], errors="coerce")
            fshare = vol / (turn / 100).replace(0, pd.NA)
            exf = EXDIV / f"{code}.parquet"
            dv = _dv_ttm(exf, pd.DatetimeIndex(m["date"]),
                         close).reset_index(drop=True)
            out = pd.DataFrame({
                "ts_code": ts,
                "trade_date": m["date"].dt.strftime("%Y%m%d"),
                "close": close,
                "dv_ratio": dv.values,
                "dv_ttm": dv.values,
                "total_mv": pd.NA,
                "circ_mv": (fshare * close / 1e4).values,
                "free_share": (fshare / 1e4).values,
                "turnover_rate": turn.values,
                "pe": pd.to_numeric(m["peTTM"], errors="coerce"),
                "pb": pd.to_numeric(m["pbMRQ"], errors="coerce"),
            })
            for d, g in out.groupby("trade_date"):
                fp = OUT_DIR / f"{d}.parquet"
                if fp.exists():
                    old = pd.read_parquet(fp)
                    g = (pd.concat([old[~old["ts_code"].isin(g["ts_code"])], g])
                           .sort_values("ts_code", kind="stable"))
                tmp = fp.with_suffix(".parquet.tmp")
                g.reset_index(drop=True).to_parquet(tmp, index=False)
                os.replace(tmp, fp)
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(syms)}", flush=True)
            time.sleep(args.sleep)
        except Exception as exc:
            print(f"  {code} FAIL {type(exc).__name__}: {exc}", flush=True)
    bs.logout()
    print(f"DONE {done}/{len(syms)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
