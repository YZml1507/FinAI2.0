#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""三层信号 sidecar 构建编排（修池子/排雷/PEAD）—— 全离线，不打网。

输入（全部已在盘）：
  * ``data/dividend_stocks/{sym}/{year}.parquet`` —— RAW 日线（含 PIT
    dividend_yield / market_cap / pctChg / preclose）；
  * ``data/dividend_stocks/exdiv/{sym}.parquet`` —— 除权事件（date/factor/
    cash_dividend）；
  * ``data/financial_pit/{sym}.parquet`` —— 财务 PIT（pub_date 对齐）；
  * ``data/forecast_pit/{sym}.parquet`` —— 业绩预告（collect_alpha_layers）；
  * ``data/statements_pit/{sym}.parquet`` —— income+balancesheet 合并；
  * ``data/pool_meta/stock_industry.parquet`` —— 行业标签（SUE 中性化）。

输出（幂等原子写，重跑字节一致）：
  * ``data/quality_veto/{sym}.parquet``    —— 日频 [date, veto, reasons]
  * ``data/landmine_events/{sym}.parquet`` —— [pub_date, rule, action, ...]
  * ``data/pead_signals/{sym}.parquet``    —— [pub_date, entry_date, ...]
  * ``data/signal_layers_manifest.json``   —— 行数/哈希/git 三件套

用法：.venv/bin/python scripts/build_signal_layers.py
      LIMIT=10 冒烟；WORKERS=8。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from data.collector import _atomic_write_parquet  # noqa: E402
from scripts.collect_financial_pit import dividend_universe  # noqa: E402
from strategy.signal_layers import (  # noqa: E402
    build_landmine_frame, build_pead_frame, build_quality_veto_frame,
)

logger = logging.getLogger("build_signal_layers")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

ROOT = _root
DATA = ROOT / "data" / "dividend_stocks"
FIN = ROOT / "data" / "financial_pit"
FC = ROOT / "data" / "forecast_pit"
STMT = ROOT / "data" / "statements_pit"
META = ROOT / "data" / "pool_meta"
#: 扰动实验旁路：``LAYERS_SUFFIX=roe8`` ⇒ 输出到 ``quality_veto_roe8/`` 等
#: 平行目录（⛔ 不污染权威 sidecar），runner 端同名环境变量选路装载。
SUFFIX = os.environ.get("LAYERS_SUFFIX", "")
OUT_VETO = ROOT / "data" / f"quality_veto{SUFFIX}"
OUT_LM = ROOT / "data" / f"landmine_events{SUFFIX}"
OUT_PEAD = ROOT / "data" / f"pead_signals{SUFFIX}"
MANIFEST = ROOT / "data" / f"signal_layers_manifest{SUFFIX}.json"

#: 构建参数环境覆盖（±20% 扰动悬崖检验用；默认=基线参数）
VETO_KW = {
    "roe_ttm_min": float(os.environ.get("Q_ROE_MIN", "10.0")),
    "payout_ocf_max": float(os.environ.get("Q_PAYOUT_MAX", "0.80")),
    "div_min_years": int(os.environ.get("Q_DIV_YEARS", "3")),
    "pseudo_price_share": float(os.environ.get("Q_PSEUDO_SHARE", "0.50")),
}
PEAD_KW = {
    "pct_min": float(os.environ.get("P_SUE_PCT", "0.80")),
    "pre_gap_max": float(os.environ.get("P_PRE_GAP", "0.07")),
    "hold_max_days": int(os.environ.get("P_HOLD_MAX", "40")),
}

WORKERS = int(os.environ.get("WORKERS", "8") or 8)
INDEX_SYMBOL = "sh.000300"


def _load_bars(symbol: str) -> pd.DataFrame:
    frames = []
    for p in sorted((DATA / symbol).glob("20*.parquet")):
        frames.append(pd.read_parquet(p))
    if not frames:
        return pd.DataFrame()
    df = (pd.concat(frames, ignore_index=True)
          .drop_duplicates(subset=["date"], keep="last")
          .sort_values("date").reset_index(drop=True))
    return df


