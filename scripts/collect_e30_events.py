#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e30 事件族数据采集器：回购公告历史 + 限售解禁明细（两轴全史落盘）。

数据源 = 东财 datacenter-web ``api/data/v1/get`` 直查（akshare 对应接口背后
同一端点，akshare 只是薄封装；直查拿 ``columns=ALL`` 原始全字段）：

- ``repurchase`` → ``reportName=RPTA_WEB_GETHGLIST_NEW``
  回购**计划/公告级**登记表（2005 年至今逐计划一行），``filter`` 按
  ``DIM_DATE``（预案公告日）截段。含公告日（DIM_DATE/NOTICEDATE/UPDATEDATE）、
  计划金额（JHJE_VAG 均值 + JEXX/JESX 上下限）、实施进度（REPURPROGRESS
  代码 + REPURPROGRESS_TEXT 派生列）、已执行金额/数量（ZJJE/ZJSL）、
  REMARK/BZ 历次公告文本。⚠ 每行=一个回购计划（非逐次公告流水）；逐笔
  执行公告另有 ``RPTA_WEB_GPHG``（GGRQ 公告日）可作补充轴，本脚本不采。
  akshare ``stock_repurchase_em`` 即此表——实测 count=5518 含 2005 起
  全部历史计划，并非纯"当前快照"，但行粒度是计划级。
- ``restricted`` → ``reportName=RPT_LIFT_STAGE``
  限售解禁**明细**（每股×每批次一行）：FREE_DATE 解禁日、CURRENT_FREE_SHARES
  实际解禁数量、FREE_SHARES 解禁数量、TOTAL_RATIO 占总股本比例、
  FREE_RATIO 占流通市值比例、FREE_SHARES_TYPE 限售股类型。
  ⚠ 无真实"公告日期"列：EUTIME 为东财数据更新时间戳，非公告日（如实记录，
  不冒名顶替）。akshare ``stock_restricted_release_detail_em`` /
  ``stock_restricted_release_queue_em`` 同为此表；``summary_em`` 是
  按日聚合（家数/总量），非明细，故不采用（需求字段=明细级）。

落盘：
- ``data/e30_repurchase/e30_repurchase.parquet`` + ``_manifest.json``
- ``data/e30_restricted/e30_restricted.parquet`` + ``_manifest.json``
均经 ``data.collector._atomic_write_parquet`` 原子写。

幂等/断点：parquet + manifest ``success=true`` → 跳过（``--force`` 强刷）；
单页失败重试 ≤3 次，耗尽记 ``failed_pages`` 继续采，``success=false``
（下次运行重采整段）。限速 ``--sleep``（默认 0.5s，下限 0.4s）。

