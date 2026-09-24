#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""二次挽回通道：cninfo_fail.jsonl 中 nomatch 的正文行
（东财压缩标题 vs 巨潮全标题 不匹配）走巨潮全文检索
fulltextSearch/full 按 "{名称} {关键词}" 直查 adjunctUrl，
候选过滤 secCode==股票代码 + |公告时间-ann_date| 最近邻 → 下 PDF 抽文本。

输出 data/notice_body/rec_{year}.parquet（同 notice_body schema），
并把成功 art_code 追加进 done_codes_mt.jsonl（与主采集器共享断点集）。

用法: recover_notice_body_fulltext.py --workers 4 [--limit 200]
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pymupdf
import requests

ROOT = Path(__file__).resolve().parents[2]
META_DIR = ROOT / 'data' / 'notice_meta'
OUT_DIR = ROOT / 'data' / 'notice_body'
DONE = OUT_DIR / 'done_codes_mt.jsonl'
LEGACY_DONE = OUT_DIR / 'done_codes.json'
FAIL = OUT_DIR / 'cninfo_fail.jsonl'
REC_FAIL = OUT_DIR / 'rec_fail.jsonl'

SEARCH = 'http://www.cninfo.com.cn/new/fulltextSearch/full'
PDF_BASE = 'http://static.cninfo.com.cn/'
EM_TAG = re.compile(r'</?em>')

_tls = threading.local()
_done_lock = threading.Lock()


def _sess() -> requests.Session:
    s = getattr(_tls, 's', None)
    if s is None:
        s = requests.Session()
        s.headers['User-Agent'] = 'Mozilla/5.0'
        s.headers['Referer'] = 'http://www.cninfo.com.cn/new/index'
        _tls.s = s
    return s


def _load_done() -> set:
    done: set = set()
    if LEGACY_DONE.exists():
        done.update(json.loads(LEGACY_DONE.read_text()))
    if DONE.exists():
        for line in DONE.read_text().splitlines():
            if line.strip():
                done.add(line.strip())
    return done


def _mark_done(code: str, done: set):
    with _done_lock:
        done.add(code)
        with DONE.open('a') as fh:
            fh.write(code + '\n')


def _mark_rec_fail(art: str, why: str):
    with _done_lock:
        with REC_FAIL.open('a') as fh:
            fh.write(json.dumps({'art': art, 'why': why},
                                ensure_ascii=False) + '\n')


def _norm(title) -> str:
    return re.sub(r'[\s　（）()<>《》：:·,.，。\-—_、/\\*\[\]()]+',
                  '', str(title))


def _strip_em(title: str) -> str:
    return EM_TAG.sub('', str(title))


def _name_candidates(name: str, ann_date: str) -> tuple[list, float]:
    """仅按股票名称全文检索（name 是最强键），±75 日窗，翻 3 页。
    返回 (原始候选列表, 中心时间戳ms)。重名他股由 secCode 过滤剔除。"""
    try:
        center = datetime.strptime(str(ann_date)[:10], '%Y-%m-%d')
    except ValueError:
        return [], 0.0
    lo = (center - timedelta(days=75)).strftime('%Y-%m-%d')
    hi = (center + timedelta(days=75)).strftime('%Y-%m-%d')
    out = []
    for page in range(1, 4):
        j = None
        for attempt in range(6):
            try:
                r = _sess().post(SEARCH, data={
                    'searchkey': str(name), 'sdate': lo, 'edate': hi,
                    'isfulltext': 'false', 'sortName': 'pubdate',
                    'sortType': 'desc', 'pageNum': page},
                    headers={'User-Agent': 'Mozilla/5.0',
                             'X-Requested-With': 'XMLHttpRequest'},
                    timeout=20)
                j = r.json()
                break
            except Exception:  # noqa: BLE001
                time.sleep(3.0 * (attempt + 1))
        if j is None:
            break
        anns = j.get('announcements') or []
        out.extend(anns)
        if not anns or len(out) >= (j.get('totalAnnouncement') or 0):
            break
    return out, center.timestamp() * 1000


def _pick(cands: list, code: str, title: str,
          ct: float) -> dict | None:
    """secCode 过滤 → 归一标题相似度(difflib) × |时间差| 打分，
    最高相似度且 ≥0.40 者胜；相似度并列取日期最近。"""
    import difflib
    want = _norm(title)
    best, best_score = None, (-1.0, float('inf'))
    for a in cands:
        if str(a.get('secCode')) != code or not a.get('adjunctUrl'):
            continue
        cn = _norm(_strip_em(a.get('announcementTitle') or ''))
        if not cn:
            continue
        sim = difflib.SequenceMatcher(None, want, cn).ratio()
        ddt = abs((a.get('announcementTime') or 0) - ct)
        score = (sim, -ddt)
        if score > best_score:
            best_score = score
            best = a
    if best is not None and best_score[0] >= 0.40:
        return best
    return None


