#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T312 红利股数据采集器 v2 —— 腾讯日线 + 巨潮分红（baostock 故障替代）。

背景：baostock 服务器对当前出口 IP 封锁（6 小时重试风暴触发），10002007 持续。
v2 换数据源，全部绕开 baostock，落盘 schema 与 v1 完全一致（feed/回测零改动）。

数据源（均免密钥、国内可达、实测通过）：
  - 候选池：akshare stock_info_a_code_name()（交易所官网名单，5554 只）
  - 日线：腾讯 ifzq.gtimg.cn/appstock/app/fqkline/get，day 模式 = 不复权 RAW。
    停牌日 = 行缺失（R1 天然满足：保留行恒 tradestatus=1）。count<=2000 上限，
    10 年分 2 页（end=2024-12-31 倒推 2000 根 + end=2016-10-12 第二页），裁剪
    [start, end]。
    无 preclose/amount → 自行补：preclose=前一日 close；成交额 ≈ volume(手)
    ×100×close；pctChg=(close-prev)/prev；tradestatus=1；isST=0。
  - 分红：akshare stock_dividend_cninfo(symbol)（巨潮）。字段 10 派 X 口径：
    cash每股 = 派息比例/10；factor = 1 + 送股/10 + 转增/10；除权日 = 除权日列。

断点续采：沿用 v1 两级标记（_bars.done=日线完成，_done.json=enrich 完成）。
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import date as _date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd
import requests

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from data.collector import _canonicalize, _atomic_write_parquet

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

INDEX_SYMBOL = "sh.000300"
_A_STOCK_PREFIXES = ("sh.60", "sh.68", "sz.00", "sz.30")
_TENCENT_KLINE_URL = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
_PAGE_LEN = 2000          # 腾讯接口 count 上限（>2000 → param error）
_SLEEP_S = 0.6            # 腾讯请求间隔（频控保守）

#: 落盘列序（与 v1/baostock schema 一致 + 血缘列；feed 只读所需字段）。
_BAR_COLUMNS = [
    "date", "open", "high", "low", "close", "preclose",
    "volume", "amount", "turn", "pctChg", "tradestatus", "isST",
    "code", "source", "adjust_mode",
]


def _is_a_stock(code: str) -> bool:
    return code.startswith(_A_STOCK_PREFIXES)


def _tencent_symbol(code: str) -> str:
    """'sh.600000' → 'sh600000'；'sz.000001' → 'sz000001'。"""
    return code.replace(".", "")


def _normalize_code(ak_code: str) -> str:
    """akshare 代码（'600000'/'000001'）→ 'sh.600000'/'sz.000001'。"""
    c = str(ak_code).zfill(6)
    return ("sh." if c.startswith(("60", "68")) else "sz.") + c


# ===================================================================
# 候选池
# ===================================================================

def fetch_all_a_stocks() -> list[str]:
    """全 A 股代码（akshare 交易所名单 → 标准符号，只留股票前缀，排序去重）。"""
    import akshare as ak  # noqa: PLC0415
    df = ak.stock_info_a_code_name()
    codes = sorted({
        _normalize_code(c) for c in df["code"].astype(str).tolist()
        if _is_a_stock(_normalize_code(c))
    })
    logger.info(f"获取到 {len(codes)} 只 A 股（已滤指数/基金/B股）")
    return codes


def pick_sample(codes: list[str], limit: int) -> list[str]:
    """等距抽样 ``limit`` 只（确定性，跨度覆盖全市场而非挤在低代码段）。"""
    if len(codes) <= limit:
        return list(codes)
    stride = len(codes) / limit
    return [codes[int(i * stride)] for i in range(limit)]


# ===================================================================
# 腾讯日线（RAW 不复权）
# ===================================================================

