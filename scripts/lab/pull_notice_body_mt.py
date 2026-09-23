#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""公告正文采集器（限定类型·多线程版）——pull_notice_body 的吞吐升级版。

变更点：fetch 由 ThreadPoolExecutor(--workers) 并发，取消固定 sleep
（东财接口实测 0.17s/条串行——8 并发 ≈ 45 条/s 理论，实测被频控压住
后自适应退避）；done_codes.json 仅在每条成功后同步追加（行级原子写
jsonl 追加模式，避免整集 dump 的写竞争——与旧版 done_codes.json 不兼
容，用独立文件 done_codes_mt.json；启动时把旧集并入）。

用法: pull_notice_body_mt.py --years 2015,2016 --workers 8
"""
from __future__ import annotations

import argparse
import glob
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

import re

# 与 pull_notice_body 同源（脚本直跑无包路径，内联常量）
BODY_TYPES = ['年度报告全文', '半年度报告', '季度报告', '业绩快报',
              '业绩预告', '重大事项', '停牌', '复牌', '诉讼', '仲裁',
              '风险提示', '退市', '违规', '处罚', '警示', '问询',
              '关注函', '监管', '立案调查']
API = ('https://np-cnotice-stock.eastmoney.com/api/content/ann'
       '?art_code={code}&client_source=web&page_index=1')


def art_code(url):
    m = re.search(r'(AN\d{15,})', str(url))
    return m.group(1) if m else None


def want(atype):
    return any(k in str(atype) for k in BODY_TYPES)

ROOT = Path(__file__).resolve().parents[2]
META_DIR = ROOT / 'data' / 'notice_meta'
OUT_DIR = ROOT / 'data' / 'notice_body'
DONE = OUT_DIR / 'done_codes_mt.jsonl'
LEGACY_DONE = OUT_DIR / 'done_codes.json'

_tls = threading.local()
_lock = threading.Lock()


def _sess() -> requests.Session:
    s = getattr(_tls, 's', None)
    if s is None:
        s = requests.Session()
        s.headers['User-Agent'] = 'Mozilla/5.0'
        _tls.s = s
    return s


def _fetch(code: str, retry: int = 3) -> str | None:
    for i in range(retry):
        try:
            r = _sess().get(API.format(code=code), timeout=20)
            if r.status_code == 429:
                time.sleep(2.0 * (i + 1))
                continue
            j = r.json()
            if j.get('success') and j.get('data'):
                return j['data'].get('notice_content') or ''
            return ''
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (i + 1))
    return None


def _load_done() -> set:
    done: set = set()
    if LEGACY_DONE.exists():
        done.update(json.loads(LEGACY_DONE.read_text()))
    if DONE.exists():
        for line in DONE.read_text().splitlines():
            line = line.strip()
            if line:
                done.add(line)
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', default='')
    ap.add_argument('--workers', type=int, default=8)
    a = ap.parse_args()
    years = set(a.years.split(',')) if a.years else None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    done = _load_done()

    for f in sorted(glob.glob(str(META_DIR / '*.parquet'))):
        if years and not any(y in Path(f).name for y in years):
            continue
        d = pd.read_parquet(f)
        d['art_code'] = d['网址'].map(art_code)
        d = d[d['art_code'].notna() & d['公告类型'].map(want)]
        d = d[~d['art_code'].isin(done)]
        if d.empty:
            continue
        yr = str(d['公告日期'].iloc[0])[:4]
        rows = []
        with ThreadPoolExecutor(max_workers=a.workers) as pool:
            futs = {pool.submit(_fetch, c): (c, r) for c, r in
                    zip(d['art_code'], d.itertuples(index=False))}
            for fut in as_completed(futs):
                code, row = futs[fut]
                txt = fut.result()
                if txt is None:
                    continue
                rows.append({'art_code': code,
                             'code': str(row.代码).zfill(6),
                             'name': row.名称, 'title': row.公告标题,
                             'atype': row.公告类型,
                             'ann_date': str(row.公告日期)[:10],
                             'text': txt})
                with _lock:
                    done.add(code)
                    with DONE.open('a') as fh:
                        fh.write(code + '\n')
        if rows:
            out_f = OUT_DIR / f'{yr}.parquet'
            buf = (pd.read_parquet(out_f) if out_f.exists()
                   else pd.DataFrame())
            pd.concat([buf, pd.DataFrame(rows)]).to_parquet(
                out_f, index=False)
        print(f'{Path(f).name}: +{len(rows)} cum_done={len(done)}',
              flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
