#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e14-bump 影子前瞻（预登记前预检）：非单调权重 [0.25,0.30) 凸包带的增量收益测算。

动机（R11 实证）：宽度→前瞻收益呈 W 形——25-30% 桶前瞻5日 +0.61%（166 天），
30-35% dead zone −0.69%（168 天）。hard+mid_cap=0 把 [0.25,0.35) 全置零 ⇒
好带 25-30% 也被规避。bump 形态 = 在该子带给 bump_cap 部分仓、dead zone 仍 0。

本脚本 = **增量袖子口径**（bump_arm − hard_arm）：hard 在 <attack 恒 0 ⇒
增量袖子只在 b∈[defense, dz_edge) 非零。事件序列与 candidates.py 完全一致：
  * 调仓节拍 = rebalance_days(20) 交易日一次（demote/ice 不重置时钟）；
  * demote = b 由 ≥attack 跌入 <attack 当日按 bump cap 重算（跌入 bump 带建仓、
    跌入 dead zone 清零）；
  * ice = b<defense 连续 ice_confirm_days(1) 日 ⇒ 次日开盘清仓；解除当日不交易；
  * mid 带内部跨界（0.30 dead-zone 边界）**不触发事件**——持仓惯性到下一节拍；
  * 成交 = 事件日 T+1 开盘（FR-BT-6）：当日分两段记账——隔夜段归旧仓
    （prev_close→open，含除权），日内段归新仓（open→close）；停牌日价格冻结
    且不可成交（卖单留存——影子简化为当日记账冻结、下一可交易日仍按目标权重差）。

记账（NAV 分数口径，一阶精确）：sleeve[s] = 该票市值/NAV。每日
  incr_ret = Σ(frac_t − frac_{t−1}) − f_{t−1}×r_cash − fee_t
持仓市值演化：隔夜 frac×(open/preclose)×factor + frac×cash_div/preclose；
日内 frac×(close/open)；卖出按其隔夜段了结；新仓只吃日内段。

费用口径：买 0.04% / 卖 0.09%（T203 黄金算例往返 0.113% 的保守近似）。
口径诚实边界（引用必须带）：
  * 影子选股=策略规则复刻（dv≥3% 降序 top50→前5、市值加权），不模拟
    plan_positions 的 2 万单票下限/流动性过滤/整手化——早年 NAV 低段
    可能高估持仓数（偏乐观方向）；
  * ΔNAV 一阶复乘近似，二阶交互忽略；增量 MDD 以袖子自身净值口径估上限；
  * 无 veto/landmine/pead 层（e8b 同参即全关）；
  * 影子=信号层预检，不构成预登记判据。

产物：experiments/lab/e14-bump/_shadow/shadow_e14_bump.{json,md}
用法：.venv/bin/python scripts/lab/e14_bump_shadow.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

BREADTH = ROOT / 'experiments/lab/market-breadth-a/breadth20_daily.parquet'
DIV_STOCKS = ROOT / 'data/dividend_stocks'
EXDIV = DIV_STOCKS / 'exdiv'
GC001 = ROOT / 'data/rates/gc001_daily.parquet'
STOCK_BASIC = ROOT / 'data/stock_basic_cache.parquet'
OUT_DIR = ROOT / 'experiments/lab/e14-bump/_shadow'

# —— e8b 构型参数（同参，⛔ 只为测增量）——
DEF, ATK = 0.25, 0.35
DZ_EDGE = 0.30            # dead zone 下缘（R11 分桶边界，先验固定）
BUMP_CAP = 0.50           # 凸包带仓位上限（预登记候选值）
ICE_CONFIRM = 1
REBALANCE_DAYS = 20
WARMUP_BARS = 210
BT_START, BT_END = '2015-01-05', '2024-12-31'

MIN_DV = 0.03             # min_dividend_yield
CANDIDATE_POOL = 50
TOP_N = 5
FEE_BUY, FEE_SELL = 0.0004, 0.0009