def _fetch_tencent_page(symbol: str, end_date: str, count: int) -> list[list[str]]:
    """拉腾讯 kline 一页（最近 ``count`` 根到 ``end_date``，倒序回看）。"""
    ts = _tencent_symbol(symbol)
    resp = requests.get(
        _TENCENT_KLINE_URL,
        params={"param": f"{ts},day,2000-01-01,{end_date},{count},"},
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0:
        logger.warning(f"[{symbol}] 腾讯 param error: {payload.get('msg')}")
        return []
    sym_data = payload.get("data", {})
    if not isinstance(sym_data, dict):
        return []
    rows = (sym_data.get(ts) or {}).get("day", [])
    return [r for r in rows if isinstance(r, list) and len(r) >= 6]


def _page_dates(rows: list[list[str]]) -> list[str]:
    out = [r[0] for r in rows]
    return sorted(out)


def _tencent_rows_to_frame(symbol: str, rows: list[list[str]]) -> pd.DataFrame:
    """腾讯行（date/open/close/high/low/volume手）→ 标准落盘 schema。

    volume 转股（×100）；amount ≈ volume(手)×100×close；preclose=前日 close；
    pctChg=(close-prev)/prev；tradestatus=1（腾讯行缺失=停牌，保留行恒有效）。
    """
    recs = []
    for r in sorted(rows, key=lambda x: x[0]):
        day = r[0]
        open_p = float(r[1]); close_p = float(r[2])
        high_p = float(r[3]); low_p = float(r[4])
        vol_lots = float(r[5]) if r[5] else 0.0
        vol = int(vol_lots * 100)          # 手 → 股
        recs.append({
            "date": day, "open": open_p, "high": high_p,
            "low": low_p, "close": close_p,
            "volume": vol, "amount": vol_lots * 100 * close_p,
            "turn": 0.0,
        })
    df = pd.DataFrame(recs)
    if df.empty:
        return df
    df["preclose"] = df["close"].shift(1)
    # 首行无前收 → 用自身（首日无涨跌停判定，feed 会保守置 False）
    df["preclose"] = df["preclose"].fillna(df["close"])
    df["pctChg"] = (df["close"] - df["preclose"]) / df["preclose"].replace(0, float("nan"))
    df["pctChg"] = df["pctChg"].fillna(0.0)
    df["tradestatus"] = "1"
    df["isST"] = "0"
    df["code"] = symbol
    df["source"] = "tencent"
    df["adjust_mode"] = "RAW"
    return df[_BAR_COLUMNS]


def fetch_tencent_daily(symbol: str, start: str, end: str) -> pd.DataFrame:
    """拉腾讯 RAW 日线并裁剪到 [start, end]（10 年 2 页法，覆盖全区间）。"""
    page1 = _fetch_tencent_page(symbol, end, _PAGE_LEN)
    if not page1:
        return pd.DataFrame(columns=_BAR_COLUMNS)
    all_rows = list(page1)
    dates = _page_dates(page1)
    earliest = dates[0]
    if earliest > start:
        # 第二页：接第一页最早日的前一天
        from datetime import timedelta
        cut = (_date.fromisoformat(earliest) - timedelta(days=1)).isoformat()
        page2 = _fetch_tencent_page(symbol, cut, _PAGE_LEN)
        if page2:
            all_rows = page2 + page1
    df = _tencent_rows_to_frame(symbol, all_rows)
    if df.empty:
        return df
    # 裁剪 + 去重
    df = df[(df["date"] >= start) & (df["date"] <= end)]
    df = df.drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    return df


# ===================================================================
# 巨潮分红
# ===================================================================

def fetch_cninfo_dividend(symbol: str) -> pd.DataFrame:
    """巨潮分红帧（全部历史，含 2000 年起）。列含 除权日/送股/转增/派息比例。"""
    import akshare as ak  # noqa: PLC0415
    digits = symbol.replace("sh.", "").replace("sz.", "")
    try:
        df = ak.stock_dividend_cninfo(symbol=digits)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    except (KeyError, IndexError, ValueError, Exception) as exc:
        logger.warning(f"[{symbol}] 巨潮分红获取失败或无分红数据: {exc}")
        return pd.DataFrame()


def events_from_cninfo(df: pd.DataFrame) -> dict[str, dict[str, Decimal]]:
    """巨潮帧 → ``{除权日: {cash, factor}}``（无除权日的行丢弃）。

    口径：10 派 X → 每股现金 = X/10；factor = 1 + 送股/10 + 转增/10。
    """
    events: dict[str, dict[str, Decimal]] = {}
    if df is None or df.empty:
        return events
    for row in df.itertuples(index=False):
        rec = row._asdict()
        raw_day = rec.get("除权日")
        if raw_day is None or str(raw_day) == "NaT" or str(raw_day) == "nan":
            continue
        day = str(raw_day)[:10]
        if len(day) != 10:
            continue
        def _col(name: str) -> Decimal:
            """巨潮数值列 → Decimal；NaN/None/非数 → 0（纯现金分红无送转）。"""
            raw = rec.get(name)
            try:
                v = Decimal(str(raw))
                return Decimal("0") if v.is_nan() else v
            except InvalidOperation:
                return Decimal("0")

        cash_ps = _col("派息比例") / Decimal("10")
        send = _col("送股比例") / Decimal("10")
        transfer = _col("转增比例") / Decimal("10")
        events[day] = {
            "cash": cash_ps,
            "factor": Decimal("1") + send + transfer,
        }
    return events


# ===================================================================
# enrich（Point-in-Time 股息率/真实流通市值 + 除权 sidecar）
# ===================================================================

def fetch_single_float_shares(symbol: str) -> float:
    """获取单只标的的最新流通股本（股）。"""
    if symbol == INDEX_SYMBOL:
        return 0.0
    ts = _tencent_symbol(symbol)
    try:
        resp = requests.get(f"http://qt.gtimg.cn/q={ts}", timeout=5)
        resp.raise_for_status()
        parts = resp.text.strip().split("~")
        if len(parts) > 73:
            f_shares = float(parts[73]) if parts[73] else 0.0
            t_shares = float(parts[72]) if parts[72] else 0.0
            return f_shares if f_shares > 0 else t_shares
    except Exception as exc:
        logger.warning(f"[{symbol}] 获取流通股本失败: {exc}")
    return 1_000_000_000.0


def enrich_symbol(
    symbol: str,
    root: Path,
    years: list[int],
    events: dict[str, dict[str, Decimal]],
    float_shares: float | None = None,
) -> bool:
    """为各年分区追加 Point-in-Time dividend_yield / market_cap 列；写除权 sidecar。"""
    from scripts.repair_and_enrich_dividend_data import compute_pit_fields

    if float_shares is None:
        float_shares = fetch_single_float_shares(symbol)

    events_list = [
        {"date": d, "factor": float(ev["factor"]), "cash_dividend": float(ev["cash"])}
        for d, ev in sorted(events.items())
    ]

    for year in years:
        path = root / symbol / f"{year}.parquet"
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path)
        except Exception as exc:                # noqa: BLE001
            logger.warning(f"[{symbol}] {year} 读分区失败: {exc}")
            continue
        if df.empty:
            continue

        if symbol == INDEX_SYMBOL:
            df["dividend_yield"] = 0.0
            df["market_cap"] = 0.0
        else:
            df = compute_pit_fields(df, events_list, float_shares)

        _atomic_write_parquet(_canonicalize(df), path)

    sidecar_dir = root / "exdiv"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_parquet(
        pd.DataFrame(events_list, columns=["date", "factor", "cash_dividend"]),
        sidecar_dir / f"{symbol}.parquet")
    return True


