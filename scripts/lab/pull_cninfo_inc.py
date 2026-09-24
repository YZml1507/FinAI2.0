"""巨潮 webapi 增量/分页接口采集器（enckey 鉴权）。

三种 style:
  inc     objectid 游标全量拉取 (load/*_inc 系列): --table load/p_info3097_inc
  paged   page/rows 分页 + 月度 SDATE/EDATE 窗口 (bigdata/p_researchreport)
  scode   逐股拉取 (info/p_info3030/3032 等, 参数 scode + 可选 sdate/edate 经 --extra)
断点: out/cursor.txt (inc) 或 out/done_keys.txt (paged/scode)。
"""
import argparse, json, sys, time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
JS_PATH = ROOT / 'scripts/lab/cninfo.js'
BASE = 'https://webapi.cninfo.com.cn/api/'
_JS = None


def enckey():
    global _JS
    if _JS is None:
        import py_mini_racer
        _JS = py_mini_racer.MiniRacer()
        _JS.eval(open(JS_PATH, encoding='utf-8').read())
    return _JS.call('getResCode1')


def call_api(table, params, retries=5):
    for i in range(retries):
        try:
            r = requests.post(BASE + table, headers={
                'Accept-Enckey': enckey(),
                'Origin': 'https://webapi.cninfo.com.cn',
                'Referer': 'https://webapi.cninfo.com.cn/',
                'X-Requested-With': 'XMLHttpRequest',
                'User-Agent': 'Mozilla/5.0 (X11; Linux x64) AppleWebKit/537.36',
            }, data=params, timeout=30)
            j = r.json()
            code = str(j.get('resultcode') or j.get('code') or '')
            if code in ('200', '000000'):
                return j.get('records') or j.get('data') or []
            msg = str(j.get('resultmsg') or j.get('msg') or '')
            if '频繁' in msg or '429' in msg or '限制' in msg or '繁忙' in msg:
                time.sleep(3 + 3 * i)
                continue
            if code in ('402',):
                raise RuntimeError(f'{table} 参数错误 {params}: {msg}')
            time.sleep(2 + 2 * i)
        except RuntimeError:
            raise
        except Exception:
            time.sleep(2 + 2 * i)
    raise RuntimeError(f'{table} {params} 连续 {retries} 次失败,中止以防记假 done')


def date_of(row):
    for k in ('DECLAREDATE', 'F001D', 'F005D', 'Publishdate', 'VARYDATE', 'ENDDATE', 'RECTIME', 'F003D'):
        v = row.get(k)
        if v:
            return str(v)[:4]
    return 'unknown'


def write_year(out_dir, year, rows):
    if not rows:
        return
    df = pd.DataFrame(rows)
    p = out_dir / f'{year}.parquet'
    if p.exists():
        df = pd.concat([pd.read_parquet(p), df]).drop_duplicates()
    df.to_parquet(p, index=False)


def run_inc(table, out_dir, rowcount, sleep):
    cur_f = out_dir / 'cursor.txt'
    oid = int(cur_f.read_text().strip()) if cur_f.exists() else 0
    n = 0
    t0 = time.time()
    buf = {}
    while True:
        recs = call_api(table, {'objectid': oid, 'rowcount': rowcount})
        if not recs:
            cur_f.write_text(str(oid))
            break
        mx = oid
        for r in recs:
            o = r.get('OBJECTID')
            if o and o > mx:
                mx = o
            buf.setdefault(date_of(r), []).append(r)
        cur_f.write_text(str(mx))
        oid = mx
        n += 1
        if n % 20 == 0:
            print(f'inc {n} pages oid={oid} last_n={len(recs)} ({time.time()-t0:.0f}s)', flush=True)
            for y, rows in buf.items():
                write_year(out_dir, y, rows)
            buf = {}
        if len(recs) < rowcount:
            break
        time.sleep(sleep)
    for y, rows in buf.items():
        write_year(out_dir, y, rows)
    print('DONE', flush=True)


