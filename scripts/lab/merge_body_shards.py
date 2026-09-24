#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""合并公告正文分片：本机 data/notice_body/*.parquet + Release
notice-body-shards 下载的分片(_sN / rec_) → 按 art_code 去重合并
→ 写回 data/notice_body/{year}.parquet（canonical，schema 统一）。

流程: ① gh release download notice-body-shards 到 staging 目录
      ② 对所有分片(本机已有 + 下载)按年聚合、art_code 去重
      ③ 写 canonical {year}.parquet + 打印各年贡献来源
用法: merge_body_shards.py [--no-download] [--staging DIR]
"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BODY = ROOT / 'data' / 'notice_body'
STAGING = ROOT / 'data' / 'notice_body_shards'
RELEASE = 'notice-body-shards'
REPO = 'YZml1507/FinAI2.0'

COLS = ['art_code', 'code', 'name', 'title', 'atype',
        'ann_date', 'text', 'pdf_url']
SHARD_RE = re.compile(r'(?:rec_)?(\d{4})(?:_s\d+[a-z]?|_d|_l\d+|_fc)?\.parquet$')


def download(staging: Path) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['gh', 'release', 'download', RELEASE, '-R', REPO,
         '--pattern', '*.parquet', '-D', str(staging), '--clobber'],
        check=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-download', action='store_true')
    ap.add_argument('--staging', default=str(STAGING))
    a = ap.parse_args()
    staging = Path(a.staging)
    if not a.no_download:
        download(staging)

    sources: dict[int, list[Path]] = {}
    for f in list(BODY.glob('*.parquet')) + list(staging.glob('*.parquet')):
        m = SHARD_RE.match(f.name)
        if m:
            sources.setdefault(int(m.group(1)), []).append(f)

    for yr in sorted(sources):
        parts = []
        for f in sorted(sources[yr]):
            try:
                d = pd.read_parquet(f)
                d = d[[c for c in COLS if c in d.columns]]
                parts.append((f.name, d))
            except Exception as e:  # noqa: BLE001
                print(f'  skip {f.name}: {e}')
        if not parts:
            continue
        alld = pd.concat([d for _, d in parts], ignore_index=True)
        before = len(alld)
        alld = alld.drop_duplicates('art_code')
        out = BODY / f'{yr}.parquet'
        alld.to_parquet(out, index=False)
        print(f'{yr}: {len(alld)} rows ({before} pre-dedup) '
              f'from {[n for n, _ in parts]}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
