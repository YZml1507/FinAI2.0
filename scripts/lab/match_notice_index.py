"""e62 换道: p_info3085 公告索引直链 → 匹配未覆盖白名单 → 下载 PDF 抽正文。

Phase A (--match): 输出 data/cninfo_notice_index/match.parquet
    {art_code, code, ann_date, title, pdf_url}
Phase B (--fetch): 逐条下载 PDF → PyMuPDF 抽文本 → canonical notice_body 分片
    {art_code, code, name, title, atype, ann_date, text, pdf_url} → {year}_ci.parquet
"""
import argparse, glob, re, sys, time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
BT = ['年度报告全文', '半年度报告', '季度报告', '业绩快报', '业绩预告',
      '重大事项', '停牌', '复牌', '诉讼', '仲裁', '风险提示', '退市',
      '违规', '处罚', '警示', '问询', '关注函', '监管', '立案调查']


def _norm(t):
    return re.sub(r'[\s　（）()<>《》：:·,.，。\-—_、/\\]+', '', str(t))


def _suf(t):
    return _norm(re.split(r'[:：]', str(t), maxsplit=1)[-1])


def _dy(t):
    return _norm(str(t).replace('年', '').replace('全文', '').replace('摘要', ''))


def art(u):
    m = re.search(r'(AN\d{15,})', str(u))
    return m.group(1) if m else None


def covered_acs():
    covered = set()
    shard_re = re.compile(r'(?:rec_)?(\d{4})(?:_s\d+[a-z]?|_d|_l\d+|_fc|_ci)?\.parquet$')
    for f in glob.glob(str(ROOT / 'data/notice_body/*.parquet')):
        if shard_re.match(Path(f).stem):
            try:
                covered.update(pd.read_parquet(f, columns=['art_code'])['art_code'])
            except Exception:
                pass
    return covered


def build_whitelist(years=None):
    rows = []
    for f in sorted(glob.glob(str(ROOT / 'data/notice_meta/*.parquet'))):
        if not Path(f).stem.isdigit():
            continue
        d = pd.read_parquet(f, columns=['网址', '公告类型', '公告日期', '代码', '公告标题', '名称'])
        d['ac'] = d['网址'].map(art)
        d = d[d['ac'].notna() & d['公告类型'].map(lambda a: any(k in str(a) for k in BT))]
        if years:
            d = d[d['公告日期'].astype(str).str[:4].astype(int).isin(years)]
        rows.append(d)
    meta = pd.concat(rows, ignore_index=True)
    meta['code'] = meta['代码'].astype(str).str.zfill(6)
    meta['ad'] = meta['公告日期'].astype(str).str[:10]
    return meta


def build_index():
    rows = []
    for f in sorted(glob.glob(str(ROOT / 'data/cninfo_notice_index/*.parquet'))):
        if Path(f).stem in ('match', 'done_keys') or not Path(f).stem[:4].isdigit():
            continue
        d = pd.read_parquet(f, columns=['SECCODE', 'F001D', 'F002V', 'F003V', 'F004V'])
        d = d[d['F004V'] == 'PDF']
        rows.append(d)
    idx = pd.concat(rows, ignore_index=True)
    idx['code'] = idx['SECCODE'].astype(str).str.zfill(6)
    idx['ad'] = idx['F001D'].astype(str).str[:10]
    idx['nt'] = idx['F002V'].map(_norm)
    idx['dyt'] = idx['F002V'].map(_dy)
    return idx