def run_paged(table, out_dir, extra, sleep):
    done_file = out_dir / 'done_keys.txt'
    done = set(done_file.read_text().split()) if done_file.exists() else set()
    keys = []
    d = pd.Timestamp('2005-01-01')
    end = pd.Timestamp.today()
    while d <= end:
        e = d + pd.offsets.MonthEnd(0)
        keys.append((d.strftime('%Y%m'), d.strftime('%Y%m%d'), e.strftime('%Y%m%d')))
        d = e + pd.Timedelta(days=1)
    todo = [k for k in keys if k[0] not in done]
    print(f'paged {table}: {len(todo)} month keys (done={len(done)})', flush=True)
    buf = {}
    for i, (k, sd, ed) in enumerate(todo):
        page = 1
        while True:
            params = dict(extra)
            params.update({'SDATE': sd, 'EDATE': ed, 'page': page, 'rows': 500})
            recs = call_api(table, params)
            inner = None
            for r in recs:
                if isinstance(r, dict) and '0' in r:
                    inner = r['0'].get('response', r['0'])
                    break
            if inner is None:
                break
            lst = inner.get('list') or []
            for r in lst:
                buf.setdefault(date_of(r), []).append(r)
            tp = int(inner.get('total_page') or 1)
            if page >= tp or not lst:
                break
            page += 1
            time.sleep(sleep)
        with open(done_file, 'a') as f:
            f.write(k + '\n')
        if (i + 1) % 10 == 0:
            print(f'paged {i+1}/{len(todo)} key={k} ({time.time():.0f})', flush=True)
            for y, rows in buf.items():
                write_year(out_dir, y, rows)
            buf = {}
    for y, rows in buf.items():
        write_year(out_dir, y, rows)
    print('DONE', flush=True)


def run_scode(table, out_dir, codes_file, extra, sleep):
    done_file = out_dir / 'done_keys.txt'
    done = set(done_file.read_text().split()) if done_file.exists() else set()
    if codes_file:
        codes = [c.strip() for c in open(codes_file) if c.strip()]
    else:
        codes = sorted(p.name.split('.')[0].zfill(6) for p in (ROOT / 'data/daily_bars').iterdir() if p.is_dir())
    todo = [c for c in codes if c not in done]
    print(f'scode {table}: {len(todo)} codes (done={len(done)})', flush=True)
    buf = {}
    t0 = time.time()
    for i, c in enumerate(todo):
        params = dict(extra)
        params['scode'] = c
        try:
            recs = call_api(table, params)
        except RuntimeError as e:
            print(f'skip {c}: {e}', flush=True)
            continue
        for r in recs:
            buf.setdefault(date_of(r), []).append(r)
        with open(done_file, 'a') as f:
            f.write(c + '\n')
        if (i + 1) % 200 == 0:
            print(f'scode {i+1}/{len(todo)} ({time.time()-t0:.0f}s)', flush=True)
            for y, rows in buf.items():
                write_year(out_dir, y, rows)
            buf = {}
        time.sleep(sleep)
    for y, rows in buf.items():
        write_year(out_dir, y, rows)
    print('DONE', flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--table', required=True)
    ap.add_argument('--style', required=True, choices=['inc', 'paged', 'scode'])
    ap.add_argument('--out', required=True)
    ap.add_argument('--rowcount', type=int, default=2000)
    ap.add_argument('--codes-file', default='')
    ap.add_argument('--extra', default='{}')
    ap.add_argument('--sleep', type=float, default=0.5)
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    extra = json.loads(args.extra)
    if args.style == 'inc':
        run_inc(args.table, out_dir, args.rowcount, args.sleep)
    elif args.style == 'paged':
        run_paged(args.table, out_dir, extra, args.sleep)
    else:
        run_scode(args.table, out_dir, args.codes_file, extra, args.sleep)


if __name__ == '__main__':
    main()
