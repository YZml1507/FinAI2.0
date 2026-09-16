#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""红利股池 2025→今 增量回补 —— Tushare 双代理版（2026-09-16 重写）。

背景：``data/dividend_stocks/`` 各分区止于 2024-12-31（采集器 ``--end`` 默认），
而除权 sidecar 与流通股本口径均采集于 2026-09-07（含 2025/2026 已公告分红）。
本脚本只做**追加**（⛔ 不改写 2015-2024 历史分区，除非第 5 条触发）：

数据源（实测报告 ``/home/ubuntu/新接口能力报告.md`` 2026-09-19 版）：
  * datahubco ``http://datahubco.com/app-api/openapi/v1/tushare/{api}``
  * promax    ``https://pcd.mobcvb.cn/tushare/pro/{api}``（tls verify=False）
  凭证在 ``.env``：``DATAHUBCO_API_KEY`` / ``PROMAX_TUSHARE_KEY``（⛔ 不入 git）。
  实测硬限制：limit≤5000（超了返回空而非截断）、**跨年区间返回空 ⇒ 按年分段**、
  16 并发 + 重试（32 并发限流）、promax 偶发 503 ``upstream_pool_exhausted``。

流程：
  1. 逐票按年分段拉 ``daily``（2025 全年 / 2026-01-01→今日）；指数
     ``sh.000300`` 走 ``index_daily``（同一 ts_code 映射）；
  2. 股票同日拉 ``daily_basic``：``circ_mv`` 官方流通市值（万元×1e4→元）落
     ``market_cap``，``turnover_rate`` 落 ``turn``（旧分区 turn=0 是缺数据，
     本次补真值；market_cap 同时替代"最新股本+送转逆推"的旧口径——官方
     逐日 float_share 天然处理送转/解禁/增发，不再依赖单点 cninfo 股本）；
  3. ``preclose`` = Tushare ``pre_close``（**除权昨收**，交易所涨跌停基准口径）。
     ⚠ 与旧分区 RAW 语义在**除权日**不同：旧分区除权日 preclose=前日真实收盘
     ⇒ pctChg 含股息名义跌幅；新分区为除权后基准 ⇒ pctChg 是真涨跌幅——
     对 ``mark_limit_flags`` 触板判定与 ``gap_slippage`` 缺口检测**更正确**
     （送转除权日不再假摔 -50% 误触跌停）。非除权日两口径逐值一致；
  4. ``dividend_yield`` 沿用项目 PIT 口径（``compute_pit_fields`` 395 天滚动，
     events=既有 exdiv sidecar，锚定股本=官方最新 float_share）⇒ 与
     2015-2024 分区同一方法论，策略读到的口径前后一致；
  5. 旧分区（≤2024）仅当该票在 2025+ 发生送转（factor>1 新事件）时，以官方
     最新 float_share 重算 market_cap（否则字节不动，保基线可比性）；
  6. 首行接续校验：新段首日 ``pre_close`` 应 ≈ 已存末根收盘（除权日除外，
     差额≈cash_dividend）；不符记入报告 ``boundary_mismatch``；
  7. 产出 ``BACKFILL_REPORT.json``（git_sha + 数据哈希 + 时间戳三件套）。

用法：.venv/bin/python scripts/backfill_dividend_stocks.py
      LIMIT=3 环境变量 → 冒烟（只跑 3 只）；WORKERS=16 默认。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as _date
from pathlib import Path

import pandas as pd
import requests

urllib3.disable_warnings()  # promax 端点 tls verify=False（实测报告确认）

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from data.collector import _atomic_write_parquet, _canonicalize  # noqa: E402
from finai.credentials import get_credential  # noqa: E402
from scripts.collect_dividend_stocks import (  # noqa: E402
    _BAR_COLUMNS,
    INDEX_SYMBOL,
    fetch_single_float_shares,  # cninfo 兜底（daily_basic 整票失败时用）
)
from scripts.repair_and_enrich_dividend_data import compute_pit_fields  # noqa: E402

