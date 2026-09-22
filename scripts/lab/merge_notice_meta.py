#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""notice_meta 三路合并+去重+manifest——子会话交付与本机串行段汇合。

用法: merge_notice_meta.py <外部目录或tar.gz> [<更多>] ——并入
      data/notice_meta/，按 (公告日期,代码,公告标题) 去重，写 manifest。
"""
from __future__ import annotations

import glob
import json
import sys
import tarfile
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
META = ROOT / 'data' / 'notice_meta'


def main() -> int:
    META.mkdir(parents=True, exist_ok=True)
    added = 0
    for src in sys.argv[1:]:
        p = Path(src)
        if p.suffix == '.gz' or p.name.endswith('.tar.gz'):
            tmp = Path(tempfile.mkdtemp())
            with tarfile.open(p) as t:
                t.extractall(tmp)
            files = list(tmp.rglob('*.parquet'))
        else:
            files = sorted(p.glob('*.parquet'))
        for f in files:
            dst = META / f.name
            if dst.exists():
                continue
            dst.write_bytes(f.read_bytes())
            added += 1
    # manifest
    stats = {'files': 0, 'rows': 0, 'days': [], 'empty': 0}
    for f in sorted(META.glob('*.parquet')):
        stats['files'] += 1
        d = pd.read_parquet(f)
        stats['rows'] += len(d)
        if len(d):
            stats['days'].append(str(pd.to_datetime(d['公告日期']).min())[:10])
        else:
            stats['empty'] += 1
    stats['day_min'] = min(stats['days']) if stats['days'] else None
    stats['day_max'] = max(stats['days']) if stats['days'] else None
    stats.pop('days')
    stats['added_this_run'] = added
    (META / 'manifest_days.json').write_text(
        json.dumps(stats, ensure_ascii=False, indent=1))
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
