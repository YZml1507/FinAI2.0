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
    """A1/A4：区间内逐行核对 ±5% 规则语义（非宽桶近似——4.8~5.0 未触板日本不应标）。

    逐行断言：in-interval 行 limit_up == pct>=5-eps 且 limit_down == pct<=-5+eps；
    区间外行 limit_up == pct>=10-eps（主板档，不误用 5%）。
    """
    print('== A1/A4：ST 史票 ±5% 规则逐行核对 + mark_limit_flags 复核 ==')
    EPS = 1e-6
    total_touch = bad_up = bad_down = bad_out = 0
    for sym in pool_st_symbols(intervals):
        df = load_pool(sym)
        if df.empty:
            continue
        flagged_df = mark_limit_flags(df)
        in_st = isst_series(df['date'], intervals[sym])
        pct = (df['close'] - df['preclose']) / df['preclose'] * 100.0
        valid = df['preclose'].notna() & (df['preclose'] != 0)
        exp_up_in = valid & (pct >= 5.0 - EPS)
        exp_dn_in = valid & (pct <= -5.0 + EPS)
        exp_up_out = valid & (pct >= 10.0 - EPS)
        touch_in = int((in_st & (exp_up_in | exp_dn_in)).sum())
        total_touch += touch_in
        bad_up += int((in_st & (flagged_df['limit_up'] != exp_up_in)).sum())
        bad_down += int((in_st & (flagged_df['limit_down'] != exp_dn_in)).sum())
        bad_out += int((~in_st & (flagged_df['limit_up'] != exp_up_out)).sum())
        if bad_up + bad_down + bad_out:
            m = in_st & ((flagged_df['limit_up'] != exp_up_in)
                         | (flagged_df['limit_down'] != exp_dn_in))
            sample = df.loc[m, 'date'].head(3).tolist()
            check(f'A1 {sym}', False,
                  f'up错{bad_up}/dn错{bad_down}/out错{bad_out} 样例{sample}')
            break
    check('A1 区间内 ±5% 逐行核对（触板日全覆盖且语义一致）',
          bad_up == 0 and bad_down == 0 and total_touch > 0,
          f'触板日 {total_touch}，up错{bad_up} dn错{bad_down}')
    check('A4 区间外不误用 5% 档（主板 10% 语义保持）', bad_out == 0,
          f'区间外 up 错标 {bad_out}')


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
    # A2c：非主板 ST 史票按设计不回写（其涨跌幅档由板块规则定，isST 不参与）——
    # 全须保持 '0'（口径边界：创业板±20%/科创板±20%/北交所±30% 无 5% ST 档）
    nc = pd.read_parquet(NC)
    st_names = nc[nc['name'].str.upper().str.contains('ST', na=False)]
    from scripts.lab.rebuild_isst_from_namechange import is_main_board
    non_main_st = sorted({c for c in st_names['code']
                          if not is_main_board(c)} & set(syms))
    bad3 = []
    for sym in non_main_st:
        df = load_pool(sym)
        if not df.empty and (df['isST'].astype(str) != '0').any():
            bad3.append(sym)
    check('A2c 非主板 ST 史票按设计全 0（无 5% 档适用）', not bad3,
          f'池内非主板 ST 史票 {len(non_main_st)} 只，异常 {bad3}')


def a3(intervals: dict) -> None:
    print('== A3：*ST柳化（sh.600423）边界抽验 ==')
    df = load_pool('sh.600423')
    d = df['date'].astype(str).str[:10]
    # 边界日遇停牌无 bar ⇒ 核「区间转移点」：边界两侧最近 bar 的 isST 须翻转正确
    transitions = [  # (截止日, 该日及前 isST, 次日及后 isST)
        ('2017-05-02', '0', '1'),   # 2017-05-03 起 *ST
        ('2019-12-19', '1', '1'),   # *ST→ST 连续（同为 ST）
        ('2021-05-19', '1', '0'),   # 2021-05-20 摘帽
        ('2026-04-27', '0', '1'),   # 2026-04-28 起 *ST
    ]
    bad = []
    for edge, want_before, want_after in transitions:
        before = df.loc[d <= edge].tail(1)
        after = df.loc[d > edge].head(1)
        if before.empty or after.empty:
            bad.append(f'{edge} 两侧缺 bar')
            continue
        gb, ga = str(before['isST'].iloc[0]), str(after['isST'].iloc[0])
        db, da = str(before['date'].iloc[0])[:10], str(after['date'].iloc[0])[:10]
        if gb != want_before or ga != want_after:
            bad.append(f'{edge}: 前 {db}={gb}(want {want_before}) '
                       f'后 {da}={ga}(want {want_after})')
    check('A3 边界转移点核对（停牌缺 bar 自动跳过）', not bad, '; '.join(bad))
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
