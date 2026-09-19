#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""华泰「高股息行业中性组合」配方在本仓数据面上的复现探针（研究用，非实验）。

配方（华泰策略 2026-04-03 研报，样本空间全A、每年5/1调仓）：
  1. 剔 ST、市值<100亿
  2. 过去3年连续分红；过去5年股息率均值>中位数 且 变异系数<中位数
  3. 预测股息率 = TTM净利×5年平均分红率/市值 ≥2.5%（无一致预期→eps_ttm近似）
  4. 综合得分=预测股息率分位×ROA分位（无ROA→ROE近似，登记替代）
  5. 行业中性：每行业取得分前2名，等权

评估：每年5/1快照→次年4/30持有，买入持有等权+分红全收益。
对照：华泰研报 11.7%/MDD23.1%（2016-2026样本内）；本仓 isst-e8b 8.58%/17.4%。

登记替代与近似（⛔ 与研报口径差异）：
  - ROA 用 ROE 替代（fina 无 ROA 字段）
  - 预测净利用 eps_ttm 替代一致预期
  - ST 剔除用当前名称含'ST'（历史ST状态不可得，偏松）
  - 样本起点 2016（需 2011-2015 五年股息率历史打底）
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / 'data/daily_basic_alla'
EV = ROOT / 'data/dividend_events_alla'
FINA = ROOT / 'data/financial_pit_alla'
IND = ROOT / 'data/pool_meta/stock_industry.parquet'

YEARS = list(range(2016, 2025))          # 持仓年 2016..2024（5/1→4/30）
HIST_Y = 5                               # 股息率历史窗
DIV_Y = 3                                # 连续分红年数
MCAP_MIN = 1e6                           # total_mv 单位千元 → 100亿=1e6千元? 校验见下
PRED_YIELD_MIN = 0.025
TOP_PER_IND = 2


def load_daily(datestr):
    f = DB / f'{datestr}.parquet'
    if not f.exists():
        return None
    return pd.read_parquet(f)


def trading_days():
    return sorted(p.stem for p in DB.glob('*.parquet'))


def cash_by_year():
    """每股每年现金分红（税前），键 (ts_code, ex_year)。"""
    rows = []
    for f in EV.glob('*.parquet'):
        e = pd.read_parquet(f, columns=['ts_code', 'ex_date', 'cash_div',
                                        'stk_bo_rate', 'stk_co_rate'])
        e = e[e['cash_div'] > 0]
        e['ex'] = pd.to_datetime(e['ex_date'], errors='coerce')
        e = e.dropna(subset=['ex'])
        e['y'] = e['ex'].dt.year
        rows.append(e[['ts_code', 'y', 'cash_div']])
    c = pd.concat(rows).groupby(['ts_code', 'y'])['cash_div'].sum()
    return c


def div_factor_by_year():
    """送转因子：键 (ts_code, ex_date)。"""
    rows = []
    for f in EV.glob('*.parquet'):
        e = pd.read_parquet(f, columns=['ts_code', 'ex_date', 'cash_div',
                                        'stk_bo_rate', 'stk_co_rate'])
        e = e.dropna(subset=['ex_date'])
        e['fac'] = 1 + e['stk_bo_rate'].fillna(0) + e['stk_co_rate'].fillna(0)
        rows.append(e[['ts_code', 'ex_date', 'cash_div', 'fac']])
    return pd.concat(rows)


def _to_bs(c):
    """tushare ts_code 600004.SH → 本仓 fina 文件 sh.600004。"""
    num, suf = c.split('.')
    return f'{suf.lower()}.{num}'


def eps_ttm_at(codes, pub_date):
    """fina pub_date≤snapshot 最近一条的 eps（TTM近似=最近年报eps）。"""
    out = {}
    for c in codes:
        f = FINA / f'{_to_bs(c)}.parquet'
        if not f.exists():
            continue
        d = pd.read_parquet(f, columns=['pub_date', 'eps', 'roe'])
        d['pub'] = pd.to_datetime(d['pub_date'])
        d = d[d['pub'] <= pd.Timestamp(pub_date)]
        if not d.empty:
            r = d.sort_values('pub').iloc[-1]
            out[c] = (r['eps'], r['roe'])
    return out


def to_tushare_code(c):
    """本仓 code 形如 sh.600004 → tushare ts_code 600004.SH。"""
    num = c.split('.')[1]
    suf = 'SH' if c.startswith('sh') else ('SZ' if c.startswith('sz') else 'BJ')
    return f'{num}.{suf}'


