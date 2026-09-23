"""e80 损失函数变体：XGBClassifier top-quintile 二分类分数（本地 CPU）。

与 e63_score_sweep_local 同构（同特征集/同 walk-forward/同 z-score/同
embargo），仅 label 构造与模型类不同：
  ytr = (截面 rank_pct(label150) ≥ 0.8).astype(int)
  score = predict_proba[:, 1]
输出 experiments/lab/e63/scores_label150_cls.parquet + results。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

ROOT = Path(__file__).resolve().parents[2]
XPATH = ROOT / "experiments/lab/e63_Xlab4.parquet"
OUT = ROOT / "experiments/lab/e63"

BASE = ['ret5', 'ret20', 'ret60', 'ret120', 'vol20', 'max20',
        'turnover20', 'amihud20', 'pe', 'pb', 'dv_ttm', 'circ_mv',
        'log_circ_mv', 'fin_bal_chg20', 'short_qty_chg20',
        'ev_letter', 'ev_resumption', 'ev_fc_pos', 'ev_fc_neg',
        'ev_incentive', 'ev_lhb', 'ev_insider_sell', 'ev_bt_inst_sell',
        'ev_reduce', 'ev_frozen', 'an_rating_dir20', 'an_epsrev20']
MIN_TRAIN_MONTHS, EMBARGO_TD = 24, 20
PARAMS = dict(max_depth=6, learning_rate=0.03, n_estimators=600,
              min_child_weight=80, subsample=0.9, colsample_bytree=0.8,
              tree_method='hist', device='cpu', n_jobs=8,
              eval_metric='logloss')
LABEL = 'label150'


def run_label(X: pd.DataFrame, label: str) -> dict:
    sig_days = np.array(sorted(X['sig_date'].unique()))
    months = sig_days[sig_days >= pd.Timestamp('2017-01-01')]
    frames, ics = [], []
    for T in months:
        tr_days = sig_days[
            sig_days < T - pd.Timedelta(days=EMBARGO_TD + 10)
        ][-MIN_TRAIN_MONTHS:]
        tr = X[X['sig_date'].isin(tr_days) & X[label].notna()]
        te = X[X['sig_date'] == T]
        if len(te) < 100 or len(tr_days) < MIN_TRAIN_MONTHS:
            continue
        rk = tr.groupby('sig_date')[label].rank(pct=True)
        ytr = (rk >= 0.8).astype(int)
        if ytr.nunique() < 2:
            continue
        m = xgb.XGBClassifier(**PARAMS)
        m.fit(tr[BASE], ytr)
        sc = m.predict_proba(te[BASE])[:, 1]
        lab = te[label]
        if lab.notna().sum() > 100:
            ics.append(pd.Series(sc[lab.notna().values]).corr(
                pd.Series(lab[lab.notna()].values), method='spearman'))
        frames.append(pd.DataFrame({'sig_date': T,
                                    'ts_code': te['ts_code'].values,
                                    'score': sc}))
    s = pd.Series(ics)
    res = {'n_months_labeled': int(s.size), 'ic_mean': float(s.mean()),
           't': float(s.mean() / (s.std() / np.sqrt(s.size))),
           'n_sig_scored': len(frames)}
    pd.concat(frames, ignore_index=True).to_parquet(
        OUT / 'scores_label150_cls.parquet')
    return res


def main() -> int:
    X = pd.read_parquet(XPATH)
    X = X[X['valid']].copy()
    for c in BASE:
        X[c] = pd.to_numeric(X[c], errors='coerce')
    mu = X.groupby('sig_date')[BASE].transform('mean')
    sd = X.groupby('sig_date')[BASE].transform('std').replace(0, np.nan)
    X[BASE] = ((X[BASE] - mu) / sd).fillna(0)
    X[LABEL] = pd.to_numeric(X[LABEL], errors='coerce')
    t0 = time.time()
    res = run_label(X, LABEL)
    res['elapsed_min'] = round((time.time() - t0) / 60, 1)
    (OUT / 'results_label150_cls.json').write_text(json.dumps(res, indent=1))
    print(LABEL + '_cls', res, flush=True)
    print('DONE', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
