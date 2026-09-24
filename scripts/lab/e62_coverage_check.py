#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e62 覆盖率核验：notice_meta 白名单行 vs notice_body 已采 art_code。

正式评估门槛：2015-2024 白名单覆盖 ≥80%（E62_BODY_NLP_PREREG.md §6）。
"""
import glob
import re
from pathlib import Path

import pandas as pd

BT = ['年度报告全文', '半年度报告', '季度报告', '业绩快报', '业绩预告',
      '重大事项', '停牌', '复牌', '诉讼', '仲裁', '风险提示', '退市',
      '违规', '处罚', '警示', '问询', '关注函', '监管', '立案调查']


def want(a):
    return any(k in str(a) for k in BT)


def art(u):
    m = re.search(r'(AN\d{15,})', str(u))
    return m.group(1) if m else None


def main():
    wl = {}
    for f in sorted(glob.glob('data/notice_meta/*.parquet')):
        if not Path(f).stem.isdigit():
            continue
        d = pd.read_parquet(f, columns=['网址', '公告类型', '公告日期'])
        d['ac'] = d['网址'].map(art)
        d = d[d['ac'].notna() & d['公告类型'].map(want)]
        for ac, yr in zip(d['ac'], d['公告日期'].astype(str).str[:4]):
            wl.setdefault(yr, set()).add(ac)

    body = {}
    for f in sorted(glob.glob('data/notice_body/*.parquet')):
        if '_d' in Path(f).stem:
            continue
        d = pd.read_parquet(f, columns=['art_code', 'ann_date'])
        for ac, yr in zip(d['art_code'], d['ann_date'].astype(str).str[:4]):
            body.setdefault(yr, set()).add(ac)

    print(f'{"year":6} {"whitelist":>10} {"body":>8} {"hit":>8} {"cov":>7}')
    tot_w = tot_hit = 0
    for y in sorted(set(wl) | set(body)):
        w, b = wl.get(y, set()), body.get(y, set())
        hit = len(w & b)
        cov = hit / max(len(w), 1)
        tot_w += len(w); tot_hit += hit
        print(f'{y:6} {len(w):>10} {len(b):>8} {hit:>8} {cov:>7.1%}', flush=True)
    w15_24 = sum(len(wl.get(str(y), set())) for y in range(2015, 2025))
    h15_24 = sum(len(wl.get(str(y), set()) & body.get(str(y), set()))
                 for y in range(2015, 2025))
    print(f'2015-2024 总覆盖 = {h15_24}/{w15_24} = {h15_24/max(w15_24,1):.1%}')
    print(f'(全期 {tot_hit}/{tot_w})')


if __name__ == '__main__':
    main()
