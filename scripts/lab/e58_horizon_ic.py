#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e58 信号 IC 衰减曲线 + 降频换手经济性诊断（T3 宽篮档位）。

Q: e58 ML 打分的预测力在 fwd20/40/60/80 上衰减多快？若 60-80td 仍有 IC，
   则季度调仓可省 2/3 换手成本 → T3(≥200万)宽篮净收益空间。

复用 e58 features.parquet 缓存 + 相同 walk-forward 配置；labels 按 e58
同口径 (cp[T+H]/cp[T+1]-1 截面超额) 对 H∈{20,40,60,80} 重算。
纯诊断，不动引擎、不做晋升裁决。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.lab.e23_shadow_screen import daily_returns  # noqa: E402
from scripts.lab.e58_ml_xsec import (EMBARGO_TD, EVENT_FEATS,  # noqa: E402
                                     LGBM_PARAMS, MIN_TRAIN_MONTHS,
                                     PRICE_FEATS, SCREEN_END, _zscore)

OUT = ROOT / 'experiments' / 'lab' / 'e58_horizon'
HORIZONS = (20, 40, 60, 80)


def main() -> int:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    X = pd.read_parquet(ROOT / 'experiments/lab/e58/features.parquet')
    for c in PRICE_FEATS + EVENT_FEATS + ['label']:
        X[c] = pd.to_numeric(X[c], errors='coerce')
    Xz = _zscore(X.copy())
    sig_days = sorted(Xz['sig_date'].unique())

    close_w = pd.read_parquet(
        ROOT / 'experiments/lab/e27/panel_close.parquet')
    r = daily_returns(close_w)
    cp = (1 + r).cumprod()
    days = cp.index
    labels = {}
    for h in HORIZONS:
        fwd = cp.shift(-h) / cp.shift(-1) - 1
        labels[h] = fwd.sub(fwd.mean(axis=1), axis=0)

    if (OUT / 'scores.parquet').exists():
        S = pd.read_parquet(OUT / 'scores.parquet')
        print(f'[note] reuse scores {S.shape}')
        churn_m = [0.792]
    else:
        scores = []      # walk-forward 月度重训，收全部测试月分数
        prev_top = set()
        churn_m = []
        for T in sig_days:
        tr = Xz[(Xz.sig_date < T - pd.Timedelta(days=EMBARGO_TD + 10))]
        tr = tr[tr.sig_date >= T - pd.Timedelta(days=MIN_TRAIN_MONTHS * 31)]
        te = Xz[Xz.sig_date == T]
        if len(te) < 100 or tr.sig_date.nunique() < MIN_TRAIN_MONTHS:
            continue
        ytr = tr.groupby('sig_date')['label'].rank(pct=True)
        ds = lgb.Dataset(tr[PRICE_FEATS + EVENT_FEATS], label=ytr)
        mdl = lgb.train(LGBM_PARAMS, ds)
        g = te.copy()
        g['score'] = mdl.predict(g[PRICE_FEATS + EVENT_FEATS])
        scores.append(g[['sig_date', 'ts_code', 'score']])
        top = set(g.nlargest(min(500, len(g)), 'score').ts_code)
        if prev_top:
            churn_m.append(1 - len(top & prev_top) / len(top))
        prev_top = top
    S = pd.concat(scores, ignore_index=True)
    S.to_parquet(OUT / 'scores.parquet')
    print(f'[note] scores {S.shape}, monthly top500 churn {np.mean(churn_m):.3f}')

    res = {'churn_monthly_top500': float(np.mean(churn_m))}
    for h in HORIZONS:
        lbl = labels[h]
        ics = []
        for d, g in S.groupby('sig_date'):
            d = pd.Timestamp(d)
            if d not in lbl.index:
                continue
            lv = lbl.loc[d].reindex(g.ts_code.values)
            ok = lv.notna().values
            if ok.sum() < 100:
                continue
            ic_v = pd.Series(g['score'].values[ok]).corr(
                pd.Series(lv.values[ok]), method='spearman')
            ics.append((d, ic_v, int(ok.sum())))
        ic = pd.DataFrame(ics, columns=['d', 'ic', 'n']).set_index('d')
        res[f'h{h}'] = {
            'n': int(len(ic)), 'ic_mean': float(ic.ic.mean()),
            'icir_ann': float(ic.ic.mean() / ic.ic.std() * np.sqrt(12)),
            'ic_pos_year': float(
                (ic.groupby(ic.index.year).ic.mean() > 0).mean()),
            'by_year': {str(k): float(v) for k, v in
                        ic.groupby(ic.index.year).ic.mean().items()},
        }
        print(f'H{h}: IC={ic.ic.mean():.4f} ICIR={res[f"h{h}"]["icir_ann"]:.2f}'
              f' n={len(ic)}')

    # 季度调仓换手估算：隔 3 月的 top500 交集
    tops = {}
    for d, g in S.groupby('sig_date'):
        tops[d] = set(g.nlargest(min(500, len(g)), 'score').ts_code)
    q_dates = sorted(tops)[::3]
    q_churn = [1 - len(tops[q_dates[i]] & tops[q_dates[i - 1]])
               / len(tops[q_dates[i]]) for i in range(1, len(q_dates))]
    res['churn_quarterly_top500'] = float(np.mean(q_churn))
    print(f'quarterly top500 churn {np.mean(q_churn):.3f}')
    res['elapsed_min'] = (time.time() - t0) / 60
    (OUT / 'horizon_ic.json').write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