def load_data() -> tuple[list[str], dict[str, float], dict, dict, dict, dict]:
    """(交易日历, breadth, bars[day][sym]=(o,c,pc,pct,dv,mcap), exdiv, gc001日息, alive)。"""
    bdf = pd.read_parquet(BREADTH)
    bdf['d'] = bdf['date'].astype(str).str[:10]
    bdf = bdf[(bdf['d'] >= BT_START) & (bdf['d'] <= BT_END)]
    days = bdf['d'].tolist()
    breadth = dict(zip(bdf['d'], bdf['breadth20'].astype(float)))

    bars: dict[str, dict[str, tuple]] = {}
    for sym_dir in sorted(DIV_STOCKS.iterdir()):
        if not sym_dir.is_dir() or not sym_dir.name.startswith(('sh.', 'sz.')):
            continue
        if sym_dir.name == 'sh.000300':
            continue                       # 指数不入选股域
        for pq in sorted(sym_dir.glob('*.parquet')):
            df = pd.read_parquet(
                pq, columns=['date', 'open', 'close', 'preclose', 'pctChg',
                             'dividend_yield', 'market_cap'])
            for r in df.itertuples(index=False):
                d = str(r.date)[:10]
                if d in breadth:
                    bars.setdefault(d, {})[sym_dir.name] = (
                        float(r.open), float(r.close), float(r.preclose),
                        float(r.pctChg),
                        float(r.dividend_yield) if pd.notna(r.dividend_yield) else 0.0,
                        float(r.market_cap) if pd.notna(r.market_cap) else 0.0)

    exdiv: dict[str, dict[str, tuple]] = {}
    for pq in sorted(EXDIV.glob('*.parquet')):
        df = pd.read_parquet(pq)
        exdiv[pq.stem] = {str(r.date)[:10]: (float(r.factor), float(r.cash_dividend))
                          for r in df.itertuples(index=False)}

    gdf = pd.read_parquet(GC001)
    gc001 = {str(r.date)[:10]: float(r.rate_annual) / 100.0 / 365.0
             for r in gdf.itertuples(index=False)}

    # alive_universe 口径：type=='1'（股票）+ ipoDate≤d + (outDate 空 或 >d)
    sb = pd.read_parquet(STOCK_BASIC)
    sb = sb[sb['type'].astype(str).str.strip() == '1']
    sb_ipo = sb['ipoDate'].astype(str).str[:10]
    sb_out = sb['outDate'].astype(str).str[:10]
    alive: dict[str, set] = {}
    for d in days:
        m = sb[(sb_ipo <= d) & ((sb_out == '') | (sb_out > d) | (sb_out == 'NaT'))]
        alive[d] = set(m['code'].astype(str))
    return days, breadth, bars, exdiv, gc001, alive


def bump_cap(b: float) -> float:
    """bump 形态：b≥attack→1；[defense,dz_edge)→BUMP_CAP；其余→0。"""
    if b >= ATK:
        return 1.0
    if DEF <= b < DZ_EDGE:
        return BUMP_CAP
    return 0.0


def hard_cap(b: float) -> float:
    return 1.0 if b >= ATK else 0.0


def select_basket(day_bars: dict, alive_set: set) -> dict[str, float]:
    """复刻 _select_stocks：dv≥3% → 降序 top50 → 前5 → 市值归一。"""
    cands = [(s, v[4], v[5]) for s, v in day_bars.items()
             if s in alive_set and v[4] >= MIN_DV and v[5] > 0]
    cands.sort(key=lambda x: -x[1])
    top = cands[:CANDIDATE_POOL][:TOP_N]
    tot = sum(c[2] for c in top)
    return {s: mc / tot for s, _, mc in top} if tot > 0 else {}