logger = logging.getLogger("backfill_dividend")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ROOT = _root
DATA = ROOT / "data" / "dividend_stocks"
EXDIV = DATA / "exdiv"
REPORT = DATA / "BACKFILL_REPORT.json"
TODAY = _date.today()
TODAY_S = TODAY.isoformat()
WORKERS = int(os.environ.get("WORKERS", "16") or 16)

#: (名称, base_url, 凭证 env 键, tls_verify)；datahubco 主、promax 备。
_ENDPOINTS = (
    ("datahubco", "http://datahubco.com/app-api/openapi/v1/tushare",
     "DATAHUBCO_API_KEY", True),
    ("promax", "https://pcd.mobcvb.cn/tushare/pro",
     "PROMAX_TUSHARE_KEY", False),
)
_PAGE_LIMIT = 5000          # ⛔ >5000 接口直接返回空（实测坑，不是截断）
_MAX_PAGES = 10             # 防御上限（单票单年 ~244 行，理论 1 页足够）
_RETRY = 4
_tls = threading.local()    # 每线程一个 Session（连接复用 + 线程隔离）


class ProxyDown(Exception):
    """双端点重试全部耗尽。"""


def _session() -> requests.Session:
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        _tls.s = s
    return s


def _query(api: str, params: dict) -> tuple[list[dict], str]:
    """双端点轮询 + 重试；返回 (records, 端点名)。全失败 raise ProxyDown。"""
    last_err = "no endpoint attempted"
    for attempt in range(_RETRY):
        for name, base, key_env, verify in _ENDPOINTS:
            try:
                r = _session().get(
                    f"{base}/{api}",
                    headers={"X-API-Key": get_credential(key_env)},
                    params=params, timeout=20, verify=verify)
                if r.status_code in (429, 500, 502, 503, 504):
                    last_err = f"{name} HTTP {r.status_code}"
                    continue
                r.raise_for_status()
                j = r.json()
                if j.get("code") != 0:
                    last_err = f"{name} code={j.get('code')} {j.get('msg')}"
                    continue
                data = j.get("data") or {}
                fields = data.get("fields") or j.get("fields") or []
                items = data.get("items") or j.get("items") or []
                return [dict(zip(fields, it)) for it in items], name
            except Exception as exc:  # noqa: BLE001
                last_err = f"{name} {type(exc).__name__}: {exc}"
        time.sleep(0.6 * (attempt + 1))
    raise ProxyDown(f"{api} 双端点 {_RETRY} 轮重试耗尽: {last_err[:200]}")


def _query_all(api: str, params: dict) -> tuple[list[dict], str]:
    """带 offset 翻页（单页恰满 5000 才翻；本场景单票单年 ~244 行实际不翻）。"""
    out, source, offset = [], "?", 0
    for _ in range(_MAX_PAGES):
        batch, source = _query(api, {**params, "limit": _PAGE_LIMIT,
                                     "offset": offset})
        out.extend(batch)
        if len(batch) < _PAGE_LIMIT:
            break
        offset += _PAGE_LIMIT
    return out, source


def _ts_code(symbol: str) -> str:
    """'sh.600000' → '600000.SH'；'sz.000001' → '000001.SZ'；'sh.000300' → '000300.SH'。"""
    exch, num = symbol.split(".", 1)
    return f"{num}.{exch.upper()}"


def _list_symbols() -> list[str]:
    return sorted(
        p.name for p in DATA.iterdir()
        if p.is_dir() and p.name != "exdiv"
        and list(p.glob("20*.parquet"))
    )


def _existing_years(sym_dir: Path) -> list[Path]:
    return sorted(sym_dir.glob("20*.parquet"))


def _last_bar(sym_dir: Path) -> tuple[str, float] | None:
    """(最新日期, 最新收盘)；无数据返回 None。"""
    last_date, last_close = None, None
    for p in _existing_years(sym_dir):
        try:
            df = pd.read_parquet(p, columns=["date", "close"])
        except Exception:
            continue
        if df.empty:
            continue
        mx = df["date"].astype(str).max()
        if last_date is None or mx > last_date:
            last_date = mx
            last_close = float(df[df["date"].astype(str) == mx]["close"].iloc[-1])
    return (last_date, last_close) if last_date else None