def main():
    days = trading_days()
    ind = pd.read_parquet(IND).set_index('ts_code')
    cash = cash_by_year()
    factors = div_factor_by_year()
    factors['ex'] = pd.to_datetime(factors['ex_date']).dt.date
    fac_map = {(r.ts_code, r.ex): (float(r.fac), float(r.cash_div))
               for r in factors.itertuples()}

    # 年收益评估：把持仓年 5/1→次年4/30 切成 daily_basic 收盘价序列
    results = []
    for hold_y in YEARS:
        snap_ds = max(d for d in days if d <= f'{hold_y}0430')
        snap = load_daily(snap_ds).set_index('ts_code')
        snap = snap[snap['close'] > 0]
        # R0: 剔 ST（当前名称近似，历史ST不可得——登记偏松近似）
        snap = snap[~snap.index.map(
            lambda tc: 'ST' in str(ind['name'].get(tc, '')))]
        # R1: 市值≥100亿（total_mv 单位万元：600004=1,339,750万元≈134亿 ✓）
        snap = snap[snap['total_mv'] >= 1e6]
        # R2a: 三年连续分红（ex_date 年在 hold_y-3..hold_y-1 各有现金分红）
        recent = cash.loc[cash.index.get_level_values(1).isin(
            range(hold_y - 3, hold_y))].reset_index()
        cnt = recent.groupby('ts_code')['y'].nunique()
        cont3 = set(cnt[cnt >= DIV_Y].index)
        cand = snap.loc[snap.index.isin(cont3)]
        if cand.empty:
            results.append((hold_y, 0, np.nan, [])); continue
        # R2b: 5年股息率均值>中位 & CV<中位（各年股息率=当年现金分红/该年末收盘价）
        # 数据可得性：daily_basic 起于2015 ⇒ 股息率历史窗截断为可得的3-5年
        # （≥3年有效年才参与；登记近似）
        yearend_px = {}
        for yy in range(hold_y - HIST_Y, hold_y):
            eds = [d for d in days if d <= f'{yy}1231']
            if eds:
                yearend_px[yy] = load_daily(max(eds)).set_index('ts_code')['close']
        ys = []
        for tc, row in cand.iterrows():
            hist = []
            for yy in range(hold_y - HIST_Y, hold_y):
                if yy not in yearend_px:
                    continue
                cd = cash.get((tc, yy), 0.0)
                px = yearend_px[yy].get(tc, np.nan)
                if pd.notna(px) and px > 0:
                    hist.append(cd / px)
            if len(hist) < 3:
                continue
            ys.append((tc, np.mean(hist), np.std(hist) / (np.mean(hist) + 1e-12)))
        ydf = pd.DataFrame(ys, columns=['ts_code', 'ymean', 'ycv']).set_index('ts_code')
        med_m, med_cv = ydf['ymean'].median(), ydf['ycv'].median()
        ydf = ydf[(ydf['ymean'] > med_m) & (ydf['ycv'] < med_cv)]
        cand = cand.loc[cand.index.isin(ydf.index)]
        if cand.empty:
            results.append((hold_y, 0, np.nan, [])); continue
        # R3: 预测股息率 ≥2.5%（eps_ttm×payout均值/价格）
        fin = eps_ttm_at(cand.index.tolist(), f'{hold_y}-04-30')
        preds = {}
        for tc, row in cand.iterrows():
            if tc not in fin or not fin[tc][0] or fin[tc][0] <= 0:
                continue
            eps = fin[tc][0]
            pays = []
            for yy in range(hold_y - 5, hold_y):
                cd = cash.get((tc, yy), 0.0)
                pays.append(min(max(cd / eps, 0.0), 1.0))
            preds[tc] = eps * np.mean(pays) / row['close']
        cand = cand.loc[[tc for tc, v in preds.items() if v >= PRED_YIELD_MIN]]
        if cand.empty:
            results.append((hold_y, 0, np.nan, [])); continue
        # R4: 得分=预测股息率分位×ROE分位；每行业前2
        scored = []
        for tc in cand.index:
            scored.append((tc, preds[tc], fin[tc][1] or 0.0))
        sdf = pd.DataFrame(scored, columns=['ts_code', 'py', 'roe']).set_index('ts_code')
        sdf['score'] = sdf['py'].rank(pct=True) * sdf['roe'].rank(pct=True)
        sdf['ind'] = [ind['industry'].get(tc, '未知') for tc in sdf.index]
        pick = (sdf.sort_values('score', ascending=False)
                  .groupby('ind').head(TOP_PER_IND))
        picks = pick.index.tolist()
        # 评估：snap_ds→次年4/30 等权买入持有（全收益）
        end_ds = max(d for d in days if d <= f'{hold_y + 1}0430')
        hold_days = [d for d in days if snap_ds < d <= end_ds]
        port_nav = 1.0
        px = {}
        prev_close = {}
        w = {tc: 1.0 / len(picks) for tc in picks}
        for d in hold_days:
            day = load_daily(d).set_index('ts_code')
            dd = pd.to_datetime(d).date()
            port_r = 0.0
            tot = 0.0
            for tc, wi in w.items():
                if tc not in day.index:
                    continue
                c = float(day.loc[tc, 'close'])
                if tc in prev_close and prev_close[tc] > 0:
                    fac, cdv = fac_map.get((tc, dd), (1.0, 0.0))
                    port_r += wi * ((c * fac + cdv) / prev_close[tc] - 1.0)
                    tot += wi
                prev_close[tc] = c
            if tot > 0:
                port_nav *= 1.0 + port_r / tot
        results.append((hold_y, len(picks), port_nav - 1.0, picks[:6]))
        print(f'{hold_y}: 池{len(picks)}只 持仓年收益 {port_nav-1:+.1%} 例{picks[:4]}',
              flush=True)

    nav = np.prod([1 + r[2] for r in results if pd.notna(r[2])])
    n = len([r for r in results if pd.notna(r[2])])
    print(f'\n华泰配方复现（{YEARS[0]}-{YEARS[-1]} 持仓年）: '
          f'累计 {nav - 1:.1%}  年化 {nav ** (1 / n) - 1:.2%}')
    print('对照：华泰研报样本内 11.7%/MDD23.1%；isst-e8b 8.58%/17.4%')


if __name__ == '__main__':
    sys.exit(main())