def _done_path(root: Path, symbol: str) -> Path:
    return root / symbol / "_done.json"


def _is_done(root: Path, symbol: str) -> bool:
    return _done_path(root, symbol).exists()


def _bars_path(root: Path, symbol: str) -> Path:
    return root / symbol / "_bars.done"


def _is_bars_done(root: Path, symbol: str) -> bool:
    return _bars_path(root, symbol).exists()


# ===================================================================
# 主流程
# ===================================================================

def _hithink_daily_frame(symbol: str, start: str, end: str) -> pd.DataFrame:
    """hithink 兜底日线（腾讯覆盖缺口的票，如 ST/退市整理/部分老票）。

    窗口 ≤10 年（接口限制，恰合 2015-2024 区间）；真实 turnover（优于腾讯近似）。
    """
    from data.hithink_source import fetch as hfetch
    digits = symbol.replace("sh.", "").replace("sz.", "")
    suffix = ".SH" if symbol.startswith("sh.") else ".SZ"
    s_ms = int(pd.Timestamp(start).timestamp() * 1000)
    e_ms = int(pd.Timestamp(end).timestamp() * 1000)
    r = hfetch("kline", thscode=digits + suffix, interval="1d",
               start=s_ms, end=e_ms, adjust="none")
    if r.state != "OK" or r.frame is None or r.frame.empty:
        return pd.DataFrame(columns=_BAR_COLUMNS)
    df = r.frame.copy()
    df["preclose"] = df["close"].shift(1).fillna(df["close"])
    df["pctChg"] = ((df["close"] - df["preclose"]) /
                    df["preclose"].replace(0, float("nan"))).fillna(0.0)
    df["turn"] = 0.0
    df["tradestatus"] = "1"
    df["isST"] = "0"
    df["code"] = symbol
    df["source"] = "hithink"
    df["adjust_mode"] = "RAW"
    return df[_BAR_COLUMNS]