用法：.venv/bin/python scripts/collect_e30_events.py --axis all
          [--start 2015-01-01] [--end 2024-12-31] [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from data.collector import _atomic_write_parquet  # noqa: E402

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
PAGE_SIZE = 500          # 实测这两张表服务端上限 500（pageSize=5000 被截回 500）
RETRIES = 3
TIMEOUT = 30
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

PROGRESS_MAP = {"001": "董事会预案", "002": "股东大会通过", "003": "股东大会否决",
                "004": "实施中", "005": "停止实施", "006": "完成实施"}

AXES = {
    "repurchase": {
        "report": "RPTA_WEB_GETHGLIST_NEW",
        "date_col": "DIM_DATE",                 # 预案公告日（公告事件锚）
        "sort": "DIM_DATE,DIM_SCODE",
        "code_col": "DIM_SCODE",
        "out_dir": _root / "data" / "e30_repurchase",
        "out_file": "e30_repurchase.parquet",
        "date_cols": ["DIM_DATE", "DIM_DATE3", "DIM_TRADEDATE", "NOTICEDATE",
                      "UPDATEDATE", "REPURSTARTDATE", "REPURENDDATE",
                      "FINISHDATE", "LBRQ", "SHMRSLTNOTICEDATE", "REPORTDATE",
                      "UPD"],
        "referer": "https://data.eastmoney.com/gphg/hglist.html",
    },
    "restricted": {
        "report": "RPT_LIFT_STAGE",
        "date_col": "FREE_DATE",                # 解禁日期
        "sort": "FREE_DATE,SECURITY_CODE",
        "code_col": "SECURITY_CODE",
        "out_dir": _root / "data" / "e30_restricted",
        "out_file": "e30_restricted.parquet",
        "date_cols": ["FREE_DATE"],
        "datetime_cols": ["EUTIME"],
        "referer": "https://data.eastmoney.com/dxf/detail.html",
    },
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fetch_page(session: requests.Session, cfg: dict, date_filter: str,
                page: int) -> dict:
    """单页请求 → 响应 ``result`` dict；任何异常/坏响应抛给上层记重试。"""
    params = {
        "reportName": cfg["report"],
        "columns": "ALL",
        "filter": date_filter,
        "pageNumber": str(page),
        "pageSize": str(PAGE_SIZE),
        "sortColumns": cfg["sort"],
        "sortTypes": _sort_types(cfg["sort"]),
        "source": "WEB",
        "client": "WEB",
    }
    r = session.get(URL, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    result = r.json().get("result")
    if not isinstance(result, dict) or "data" not in result:
        raise ValueError(f"unexpected payload shape: {r.text[:200]}")
    return result


def _fetch_page_retry(session: requests.Session, cfg: dict, date_filter: str,
                      page: int, sleep: float) -> dict:
    """重试 ≤ RETRIES 次；耗尽后抛最后一次异常。"""
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            return _fetch_page(session, cfg, date_filter, page)
        except Exception as exc:  # noqa: BLE001 — 网络/解析失败统一重试
            last = exc
            print(f"    page {page} attempt {attempt}/{RETRIES} "
                  f"failed: {type(exc).__name__} {str(exc)[:120]}",
                  flush=True)
            time.sleep(max(sleep, 1.0) * attempt)
    raise last  # type: ignore[misc]


def _sort_types(sort: str) -> str:
    # 首列日期降序取最新在前不重要——统一升序排法更利于审计，这里跟随各轴声明。
    n = len(sort.split(","))
    return ",".join(["1"] * n)


def collect_axis(axis: str, args) -> int:
    cfg = AXES[axis]
    out_dir: Path = cfg["out_dir"]
    out_file = out_dir / cfg["out_file"]
    manifest_path = out_dir / "_manifest.json"
    date_col = cfg["date_col"]
    date_filter = (f"({date_col}>='{args.start}')"
                   f"({date_col}<='{args.end}')")

    # 幂等：成功产物已存在 → 直接跳过。
    if not args.force and out_file.exists() and manifest_path.exists():
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — manifest 坏了就重采
            m = {}
        if m.get("success") is True:
            print(f"[{axis}] skip: {out_file} 已存在且 manifest success "
                  f"(rows={m.get('total_rows')}, "
                  f"{m.get('date_min')}→{m.get('date_max')})")
            return 0

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Referer": cfg["referer"]})

    t0 = time.time()
    started = _utc_iso()
    first = _fetch_page_retry(session, cfg, date_filter, 1, args.sleep)
    total_pages = int(first.get("pages") or 0)
    expected = int(first.get("count") or 0)
    print(f"[{axis}] {cfg['report']} filter={date_filter} "
          f"pages={total_pages} count={expected}", flush=True)
    if args.dry_run:
        print(f"[{axis}] dry-run: 首页 rows={len(first['data'])} "
              f"cols={sorted(first['data'][0].keys()) if first['data'] else []}")
        return 0

    n_pages = min(total_pages, args.limit) if args.limit else total_pages
    frames: list[pd.DataFrame] = [pd.DataFrame(first["data"])]
    failed_pages: list[int] = []
    fetched = len(first["data"])

    for page in range(2, n_pages + 1):
        time.sleep(args.sleep)
        try:
            result = _fetch_page_retry(session, cfg, date_filter, page,
                                       args.sleep)
        except Exception as exc:  # noqa: BLE001 — 记失败页继续采
            failed_pages.append(page)
            print(f"  [{axis}] page {page}/{total_pages} GIVE-UP: "
                  f"{type(exc).__name__} {str(exc)[:120]}", flush=True)
            continue
        rows = len(result["data"])
        fetched += rows
        frames.append(pd.DataFrame(result["data"]))
        if page % 10 == 0 or page == n_pages:
            print(f"  [{axis}] page {page}/{total_pages} rows={rows} "
                  f"cum={fetched}", flush=True)

    big = (pd.concat(frames, ignore_index=True)
           if frames else pd.DataFrame())
    if not big.empty:
        for c in cfg["date_cols"]:
            if c in big.columns:
                big[c] = pd.to_datetime(big[c], errors="coerce").dt.date
        for c in cfg.get("datetime_cols", []):
            if c in big.columns:
                big[c] = pd.to_datetime(big[c], errors="coerce")
        if axis == "repurchase" and "REPURPROGRESS" in big.columns:
            big["REPURPROGRESS_TEXT"] = big["REPURPROGRESS"].map(PROGRESS_MAP)
        sort_cols = [date_col, cfg["code_col"]]
        big = (big.sort_values([c for c in sort_cols if c in big.columns],
                               kind="stable")
                  .reset_index(drop=True))

    out_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_parquet(big, out_file)

    manifest = {
        "axis": axis,
        "reportName": cfg["report"],
        "source_url": URL,
        "filter": date_filter,
        "date_col": date_col,
        "range": [args.start, args.end],
        "page_size": PAGE_SIZE,
        "total_pages": total_pages,
        "pages_fetched": n_pages - len(failed_pages),
        "expected_count": expected,
        "total_rows": int(len(big)),
        "unique_securities": (int(big[cfg["code_col"]].nunique())
                              if not big.empty and cfg["code_col"] in big else 0),
        "date_min": (str(big[date_col].min())
                     if not big.empty and date_col in big else None),
        "date_max": (str(big[date_col].max())
                     if not big.empty and date_col in big else None),
        "has_announcement_date": (
            {"col": "DIM_DATE", "latest_notice_col": "NOTICEDATE"}
            if axis == "repurchase"
            else {"col": None,
                  "note": "无真实公告日期列；EUTIME=东财数据更新时间戳（已保留），"
                          "PIT 锚只能用 FREE_DATE 或另接公告源"}),
        "failed_pages": failed_pages,
        "success": not failed_pages and len(big) == expected,
        "started_at": started,
        "finished_at": _utc_iso(),
        "duration_sec": round(time.time() - t0, 1),
        "output_file": str(out_file.relative_to(_root)),
        "note": ("success=true 要求零失败页且实采行数=服务端 count；"
                 "limit 截断时 success=false"),
    }
    if args.limit and n_pages < total_pages:
        manifest["success"] = False
        manifest["note"] += "；本次 --limit 截断"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    print(f"[{axis}] done: rows={len(big)}/{expected} "
          f"failed_pages={failed_pages} manifest={manifest_path}", flush=True)
    return 0 if manifest["success"] else 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--axis", required=True,
                    choices=["repurchase", "restricted", "all"])
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--sleep", type=float, default=0.5)
    ap.add_argument("--limit", type=int, default=0,
                    help="每轴只采前 N 页（冒烟用）")
    ap.add_argument("--force", action="store_true",
                    help="忽略幂等跳过，强制重采")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.sleep < 0.4:
        print("⛔ --sleep 必须 ≥0.4s")
        return 2

    axes = ["repurchase", "restricted"] if args.axis == "all" else [args.axis]
    rc = 0
    for a in axes:
        rc |= collect_axis(a, args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
