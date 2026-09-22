#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通用单因子截面筛选器——任何 (date, ts_code, signal) 面板一键出裁决指标。

输入: parquet/csv 或内存 DataFrame，列含 date + ts_code + >=1 信号列
输出: 月频 rank IC(fwd20 超额) / t / ICIR / 分年 / 安慰剂 / 池内命中率

复用于 e60 一致预期族、notice 频次因子、fund_holdings 变体等。
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.lab.e23_shadow_screen import daily_returns  # noqa: E402

POOL_DIR = ROOT / 'data' / 'dividend_stocks'


def load_pool_codes() -> set:
    """池= data/dividend_stocks 下 sh.600000 / sz.000001 目录名。"""
    out = set()
    for d in POOL_DIR.iterdir():
        if not d.is_dir() or '.' not in d.name:
            continue
        ex, num = d.name.split('.', 1)
        ex = ex.upper()
        if ex in ('SH', 'SZ', 'BJ') and len(num) == 6 and num.isdigit():
            out.add(f'{num}.{ex}')
    return out


def fwd_panel(rets: pd.DataFrame, h: int = 20) -> pd.DataFrame:
    cp = (1 + rets.fillna(0)).cumprod()
    fwd = cp.shift(-h) / cp.shift(-1) - 1
    return fwd.sub(fwd.mean(axis=1), axis=0)


def screen(sig: pd.DataFrame, sig_col: str, rets: pd.DataFrame,
           pool: set | None = None, h: int = 20,
           placebo: bool = True) -> dict:
    """sig: [date, ts_code, sig_col]；date 为信号可见日（次日生效）。"""
    fwd = fwd_panel(rets, h)
    sig = sig.copy()
    sig['date'] = pd.to_datetime(sig['date'])
    mend = (pd.Series(rets.index, index=rets.index)
            .groupby(rets.index.to_period('M')).max())
    ics, n_pool, n_sig = [], 0, 0
    for d in mend:
        s = sig[sig['date'] <= d]
        if s.empty:
            continue
        v = (s.sort_values('date').groupby('ts_code')[sig_col].last()
             .dropna())
        r = fwd.loc[d]
        common = v.index.intersection(r.dropna().index)
        n_sig += len(common)
        if pool:
            n_pool += len(set(common) & pool)
        if len(common) < 100:
            continue
        ics.append((d, v[common].rank().corr(r[common].rank())))
    ic = pd.DataFrame(ics, columns=['date', 'ic']).set_index('date')
    out = {'n_months': int(len(ic)),
           'ic_mean': float(ic.ic.mean()) if len(ic) else None,
           't': float(ic.ic.mean() / ic.ic.std() * np.sqrt(len(ic)))
           if len(ic) > 2 else None,
           'icir_ann': float(ic.ic.mean() / ic.ic.std() * np.sqrt(12))
           if len(ic) > 2 and ic.ic.std() > 0 else None,
           'by_year': {str(k): float(v) for k, v in
                       ic.groupby(ic.index.year).ic.mean().items()}
           if len(ic) else {},
           'pool_hit_rate': (n_pool / n_sig) if n_sig and pool else None}
    if placebo and len(ic):
        rng = np.random.default_rng(42)
        pic = []
        for d in mend:
            s = sig[sig['date'] <= d]
            if s.empty:
                continue
            v = (s.sort_values('date').groupby('ts_code')[sig_col]
                 .last().dropna())
            r = fwd.loc[d]
            common = v.index.intersection(r.dropna().index)
            if len(common) < 100:
                continue
            pic.append(float(np.corrcoef(
                rng.permutation(v[common].rank().values),
                r[common].rank().values)[0, 1]))
        out['placebo_ic'] = float(np.mean(pic)) if pic else None
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('panel', help='parquet: date,ts_code,<signals>')
    ap.add_argument('--signals', required=True, help='逗号分隔信号列')
    ap.add_argument('--h', type=int, default=20)
    a = ap.parse_args()
    sig = pd.read_parquet(a.panel)
    close_w = pd.read_parquet(ROOT / 'experiments/lab/e27/panel_close.parquet')
    rets = daily_returns(close_w)
    pool = load_pool_codes()
    res = {}
    for c in a.signals.split(','):
        res[c] = screen(sig[['date', 'ts_code', c]].dropna(), c,
                        rets, pool, a.h)
        print(c, {k: (round(v, 4) if isinstance(v, float) else v)
                  for k, v in res[c].items()})
    out = Path(a.panel).with_suffix('.screen.json')
    import json
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print('->', out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
