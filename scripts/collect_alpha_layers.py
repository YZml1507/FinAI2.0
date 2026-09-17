#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Alpha 三层信号底座采集（2026-09-17 立项：修池子/排雷/PEAD）。

经 Tushare 双代理（datahubco 主 / promax 备，凭证在 .env）为 487 红利池补采：

  1. ``forecast`` 业绩预告 → ``data/forecast_pit/{sym}.parquet``
     （排雷 L1：预亏/下修；PEAD S3 备用底座）；
  2. ``income`` + ``balancesheet`` 合并 → ``data/statements_pit/{sym}.parquet``
     （排雷 L4 应收偏离 / L5 存贷双高；存贷双高须货币资金+有息负债+利息收入）；
  3. ``stock_basic`` 行业标签 → ``data/pool_meta/stock_industry.parquet``
     （PEAD SUE 行业中性化；静态标签，行业极少变更，风险已登记）。

PIT 硬红线（与 collect_financial_pit/backfill_financial_pit 同口径）：
  * ``pub_date``（=ann_date）缺失的行直接丢弃，宁缺勿错；
  * 同一 end_date 多次披露（更正/重述）**全部保留**，各行自带 ann_date——
    下游按 pub_date 对齐时后发布者天然覆盖先发布者，这才是正确的 PIT 语义；
  * 幂等原子写 ``_atomic_write_parquet``；产出 ``*_REPORT.json`` 三件套。

用法：.venv/bin/python scripts/collect_alpha_layers.py
      LIMIT=3 冒烟；WORKERS=16 默认。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from data.collector import _atomic_write_parquet  # noqa: E402
from scripts.backfill_dividend_stocks import (  # noqa: E402
    _query_all, _ts_code,
)
from scripts.collect_financial_pit import dividend_universe  # noqa: E402

logger = logging.getLogger("collect_alpha_layers")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ROOT = _root
FORECAST_DIR = ROOT / "data" / "forecast_pit"
STMT_DIR = ROOT / "data" / "statements_pit"
META_DIR = ROOT / "data" / "pool_meta"
WORKERS = int(os.environ.get("WORKERS", "16") or 16)

_FORECAST_COLS = [
    "code", "pub_date", "end_date", "type", "p_change_min", "p_change_max",
    "net_profit_min", "net_profit_max", "last_parent_net", "first_ann_date",
    "update_flag", "summary", "source",
]
_INCOME_COLS = [
    "end_date", "ann_date", "f_ann_date", "comp_type", "update_flag",
    "revenue", "total_revenue", "n_income_attr_p", "int_income", "int_exp",
]
_BS_COLS = [
    "end_date", "ann_date", "f_ann_date", "comp_type", "update_flag",
    "money_cap", "trad_asset", "accounts_receiv", "accounts_receiv_bill",
    "st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y", "cb_borr",
    "total_assets", "total_liab",
]
_STMT_COLS = [
    "code", "pub_date", "end_date", "comp_type",
    "revenue", "total_revenue", "n_income_attr_p", "int_income", "int_exp",
    "money_cap", "trad_asset", "accounts_receiv", "accounts_receiv_bill",
    "st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y", "cb_borr",
    "total_assets", "total_liab", "update_flag", "source",
]


def _dt8(s: pd.Series) -> pd.Series:
    """YYYYMMDD 字符串列 → datetime64（非法值 NaT）。"""
    return pd.to_datetime(s, format="%Y%m%d", errors="coerce")


def _num(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        else:
            df[c] = float("nan")
    return df


def _fetch_forecast(ts: str) -> pd.DataFrame:
    """单票全历史业绩预告 → 规范帧（pub_date=ann_date，缺 pub 丢弃）。"""
    recs, src = _query_all("forecast", {"ts_code": ts})
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs)
    df["pub_date"] = _dt8(df.get("ann_date"))
    df["end_date"] = _dt8(df.get("end_date"))
    df["first_ann_date"] = _dt8(df.get("first_ann_date"))
    df = df[df["pub_date"].notna() & df["end_date"].notna()]
    if df.empty:
        return df
    df = _num(df, ["p_change_min", "p_change_max", "net_profit_min",
                   "net_profit_max", "last_parent_net"])
    df["code"] = ts
    df["source"] = f"tushare:{src}"
    for txt in ("summary", "change_reason"):
        if txt in df.columns:
            df[txt] = df[txt].astype(str).str.slice(0, 200)
    df["summary"] = df.get("summary")
    out = (df.sort_values(["pub_date", "end_date"])
             .drop_duplicates(subset=["pub_date", "end_date"], keep="last"))
    return out[[c for c in _FORECAST_COLS if c in out.columns or c == "code"]]


