# -*- coding: utf-8 -*-
"""e25 双面板影子筛选单元测试（合成数据，离线确定性）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.lab import e25_factor_screen as m


def _pit_entry(rows):
    df = pd.DataFrame(rows)
    pub = pd.to_datetime(df['pub_date']).to_numpy(dtype='datetime64[ns]')
    vals = df[m.PIT_FIELDS].to_numpy(dtype=np.float64)
    order = np.argsort(pub)
    return pub[order], vals[order]


def test_bs_to_ts():
    assert m.bs_to_ts('sh.600000') == '600000.SH'
    assert m.bs_to_ts('sz.000001') == '000001.SZ'


def test_pit_snapshot_pub_date_invisibility():
    """pub_date > T 不可见；最新可见为 ≤T 的最后一条；无新鲜度截断。"""
    pit = {'600000.SH': _pit_entry([
        dict(pub_date='2020-01-10', roe=10, net_profit_yoy=5,
             deducted_net_profit_yoy=4, debt_to_assets=50,
             cash_flow_per_share=1.0),
        dict(pub_date='2020-03-01', roe=20, net_profit_yoy=8,
             deducted_net_profit_yoy=9, debt_to_assets=40,
             cash_flow_per_share=2.0),
        dict(pub_date='2020-06-01', roe=99, net_profit_yoy=99,
             deducted_net_profit_yoy=99, debt_to_assets=99,
             cash_flow_per_share=9.0)])}
    snap = m.pit_snapshot(pd.Timestamp('2020-04-01'), pit)
    assert snap['roe']['600000.SH'] == 20          # 3-01 可见，6-01 不可见
    assert snap['eq_gap']['600000.SH'] == 9 - 8    # dnpy - npy
    # 无新鲜度截断：陈旧一年仍取最新可见（e23 H5/H6 的 60d 截断不适用）
    snap2 = m.pit_snapshot(pd.Timestamp('2021-06-01'), pit)
    assert snap2['roe']['600000.SH'] == 99
    assert snap2['stale']['600000.SH'] > m.STALE_DIAG_DAYS


def test_pit_snapshot_window_min_count():
    """近 8 条聚合：<4 条有效观测 → NaN。"""
    rows = [dict(pub_date=f'2019-0{i+1}-15', roe=float(i), net_profit_yoy=1.0,
                 deducted_net_profit_yoy=0.0, debt_to_assets=50,
                 cash_flow_per_share=0.0) for i in range(3)]
    pit = {'A.SH': _pit_entry(rows)}
    snap = m.pit_snapshot(pd.Timestamp('2020-01-01'), pit)
    assert np.isnan(snap['roe_std8']['A.SH'])       # 3<4 → NaN
    assert np.isnan(snap['npyoy_pos8']['A.SH'])
    rows.append(dict(pub_date='2019-04-15', roe=10.0, net_profit_yoy=-2.0,
                     deducted_net_profit_yoy=0.0, debt_to_assets=50,
                     cash_flow_per_share=0.0))
    pit = {'A.SH': _pit_entry(rows)}
    snap = m.pit_snapshot(pd.Timestamp('2020-01-01'), pit)
    assert snap['roe_std8']['A.SH'] == pytest.approx(
        np.std([0., 1., 2., 10.], ddof=1))
    assert snap['npyoy_pos8']['A.SH'] == pytest.approx(0.75)  # 3/4 条 >0


def test_div_snapshot_completed_years():
    """P6 计近 5 个已完成自然年；P7 需相邻两年均非零。"""
    divc = {'A.SH': {'years': {2015, 2017, 2018, 2019},
                     'cash': {2018: 1.0, 2019: 1.5}},
            'B.SH': {'years': {2019}, 'cash': {2019: 1.0}}}
    d = m.div_snapshot(pd.Timestamp('2020-06-30'), divc)
    # 已完成年 = 2015..2019；A 命中 4 年
    assert d['div_cont5']['A.SH'] == 4.0
    assert d['div_cont5']['B.SH'] == 1.0
    assert d['div_growth']['A.SH'] == pytest.approx(0.5)      # 1.5/1.0-1
    assert np.isnan(d['div_growth']['B.SH'])                # 2018 缺失 → NaN


def test_signal_at_pe_pb_positive_only():
    idx = pd.DatetimeIndex(['2020-01-31'])
    pe = pd.DataFrame({'A.SH': [15.0], 'B.SH': [-5.0], 'C.SH': [0.0]},
                      index=idx)
    pb = pd.DataFrame({'A.SH': [1.5], 'B.SH': [0.8], 'C.SH': [-0.2]},
                      index=idx)
    T = idx[0]
    s_pe = m.signal_at('pe', T, {}, {}, pe, pb)
    assert s_pe['A.SH'] == 15.0 and np.isnan(s_pe['B.SH']) \
        and np.isnan(s_pe['C.SH'])
    s_pb = m.signal_at('pb', T, {}, {}, pe, pb)
    assert np.isnan(s_pb['C.SH'])


def test_eval_month_quintile_direction():
    """低好 → 升序取头为 top；n<5 → NaN 价差。"""
    syms = [f's{i}' for i in range(10)]
    sig = pd.Series(range(10), index=syms, dtype=float)
    base = pd.Series(True, index=syms)
    fwd = pd.Series(np.linspace(0, 0.09, 10), index=syms)
    nan_row = pd.Series(0.0, index=syms)
    row = m.eval_month(sig, True, base, fwd, nan_row)
    assert row['_top'] == {'s0', 's1'}          # 升序头 = 最小值
    assert row['spread'] == pytest.approx(
        fwd[['s0', 's1']].mean() - fwd[['s8', 's9']].mean())
    row2 = m.eval_month(sig, False, base, fwd, nan_row)
    assert row2['_top'] == {'s8', 's9'}         # 高好 → 降序头
    row3 = m.eval_month(sig.iloc[:4], True, base, fwd, nan_row)
    assert row3['n'] == 4 and np.isnan(row3['spread'])


def _rows(hyp, panel, spreads, n=1500, top=None):
    return [dict(hyp=hyp, panel=panel, T=t, n=n, spread=sp, excess=0.01,
                 nan_ratio=0.0, _top=top or {'a', 'b', 'c'})
            for t, sp in spreads]


def test_stats_grade_tiers_and_inconclusive():
    """强 t≥2.85+G2/G3/G4；弱 2.0≤t<2.85+G2/G3/G4；M<60 → INCONCLUSIVE。"""
    # 6 个跨年正价差 + 两半窗同号为正；构造大 t
    spreads = [(f'{y}-0{m}-28', 0.05 + 0.001 * k)
               for k, (y, m) in enumerate(
                   [(2016, 1), (2017, 2), (2018, 3), (2021, 1), (2022, 2),
                    (2023, 3)])]
    res = m.stats(_rows('F3', 'F', spreads))
    assert res['F3']['M'] == 6
    assert res['F3']['grade'] == 'INCONCLUSIVE'   # M=6<60 一票否决
    # 70 月大 t → 强
    spreads70 = [(f'{2015 + k // 12}-{k % 12 + 1:02d}-28', 0.05)
                 for k in range(70)]
    res70 = m.stats(_rows('F3', 'F', spreads70))
    assert res70['F3']['t'] == np.inf or res70['F3']['t'] > m.T_STRONG
    assert res70['F3']['grade'] == '强'
    # 70 月零均值噪声 → t 不达标 → 负
    rng = np.random.default_rng(1)
    noise = [(f'{2015 + k // 12}-{k % 12 + 1:02d}-28',
              float(rng.normal(0, 0.02))) for k in range(70)]
    resn = m.stats(_rows('F3', 'F', noise))
    assert resn['F3']['grade'] in ('负', '弱')


def test_stats_g4_fraction_semantics():
    """G4：信号有定义月(n≥5)中低样本月占比 ≤30%；按面板阈值。"""
    # Panel P：10 个有定义月中 3 个月 n<300 → 占比 0.3 ≤0.30 过
    rows = ([dict(hyp='P1', panel='P', T=f'2020-{k:02d}-28', n=200,
                  spread=0.01, excess=0.0, nan_ratio=0.0, _top={'a'})
             for k in range(1, 4)]
            + [dict(hyp='P1', panel='P', T=f'2020-{k:02d}-28', n=500,
                    spread=0.01, excess=0.0, nan_ratio=0.0, _top={'a'})
               for k in range(4, 11)]
            + [dict(hyp='P1', panel='P', T='2020-11-30', n=2,        # n<5 不算有定义月
                    spread=np.nan, excess=np.nan, nan_ratio=np.nan, _top=None)])
    res = m.stats(rows)
    assert res['P1']['g4_low_frac'] == pytest.approx(0.3)
    assert res['P1']['G4'] is True
    # Panel F 同样 200 <1000 → 全部低样本 → 占比 1.0 失败
    rowsF = [dict(hyp='F3', panel='F', T=f'2020-{k:02d}-28', n=200,
                  spread=0.01, excess=0.0, nan_ratio=0.0, _top={'a'})
             for k in range(1, 4)]
    assert m.stats(rowsF)['F3']['G4'] is False


def test_composite_direction_normalization_and_trigger():
    """合成臂：低好假设贡献取负；同面板 <2 弱及以上不触发。"""
    res = {'P1': {'grade': '弱'}, 'P2': {'grade': '强'},
           'P4': {'grade': '负'},  # P4 低好但不入选
           'F1': {'grade': '负'}}
    idx = pd.Index([f's{i}' for i in range(10)])
    sig_cache = {
        ('P1', 'T'): pd.Series(np.arange(10, dtype=float), index=idx),
        ('P2', 'T'): pd.Series(np.arange(10, dtype=float), index=idx),
        ('P4', 'T'): pd.Series(np.arange(10, dtype=float), index=idx),
        ('F1', 'T'): pd.Series(np.arange(10, dtype=float), index=idx),
    }
    base = pd.Series(True, index=idx)
    fwd = pd.Series(np.linspace(0, 0.09, 10), index=idx)
    nan_row = pd.Series(0.0, index=idx)
    ctx = {'T': (base.copy(), base.copy(), fwd, nan_row)}
    rows = m.composite_rows(res, sig_cache, ctx)
    hyps = {r['hyp'] for r in rows}
    assert hyps == {'CP'}          # P 面板 2 个弱+ → CP；F 面板 0 个 → 不触发
    row = [r for r in rows if r['hyp'] == 'CP'][0]
    # P1 高好 z 正向、P2 低好 z 取负 → 合成后两端极值抵消，top 应为中间秩
    assert row['panel'] == 'P'
    assert row['_top'] is not None and len(row['_top']) == 2
    # F 面板无任何合成行
    assert not any(r['hyp'] == 'CF' for r in rows)
