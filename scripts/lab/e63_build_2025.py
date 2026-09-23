"""e63 特征矩阵 2025+ 重建（OOS-2025 观察用，不训练）。

复刻 e63_Xlab4 的 46 列口径：
  panel = experiments/lab/e27/panel_close_2025.parquet（→2026-09-22）
  sig_days = 月末 2025-01 → 2026-08 + 最新交易日（打分用）
  基座特征/事件旗标/分析师窗：复用 e58_ml_xsec 的同名函数
  s1-s4/fwd_ep：merge_asof 向后取 ≤T 最新（data/consensus/）
  fund_cov：报告期 period_end+120d 可见性（data/fund_holdings_agg/）
  fund_cov_chg：fund_cov 环比上月
  label{h}：fwd h 日超额截面去均值；到期不足记 NaN（打分不依赖）

输出 experiments/lab/e63_Xlab_2025.parquet + build_notes.json
"""
from __future__ import annotations

import glob
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.lab.e23_shadow_screen import MIN_LISTED_DAYS, daily_returns  # noqa: E402
from scripts.lab import e58_ml_xsec as e58  # noqa: E402
from scripts.lab.e83_build_aux_features import (  # noqa: E402
    build_ann_cnt60, build_gdhs_qoq)

PANEL = ROOT / "experiments/lab/e27/panel_close_2025.parquet"
OUT = ROOT / "experiments/lab/e63_Xlab_2025.parquet"
NOTES = ROOT / "experiments/lab/e63_build_2025_notes.json"
HORIZONS = (20, 40, 60, 90, 120, 150, 180, 240)
FUND_LAG_DAYS = 120
SIG_START = pd.Timestamp("2025-01-01")


def _load_consensus() -> pd.DataFrame:
    m = pd.read_parquet(ROOT / "data/consensus/sig_monthly.parquet")
    ep = pd.read_parquet(ROOT / "data/consensus/sig_ep.parquet")
    m["date"] = pd.to_datetime(m["date"])
    ep["date"] = pd.to_datetime(ep["date"])
    c = m.merge(ep, on=["date", "ts_code"], how="outer").sort_values("date")
    return c


def _load_fund_cov(days) -> pd.DataFrame:
    """fund_cov panel: 每交易日×股票 最近可见报告期的基金覆盖家数。"""
    frames = []
    for f in sorted(glob.glob(str(ROOT / "data/fund_holdings_agg/*.parquet"))):
        d = pd.read_parquet(f)
        if d.empty:
            continue
        d["avail"] = pd.to_datetime(d["报告期"]) + pd.Timedelta(
            days=FUND_LAG_DAYS)
        frames.append(d[["avail", "股票代码", "基金覆盖家数"]])
    f = pd.concat(frames).dropna(subset=["avail"])
    f["ts_code"] = f["股票代码"].astype(str).map(
        lambda s: f"{s}.SH" if s.startswith("6") else f"{s}.SZ")
    f = f.sort_values("avail")
    # asof join 每只股
    panel = pd.DataFrame(index=days)
    for code, g in f.groupby("ts_code"):
        s = pd.Series(g["基金覆盖家数"].values, index=g["avail"].values)
        panel[code] = s.reindex(s.index.union(days)).ffill().reindex(days)
    return panel