def do_match(years):
    meta = build_whitelist(years)
    covered = covered_acs()
    meta = meta[~meta['ac'].isin(covered)]
    print('uncovered whitelist:', len(meta))
    idx = build_index()
    print('cninfo index rows:', len(idx))
    by_code = {}
    for code, ad, nt, dyt, pdf, title in zip(idx['code'], idx['ad'], idx['nt'], idx['dyt'],
                                             idx['F003V'], idx['F002V']):
        by_code.setdefault(code, []).append((ad, nt, dyt, pdf, title))
    out = []
    hit = 0
    for ac, code, ad, title in zip(meta['ac'], meta['code'], meta['ad'], meta['公告标题']):
        cands = by_code.get(code)
        if not cands:
            continue
        em_s = _suf(title)
        em_d = _dy(em_s)
        d0 = pd.Timestamp(ad)
        best = None
        for cad, nt, dyt, pdf, ctitle in cands:
            if abs((pd.Timestamp(cad) - d0).days) > 2:
                continue
            ok = (em_s and (em_s == nt or (len(em_s) >= 10 and em_s in nt))
                  ) or (em_d and (em_d == dyt or (len(em_d) >= 10 and (em_d in dyt or dyt in em_d))))
            if ok:
                best = (cad, ctitle, pdf)
                break
        if best:
            cad, ctitle, pdf = best
            out.append({'art_code': ac, 'code': code, 'ann_date': cad,
                        'title': ctitle, 'pdf_url': pdf})
            hit += 1
    print('matched:', hit)
    df = pd.DataFrame(out).drop_duplicates('art_code')
    df.to_parquet(ROOT / 'data/cninfo_notice_index/match.parquet', index=False)
    print('wrote match.parquet', len(df))


def do_fetch(workers=6, suffix='_ci'):
    import concurrent.futures as cf
    m = pd.read_parquet(ROOT / 'data/cninfo_notice_index/match.parquet')
    done_f = ROOT / 'data/cninfo_notice_index/done_ac.txt'
    done = set(done_f.read_text().split()) if done_f.exists() else set()
    m = m[~m['art_code'].isin(done)]
    print('to fetch:', len(m))
    try:
        import fitz
    except ImportError:
        sys.exit('pip install pymupdf')

    def one(r):
        try:
            r_ = requests.get(r['pdf_url'], timeout=30,
                              headers={'User-Agent': 'Mozilla/5.0'})
            if r_.status_code != 200 or len(r_.content) < 500:
                return r['art_code'], None
            doc = fitz.open(stream=r_.content, filetype='pdf')
            txt = '\n'.join(p.get_text() for p in doc)
            doc.close()
            return r['art_code'], txt
        except Exception:
            return r['art_code'], None

    buf = {}
    n_ok = n_bad = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, row): row for _, row in m.iterrows()}
        for i, fut in enumerate(cf.as_completed(futs)):
            row = futs[fut]
            ac, txt = fut.result()
            with open(done_f, 'a') as f:
                f.write(ac + '\n')
            if txt and len(txt) > 200:
                y = str(row['ann_date'])[:4]
                buf.setdefault(y, []).append({
                    'art_code': ac, 'code': row['code'], 'name': '',
                    'title': row['title'], 'atype': '', 'ann_date': row['ann_date'],
                    'text': txt, 'pdf_url': row['pdf_url']})
                n_ok += 1
            else:
                n_bad += 1
            if (i + 1) % 200 == 0:
                for y, rows in buf.items():
                    p = ROOT / f'data/notice_body/{y}{suffix}.parquet'
                    df = pd.DataFrame(rows)
                    if p.exists():
                        df = pd.concat([pd.read_parquet(p), df]).drop_duplicates('art_code')
                    df.to_parquet(p, index=False)
                buf = {}
                print(f'{i+1}/{len(m)} ok={n_ok} bad={n_bad}', flush=True)
    for y, rows in buf.items():
        p = ROOT / f'data/notice_body/{y}{suffix}.parquet'
        df = pd.DataFrame(rows)
        if p.exists():
            df = pd.concat([pd.read_parquet(p), df]).drop_duplicates('art_code')
        df.to_parquet(p, index=False)
    print('DONE ok=', n_ok, 'bad=', n_bad)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--match', action='store_true')
    ap.add_argument('--fetch', action='store_true')
    ap.add_argument('--years', default='')
    ap.add_argument('--workers', type=int, default=6)
    a = ap.parse_args()
    years = [int(y) for y in a.years.split(',') if y] or None
    if a.match:
        do_match(years)
    if a.fetch:
        do_fetch(a.workers)
