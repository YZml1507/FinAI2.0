#!/usr/bin/env python3
"""e62 公告正文 NLP 因子族首验（docs/E62_BODY_NLP_PREREG.md §2-4）。

输入：data/notice_body/*.parquet（art_code, code, title, atype, ann_date, text）
输出：experiments/lab/e62/nlp_firstlook.json —— 月频 IC/t/安慰剂/命中率/覆盖度。

信号（§2）：nlp_loglen, nlp_ann_cnt, nlp_risk_den, nlp_pos_den, nlp_lit_frac
聚合：逐股近 60 自然日窗口，月末截面；fwd20d 收益评估。
门禁（§5）：判强 IC>=0.04 & t>=3；安慰剂 ±20d p<0.05；池内命中>=70%；覆盖登记。
"""
from __future__ import annotations

import argparse
import glob
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BODY_DIR = ROOT / "data" / "notice_body"
BARS_DIR = ROOT / "data" / "daily_bars"
OUT = ROOT / "experiments" / "lab" / "e62" / "nlp_firstlook.json"

# §4 中文词表（显式登记；不用英译词典）
RISK_WORDS = re.compile(
    "风险|亏损|下滑|下降|违约|诉讼|仲裁|处罚|警示|违规|退市|减值|冻结|"
    "质押|担保|亏损|无法表示意见|保留意见|否定意见|立案调查|行政处罚|"
    "问询|关注函|整改|重大不确定|可能面临|提醒广大投资者"
)
POS_WORDS = re.compile(
    "增长|盈利|改善|提升|突破|中标|获批|通过|完成|恢复|分红|回购|增持|"
    "战略合作|订单|创新高|扭亏|同比上升|大幅增长"
)
LIT_TITLE = re.compile(
    "诉讼|仲裁|处罚|问询|关注函|监管|立案|违规|警示|整改|问询函|纪律处分|"
    "公开谴责|行政监管|证监会|警示函"
)


def _ts(code: str) -> str:
    p = code[0]
    if p in "03":
        return f"sz.{code}"
    if p == "6":
        return f"sh.{code}"
    return f"bj.{code}"


_SHARD_RE = re.compile(r"(?:rec_)?(\d{4})(?:_s\d+[a-z]?|_d)?$")


def load_body() -> pd.DataFrame:
    """逐文件抽取特征后即弃 text——合并语料全量载入会 OOM，
    只保留特征列拼接。"""
    frames = []
    for f in sorted(glob.glob(str(BODY_DIR / "*.parquet"))):
        stem = Path(f).stem
        if not _SHARD_RE.match(stem) or "_d" in stem:
            continue
        d = pd.read_parquet(
            f, columns=["art_code", "code", "title", "atype",
                        "ann_date", "text"])
        d["nchar"] = d["text"].fillna("").str.len()
        d["risk_hits"] = d["text"].fillna("").map(
            lambda t: len(RISK_WORDS.findall(t)))
        d["pos_hits"] = d["text"].fillna("").map(
            lambda t: len(POS_WORDS.findall(t)))
        d["is_lit"] = d["title"].fillna("").str.contains(LIT_TITLE).astype(int)
        frames.append(d[["art_code", "code", "ann_date", "atype",
                         "nchar", "risk_hits", "pos_hits", "is_lit"]])
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["art_code"])
    df["ann_date"] = pd.to_datetime(df["ann_date"])
    df["ts_code"] = df["code"].map(_ts)
    return df[["ts_code", "ann_date", "nchar", "risk_hits",
               "pos_hits", "is_lit", "atype"]]


