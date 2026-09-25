"""通用巨潮 webapi 表采集器（enckey 鉴权,无需 token）。

用法:
  python scripts/lab/pull_cninfo_table.py --table sysapi/p_sysapi1094 --style tdate \
      --start 2005-01-01 --out data/cninfo_pledge
  python scripts/lab/pull_cninfo_table.py --table sysapi/p_sysapi1119 --style rdate --out data/cninfo_disclosure_schedule
  python scripts/lab/pull_cninfo_table.py --table stock/p_stock2328 --style scode --out data/cninfo_express
  python scripts/lab/pull_cninfo_table.py --table sysapi/p_sysapi1050 --style sdate --out data/cninfo_restructure

style:
  tdate  逐日历日拉取 (--start/--end)
  rdate  逐季度报告期拉取 (--start/--end)
  sdate  按 --win-days 窗口的 sdate/edate 区间拉取
  scode  逐股拉取 (codes 来自 --codes-file 或 data/daily_bars 目录)
断点: out/done_keys.txt 记录已完成 key。
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


def call_api(table, params, retries=4):
    for i in range(retries):
        try:
            r = requests.post(BASE + table, headers={
                'Accept-Enckey': enckey(),
                'Origin': 'https://webapi.cninfo.com.cn',
                'Referer': 'https://webapi.cninfo.com.cn/',
                'X-Requested-With': 'XMLHttpRequest',
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
            }, data=params, timeout=30)
            j = r.json()
            code = str(j.get('resultcode') or j.get('code') or '')
            if code in ('200', '000000'):
                recs = j.get('records') or j.get('data') or []
                return recs if isinstance(recs, list) else [recs]
            msg = str(j.get('resultmsg') or j.get('msg') or '')
            if '频繁' in msg or '429' in msg or '限制' in msg:
                time.sleep(3 + 3 * i)
                continue
            if code in ('402',):  # 参数错误,重试无意义
                raise RuntimeError(f'{table} 参数错误 {params}: {msg}')
            time.sleep(2 + 2 * i)
        except RuntimeError:
            raise
        except Exception:
            time.sleep(2 + 2 * i)
    raise RuntimeError(f'{table} {params} 连续 {retries} 次失败,中止以防记假 done')


def load_done(p):
    return set(p.read_text().split()) if p.exists() else set()


def write_year(out_dir, year, rows):
    if not rows:
        return
    df = pd.DataFrame(rows)
    p = out_dir / f'{year}.parquet'
    if p.exists():
        df = pd.concat([pd.read_parquet(p), df]).drop_duplicates()
    df.to_parquet(p, index=False)


def date_of(row):
    for k in ('DECLAREDATE', 'F001D', 'TRADEDATE', 'VARYDATE', 'ENDDATE', 'STARTDATE', 'RECTIME', 'F003D'):
        v = row.get(k)
        if v:
            return str(v)[:4]
    return 'unknown'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--table', required=True)
    ap.add_argument('--style', required=True, choices=['tdate', 'rdate', 'sdate', 'scode'])
    ap.add_argument('--start', default='2005-01-01')
    ap.add_argument('--end', default=pd.Timestamp.today().strftime('%Y-%m-%d'))
    ap.add_argument('--out', required=True)
    ap.add_argument('--win-days', type=int, default=120)
    ap.add_argument('--codes-file', default='')
    ap.add_argument('--extra', default='{}', help='JSON dict of extra params')
    ap.add_argument('--sleep', type=float, default=0.5)
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    done_file = out_dir / 'done_keys.txt'
    done = load_done(done_file)
    extra = json.loads(args.extra)

    if args.style == 'tdate':
        keys = [(d.strftime('%Y-%m-%d'), {'tdate': d.strftime('%Y-%m-%d')})
                for d in pd.date_range(args.start, args.end, freq='D')]
    elif args.style == 'rdate':
        keys = [(d.strftime('%Y-%m-%d'), {'rdate': d.strftime('%Y-%m-%d')})
                for d in pd.date_range(args.start, args.end, freq='QE')]
    elif args.style == 'sdate':
        keys = []
        d = pd.Timestamp(args.start)
        end = pd.Timestamp(args.end)
        while d <= end:
            e = min(d + pd.Timedelta(days=args.win_days), end)
            keys.append((d.strftime('%Y-%m-%d'),
                         {'sdate': d.strftime('%Y-%m-%d'), 'edate': e.strftime('%Y-%m-%d')}))
            d = e + pd.Timedelta(days=1)
    else:  # scode
        if args.codes_file:
            codes = [c.strip() for c in open(args.codes_file) if c.strip()]
        else:
            codes = sorted(p.name for p in (ROOT / 'data/daily_bars').iterdir() if p.is_dir())
            codes = [c.split('.')[0].zfill(6) for c in codes]
        keys = [(c, {'scode': c}) for c in codes]

    todo = [(k, p) for k, p in keys if k not in done]
    print(f'{args.table} style={args.style}: {len(todo)} keys to pull (done={len(done)})', flush=True)
    n = 0
    t0 = time.time()
    buf = {}
    for k, params in todo:
        params.update(extra)
        recs = call_api(args.table, params)
        for r in recs:
            buf.setdefault(date_of(r), []).append(r)
        with open(done_file, 'a') as f:
            f.write(k + '\n')
        n += 1
        if n % 100 == 0 or len(recs) > 2000:
            print(f'{n}/{len(todo)} key={k} recs={len(recs)} ({time.time()-t0:.0f}s)', flush=True)
            for y, rows in buf.items():
                write_year(out_dir, y, rows)
            buf = {}
        time.sleep(args.sleep)
    for y, rows in buf.items():
        write_year(out_dir, y, rows)
    print('DONE', flush=True)


if __name__ == '__main__':
    main()
