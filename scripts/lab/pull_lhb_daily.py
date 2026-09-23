"""scripts/lab/pull_lhb_daily.py —— 龙虎榜明细日增量续采。

源：akshare ``stock_lhb_detail_em``（东财）。落盘与既有分片同构：
``data/lhb/{yyyymmdd}.parquet``，列
  trade_date,ts_code,name,close,pct_change,amount,net_buy,
  buy_amount,sell_amount,reason,source,retrieved_at

幂等：已存在的日期文件跳过；--start 缺省 = 最新分片次日。
用途：e37 veto V2（10 日 ≥2 次上榜）与日历延伸的新鲜度来源，
daily_ops 编排内 ``lhb`` 步调用。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import akshare as ak
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
LHB = ROOT / "data" / "lhb"
CODE_MAP = {"000": "SZ", "001": "SZ", "002": "SZ", "003": "SZ",
            "300": "SZ", "301": "SZ",
            "600": "SH", "601": "SH", "603": "SH", "605": "SH",
            "688": "SH", "689": "SH",
            "430": "BJ", "831": "BJ", "832": "BJ", "833": "BJ",
            "834": "BJ", "835": "BJ", "836": "BJ", "837": "BJ",
            "838": "BJ", "839": "BJ", "870": "BJ", "871": "BJ",
            "872": "BJ", "873": "BJ", "920": "BJ"}


def _ts(code: str) -> str:
    c = str(code).zfill(6)
    return f"{c}.{CODE_MAP.get(c[:3], 'SZ' if c[0] in '03' else 'SH')}"


def _latest_file_date() -> str | None:
    files = sorted(LHB.glob("*.parquet"))
    return files[-1].stem if files else None


def _trade_days(start: str, end: str) -> list[str]:
    """交易日历：复用 panel_close 的索引；无数据时退化为工作日。"""
    try:
        panel = ROOT / "experiments" / "lab" / "e27_gdhs" / "panel_close.parquet"
        idx = pd.read_parquet(panel, columns=["sh.600519"]).index
    except Exception:  # noqa: BLE001
        idx = pd.bdate_range(start, end)
    days = [d.strftime("%Y%m%d") for d in idx
            if start <= d.strftime("%Y%m%d") <= end]
    return days


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None,
                    help="YYYYMMDD；缺省 = 最新分片次日")
    ap.add_argument("--end", default=pd.Timestamp.today().strftime("%Y%m%d"))
    ap.add_argument("--sleep", type=float, default=0.4)
    args = ap.parse_args()

    latest = _latest_file_date()
    start = args.start
    if start is None:
        if latest is None:
            print("no existing shards; pass --start")
            return 2
        start = (pd.Timestamp(latest) + pd.Timedelta(days=1)
                 ).strftime("%Y%m%d")
    if start > args.end:
        print(f"up to date (latest={latest} >= end={args.end})")
        return 0

    days = _trade_days(start, args.end)
    have = {f.stem for f in LHB.glob("*.parquet")}
    have |= {f.stem for f in LHB.glob("*.empty")}
    todo = [d for d in days if d not in have]
    print(f"latest={latest} start={start} end={args.end} "
          f"trade_days={len(days)} todo={len(todo)}", flush=True)
    n_ok = 0
    for d in todo:
        try:
            df = ak.stock_lhb_detail_em(start_date=d, end_date=d)
        except Exception as e:  # noqa: BLE001
            print(f"{d}: api fail {e}", flush=True)
            continue
        time.sleep(args.sleep)
        if df is None or df.empty:
            (LHB / f"{d}.empty").write_text("")  # 空日标记防重拉
            continue
        out = pd.DataFrame({
            "trade_date": f"{d[:4]}-{d[4:6]}-{d[6:]}",
            "ts_code": df["代码"].astype(str).str.zfill(6).map(_ts),
            "name": df["名称"],
            "close": pd.to_numeric(df["收盘价"], errors="coerce"),
            "pct_change": pd.to_numeric(df["涨跌幅"], errors="coerce"),
            "amount": pd.to_numeric(df["龙虎榜成交额"], errors="coerce"),
            "net_buy": pd.to_numeric(df["龙虎榜净买额"], errors="coerce"),
            "buy_amount": pd.to_numeric(df["龙虎榜买入额"], errors="coerce"),
            "sell_amount": pd.to_numeric(df["龙虎榜卖出额"], errors="coerce"),
            "reason": df["上榜原因"],
            "source": "akshare_eastmoney",
            "retrieved_at": pd.Timestamp.now('UTC').isoformat(),
        })
        out.to_parquet(LHB / f"{d}.parquet", index=False)
        n_ok += 1
        print(f"{d}: +{len(out)} cum={n_ok}", flush=True)
    print(f"done {n_ok} days written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
