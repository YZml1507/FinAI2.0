#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C3 池扩容·补采①：全 A 财务指标 PIT（datahubco tushare fina_indicator）。

范围：bar 宇宙 = market-breadth-a/daily_bars（5220 在市）∪ delisted_bars
（253 退市）共 5473 只——池候选只能是有 bar 的票；现池 487 只亦重采，
保证本目录单一来源口径（不混 citydata 旧数）。

产物：data/financial_pit_alla/{sym}.parquet（每票一文件、原子写、断点续采
——已存在的直接跳过；整表不重采）。

字段映射（fina_indicator → 现有 financial_pit schema + eps）：
  pub_date ← ann_date（真实公告日，⛔ PIT 唯一对齐键，永不 end_date）
  stat_date ← end_date（报告期，仅元数据）
  roe / netprofit_yoy→net_profit_yoy / dt_netprofit_yoy→deducted_net_profit_yoy
  debt_to_assets / ocfps→cash_flow_per_share / eps→eps（C3 支付率分母）
  source='tushare:datahubco'

用法：.venv/bin/python scripts/lab/collect_fina_pit_alla.py [--limit N]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / 'data/financial_pit_alla'
REPORT = OUT_DIR / 'FINA_ALLA_COLLECTION_REPORT.json'
URL = 'http://datahubco.com/app-api/openapi/v1/tushare/fina_indicator'
sys.path.insert(0, str(ROOT))
from scripts._secrets import require_env  # noqa: E402

KEY = None  # ⛔ 不再硬编码；fetch 时经 require_env 惰性取 DATAHUBCO_API_KEY
SLEEP = 0.12
RETRIES = 3
TIMEOUT = 20
BAR_BASES = [ROOT / 'experiments/lab/market-breadth-a/daily_bars',
             ROOT / 'experiments/lab/market-breadth-a/delisted_bars']

# 输出列序 = 现有 data/financial_pit schema + eps（C3 支付率分母）
FIELD_MAP = {
    'ann_date': 'pub_date', 'end_date': 'stat_date', 'roe': 'roe',
    'netprofit_yoy': 'net_profit_yoy',
    'dt_netprofit_yoy': 'deducted_net_profit_yoy',
    'debt_to_assets': 'debt_to_assets', 'ocfps': 'cash_flow_per_share',
    'eps': 'eps',
}
COLS = ['code', 'pub_date', 'stat_date', 'roe', 'net_profit_yoy',
        'deducted_net_profit_yoy', 'debt_to_assets', 'cash_flow_per_share',
        'eps', 'source']


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    except Exception:
        return 'unknown'


def to_ts(code: str) -> str:
    mkt, num = code.split('.')
    return f'{num}.{mkt.upper()}'


def universe() -> list:
    syms = set()
    for b in BAR_BASES:
        for p in b.glob('*.parquet'):
            if p.stem.startswith(('sh.', 'sz.', 'bj.')):
                syms.add(p.stem)
    return sorted(syms)


def fetch(ts_code: str) -> tuple:
    """→ (fields, items)；datahubco 返回 {data:{fields,items}}。"""
    for attempt in range(RETRIES):
        try:
            r = requests.get(URL, params={'ts_code': ts_code},
                             headers={'X-API-Key': require_env('DATAHUBCO_API_KEY')}, timeout=TIMEOUT)
            if r.status_code == 200:
                d = r.json().get('data') or {}
                return d.get('fields') or [], d.get('items') or []
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f'fina_indicator fetch failed {ts_code}')


def to_frame(code: str, fields: list, items: list) -> pd.DataFrame:
    df = pd.DataFrame(items, columns=fields)
    out = pd.DataFrame({
        'code': code,
        'pub_date': pd.to_datetime(df['ann_date'], format='%Y%m%d',
                                   errors='coerce'),
        'stat_date': pd.to_datetime(df['end_date'], format='%Y%m%d',
                                    errors='coerce'),
        'roe': pd.to_numeric(df['roe'], errors='coerce'),
        'net_profit_yoy': pd.to_numeric(df['netprofit_yoy'], errors='coerce'),
        'deducted_net_profit_yoy': pd.to_numeric(df['dt_netprofit_yoy'],
                                               errors='coerce'),
        'debt_to_assets': pd.to_numeric(df['debt_to_assets'], errors='coerce'),
        'cash_flow_per_share': pd.to_numeric(df['ocfps'], errors='coerce'),
        'eps': pd.to_numeric(df['eps'], errors='coerce'),
        'source': 'tushare:datahubco',
    })[COLS]
    # 源偶发同 (pub_date,stat_date) 重复行（600000 实测）——保末去重，
    # 与 T107 collect_financials「保末去重」口径一致；
    # 幂等：同票重跑同内容（按 pub_date/stat_date 排序定型）
    return (out.drop_duplicates(subset=['pub_date', 'stat_date'], keep='last')
               .sort_values(['pub_date', 'stat_date']).reset_index(drop=True))


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    syms = universe()
    todo = [s for s in syms if not (OUT_DIR / f'{s}.parquet').exists()]
    if args.limit:
        todo = todo[:args.limit]
    print(f'[fina-alla] universe={len(syms)} todo={len(todo)}')
    fails, done = [], 0
    for i, sym in enumerate(todo, 1):
        try:
            fields, items = fetch(to_ts(sym))
            if not items:
                fails.append({'code': sym, 'reason': 'empty'})
            else:
                df = to_frame(sym, fields, items)
                tmp = OUT_DIR / f'{sym}.parquet.tmp'
                df.to_parquet(tmp, index=False)
                tmp.rename(OUT_DIR / f'{sym}.parquet')
                done += 1
        except Exception as e:  # noqa: BLE001
            fails.append({'code': sym, 'reason': str(e)[:200]})
        time.sleep(SLEEP)
        if i % 250 == 0 or i == len(todo):
            print(f'[fina-alla] {i}/{len(todo)} done={done} fails={len(fails)}',
                  flush=True)
    report = {
        'git_sha': git_sha(),
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'universe': len(syms), 'todo': len(todo), 'done': done,
        'fails': fails, 'key': 'ann_date→pub_date（PIT 对齐键）',
        'note': '单一来源 datahubco；与 data/financial_pit（citydata 旧数）不混用',
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                      encoding='utf-8')
    print(f'[fina-alla] done={done} fails={len(fails)} -> {REPORT}')
    return 0 if not fails else 1


if __name__ == '__main__':
    sys.exit(main())