def _fetch_statements(ts: str) -> pd.DataFrame:
    """单票 income+balancesheet 按 end_date 外合并 → 规范帧。

    ``pub_date`` = 两表 ann_date 的较大者（保守：字段可见性取更晚的公告日）。
    """
    inc, src_i = _query_all("income", {"ts_code": ts})
    bs, src_b = _query_all("balancesheet", {"ts_code": ts})
    if not inc and not bs:
        return pd.DataFrame()
    di = pd.DataFrame(inc) if inc else pd.DataFrame(columns=_INCOME_COLS)
    db = pd.DataFrame(bs) if bs else pd.DataFrame(columns=_BS_COLS)
    for df in (di, db):
        df["ann_date"] = _dt8(df.get("ann_date"))
        df["f_ann_date"] = _dt8(df.get("f_ann_date"))
        df["end_date"] = _dt8(df.get("end_date"))
        df.dropna(subset=["end_date"], inplace=True)
    di = di[[c for c in _INCOME_COLS if c in di.columns]]
    db = db[[c for c in _BS_COLS if c in db.columns]]
    # 同 (end_date, ann_date) 重复行：取非空字段更多者（重述记录字段更全）
    for df in (di, db):
        if df.empty:
            continue
        df["_nn"] = df.notna().sum(axis=1)
        df.sort_values("_nn", inplace=True)
        df.drop_duplicates(subset=["end_date", "ann_date"], keep="last",
                           inplace=True)
        df.drop(columns="_nn", inplace=True)
    # 外合并：同 end_date 多版本（不同 ann_date）保留为多行，pub_date 取较迟者
    m = di.merge(db, on="end_date", how="outer", suffixes=("_i", "_b"))
    m["pub_date"] = m[["ann_date_i", "ann_date_b"]].max(axis=1)
    m["comp_type"] = m.get("comp_type_i").fillna(m.get("comp_type_b"))
    m["update_flag"] = m.get("update_flag_i").fillna(m.get("update_flag_b"))
    m = m[m["pub_date"].notna()]
    if m.empty:
        return m
    m["code"] = ts
    m["source"] = f"tushare:{src_i}+{src_b}"
    num_cols = [c for c in _STMT_COLS if c not in
                ("code", "pub_date", "end_date", "comp_type",
                 "update_flag", "source")]
    m = _num(m, num_cols)
    out = (m.sort_values(["pub_date", "end_date"])
             .drop_duplicates(subset=["pub_date", "end_date"], keep="last"))
    return out[_STMT_COLS]


def process_symbol(symbol: str) -> dict:
    """单票采集：forecast + statements 两张表。不 raise。"""
    res = {"symbol": symbol, "status": "ok", "forecast_rows": 0,
           "stmt_rows": 0}
    try:
        ts = _ts_code(symbol)
        fc = _fetch_forecast(ts)
        st = _fetch_statements(ts)
        if fc.empty and st.empty:
            res["status"] = "empty"
            return res
        if not fc.empty:
            _atomic_write_parquet(fc, FORECAST_DIR / f"{symbol}.parquet")
            res["forecast_rows"] = len(fc)
        if not st.empty:
            _atomic_write_parquet(st, STMT_DIR / f"{symbol}.parquet")
            res["stmt_rows"] = len(st)
        return res
    except Exception as exc:  # noqa: BLE001
        res["status"] = "fail"
        res["reason"] = f"{type(exc).__name__}: {exc}"[:200]
        return res


def _fetch_industry(symbols: list[str]) -> int:
    """全市场 stock_basic（L/D/P 三态）→ pool_meta/stock_industry.parquet。"""
    frames = []
    for st in ("L", "D", "P"):
        recs, src = _query_all("stock_basic", {"list_status": st})
        if recs:
            df = pd.DataFrame(recs)
            df["list_status"] = st
            df["source"] = f"tushare:{src}"
            frames.append(df)
    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True)
    keep = [c for c in ("ts_code", "name", "industry", "area", "market",
                        "list_date", "list_status", "source")
            if c in df.columns]
    df = df[keep].drop_duplicates(subset=["ts_code"], keep="last")
    META_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_parquet(df, META_DIR / "stock_industry.parquet")
    return len(df)


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
    symbols = dividend_universe(ROOT / "data" / "dividend_stocks")
    limit = int(os.environ.get("LIMIT", "0") or 0)
    if limit:
        symbols = symbols[:limit]
    logger.info(f"[plan] alpha 三层底座采集 {len(symbols)} 只，{WORKERS} 线程")
    t0 = time.time()
    results, counts = [], {"ok": 0, "empty": 0, "fail": 0}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(process_symbol, s): s for s in symbols}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            if i % 100 == 0 or i == len(symbols):
                logger.info(f"[{i}/{len(symbols)}] ok={counts['ok']} "
                            f"empty={counts['empty']} fail={counts['fail']} "
                            f"elapsed={(time.time()-t0)/60:.1f}m")
    n_ind = _fetch_industry(symbols)
    report = {
        "total": len(symbols), **counts,
        "forecast_rows_total": sum(r.get("forecast_rows", 0) for r in results),
        "stmt_rows_total": sum(r.get("stmt_rows", 0) for r in results),
        "industry_rows": n_ind,
        "fail_symbols": [r["symbol"] for r in results if r["status"] == "fail"],
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "git_sha": _git_sha(),
        "forecast_hash": _data_hash(FORECAST_DIR),
        "stmt_hash": _data_hash(STMT_DIR),
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "schema": ("forecast_pit: code,pub_date,end_date,type,p_change_min/max,"
                   "net_profit_min/max,last_parent_net,first_ann_date,"
                   "update_flag,summary,source | statements_pit: income+"
                   "balancesheet 按 end_date 外合并, pub_date=两表ann_date较迟者"),
        "sources": ["tushare:datahubco/promax(forecast+income+balancesheet+"
                    "stock_basic 单票全历史)"],
    }
    (FORECAST_DIR / "COLLECT_REPORT.json").parent.mkdir(
        parents=True, exist_ok=True)
    (FORECAST_DIR / "COLLECT_REPORT.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2))
    logger.info(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if counts["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
