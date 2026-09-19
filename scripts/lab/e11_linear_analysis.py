#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e11-linear 扰动矩阵 → C7/G-2a~c 判据计算（结果收齐后跑）。

读取 experiments/lab/e11-linear*/experiment.json，输出：
  - G-2a 平台宽度：CAGR ≥ (2/3)*max 的格点占比（判据 ≥50%）
  - G-2b 退化单调性：沿 d 轴（a 固定）与 a 轴（d 固定）的退化单调检查
  - G-2c Lipschitz：L = max|ΔCAGR|/|Δθ|，判据 L×10% ≤ 6.3pp
  - G-2g 换手、主实验 CAGR/MDD 对照 e8b
输出 docs/audit/e11_linear_g2_analysis_20260919.md + stdout 摘要。

用法：.venv/bin/python scripts/lab/e11_linear_analysis.py
"""
from __future__ import annotations

import json
import sys
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / 'experiments/lab'
OUT = ROOT / 'docs/audit/e11_linear_g2_analysis_20260919.md'
E8B_CAGR = 0.085814
E8B_MDD = 0.173990
E8B_TURN = 4.6074
DEG_OK = 0.010
DEG_CRIT = 0.015


def load_cells() -> dict:
    cells = {}
    for f in sorted(LAB.glob('e11-linear*/experiment.json')):
        j = json.loads(f.read_text(encoding='utf-8'))
        cells[j['experiment']] = {
            'cagr': float(j['cagr']),
            'mdd': float(j['max_drawdown']),
            'turnover': float(j['annual_turnover']),
            'd': float(j['overrides']['breadth_defense_threshold']),
            'a': float(j['overrides']['breadth_attack_threshold']),
        }
    return cells


def main() -> int:
    cells = load_cells()
    base = cells.get('e11-linear')
    pg = {k: v for k, v in cells.items() if k.startswith('e11-linear-pg-')}
    if base is None or len(pg) < 15:
        print(f'[WAIT] 结果未齐：主实验={base is not None}，扰动格={len(pg)}/15')
        return 2

    grid = {(v['d'], v['a']): v['cagr'] for v in pg.values()}
    peak = max(grid.values())

    cutoff = peak * 2 / 3
    n_ok = sum(1 for c in grid.values() if c >= cutoff)
    g2a_ratio = n_ok / len(grid)
    g2a_pass = g2a_ratio >= 0.50

    viol = []
    by_a: dict[float, list] = {}
    by_d: dict[float, list] = {}
    for (d, a), c in grid.items():
        by_a.setdefault(a, []).append((d, c))
        by_d.setdefault(d, []).append((a, c))
    tol = 0.005
    for a, rows in by_a.items():
        rows.sort()
        ds = [r[0] for r in rows]
        cs = [r[1] for r in rows]
        if 0.25 in ds:
            i0 = ds.index(0.25)
            for i in range(i0):
                if cs[i] > cs[i + 1] + tol:
                    viol.append(f'a={a}: d {ds[i]}->{ds[i+1]} 应升反降 ({cs[i]:.4f}>{cs[i+1]:.4f})')
            for i in range(i0, len(rows) - 1):
                if cs[i] < cs[i + 1] - tol:
                    viol.append(f'a={a}: d {ds[i]}->{ds[i+1]} 应降反升 ({cs[i]:.4f}<{cs[i+1]:.4f})')
    for d, rows in by_d.items():
        rows.sort()
        as_ = [r[0] for r in rows]
        cs = [r[1] for r in rows]
        if 0.35 in as_:
            i0 = as_.index(0.35)
            for i in range(i0):
                if cs[i] > cs[i + 1] + tol:
                    viol.append(f'd={d}: a {as_[i]}->{as_[i+1]} 应升反降 ({cs[i]:.4f}>{cs[i+1]:.4f})')
            for i in range(i0, len(rows) - 1):
                if cs[i] < cs[i + 1] - tol:
                    viol.append(f'd={d}: a {as_[i]}->{as_[i+1]} 应降反升 ({cs[i]:.4f}<{cs[i+1]:.4f})')
    g2b_pass = len(viol) == 0

    step_d = {0.025, 0.05}
    step_a = {0.03, 0.035, 0.04, 0.07}
    worst_l = 0.0
    worst_pair = None
    items = list(grid.items())
    for (p1, c1), (p2, c2) in combinations(items, 2):
        dd = abs(round(p1[0] - p2[0], 4))
        da = abs(round(p1[1] - p2[1], 4))
        axis_aligned = (dd in step_d and da == 0) or (da in step_a and dd == 0)
        if axis_aligned:
            dtheta = (dd ** 2 + da ** 2) ** 0.5
            lv = abs(c1 - c2) / dtheta
            if lv > worst_l:
                worst_l = lv
                worst_pair = (p1, p2)
    g2c_metric = worst_l * 0.10
    g2c_pass = g2c_metric <= 0.063

    deg = E8B_CAGR - base['cagr']
    if deg < DEG_OK:
        cagr_verdict = '接口修复成立（退化<1.0pp）'
    elif deg < DEG_CRIT:
        cagr_verdict = '临界（1.0~1.5pp）'
    else:
        cagr_verdict = '代价过大（>1.5pp）'
    g2g_pass = base['turnover'] <= 8.0

    lines = [
        '# e11-linear G-2 分析（C7 判据落地，2026-09-19）',
        '',
        '复跑：`.venv/bin/python scripts/lab/e11_linear_analysis.py`（读各 experiment.json）。',
        '',
        '## 主实验 e11-linear vs e8b（对照）',
        '',
        f"- CAGR {base['cagr']:.4%} vs e8b {E8B_CAGR:.4%} → 退化 {deg:.4%} ⇒ **{cagr_verdict}**",  # noqa: E501
        f"- MDD {base['mdd']:.4%} vs e8b {E8B_MDD:.4%}",
        f"- 年化换手 {base['turnover']:.2f} vs e8b {E8B_TURN:.2f}（G-2g ≤8 ⇒ {'PASS' if g2g_pass else 'FAIL'}）",
        '',
        '## G-2a 平台宽度',
        '',
        f'- 15 格峰值 CAGR = {peak:.4%}，达标线 (2/3)x峰值 = {cutoff:.4%}',
        f'- 达标格点 {n_ok}/15 = {g2a_ratio:.1%} ⇒ {"PASS(>=50%)" if g2a_pass else "FAIL(<50%)"}',
        '',
        '## G-2b 退化单调性（容忍带 ±0.5pp）',
        '',
        f'- 非单调违例 {len(viol)} 处 ⇒ {"PASS" if g2b_pass else "FAIL"}',
    ]
    lines += [f'  - {v}' for v in viol] if viol else ['  - 无']
    lines += [
        '',
        '## G-2c Lipschitz',
        '',
        f'- L = {worst_l:.3f}（worst pair {worst_pair}），Lx10% = {g2c_metric:.4%} ≤ 6.3pp ⇒ {"PASS" if g2c_pass else "FAIL"}',
        '',
        '## 15 格矩阵（CAGR）',
        '',
        '| d\\a | 0.28 | 0.31 | 0.35 | 0.385 | 0.42 |',
        '|---|---|---|---|---|---|',
    ]
    for d in (0.20, 0.225, 0.25, 0.275, 0.30):
        row = [f'| {d} |']
        for a in (0.28, 0.31, 0.35, 0.385, 0.42):
            v = grid.get((d, a))
            row.append(f' {v:.4%} |' if v is not None else ' — |')
        lines.append(''.join(row))
    allpass = g2a_pass and g2b_pass and g2c_pass and g2g_pass and deg < DEG_OK
    verdict = ('G-2 修复成立 → 补 G-2f 留出段复核 → 提交六维门禁重评'
               if allpass else '见各分项 FAIL 处置（预登记 §七模板）')
    lines += ['', f'## 综合裁决：{verdict}']
    OUT.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines))
    return 0 if allpass else 1


if __name__ == '__main__':
    sys.exit(main())