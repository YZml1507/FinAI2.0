#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P8 PEAD 简化探路实验（2026-09-16 立项执行）。

回答一个问题：**红利池 487 只里，业绩公告后是否存在可利用的漂移（PEAD）？**

事件定义（归母口径 + 扣非口径两路对照，探路不预裁）：
  * 强正面：net_profit_yoy ≥ +30%（另算 dt 扣非口径 ≥+30%）
  * 强负面：net_profit_yoy ≤ -30%（对称组——红利股爆雷同样重要，
    负 PEAD 决定『该跑多快』）
  * 对照：|yoy| < 10% 平淡组

执行口径（与回测引擎 FR-BT-6 对齐）：
  * 公告日 pub_date（盘后披露为主）⇒ 入场 = 次一交易日**开盘价**（T+1）；
  * 持有 H ∈ {5,10,20,40} 个交易日，出场 = 第 H 日收盘价；
  * 超额 = 个股区间收益 − 沪深300（sh.000300）同区间收益。

产出（experiments/lab/pead_probe/）：
  pead_events.csv        全部事件明细
  pead_horizon_stats.csv 分组×持有期 均值/中位/胜率/n
  PEAD_PROBE_REPORT.md   结论报告（含数据体检与判读）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
FIN = ROOT / "data" / "financial_pit"
BARS = ROOT / "data" / "dividend_stocks"
OUT = ROOT / "experiments" / "lab" / "pead_probe"
OUT.mkdir(parents=True, exist_ok=True)

HORIZONS = (5, 10, 20, 40)
STRONG_POS = 30.0
STRONG_NEG = -30.0
FLAT = 10.0


def _load_bars(symbol: str) -> pd.DataFrame:
    """合并该票全部年度分区 → (date, open, close) 升序。"""
    frames = []
    for p in sorted((BARS / symbol).glob("20*.parquet")):
        df = pd.read_parquet(p, columns=["date", "open", "close"])
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = (pd.concat(frames, ignore_index=True)
          .drop_duplicates(subset=["date"], keep="last")
          .sort_values("date").reset_index(drop=True))
    df["date"] = pd.to_datetime(df["date"])
    return df


def _load_fin(symbol: str) -> pd.DataFrame:
    p = FIN / f"{symbol}.parquet"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_parquet(p)
    df["pub_date"] = pd.to_datetime(df["pub_date"])
    df["stat_date"] = pd.to_datetime(df["stat_date"])
    return df.sort_values("pub_date").reset_index(drop=True)


def _pool_benchmark(all_bars: dict[str, pd.DataFrame]) -> tuple[pd.Series, pd.Series]:
    """等权池基准：entry 日 open→close 均值 + 常规日 close→prev_close 均值。

    返回 (entryday_ret[d], full_ret[d]) 两个按 date 索引的 Series。
    用池内均值当基准 = 控制『红利池整体行情』，只留事件相对池子的净效应。
    """
    entry_acc, full_acc = {}, {}
    for bars in all_bars.values():
        if bars.empty:
            continue
        b = bars.set_index("date")
        oc = b["close"] / b["open"] - 1.0
        cc = b["close"] / b["close"].shift(1) - 1.0
        for d, v in oc.dropna().items():
            entry_acc.setdefault(d, []).append(float(v))
        for d, v in cc.dropna().items():
            full_acc.setdefault(d, []).append(float(v))
    entry_s = pd.Series({d: float(np.mean(v)) for d, v in entry_acc.items()})
    full_s = pd.Series({d: float(np.mean(v)) for d, v in full_acc.items()})
    return entry_s.sort_index(), full_s.sort_index()


def _bucket(yoy, dcol: str) -> str:
    try:
        yoy = float(yoy)
    except (TypeError, ValueError):
        return "unknown"
    if not np.isfinite(yoy):
        return "unknown"
    if yoy >= STRONG_POS:
        return "strong_pos"
    if yoy <= STRONG_NEG:
        return "strong_neg"
    if abs(yoy) < FLAT:
        return "flat"
    return "mid"


