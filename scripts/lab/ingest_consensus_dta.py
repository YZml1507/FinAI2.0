#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一致预期 dta→parquet 接入管线（CSMAR ¥5.4 数据包）。

用法:
    python scripts/lab/ingest_consensus_dta.py <dta文件或目录> [--out data/consensus]

- 输入: 商家合并后的 .dta (Stata) 文件，按股/按年分片均可
- 输出: data/consensus/YYYY.parquet 按公告/发布日期年分片 + manifest.json
- 验收: 退市股样本覆盖(300104/600074)、字段盘点、日期范围、年分布
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DELISTED_SAMPLES = ['300104', '600074']  # 乐视网/保千里
CAND_DATE = ['estbdt', 'ann_date', 'date', 'est_date', 'rpt_date',
             'Estbdt', 'ANNDATE', 'ESTBDT', 'pub_date', 'disclosure_date']
CAND_CODE = ['stockcode', 'stkcd', 'code', 'symbol', 'Stkcd', 'STKCD',
             'ts_code', '证券代码', '股票代码']
CAND_EST = ['eps', 'f_eps', 'est_eps', 'mean_eps', 'FEPS', 'estpe',
            'net_profit', 'f_np', 'est_np', 'profit_forecast',
            'target_price', 'f_tp', 'rating', 'f_rating']


def norm_code(c: str) -> str:
    c = str(c).strip().upper().split('.')[0].zfill(6)
    suf = 'SH' if c[0] in '69' or c.startswith(('500', '501', '502', '510',
                                                '511', '512', '513', '515',
                                                '518', '580', '582')) \
        else 'SZ' if c[0] in '023' or c.startswith(('159', '160', '184')) \
        else 'BJ' if c[0] in '48' else 'SH'
    return f'{c}.{suf}'


def find_col(df: pd.DataFrame, cands: list[str]) -> str | None:
    low = {c.lower(): c for c in df.columns}
    for c in cands:
        if c.lower() in low:
            return low[c.lower()]
    return None


def ingest(paths: list[Path], out_dir: Path) -> dict:
    frames = []
    for p in paths:
        try:
            df = pd.read_stata(p, convert_dates=True)
        except Exception as e:  # noqa: BLE001
            print(f'[skip] {p.name}: {e}')
            continue
        df['__src'] = p.name
        frames.append(df)
        print(f'[read] {p.name}: {df.shape}')
    if not frames:
        raise SystemExit('no dta files readable')
    d = pd.concat(frames, ignore_index=True)

    code_col = find_col(d, CAND_CODE)
    date_col = find_col(d, CAND_DATE)
    if code_col is None:
        raise SystemExit(f'no code column among {d.columns.tolist()}')
    d['ts_code'] = d[code_col].astype(str).map(norm_code)
    if date_col is not None:
        d['est_date'] = pd.to_datetime(d[date_col], errors='coerce')
    else:
        d['est_date'] = pd.NaT
        print('[warn] no date column found — single-year file assumed')

    est_cols = [c for c in d.columns if any(k in c.lower() for k in
                ['eps', 'np', 'profit', 'rating', 'target', 'price',
                 'pe', 'pb', 'income', 'revenue', 'estimate'])]
    out_dir.mkdir(parents=True, exist_ok=True)
    if d['est_date'].notna().any():
        d['yr'] = d['est_date'].dt.year
        for yr, g in d.groupby('yr'):
            g.drop(columns='__src').to_parquet(out_dir / f'{int(yr)}.parquet',
                                             index=False)
    else:
        d.drop(columns='__src').to_parquet(out_dir / 'all.parquet', index=False)

    bare = d[code_col].astype(str).str.strip().str.zfill(6)
    manifest = {
        'rows': int(len(d)),
        'files_in': [p.name for p in paths],
        'columns': d.columns.tolist(),
        'code_col': code_col, 'date_col': date_col,
        'est_fields_guess': est_cols,
        'date_range': [str(d['est_date'].min()), str(d['est_date'].max())],
        'n_codes': int(d['ts_code'].nunique()),
        'years': (d.groupby(d['est_date'].dt.year).size()
                  .to_dict() if d['est_date'].notna().any() else {}),
        'delisted_samples': {s: int((bare == s).sum())
                             for s in DELISTED_SAMPLES},
    }
    (out_dir / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src', help='dta 文件或目录')
    ap.add_argument('--out', default=str(ROOT / 'data' / 'consensus'))
    a = ap.parse_args()
    src = Path(a.src)
    paths = sorted(src.glob('*.dta')) if src.is_dir() else [src]
    m = ingest(paths, Path(a.out))
    print(json.dumps({k: m[k] for k in
                      ['rows', 'n_codes', 'date_range', 'delisted_samples']},
                     ensure_ascii=False))
    ok = all(v > 0 for v in m['delisted_samples'].values())
    print('ACCEPTANCE:', 'PASS' if ok else 'FAIL — delisted samples missing')


if __name__ == '__main__':
    sys.exit(main())
