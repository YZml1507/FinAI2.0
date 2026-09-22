#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""公告正文采集器（限定类型）——元数据全量完成后运行。

spec: 元数据全量 + 限定类型正文（财报/重大事项/诉讼/停牌/风险类）。
流程: data/notice_meta/*.parquet → 类型过滤 → art_code 提取 →
      np-cnotice-stock API notice_content → data/notice_body/YYYY.parquet
断点续传: 已采 art_code 写 done_codes.json，重跑自动跳过。
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
META_DIR = ROOT / 'data' / 'notice_meta'
OUT_DIR = ROOT / 'data' / 'notice_body'
DONE = OUT_DIR / 'done_codes.json'

# 限定类型白名单（含子串匹配）
BODY_TYPES = ['年度报告全文', '半年度报告', '季度报告', '业绩快报',
              '业绩预告', '重大事项', '停牌', '复牌', '诉讼', '仲裁',
              '风险提示', '退市', '违规', '处罚', '警示', '问询',
              '关注函', '监管', '立案调查']

API = ('https://np-cnotice-stock.eastmoney.com/api/content/ann'
       '?art_code={code}&client_source=web&page_index=1')


def art_code(url: str) -> str | None:
    m = re.search(r'(AN\d{15,})', str(url))
    return m.group(1) if m else None


def want(atype: str) -> bool:
    return any(k in str(atype) for k in BODY_TYPES)


def fetch(code: str, sess: requests.Session, retry: int = 3) -> str | None:
    for i in range(retry):
        try:
            r = sess.get(API.format(code=code), timeout=15)
            j = r.json()
            if j.get('success') and j.get('data'):
                return j['data'].get('notice_content') or ''
            return ''
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (i + 1))
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', default='', help='逗号分隔年份限制')
    ap.add_argument('--sleep', type=float, default=0.18)
    a = ap.parse_args()
    years = set(a.years.split(',')) if a.years else None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    done: set = set()
    if DONE.exists():
        done = set(json.loads(DONE.read_text()))
    sess = requests.Session()
    sess.headers['User-Agent'] = 'Mozilla/5.0'

    for f in sorted(glob.glob(str(META_DIR / '*.parquet'))):
        d = pd.read_parquet(f)
        if years and not any(y in Path(f).name for y in years):
            continue
        d['art_code'] = d['网址'].map(art_code)
        d = d[d['art_code'].notna() & d['公告类型'].map(want)]
        d = d[~d['art_code'].isin(done)]
        if d.empty:
            continue
        yr = str(d['公告日期'].iloc[0])[:4]
        out_f = OUT_DIR / f'{yr}.parquet'
        buf = (pd.read_parquet(out_f) if out_f.exists()
               else pd.DataFrame())
        rows = []
        for _, row in d.iterrows():
            txt = fetch(row['art_code'], sess)
            if txt is None:
                continue
            rows.append({'art_code': row['art_code'],
                         'code': str(row['代码']).zfill(6),
                         'name': row['名称'], 'title': row['公告标题'],
                         'atype': row['公告类型'],
                         'ann_date': str(row['公告日期'])[:10],
                         'text': txt})
            done.add(row['art_code'])
            time.sleep(a.sleep)
            if len(rows) >= 500:
                buf = pd.concat([buf, pd.DataFrame(rows)])
                rows = []
        if rows:
            buf = pd.concat([buf, pd.DataFrame(rows)])
        if not buf.empty:
            buf.to_parquet(out_f, index=False)
        DONE.write_text(json.dumps(sorted(done)))
        print(f'{Path(f).name}: cum_done={len(done)}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
