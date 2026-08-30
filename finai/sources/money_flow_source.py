"""Money-flow (资金流) axis collector for the FinAI data layer.

EXECUTION_PLAN_20260817 §2.3, axis 1 (money_flow). Contract:
``docs/engineering/money_flow_contract.md``.

Primary source: ``efinance.stock.get_history_bill`` (EastMoney push2his lineage,
probed OK 2026-08-17: 121 rows for 000001, 2026-02-13..08-17).
Backup source: akshare 同花顺系 ``stock_fund_flow_individual`` -- **blocked** by
FINDING-554 (Length mismatch parse defect) until wrapped/fixed; the collector
keeps the switch point but refuses the backup leg until the wrapper exists.

PIT contract: per-day money-flow aggregates are only final after that session's
close, so T-day values are decision-safe only at T+1 (same convention as the
daily_basic sidecar). No high/low extremes are produced by this axis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

SOURCE_LABEL = "efinance_eastmoney"
BACKUP_LABEL = "akshare_ths"  # blocked by FINDING-554

# efinance column -> contract column
_COLUMN_MAP = {
    "日期": "trade_date",
    "股票代码": "code",
    "主力净流入": "main_net_inflow",
    "小单净流入": "small_net_inflow",
    "中单净流入": "mid_net_inflow",
    "大单净流入": "large_net_inflow",
    "超大单净流入": "super_large_net_inflow",
    "主力净流入占比": "main_net_ratio_pct",
    "收盘价": "close",
    "涨跌幅": "pct_change",
}

CONTRACT_COLUMNS = [
    "trade_date",
    "ts_code",
    "main_net_inflow",
    "small_net_inflow",
    "mid_net_inflow",
    "large_net_inflow",
    "super_large_net_inflow",
    "main_net_ratio_pct",
    "close",
    "pct_change",
    "source",
    "retrieved_at",
]


class MoneyFlowSourceError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_efinance_history(code: str, *, retries: int = 3, backoff: float = 1.5) -> pd.DataFrame:
    """Fetch per-stock daily money flow from efinance (primary, EastMoney lineage).

    Retries with exponential backoff on connection-level failures
    (FINDING-175: RemoteDisconnected / proxy drops are NOT "interface
    unavailable"; a long sequential backfill over 5500+ stocks routinely hits
    them). Deterministic parse errors are not retried.
    """
    import efinance as ef
    import time

    last: Exception | None = None
    for attempt in range(retries):
        try:
            df = ef.stock.get_history_bill(stock_code=code)
            if df is None or df.empty:
                raise MoneyFlowSourceError(f"efinance.get_history_bill({code}) returned 0 rows")
            return df
        except MoneyFlowSourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            msg = str(exc).lower()
            if not any(
                k in msg
                for k in ("remote disconnected", "connection aborted", "max retries",
                          "timed out", "timeout", "proxy", "unreachable", "ssl",
                          # FINDING-555: push2his throttling also surfaces as an
                          # empty / non-JSON body (JSONDecodeError "Expecting
                          # value") -- same throttle, must retry like a
                          # connection failure.
                          "expecting value", "json decode", "empty response",
                          "no data", "invalid")
            ):
                raise MoneyFlowSourceError(
                    f"efinance.get_history_bill({code}) failed: {type(exc).__name__}: {exc}"
                ) from exc
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
    assert last is not None
    raise MoneyFlowSourceError(
        f"efinance.get_history_bill({code}) failed after {retries} attempts: "
        f"{type(last).__name__}: {last}"
    ) from last


def normalize_efinance_frame(raw: pd.DataFrame, code: str) -> pd.DataFrame:
    """Map an efinance history-bill frame to the contract shape."""
    missing = [c for c in _COLUMN_MAP if c not in raw.columns]
    if missing:
        raise MoneyFlowSourceError(
            f"efinance frame missing columns {missing} for {code}"
        )
    out = pd.DataFrame(index=raw.index)
    out["code"] = raw["股票代码"].astype(str).str.strip()
    for src_col, tgt in _COLUMN_MAP.items():
        if tgt == "code":
            continue
        out[tgt] = pd.to_numeric(raw[src_col], errors="coerce")
    out["trade_date"] = pd.to_datetime(
        raw["日期"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")
    if out["trade_date"].isna().any():
        bad = int(out["trade_date"].isna().sum())
        raise MoneyFlowSourceError(f"{code}: {bad} rows with unparseable dates")
    out["ts_code"] = out["code"].astype(str).str.upper()
    # keep a canonical-suffixed code if available; the raw column is bare 6-digit
    out["source"] = SOURCE_LABEL
    out["retrieved_at"] = _utc_now()
    return out[CONTRACT_COLUMNS].sort_values("trade_date").reset_index(drop=True)


@dataclass
class MoneyFlowCollector:
    """Per-stock daily money-flow collector with a fail-closed backup switch."""

    backup_enabled: bool = False  # FINDING-554 blocks akshare 同花顺 leg

    def fetch_daily_basic(self, code: str) -> pd.DataFrame:  # kept for symmetry
        return self.fetch(code)

    def fetch(self, code: str) -> pd.DataFrame:
        raw = fetch_efinance_history(code)
        return normalize_efinance_frame(raw, code)


def collect_money_flow(
    codes: Iterable[str],
    *,
    backup_enabled: bool = False,
    max_failures: int = 200,
    pace_s: float = 0.15,
    circuit_break: int = 5,
    circuit_cooldown_s: float = 45.0,
) -> dict[str, Any]:
    """Collect per-stock daily money flow into one frame; fails loudly on gaps.

    Fail-closed: stocks that fail on the primary source are *dropped* and
    counted, never silently zero-filled (same discipline as the daily_basic
    collector -- a dropped row keeps the gap visible).

    FINDING-555 pacing: a rapid sequential backfill over ~5300 stocks triggers
    connection-level throttling on push2his.eastmoney.com (RemoteDisconnected).
    ``pace_s`` spaces requests out; ``circuit_break``/``circuit_cooldown_s``
    pause the whole run for a cooldown when N *consecutive* connection-level
    failures occur, then resume.
    """
    import time

    collector = MoneyFlowCollector(backup_enabled=backup_enabled)
    frames: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []
    consecutive = 0
    for code in codes:
        try:
            frames.append(collector.fetch(code))
            consecutive = 0
        except Exception as exc:  # noqa: BLE001
            failures.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
            consecutive += 1
            msg = str(exc).lower()
            if consecutive >= circuit_break and any(
                k in msg
                for k in ("remote disconnected", "connection aborted", "max retries",
                          "timed out", "timeout", "unreachable",
                          # FINDING-555: empty / non-JSON body is the same
                          # push2his throttle signature -- cool down on it too.
                          "expecting value", "json decode", "empty response")
            ):
                print(
                    f"circuit break: {consecutive} consecutive connection failures "
                    f"at {code}; cooling {circuit_cooldown_s}s",
                    flush=True,
                )
                time.sleep(circuit_cooldown_s)
                consecutive = 0
            if len(failures) > max_failures:
                raise MoneyFlowSourceError(
                    f"money_flow: {len(failures)} failures (> {max_failures}) -- "
                    "refusing to publish a gapped axis; first: "
                    f"{failures[:3]}"
                ) from exc
        if pace_s > 0:
            time.sleep(pace_s)
    if not frames:
        raise MoneyFlowSourceError("money_flow: no frames collected")
    out = pd.concat(frames, ignore_index=True)
    out = out[~out["ts_code"].isna()].reset_index(drop=True)
    return {
        "frame": out,
        "failures": failures,
        "rows": len(out),
        "stocks_ok": len(frames) - len(failures),
        "stocks_failed": len(failures),
        "source": SOURCE_LABEL,
    }
