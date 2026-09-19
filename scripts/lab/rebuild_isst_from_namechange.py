#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""E12-isST 重建：按 PIT 名称史回写日线 isST 列（数据层语义变更）。

依据：docs/E12_ISST_REBUILD_DESIGN.md（设计冻结，预登记）。
规则：isST(date)=1  iff  ∃ namechange 行：start_date <= date <= (end_date or +∞)
      and 'ST' in name.upper()。*ST/ST 统一命中；摘帽（end_date 非空）后恢复 0；
      无名称史区间 isST=0（与现状一致，不制造不存在的标记）。

写入目标（按文件原 dtype 回写，str 列写 '0'/'1'、int 列写 0/1，保证幂等）：
  1. data/dividend_stocks/{sym}/{year}.parquet        —— 权威池（语义变更）
  2. experiments/lab/market-breadth-a/daily_bars/{sym}.parquet
  3. experiments/lab/market-breadth-a/delisted_bars/{sym}.parquet

纪律：每票 tmp→rename 原子写；跑前跑后 SHA-256 留痕（manifest）；
--dry-run 只出影响面报告不落盘。幂等：二次运行 files_changed=0。

用法：
  .venv/bin/python scripts/lab/rebuild_isst_from_namechange.py --dry-run
  .venv/bin/python scripts/lab/rebuild_isst_from_namechange.py [--targets dividend|breadth|all]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

NC = ROOT / 'data/namechange/namechange.parquet'
MANIFEST = ROOT / 'data/namechange/ISST_REBUILD_MANIFEST.json'
TARGETS = {
    'dividend': [ROOT / 'data/dividend_stocks'],
    'breadth': [ROOT / 'experiments/lab/market-breadth-a/daily_bars',
                ROOT / 'experiments/lab/market-breadth-a/delisted_bars'],
}


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    except Exception:
        return 'unknown'


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load_st_intervals() -> dict:
    """{code: [(start,end),...]}（YYYYMMDD 字符串，end 缺省=99999999）。"""
    nc = pd.read_parquet(NC)
    st = nc[nc['name'].str.upper().str.contains('ST', na=False)]
    out: dict[str, list] = {}
    for r in st.itertuples():
        end = r.end_date if isinstance(r.end_date, str) and r.end_date else '99999999'
        out.setdefault(r.code, []).append((r.start_date, end))
    return out


def bar_files(base: Path) -> list:
    """权威池按 {sym}/{year}.parquet 布局；宽度宇宙按 {sym}.parquet 平铺。"""
    files = []
    for p in sorted(base.rglob('*.parquet')):
        if p.parent == base and p.stem.startswith(('sh.', 'sz.', 'bj.')):
            files.append((p.stem, p))
        elif p.parent.name.startswith(('sh.', 'sz.', 'bj.')):
            files.append((p.parent.name, p))
    return files


def isst_series(dates: pd.Series, intervals: list) -> pd.Series:
    """dates → bool 掩码。兼容 'YYYY-MM-DD' 对象列 / datetime64 / 'YYYYMMDD'。"""
    if not intervals:
        return pd.Series(False, index=dates.index)
    d = dates.astype(str).str.replace('-', '', regex=False).str[:8]
    mask = pd.Series(False, index=dates.index)
    for lo, hi in intervals:
        mask |= (d >= lo) & (d <= hi)
    return mask


def rebuild_file(path: Path, intervals: list, dry: bool) -> dict:
    before = sha256(path)
    df = pd.read_parquet(path)
    want = isst_series(df['date'], intervals)
    if df['isST'].dtype == object or str(df['isST'].dtype).startswith('str'):
        new = want.map({True: '1', False: '0'}).astype(df['isST'].dtype)
    else:
        new = want.astype(df['isST'].dtype)
    flipped = int((new.values != df['isST'].values).sum())
    rec = {'file': str(path.relative_to(ROOT)), 'sha_before': before,
           'rows': len(df), 'flipped': flipped}
    if flipped == 0:
        rec['sha_after'] = before
        rec['changed'] = False
        return rec
    if not dry:
        df['isST'] = new
        tmp = path.with_suffix('.parquet.tmp')
        df.to_parquet(tmp, index=False)
        tmp.replace(path)
        rec['sha_after'] = sha256(path)
    else:
        rec['sha_after'] = None
    rec['changed'] = not dry
    rec['would_change'] = dry
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='只出影响面报告，不写任何 bar 文件')
    ap.add_argument('--targets', default='all',
                    choices=['dividend', 'breadth', 'all'])
    ap.add_argument('--report', default=str(MANIFEST))
    args = ap.parse_args()

    intervals_map = load_st_intervals()
    bases = (TARGETS['dividend'] + TARGETS['breadth'] if args.targets == 'all'
             else TARGETS[args.targets])
    manifest = {
        'git_sha': git_sha(),
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'dry_run': args.dry_run,
        'targets': args.targets,
        'rule': "isST=1 iff namechange行 start<=date<=(end or inf) and 'ST' in name.upper()",
        'files': [],
        'summary': {},
    }
    tot = {'scanned': 0, 'changed': 0, 'flipped_rows': 0,
           'symbols_affected': 0}
    affected = set()
    for base in bases:
        if not base.exists():
            print(f'[SKIP] {base} 不存在', file=sys.stderr)
            continue
        for sym, f in bar_files(base):
            rec = rebuild_file(f, intervals_map.get(sym, []), args.dry_run)
            manifest['files'].append(rec)
            tot['scanned'] += 1
            tot['flipped_rows'] += rec['flipped']
            if rec['flipped']:
                tot['changed'] += 1
                affected.add(sym)
    tot['symbols_affected'] = len(affected)
    manifest['summary'] = tot
    manifest['symbols_affected'] = sorted(affected)

    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    tmp = rp.with_suffix('.tmp')
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                   encoding='utf-8')
    tmp.replace(rp)

    mode = 'DRY-RUN' if args.dry_run else 'REBUILD'
    print(f'[{mode}] 扫描 {tot["scanned"]} 文件，'
          f'{"将影响" if args.dry_run else "变更"} {tot["changed"]} 文件 / '
          f'{tot["flipped_rows"]} 行翻转 / {tot["symbols_affected"]} 票受影响')
    print(f'[{mode}] manifest -> {rp}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
