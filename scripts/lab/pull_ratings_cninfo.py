#!/usr/bin/env python3
"""拉取巨潮 webapi 投资评级原子全史 (p_sysapi1089)。

每行 = 机构 x 研究员 x 评级 x 评级变化 x 目标价, 2005-至今。
鉴权: Accept-EncKey = base64(unix_ts), access_token 表单字段 (org secret CNINFO_ACCESS_TOKEN)。
落盘: data/ratings_cninfo/{year}.parquet  (增量: 已有年份文件读回合入, 去重 by 全字段)。
用法: ./.venv/bin/python -u scripts/lab/pull_ratings_cninfo.py [--start 2005-01-01] [--end 2026-12-31] [--workers 2]
"""
import argparse, base64, json, os, sys, time, urllib.parse, urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'data' / 'ratings_cninfo'
OUT.mkdir(parents=True, exist_ok=True)
DONE = OUT / 'done_days.txt'
API = 'http://webapi.cninfo.com.cn/api/sysapi/p_sysapi1089'
TOKEN = os.environ.get('CNINFO_ACCESS_TOKEN', '')
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
COLS = {'SECCODE': 'code', 'SECNAME': 'name', 'DECLAREDATE': 'ann_date',
        'F002V': 'org', 'F003V': 'analyst', 'F004V': 'rating',
        'F006V': 'first_cover', 'F007V': 'rating_chg', 'F008V': 'prev_rating',
        'F009N': 'target_low', 'F010N': 'target_high'}


def enckey():
    return base64.b64encode(str(int(time.time())).encode()).decode()


def fetch_day(td: str, retries: int = 6) -> list:
    for i in range(retries):
        try:
            body = urllib.parse.urlencode({'tdate': td, 'access_token': TOKEN}).encode()
            req = urllib.request.Request(API, data=body, headers={
                'User-Agent': UA, 'Accept-EncKey': enckey(),
                'Referer': 'http://webapi.cninfo.com.cn/'})
            d = json.loads(urllib.request.urlopen(req, timeout=30).read())
            if d.get('resultcode') == 200:
                return d.get('records') or []
            if '没有购买' in str(d.get('resultmsg')) or '过期' in str(d.get('resultmsg')):
                raise SystemExit(f'账号凭证失效: {d}')
            if d.get('resultcode') == 429 or '限流' in str(d.get('resultmsg')):
                time.sleep(15 + 15 * i)
                continue
            time.sleep(3 + 3 * i)
        except SystemExit:
            raise
        except Exception:
            time.sleep(2 + 2 * i)
    raise RuntimeError(f'fetch_day {td} 连续 {retries} 次失败,中止以防记假 done')


def load_done() -> set:
    if DONE.exists():
        return set(DONE.read_text().split())
    return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2005-01-01')
    ap.add_argument('--end', default=pd.Timestamp.today().strftime('%Y-%m-%d'))
    ap.add_argument('--workers', type=int, default=1)  # 保留参数, 串行最稳
    args = ap.parse_args()
    if not TOKEN:
        sys.exit('需要环境变量 CNINFO_ACCESS_TOKEN')
    days = [d.strftime('%Y-%m-%d') for d in pd.date_range(args.start, args.end, freq='D')]
    done = load_done()
    todo = [d for d in days if d not in done]
    print(f'{len(todo)} days to pull (done={len(done)})', flush=True)
    buf = {}
    n_done = 0
    t0 = time.time()
    for td in todo:
        recs = fetch_day(td)
        y = td[:4]
        for r in recs:
            row = {dst: r.get(src) for src, dst in COLS.items()}
            buf.setdefault(y, []).append(row)
        with open(DONE, 'a') as f:
            f.write(td + '\n')
        n_done += 1
        if n_done % 200 == 0:
            print(f'{n_done}/{len(todo)} days, last={td} recs={len(recs)} ({time.time()-t0:.0f}s)', flush=True)
        time.sleep(0.45)
    for y, rows in buf.items():
        if not rows:
            continue
        df = pd.DataFrame(rows)
        p = OUT / f'{y}.parquet'
        if p.exists():
            df = pd.concat([pd.read_parquet(p), df]).drop_duplicates()
        df.to_parquet(p, index=False)
        print(f'{y}: +{len(rows)} -> {p.name} ({len(df)} rows)', flush=True)
    print('DONE', flush=True)


if __name__ == '__main__':
    main()
