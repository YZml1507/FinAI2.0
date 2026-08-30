"""Announcement (公告: 业绩预告/快报/报表) axis collector for the FinAI data layer.

EXECUTION_PLAN_20260817 §2.3, axis 6 (announcement). Contract:
``docs/engineering/announcement_contract.md``.

Sources (all akshare EastMoney datacenter lineage, probed OK 2026-08-17):
  - ``stock_yjkb_em(date=quarter_end)``  -> kind=express  (60 rows, 20260630)
  - ``stock_yjbb_em(date=quarter_end)``  -> kind=report   (1302 rows, 20260630)
  - ``stock_profit_forecast_em(symbol="")`` -> kind=forecast (2825 rows, all)

PIT contract: the PIT key is the announcement date (ann_date), NOT the report
period (FINDING-127). Rows are decision-safe from ann_date + 1.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

SOURCE_LABEL = "akshare_eastmoney"

CONTRACT_COLUMNS = [
    "ann_date",
    "ts_code",
    "report_period",
    "kind",
    "name",
    "eps",
    "revenue",
    "revenue_yoy_pct",
    "source",
    "retrieved_at",
]


class AnnouncementSourceError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _suffix_code(code: str) -> str:
    code = str(code).strip().zfill(6)
    if code.startswith(("4", "8", "9")):
        return f"{code}.BJ"
    if code.startswith("6"):
        return f"{code}.SH"
    return f"{code}.SZ"


def fetch_yjkb(report_date: str) -> pd.DataFrame:
    """业绩快报 (express) for a quarter-end report date."""
    import akshare as ak

    try:
        df = ak.stock_yjkb_em(date=report_date.replace("-", ""))
    except Exception as exc:  # noqa: BLE001
        raise AnnouncementSourceError(
            f"stock_yjkb_em({report_date}) failed: {type(exc).__name__}: {exc}"
        ) from exc
    if df is None or df.empty:
        raise AnnouncementSourceError(f"stock_yjkb_em({report_date}) returned 0 rows")
    return df


def fetch_yjbb(report_date: str) -> pd.DataFrame:
    """业绩报表 (report) for a quarter-end report date."""
    import akshare as ak

    try:
        df = ak.stock_yjbb_em(date=report_date.replace("-", ""))
    except Exception as exc:  # noqa: BLE001
        raise AnnouncementSourceError(
            f"stock_yjbb_em({report_date}) failed: {type(exc).__name__}: {exc}"
        ) from exc
    if df is None or df.empty:
        raise AnnouncementSourceError(f"stock_yjbb_em({report_date}) returned 0 rows")
    return df


def fetch_yjyg(report_date: str) -> pd.DataFrame:
    """业绩预告 (forecast) for a quarter-end report date.

    NOTE: the correct forecast interface is stock_yjyg_em (公司业绩预告),
    NOT stock_profit_forecast_em (研报盈利预测/机构评级 -- different semantics,
    no announcement date). Verified 2026-08-17: yjyg returns 4861 rows for
    20260630 and carries a 公告日期 column.
    """
    import akshare as ak

    try:
        df = ak.stock_yjyg_em(date=report_date.replace("-", ""))
    except Exception as exc:  # noqa: BLE001
        raise AnnouncementSourceError(
            f"stock_yjyg_em({report_date}) failed: {type(exc).__name__}: {exc}"
        ) from exc
    if df is None or df.empty:
        raise AnnouncementSourceError(f"stock_yjyg_em({report_date}) returned 0 rows")
    return df


def _require_columns(raw: pd.DataFrame, required: list[str], kind: str) -> None:
    missing = [c for c in required if c not in raw.columns]
    if missing:
        raise AnnouncementSourceError(
            f"announcement({kind}) frame missing columns {missing} -- refusing "
            "to silently NaN-fill (FINDING-510: fail-closed on schema drift)"
        )


def normalize_express(raw: pd.DataFrame, report_date: str) -> pd.DataFrame:
    _require_columns(raw, ["股票代码", "股票简称", "公告日期"], "express")
    out = pd.DataFrame(index=raw.index)
    out["code"] = raw["股票代码"].astype(str).str.strip()
    out["name"] = raw["股票简称"].astype(str)
    out["eps"] = pd.to_numeric(raw.get("每股收益"), errors="coerce")
    out["revenue"] = pd.to_numeric(raw.get("营业收入-营业收入"), errors="coerce")
    out["ann_date"] = pd.to_datetime(raw.get("公告日期"), errors="coerce").dt.strftime("%Y-%m-%d")
    out["report_period"] = report_date
    out["kind"] = "express"
    return _finish(out)


def normalize_report(raw: pd.DataFrame, report_date: str) -> pd.DataFrame:
    _require_columns(raw, ["股票代码", "股票简称", "最新公告日期"], "report")
    out = pd.DataFrame(index=raw.index)
    out["code"] = raw["股票代码"].astype(str).str.strip()
    out["name"] = raw["股票简称"].astype(str)
    out["eps"] = pd.to_numeric(raw.get("每股收益"), errors="coerce")
    out["revenue"] = pd.to_numeric(raw.get("营业总收入-营业总收入"), errors="coerce")
    out["revenue_yoy_pct"] = pd.to_numeric(raw.get("营业总收入-同比增长"), errors="coerce")
    out["ann_date"] = pd.to_datetime(raw.get("最新公告日期"), errors="coerce").dt.strftime("%Y-%m-%d")
    out["report_period"] = report_date
    out["kind"] = "report"
    return _finish(out)


def normalize_forecast(raw: pd.DataFrame, report_date: str) -> pd.DataFrame:
    _require_columns(raw, ["股票代码", "股票简称", "公告日期"], "forecast")
    out = pd.DataFrame(index=raw.index)
    out["code"] = raw["股票代码"].astype(str).str.strip()
    out["name"] = raw["股票简称"].astype(str)
    out["eps"] = pd.to_numeric(raw.get("预测数值"), errors="coerce")
    out["revenue"] = None
    out["revenue_yoy_pct"] = pd.to_numeric(raw.get("业绩变动幅度"), errors="coerce")
    out["ann_date"] = pd.to_datetime(raw.get("公告日期"), errors="coerce").dt.strftime("%Y-%m-%d")
    out["report_period"] = report_date
    out["kind"] = "forecast"
    return _finish(out)


def _finish(out: pd.DataFrame) -> pd.DataFrame:
    out["ts_code"] = out["code"].map(_suffix_code)
    out["source"] = SOURCE_LABEL
    out["retrieved_at"] = _utc_now()
    for c in ("eps", "revenue", "revenue_yoy_pct", "ann_date"):
        if c not in out.columns:
            out[c] = None
    return out[CONTRACT_COLUMNS]


def collect_announcement(
    report_dates: Iterable[str],
    *,
    max_failures: int = 20,
) -> dict[str, Any]:
    """Collect express + report + forecast per quarter-end date.

    PIT discipline (FINDING-127): the decision key is ann_date (公告日期), never
    report_period. Rows without an ann_date are dropped and counted -- they are
    not decision-safe.
    """
    frames: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []
    dropped_no_ann = 0
    for rd in report_dates:
        try:
            for label, fn, norm in (
                ("yjkb", lambda d=rd: fetch_yjkb(d), normalize_express),
                ("yjbb", lambda d=rd: fetch_yjbb(d), normalize_report),
                ("yjyg", lambda d=rd: fetch_yjyg(d), normalize_forecast),
            ):
                df = norm(fn(), rd)
                dropped_no_ann += int(df["ann_date"].isna().sum())
                frames.append(df[~df["ann_date"].isna()])
        except Exception as exc:  # noqa: BLE001
            failures.append({"report_date": rd, "error": f"{type(exc).__name__}: {exc}"})
            if len(failures) > max_failures:
                raise AnnouncementSourceError(
                    f"announcement: {len(failures)} failures (> {max_failures}) -- "
                    f"refusing to publish; first: {failures[:3]}"
                ) from exc
    if not frames:
        raise AnnouncementSourceError("announcement: no frames collected")
    out = pd.concat(frames, ignore_index=True)
    out = out[~out["ts_code"].isna()].reset_index(drop=True)
    return {
        "frame": out,
        "failures": failures,
        "rows": len(out),
        "dropped_no_ann_date": dropped_no_ann,
        "kinds": sorted(out["kind"].unique().tolist()),
        "source": SOURCE_LABEL,
    }