def main() -> int:
    t0 = time.time()
    close_w = pd.read_parquet(PANEL)
    r = daily_returns(close_w)
    days = r.index
    month_ends = pd.Series(days).groupby([days.year, days.month]).max()
    sig_days = pd.DatetimeIndex(sorted(month_ends))
    sig_days = sig_days[sig_days >= SIG_START]
    last_day = days[-1]
    if last_day > sig_days[-1]:
        sig_days = sig_days.append(pd.DatetimeIndex([last_day]))
    print(f"[note] sig_days {len(sig_days)} {sig_days[0]}→{sig_days[-1]}",
          flush=True)

    # 价量（同 e58）
    feats = {}
    for h in (5, 20, 60, 120):
        feats[f"ret{h}"] = close_w / close_w.shift(h) - 1
    feats["vol20"] = r.rolling(20).std()
    feats["max20"] = r.rolling(20).max()
    db = e58._load_daily_basic()
    tov = db["turnover_rate"] / 100.0
    feats["turnover20"] = tov.rolling(20).mean().reindex(
        index=days, columns=close_w.columns)
    amt = tov * db["circ_mv"] * 1e4
    amih = (r.abs() / amt.replace(0, np.nan)) * 1e8
    feats["amihud20"] = amih.rolling(20).mean().reindex(
        index=days, columns=close_w.columns)
    for c in ("pe", "pb", "dv_ttm", "circ_mv"):
        feats[c] = db[c].reindex(index=days, columns=close_w.columns)
    feats["log_circ_mv"] = np.log(feats["circ_mv"].clip(lower=1e6))
    feats = {k: v.reindex(index=days) for k, v in feats.items()}

    mg = e58._load_margin(days, close_w.columns)
    feats["fin_bal_chg20"] = (mg["fin_balance"]
                            / mg["fin_balance"].shift(20) - 1)
    feats["short_qty_chg20"] = (mg["short_qty"]
                              / mg["short_qty"].replace(0, np.nan).shift(20)
                              - 1)

    cp = (1 + r).cumprod()
    labels = {}
    for h in HORIZONS:
        fwd = cp.shift(-h) / cp.shift(-1) - 1
        labels[f"label{h}"] = fwd.sub(fwd.mean(axis=1), axis=0)
    labels["label"] = labels["label20"]

    print("[note] building event sets...", flush=True)
    esets = e58.build_event_sets(days)
    # 复牌事件用 2025 面板重算（e58 内置读旧 panel 仅到 2024）
    from scripts.lab import e52_resumption_screen as e52  # noqa: E402
    ev = e52.resumption_events(close_w, 5, 10000)
    esets["ev_resumption"] = e58._day_codes(ev, "trade_date")
    ana = e58.analyst_feats()
    cons = _load_consensus()
    cov = _load_fund_cov(days)
    cov_chg = cov.diff(21)
    # e83 v7 辅助特征（PIT 锚定公告日，勿未来函数）
    aux_ann = build_ann_cnt60(sig_days.values)
    aux_ann = {(r.sig_date, r.ts_code): r.ann_cnt60
               for r in aux_ann.itertuples()}
    aux_g = build_gdhs_qoq(sig_days.values)
    aux_g = {(r.sig_date, r.ts_code): r.gdhs_qoq
             for r in aux_g.itertuples()}

    valid20 = (~close_w.isna()).cumsum().ge(MIN_LISTED_DAYS)
    cons_idx = cons.set_index("date").sort_index()

    rows = []
    for T in sig_days:
        i = days.get_loc(T)
        col = pd.DataFrame(index=close_w.columns)
        for k, p in feats.items():
            col[k] = p.loc[T]
        for h in HORIZONS:
            col[f"label{h}"] = labels[f"label{h}"].loc[T]
        col["label"] = col["label20"]
        col["valid"] = valid20.loc[T]
        col = col[col["valid"]]
        w0, w1 = i - 19, i
        win_days = days[max(w0, 0):w1 + 1]
        for tag, d2c in esets.items():
            hot = set()
            for d in win_days:
                hot |= d2c.get(d, set())
            col[tag] = col.index.isin(hot).astype(int)
        if not ana.empty:
            a20 = ana[(ana.ann_date > T - pd.Timedelta(days=30))
                      & (ana.ann_date <= T)]
            col["an_rating_dir20"] = pd.to_numeric(
                col.index.map(
                    a20[a20.kind == "rating"].groupby("ts_code")
                    .rating_dir.sum()), errors="coerce").fillna(0).astype(float)
            col["an_epsrev20"] = pd.to_numeric(
                col.index.map(
                    a20[a20.kind == "forecast"].groupby("ts_code")
                    .fy_np_chg.count()), errors="coerce").fillna(0).astype(float)
        # consensus asof merge（≤T 最新一行）
        sub = cons_idx[cons_idx.index <= T]
        if not sub.empty:
            last = sub.groupby("ts_code").tail(1).set_index("ts_code")
            for c in ("s1_eps_rev90", "s2_np_rev90", "s3_fy_slope",
                      "s4_pe_chg", "fwd_ep"):
                col[c] = pd.to_numeric(col.index.map(last[c]),
                                       errors="coerce")
        col["fund_cov"] = pd.to_numeric(col.index.map(cov.loc[T]),
                                        errors="coerce")
        col["fund_cov_chg"] = pd.to_numeric(
            col.index.map(cov_chg.loc[T]), errors="coerce")
        col["ann_cnt60"] = pd.to_numeric(
            col.index.map(lambda c: aux_ann.get((T, c))),
            errors="coerce")
        col["gdhs_qoq"] = pd.to_numeric(
            col.index.map(lambda c: aux_g.get((T, c))),
            errors="coerce")
        col["sig_date"] = T
        rows.append(col.reset_index().rename(columns={"index": "ts_code"}))
        print(f"[note] {T.date()} rows={len(col)}", flush=True)

    X = pd.concat(rows, ignore_index=True)
    X.to_parquet(OUT)
    NOTES.write_text(json.dumps({
        "sig_days": [str(d.date()) for d in sig_days],
        "rows": len(X), "panel": str(PANEL),
        "fund_lag_days": FUND_LAG_DAYS,
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "feature_nulls": {c: float(X[c].isna().mean())
                          for c in X.columns if c.startswith(
                              ("ret", "pe", "pb", "dv", "circ", "fin",
                               "short", "s1", "s2", "s3", "s4",
                               "fwd_ep", "fund"))},
    }, ensure_ascii=False, indent=1))
    print(f"[note] DONE rows={len(X)} {(time.time()-t0)/60:.1f}min",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