def _year_segments(seg_start: _date, seg_end: _date) -> list[tuple[str, str]]:
    """[seg_start, seg_end] 按日历年分段（跨年区间接口返回空——实测硬限制）。"""
    out = []
    for y in range(seg_start.year, seg_end.year + 1):
        s = max(seg_start, _date(y, 1, 1))
        e = min(seg_end, _date(y, 12, 31))
        if s <= e:
            out.append((s.strftime("%Y%m%d"), e.strftime("%Y%m%d")))
    return out


def _load_events(symbol: str) -> list[dict]:
    p = EXDIV / f"{symbol}.parquet"
    if not p.exists():
        return []
    try:
        df = pd.read_parquet(p)
    except Exception:
        return []
    return [
        {"date": str(r.date)[:10], "factor": float(r.factor),
         "cash_dividend": float(r.cash_dividend)}
        for r in df.itertuples(index=False)
    ]


def _needs_legacy_reenrich(events: list[dict], cutoff_year: int = 2025) -> bool:
    """2025+ 新送转（factor>1）⇒ 旧分区市值逆推已失真，须重算。"""
    for e in events:
        if float(e.get("factor", 1.0)) > 1.0001 and str(e["date"])[:4] >= str(cutoff_year):
            return True
    return False


def _fetch_daily(ts: str, start: str, end: str, is_index: bool) -> tuple[pd.DataFrame, str]:
    """按年分段内的一年：daily（股票）/ index_daily（指数）。"""
    api = "index_daily" if is_index else "daily"
    recs, src = _query_all(api, {"ts_code": ts, "start_date": start, "end_date": end})
    if not recs:
        return pd.DataFrame(), src
    df = pd.DataFrame(recs)
    df = df.drop_duplicates(subset=["trade_date"], keep="last")
    return df, src


def _fetch_basic(ts: str, start: str, end: str) -> pd.DataFrame:
    """daily_basic（仅股票）：trade_date + float_share + circ_mv + turnover_rate。"""
    recs, _ = _query_all("daily_basic",
                         {"ts_code": ts, "start_date": start, "end_date": end})
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs)
    keep = [c for c in ("trade_date", "float_share", "circ_mv", "turnover_rate",
                        "dv_ttm") if c in df.columns]
    return df[keep].drop_duplicates(subset=["trade_date"], keep="last")


