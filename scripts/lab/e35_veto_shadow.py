"""e35 风控 veto 层影子测试（描述性评估，非假设筛选，不需冻结预登记——
它复用已收单族的事件定义，回答的是工程问题：veto 层在池内有没有边际）。

被测 veto 规则（全部为已收单的负漂形态）：
V1 户数激增：最近 gdhs 期户数环比 >+30%（e28 剔除过滤器口径）
V2 重复上榜：近 10 交易日 ≥2 次上龙虎榜（e29-L4_rep）
V3 跌榜：近 10 交易日上过跌幅触发榜（e29-L5_dn）
V4 机构大宗卖出：近 20 交易日出现 seller=机构专用 的大宗（e32-B2_seller_inst）

评价：C3 年度池成员内，月末 T 对每票算 veto 命中（≤T 数据）；
前瞻窗 = T→T+21d 个股收益。统计：命中组 vs 未命中组月均收益差
（per-month diff → t 检验）；再按年分布一致性。
如实披露：C3 池 ≠ e8b 487 池（年度质量过滤池），结论外推有界。
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

from scripts.lab import e23_shadow_screen as e23  # noqa: E402
from scripts.lab import e27_insider_screen as e27  # noqa: E402
from scripts.lab import e28_gdhs_screen as e28  # noqa: E402
from scripts.lab import e32_blocktrade_screen as e32  # noqa: E402
from scripts.lab.e29_lhb_screen import REASON_DOWN, load_lhb  # noqa: E402

OUT_DIR = ROOT / 'experiments' / 'lab' / 'e35'
POOL_PATH = ROOT / 'data' / 'c3_pool' / 'pool_yearly.parquet'
GDHS_SURGE = 0.30          # V1 阈值（e28 冻结口径）
LHB_WIN, BT_WIN = 10, 20   # V2/V3、V4 回看窗（交易日）


def _note(m: str) -> None:
    print(f"[note] {m}")


def pool_members(T: pd.Timestamp, pool_df: pd.DataFrame) -> list[str]:
    """C3 池年度成员（year=首个交易日快照口径）；'sh.600000'→'600000.SH'。"""
    row = pool_df[pool_df['year'] == T.year]
    if row.empty:
        return []
    syms = row.iloc[0]['symbols']
    return [s.split('.')[1] + '.' + s.split('.')[0].upper() for s in syms]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit-days', type=int, default=None)
    args = ap.parse_args()
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    suf = f"_lim{args.limit_days}" if args.limit_days else ""
    close_w = pd.read_parquet(e27.OUT_DIR / f'panel_close{suf}.parquet')
    r = e23.daily_returns(close_w)
    idx = r.index
    pos = pd.Series(np.arange(len(idx)), index=idx)
    Ts = [T for T in e23.month_ends(idx)
          if int(pos[T]) + e23.HORIZON <= len(idx) - 1]
    fwd = e23.fwd_returns(close_w, r)
    _note(f"panel {close_w.shape}")

    pool_df = pd.read_parquet(POOL_PATH)

    # V1 gdhs：每股最新可见期户数环比（月末 T 前已公告）
    long = e28.load_gdhs_records()
    long = long.sort_values('ann_date')
    gdhs_by_code = {c: g for c, g in long.groupby('code')}
    _note(f"gdhs codes={len(gdhs_by_code)}")

    # V2/V3 lhb 事件
    lhb = load_lhb()
    lhb['is_down'] = lhb['reason'].str.contains('|'.join(REASON_DOWN),
                                              na=False)
    # 每股事件日列表
    lhb_days = lhb.groupby('ts_code')['trade_date'].apply(
        lambda s: sorted(set(s))).to_dict()
    lhb_dn_days = lhb[lhb['is_down']].groupby('ts_code')['trade_date'].apply(
        lambda s: sorted(set(s))).to_dict()
    _note(f"lhb stocks={len(lhb_days)} down={len(lhb_dn_days)}")

    # V4 block_trade 机构卖方事件
    bt = e32.load_block_trade()
    bt_days = bt[bt['seller_inst']].groupby('ts_code')['trade_date'].apply(
        lambda s: sorted(set(s))).to_dict()
    _note(f"bt inst-sell stocks={len(bt_days)}")

    def veto_flags(code: str, T: pd.Timestamp) -> dict[str, bool]:
        fl = {'V1': False, 'V2': False, 'V3': False, 'V4': False}
        g = gdhs_by_code.get(code.split('.')[0])  # gdhs 键=裸 6 位码
        if g is not None:
            g2 = g[g['ann_date'] <= T]
            if len(g2):
                last = g2.iloc[-1]
                prev = float(last['holders_prev'])
                cur = float(last['holders'])
                if prev > 0 and cur / prev - 1 > GDHS_SURGE:
                    fl['V1'] = True
        for src, win, key in ((lhb_days, LHB_WIN, 'V2'),
                              (lhb_dn_days, LHB_WIN, 'V3'),
                              (bt_days, BT_WIN, 'V4')):
            days = src.get(code)
            if days:
                p = int(pos[T])
                lo = idx[max(0, p - win + 1)]
                if key == 'V2':
                    cnt = sum(1 for d in days if lo <= d <= T)
                    if cnt >= 2:
                        fl['V2'] = True
                elif any(lo <= d <= T for d in days):
                    fl[key] = True
        return fl

    rows = []
    for T in Ts:
        members = [c for c in pool_members(T, pool_df) if c in r.columns]
        if not members:
            continue
        fwd_row = fwd.loc[T]
        for c in members:
            fv = fwd_row.get(c, np.nan)
            if np.isnan(fv):
                continue
            fl = veto_flags(c, T)
            rows.append({'T': T, 'ts_code': c, 'fwd': float(fv),
                         **fl, 'any_veto': any(fl.values())})
    df = pd.DataFrame(rows)
    _note(f"pool-month rows={len(df)}")

    out: dict = {'meta': {'months': len(Ts), 'pool': 'c3_pool yearly',
                          'note': '影子评估非冻结假设族；C3池≠e8b 487池'}}
    grp = df.groupby('T')
    diffs = grp.apply(lambda g: float(
        g.loc[~g['any_veto'], 'fwd'].mean()
        - g.loc[g['any_veto'], 'fwd'].mean())
        if g['any_veto'].any() and (~g['any_veto']).any() else np.nan).dropna()
    out['any_veto'] = {
        'months': int(len(diffs)),
        'mean_diff': float(diffs.mean()),
        't': float(diffs.mean() / (diffs.std(ddof=1) / np.sqrt(len(diffs))))
        if len(diffs) > 1 else np.nan,
        'hit_rate': float(df['any_veto'].mean()),
        'hit_fwd_mean': float(df.loc[df['any_veto'], 'fwd'].mean()),
        'nohit_fwd_mean': float(df.loc[~df['any_veto'], 'fwd'].mean()),
        'year_cons': float(
            (diffs.groupby(diffs.index.year).mean() > 0).mean()),
    }
    for v in ('V1', 'V2', 'V3', 'V4'):
        sub = df[df[v] | ~df[v]]
        d2 = grp.apply(lambda g: float(
            g.loc[~g[v], 'fwd'].mean() - g.loc[g[v], 'fwd'].mean())
            if g[v].any() else np.nan).dropna()
        out[v] = {
            'months': int(len(d2)),
            'mean_diff': float(d2.mean()) if len(d2) else np.nan,
            't': float(d2.mean() / (d2.std(ddof=1) / np.sqrt(len(d2))))
            if len(d2) > 1 else np.nan,
            'hit_rate': float(df[v].mean()),
        }
    (OUT_DIR / 'e35_results.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str))
    _note(f"done in {time.time()-t0:.0f}s -> {OUT_DIR/'e35_results.json'}")
    print(json.dumps({k: v for k, v in out.items() if k != 'meta'},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
