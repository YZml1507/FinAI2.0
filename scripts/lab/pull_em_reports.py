#!/usr/bin/env python3
"""拉取东财 reportapi 个股研报原子全史 (每股全区间翻页)。

每行 = 研报: 机构 x 作者 x 日期 x 评级 x 前评级 x 目标价 x 三年EPS预测。
EPS 字段 ~2018 起才有值 (EM 口径); 历史深度 ~2017+。
落盘: data/em_reports/{prefix3}.parquet  (增量: done_codes2.txt 断点, 每股全量覆盖写)。
用法: ./.venv/bin/python -u scripts/lab/pull_em_reports.py [--workers 4] [--codes 000001,600519]
"""
import argparse, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'data' / 'em_reports'
OUT.mkdir(parents=True, exist_ok=True)
DONE = OUT / 'done_codes2.txt'
API = 'https://reportapi.eastmoney.com/report/list'
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
      "Referer": "https://data.eastmoney.com/"}
KEEP = ['infoCode', 'publishDate', 'orgSName', 'orgName', 'author', 'emRatingName',
        'lastEmRatingName', 'indvAimPriceL', 'indvAimPriceT',
        'predictThisYearEps', 'predictNextYearEps', 'predictNextTwoYearEps',
        'predictThisYearPe', 'predictNextYearPe', 'predictNextTwoYearPe',
        'indvInduName', 'industryName', 'title', 'attachPages', 'encodeUrl']


def stock_codes() -> list:
    p = ROOT / 'data' / 'daily_bars'
    out = []
    for d in p.iterdir():
        if not d.is_dir():
            continue
        c = d.name.split('.')[-1]
        if len(c) == 6 and c.isdigit():
            out.append(c)
    return sorted(set(out))


def fetch_stock(code: str, max_pages: int = 40) -> list:
    rows = []
    for page in range(1, max_pages + 1):
        params = {"industryCode": "*", "pageSize": "100", "industry": "*",
                  "rating": "*", "ratingChange": "*",
                  "beginTime": "2000-01-01", "endTime": "2030-01-01",
                  "pageNo": str(page), "fields": "", "qType": "0",
                  "orgCode": "", "code": code, "rcode": "",
                  "p": str(page), "pageNum": str(page), "pageNumber": str(page)}
        d = None
        for i in range(5):
            try:
                d = requests.get(API, params=params, headers=UA, timeout=25).json()
                break
            except Exception:
                time.sleep(1.5 + i)
        if d is None:
            break
        data = d.get('data') or []
        rows.extend(data)
        if page >= (d.get('TotalPage') or 1):
            break
        time.sleep(0.25)
    return rows


def norm(rows: list, code: str) -> pd.DataFrame:
    out = []
    for r in rows:
        row = {}
        for k in KEEP:
            v = r.get(k)
            if isinstance(v, (list, tuple)):
                v = ','.join(str(x) for x in v)
            row[k] = v
        row['code'] = code
        out.append(row)
    return pd.DataFrame(out)


def _flush(buf: dict):
    for pref, dfs in buf.items():
        df = pd.concat(dfs)
        p = OUT / f'{pref}.parquet'
        if p.exists():
            df = pd.concat([pd.read_parquet(p), df]).drop_duplicates(subset=['infoCode', 'code'])
        df.to_parquet(p, index=False)
        print(f'{pref}: +{len(df)} rows -> {p.name}', flush=True)
    buf.clear()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--codes', default='')
    args = ap.parse_args()
    codes = args.codes.split(',') if args.codes else stock_codes()
    done = set(DONE.read_text().split()) if DONE.exists() else set()
    todo = [c for c in codes if c not in done]
    print(f'{len(todo)} stocks to pull (done={len(done)})', flush=True)
    buf, n = {}, 0
    t0 = time.time()

    def job(c):
        return c, norm(fetch_stock(c), c)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(job, c): c for c in todo}
        for fu in as_completed(futs):
            c, df = fu.result()
            n += 1
            if len(df):
                buf.setdefault(c[:3], []).append(df)
            with open(DONE, 'a') as f:
                f.write(c + '\n')
            if n % 500 == 0:
                _flush(buf)
                print(f'{n}/{len(todo)} stocks flushed ({time.time()-t0:.0f}s)', flush=True)
    _flush(buf)
    print('DONE', flush=True)


if __name__ == '__main__':
    main()
