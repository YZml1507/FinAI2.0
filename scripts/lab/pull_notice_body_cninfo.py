#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""公告正文采集器 v2（巨潮 cninfo PDF 源）——东财 np-cnotice 被封后的替代通道。

东财正文 API(np-cnotice)2026-09-23 起对数据中心 IP 整体 567 封禁
（双出口 IP 复测同结论）。本脚本走权威源：巨潮 hisAnnouncement 查询
→ adjunctUrl → static.cninfo.com.cn PDF → PyMuPDF 文本抽取。

流程: notice_meta 白名单行(code+title+ann_date) → 按 (股票,年) 聚合
      → cninfo 分页索引{title→adjunctUrl} → 标题匹配 → 下载PDF抽文本
      → data/notice_body/{year}.parquet（沿用旧 schema 加 pdf_url）。
断点: done_codes_mt.jsonl 按 art_code 行级追加（与 mt 版共享断点集）。

用法: pull_notice_body_cninfo.py --years 2018,2019 --workers 8
      [--limit-stocks 20]（冒烟用）
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

BODY_TYPES = ['年度报告全文', '半年度报告', '季度报告', '业绩快报',
              '业绩预告', '重大事项', '停牌', '复牌', '诉讼', '仲裁',
              '风险提示', '退市', '违规', '处罚', '警示', '问询',
              '关注函', '监管', '立案调查']

QUERY = 'http://www.cninfo.com.cn/new/hisAnnouncement/query'
PDF_BASE = 'http://static.cninfo.com.cn/'

_tls = threading.local()
_done_lock = threading.Lock()
_q_lock = threading.Semaphore(4)  # cninfo 查询并发上限（礼貌）


def _sess() -> requests.Session:
    s = getattr(_tls, 's', None)
    if s is None:
        s = requests.Session()
        s.headers['User-Agent'] = 'Mozilla/5.0'
        s.headers['Referer'] = 'http://www.cninfo.com.cn/new/index'
        _tls.s = s
    return s


ORGID_FILE = OUT_DIR / 'orgid_map.json'
_orgid_lock = threading.Lock()
_orgid_map: dict | None = None
TOP_SEARCH = 'http://www.cninfo.com.cn/new/information/topSearch/query'


def _orgid_cache() -> dict:
    global _orgid_map
    if _orgid_map is None:
        _orgid_map = (json.loads(ORGID_FILE.read_text())
                      if ORGID_FILE.exists() else {})
    return _orgid_map


def _resolve_orgid(code: str) -> str | None:
    """topSearch 官方解析 code→orgId（处理 99xxxxx 非 gssz 式）。"""
    try:
        r = _sess().post(TOP_SEARCH,
                         data={'keyWord': code, 'maxSecNum': 10},
                         headers={'User-Agent': 'Mozilla/5.0',
                                  'X-Requested-With': 'XMLHttpRequest'},
                         timeout=15)
        for s in r.json():
            if str(s.get('code')) == code:
                return s.get('orgId')
    except Exception:  # noqa: BLE001
        return None
    return None


def _org_id(code: str) -> str:
    c = code.zfill(6)
    m = _orgid_cache()
    if c in m:
        return m[c]
    org = _resolve_orgid(c)
    if not org:
        org = f'gssh0{c}' if c[0] in '69' else f'gssz0{c}'
    with _orgid_lock:
        m[c] = org
        ORGID_FILE.write_text(json.dumps(m, ensure_ascii=False))
    return org


def want(atype) -> bool:
    return any(k in str(atype) for k in BODY_TYPES)


def art_code(url) -> str | None:
    m = re.search(r'(AN\d{15,})', str(url))
    return m.group(1) if m else None


def _norm(title) -> str:
    return re.sub(r'[\s　（）()<>《》：:·,.，。\-—_、/\\]+', '', str(title))


def _norm_suffix(title) -> str:
    """东财标题带 '证券简称:' 前缀时剥掉首段再归一。"""
    return _norm(re.split(r'[:：]', str(title), maxsplit=1)[-1])


def cninfo_index(code: str, year: int) -> list[dict]:
    """该股该年全部公告 [{title,adjunctUrl,time}]，分页拉满。"""
    out = []
    page = 1
    while True:
        with _q_lock:
            for attempt in range(4):
                try:
                    r = _sess().post(QUERY, data={
                        'pageNum': page, 'pageSize': 30,
                        'tabName': 'fulltext', 'column': '',
                        'stock': f'{code},{_org_id(code)}',
                        'searchkey': '', 'secid': '', 'plate': '',
                        'category': '', 'trade': '',
                        'seDate': f'{year}-01-01~{year}-12-31',
                        'sortName': '', 'sortType': '',
                        'isHLtitle': 'true'}, timeout=20)
                    j = r.json()
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(2.0 * (attempt + 1))
            else:
                return out
        anns = j.get('announcements') or []
        for a in anns:
            out.append({'title': a.get('announcementTitle') or '',
                        'url': a.get('adjunctUrl') or '',
                        't': a.get('announcementTime') or 0})
        total = j.get('totalAnnouncement') or 0
        if page * 30 >= total or not anns:
            return out
        page += 1


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


