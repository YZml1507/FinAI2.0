#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e11-linear 行为探针（E4 先例：出指标前先验执行一致性）。

复刻 strategy/candidates.py 的择时语义在真实宽度序列上回放，断言
E11_LINEAR_PREREG §六 全部 6 条。只读分析，不跑回测、不改仓。

用法：.venv/bin/python scripts/lab/e11_linear_probe.py
退出码：全部断言过 = 0；任一失败 = 1。
"""
from __future__ import annotations

import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from strategy.candidates import DividendConfig, DividendStrategy  # noqa: E402

BREADTH = ROOT / 'experiments/lab/market-breadth-a/breadth20_daily.parquet'
DEF = Decimal('0.25')
ATK = Decimal('0.35')
SPAN = ATK - DEF
REBALANCE_DAYS = 5
WARMUP_BARS = 40

_failures: list[str] = []


def check(cond: bool, label: str, detail: str = '') -> None:
    status = 'PASS' if cond else 'FAIL'
    print(f'[{status}] {label}' + (f' -- {detail}' if detail and not cond else ''))
    if not cond:
        _failures.append(label + (f' -- {detail}' if detail else ''))


def make_cfg(mode: str) -> DividendConfig:
    return DividendConfig(
        use_breadth_timing=True, use_ma200_timing=False,
        breadth_series={'2015-01-05': Decimal('0.30')},
        breadth_attack_threshold=ATK, breadth_defense_threshold=DEF,
        breadth_mid_cap=Decimal('0'),
        breadth_ice_confirm_days=1,
        breadth_demote_liquidate=True,
        breadth_weight_mode=mode,
    )


def replay(mode: str):
    """复刻 candidates.py ③.5/④ 语义：返回 (cap_seq, events, ice_dates, release_dates)。

    cap_seq: [(date, cap)] 到仓日（scheduled/demote）序列；
    events:  [(kind, date)]；ice_dates/release_dates: 确认/解除日集合。
    """
    cfg = make_cfg(mode)
    df = pd.read_parquet(BREADTH)
    bs = [Decimal(str(x)) for x in df['breadth20']]
    dates = [str(d)[:10] for d in df['date']]
    n = len(bs)

    ice = False
    streak = 0
    last_rb = 0
    prev_b = None
    cap_seq: list[tuple[str, Decimal, Decimal]] = []
    events: list[tuple[str, str]] = []
    ice_dates: list[str] = []
    release_dates: list[str] = []
    for i in range(1, n + 1):
        b = bs[i - 1]
        d = dates[i - 1]
        if i < WARMUP_BARS:
            prev_b = b
            continue
        # ③.5 冰点状态机（与 candidates.py 同序）
        if ice:
            if b >= DEF:
                ice = False
                streak = 0
                release_dates.append(d)
                events.append(('ice_release', d))
            prev_b = b
            continue
        if b < DEF:
            streak += 1
            if streak >= cfg.breadth_ice_confirm_days:
                ice = True
                streak = 0
                ice_dates.append(d)
                events.append(('ice_confirm', d))
            prev_b = b
            continue
        streak = 0
        # ④ 节拍 + demote
        demote = (cfg.breadth_demote_liquidate and prev_b is not None
                  and prev_b >= ATK and b < ATK)
        scheduled = (i - last_rb) >= REBALANCE_DAYS
        if scheduled:
            last_rb = i
        if scheduled or demote:
            cap = DividendStrategy._breadth_cap(cfg, b)
            cap_seq.append((d, b, cap))
            events.append(('rebalance' if scheduled else 'demote', d))
        prev_b = b
    return cap_seq, events, ice_dates, release_dates


def main() -> int:
    cfg = make_cfg('linear')
    cap_fn = DividendStrategy._breadth_cap

    # 断言 1：[0.25, 0.35] 严格单调递增、值域恰为 [0,1]
    grid = [DEF + SPAN * Decimal(k) / Decimal(1000) for k in range(1001)]
    vals = [cap_fn(cfg, b) for b in grid]
    check(all(vals[i] < vals[i + 1] for i in range(1000)),
          'A1 w(b) 在 [defense, attack] 严格单调递增')
    check(vals[0] == Decimal(0) and vals[-1] == Decimal(1),
          'A1b 值域恰为 [0,1]（端点闭包）', f'{vals[0]}, {vals[-1]}')

    # 断言 2：两端与 hard 完全一致（向后兼容）
    cfg_hard = make_cfg('hard')
    ok_lo = all(cap_fn(cfg, b) == Decimal(0) for b in
                (Decimal('0'), Decimal('0.1'), Decimal('0.2499')))
    ok_hi = all(cap_fn(cfg, b) == Decimal(1) for b in
                (Decimal('0.35'), Decimal('0.5'), Decimal('0.99')))
    ok_hard = all(cap_fn(cfg, b) == cap_fn(cfg_hard, b)
                  for b in (Decimal('0'), Decimal('0.2499'),
                            Decimal('0.35'), Decimal('0.99')))
    check(ok_lo and ok_hi and ok_hard,
          'A2 b<defense=0、b>=attack=1，且两端与 hard 逐值一致')

    # 断言 3：单日 |Δcap| ≤ |Δb|/span（Lipschitz=1/span）
    df = pd.read_parquet(BREADTH)
    bs = [Decimal(str(x)) for x in df['breadth20']]
    worst = Decimal(0)
    worst_pair = None
    for i in range(len(bs) - 1):
        lhs = abs(cap_fn(cfg, bs[i + 1]) - cap_fn(cfg, bs[i]))
        rhs = abs(bs[i + 1] - bs[i]) / SPAN
        if lhs > worst:
            worst = lhs
            worst_pair = (i, str(lhs), str(rhs))
        if lhs > rhs:
            check(False, 'A3 |Δcap| ≤ |Δb|/span',
                  f'idx {i}: {lhs} > {rhs}')
            break
    else:
        check(True, 'A3 单日 |Δcap| ≤ |Δb|/span（全序列成立）',
              f'worst {worst_pair}')

    # 回放两模式
    lin_seq, lin_events, lin_ice, lin_rel = replay('linear')
    hard_seq, hard_events, hard_ice, hard_rel = replay('hard')

    # 断言 4：demote 跨界日在 linear 下仍出单（目标仓=linear(b)，非全清）
    lin_demote_days = {d for k, d in lin_events if k == 'demote'}
    lin_demote_rows = [(d, b, cap) for d, b, cap in lin_seq
                       if d in lin_demote_days]
    check(len(lin_demote_days) == 86,
          'A4a demote 跨界日计数与影子前瞻一致（86）',
          f'actual={len(lin_demote_days)}')
    check(len(lin_demote_rows) == len(lin_demote_days),
          'A4b demote 日出单存在（每个跨界日均产生到仓计划）',
          f'{len(lin_demote_rows)} vs {len(lin_demote_days)}')
    check(all(cap == cap_fn(cfg, b) for d, b, cap in lin_demote_rows),
          'A4c demote 日目标仓恰为 linear(b)（不再硬切 0/1）')
    n_full_liquid = sum(1 for _, _, cap in lin_demote_rows if cap == 0)
    print(f'[INFO] demote 日 linear cap>0 占 '
          f'{len(lin_demote_rows) - n_full_liquid}/{len(lin_demote_rows)}')

    # 断言 5：ice 路径逐日与 hard 一致
    check(lin_ice == hard_ice and lin_rel == hard_rel,
          'A5 ice 确认/解除日期集合与 hard 完全一致',
          f'lin {len(lin_ice)}/{len(lin_rel)} vs hard {len(hard_ice)}/{len(hard_rel)}')
    check(len(lin_ice) == 124,
          'A5b ice 确认次数 124（与影子前瞻一致）', f'actual={len(lin_ice)}')

    # 断言 6：到仓日集合与 hard 完全一致（节拍时钟不受 weight_mode 影响）
    lin_days = [d for d, _, _ in lin_seq]
    hard_days = [d for d, _, _ in hard_seq]
    check(lin_days == hard_days,
          'A6 到仓日集合与 hard 完全一致（540 日）',
          f'lin {len(lin_days)} vs hard {len(hard_days)}')

    # 附加画像：分数仓天数
    frac = sum(1 for _, _, cap in lin_seq if Decimal(0) < cap < 1)
    print(f'[INFO] linear 分数仓到仓日 {frac}/{len(lin_seq)}')
    check(frac >= 100, 'A7 分数仓生效面 ≥100 日（接口改造实际生效）',
          f'actual={frac}')

    if _failures:
        print(f'\n❌ {len(_failures)} 条断言失败')
        return 1
    print('\n✅ 全部断言通过（e11-linear 行为探针）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())