def _pdf_text(adjunct: str, retry: int = 3) -> str | None:
    for i in range(retry):
        try:
            r = _sess().get(PDF_BASE + adjunct,
                            headers={'User-Agent': 'Mozilla/5.0'},
                            timeout=40)
            if r.status_code != 200 or len(r.content) < 500:
                time.sleep(2.0 * (i + 1))
                continue
            doc = pymupdf.open(stream=io.BytesIO(r.content),
                               filetype='pdf')
            return '\n'.join(p.get_text() for p in doc)
        except Exception:  # noqa: BLE001
            time.sleep(2.0 * (i + 1))
    return None


def _job(row) -> tuple[dict | None, str | None]:
    ac, code, name, title, atype, ann_date = row
    cands, ct = _name_candidates(name, ann_date)
    pick = _pick(cands, code, title, ct)
    if pick is None:
        return None, 'rec_nomatch'
    txt = _pdf_text(pick['adjunctUrl'])
    if txt is None:
        return None, 'rec_pdf_fail'
    if not txt.strip():
        return None, 'rec_empty'
    return {'art_code': ac, 'code': code, 'name': name,
            'title': title, 'atype': atype,
            'ann_date': str(ann_date)[:10],
            'text': txt, 'pdf_url': pick['adjunctUrl']}, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()

    done = _load_done()
    rec_done: set = set()
    if REC_FAIL.exists():
        for line in REC_FAIL.read_text().splitlines():
            if line.strip():
                rec_done.add(json.loads(line)['art'])

    fails: set = set()
    with FAIL.open() as fh:
        for line in fh:
            if line.strip():
                fails.add(json.loads(line)['art'])

    # join meta：art_code → (code,name,title,atype,ann_date)，限 {0,3,6}
    # 一次性扫描缓存 rec_join.parquet（元数据扫描 ~7min，缓存后续秒级）
    join_f = OUT_DIR / 'rec_join.parquet'
    if join_f.exists():
        jd = pd.read_parquet(join_f)
    else:
        parts = []
        files = [f for f in sorted(glob.glob(str(META_DIR / '*.parquet')))
                 if Path(f).stem.isdigit()]
        for i, f in enumerate(files):
            d = pd.read_parquet(f, columns=['网址', '代码', '名称',
                                            '公告标题', '公告类型', '公告日期'])
            d['art_code'] = d['网址'].astype(str).str.extract(
                r'(AN\d{15,})', expand=False)
            d = d[d['art_code'].isin(fails)]
            if len(d):
                parts.append(d[['art_code', '代码', '名称', '公告标题',
                                '公告类型', '公告日期']])
            if i % 500 == 0:
                print(f'scan {i}/{len(files)}', flush=True)
        jd = pd.concat(parts) if parts else pd.DataFrame(
            columns=['art_code', '代码', '名称', '公告标题',
                     '公告类型', '公告日期'])
        jd.to_parquet(join_f, index=False)
    jd = jd[~jd['art_code'].isin(done) & ~jd['art_code'].isin(rec_done)]
    rows = []
    for r in jd.itertuples(index=False):
        code = str(r.代码).zfill(6)
        if code[0] not in '036':
            continue
        rows.append((r.art_code, code, r.名称, r.公告标题,
                     r.公告类型, r.公告日期))
    if a.limit:
        rows = rows[:a.limit]
    print(f'{len(rows)} recoverable rows (done={len(done)})', flush=True)

    by_year: dict[str, list] = {}
    n_ok = n_miss = 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = {pool.submit(_job, r): r[0] for r in rows}
        for fut in as_completed(futs):
            ac = futs[fut]
            try:
                row, why = fut.result()
            except Exception:  # noqa: BLE001
                row, why = None, 'rec_exc'
            if row:
                by_year.setdefault(str(row['ann_date'])[:4], []).append(row)
                _mark_done(ac, done)
                n_ok += 1
            else:
                _mark_rec_fail(ac, why or 'unk')
                n_miss += 1
            if (n_ok + n_miss) % 500 == 0:
                print(f'progress ok={n_ok} miss={n_miss}', flush=True)
    for yr, rs in by_year.items():
        out_f = OUT_DIR / f'rec_{yr}.parquet'
        buf = pd.read_parquet(out_f) if out_f.exists() else pd.DataFrame()
        pd.concat([buf, pd.DataFrame(rs)]).to_parquet(out_f, index=False)
    print(f'DONE ok={n_ok} miss={n_miss}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