def _mark_fail(code: str, why: str):
    with _done_lock:
        with FAIL.open('a') as fh:
            fh.write(json.dumps({'art': code, 'why': why},
                                ensure_ascii=False) + '\n')


def _job(meta_row, idx_by_title):
    """匹配标题→下PDF→抽文本。返回 row dict 或 None。"""
    ac, code, name, title, atype, ann_date = meta_row
    cand = idx_by_title.get(_norm(title)) or \
        idx_by_title.get(_norm_suffix(title))
    if not cand:  # 模糊兜底：前缀差异/截断标题常见
        probe = _norm_suffix(title)[:18]
        for nt, u in idx_by_title.items():
            if probe and probe in nt:
                cand = u
                break
    if not cand:
        return None, 'nomatch'
    txt = _pdf_text(cand)
    if txt is None:
        return None, 'pdf_fail'
    if not txt.strip():
        return None, 'empty'
    return {'art_code': ac, 'code': code, 'name': name,
            'title': title, 'atype': atype,
            'ann_date': str(ann_date)[:10],
            'text': txt, 'pdf_url': cand}, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', default='')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--limit-stocks', type=int, default=0)
    ap.add_argument('--code-prefix', default='',
                    help='逗号分隔股票代码前缀（如 0,1,2）；缺省全量。'
                         '用于同年代码面拆多车道')
    ap.add_argument('--out-suffix', default='',
                    help='输出文件名后缀（如 _d → 2024_d.parquet）；'
                         '并行同年代道防写冲突必传')
    a = ap.parse_args()
    years = set(a.years.split(',')) if a.years else None
    prefixes = tuple(p for p in a.code_prefix.split(',') if p)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    done = _load_done()

    # 1) 聚合白名单元数据 → (stock,year) -> rows
    groups: dict[tuple[str, int], list] = {}
    for f in sorted(glob.glob(str(META_DIR / '*.parquet'))):
        if years and not any(y in Path(f).name for y in years):
            continue
        d = pd.read_parquet(f)
        d['art_code'] = d['网址'].map(art_code)
        d = d[d['art_code'].notna() & d['公告类型'].map(want)]
        d = d[~d['art_code'].isin(done)]
        for r in d.itertuples(index=False):
            ac = r.art_code
            yr = int(str(r.公告日期)[:4])
            code = str(r.代码).zfill(6)
            if prefixes and not code.startswith(prefixes):
                continue
            groups.setdefault((code, yr), []).append(
                (ac, code, r.名称, r.公告标题, r.公告类型, r.公告日期))
    keys = sorted(groups)
    if a.limit_stocks:
        keys = keys[:a.limit_stocks]
    print(f'{(len(keys))} stock-year groups, cum_done={len(done)}',
          flush=True)

    n_ok = n_miss = 0
    for code, yr in keys:
        rows_meta = groups[(code, yr)]
        idx = cninfo_index(code, yr)
        idx_by_title = {_norm(a['title']): a['url'] for a in idx
                        if a['url']}
        rows = []
        with ThreadPoolExecutor(max_workers=a.workers) as pool:
            futs = {pool.submit(_job, m, idx_by_title): m[0]
                    for m in rows_meta}
            for fut in as_completed(futs):
                ac = futs[fut]
                try:
                    row, why = fut.result()
                except Exception:  # noqa: BLE001
                    row, why = None, 'exc'
                if row:
                    rows.append(row)
                    _mark_done(ac, done)
                else:
                    _mark_fail(ac, why or 'unk')
        out_f = OUT_DIR / f'{yr}{a.out_suffix}.parquet'
        if rows:
            buf = (pd.read_parquet(out_f) if out_f.exists()
                   else pd.DataFrame())
            pd.concat([buf, pd.DataFrame(rows)]
                      ).to_parquet(out_f, index=False)
        n_ok += len(rows)
        n_miss += len(rows_meta) - len(rows)
        print(f'{code} {yr}: +{len(rows)}/{len(rows_meta)} '
              f'cum_ok={n_ok} miss={n_miss}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
