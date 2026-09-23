#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e62 公告正文 NLP 信号构造（docs/E62_BODY_NLP_PREREG.md 冻结口径）。

输入: data/notice_body/{year}.parquet（pull_notice_body_cninfo.py 产物,
      列 art_code/code/name/title/atype/ann_date/text/pdf_url）
输出: experiments/lab/e62/sig_monthly.parquet ——
      (sig_date=月末, ts_code, nlp_loglen/nlp_ann_cnt/nlp_risk_den/
       nlp_pos_den/nlp_lit_frac)

口径（prereg §2）：逐股近 60 自然日滚动窗；月末截面快照；
无公告股 NaN 非 0；词表复用 e75_ar_text 冻结表。
nlp_type_mix 落地为 nlp_lit_frac（诉讼/处罚/问询/关注函/监管/立案类
公告占窗口内白名单公告比例，方向负向）——与 prereg §2 修订一致。
"""
from __future__ import annotations

import argparse
import glob
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.lab.e75_ar_text import NEG_WORDS, POS_WORDS  # noqa: E402
from scripts.lab.e61_signals import norm_code  # noqa: E402

SRC = ROOT / 'data' / 'notice_body'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e62'

# 诉讼/处罚/问询/监管类（负向占比口径，与 prereg type_mix 方向假设对齐）
LIT_KW = ('诉讼', '仲裁', '处罚', '问询', '关注函', '监管', '立案调查',
          '违规', '警示')
WINDOW_D = 60
MIN_OBS = 3  # 窗口内 <3 条白名单公告 → NaN（稀薄不可比）


def _count_words(text: str, words) -> int:
    return sum(text.count(w) for w in words)


def load_events() -> pd.DataFrame:
    rows = []
    for f in sorted(glob.glob(str(SRC / '20*.parquet'))):
        d = pd.read_parquet(f)
        d = d[d['text'].notna() & (d['text'].str.len() > 50)]
        if d.empty:
            continue
        d['ts_code'] = d['code'].map(norm_code)
        d = d[d['ts_code'].notna()]
        d['nchars'] = d['text'].str.len()
        d['risk_hits'] = d['text'].map(lambda t: t.count('风险'))
        d['pos_hits'] = d['text'].map(lambda t: _count_words(t, POS_WORDS))
        d['neg_hits'] = d['text'].map(lambda t: _count_words(t, NEG_WORDS))
        d['lit'] = d['atype'].map(
            lambda a: any(k in str(a) for k in LIT_KW))
        d['ann_date'] = pd.to_datetime(d['ann_date'])
        rows.append(d[['ts_code', 'ann_date', 'nchars', 'risk_hits',
                       'pos_hits', 'neg_hits', 'lit']])
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def build(df: pd.DataFrame) -> pd.DataFrame:
    """逐股 60 日滚动聚合 → 月末快照。"""
    outs = []
    for code, sub in df.groupby('ts_code'):
        sub = sub.set_index('ann_date').sort_index()
        g = (sub.resample('D')
             .agg(cnt=('nchars', 'size'), nchars=('nchars', 'sum'),
                  risk=('risk_hits', 'sum'), pos=('pos_hits', 'sum'),
                  neg=('neg_hits', 'sum'), lit=('lit', 'sum')))
        idx = pd.date_range(g.index.min(), g.index.max(), freq='D')
        g = g.reindex(idx).fillna(0)
        roll = g.rolling(WINDOW_D, min_periods=1).sum()
        ok = roll['cnt'] >= MIN_OBS
        kc = (roll['nchars'] / 1000.0).replace(0, np.nan)
        snap = pd.DataFrame({
            'nlp_loglen': np.where(ok, np.log1p(
                roll['nchars'] / roll['cnt'].replace(0, np.nan)),
                np.nan),
            'nlp_ann_cnt': roll['cnt'].where(ok),
            'nlp_risk_den': (roll['risk'] / kc).where(ok),
            'nlp_pos_den': (roll['pos'] / kc).where(ok),
            'nlp_lit_frac': (roll['lit']
                             / roll['cnt'].replace(0, np.nan)).where(ok),
        })
        snap = snap.resample('ME').last().dropna(how='all')
        snap['ts_code'] = code
        outs.append(snap.reset_index(names='sig_date'))
    return pd.concat(outs, ignore_index=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=str(OUT_DIR / 'sig_monthly.parquet'))
    a = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = load_events()
    if df.empty:
        print('[e62] no body rows yet')
        return 1
    print(f'[e62] events={len(df)} codes={df.ts_code.nunique()} '
          f'{df.ann_date.min().date()}~{df.ann_date.max().date()}')
    sig = build(df)
    sig.to_parquet(a.out)
    print(f'[e62] -> {a.out} rows={len(sig)} '
          f'months={sig.sig_date.nunique()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
