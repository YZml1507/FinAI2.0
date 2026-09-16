#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""financial_pit 财务 PIT 回补/扩展 —— Tushare 双代理版（2026-09-16）。

背景：``data/financial_pit/``（487 只）止于 2024Q3（citydata 采集 END=20241231），
缺 2024 年报 → 2026 中报共 7 个报告期；且原口径**缺扣非字段**（P8 PEAD 探路
登记的已知缺口）。本脚本经 promax ``fina_indicator`` 单票全历史模式
（一次调用返回全部报告期，实测 ~166 期/票）回补：

  1. 新报告期（stat_date 未覆盖）整行追加，``source="tushare:promax"``；
  2. 存量期**不改写原值**（已验证数据），仅补新列 ``deducted_net_profit_yoy``
     （扣非净利同比 = ``dt_netprofit_yoy``）——含 ≤2024 历史行一并回填；
  3. Tushare 全历史含 <2015 报告期，一并补入（修复 PIT 边界缺口：回测起点
     2015-01-05 当时可见的最新财报是 2014Q3，原采集 START=20150101 把它滤了）；
  4. PIT 硬红线：``pub_date``（=ann_date）缺失的行直接丢弃（宁缺勿错）；
  5. Tushare 同 (stat,pub) 存在重复行（更正/调整记录），去重取字段更完整者；
  6. 产出 ``BACKFILL_FIN_REPORT.json`` 三件套。

实测对账（600000）：重叠期 ann_date 一致率 100%，netprofit_yoy/roe 逐值一致。

用法：.venv/bin/python scripts/backfill_financial_pit.py
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
from scripts.backfill_dividend_stocks import _query_all, _ts_code  # noqa: E402
from scripts.collect_financial_pit import _COLUMNS, dividend_universe  # noqa: E402

logger = logging.getLogger("backfill_fin_pit")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ROOT = _root
FIN = ROOT / "data" / "financial_pit"
REPORT = FIN / "BACKFILL_FIN_REPORT.json"
WORKERS = int(os.environ.get("WORKERS", "16") or 16)

#: Tushare fina_indicator → 落盘列映射（对齐 collect_financial_pit._FIELD_MAP +
#: 新增扣非列）。源缺列 ⇒ 该列为 NaN（⛔ 不造值）。
_FIELD_MAP = {
    "roe": "roe",
    "netprofit_yoy": "net_profit_yoy",
    "dt_netprofit_yoy": "deducted_net_profit_yoy",
    "debt_to_assets": "debt_to_assets",
    "ocfps": "cash_flow_per_share",
}
_NEW_COL = "deducted_net_profit_yoy"
_OUT_COLUMNS = [
    "code", "pub_date", "stat_date", "roe", "net_profit_yoy",
    "deducted_net_profit_yoy", "debt_to_assets", "cash_flow_per_share", "source",
]
_NUM_COLS = ["roe", "net_profit_yoy", "deducted_net_profit_yoy",
             "debt_to_assets", "cash_flow_per_share"]


def _fetch_one(ts: str) -> pd.DataFrame:
    """单票全历史 fina_indicator → 规范化 PIT 帧（已按完整度去重）。"""
    recs, _ = _query_all("fina_indicator", {"ts_code": ts})
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs)
    df["pub_date"] = pd.to_datetime(df.get("ann_date"), format="%Y%m%d",
                                  errors="coerce")
    df["stat_date"] = pd.to_datetime(df.get("end_date"), format="%Y%m%d",
                                   errors="coerce")
    df = df[df["pub_date"].notna() & df["stat_date"].notna()]
    if df.empty:
        return df
    for src in _FIELD_MAP:
        df[src] = pd.to_numeric(df.get(src), errors="coerce")
    # 同 (stat,pub) 重复行：取非空字段更多者（更正记录字段更全）
    df["_nn"] = df[list(_FIELD_MAP)].notna().sum(axis=1)
    df = (df.sort_values("_nn")
            .drop_duplicates(subset=["stat_date", "pub_date"], keep="last")
            .drop(columns="_nn"))
    return df


