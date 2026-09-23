#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e75 年报文本特征提取（docs/E75_AR_TEXT_PREREG.md 冻结）。

语料：data/annual_reports_txt/{年份目录}/*.txt
文件名锚：{code}_{财年}_{简称}_{标题}_{披露日}.txt（PIT 锚=披露日）。

输出：experiments/lab/e75/features.parquet
  code, fyear, pubdate, title, path, nchars, risk_per_k, pos, neg,
  tone_net, sketch(list[int] 底64签名), is_revision, enc

MinHash-lite：每 5 字符抽样成串→步长3切片 3-gram→set→sorted→
底64 hash 值签名；同年股连财年 Jaccard = |交|/64（无偏估计）。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
AR_DIR = ROOT / 'data' / 'annual_reports_txt'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e75'

# 冻结词表（E75 附录 A —— 改动须新臂编号）
POS_WORDS = ("增长 提升 改善 盈利 突破 领先 稳健 优化 创新高 超预期 "
             "向好 强劲 扩大 巩固 回升 转型成功 高质量 充裕 受益 红利 "
             "升级 增效 回暖 稳中向好").split()
NEG_WORDS = ("下滑 亏损 风险 减值 违约 诉讼 处罚 退市 警示 承压 恶化 "
             "萎缩 逾期 冻结 受限 不确定 质疑 困境 违约风险 持续经营 "
             "大幅波动 下调 疲软 拖累").split()

# 文件名宽松解析：code_fyear_任意_YYYY-MM-DD.txt
NAME_RE = re.compile(r'^(\d{6})_(\d{4})_(.*)_(\d{4}-\d{2}-\d{2})\.txt$')
SKIP_TITLE = ('摘要', '英文', '英文版', '取消')


def _decode(raw: bytes) -> tuple[str, str]:
    """先 utf-8 后 gb18030，两败记 '?' 不猜码。"""
    for enc in ('utf-8', 'gb18030'):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace'), 'utf8-replace'


def _sketch64(text: str) -> list[int]:
    """底-64 3-gram 签名（Jaccard 无偏估计）。"""
    s = text[::5]
    grams = {hash(s[i:i + 3]) for i in range(0, len(s) - 2, 3)}
    return sorted(grams)[:64] if len(grams) >= 64 else sorted(grams)


def _count_terms(text: str) -> tuple[int, int, int]:
    risk = text.count('风险')
    pos = sum(text.count(w) for w in POS_WORDS)
    neg = sum(text.count(w) for w in NEG_WORDS)
    return risk, pos, neg


def extract(fy_min: int = 2014) -> pd.DataFrame:
    rows = []
    files = sorted(AR_DIR.glob('*/*.txt'))
    t0 = time.time()
    for k, f in enumerate(files):
        base = f.name
        m = NAME_RE.match(base)
        if not m:
            continue
        code, fy, title, pub = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        if fy < fy_min or any(x in title for x in SKIP_TITLE):
            continue
        raw = f.read_bytes()
        if not raw:
            continue
        text, enc = _decode(raw)
        n = len(text)
        risk, pos, neg = _count_terms(text)
        rows.append(dict(
            code=code, fyear=fy, title=title,
            pubdate=pd.to_datetime(pub), path=str(f.relative_to(ROOT)),
            nchars=n, risk_per_k=risk / max(n / 1000.0, 1.0),
            pos=pos, neg=neg,
            tone_net=(pos - neg) / max(pos + neg, 1),
            sketch=_sketch64(text),
            is_revision=bool(re.search(r'更新后|修订|更正|补充|取消后重新', title)),
            enc=enc))
        if (k + 1) % 10000 == 0:
            print(f"[progress] {k + 1}/{len(files)} "
                  f"({(time.time() - t0) / 60:.1f}min)", flush=True)
    df = pd.DataFrame(rows)
    # 每股每年首次披露：同 code+fyear 多条（修订/更新）取最早 pubdate
    df = df.sort_values('pubdate')
    df['is_first'] = ~df.duplicated(['code', 'fyear'], keep='first')
    return df


def yoy_similarity(df: pd.DataFrame) -> pd.Series:
    """同 code 相邻财年 sketch 底64 Jaccard；无上年报→NaN。"""
    sig = df.set_index(['code', 'fyear'])['sketch'].to_dict()
    out = []
    for code, fy in zip(df['code'], df['fyear']):
        a = sig.get((code, fy)); b = sig.get((code, fy - 1))
        if not a or not b:
            out.append(np.nan)
            continue
        inter = len(set(a) & set(b))
        out.append(inter / min(len(a), len(b), 64))
    return pd.Series(out, index=df.index, name='yoy_jac')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--fy-min', type=int, default=2014)
    ap.add_argument('--out', default=str(OUT_DIR / 'features.parquet'))
    args = ap.parse_args()
    df = extract(args.fy_min)
    print(f"[extract] rows={len(df)} codes={df['code'].nunique()} "
          f"enc={df['enc'].value_counts().to_dict()}", flush=True)
    df['yoy_jac'] = yoy_similarity(df)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out)
    df[['code', 'fyear', 'pubdate', 'nchars', 'risk_per_k', 'tone_net',
        'yoy_jac', 'is_revision', 'is_first']].describe().to_json(
        out.with_suffix('.stats.json'))
    first = df[df['is_first']]
    print(f"[done] {out} rows={len(df)} 首次披露={len(first)} "
          f"yoy_sim覆盖={df['yoy_jac'].notna().mean():.1%}", flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