def monthly_signals(body: pd.DataFrame, ends: pd.DatetimeIndex, win: int = 60) -> pd.DataFrame:
    """逐股每月末：近 win 自然日窗口聚合。"""
    recs = []
    body = body.sort_values("ann_date")
    dates = body["ann_date"].values
    for e in ends:
        lo = e - pd.Timedelta(days=win)
        m = (dates >= np.datetime64(lo)) & (dates <= np.datetime64(e))
        if not m.any():
            continue
        g = body.iloc[np.flatnonzero(m)].groupby("ts_code")
        s = pd.DataFrame({
            "nlp_ann_cnt": g.size(),
            "nlp_loglen": g["nchar"].mean().map(lambda x: np.log1p(x)),
            "nlp_risk_den": g.apply(lambda d: d["risk_hits"].sum() / max(d["nchar"].sum(), 1) * 1000, include_groups=False),
            "nlp_pos_den": g.apply(lambda d: d["pos_hits"].sum() / max(d["nchar"].sum(), 1) * 1000, include_groups=False),
            "nlp_lit_frac": g["is_lit"].mean(),
        })
        s["date"] = e
        recs.append(s.reset_index())
    return pd.concat(recs, ignore_index=True) if recs else pd.DataFrame()


def fwd20_returns() -> pd.DataFrame:
    """ts_code × date → fwd20d close-to-close return（含末尾 NaN）。"""
    px = []
    for f in sorted(glob.glob(str(BARS_DIR / "[sb][hzj].*/*.parquet"))):
        d = pd.read_parquet(f, columns=["code", "date", "close"])
        px.append(d)
    bars = pd.concat(px, ignore_index=True)
    bars["date"] = pd.to_datetime(bars["date"])
    bars["close"] = pd.to_numeric(bars["close"], errors="coerce")
    bars = bars.sort_values(["code", "date"])
    bars["fwd20"] = bars.groupby("code")["close"].shift(-20) / bars["close"] - 1
    return bars[["code", "date", "fwd20"]].rename(columns={"code": "ts_code"})


def eval_ic(sig: pd.DataFrame, rets: pd.DataFrame, col: str, shift_days: int = 0) -> dict:
    d = sig[["ts_code", "date", col]].copy()
    if shift_days:
        d["date"] = d["date"] + pd.Timedelta(days=shift_days)
    d = d.merge(rets, on=["ts_code", "date"], how="inner").dropna(subset=[col, "fwd20"])
    ic = d.groupby("date").apply(
        lambda g: g[col].corr(g["fwd20"], method="spearman") if len(g) >= 20 else np.nan,
        include_groups=False)
    ic = ic.dropna()
    if len(ic) < 6:
        return {"ic": None, "t": None, "months": len(ic)}
    t = ic.mean() / (ic.std(ddof=1) / np.sqrt(len(ic))) if ic.std(ddof=1) > 0 else 0.0
    return {"ic": round(float(ic.mean()), 4), "t": round(float(t), 2), "months": int(len(ic))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--win", type=int, default=60)
    args = ap.parse_args()

    body = load_body()
    rets = fwd20_returns()
    ends = pd.date_range(
        max(body["ann_date"].min(), pd.Timestamp("2015-03-01")),
        body["ann_date"].max(), freq="ME")
    sig = monthly_signals(body, ends, win=args.win)

    out = {"n_rows": int(len(body)), "n_stock_months": int(len(sig)),
           "date_span": [str(body["ann_date"].min().date()), str(body["ann_date"].max().date())],
           "signals": {}}
    for col in ["nlp_ann_cnt", "nlp_loglen", "nlp_risk_den", "nlp_pos_den", "nlp_lit_frac"]:
        res = eval_ic(sig, rets, col)
        pl = [eval_ic(sig, rets, col, s)["ic"] for s in (-20, 20)]
        pl = [p for p in pl if p is not None]
        res["placebo_ics"] = pl
        res["placebo_p"] = (
            round(float(np.mean([abs(p) >= abs(res["ic"]) for p in pl])), 3)
            if pl and res["ic"] is not None else None)
        out["signals"][col] = res

    # 覆盖度：月末截面中有信号的股数 / 当日有行情股数
    have_px = rets.groupby("date")["ts_code"].nunique()
    have_sig = sig.groupby("date")["ts_code"].nunique()
    cov = (have_sig / have_px.reindex(have_sig.index)).dropna()
    out["coverage"] = {"mean": round(float(cov.mean()), 3) if len(cov) else None,
                       "months": int(len(cov))}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