def _load_opt(d: Path, symbol: str) -> pd.DataFrame:
    p = d / f"{symbol}.parquet"
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def _build_one(symbol: str) -> dict:
    """单票三层表构建 + 落盘。不 raise。"""
    res = {"symbol": symbol, "status": "ok"}
    try:
        bars = _load_bars(symbol)
        exdiv = _load_opt(DATA / "exdiv", symbol)
        pit = _load_opt(FIN, symbol)
        stmt = _load_opt(STMT, symbol)
        if bars.empty:
            res["status"] = "no_bars"
            return res
        # 金融类报表结构（comp_type≠1：银行/保险/证券）判定——取 comp_type 众数
        fin_sector = False
        if not stmt.empty and "comp_type" in stmt.columns:
            comp = (stmt["comp_type"].dropna().astype(str)
                    .str.replace(r"\.0$", "", regex=True))
            if len(comp):
                fin_sector = comp.mode().iloc[0] != "1"
        # ① 质量否决（日频）
        veto = build_quality_veto_frame(bars, exdiv, pit,
                                        fin_sector=fin_sector, **VETO_KW)
        _atomic_write_parquet(veto, OUT_VETO / f"{symbol}.parquet")
        res["veto_rows"] = len(veto)
        res["veto_rate"] = round(float(veto["veto"].mean()), 4) if len(veto) else 0
        # ② 排雷事件
        lm = build_landmine_frame(
            symbol, pit, _load_opt(FC, symbol), stmt)
        if not lm.empty:
            _atomic_write_parquet(lm, OUT_LM / f"{symbol}.parquet")
        res["landmine_events"] = len(lm)
        return res
    except Exception as exc:  # noqa: BLE001
        res["status"] = "fail"
        res["reason"] = f"{type(exc).__name__}: {exc}"[:200]
        return res


def _data_hash(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(d.rglob("*.parquet")):
        h.update(str(p.relative_to(d)).encode())
        h.update(str(p.stat().st_size).encode())
    return h.hexdigest()[:16]


def _git_sha() -> str:
    import subprocess
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def main() -> int:
    symbols = dividend_universe(DATA)
    limit = int(os.environ.get("LIMIT", "0") or 0)
    if limit:
        symbols = symbols[:limit]

    # 交易日历 = 指数分区日期（与回测 runner 同口径）
    idx = _load_bars(INDEX_SYMBOL)
    if idx.empty:
        logger.error("指数分区缺失，无交易日历")
        return 1
    calendar = pd.to_datetime(idx["date"]).dt.date.tolist()

    industry: dict[str, str] = {}
    ind_p = META / "stock_industry.parquet"
    if ind_p.exists():
        ind = pd.read_parquet(ind_p)
        industry = {r["ts_code"]: str(r["industry"]) for _, r in ind.iterrows()
                    if pd.notna(r.get("industry"))}
        # ts_code '600000.SH' → 'sh.600000'
        industry = {f"{c.split('.')[1].lower()}.{c.split('.')[0]}": v
                    for c, v in industry.items()}
    logger.info(f"[plan] 三层构建 {len(symbols)} 只，{WORKERS} 进程，"
                f"行业标签 {len(industry)} 条")

    OUT_VETO.mkdir(parents=True, exist_ok=True)
    OUT_LM.mkdir(parents=True, exist_ok=True)
    OUT_PEAD.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    results, counts = [], {"ok": 0, "no_bars": 0, "fail": 0}
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_build_one, s): s for s in symbols}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            if i % 50 == 0 or i == len(symbols):
                logger.info(f"[{i}/{len(symbols)}] ok={counts['ok']} "
                            f"fail={counts['fail']} "
                            f"elapsed={(time.time()-t0)/60:.1f}m")

    # ③ PEAD 需要全池截面分位 ⇒ 单进程统一构建（数据已全部在盘）
    logger.info("构建 PEAD 事件帧（全池截面分位）...")
    pool_pit = {s: _load_opt(FIN, s) for s in symbols}
    pool_bars = {s: _load_bars(s) for s in symbols}
    pead = build_pead_frame(pool_pit, pool_bars, calendar,
                            industry=industry, **PEAD_KW)
    n_eligible = 0
    if not pead.empty:
        for sym, grp in pead.groupby("symbol"):
            _atomic_write_parquet(
                grp.reset_index(drop=True), OUT_PEAD / f"{sym}.parquet")
        n_eligible = int(pead["eligible"].sum())

    veto_rates = [r["veto_rate"] for r in results if r.get("veto_rate")]
    manifest = {
        "total_symbols": len(symbols), **counts,
        "veto_rate_mean": round(sum(veto_rates) / len(veto_rates), 4)
        if veto_rates else 0,
        "landmine_events_total": sum(r.get("landmine_events", 0)
                                     for r in results),
        "pead_events_total": len(pead), "pead_eligible": n_eligible,
        "fail_symbols": [r["symbol"] for r in results
                         if r["status"] == "fail"],
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "git_sha": _git_sha(),
        "veto_hash": _data_hash(OUT_VETO), "landmine_hash": _data_hash(OUT_LM),
        "pead_hash": _data_hash(OUT_PEAD),
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "params": {**VETO_KW, **PEAD_KW, "pseudo_lookback_bars": 250},
        "layers_suffix": SUFFIX,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    logger.info(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if counts["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
