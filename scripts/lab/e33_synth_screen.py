"""e33 候选库四族合成影子筛选（月频五分位框架）。

预登记：docs/E33_CANDIDATE_SYNTH_PREREG.md（冻结后方可 --run）。
四族（越大越好归一化）：S5=−holders_z4 / M5=−short_chg20 / F6=−roe_std8 /
I3=−(个股21d收益−CSRC行业均值)（e34 口径；H2 为稳健性旁证不入主合成）。
S2 撞冗余线（e31 ρ=0.585）按一族计不入合成。

臂：C1 等权 z 合成五分位价差；C2 四族同入各自最优五分位的交集篮子
（等权收益 vs 全 A 基线）；C3 leave-one-out 消融（C1 去掉各族）；
C4 单信号五分位价差族内复测对照。
闸门同 e26：t≥2.6(Bonferroni 4)+G2+G3+G4；M5 缺 margin 数据时 C1
以三族合成跑并在 meta 如实标注成员。
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
from scripts.lab import e25_factor_screen as e25  # noqa: E402
from scripts.lab import e26_margin_screen as e26  # noqa: E402
from scripts.lab import e27_insider_screen as e27  # noqa: E402
from scripts.lab import e28_gdhs_screen as e28  # noqa: E402
from scripts.lab import e31_candidate_crosscorr as e31  # noqa: E402
from scripts.lab.e26_margin_screen import eval_month, stats  # noqa: E402
from scripts.lab.e34_industry_screen import (
    IND_REV_WIN, industry_map_at, load_industry_snapshots)  # noqa: E402

OUT_DIR = ROOT / 'experiments' / 'lab' / 'e33'
MARGIN_DIR = ROOT / 'data' / 'margin_detail'
MARGIN_MIN_DAYS = 2300

FAMILIES = ['S5', 'M5', 'F6', 'I3']


def _note(m: str) -> None:
    print(f"[note] {m}")


def _z(s: pd.Series, ok: pd.Series) -> pd.Series:
    v = s[ok & s.notna()]
    if len(v) < 30 or v.std(ddof=0) == 0:
        return pd.Series(np.nan, index=s.index)
    return (s - v.mean()) / v.std(ddof=0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    ap.add_argument('--limit-days', type=int, default=None)
    args = ap.parse_args()
    if not args.run:
        print("⛔ 预登记未冻结；--run 前须 docs/E33_CANDIDATE_SYNTH_PREREG.md 冻结")
        return 2

    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suf = f"_lim{args.limit_days}" if args.limit_days else ""
    close_w = pd.read_parquet(e27.OUT_DIR / f'panel_close{suf}.parquet')
    r = e23.daily_returns(close_w)
    idx = r.index
    ipo = e23.first_seen_listed(r, close_w)
    listed_ok = pd.DataFrame(False, index=idx, columns=r.columns)
    ipo_map = {c: pd.to_datetime(v) for c, v in ipo.items() if v}
    for c in r.columns:
        t0i = ipo_map.get(c)
        if t0i is None:
            continue
        listed_ok[c] = ((idx >= t0i).cumsum() - 1) >= e23.MIN_LISTED_DAYS
    fwd = e23.fwd_returns(close_w, r)
    valid_cum = (~r.isna()).astype(float).cumsum()
    nanfrac = 1 - (valid_cum.shift(-e23.HORIZON) - valid_cum.shift(-1)) / e23.HORIZON
    _note(f"panel {close_w.shape}")

    pos = pd.Series(np.arange(len(idx)), index=idx)
    Ts = [T for T in e23.month_ends(idx)
          if int(pos[T]) + e23.HORIZON <= len(idx) - 1]

    # ---- 信号构件（同源 e31） ----
    long = e28.load_gdhs_records()
    gdhs_vis = e28.visible_signal(long, list(Ts))
    pit_cache = e25.load_pit()
    pit = {T: e25.pit_snapshot(T, pit_cache) for T in Ts}
    cs = np.log1p(r.fillna(0.0)).cumsum()
    ret21 = np.expm1(cs - cs.shift(IND_REV_WIN))
    snaps = load_industry_snapshots()

    m5 = None
    margin_dates = None
    n_margin = len(list(MARGIN_DIR.glob('*.parquet')))
    m5_in = False
    if n_margin >= MARGIN_MIN_DAYS:
        P26 = e26.load_panels(args.limit_days)
        m5 = e26.margin_signal_frames(P26)['short_chg20']
        margin_dates = P26['fin_balance'].index
        m5_in = True
        _note(f"margin_days={len(margin_dates)} M5 入合成")
    else:
        _note(f"margin_detail 仅 {n_margin} 日 <{MARGIN_MIN_DAYS}，"
              f"合成以三族(S5/F6/I3)跑，meta 标注")

    ctx31 = {'gdhs_vis': gdhs_vis, 'pit': pit, 'h2': ret21, 'm5': m5,
             'margin_dates': margin_dates,
             'six2ts': {t.split('.')[0]: t for t in r.columns}}

    active_fams = [f for f in FAMILIES if m5_in or f != 'M5']
    monthly_rows: list[dict] = []
    loo_rows: list[dict] = []
    single_rows: list[dict] = []

    for T in Ts:
        base_ok = listed_ok.loc[T] & close_w.loc[T].notna()
        fwd_row, nan_row = fwd.loc[T], nanfrac.loc[T]
        sigs = e31.build_signals(T, ctx31)          # S2/S5/F6/H2/(M5)
        # I3 行业内残差反转（越大越好）：−(r21 − 行业均值)
        imap = industry_map_at(snaps, T)
        if imap is not None:
            ind = imap.reindex(r.columns)
            tmp = pd.DataFrame({'v': ret21.loc[T], 'ind': ind})
            grp = tmp.dropna().groupby('ind')['v'].mean()
            sigs['I3'] = -(ret21.loc[T] - ind.map(grp))
        else:
            sigs['I3'] = None

        zs = {}
        for f in active_fams:
            s = sigs.get(f)
            if s is None:
                continue
            z = _z(s, base_ok)
            if z.notna().sum() >= 30:
                zs[f] = z
        # C1 等权 z 合成
        if len(zs) >= 2:
            comp = pd.concat(zs, axis=1).mean(axis=1)
            row = eval_month(comp, False, base_ok, fwd_row, nan_row)
            row['mrg_n'] = int(base_ok.sum())
            row['n_fams'] = len(zs)
            monthly_rows.append(dict(hyp='C1', T=str(T.date()), **row))
            # C3 leave-one-out
            for f in zs:
                loo = pd.concat({k: v for k, v in zs.items() if k != f},
                                axis=1).mean(axis=1)
                lrow = eval_month(loo, False, base_ok, fwd_row, nan_row)
                lrow['mrg_n'] = int(base_ok.sum())
                loo_rows.append(dict(hyp=f'C3_no{f}', T=str(T.date()), **lrow))
            # C2 交集：各族 top quintile
            qsets = []
            for f, z in zs.items():
                ok = base_ok & z.notna()
                n = int(ok.sum())
                if n >= 5:
                    qsets.append(set(z[ok].nlargest(max(1, n // 5)).index))
            if len(qsets) == len(zs) and qsets:
                inter = set.intersection(*qsets)
                ok_all = base_ok & fwd_row.notna()
                inter_ok = [c for c in inter if ok_all.get(c, False)]
                if len(inter_ok) >= 1 and int(ok_all.sum()) > 0:
                    ex = float(fwd_row[inter_ok].mean()
                               - fwd_row[ok_all].mean())
                    # C2 可检验统计量=交集篮子 vs 全 A 超额（写入 spread 供 t）
                    c2 = dict(n=len(inter), spread=ex, excess=ex,
                              nan_ratio=np.nan, _top=inter)
                else:
                    c2 = dict(n=0, spread=np.nan, excess=np.nan,
                              nan_ratio=np.nan, _top=None)
            else:
                c2 = dict(n=0, spread=np.nan, excess=np.nan,
                          nan_ratio=np.nan, _top=None)
            c2['mrg_n'] = int(base_ok.sum())
            monthly_rows.append(dict(hyp='C2', T=str(T.date()), **c2))
        # C4 单信号族内复测
        for f, z in zs.items():
            srow = eval_month(z, False, base_ok, fwd_row, nan_row)
            srow['mrg_n'] = int(base_ok.sum())
            single_rows.append(dict(hyp=f'C4_{f}', T=str(T.date()), **srow))

    res = stats(monthly_rows)
    # C2 的 G4 语义≠覆盖率：篮子宽度 ≥30 股的月占比 ≤30% 低样本门槛
    # （stats() 的 300 股地板对交集篮不适用——篮本来就小）
    c2_rows = [r for r in monthly_rows if r['hyp'] == 'C2']
    if 'C2' in res and c2_rows:
        df2 = pd.DataFrame(c2_rows)
        defined = df2[df2['n'] >= 1]
        low_frac = float((defined['n'] < 30).mean()) if len(defined) else np.nan
        v = res['C2']
        v['g4_basket_low_frac'] = low_frac
        v['G4'] = bool(not np.isnan(low_frac) and low_frac <= 0.30)
        t = v.get('t')
        if v.get('M', 0) < 60:
            v['grade'] = 'INCONCLUSIVE'
        elif not np.isnan(t) and t >= 2.6 and v['G2'] and v['G3'] and v['G4']:
            v['grade'] = '强'
        elif not np.isnan(t) and t >= 2.0 and v['G2'] and v['G3'] and v['G4']:
            v['grade'] = '弱'
        else:
            v['grade'] = '负'
    res_loo = stats(loo_rows)
    res_single = stats(single_rows)
    out = {'meta': {'months': len(Ts), 'm5_in_synth': m5_in,
                    'active_families': active_fams,
                    'window': [str(Ts[0].date()), str(Ts[-1].date())]},
           'synth': res, 'loo': res_loo, 'single': res_single}
    (OUT_DIR / 'e33_results.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str))
    _note(f"done in {time.time()-t0:.0f}s -> {OUT_DIR/'e33_results.json'}")
    for k, v in {**res, **res_loo, **res_single}.items():
        print(f"{k:10s} M={v.get('M')} t={v.get('t')} grade={v.get('grade')} "
              f"spread={v.get('mean_spread')} excess={v.get('mean_excess')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
