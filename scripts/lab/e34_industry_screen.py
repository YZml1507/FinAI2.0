"""e34 行业分类因子族影子筛选（月频五分位框架）。

预登记：docs/E34_INDUSTRY_PREREG.md（冻结后方可 --run）。
信号=个股所属行业得分（行业收益=成员等权均值，PIT 快照归属）。
假设：I1 行业动量(63d) / I2 行业反转(21d,低好) / I3 行业内残差反转(21d,低好)
     / I4 行业广度(r20>0 占比,高好) / I5 族内对照=个股 21d 反转(=H2 口径)。
框架与闸门同 e26（t≥2.6+G2 年一致性+G3 换手成本+G4 覆盖）。
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
from scripts.lab.e26_margin_screen import eval_month, stats  # noqa: E402

IND_DIR = ROOT / 'data' / 'industry'
OUT_DIR = ROOT / 'experiments' / 'lab' / 'e34'

MIN_DEFINED_N = 30       # eval_month 下限（行业分层每分位须够票）
IND_MOM_WIN = 63         # I1 行业动量窗
IND_REV_WIN = 21         # I2/I3 窗
IND_BREADTH_WIN = 20     # I4 广度窗


def _note(m: str) -> None:
    print(f"[note] {m}")


def load_industry_snapshots() -> dict[pd.Timestamp, pd.Series]:
    """{快照日: Series(code→industry)}；剔除 industry 空值行。"""
    out = {}
    for f in sorted(IND_DIR.glob('*.parquet')):
        if f.stem.startswith('_'):
            continue
        snap_date = pd.Timestamp(f.stem)
        d = pd.read_parquet(f)
        d = d[d['industry'].notna() & (d['industry'].astype(str).str.len() > 0)]
        # baostock code 'sh.600000' → '600000.SH'
        codes = d['code'].astype(str)
        ts = codes.str.split('.').str[1] + '.' + codes.str.split('.').str[0].str.upper()
        out[snap_date] = pd.Series(d['industry'].values, index=ts.values)
    return out


def industry_map_at(snaps: dict, T: pd.Timestamp) -> pd.Series | None:
    """PIT：取文件日期 ≤T 的最新快照。"""
    ks = [k for k in snaps if k <= T]
    return snaps[max(ks)] if ks else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    ap.add_argument('--limit-days', type=int, default=None)
    args = ap.parse_args()
    if not args.run:
        print("⛔ 预登记未冻结；--run 前须 docs/E34_INDUSTRY_PREREG.md 冻结")
        return 2

    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    from scripts.lab import e27_insider_screen as e27
    suf = f"_lim{args.limit_days}" if args.limit_days else ""
    if not (e27.OUT_DIR / f'panel_close{suf}.parquet').exists():
        _note("build panel")
        e27.build_panel(args.limit_days)
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

    snaps = load_industry_snapshots()
    _note(f"industry snapshots={len(snaps)}")
    snap_days = np.array(sorted(snaps.keys()))

    # 预计算滚动窗收益（对数复利）：win_ret[t] = 个股 t-win+1..t 累计收益
    cs = np.log1p(r.fillna(0.0)).cumsum()
    ret = {w: np.expm1(cs - cs.shift(w)) for w in
           (IND_MOM_WIN, IND_REV_WIN, IND_BREADTH_WIN)}

    pos = pd.Series(np.arange(len(idx)), index=idx)
    Ts = [T for T in e23.month_ends(idx)
          if int(pos[T]) + e23.HORIZON <= len(idx) - 1]

    hyps = ['I1', 'I2', 'I3', 'I4', 'I5']
    monthly_rows: list[dict] = []
    for T in Ts:
        base_ok = listed_ok.loc[T] & close_w.loc[T].notna()
        fwd_row, nan_row = fwd.loc[T], nanfrac.loc[T]
        imap = industry_map_at(snaps, T)
        if imap is None:
            continue
        ind = imap.reindex(r.columns)
        has_ind = ind.notna()

        # 行业 trailing 收益 = 成员等权均值
        def _ind_score(win_ret_row: pd.Series) -> pd.Series:
            tmp = pd.DataFrame({'v': win_ret_row, 'ind': ind})
            grp = tmp.dropna().groupby('ind')['v'].mean()
            return ind.map(grp)

        mom63 = _ind_score(ret[IND_MOM_WIN].loc[T])
        rev21 = _ind_score(ret[IND_REV_WIN].loc[T])
        resid21 = ret[IND_REV_WIN].loc[T] - rev21          # 行业内残差
        breadth = _ind_score((ret[IND_BREADTH_WIN].loc[T] > 0).astype(float))

        sigs = {
            'I1': (mom63, False), 'I2': (rev21, True),
            'I3': (resid21, True), 'I4': (breadth, False),
            'I5': (ret[IND_REV_WIN].loc[T], True),
        }
        for h in hyps:
            sig, low = sigs[h]
            sig = sig.where(has_ind) if h != 'I5' else sig
            row = eval_month(sig, low, base_ok, fwd_row, nan_row)
            # G4 复用 mrg_n 字段语义=当日有效宇宙总数（覆盖分子=n）
            row['mrg_n'] = int(base_ok.sum())
            monthly_rows.append(dict(hyp=h, T=str(T.date()), **row))

    res = stats(monthly_rows)
    out = {'meta': {'months': len(Ts), 'window': [str(Ts[0].date()),
                    str(Ts[-1].date())], 'framework': 'e26 skeleton'},
           'results': res,
           'monthly': [{k: (v if not isinstance(v, set) else len(v))
                        for k, v in row.items()} for row in monthly_rows]}
    (OUT_DIR / 'e34_results.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str))
    _note(f"done in {time.time()-t0:.0f}s -> {OUT_DIR/'e34_results.json'}")
    for h in hyps:
        v = res.get(h, {})
        print(f"{h}: M={v.get('M')} t={v.get('t')} grade={v.get('grade')} "
              f"G2={v.get('G2')} G3={v.get('G3')} G4={v.get('G4')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
