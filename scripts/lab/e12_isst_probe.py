#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""E12-isST 重建验收探针（重建完成后、任何重跑之前必须先全过）。

断言（对应 docs/E12_ISST_REBUILD_DESIGN.md §三）：
  A1 现池 ST 史票：ST 生效区间内的 5% 档日（|pct|∈[4.8,5.2]%）经
     mark_limit_flags 必须 100% 被标记（limit_up|limit_down）；
  A2 非 ST 票 isST 全 '0'（抽样）；ST 史票区间外 isST='0'（防误标扩散）；
  A3 *ST柳化（sh.600423）抽验：namechange 推导区间内 isST='1'、区间外 '0'，
     边界日 2021-05-19/20、2026-04-27/28 逐日核对；
  A4 mark_limit_flags 在重建帧上对 ST 生效日按 5% 档判触板（单测口径复核）；
  A5 幂等：重建脚本二次运行 summary.changed==0。

用法：.venv/bin/python scripts/lab/e12_isst_probe.py
退出码：0=全过；1=有断言失败（详情打印）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from data.cleaner import mark_limit_flags          # noqa: E402
from scripts.lab.rebuild_isst_from_namechange import (  # noqa: E402
    bar_files, load_st_intervals, isst_series, TARGETS)

NC = ROOT / 'data/namechange/namechange.parquet'
POOL = ROOT / 'data/dividend_stocks'
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    print(f'  [{"PASS" if ok else "FAIL"}] {name}' + (f' — {detail}' if detail else ''))
    if not ok:
        FAILS.append(f'{name}: {detail}')


def pool_st_symbols(intervals: dict) -> list:
    syms = {p.name for p in POOL.iterdir()
            if p.is_dir() and p.name.startswith(('sh.', 'sz.', 'bj.'))}
    return sorted(syms & set(intervals))


def load_pool(sym: str) -> pd.DataFrame:
    dfs = [pd.read_parquet(f) for f in sorted((POOL / sym).glob('*.parquet'))]
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()


def a1_a4(intervals: dict) -> None:
    print('== A1/A4：ST 史票 5% 档覆盖 + mark_limit_flags 复核 ==')
    total_band = flagged = 0
    for sym in pool_st_symbols(intervals):
        df = load_pool(sym)
        if df.empty:
            continue
        flagged_df = mark_limit_flags(df)
        in_st = isst_series(df['date'], intervals[sym])
        pct = (df['close'] - df['preclose']) / df['preclose'] * 100.0
        band = in_st & df['preclose'].notna() & (df['preclose'] != 0) \
            & (pct.abs() >= 4.8) & (pct.abs() <= 5.2)
        hit = (flagged_df['limit_up'] | flagged_df['limit_down']) & band
        total_band += int(band.sum())
        flagged += int(hit.sum())
        if int(band.sum()) != int(hit.sum()):
            missed = df.loc[band & ~hit.astype(bool), 'date'].head(5).tolist()
            check(f'A1 {sym}', False, f'{int(hit.sum())}/{int(band.sum())} 漏标 {missed}')
    check('A1 全池 ST 史票 5% 档日覆盖', flagged == total_band and total_band > 0,
          f'{flagged}/{total_band}')


def a2(intervals: dict) -> None:
    print('== A2：非 ST 票不误标 + ST 史票区间外为 0 ==')
    syms = sorted({p.name for p in POOL.iterdir()
                   if p.is_dir() and p.name.startswith(('sh.', 'sz.', 'bj.'))})
    st_syms = set(pool_st_symbols(intervals))
    non_st = [s for s in syms if s not in st_syms]
    sample = non_st[:: max(1, len(non_st) // 30)][:30]
    bad = []
    for sym in sample:
        df = load_pool(sym)
        if not df.empty and (df['isST'].astype(str) != '0').any():
            bad.append(sym)
    check('A2a 非 ST 抽样 30 票全 0', not bad, f'异常 {bad}')
    bad2 = []
    for sym in pool_st_symbols(intervals):
        df = load_pool(sym)
        if df.empty:
            continue
        out_mask = ~isst_series(df['date'], intervals[sym])
        if (df.loc[out_mask, 'isST'].astype(str) != '0').any():
            bad2.append(sym)
    check('A2b ST 史票区间外全 0', not bad2, f'异常 {bad2}')


def a3(intervals: dict) -> None:
    print('== A3：*ST柳化（sh.600423）边界抽验 ==')
    df = load_pool('sh.600423')
    d = df['date'].astype(str).str[:10]
    cases = [('2017-05-03', '1'), ('2019-12-19', '1'), ('2019-12-20', '1'),
             ('2021-05-19', '1'), ('2021-05-20', '0'),
             ('2026-04-27', '0'), ('2026-04-28', '1')]
    bad = []
    for day, want in cases:
        row = df.loc[d == day]
        if row.empty:
            bad.append(f'{day} 无 bar')
            continue
        got = str(row['isST'].iloc[0])
        if got != want:
            bad.append(f'{day}: got {got} want {want}')
    check('A3 边界日逐日核对', not bad, '; '.join(bad))
    in_st = isst_series(df['date'], intervals['sh.600423'])
    consistent = ((df['isST'].astype(str) == '1') == in_st).all()
    check('A3 全序列与 namechange 区间一致', bool(consistent))


def a5() -> None:
    print('== A5：幂等（重建二次运行 0 变更）==')
    with tempfile.TemporaryDirectory() as td:
        rpt = Path(td) / 'm.json'
        r = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/lab/rebuild_isst_from_namechange.py'),
             '--report', str(rpt)], capture_output=True, text=True, cwd=ROOT)
        if r.returncode != 0:
            check('A5 二次运行', False, r.stderr[-300:])
            return
        summary = json.loads(rpt.read_text(encoding='utf-8'))['summary']
        check('A5 二次运行 0 变更', summary['changed'] == 0,
          f"scanned={summary['scanned']} changed={summary['changed']}")


def main() -> int:
    intervals = load_st_intervals()
    print(f'[probe] namechange ST 区间票数 {len(intervals)}；'
          f'池内 ST 史票 {len(pool_st_symbols(intervals))}')
    a1_a4(intervals)
    a2(intervals)
    a3(intervals)
    a5()
    if FAILS:
        print(f'\n[RESULT] FAIL ×{len(FAILS)}')
        for f_ in FAILS:
            print('  -', f_)
        return 1
    print('\n[RESULT] ALL PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
