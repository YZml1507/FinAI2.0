"""按 stock_dzjy_mrmx 契约重拉 2025-01-01→今天的大宗交易明细（含券商列）。

上一轮续采误用了 mrtj（每日统计，无买卖双方营业部），导致 419 个
CN-schema 分片落盘、ev_bt_inst_sell 特征在 2025+ 无法计算。本脚本按
90 天窗口批量拉取并按 trade_date 落日分片，schema 与 2015-2024 EN 分片一致。
"""
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from finai.sources.block_trade_source import (  # noqa: E402
    fetch_eastmoney, normalize_frame, DEDUP_KEY)

OUT = ROOT / 'data/block_trade'
START = '2025-01-01'
END = pd.Timestamp.today().strftime('%Y-%m-%d')


def main() -> int:
    cur = pd.Timestamp(START)
    end = pd.Timestamp(END)
    n_files = n_rows = n_fail = 0
    while cur <= end:
        w_end = min(cur + pd.Timedelta(days=90), end)
        try:
            raw = fetch_eastmoney(
                cur.strftime('%Y-%m-%d'), w_end.strftime('%Y-%m-%d'))
            norm = normalize_frame(raw, cur.strftime('%Y-%m-%d'))
            for day, g in norm.groupby('trade_date'):
                day_key = str(day)[:10].replace('-', '')
                g = g.drop_duplicates(subset=DEDUP_KEY)
                p = OUT / f'{day_key}.parquet'
                g.to_parquet(p, index=False)
                n_files += 1
                n_rows += len(g)
            print(f'block {cur.date()}→{w_end.date()} rows={len(norm)}',
                  flush=True)
        except Exception as e:  # noqa: BLE001
            n_fail += 1
            print(f'block FAIL {cur.date()}→{w_end.date()} '
                  f'{type(e).__name__}: {str(e)[:120]}', flush=True)
        cur = w_end + pd.Timedelta(days=1)
        time.sleep(0.4)
    print(f'DONE files={n_files} rows={n_rows} fails={n_fail}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
