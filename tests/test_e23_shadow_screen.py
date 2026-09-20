# -*- coding: utf-8 -*-
"""e23 影子筛选单元测试（合成 3 股 × 300 日面板；规格 e23_shadow_spec.md）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.lab import e23_shadow_screen as m


def _panel(days=300, syms=('A.SH', 'B.SZ', 'C.SZ')):
    rng = np.random.default_rng(7)
    idx = pd.bdate_range('2020-01-01', periods=days)
    close = pd.DataFrame(100 * np.exp(np.cumsum(
        rng.normal(0.0005, 0.01, (days, len(syms))), axis=0)),
        index=idx, columns=list(syms))
    turn = pd.DataFrame(rng.uniform(0.5, 3.0, (days, len(syms))),
                        index=idx, columns=list(syms))
    return close, turn


def test_fwd20_window_boundary():
    """fwd20(T)=close[D[i+21]]/close[D[i+1]]-1：构造恒等收益验证边界。"""
    days, syms = 40, ['A.SH']
    idx = pd.bdate_range('2020-01-01', periods=days)
    close = pd.DataFrame(np.linspace(100, 139, days), index=idx, columns=syms)
    r = close / close.shift(1) - 1
    fwd = m.fwd_returns(close, r)
    i = 5
    expect = close.iloc[i + 21, 0] / close.iloc[i + 1, 0] - 1
    assert fwd.iloc[i, 0] == pytest.approx(expect, rel=1e-9)


def test_mom_12_1_excludes_last_21_days():
    """H1 窗 = (T-252, T-21]：近 21 日暴涨不得进入信号。"""
    close, turn = _panel()
    syms = close.columns
    # C 股：最后 15 天每天 +8%（|r|<0.11 不被剔）；A/B 平
    close.iloc[:, 0] = 100.0
    close.iloc[:, 1] = 100.0
    close.loc[close.index[-15]:, syms[2]] = (
        close.loc[:, syms[2]].iloc[-16] *
        np.cumprod(np.full(15, 1.08)))
    r = close / close.shift(1) - 1
    sigs = m.signal_frames(r, turn, close)
    T = close.index[-22]  # 月末假设
    # C 在 T 时点 mom 应不含 T-21..T 段 ⇒ 不会因暴涨而领先 A/B
    assert sigs['H1'].loc[T, syms[2]] < sigs['H1'].loc[T, syms[0]] + 0.3
    # 且其 mom ≈ 暴涨前段的收益（近 21 日 0 收益段之外）
    assert sigs['H1'].loc[T, syms[2]] == pytest.approx(0.0, abs=0.15)


def test_pub_date_pit_invisibility():
    """pub_date > T 的财报不可见；≤60 日新鲜度约束。"""
    fina = {'600000.SH': pd.DataFrame({
        'pub_date': ['2020-01-10', '2020-03-01', '2020-06-01'],
        'deducted_net_profit_yoy': [5.0, 30.0, 99.0],
        'cash_flow_per_share': [1.0, 2.0, 3.0]})}
    T = pd.Timestamp('2020-04-01')
    s = m.pit_fina_signal(T, fina, 'deducted_net_profit_yoy')
    assert s['600000.SH'] == 30.0  # 最新可见=3-01，6-01 不可见
    T2 = pd.Timestamp('2020-05-15')  # 3-01 公告距今 74>60 日 → NaN（缺席）
    assert '600000.SH' not in m.pit_fina_signal(T2, fina,
                                              'deducted_net_profit_yoy').index


def test_t_stat_formula_and_turnover():
    """stats()：t = mean/(std/√M)；单边换手 = |S_T\\S_{T-1}|/|S_T|。"""
    rows = []
    tops = [set('abcde'), set('abdce')]  # 第2月换出e换入c? 保持|b-a|=1? 用实际差集
    tops = [{'s%d' % i for i in range(5)},
            {'s%d' % i for i in range(1, 6)}]  # 换出 s0 换入 s5 → 1/5=0.2
    spreads = [0.02, 0.04, 0.06, 0.08]
    for k, sp in enumerate(spreads):
        rows.append(dict(hyp='H1', T=f'2020-0{k+1}-28', n=1500,
                         spread=sp, excess=0.01, _top=tops[min(k, 1)]))
    res, _ = m.stats(rows)
    import math
    sp = np.array(spreads)
    t_expect = sp.mean() / (sp.std(ddof=1) / math.sqrt(4))
    assert res['H1']['t'] == pytest.approx(t_expect, rel=1e-9)
    # 换手：top 序列 [S0,S0,S1,S1] → 变化两次 0/5? 实际相邻 (S0,S0)=0,(S0,S1)=0.2,(S1,S1)=0 → mean=0.2/3
    assert res['H1']['turn_top'] == pytest.approx(0.2 / 3, rel=1e-9)


def test_grade_tiers():
    # 跨年正 spread → G2 过；t 大 → 强
    rows = []
    for k, (t, sp) in enumerate(
            [('2018-01-31', 0.04), ('2018-02-28', 0.05), ('2019-03-29', 0.06),
             ('2021-01-29', 0.05), ('2022-02-28', 0.06), ('2023-03-31', 0.07)]):
        rows.append(dict(hyp='H3', T=t, n=1500, spread=sp, excess=0.02,
                         _top={'a', 'b', 'c', 'd', 'e'}))
    res, _ = m.stats(rows)
    assert res['H3']['grade'] == '强'
    # 单一半年窗 → G2 两半窗同号为正失败 → 至多「弱」
    rows2 = [dict(hyp='H4', T=f'2021-0{k}-28', n=1500, spread=0.05,
                  excess=0.02, _top={'a'}) for k in range(1, 4)]
    res2, _ = m.stats(rows2)
    assert res2['H4']['G2'] is False and res2['H4']['grade'] == '弱'
