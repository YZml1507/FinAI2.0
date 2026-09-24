#!/usr/bin/env python3
"""FinCorpus 公告正文 ↔ notice_meta 匹配：回收 art_code + ann_date。

输入：/home/ubuntu/fincorpus_parsed.parquet (code, title, year, body)
      data/notice_meta/*.parquet (代码, 公告标题, 公告类型, 公告日期, 网址)
输出：data/notice_body/{year}_fc.parquet (art_code, code, ann_date, title, body)

匹配：归一化标题精确匹配 → 前后缀剥名匹配 → 同日任一公告兜底（按标题相似度）。
"""
import glob, re, sys
from pathlib import Path
import pandas as pd

ROOT = Path('/home/ubuntu/repos/FinAI2-0')

def _norm(t):
    return re.sub(r'[\s　（）()<>《》：:·,.，。\-—_、/\\]+', '', str(t))

def _norm_suffix(t):
    return _norm(re.split(r'[:：]', str(t), maxsplit=1)[-1])

def _deyear(t):
    return _norm(str(t).replace('年', ''))

BT = ['年度报告全文', '半年度报告', '季度报告', '业绩快报', '业绩预告',
      '重大事项', '停牌', '复牌', '诉讼', '仲裁', '风险提示', '退市',
      '违规', '处罚', '警示', '问询', '关注函', '监管', '立案调查']

def art(u):
    m = re.search(r'(AN\d{15,})', str(u))
    return m.group(1) if m else None

def main():
    # 1) notice_meta → (code, normtitle) → (art_code, ann_date, type)
    meta_rows = []
    for f in sorted(glob.glob(str(ROOT / 'data/notice_meta/*.parquet'))):
        if not Path(f).stem.isdigit():
            continue
        d = pd.read_parquet(f, columns=['代码', '公告标题', '公告类型', '公告日期', '网址'])
        d = d[d['公告类型'].map(lambda a: any(k in str(a) for k in BT))]
        d['ac'] = d['网址'].map(art)
        d = d[d['ac'].notna()]
        meta_rows.append(d)
    meta = pd.concat(meta_rows, ignore_index=True)
    meta['nt'] = meta['公告标题'].map(_norm)
    meta['ns'] = meta['公告标题'].map(_norm_suffix)
    meta['code'] = meta['代码'].astype(str).str.zfill(6)
    print('meta whitelist rows:', len(meta))

    # 2) lookup: (code, norm) → row; per-code list for fuzzy fallback
    lut = {}
    by_code = {}
    for code, nt, ns, ac, dt, tp in zip(meta['code'], meta['nt'], meta['ns'],
                                       meta['ac'], meta['公告日期'], meta['公告类型']):
        lut.setdefault((code, nt), (ac, dt, tp))
        lut.setdefault((code, ns), (ac, dt, tp))
        by_code.setdefault(code, []).append((nt, ns, ac, dt, tp))

    # already-covered art_codes → skip them in fuzzy pass
    import glob as _g
    covered = set()
    shard_re = re.compile(r'(?:rec_)?(\d{4})(?:_s\d+[a-z]?|_d|_l\d+|_fc)?\.parquet$')
    for f in _g.glob(str(ROOT / 'data/notice_body/*.parquet')):
        if shard_re.match(Path(f).stem):
            try:
                covered.update(pd.read_parquet(f, columns=['art_code'])['art_code'])
            except Exception:
                pass
    print('already covered:', len(covered))

    # 3) fincorpus
    fc = pd.read_parquet('/home/ubuntu/fincorpus_parsed.parquet')
    fc['nt'] = fc['title'].map(_norm)
    fc['ns'] = fc['title'].map(_norm_suffix)
    out = {y: [] for y in range(2015, 2020)}
    hit = 0
    for code, nt, ns, yr, title, body in zip(fc['code'], fc['nt'], fc['ns'],
                                           fc['year'], fc['title'], fc['body']):
        if yr not in out:
            continue
        r = lut.get((code, nt)) or lut.get((code, ns))
        if r:
            ac, dt, tp = r
            out[yr].append({'art_code': ac, 'code': code, 'ann_date': str(dt)[:10],
                            'title': title, 'type': tp, 'body': body})
            hit += 1
    print('matched:', hit)
    # fuzzy pass: only for whitelist art_codes not yet covered — deyear + containment
    cands = {}
    for code, rows in by_code.items():
        todo = [(nt, ns, ac, dt, tp) for nt, ns, ac, dt, tp in rows if ac not in covered]
        if todo:
            cands[code] = todo
    fc2 = fc[fc['year'].isin(out.keys()) & fc['code'].isin(cands.keys())]
    fc2['dy'] = fc2['title'].map(_deyear)
    hit2 = 0
    for code, dy, ns, yr, title, body in zip(fc2['code'], fc2['dy'], fc2['ns'],
                                            fc2['year'], fc2['title'], fc2['body']):
        if not dy and not ns:
            continue
        best = None
        for nt, nsm, ac, dt, tp in cands[code]:
            if ac in covered:
                continue
            ntd = nt.replace('年', '')
            nsd = nsm.replace('年', '')
            ok = (dy and (dy == ntd or dy == nsd or (len(dy) >= 10 and (dy in ntd or dy in nsd)))
                  ) or (ns and len(ns) >= 10 and (ns in nt or ns in nsm))
            if ok:
                best = (ac, dt, tp)
                break
        if best:
            ac, dt, tp = best
            covered.add(ac)
            out[yr].append({'art_code': ac, 'code': code, 'ann_date': str(dt)[:10],
                            'title': title, 'type': tp, 'body': body})
            hit2 += 1
    print('fuzzy matched:', hit2)
    for yr, rows in out.items():
        if rows:
            df = pd.DataFrame(rows).drop_duplicates('art_code')
            p = ROOT / f'data/notice_body/{yr}_fc.parquet'
            df.to_parquet(p, index=False)
            print(yr, len(df), '→', p.name)

if __name__ == '__main__':
    main()