def process_symbol(symbol: str) -> dict:
    """单票回补：存量期只补扣非列，新期整行追加。不 raise。"""
    path = FIN / f"{symbol}.parquet"
    res = {"symbol": symbol, "status": "ok", "appended": 0,
           "deducted_backfilled": 0}
    try:
        ts = _ts_code(symbol)
        new = _fetch_one(ts)
        if new.empty:
            res["status"] = "empty"
            return res
        old = pd.read_parquet(path) if path.exists() else pd.DataFrame(
            columns=_COLUMNS)

        recs = []
        old_stats = set(pd.to_datetime(old["stat_date"]).dt.strftime("%Y-%m-%d")) \
            if not old.empty else set()
        for _, row in new.iterrows():
            stat_s = row["stat_date"].strftime("%Y-%m-%d")
            if stat_s in old_stats:
                continue                       # 存量期不改写（仅扣非列统一回填）
            rec = {"code": symbol, "pub_date": row["pub_date"],
                   "stat_date": row["stat_date"], "source": "tushare:promax"}
            for src_col, dst_col in _FIELD_MAP.items():
                rec[dst_col] = row.get(src_col)
            recs.append(rec)
        add = pd.DataFrame(recs, columns=_OUT_COLUMNS) if recs else \
            pd.DataFrame(columns=_OUT_COLUMNS)

        out = pd.concat([old, add], ignore_index=True)
        if _NEW_COL not in out.columns:
            out[_NEW_COL] = float("nan")
        # 扣非列回填：按 stat_date 把 tushare dt_netprofit_yoy 映到存量行
        ded_map = (new.dropna(subset=["dt_netprofit_yoy"])
                   .assign(stat_s=lambda d: d["stat_date"].dt.strftime("%Y-%m-%d"))
                   .drop_duplicates("stat_s", keep="last")
                   .set_index("stat_s")["dt_netprofit_yoy"])
        stat_key = pd.to_datetime(out["stat_date"]).dt.strftime("%Y-%m-%d")
        mapped = stat_key.map(ded_map)
        fill_mask = out[_NEW_COL].isna() & mapped.notna()
        res["deducted_backfilled"] = int(fill_mask.sum())
        out.loc[fill_mask, _NEW_COL] = mapped[fill_mask]

        out = (out.sort_values(["pub_date", "stat_date"])
                  .drop_duplicates(subset=["pub_date", "stat_date"], keep="last")
                  .reset_index(drop=True))
        out = out[_OUT_COLUMNS]
        for c in _NUM_COLS:
            out[c] = pd.to_numeric(out[c], errors="coerce")
        _atomic_write_parquet(out, path)
        res["appended"] = len(add)
        return res
    except Exception as exc:  # noqa: BLE001
        res["status"] = "fail"
        res["reason"] = f"{type(exc).__name__}: {exc}"[:200]
        return res


def _data_hash() -> str:
    h = hashlib.sha256()
    for p in sorted(FIN.rglob("*.parquet")):
        h.update(str(p.relative_to(FIN)).encode())
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
    # dividend_universe 吃 data/dividend_stocks 根（_done.json 判定，剔指数）
    symbols = dividend_universe(ROOT / "data" / "dividend_stocks")
    limit = int(os.environ.get("LIMIT", "0") or 0)
    if limit:
        symbols = symbols[:limit]
    logger.info(f"[plan] 财务回补 {len(symbols)} 只，{WORKERS} 线程")
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
    report = {
        "total": len(symbols), **counts,
        "appended_rows_total": sum(r.get("appended", 0) for r in results),
        "deducted_backfilled_total": sum(r.get("deducted_backfilled", 0)
                                         for r in results),
        "fail_symbols": [r["symbol"] for r in results if r["status"] == "fail"],
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "git_sha": _git_sha(), "data_hash": _data_hash(),
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "schema_change": f"+{_NEW_COL}（扣非净利同比，历史行同步回填）",
        "sources": ["tushare:promax(fina_indicator 单票全历史)"],
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    logger.info(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if counts["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
