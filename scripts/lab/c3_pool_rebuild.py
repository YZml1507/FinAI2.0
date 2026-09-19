#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C3 池重选：P1 七规则 × PIT 全 A 宇宙 → 年度池快照（docs/C3_POOL_RESELECT_PREREG.md）。

快照日 = 每自然年首个交易日（2015 用 2015-01-05；PIT 等价「上年末」口径，
⛔ 不许用年末数据回选当年）。规则逐项本仓口径：

  R1 dv_ratio ≥ 3      —— daily_basic 分片当日截面（data/daily_basic_alla/）
  R2 ROE > 0           —— 最近已公告年报（pub_date ≤ 快照日, stat_date 止 1231）
  R3 净利润同比 > 0     —— 同一行
  R4 资产负债率 < 80    —— 同一行
  R5 支付率 ∈ (0,1)    —— Σcash_div(税前, ann_date ≤ 快照日, end_date=最近财年)
                         / eps(同财年)（ann_date 过滤 = 只算已公告，PIT 严）
  R6 近一年跌幅 >50% 剔 —— daily_basic close 截面（原始价口径，登记简化）
  R7 行业权重 ≤ 20%    —— 当前快照标签（登记简化）；超限行业按 dv 降序截断，
                         「未知行业」单列；cap=max(1,floor(0.2×N))

产物：data/c3_pool/pool_yearly.parquet（{year, symbols}）+ manifest
（每年逐规则存活数 = 预登记冻结所需的本仓复核证据）。幂等确定性输出。

用法：.venv/bin/python scripts/lab/c3_pool_rebuild.py [--years 2015-2024]
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DV_DIR = ROOT / 'data/daily_basic_alla'
FINA_DIR = ROOT / 'data/financial_pit_alla'
DIV_DIR = ROOT / 'data/dividend_events_alla'
INDUSTRY = ROOT / 'data/pool_meta/stock_industry.parquet'
STOCK_BASIC = ROOT / 'data/stock_basic_cache.parquet'
OUT_DIR = ROOT / 'data/c3_pool'
OUT = OUT_DIR / 'pool_yearly.parquet'
MANIFEST = OUT_DIR / 'C3_POOL_MANIFEST.json'

DV_MIN = 3.0
DEBT_MAX = 80.0
DROP_EXCLUDE = -0.50
IND_CAP = 0.20


def to_repo(ts: str) -> str:
    return ts[-2:].lower() + '.' + ts[:6]


def snapshot_days() -> dict[int, str]:
    """每年首个 daily_basic 分片日 = 年首快照。"""
    days = sorted(p.stem for p in DV_DIR.glob('*.parquet'))
    out = {}
    for d in days:
        y = int(d[:4])
        out.setdefault(y, d)
    return out


def alive_at(day_yyyymmdd: str) -> set:
    sb = pd.read_parquet(STOCK_BASIC)
    sb = sb[sb['type'].astype(str).str.strip() == '1']
    ipo = sb['ipoDate'].astype(str).str.replace('-', '', regex=False).str[:8]
    out = sb['outDate'].astype(str).str.replace('-', '', regex=False).str[:8]
    m = (ipo <= day_yyyymmdd) & ((out == '') | (out > day_yyyymmdd))
    return set(sb.loc[m, 'code'])


def load_fina(sym: str) -> pd.DataFrame:
    p = FINA_DIR / f'{sym}.parquet'
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def latest_annual(fina: pd.DataFrame, day: str):
    if fina.empty:
        return None
    ann = fina[fina['stat_date'].astype(str).str[5:10] == '12-31']
    ann = ann[pd.to_datetime(ann['pub_date']).dt.strftime('%Y%m%d') <= day]
    if ann.empty:
        return None
    return ann.sort_values('stat_date').iloc[-1]


def payout_ok(sym: str, stat_year: int, eps: float, day: str) -> bool:
    if not eps or eps <= 0:
        return False
    p = DIV_DIR / f'{sym}.parquet'
    if not p.exists():
        return False
    ev = pd.read_parquet(p)
    if ev.empty or 'cash_div' not in ev.columns:
        return False
    m = (ev['end_date'].astype(str).str[:4] == str(stat_year)) \
        & (ev['ann_date'].astype(str).str[:8] <= day) \
        & (ev['div_proc'].astype(str) == '实施')
    divs = pd.to_numeric(ev.loc[m, 'cash_div'], errors='coerce').sum()
    if divs <= 0:
        return False
    return 0.0 < float(divs) / float(eps) < 1.0