def _to_bars(df_d: pd.DataFrame, df_b: pd.DataFrame, symbol: str,
             source: str) -> pd.DataFrame:
    """Tushare 记录 → 项目落盘 schema（_BAR_COLUMNS + market_cap/dividend_yield）。

    单位换算：vol（手）×100→股；amount（千元）×1e3→元；pct_chg（%）/100→比率；
    circ_mv（万元）×1e4→元；float_share（万股）×1e4→股。
    """
    d = df_d.copy()
    d["date"] = pd.to_datetime(d["trade_date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
    for c in ("open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    out = pd.DataFrame({
        "date": d["date"],
        "open": d["open"], "high": d["high"], "low": d["low"],
        "close": d["close"], "preclose": d["pre_close"],
        "volume": (d["vol"] * 100).round().astype("int64"),
        "amount": d["amount"] * 1e3,
        "turn": 0.0,
        "pctChg": d["pct_chg"] / 100.0,
        "tradestatus": "1", "isST": "0",
        "code": symbol, "source": f"tushare:{source}", "adjust_mode": "RAW",
    })
    if not df_b.empty:
        b = df_b.copy()
        b["date"] = pd.to_datetime(b["trade_date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
        for c in ("float_share", "circ_mv", "turnover_rate"):
            if c in b.columns:
                b[c] = pd.to_numeric(b[c], errors="coerce")
        out = out.merge(b[["date", "float_share", "circ_mv", "turnover_rate"]],
                        on="date", how="left")
        # 官方流通市值（元）= circ_mv（万元）×1e4；缺失退化 float_share×close
        out["market_cap"] = out["circ_mv"] * 1e4
        fb = out["float_share"] * 1e4 * out["close"]
        out["market_cap"] = out["market_cap"].fillna(fb)
        out["turn"] = out["turnover_rate"].fillna(0.0)
        out = out.drop(columns=["float_share", "circ_mv", "turnover_rate"])
    else:
        out["market_cap"] = float("nan")
    out = out.dropna(subset=["close"]).reset_index(drop=True)
    return out


def process_symbol(symbol: str) -> dict:
    """单票回补；返回结果记录（不 raise，失败记入 fail）。"""
    sym_dir = DATA / symbol
    res = {"symbol": symbol, "status": "ok", "appended_rows": 0,
           "years_written": [], "reenriched_legacy": False,
           "sources": set()}
    try:
        last = _last_bar(sym_dir)
        if last is None:
            res["status"] = "fail"; res["reason"] = "无既有分区"
            return res
        last_date, last_close = last
        if last_date >= TODAY_S:
            res["status"] = "skip_fresh"
            return res
        seg_start = _date.fromisoformat(last_date) + _day(1)
        ts = _ts_code(symbol)
        is_index = symbol == INDEX_SYMBOL

        events = _load_events(symbol)
        frames: list[pd.DataFrame] = []
        latest_float_shares: float | None = None   # 股（官方 float_share 万股×1e4）
        basic_missing_years: list[int] = []

        for ys, ye in _year_segments(seg_start, TODAY):
            df_d, src = _fetch_daily(ts, ys, ye, is_index)
            res["sources"].add(src)
            if df_d.empty:
                continue
            df_b = pd.DataFrame() if is_index else _fetch_basic(ts, ys, ye)
            if not is_index and df_b.empty:
                basic_missing_years.append(int(ys[:4]))
            bars = _to_bars(df_d, df_b, symbol, src)
            if not df_b.empty and "float_share" in df_b.columns:
                fs = pd.to_numeric(df_b["float_share"], errors="coerce").dropna()
                if len(fs):
                    latest_float_shares = float(fs.iloc[-1]) * 1e4
            frames.append(bars)

        if not frames:
            res["status"] = "empty"
            return res
        df = (pd.concat(frames, ignore_index=True)
              .sort_values("date")
              .drop_duplicates(subset=["date"], keep="last")
              .reset_index(drop=True))

        # 首行接续校验：新段首日 preclose ≈ 已存末根收盘（除权日容忍差额）
        first = df.iloc[0]
        expected = last_close
        ev_first = [e for e in events if e["date"] == str(first["date"])]
        if ev_first:
            e = ev_first[0]
            expected = (last_close - float(e.get("cash_dividend", 0.0))) \
                / max(float(e.get("factor", 1.0)), 1e-9)
        if expected and abs(float(first["preclose"]) - expected) / expected > 0.005:
            res.setdefault("notes", []).append(
                f"boundary_preclose: stored_close={last_close} "
                f"first_preclose={first['preclose']} expected≈{expected:.4f}")

        # cninfo 兜底股本（daily_basic 缺时的锚）
        if not is_index and latest_float_shares is None:
            try:
                latest_float_shares = fetch_single_float_shares(symbol)
            except Exception:  # noqa: BLE001
                latest_float_shares = None

        # 按年分区落盘
        for year, grp in df.groupby(df["date"].str[:4]):
            grp = grp.copy()
            if is_index:
                grp["dividend_yield"] = 0.0
                grp["market_cap"] = 0.0
            else:
                # compute_pit_fields 会重写 market_cap（最新股本+送转逆推）；
                # 先暂存 _to_bars 算好的官方市值，跑完恢复——官方逐日
                # float_share 优先，缺口处回落逆推值。
                official_cap = grp["market_cap"]
                grp = compute_pit_fields(
                    grp, events, latest_float_shares or float("nan"))
                grp["market_cap"] = official_cap.fillna(grp["market_cap"])
            out = sym_dir / f"{year}.parquet"
            if out.exists():
                old = pd.read_parquet(out)
                grp = (pd.concat([old, grp], ignore_index=True)
                       .drop_duplicates(subset=["date"], keep="last")
                       .sort_values("date").reset_index(drop=True))
            _atomic_write_parquet(_canonicalize(grp[_out_columns(symbol)]), out)
            res["years_written"].append(int(year))

        res["appended_rows"] = int(len(df))
        # 旧分区重算（仅当 2025+ 送转失真且股本锚可用）
        if (not is_index and latest_float_shares
                and _needs_legacy_reenrich(events)):
            for p in _existing_years(sym_dir):
                if int(p.stem) >= 2025:
                    continue
                old = pd.read_parquet(p)
                fixed = compute_pit_fields(old, events, latest_float_shares)
                _atomic_write_parquet(_canonicalize(fixed), p)
            res["reenriched_legacy"] = True
        if basic_missing_years:
            res.setdefault("notes", []).append(
                f"daily_basic 缺年份: {basic_missing_years}（market_cap 走逆推）")
        if not is_index and latest_float_shares is None and basic_missing_years:
            res["status"] = "partial_no_shares"
        res["sources"] = sorted(res["sources"])
        return res
    except Exception as exc:  # noqa: BLE001
        res["status"] = "fail"
        res["reason"] = f"{type(exc).__name__}: {exc}"[:200]
        return res


def _out_columns(symbol: str) -> list[str]:
    """落盘列序：股票 market_cap→dividend_yield；指数反之（对齐存量约定）。"""
    if symbol == INDEX_SYMBOL:
        return _BAR_COLUMNS + ["dividend_yield", "market_cap"]
    return _BAR_COLUMNS + ["market_cap", "dividend_yield"]


def _day(n: int):
    from datetime import timedelta
    return timedelta(days=n)


def _data_hash() -> str:
    h = hashlib.sha256()
    for p in sorted(DATA.rglob("*.parquet")):
        h.update(str(p.relative_to(DATA)).encode())
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
    symbols = _list_symbols()
    limit = int(os.environ.get("LIMIT", "0") or 0)
    if limit:
        symbols = symbols[:limit]
    logger.info(f"[plan] 回补 {len(symbols)} 只 → 截止 {TODAY_S}，{WORKERS} 线程")
    t0 = time.time()
    results, counts = [], {"ok": 0, "skip_fresh": 0, "empty": 0,
                           "partial_no_shares": 0, "fail": 0}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(process_symbol, s): s for s in symbols}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            if i % 50 == 0 or i == len(symbols):
                el = (time.time() - t0) / 60
                eta = el / i * (len(symbols) - i) if i else 0
                logger.info(
                    f"[{i}/{len(symbols)}] ok={counts['ok']} skip={counts['skip_fresh']} "
                    f"empty={counts['empty']} partial={counts['partial_no_shares']} "
                    f"fail={counts['fail']} elapsed={el:.1f}m eta={eta:.1f}m")
    fails = [r["symbol"] for r in results if r["status"] == "fail"]
    partials = [r["symbol"] for r in results if r["status"] == "partial_no_shares"]
    mismatches = [{"symbol": r["symbol"], "note": n}
                  for r in results for n in r.get("notes", [])
                  if n.startswith("boundary_preclose")]
    report = {
        "total": len(symbols), **counts,
        "appended_rows_total": sum(r.get("appended_rows", 0) for r in results),
        "reenriched_legacy_symbols": [
            r["symbol"] for r in results if r.get("reenriched_legacy")],
        "fail_symbols": fails, "partial_symbols": partials,
        "boundary_mismatch": mismatches,
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "backfill_to": TODAY_S, "git_sha": _git_sha(), "data_hash": _data_hash(),
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "workers": WORKERS,
        "sources": ["tushare:datahubco(主)", "tushare:promax(备)",
                    "cninfo(sidecar/股本兜底)"],
        "notes": [
            "preclose=Tushare pre_close（除权昨收口径，与旧分区 RAW 语义在除权日不同）",
            "market_cap=daily_basic.circ_mv×1e4（官方流通市值，替代逆推口径）",
            "dividend_yield=compute_pit_fields 395天滚动（与旧分区同方法论）",
            "turn=daily_basic.turnover_rate（旧分区该列恒为0缺数据）",
        ],
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    logger.info(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if counts["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
