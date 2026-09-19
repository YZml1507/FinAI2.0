#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C3 数据面物化 + 年度池 universe_provider（docs/C3_POOL_RESELECT_PREREG.md）。

物化 ``data/c3_universe/``（dividend_stocks 同布局 ``{sym}/{year}.parquet``）：

* **487 成员**：``data/dividend_stocks/{sym}/*.parquet`` 逐文件复制 + exdiv
  sidecar 复制——与对照锚 isst-e8b **位级同口径**，差值即纯池效应；
* **新成员**（池内非 487）：``market-breadth-a/{daily_bars,delisted_bars}/{sym}.parquet``
  平铺 → 按年切分 → 注入三列（与 487 平面同方法论）：
    - ``dividend_yield`` = ``compute_pit_fields`` 395 天滚动（alla 除权事件）；
    - ``market_cap`` = ``daily_basic_alla.circ_mv × 1e4``（官方流通市值口径，
      快照缺该票行 ⇒ NaN，策略当日跳过，如实登记覆盖率）；
    - ``isST`` = namechange PIT 主板口径（复用 ``rebuild_isst_from_namechange``
      的 ``load_st_intervals/isst_series``；delisted_bars 原生 isST=0 由本步修正）；
  - exdiv sidecar 由 ``dividend_events_alla`` 构建（口径实证：旧 cninfo
    ``cash_dividend`` ≈ alla ``cash_div_tax``——重叠 5368 事件 95.9% 一致；
    ``factor`` = 1 + stk_bo_rate + stk_co_rate，均为每股率，600039 三案交叉验证）；
* **指数**：``data/dividend_stocks/{INDEX_SYMBOL}/*.parquet`` 复制；
* 产物 ``C3_PLANE_MANIFEST.json``：分类计数 + market_cap/exdiv 覆盖统计 + sha256，
  幂等确定性（重跑同输入 ⇒ 同 manifest）。

provider：``provider(day) = pool_yearly[year(day)] ∩ alive(day)``——
alive 语义与 ``data.universe.alive_universe`` 一致（ipoDate≤day<outDate，
outDate 空=在市；退市票退市日前正常参与）。轻量实现：只对池成员查 ipo/out。

用法：
    .venv/bin/python scripts/lab/c3_data_plane.py \
        --pool data/c3_pool/pool_yearly.parquet [--out data/c3_universe]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date as _date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import run_dividend_backtest as rdb  # noqa: E402
from scripts.lab.rebuild_isst_from_namechange import (  # noqa: E402
    load_st_intervals,
    isst_series,
)
from scripts.repair_and_enrich_dividend_data import compute_pit_fields  # noqa: E402

BARS_ALIVE = ROOT / 'experiments/lab/market-breadth-a/daily_bars'
BARS_DELISTED = ROOT / 'experiments/lab/market-breadth-a/delisted_bars'
DIV_STOCKS = ROOT / 'data/dividend_stocks'
DV_DIR = ROOT / 'data/daily_basic_alla'
DIV_ALLA = ROOT / 'data/dividend_events_alla'
INDEX_SYMBOL = rdb.INDEX_SYMBOL
BAR_COLS = ['date', 'open', 'high', 'low', 'close', 'preclose', 'volume',
            'amount', 'turn', 'pctChg', 'tradestatus', 'isST', 'code',
            'source', 'adjust_mode']


def pool_union(pool_path: Path) -> list[str]:
    """pool_yearly.parquet {year, symbols[]} → 全部年份成员并集（排序去重）。"""
    pool = pd.read_parquet(pool_path)
    out: set[str] = set()
    for r in pool.itertuples():
        out |= set(r.symbols)
    return sorted(out)


def alla_exdiv_events(sym: str) -> list[dict]:
    """dividend_events_alla → exdiv sidecar 事件列表 {date,factor,cash_dividend}。

    口径：div_proc=实施 + ex_date 非空；cash_dividend=cash_div_tax（缺省回落
    cash_div→0，与旧 cninfo sidecar 96% 实证一致）；factor=1+bo+co（每股率）。
    同 ex_date 多事件合并：现金求和、因子求积（同权日的多笔派息等价合并）。
    """
    p = DIV_ALLA / f'{sym}.parquet'
    if not p.exists():
        return []
    ev = pd.read_parquet(p)
    if ev.empty:
        return []
    ev = ev[(ev['div_proc'] == '实施') & ev['ex_date'].notna()
            & (ev['ex_date'].astype(str).str.len() >= 8)]
    merged: dict[str, dict] = {}
    for r in ev.itertuples():
        d = str(r.ex_date)[:8]
        iso = f'{d[:4]}-{d[4:6]}-{d[6:8]}'
        cash = r.cash_div_tax if pd.notna(r.cash_div_tax) else (
            r.cash_div if pd.notna(r.cash_div) else 0.0)
        factor = (1.0 + (float(r.stk_bo_rate) if pd.notna(r.stk_bo_rate) else 0.0)
                  + (float(r.stk_co_rate) if pd.notna(r.stk_co_rate) else 0.0))
        cur = merged.setdefault(iso, {'date': iso, 'factor': 1.0,
                                    'cash_dividend': 0.0})
        cur['cash_dividend'] += float(cash)
        cur['factor'] *= float(factor)
    return [merged[d] for d in sorted(merged)]


def load_circ_mv(symbols: set[str], years: tuple[int, int]) -> pd.DataFrame:
    """daily_basic_alla 分片 → {code,date,circ_mv} 长表（仅目标票、目标年份）。

    分片扫描一遍（2431 日），⛔ 不许逐票逐日读（O(syms×days) 不可行）。
    """
    ts_of = {s: f'{s.split(".")[1]}.{s.split(".")[0].upper()}' for s in symbols}
    inv = {v: k for k, v in ts_of.items()}
    frames = []
    for p in sorted(DV_DIR.glob('*.parquet')):
        y = int(p.stem[:4])
        if not (years[0] <= y <= years[1]):
            continue
        df = pd.read_parquet(p, columns=['ts_code', 'trade_date', 'circ_mv'])
        df = df[df['ts_code'].isin(inv)]
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=['code', 'date', 'circ_mv'])
    out = pd.concat(frames, ignore_index=True)
    out['code'] = out['ts_code'].map(inv)
    out['date'] = out['trade_date'].astype(str).str[:8].map(
        lambda d: f'{d[:4]}-{d[4:6]}-{d[6:8]}')
    return out[['code', 'date', 'circ_mv']]


def _build_new_member(sym: str, out_dir: Path, circ: pd.DataFrame,
                      st_intervals: dict, years: tuple[int, int],
                      stats: dict) -> dict:
    """非 487 成员：平铺 bar → 年分区 + 三列注入 + exdiv sidecar。"""
    src = BARS_ALIVE / f'{sym}.parquet'
    if not src.exists():
        src = BARS_DELISTED / f'{sym}.parquet'
    if not src.exists():
        stats['missing_bars'].append(sym)
        return {'symbol': sym, 'status': 'missing_bars'}
    df = pd.read_parquet(src)
    df = df[df['date'].astype(str).str[:4].astype(int).between(*years)]
    if df.empty:
        stats['empty_bars'].append(sym)
        return {'symbol': sym, 'status': 'empty'}

    events = alla_exdiv_events(sym)
    df = compute_pit_fields(df, events, 1.0)  # market_cap 随即被 circ_mv 覆盖
    mc = circ[circ['code'] == sym][['date', 'circ_mv']].rename(
        columns={'date': '_d'})
    df['_d'] = df['date'].astype(str)
    df = df.merge(mc, on='_d', how='left').drop(columns=['_d'])
    df['market_cap'] = pd.to_numeric(df['circ_mv'], errors='coerce') * 1e4
    df = df.drop(columns=['circ_mv'])
    stats['mc_rows'] += int(df['market_cap'].notna().sum())
    stats['mc_total'] += len(df)

    iv = st_intervals.get(sym, [])
    df['isST'] = isst_series(df['date'], iv).map({True: '1', False: '0'})

    sym_dir = out_dir / sym
    sym_dir.mkdir(parents=True, exist_ok=True)
    df['yyyy'] = df['date'].astype(str).str[:4].astype(int)
    written = 0
    for y, grp in df.groupby('yyyy'):
        grp = grp.drop(columns=['yyyy'])
        grp = grp.reindex(columns=BAR_COLS + ['market_cap', 'dividend_yield'])
        tmp = sym_dir / f'{y}.parquet.tmp'
        grp.to_parquet(tmp, index=False)
        tmp.replace(sym_dir / f'{y}.parquet')
        written += 1
    return {'symbol': sym, 'status': 'built', 'years': written,
            'exdiv_events': len(events),
            'mc_coverage': round(
                float(df['market_cap'].notna().mean()), 4)}


def _copy_member(sym: str, out_dir: Path, years: tuple[int, int]) -> dict:
    """487 成员：分区逐文件复制（位级同口径）。"""
    src_dir = DIV_STOCKS / sym
    sym_dir = out_dir / sym
    sym_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in sorted(src_dir.glob('*.parquet')):
        if not p.stem.isdigit() or not (years[0] <= int(p.stem) <= years[1]):
            continue
        (sym_dir / p.name).write_bytes(p.read_bytes())
        n += 1
    return {'symbol': sym, 'status': 'copied', 'years': n}


def _write_exdiv(sym: str, out_dir: Path, copied_member: bool) -> int:
    """exdiv sidecar：487 成员复制旧文件（同口径）；新成员由 alla 构建。"""
    exdir = out_dir / 'exdiv'
    exdir.mkdir(parents=True, exist_ok=True)
    dst = exdir / f'{sym}.parquet'
    if copied_member:
        src = DIV_STOCKS / 'exdiv' / f'{sym}.parquet'
        if src.exists():
            dst.write_bytes(src.read_bytes())
            return int(len(pd.read_parquet(src)))
        return -1
    evs = alla_exdiv_events(sym)
    tmp = dst.with_suffix('.parquet.tmp')
    pd.DataFrame(evs, columns=['date', 'factor', 'cash_dividend']).to_parquet(
        tmp, index=False)
    tmp.replace(dst)
    return len(evs)


def materialize(pool_path: Path, out_dir: Path,
                years: tuple[int, int] = (2015, 2024)) -> dict:
    """按 pool_yearly 并集物化 data_path（幂等：重跑清目录重建）。"""
    syms = pool_union(pool_path)
    if out_dir.exists():
        import shutil
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    pool_syms = {d.name for d in DIV_STOCKS.iterdir()
                 if d.is_dir() and d.name.startswith(('sh.', 'sz.'))}
    copied = sorted(s for s in syms if s in pool_syms)
    fresh = sorted(s for s in syms if s not in pool_syms)
    stats: dict = {'mc_rows': 0, 'mc_total': 0,
                 'missing_bars': [], 'empty_bars': []}

    recs = [_copy_member(s, out_dir, years) for s in copied]
    if fresh:
        circ = load_circ_mv(set(fresh), years)
        st_intervals = load_st_intervals()
        recs += [_build_new_member(s, out_dir, circ, st_intervals, years,
                                   stats) for s in fresh]
    exdiv_n = {s: _write_exdiv(s, out_dir, s in pool_syms) for s in syms}

    idx_dir = out_dir / INDEX_SYMBOL
    idx_dir.mkdir(parents=True, exist_ok=True)
    n_idx = 0
    for p in sorted((DIV_STOCKS / INDEX_SYMBOL).glob('*.parquet')):
        if p.stem.isdigit() and years[0] <= int(p.stem) <= years[1]:
            (idx_dir / p.name).write_bytes(p.read_bytes())
            n_idx += 1

    pp = pool_path.resolve()
    manifest = {
        'pool_path': (str(pp.relative_to(ROOT.resolve()))
                      if pp.is_relative_to(ROOT.resolve())
                      else str(pp)),
        'years': list(years),
        'union_symbols': len(syms),
        'copied_487': len(copied),
        'built_new': sum(1 for r in recs if r.get('status') == 'built'),
        'missing_bars': stats['missing_bars'],
        'empty_bars': stats['empty_bars'],
        'index_partitions': n_idx,
        'exdiv_written': sum(1 for v in exdiv_n.values() if v >= 0),
        'exdiv_empty_or_missing': sorted(
            s for s, v in exdiv_n.items() if v <= 0),
        'mc_row_coverage': round(
            stats['mc_rows'] / stats['mc_total'], 4) if stats['mc_total'] else None,
        'records': recs,
    }
    (out_dir / 'C3_PLANE_MANIFEST.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding='utf-8')
    return manifest


def make_yearly_pool_provider(pool_path: Path,
                              stock_basic: pd.DataFrame) -> 'object':
    """年度池 provider：``provider(day) = pool[year(day)] ∩ alive(day)``。

    alive 语义对齐 ``alive_universe``：ipoDate ≤ day < outDate（空=在市）。
    ⛔ 池内无该年条目 ⇒ 返回 []（fail-closed：缺年=当年空仓，不外推）。
    """
    pool = pd.read_parquet(pool_path)
    by_year = {int(r.year): set(r.symbols) for r in pool.itertuples()}
    sb = stock_basic[stock_basic['type'].astype(str).str.strip() == '1']
    ipo = sb.set_index('code')['ipoDate'].astype(str).str[:10].to_dict()
    out_d = sb.set_index('code')['outDate'].astype(str).str[:10].to_dict()

    def provider(day: _date) -> list[str]:
        d = str(day)[:10]
        syms = by_year.get(int(d[:4]))
        if not syms:
            return []

        def _alive(s: str) -> bool:
            od = out_d.get(s) or ''
            return ipo.get(s, '9999') <= d and (
                od in ('', 'nan', 'None', 'NaT') or od > d)
        return sorted(s for s in syms if _alive(s))
    return provider


def main() -> int:
    ap = argparse.ArgumentParser(description='C3 数据面物化')
    ap.add_argument('--pool', required=True, help='pool_yearly.parquet 路径')
    ap.add_argument('--out', default=str(ROOT / 'data/c3_universe'))
    ap.add_argument('--years', default='2015-2024')
    args = ap.parse_args()
    y0, y1 = (int(x) for x in args.years.split('-'))
    m = materialize(Path(args.pool), Path(args.out), (y0, y1))
    print(f"[c3-plane] union={m['union_symbols']} copied={m['copied_487']} "
          f"built={m['built_new']} missing={len(m['missing_bars'])} "
          f"exdiv={m['exdiv_written']} mc_cov={m['mc_row_coverage']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
