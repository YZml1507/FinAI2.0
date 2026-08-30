"""Margin (两融) axis collector for the FinAI data layer.

EXECUTION_PLAN_20260817 §2.3, axis 3 (margin). Contract:
``docs/engineering/margin_contract.md``.

Sources: akshare ``stock_margin_detail_sse`` (SSE official) +
``stock_margin_detail_szse`` (SZSE official), both probed OK 2026-08-17
(SSE 1996 rows / SZSE 2101 rows for 2026-08-14).

PIT contract: margin figures are published after that session's close, so
T-day values are decision-safe only at T+1.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

SOURCE_SSE = "akshare_sse"
SOURCE_SZSE = "akshare_szse"

CONTRACT_COLUMNS = [
    "trade_date",
    "ts_code",
    "name",
    "rz_balance",
    "rz_buy_amount",
    "rq_balance",
    "rq_sell_amount",
    "market",
    "source",
    "retrieved_at",
]


class MarginSourceError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _suffix_code(code: str, market: str) -> str:
    code = str(code).strip().zfill(6)
    return f"{code}.{market}"


def fetch_sse(date: str) -> pd.DataFrame:
    import akshare as ak

    try:
        df = ak.stock_margin_detail_sse(date=date.replace("-", ""))
    except Exception as exc:  # noqa: BLE001
        raise MarginSourceError(f"stock_margin_detail_sse({date}) failed: {type(exc).__name__}: {exc}") from exc
    if df is None or df.empty:
        raise MarginSourceError(f"stock_margin_detail_sse({date}) returned 0 rows")
    return df


def fetch_szse(date: str) -> pd.DataFrame:
    import akshare as ak

    try:
        df = ak.stock_margin_detail_szse(date=date.replace("-", ""))
    except Exception as exc:  # noqa: BLE001
        raise MarginSourceError(f"stock_margin_detail_szse({date}) failed: {type(exc).__name__}: {exc}") from exc
    if df is None or df.empty:
        raise MarginSourceError(f"stock_margin_detail_szse({date}) returned 0 rows")
    return df


_SSE_REQUIRED = ["标的证券代码", "标的证券简称", "融资余额", "融资买入额", "融券余量", "融券卖出量"]
_SZSE_REQUIRED = ["证券代码", "证券简称", "融资买入额", "融资余额", "融券卖出量", "融券余量"]


def _require_columns(raw: pd.DataFrame, required: list[str], market: str) -> None:
    missing = [c for c in required if c not in raw.columns]
    if missing:
        raise MarginSourceError(
            f"{market} margin frame missing columns {missing} -- refusing to "
            "silently NaN-fill (FINDING-510: fail-closed on schema drift)"
        )


def normalize_sse(raw: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    # SSE cols: 信用交易日期/标的证券代码/标的证券简称/融资余额/融资买入额/...
    _require_columns(raw, _SSE_REQUIRED, "SSE")
    out = pd.DataFrame(index=raw.index)
    out["code"] = raw["标的证券代码"].astype(str).str.strip()
    out["name"] = raw["标的证券简称"].astype(str)
    out["rz_balance"] = pd.to_numeric(raw.get("融资余额"), errors="coerce")
    out["rz_buy_amount"] = pd.to_numeric(raw.get("融资买入额"), errors="coerce")
    out["rq_balance"] = pd.to_numeric(raw.get("融券余量"), errors="coerce")
    out["rq_sell_amount"] = pd.to_numeric(raw.get("融券卖出量"), errors="coerce")
    out["market"] = "SH"
    return _finish(out, trade_date, SOURCE_SSE)


def normalize_szse(raw: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    # SZSE cols: 证券代码/证券简称/融资买入额/融资余额/融券卖出量/融券余量/...
    _require_columns(raw, _SZSE_REQUIRED, "SZSE")
    out = pd.DataFrame(index=raw.index)
    out["code"] = raw["证券代码"].astype(str).str.strip()
    out["name"] = raw["证券简称"].astype(str)
    out["rz_buy_amount"] = pd.to_numeric(raw.get("融资买入额"), errors="coerce")
    out["rz_balance"] = pd.to_numeric(raw.get("融资余额"), errors="coerce")
    out["rq_sell_amount"] = pd.to_numeric(raw.get("融券卖出量"), errors="coerce")
    out["rq_balance"] = pd.to_numeric(raw.get("融券余量"), errors="coerce")
    out["market"] = "SZ"
    return _finish(out, trade_date, SOURCE_SZSE)


def _finish(out: pd.DataFrame, trade_date: str, source: str) -> pd.DataFrame:
    out["trade_date"] = trade_date
    out["ts_code"] = [
        _suffix_code(c, m) for c, m in zip(out["code"], out["market"])
    ]
    out["source"] = source
    out["retrieved_at"] = _utc_now()
    return out[CONTRACT_COLUMNS].sort_values("ts_code").reset_index(drop=True)


def collect_margin(
    dates: Iterable[str],
    *,
    max_failures: int = 30,
) -> dict[str, Any]:
    """Collect per-date margin detail (SSE + SZSE) into one frame."""
    frames: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []
    for d in dates:
        try:
            sse = normalize_sse(fetch_sse(d), d)
            szse = normalize_szse(fetch_szse(d), d)
            frames.append(pd.concat([sse, szse], ignore_index=True))
        except Exception as exc:  # noqa: BLE001
            failures.append({"date": d, "error": f"{type(exc).__name__}: {exc}"})
            if len(failures) > max_failures:
                raise MarginSourceError(
                    f"margin: {len(failures)} failures (> {max_failures}) -- "
                    f"refusing to publish; first: {failures[:3]}"
                ) from exc
    if not frames:
        raise MarginSourceError("margin: no frames collected")
    out = pd.concat(frames, ignore_index=True)
    out = out[~out["ts_code"].isna()].reset_index(drop=True)
    return {
        "frame": out,
        "failures": failures,
        "rows": len(out),
        "dates_ok": len(frames) - len(failures),
        "dates_failed": len(failures),
        "source": f"{SOURCE_SSE}+{SOURCE_SZSE}",
    }