def main() -> int:
    days, breadth, bars, exdiv, gc001, alive = load_data()
    n = len(days)
    print(f'日历 {n} 天 {days[0]}~{days[-1]}')

    ice = False
    streak = 0
    last_rb = 0
    prev_b = None
    pending: list[tuple[int, dict[str, float]]] = []   # (fill_bar_idx, 目标权重)
    sleeve: dict[str, float] = {}                     # {sym: 市值/NAV}
    events: list[tuple[str, str, float, float]] = []
    incr_series: list[tuple[str, float]] = []
    nav_incr = 1.0
    sleeve_nav, sleeve_peak, sleeve_mdd = 1.0, 1.0, 0.0
    fee_drag = 0.0
    bump_days = invested_days = 0
    # 归因诊断：袖子空→非空开 episode，按（事件类型, 入带方向）分桶累计增量
    episode_tag: tuple[str, str] | None = None   # (kind, 'rising'/'falling')
    episode_pnl: dict[tuple[str, str], float] = {}
    b_hist: list[float] = []

    for i, d in enumerate(days, start=1):
        b = breadth[d]
        day_bars = bars.get(d, {})
        f_prev = sum(sleeve.values())               # 昨收持仓市值/NAV
        pnl = 0.0                                   # 当日袖子 P&L（NAV 分数）

        # ① 隔夜段：旧仓 prev_close→open（含除权现金/送转）
        for s in list(sleeve):
            if s not in day_bars:
                continue                            # 停牌冻结
            o, c, pc, pct, dv, mc = day_bars[s]
            factor, cash_div = exdiv.get(s, {}).get(d, (1.0, 0.0))
            new = sleeve[s] * (o / pc) * factor + sleeve[s] * cash_div / pc
            pnl += new - sleeve[s]
            sleeve[s] = new

        # ② 开盘成交：昨日事件日的目标权重（T+1）
        fee = 0.0
        if pending and pending[0][0] == i:
            target_w = pending.pop(0)[1]
            new_sleeve: dict[str, float] = {}
            for s, w in sleeve.items():             # 不在目标的仓 → 卖
                if s not in target_w:
                    if s in day_bars:
                        fee += sleeve[s] * FEE_SELL
                    else:
                        new_sleeve[s] = sleeve[s]   # 停牌卖不出，留存
                else:
                    new_sleeve[s] = sleeve[s]       # 目标内老仓保留开盘市值
            for s, w in target_w.items():
                if s in day_bars:
                    if s in new_sleeve:
                        diff = w - new_sleeve[s]    # 调仓差额（增/减）
                        fee += (diff * FEE_BUY if diff > 0 else -diff * FEE_SELL)
                        new_sleeve[s] = w
                    else:
                        new_sleeve[s] = w
                        fee += w * FEE_BUY
            sleeve = new_sleeve
            fee_drag += fee

        # ③ 日内段：open→close（含当日新买仓）
        for s in list(sleeve):
            if s not in day_bars:
                continue
            o, c, pc, pct, dv, mc = day_bars[s]
            delta = sleeve[s] * (c / o - 1.0)
            sleeve[s] += delta
            pnl += delta

        f_t = sum(sleeve.values())
        r_cash = gc001.get(d, 0.0)
        incr = pnl - f_prev * r_cash - fee          # 增量 = 袖子P&L − 现金机会成本 − 费
        nav_incr *= (1.0 + incr)
        incr_series.append((d, incr))
        if episode_tag is not None:
            episode_pnl[episode_tag] = episode_pnl.get(episode_tag, 0.0) + incr
            if f_t < 1e-9:
                episode_tag = None                  # 袖子清空 → episode 关闭
        if f_prev > 0:
            sleeve_nav *= 1.0 + pnl / f_prev
            invested_days += 1
        else:
            sleeve_nav *= 1.0 + r_cash
        sleeve_peak = max(sleeve_peak, sleeve_nav)
        sleeve_mdd = max(sleeve_mdd, (sleeve_peak - sleeve_nav) / sleeve_peak)

        # —— 当日事件判定（T 决策 → T+1 开盘成交）——
        if DEF <= b < DZ_EDGE:
            bump_days += 1
        if i < WARMUP_BARS:
            prev_b = b
            continue
        if ice:
            if b >= DEF:
                ice = False
                streak = 0
                events.append(('ice_release', d, b, 0.0))
            prev_b = b
            continue
        if b < DEF:
            streak += 1
            if streak >= ICE_CONFIRM:
                ice = True
                streak = 0
                events.append(('ice_confirm', d, b, -f_t))
                pending.append((i + 1, {}))
            prev_b = b
            continue
        streak = 0
        demote = prev_b is not None and prev_b >= ATK and b < ATK
        scheduled = (i - last_rb) >= REBALANCE_DAYS
        if scheduled:
            last_rb = i
        if scheduled or demote:
            delta_cap = bump_cap(b) - hard_cap(b)
            basket = select_basket(day_bars, alive[d]) if delta_cap > 0 else {}
            target = {s: w * delta_cap for s, w in basket.items()}
            kind = 'demote' if demote else 'rebalance'
            events.append((kind, d, b, delta_cap - f_t))
            if delta_cap > 0 and f_t < 1e-9 and episode_tag is None:
                # 新 episode：入带方向 = 近 5 日宽度动量
                direction = 'rising' if (len(b_hist) >= 5
                                         and b > b_hist[-5]) else 'falling'
                episode_tag = (kind, direction)
            if i + 1 <= n:
                pending.append((i + 1, target))
        prev_b = b
        b_hist.append(b)

    years = n / 242.0
    total_incr = nav_incr - 1.0
    ann_incr = nav_incr ** (1.0 / years) - 1.0
    n_events = sum(1 for e in events
                   if e[0] in ('rebalance', 'demote') and abs(e[3]) > 1e-9)
    by_year: dict[str, float] = {}
    for d, r in incr_series:
        by_year[d[:4]] = by_year.get(d[:4], 1.0) * (1.0 + r)
    by_year = {y: v - 1.0 for y, v in by_year.items()}

    result = {
        'config': {'bump_cap': BUMP_CAP, 'dz_edge': DZ_EDGE, 'defense': DEF,
                   'attack': ATK, 'rebalance_days': REBALANCE_DAYS,
                   'warmup_bars': WARMUP_BARS, 'ice_confirm': ICE_CONFIRM,
                   'fee_buy': FEE_BUY, 'fee_sell': FEE_SELL},
        'window': [days[0], days[-1]], 'trading_days': n,
        'bump_band_days': bump_days,
        'sleeve_invested_days': invested_days,
        'bump_events': n_events,
        'total_incr_return': total_incr,
        'annualized_incr_cagr_pp': ann_incr * 100,
        'sleeve_mdd': sleeve_mdd,
        'fee_drag_total': fee_drag,
        'incr_by_year': by_year,
        'episode_pnl_by_kind_direction': {
            f'{k[0]}|{k[1]}': round(v, 6) for k, v in episode_pnl.items()},
        'event_log_tail': [(k, d, round(bv, 4), round(dc, 4))
                         for k, d, bv, dc in events[-15:]],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_DIR / 'shadow_e14_bump.json.tmp'
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    tmp.replace(OUT_DIR / 'shadow_e14_bump.json')

    md = [f"# e14-bump 影子前瞻（{days[0]}~{days[-1]}，增量袖子口径）", '',
          f"- bump 带天数 {bump_days}（b∈[{DEF},{DZ_EDGE})）；袖子持仓日 {invested_days}",
          f"- 建仓/清仓事件 {n_events} 次；费用拖累合计 {fee_drag:.4%} NAV",
          f"- **增量总收益 {total_incr:+.2%} ⇒ 年化增量 ΔCAGR ≈ {ann_incr * 100:+.2f}pp**",
          f"- 袖子自身 MDD {sleeve_mdd:.2%}（增量回撤代价上限 proxy）",
          '', '| 年 | 增量收益 |', '|---|---|']
    md += [f'| {y} | {v:+.2%} |' for y, v in sorted(by_year.items())]
    md += ['', '### 归因：按建仓事件类型 × 入带方向（episode 累计增量）', '',
           '| 事件类型 | 方向 | 累计增量 |', '|---|---|---|']
    md += [f'| {k[0]} | {k[1]} | {v:+.3%} |'
           for k, v in sorted(episode_pnl.items())]
    (OUT_DIR / 'shadow_e14_bump.md').write_text('\n'.join(md), encoding='utf-8')
    print('\n'.join(md))
    print(f'\n产物 → {OUT_DIR}/shadow_e14_bump.{{json,md}}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