def base_partition_day(snap: str) -> str | None:
    """距快照约一年前的最近 daily_basic 分片日（≤去年同期日）。"""
    target = str(int(snap[:4]) - 1) + snap[4:]
    days = [p.stem for p in DV_DIR.glob('*.parquet') if p.stem <= target]
    return max(days) if days else None


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', default='2015-2024')
    args = ap.parse_args()
    y0, y1 = (int(x) for x in args.years.split('-'))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    snaps = {y: d for y, d in snapshot_days().items() if y0 <= y <= y1}
    industry_map = (pd.read_parquet(INDUSTRY)
                    .set_index('ts_code')['industry'].to_dict())

    pool_rows, attrition = [], []
    for year, snap in sorted(snaps.items()):
        dv = pd.read_parquet(DV_DIR / f'{snap}.parquet')
        dv['code'] = dv['ts_code'].map(to_repo)
        alive = alive_at(snap)
        n = {'universe_alive': len(alive), 'dv_rows': len(dv)}
        cand = dv[dv['code'].isin(alive) & dv['dv_ratio'].notna()
                  & (dv['dv_ratio'] >= DV_MIN)].copy()
        n['R1_dv'] = len(cand)

        survivors = []  # (sym, dv, close_now)
        fina_cache: dict[str, object] = {}
        for r in cand.itertuples():
            sym = r.code
            if sym not in fina_cache:
                fina_cache[sym] = latest_annual(load_fina(sym), snap)
            row = fina_cache[sym]
            if row is None:
                continue
            if not (row['roe'] > 0 and row['net_profit_yoy'] > 0
                    and row['debt_to_assets'] < DEBT_MAX):
                continue
            if not payout_ok(sym, int(str(row['stat_date'])[:4]),
                             row.get('eps'), snap):
                continue
            survivors.append((sym, float(r.dv_ratio), float(r.close)))
        n['R2R5_quality'] = len(survivors)

        # R6：近一年跌幅 >50% 剔除——基期 = 距快照约一年前的最近 daily_basic
        # 分片（原始价口径）；基期缺失（上市不足一年/历史停牌）不剔除。
        base_day = base_partition_day(snap)
        base_px = {}
        if base_day:
            bd = pd.read_parquet(DV_DIR / f'{base_day}.parquet')
            bd['code'] = bd['ts_code'].map(to_repo)
            base_px = dict(zip(bd['code'], bd['close']))
        kept6 = []
        for s, v, px in survivors:
            bp = base_px.get(s)
            if bp and px and (px / bp - 1.0) < DROP_EXCLUDE:
                continue
            kept6.append((s, v))
        survivors = kept6
        n['R6_drop'] = len(survivors)

        # R7：行业 ≤20%（当前快照标签；超限行业按 dv 降序截断至 ≤20%，
        # 「未知」单列；迭代至无行业超限——分母随截断收缩）
        ind = {s: industry_map.get(
            f"{s.split('.')[1]}.{s.split('.')[0].upper()}", '未知')
            for s, _ in survivors}
        survivors.sort(key=lambda x: -x[1])
        kept = list(survivors)
        while kept:
            cnt = {}
            for s, _ in kept:
                cnt[ind[s]] = cnt.get(ind[s], 0) + 1
            cap = max(1, int(len(kept) * IND_CAP))
            over = [i for i, c in cnt.items() if c > cap]
            if not over:
                break
            worst = max(over, key=lambda i: cnt[i])
            for j in range(len(kept) - 1, -1, -1):
                if ind[kept[j][0]] == worst and cnt[worst] > cap:
                    kept.pop(j)
                    cnt[worst] -= 1
        n['R7_industry_cap'] = int(len(kept) * IND_CAP)
        n['final_pool'] = len(kept)
        attrition.append({'year': year, 'snapshot': snap, **n})
        pool_rows.append({'year': year,
                          'symbols': sorted(s for s, _ in kept)})
        print(f'[c3] {year} snap={snap} dv≥3%={n["R1_dv"]} '
              f'quality={n["R2R5_quality"]} final={n["final_pool"]}',
                  flush=True)

    out = pd.DataFrame(pool_rows)
    tmp = OUT.with_suffix('.parquet.tmp')
    out.to_parquet(tmp, index=False)
    tmp.rename(OUT)
    sha = hashlib.sha256(OUT.read_bytes()).hexdigest()
    MANIFEST.write_text(json.dumps(
        {'sha256': sha, 'snap_rule': 'year-first-trading-day',
         'attrition': attrition}, ensure_ascii=False, indent=1),
        encoding='utf-8')
    print(f'[c3] done -> {OUT} sha={sha[:16]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
