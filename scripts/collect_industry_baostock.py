"""证监会行业分类时点快照采集（baostock query_stock_industry）。

- 每季末一次快照：YYYY-03-31/06-30/09-30/12-31，2009Q4..2024Q4
- 输出 data/industry/{YYYYMMDD}.parquet，列 updateDate/code/code_name/industry/industryClassification
- PIT 说明：baostock 返回 date 当日有效归属，天然时点正确；granularity=季
- 用途：行业中性化/行业轮动使能数据（证监会分类，较申万粗——memo 26 登记）
"""
import argparse, json, sys, time
from pathlib import Path
import baostock as bs
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "industry"
QENDS = ["0331", "0630", "0930", "1231"]


def fetch(bs_date: str) -> pd.DataFrame:
    rs = bs.query_stock_industry(date=bs_date)
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=rs.fields)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-year", type=int, default=2009)
    ap.add_argument("--end-year", type=int, default=2024)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    lg = bs.login()
    assert lg.error_code == "0", lg.error_msg
    manifest = {}
    try:
        for y in range(args.start_year, args.end_year + 1):
            for qe in QENDS:
                d = f"{y}{qe}"
                out = OUT / f"{d}.parquet"
                if out.exists() and out.stat().st_size > 100:
                    continue
                df = fetch(f"{y}-{qe[:2]}-{qe[2:]}")
                if df.empty:
                    print(f"{d}: EMPTY", flush=True)
                    continue
                df.to_parquet(out, index=False)
                manifest[d] = {"rows": len(df)}
                print(f"{d}: {len(df)} rows", flush=True)
                time.sleep(0.3)
    finally:
        bs.logout()
    (OUT / "_manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    print(f"done: {len(manifest)} snapshots")


if __name__ == "__main__":
    sys.exit(main())