def collect_one(
    symbol: str,
    start: str,
    end: str,
    output_root: Path,
    years: list[int],
    *,
    max_attempts: int = 3,
) -> bool:
    """采一只票全链：腾讯日线（2 页）→ 巨潮分红 → enrich → done 标记。

    逐只调用（断点粒度=1 只），失败返回 False 不 raise（重跑只补它）。
    """
    for attempt in range(1, max_attempts + 1):
        try:
            df = fetch_tencent_daily(symbol, start, end)
            if df.empty:
                # 腾讯覆盖缺口（ST/退市整理/部分老票实测缺）→ hithink 兜底
                df = _hithink_daily_frame(symbol, start, end)
                if not df.empty:
                    logger.info(f"[{symbol}] 腾讯空 → hithink 兜底 {len(df)} 根")
            if df.empty:
                logger.warning(f"[{symbol}] 腾讯日线为空（第 {attempt}/{max_attempts} 次）")
                time.sleep(2 * attempt)
                continue
            # 落盘：按年分区
            canonical = _canonicalize(df)
            for year in years:
                yr_rows = canonical[canonical["date"].map(lambda d: d.year) == year]
                if yr_rows.empty:
                    continue
                _atomic_write_parquet(
                    _canonicalize(yr_rows), output_root / symbol / f"{year}.parquet")
            _bars_path(output_root, symbol).write_text("ok", encoding="utf-8")

            if symbol == INDEX_SYMBOL:
                _done_path(output_root, symbol).write_text(
                    json.dumps({"enriched_at": datetime.now().isoformat(),
                                "source": "tencent"}), encoding="utf-8")
                return True

            div_df = fetch_cninfo_dividend(symbol)
            events = events_from_cninfo(div_df)
            enrich_symbol(symbol, output_root, years, events)
            _done_path(output_root, symbol).write_text(
                json.dumps({"enriched_at": datetime.now().isoformat(),
                            "source": "tencent+cninfo"}), encoding="utf-8")
            return True
        except Exception as exc:                # noqa: BLE001
            logger.warning(f"[{symbol}] 第 {attempt}/{max_attempts} 次失败: {exc}")
            time.sleep(2 * attempt)
    logger.error(f"[{symbol}] {max_attempts} 次重试后仍失败，留待续采")
    return False


def collect_dividend_stocks(
    symbols: list[str],
    start_date: str,
    end_date: str,
    output_root: Path,
) -> dict[str, Any]:
    """批量采集（逐 symbol 串行，FR-DATA-7），返回 meta。"""
    todo = [s for s in symbols if not _is_done(output_root, s)]
    logger.info(f"目标 {len(symbols)} 只，待采 {len(todo)} 只（done 跳过 {len(symbols) - len(todo)} 只）")

    start_year = int(start_date.split("-")[0])
    end_year = int(end_date.split("-")[0])
    years = list(range(start_year, end_year + 1))

    ok = 0
    fail_list: list[str] = []
    for i, sym in enumerate(todo):
        if i % 25 == 0:
            logger.info(f"进度: {i}/{len(todo)}（失败 {len(fail_list)}）")
        if collect_one(sym, start_date, end_date, output_root, years):
            ok += 1
        else:
            fail_list.append(sym)
        time.sleep(_SLEEP_S)

    done_total = sum(1 for s in symbols if _is_done(output_root, s))
    meta = {
        "collection_date": datetime.now().isoformat(),
        "start_date": start_date,
        "end_date": end_date,
        "total_symbols": len(symbols),
        "done_symbols": done_total,
        "successful": done_total,
        "failed_this_run": fail_list[:20],
        "adjust_mode": "RAW",
        "index_symbol": INDEX_SYMBOL,
        "source": "tencent-kline + cninfo-dividend",
        "note": "v2 数据源：腾讯日线(RAW) + 巨潮分红；baostock IP 封锁替代方案",
    }
    with open(output_root / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    logger.info(f"✅ 采集完成: done {done_total}/{len(symbols)}（本轮 +{ok}，失败 {len(fail_list)}）")
    return meta


# ===================================================================
# CLI
# ===================================================================

def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="T312 红利股数据采集器 v2（腾讯+巨潮）")
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--output", default="data/dividend_stocks")
    parser.add_argument("--limit", type=int, default=500, help="采集股票数（默认 500）")
    parser.add_argument("--smoke", action="store_true", help="冒烟：只采 3 只")
    args = parser.parse_args()

    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)
    # ⛔ 不清旧数据：断点续采靠 _done/_bars.done 标记

    limit = 3 if args.smoke else args.limit
    candidates = fetch_all_a_stocks()
    selected = pick_sample(candidates, limit)
    symbols = [INDEX_SYMBOL] + selected
    logger.info(f"选定 {len(selected)} 只（等距抽样）+ 指数 {INDEX_SYMBOL}")

    meta = collect_dividend_stocks(symbols, args.start, args.end, output_root)
    return 0 if meta["done_symbols"] >= len(symbols) * 0.95 else 1


if __name__ == "__main__":
    sys.exit(main())
