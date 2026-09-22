"""e65 前置：全 A 日线 OHLCV 采集（baostock, RAW 不复权）。

输出 schema 对齐 data/dividend_stocks 引擎消费格式:
date,open,high,low,close,preclose,volume,amount,turn,pctChg,tradestatus,isST,
code,source,adjust_mode

落盘: data/daily_bars/{symbol}/{year}.parquet   (symbol = sh.600000 形态)
断点续传: 已有分区跳过。限速 sleep(0.3)。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import baostock as bs
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'data' / 'daily_bars'
FIELDS = ('date,open,high,low,close,preclose,volume,amount,turn,pctChg,'
          'tradestatus,isST')
START, END = '2014-06-01', '2024-12-31'


def universe() -> list[str]:
    """e63 矩阵全股票 -> baostock 码。"""
    X = pd.read_parquet(ROOT / 'experiments/lab/e63_Xlab.parquet',
                        columns=['ts_code'])
    codes = sorted(X['ts_code'].unique())
    out = []
    for c in codes:
        num, ex = c.split('.')
        out.append(('sh.' if ex == 'SH' else 'sz.') + num)
    return out


def pull_one(code: str) -> pd.DataFrame:
    rs = bs.query_history_k_data_plus(
        code, FIELDS, start_date=START, end_date=END,
        frequency='d', adjustflag='3')
    rows = []
    while rs.error_code == '0' and rs.next():
        rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=FIELDS.split(','))
    if df.empty:
        return df
    df['code'] = code
    df['source'] = 'baostock'
    df['adjust_mode'] = 'RAW'
    df = df[df['tradestatus'] == '1']          # R1 停牌脏行滤除
    for c in ('open', 'high', 'low', 'close', 'preclose', 'volume',
              'amount', 'turn', 'pctChg'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['date'] = pd.to_datetime(df['date']).dt.date
    return df


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lg = bs.login()
    assert lg.error_code == '0', lg.error_msg
    codes = universe()
    done, fail = 0, []
    try:
        for i, code in enumerate(codes):
            sym_dir = OUT / code
            try:
                df = pull_one(code)
                if df.empty:
                    done += 1
                    continue
                for yr, sub in df.groupby(df['date'].apply(lambda d: d.year)):
                    sym_dir.mkdir(parents=True, exist_ok=True)
                    fp = sym_dir / f'{yr}.parquet'
                    if fp.exists():
                        old = pd.read_parquet(fp)
                        sub = (pd.concat([old, sub]).drop_duplicates('date')
                                 .sort_values('date'))
                    sub.to_parquet(fp)
                done += 1
            except Exception as e:            # noqa: BLE001
                fail.append((code, str(e)[:100]))
            if (i + 1) % 100 == 0:
                print(f'[bars] {i+1}/{len(codes)} done={done} fail={len(fail)}',
                      flush=True)
            time.sleep(0.3)
    finally:
        bs.logout()
    pd.DataFrame(fail, columns=['code', 'err']).to_csv(
        OUT / 'pull_failures.csv', index=False)
    print(f'[bars] DONE done={done} fail={len(fail)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
