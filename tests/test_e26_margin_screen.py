# -*- coding: utf-8 -*-
"""e26 两融因子影子筛选单元测试（合成数据，离线确定性）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.lab import e26_margin_screen as m


def _margin_panels(days=60, syms=('A.SH', 'B.SZ')):
    idx = pd.bdate_range('2020-01-01', periods=days)
    rng = np.random.default_rng(3)
    bal = pd.DataFrame(rng.uniform(1e8, 5e8, (days, len(syms))),
                       index=idx, columns=list(syms))
    buy = pd.DataFrame(rng.uniform(1e7, 5e7, (days, len(syms))),
                       index=idx, columns=list(syms))
    sqty = pd.DataFrame(rng.uniform(1e5, 1e6, (days, len(syms))),
                        index=idx, columns=list(syms))
    cmv = pd.DataFrame(np.full((days, len(syms)), 5e6),  # 万元
                       index=idx, columns=list(syms))
    return {'fin_balance': bal, 'fin_buy': buy, 'short_qty': sqty,
            'circ_mv': cmv}


def test_signal_frames_unit_normalization():
    """circ_mv 万元→元：fin_cmv = bal/(circ_mv×1e4)，量级 ~1-5% 而非万倍。"""
    P = _margin_panels()
    P['fin_balance'].iloc[:, 0] = 2.5e9        # 25 亿融资余额
    P['circ_mv'].iloc[:, 0] = 5e4              # 5 亿流通市值（万元口径）
    fr = m.margin_signal_frames(P)
    # 5e4 万元 ×1e4 = 5e8 元 → 2.5e9/5e8 = 5.0（该构造值无单位比例上限约束，
    # 验证的是 ×1e4 归一已生效：若漏乘会得到 5e-5）
    assert fr['fin_cmv'].iloc[-1, 0] == pytest.approx(5.0)


def test_chg_window_lengths():
    """M1 用 20 日窗、M4 用 5 日窗：构造确定性序列验证边界。"""
    P = _margin_panels(days=40, syms=('A.SH',))
    P['fin_balance'] = pd.DataFrame(
        np.exp(np.linspace(0, 0.4, 40)).reshape(-1, 1) * 1e8,
        index=P['fin_balance'].index, columns=['A.SH'])
    fr = m.margin_signal_frames(P)
    bal = P['fin_balance']['A.SH']
    assert fr['fin_chg20'].iloc[-1, 0] == pytest.approx(
        bal.iloc[-1] / bal.iloc[-21] - 1)
    assert fr['fin_chg5'].iloc[-1, 0] == pytest.approx(
        bal.iloc[-1] / bal.iloc[-6] - 1)


def test_min_obs_and_zero_denom():
    """滚动均值 <10 有效日 → NaN；融券分母为 0 → NaN。"""
    P = _margin_panels(days=30, syms=('A.SH',))
    P['fin_buy'].iloc[:15] = np.nan          # 前 15 日无买入数据
    P['short_qty'].iloc[:20] = 0.0           # 融券分母段全零
    fr = m.margin_signal_frames(P)
    # 第 19 日：近 20 日窗（0..19）buy 有效日=15..19 仅 5 <10 → NaN
    assert np.isnan(fr['buy_int20'].iloc[19, 0])
    # 第 24 日：窗（5..24）有效日=15..24 共 10 ≥10 → 有值
    assert not np.isnan(fr['buy_int20'].iloc[24, 0])
    assert np.isnan(fr['short_chg20'].iloc[29, 0])   # 分母 0 → NaN


def test_one_day_lag_is_pit(tmp_path):
    """信号日 T 只能用 ≤T−1 的 margin 行——searchsorted(side='left')-1。"""
    margin_dates = pd.bdate_range('2020-01-01', periods=30)
    T = margin_dates[15]
    j = int(margin_dates.searchsorted(T, side='left')) - 1
    assert j == 14          # 取到 T-1 行，T 日本身不可见
    # T 非交易日时（假设周末）：仍取 <T 的最后 margin 日
    T2 = pd.Timestamp('2020-01-19')   # 周日
    j2 = int(margin_dates.searchsorted(T2, side='left')) - 1
    assert margin_dates[j2] < T2


def test_stats_g4_adaptive_threshold():
    """G4：n < max(300, 当日标的数×50%) 的有定义月占比 ≤30%。
    标的数 900 → 阈值 450；标的数 3400 → 阈值 1700。"""
    rows = [
        # 两融标的 900 只日：n=400 <450 → 低样本月
        dict(hyp='M1', T='2020-01-31', n=400, mrg_n=900, spread=0.01,
             excess=0.0, nan_ratio=0.0, _top={'a'}),
        # 标的 3400 只日：n=1600 <1700 → 低样本月
        dict(hyp='M1', T='2020-02-28', n=1600, mrg_n=3400, spread=0.01,
             excess=0.0, nan_ratio=0.0, _top={'a'}),
        # 标的 3400 只日：n=2000 ≥1700 → 正常月
        dict(hyp='M1', T='2020-03-31', n=2000, mrg_n=3400, spread=0.01,
             excess=0.0, nan_ratio=0.0, _top={'a'}),
    ]
    res = m.stats(rows)
    assert res['M1']['g4_low_frac'] == pytest.approx(2 / 3)
    assert res['M1']['G4'] is False
    # 全月覆盖达标 → G4 过
    rows2 = [dict(hyp='M1', T=f'2020-{k:02d}-28', n=800, mrg_n=900,
                  spread=0.01, excess=0.0, nan_ratio=0.0, _top={'a'})
             for k in range(1, 4)]
    assert m.stats(rows2)['M1']['G4'] is True


def test_stats_strong_threshold_26():
    """e26「强」阈 t≥2.6（6 假设口径）；M<60 → INCONCLUSIVE。"""
    spreads = [(f'{2015 + k // 12}-{k % 12 + 1:02d}-28', 0.05)
               for k in range(70)]
    rows = [dict(hyp='M1', T=t, n=800, mrg_n=900, spread=sp, excess=0.01,
                 nan_ratio=0.0, _top={'a', 'b'}) for t, sp in spreads]
    res = m.stats(rows)
    assert res['M1']['t'] >= m.T_STRONG and res['M1']['grade'] == '强'
    rows_short = rows[:10]
    assert m.stats(rows_short)['M1']['grade'] == 'INCONCLUSIVE'


def test_composite_trigger_and_sign():
    """合成臂：全部低好假设统一取负 z；<2 弱不触发。"""
    res = {'M1': {'grade': '弱'}, 'M2': {'grade': '强'},
           'M3': {'grade': '负'}}
    idx = pd.Index([f's{i}' for i in range(10)])
    sig_cache = {
        ('M1', 'T'): pd.Series(np.arange(10, dtype=float), index=idx),
        ('M2', 'T'): pd.Series(np.arange(10, dtype=float), index=idx),
        ('M3', 'T'): pd.Series(np.arange(10, dtype=float), index=idx),
    }
    base = pd.Series(True, index=idx)
    fwd = pd.Series(np.linspace(0, 0.09, 10), index=idx)
    nan_row = pd.Series(0.0, index=idx)
    ctx = {'T': (base, fwd, nan_row)}
    rows = m.composite_rows(res, sig_cache, ctx)
    assert [r['hyp'] for r in rows] == ['CM']
    # M1/M2 同为低好、信号完全正相关 → 合成 -z 等值 → top=排序稳定前两格
    assert rows[0]['_top'] == {'s0', 's1'}
    # 单弱不触发
    res2 = {'M1': {'grade': '弱'}}
    assert m.composite_rows(res2, sig_cache, ctx) == []
