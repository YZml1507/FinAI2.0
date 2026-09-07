#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase 3.6 多策略研发 · Task B —— 中证红利/沪深300 成分股 PIT 财务质量特征采集。

需求（agy 下发，Phase 3.6）：2015~2024 年，487 只高股息标的（沿用 T312 已采
``data/dividend_stocks/`` 股票池）的**Point-in-Time** 财务质量特征，落盘
``data/financial_pit/{symbol}.parquet``。

必需字段（agy 逐字）：
  · code              标的代码（本仓标准 ``sh.600000`` / ``sz.000001``）
  · pub_date          真实公告日（⛔ PIT 硬红线：对齐主键只认 pub_date，
                      永不 stat_date/end_date —— 未来函数）
  · stat_date         报告期（仅元数据列，不参与对齐）
  · roe               净资产收益率（fina_indicator.roe，加权）
  · net_profit_yoy    归母净利润同比增长率（fina_indicator.netprofit_yoy）
  · debt_to_assets    资产负债率（fina_indicator.debt_to_assets）
  · cash_flow_per_share 每股经营现金流（fina_indicator.ocfps）

数据源：``finai/sources/citydata_source.py`` 的 ``fina_indicator``（tushare 镜像）。
字段映射已实测（600519 66 行，ann_date 20150421→20241026，四字段全有值）。

反伪约束（agy 硬性）：
  · **pub_date 是唯一对齐键**。CITYDATA ``fina_indicator`` 的 ``ann_date`` 即真实
    公告日；``end_date`` 是报告期（等同 baostock statDate，⛔ 只作元数据）。
  · 无静态前向填充：某报告期 pub_date 缺失 = 该期未公告，保留 NaN 不造值。
  · 幂等：同区间重跑逐字节一致（``_canonicalize`` + ``_atomic_write_parquet``）。
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from data.collector import _atomic_write_parquet, hash_file
from finai.sources import citydata_source as cs

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _canonicalize_fin(frame: pd.DataFrame) -> pd.DataFrame:
    """财务帧落盘前规范化（⛔ 不借用 bars 专用 ``_canonicalize``，它死磕 date 列）。"""
    out = frame.copy()
    return (out.sort_values(["pub_date", "stat_date"])
               .drop_duplicates(subset=["pub_date", "stat_date"], keep="last")
               .reset_index(drop=True))

START = "20150101"
END = "20241231"
_SLEEP_S = 0.5

#: 落盘列序（血缘 + PIT 元数据）。
_COLUMNS = [
    "code", "pub_date", "stat_date", "roe", "net_profit_yoy",
    "debt_to_assets", "cash_flow_per_share", "source",
]

#: 源列 → 落盘列映射（唯一写死点，⛔ 不手写散布）。
_FIELD_MAP = {
    "roe": "roe",
    "netprofit_yoy": "net_profit_yoy",
    "debt_to_assets": "debt_to_assets",
    "ocfps": "cash_flow_per_share",
}


def dividend_universe(root: Path) -> list[str]:
    """T312 已采的高股息股票池（487 只，剔除指数 sh.000300）。"""
    stocks = [
        d for d in root.iterdir()
        if d.is_dir() and (d / "_done.json").exists() and d.name != "sh.000300"
    ]
    return sorted(d.name for d in stocks)


def _ts_code(symbol: str) -> str:
    digits = symbol.replace("sh.", "").replace("sz.", "")
    return digits + (".SH" if symbol.startswith("sh.") else ".SZ")


def _fetch_retry(api: str, **params: Any) -> Any:
    last = None
    for attempt in range(1, 4):
        r = cs.fetch(api, **params)
        if r.state == "OK":
            return r
        if r.state != "FAIL_UNREACHABLE":
            raise RuntimeError(f"{api} {params.get('ts_code')} 失败: {r.state} {getattr(r, 'detail', '')}")
        last = r
        time.sleep(2 * attempt)
    raise RuntimeError(f"{api} {params.get('ts_code')} 3 次瞬断: {getattr(last, 'detail', '')}")