def main() -> int:
    symbols = sorted(
        d.name for d in BARS.iterdir()
        if d.is_dir() and d.name != "exdiv"
        and (FIN / f"{d.name}.parquet").exists()
        and d.name != "sh.000300")

    idx = _load_bars("sh.000300")
    idx = idx.set_index("date")["close"]

    # 一次性加载全池 bars（基准构造与事件共用）
    all_bars = {s: _load_bars(s) for s in symbols}
    pool_entry, pool_full = _pool_benchmark(all_bars)
    pool_dates = pool_full.index.values

    def _pool_window_ret(entry_d, exit_d) -> float:
        """池基准在 [entry_d, exit_d] 窗口的复利收益（entry 日用 open→close）。"""
        e = pool_entry.get(entry_d)
        if e is None or not np.isfinite(e):
            return np.nan
        mask = (pool_full.index > entry_d) & (pool_full.index <= exit_d)
        sub = pool_full[mask]
        return float((1.0 + e) * float((1.0 + sub).prod()) - 1.0)

    events = []
    for sym in symbols:
        fin = _load_fin(sym)
        bars = all_bars.get(sym, pd.DataFrame())
        if fin.empty or bars.empty:
            continue
        px_dates = bars["date"].values          # datetime64 升序
        for _, ev in fin.iterrows():
            pub = ev["pub_date"]
            # 入场 = pub_date 之后第一个交易日（T+1 开盘）
            i0 = int(np.searchsorted(px_dates, np.datetime64(pub), side="right"))
            if i0 >= len(bars):
                continue                       # 公告在数据末端之后
            entry_open = float(bars["open"].iloc[i0])
            entry_date = bars["date"].iloc[i0]
            if not np.isfinite(entry_open) or entry_open <= 0:
                continue
            row = {
                "symbol": sym, "pub_date": pub.strftime("%Y-%m-%d"),
                "stat_date": ev["stat_date"].strftime("%Y-%m-%d"),
                "entry_date": entry_date.strftime("%Y-%m-%d"),
                "net_profit_yoy": ev.get("net_profit_yoy"),
                "deducted_yoy": ev.get("deducted_net_profit_yoy"),
            }
            for h in HORIZONS:
                i1 = i0 + h
                if i1 >= len(bars):
                    row[f"ret_{h}"] = np.nan
                    row[f"exc_{h}"] = np.nan
                    row[f"pool_exc_{h}"] = np.nan
                    continue
                exit_close = float(bars["close"].iloc[i1])
                exit_date = bars["date"].iloc[i1]
                ret = exit_close / entry_open - 1.0
                # 沪深300 同区间（参考口径）
                try:
                    b0 = idx.loc[:entry_date].iloc[-1]
                    b1 = idx.loc[:exit_date].iloc[-1]
                    exc = ret - (float(b1) / float(b0) - 1.0)
                except Exception:
                    exc = np.nan
                row[f"ret_{h}"] = ret
                row[f"exc_{h}"] = exc
                # 主口径：相对等权池基准的超额（控制池内整体行情）
                row[f"pool_exc_{h}"] = ret - _pool_window_ret(entry_date, exit_date)
            events.append(row)

    ev = pd.DataFrame(events)
    if ev.empty:
        print("无事件")
        return 1
    ev["bucket"] = ev["net_profit_yoy"].map(lambda v: _bucket(v, "n"))
    ev["bucket_dt"] = ev["deducted_yoy"].map(lambda v: _bucket(v, "d"))
    ev["year"] = ev["pub_date"].str[:4]
    ev.to_csv(OUT / "pead_events.csv", index=False)

    # 分组×持有期统计（主口径 = 相对等权池超额 pool_exc；指数超额 exc 作参考）
    rows = []
    for col in ("bucket", "bucket_dt"):
        for b, grp in ev.groupby(col):
            for h in HORIZONS:
                s = grp[f"pool_exc_{h}"].dropna()
                if len(s) < 30:
                    continue
                s300 = grp[f"exc_{h}"].dropna()
                rows.append({
                    "metric": col, "bucket": b, "horizon": h, "n": len(s),
                    "pool_exc_mean_bp": round(s.mean() * 1e4, 1),
                    "pool_exc_median_bp": round(s.median() * 1e4, 1),
                    "win_rate": round(float((s > 0).mean()), 3),
                    "t_stat": round(float(s.mean() / (s.std() / np.sqrt(len(s)))), 2)
                    if s.std() > 0 else None,
                    "hs300_exc_mean_bp": round(s300.mean() * 1e4, 1),
                })
    stats = pd.DataFrame(rows)
    stats.to_csv(OUT / "pead_horizon_stats.csv", index=False)

    # 年度分布
    year_counts = ev.groupby(["year", "bucket"]).size().unstack(fill_value=0)

    # 报告
    lines = [
        "# P8 PEAD 简化探路报告",
        "",
        f"> 生成：{pd.Timestamp.now():%Y-%m-%d %H:%M} ｜ 池={len(symbols)} 只 ｜ "
        f"事件总数={len(ev)} ｜ 数据截止 2026-09-16",
        "",
        "## 判读口径",
        "- 事件 = 财报公告（pub_date 对齐，零前视）；入场 T+1 开盘价；",
        "- 主超额口径 pool_exc = 个股收益 − 等权池基准同区间（控制池内 beta）；",
        "  hs300_exc 列为沪深300 口径参考；单位=基点(bp)；",
        "- |t_stat|>2 视为统计上不可忽略（粗略，未做多重检验校正）。",
        "",
        "## 分组 × 持有期 超额收益（bp）",
        "",
        stats.to_string(index=False) if not stats.empty else "（样本不足）",
        "",
        "## 年度事件数",
        "",
        year_counts.to_string(),
        "",
        "## 原始断言",
        "- 主口径=pool_exc（相对等权池）；hs300 列仅作参考（含池子整体 beta 行情）。",
        "- 若 strong_pos 组在 10-40 日 horizon 持续正超额且 t>2 ⇒ PEAD alpha 存在，",
        "  值得升正式策略实验；若仅 5 日内有效 ⇒ 是公告跳空不是漂移，",
        "  对中低频调仓无意义（我们 T+1 开盘才进场）。",
        "- strong_neg 组若显著为负 ⇒ 红利池需要『财报爆雷前置退出』规则——",
        "  这本身就可能比多头 PEAD 更值钱。",
    ]
    (OUT / "PEAD_PROBE_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(stats.to_string(index=False) if not stats.empty else "样本不足")
    print(f"\n事件 {len(ev)} 条 → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
