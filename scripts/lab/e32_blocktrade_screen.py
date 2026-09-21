"""e32 大宗交易事件族影子筛选（事件研究法）。

预登记：docs/E32_BLOCKTRADE_PREREG.md（冻结后方可 --run）。
形态：稀疏事件 → 事件窗 CAR（框架同源 e29：安慰剂前置门 + 截面均值窗基线
+ 事件日聚类 t + 中位/sign_ratio/年一致性三联）。

事件：data/block_trade/{YYYYMMDD}.parquet 逐日大宗成交明细。
列：trade_date/ts_code/deal_price/premium_discount_pct/volume/amount/
    amount_mv_ratio/buyer_broker/seller_broker。
T0=trade_date（大宗收盘后披露），T1=次一交易日入场；
CAR(h) = 个股窗口收益 − 同日全 A 有效股截面均值窗口收益，h∈{1,5,10,20}。

假设（冻结口径）：
B1 溢价(premium>0) vs 折价(<0) 分臂；B2 买方机构专用 vs 卖方机构专用分臂；
B3 amount_mv_ratio 三分位单调性；B4 同股同日≥2 笔 vs 单笔；B5 对照披露。
判「强」需簇稳健 t≥2.6 且 h20 CAR 与假设方向同号且多窗一致且年一致性≥0.6
且 n≥300（负向臂对称：t≤−2.6 且 CAR<0）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.lab.e23_shadow_screen import MIN_LISTED_DAYS, daily_returns  # noqa: E402
from scripts.lab import e27_insider_screen as e27  # noqa: E402
from scripts.lab.e29_lhb_screen import (  # noqa: E402
    HORIZONS, MIN_EVENTS, STRONG_T, YEAR_CONS_MIN,
    arm_stats, baseline_mean, car_table, fwd_panels, placebo_gate, verdict,
)

BT_DIR = ROOT / 'data' / 'block_trade'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e32'

_INST = '机构专用'


def _note(m: str) -> None:
    print(f"[note] {m}")


def load_block_trade() -> pd.DataFrame:
    frames = []
    for f in sorted(BT_DIR.glob('*.parquet')):
        if f.stem.startswith('_'):
            continue
        d = pd.read_parquet(f)
        d['trade_date'] = pd.to_datetime(d['trade_date'], format='%Y-%m-%d')
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    for c in ('premium_discount_pct', 'amount_mv_ratio', 'amount'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['buyer_inst'] = df['buyer_broker'].astype(str).str.contains(_INST)
    df['seller_inst'] = df['seller_broker'].astype(str).str.contains(_INST)
    # 同股同日多笔 → 事件合并：金额/市值比求和，溢折价按金额加权
    df = df.sort_values('trade_date')
    g = df.groupby(['trade_date', 'ts_code'])
    def _wavg(x):
        w = df.loc[x.index, 'amount']
        return float(np.average(x, weights=w)) if w.sum() > 0 else float(x.mean())
    ev = g.agg(amount=('amount', 'sum'),
               amount_mv_ratio=('amount_mv_ratio', 'sum'),
               prem=_wavg('premium_discount_pct'),
               buyer_inst=('buyer_inst', 'any'),
               seller_inst=('seller_inst', 'any'),
               n_deals=('amount', 'size')).reset_index()
    return ev


def verdict_signed(st: dict, expect: int) -> str:
    """方向感知裁决：expect=+1 要求 h20 car>0 且 t≥STRONG_T；
    expect=−1 要求 h20 car<0 且 t≤−STRONG_T（负漂确认）。"""
    n = st.get('n_events', 0)
    if n < MIN_EVENTS:
        return 'INCONCLUSIVE(样本不足)'
    h20 = st.get('h20', {})
    t = h20.get('t_cluster', np.nan)
    cm = h20.get('car_mean', np.nan)
    if np.isnan(t) or np.isnan(cm):
        return 'INCONCLUSIVE'
    dirs = [st.get(f'h{h}', {}).get('car_mean', np.nan) for h in HORIZONS]
    dirs = [d for d in dirs if not np.isnan(d)]
    same = len(dirs) > 0 and (all(d > 0 for d in dirs) or all(d < 0 for d in dirs))
    yc = h20.get('year_cons', 0) if expect > 0 else 1 - h20.get('year_cons', 1)
    if expect > 0:
        strong = t >= STRONG_T and cm > 0 and same and yc >= YEAR_CONS_MIN
    else:
        strong = t <= -STRONG_T and cm < 0 and same and yc >= YEAR_CONS_MIN
    if strong:
        return '强'
    if abs(t) >= 2.0:
        return '弱'
    return '负'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true', help='冻结后才允许运行')
    ap.add_argument('--limit-days', type=int, default=None)
    args = ap.parse_args()
    if not args.run:
        print("⛔ 预登记未冻结；--run 前须 docs/E32_BLOCKTRADE_PREREG.md 冻结")
        return 2

    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suf = f"_lim{args.limit_days}" if args.limit_days else ""
    if not (e27.OUT_DIR / f'panel_close{suf}.parquet').exists():
        _note("build panel (close/circ_mv via e27 loader)")
        e27.build_panel(args.limit_days)
    close_w = pd.read_parquet(e27.OUT_DIR / f'panel_close{suf}.parquet')
    r = daily_returns(close_w)
    valid = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    days_idx = r.index
    _note(f"panel close {close_w.shape}; trading days={len(close_w)}")

    ev = load_block_trade()
    _note(f"block_trade events(merged)={len(ev)}")
    day_set = set(days_idx)
    col_set = set(r.columns)
    ev = ev[ev['trade_date'].isin(day_set) & ev['ts_code'].isin(col_set)]
    _note(f"panel-matched events={len(ev)}")

    fwd = fwd_panels(r)
    base = baseline_mean(fwd, valid)
    plc = placebo_gate(r, valid, days_idx, fwd, base)
    _note(f"placebo gate: {plc}")
    results: dict = {'meta': {'panel_days': len(days_idx), 'horizons': HORIZONS,
                              'min_events': MIN_EVENTS, 'strong_t': STRONG_T},
                     'placebo': plc}
    if not plc['pass']:
        _note("⛔ 安慰剂检验失败——基线有偏，拒绝产出假设判定")
        results['aborted'] = 'placebo_gate_failed'
        (OUT_DIR / 'e32_results.json').write_text(
            json.dumps(results, ensure_ascii=False, indent=1, default=str))
        return 3

    # ---------- arms ----------
    arms_expect: dict[str, tuple[pd.DataFrame, int]] = {}
    prem = ev.dropna(subset=['prem'])
    arms_expect['B1_prem'] = (prem[prem['prem'] > 0], +1)      # 溢价接盘
    arms_expect['B1_disc'] = (prem[prem['prem'] < 0], -1)     # 折价甩卖
    arms_expect['B2_buyer_inst'] = (ev[ev['buyer_inst']], +1)
    arms_expect['B2_seller_inst'] = (ev[ev['seller_inst'] & ~ev['buyer_inst']], -1)
    arms_expect['B4_multi'] = (ev[ev['n_deals'] >= 2], -1)
    arms_expect['B4_single'] = (ev[ev['n_deals'] == 1], 0)    # 0=中性披露

    for name, (sub, expect) in arms_expect.items():
        car = car_table(sub[['trade_date', 'ts_code']], r, valid, days_idx,
                        fwd, base)
        st = arm_stats(car, name)
        st['verdict'] = verdict_signed(st, expect) if expect != 0 else verdict(st)
        results[name] = st
        _note(f"{name}: n={st['n_events']} verdict={st['verdict']} "
              f"h20 t_clu={st.get('h20', {}).get('t_cluster')}")

    # B3 规模强度三分位
    b3 = ev.dropna(subset=['amount_mv_ratio'])
    if len(b3) >= 3 * MIN_EVENTS:
        try:
            b3 = b3.copy()
            b3['terc'] = pd.qcut(b3['amount_mv_ratio'], 3,
                                 labels=['lo', 'mid', 'hi'])
        except ValueError:
            b3['terc'] = None
        tstats = {}
        for gname in ('lo', 'mid', 'hi'):
            sub = b3[b3['terc'] == gname]
            car = car_table(sub[['trade_date', 'ts_code']], r, valid, days_idx,
                            fwd, base)
            tstats[gname] = arm_stats(car, f'B3_{gname}')
            tstats[gname]['verdict'] = verdict(tstats[gname])
        means = [tstats[g].get('h20', {}).get('car_mean', np.nan)
                 for g in ('lo', 'mid', 'hi')]
        mono = all(not np.isnan(m) for m in means) and means[0] <= means[1] <= means[2]
        top = tstats['hi']
        results['B3'] = {'terciles': tstats, 'monotone': mono,
                         'verdict': '强' if (top['verdict'] == '强' and mono)
                         else ('弱' if top['verdict'] != '负' else '负'),
                         'n_events': int(len(b3))}
    else:
        results['B3'] = {'verdict': 'INCONCLUSIVE(样本不足)',
                         'n_events': int(len(b3))}

    results['B5_control'] = {'note': '同日全A截面均值基线构造上≈0，披露事件天数',
                             'n_days': int(ev['trade_date'].nunique())}

    (OUT_DIR / 'e32_results.json').write_text(
        json.dumps(results, ensure_ascii=False, indent=1, default=str))
    _note(f"done in {time.time()-t0:.0f}s -> {OUT_DIR/'e32_results.json'}")
    for k, v in results.items():
        if k in ('meta', 'placebo'):
            continue
        print(f"{k:16s} n={v.get('n_events', 0):6d} verdict={v.get('verdict')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