def fetch_one_financial(symbol: str) -> pd.DataFrame:
    """单股全周期财务指标 → PIT 帧（pub_date 主键，stat_date 仅元数据）。"""
    ts_code = _ts_code(symbol)
    r = _fetch_retry("fina_indicator", ts_code=ts_code, start_date=START, end_date=END)
    if r.frame is None or r.frame.empty:
        return pd.DataFrame(columns=_COLUMNS)

    src = r.frame.copy()
    recs = []
    for _, row in src.iterrows():
        rec = {
            "code": symbol,
            "pub_date": str(row.get("ann_date")),
            "stat_date": str(row.get("end_date")),
        }
        for src_col, dst_col in _FIELD_MAP.items():
            rec[dst_col] = row.get(src_col)
        recs.append(rec)

    df = pd.DataFrame(recs, columns=_COLUMNS)
    df["source"] = "citydata"
    # pub_date/stat_date 规范化为 YYYY-MM-DD；畸形/缺失保留 NaN（⛔ 不造日期）
    df["pub_date"] = pd.to_datetime(df["pub_date"], format="%Y%m%d", errors="coerce")
    df["stat_date"] = pd.to_datetime(df["stat_date"], format="%Y%m%d", errors="coerce")
    for c in ["roe", "net_profit_yoy", "debt_to_assets", "cash_flow_per_share"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # ⛔ PIT 硬红线：pub_date 缺失的行直接丢弃（无法判定可对齐时点，宁缺勿错）
    dropped = int(df["pub_date"].isna().sum())
    df = df[df["pub_date"].notna()].reset_index(drop=True)
    df = df.sort_values("pub_date").reset_index(drop=True)
    if dropped:
        logger.warning(f"[{symbol}] 丢弃 {dropped} 行 pub_date 缺失（PIT 不可对齐）")
    return df


def collect_financials(
    symbols: list[str],
    output_root: Path,
) -> dict[str, Any]:
    """批量采集财务 PIT 特征，每股一个 parquet，产出 meta.json。"""
    output_root.mkdir(parents=True, exist_ok=True)

    meta_rows: list[dict[str, Any]] = []
    for i, sym in enumerate(symbols):
        if i % 50 == 0:
            logger.info(f"进度: {i}/{len(symbols)}")
        try:
            df = fetch_one_financial(sym)
        except Exception as exc:             # noqa: BLE001
            logger.error(f"[{sym}] 失败: {exc}")
            meta_rows.append({"symbol": sym, "error": str(exc)})
            continue

        # ⛔ 统计口径必须与落盘帧严格一致：先去重再统计，并哈希**已写文件**。
        #    旧实现先写 canon、再用未去重的 df 计数，导致 meta 虚报行数/空值率。
        canon = _canonicalize_fin(df) if not df.empty else df
        sha = None
        if not df.empty:
            path = output_root / f"{sym}.parquet"
            _atomic_write_parquet(canon, path)
            sha = hash_file(path)

        n = len(canon)
        n_null = int(canon[["roe", "net_profit_yoy", "debt_to_assets",
                            "cash_flow_per_share"]].isna().sum().sum()) if n else 0
        meta_rows.append({
            "symbol": sym,
            "rows": n,
            "pub_date_min": canon["pub_date"].min().strftime("%Y-%m-%d") if n else None,
            "pub_date_max": canon["pub_date"].max().strftime("%Y-%m-%d") if n else None,
            "null_rate": round(n_null / (n * 4), 6) if n else None,
            "sha256": sha,
        })
        time.sleep(_SLEEP_S)

    meta = {
        "task": "Task B — PIT 财务质量特征",
        "collection_date": datetime.now().isoformat(),
        "start": START, "end": END,
        "source": "citydata(fina_indicator)",
        "align_key": "pub_date（ann_date）",
        "note": "stat_date=报告期仅元数据列，不参与对齐；pub_date 缺失行丢弃（零前视）",
        "field_map": _FIELD_MAP,
        "total_symbols": len(symbols),
        "successful": sum(1 for r in meta_rows if "error" not in r and r.get("rows", 0) > 0),
        "total_rows": sum(r.get("rows") or 0 for r in meta_rows),
        "mean_null_rate": round(
            sum(r["null_rate"] for r in meta_rows if r.get("null_rate") is not None)
            / max(1, sum(1 for r in meta_rows if r.get("null_rate") is not None)), 6),
        "symbols": meta_rows,
    }
    with open(output_root / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    logger.info(f"✅ Task B 完成: {meta['successful']}/{len(symbols)} 只")
    return meta


def rebuild_meta(output_root: Path) -> dict[str, Any]:
    """仅依据磁盘上**已落盘**的 parquet 重建 meta.json（零网络、零重采）。

    用途：修正历史 meta 与文件不一致（如旧版按未去重帧计数）；也可在手工
    增删分区后重新对齐元数据。统计口径与 :func:`collect_financials` 完全一致。
    """
    paths = sorted(output_root.glob("*.parquet"))
    meta_rows: list[dict[str, Any]] = []
    for path in paths:
        df = pd.read_parquet(path)
        n = len(df)
        cols = ["roe", "net_profit_yoy", "debt_to_assets", "cash_flow_per_share"]
        n_null = int(df[cols].isna().sum().sum()) if n else 0
        meta_rows.append({
            "symbol": path.stem,
            "rows": n,
            "pub_date_min": df["pub_date"].min().strftime("%Y-%m-%d") if n else None,
            "pub_date_max": df["pub_date"].max().strftime("%Y-%m-%d") if n else None,
            "null_rate": round(n_null / (n * 4), 6) if n else None,
            "sha256": hash_file(path),
        })

    meta = {
        "task": "Task B — PIT 财务质量特征",
        "collection_date": datetime.now().isoformat(),
        "start": START, "end": END,
        "source": "citydata(fina_indicator)",
        "align_key": "pub_date（ann_date）",
        "note": "stat_date=报告期仅元数据列，不参与对齐；pub_date 缺失行丢弃（零前视）",
        "field_map": _FIELD_MAP,
        "meta_build": "rebuild-from-disk（依据已落盘 parquet 重算，未重新采集）",
        "total_symbols": len(meta_rows),
        "successful": sum(1 for r in meta_rows if r["rows"] > 0),
        "total_rows": sum(r["rows"] for r in meta_rows),
        "mean_null_rate": round(
            sum(r["null_rate"] for r in meta_rows if r["null_rate"] is not None)
            / max(1, len(meta_rows)), 6),
        "symbols": meta_rows,
    }
    with open(output_root / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    logger.info(f"✅ meta.json 已按磁盘重建: {meta['successful']}/{len(meta_rows)} 只，"
                f"{meta['total_rows']} 行")
    return meta


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Task B PIT 财务特征采集（CITYDATA）")
    parser.add_argument("--output", default="data/financial_pit")
    parser.add_argument("--limit", type=int, default=0, help="冒烟：只采前 N 只")
    parser.add_argument("--rebuild-meta", action="store_true",
                        help="不联网重采，仅按磁盘已有 parquet 重建 meta.json")
    args = parser.parse_args()

    output = Path(args.output)
    if args.rebuild_meta:
        rebuild_meta(output)
        return 0

    universe = dividend_universe(Path("data/dividend_stocks"))
    if args.limit:
        universe = universe[: args.limit]
    logger.info(f"股票池 {len(universe)} 只")
    meta = collect_financials(universe, Path(args.output))
    return 0 if meta["successful"] == len(universe) else 1


if __name__ == "__main__":
    sys.exit(main())
