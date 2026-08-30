"""Dragon-Tiger list (龙虎榜) axis collector for the FinAI data layer.

EXECUTION_PLAN_20260817 §2.3, axis 2 (lhb). Contract:
``docs/engineering/lhb_contract.md``.

Primary source: akshare ``stock_lhb_detail_em`` (EastMoney datacenter lineage,
probed OK 2026-08-17: 74 rows for 2026-08-14).
Backup source: akshare ``stock_lhb_detail_daily_sina`` (Sina lineage,
lineage-independent per FINDING-507; probed OK: 72 rows for 2026-08-14).
Third observation: adata ``list_a_list_daily`` (EastMoney mirror -- NOT an
independent backup, kept for cross-check only).

PIT contract: the board list only exists after that session's close, so T-day
rows are decision-safe only at T+1.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

SOURCE_LABEL = "akshare_eastmoney"
BACKUP_LABEL = "akshare_sina"
OBSERVE_LABEL = "adata"

# EastMoney stock_lhb_detail_em column -> contract column (Chinese -> English)
EM_COLUMN_MAP = {
    "上榜日": "trade_date",
    "代码": "code",
    "名称": "name",
    "收盘价": "close",
    "涨跌幅": "pct_change",
    "龙虎榜成交额": "amount",
    "龙虎榜净买额": "net_buy",
    "龙虎榜买入额": "buy_amount",
    "龙虎榜卖出额": "sell_amount",
    "上榜原因": "reason",
}

CONTRACT_COLUMNS = [
    "trade_date",
    "ts_code",
    "name",
    "close",
    "pct_change",
    "amount",
    "net_buy",
    "buy_amount",
    "sell_amount",
    "reason",
    "source",
    "retrieved_at",
]


class LhbSourceError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _suffix_code(code: str) -> str:
    """Bare 6-digit code -> NN.NN.SH/SZ/BJ (same convention as daily_basic)."""
    code = str(code).strip().zfill(6)
    if code.startswith(("4", "8", "9")):  # 北交所 4/8/9 开头 (920 系) 与 43/83/87
        return f"{code}.BJ"
    if code.startswith("6"):
        return f"{code}.SH"
    return f"{code}.SZ"


def fetch_eastmoney(start_date: str, end_date: str) -> pd.DataFrame:
    import akshare as ak

    try:
        df = ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date)
    except Exception as exc:  # noqa: BLE001
        raise LhbSourceError(
            f"stock_lhb_detail_em({start_date},{end_date}) failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if df is None or df.empty:
        raise LhbSourceError(f"stock_lhb_detail_em({start_date},{end_date}) returned 0 rows")
    return df


def fetch_sina(date: str) -> pd.DataFrame:
    import akshare as ak

    try:
        df = ak.stock_lhb_detail_daily_sina(date=date.replace("-", ""))
    except Exception as exc:  # noqa: BLE001
        raise LhbSourceError(
            f"stock_lhb_detail_daily_sina({date}) failed: {type(exc).__name__}: {exc}"
        ) from exc
    if df is None or df.empty:
        raise LhbSourceError(f"stock_lhb_detail_daily_sina({date}) returned 0 rows")
    return df


def normalize_eastmoney_frame(raw: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    """Map an EastMoney lhb frame to the contract shape (per-date)."""
    missing = [c for c in EM_COLUMN_MAP if c not in raw.columns]
    if missing:
        raise LhbSourceError(f"eastmoney lhb frame missing columns {missing}")
    out = pd.DataFrame(index=raw.index)
    out["code"] = raw["代码"].astype(str).str.strip()
    for src_col, tgt in EM_COLUMN_MAP.items():
        if tgt == "code":
            continue
        if tgt == "trade_date":
            out[tgt] = pd.to_datetime(raw[src_col], errors="coerce").dt.strftime("%Y-%m-%d")
        elif tgt == "name" or tgt == "reason":
            out[tgt] = raw[src_col].astype(str)
        else:
            out[tgt] = pd.to_numeric(raw[src_col], errors="coerce")
    out["trade_date"] = out["trade_date"].fillna(trade_date)
    out["ts_code"] = out["code"].map(_suffix_code)
    out["source"] = SOURCE_LABEL
    out["retrieved_at"] = _utc_now()
    return out[CONTRACT_COLUMNS].sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


class LhbCollector:
    """Per-date dragon-tiger list collector with EastMoney -> Sina failover."""

    def __init__(self, backup_enabled: bool = True) -> None:
        self.backup_enabled = backup_enabled

    def fetch_date(self, trade_date: str) -> pd.DataFrame:
        raw = fetch_eastmoney(trade_date.replace("-", ""), trade_date.replace("-", ""))
        try:
            return normalize_eastmoney_frame(raw, trade_date)
        except LhbSourceError:
            if not self.backup_enabled:
                raise
        # EastMoney normalize failed (schema drift) -> try Sina backup
        raw_sina = fetch_sina(trade_date)
        sina = raw_sina.rename(
            columns={
                "股票代码": "code",
                "股票名称": "name",
                "收盘价": "close",
                "涨跌幅": "pct_change",
            }
        )
        out = pd.DataFrame(index=sina.index)
        out["code"] = sina["code"].astype(str).str.strip()
        out["trade_date"] = trade_date
        out["ts_code"] = out["code"].map(_suffix_code)
        out["name"] = sina.get("name")
        for c in ("close", "pct_change"):
            out[c] = pd.to_numeric(sina.get(c), errors="coerce")
        for c in ("amount", "net_buy", "buy_amount", "sell_amount", "reason"):
            out[c] = None
        out["source"] = BACKUP_LABEL
        out["retrieved_at"] = _utc_now()
        return out[CONTRACT_COLUMNS]


def collect_lhb(
    dates: Iterable[str],
    *,
    backup_enabled: bool = True,
    max_failures: int = 30,
) -> dict[str, Any]:
    """Collect per-date lhb lists into one frame; fails loudly on gaps."""
    collector = LhbCollector(backup_enabled=backup_enabled)
    frames: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []
    for d in dates:
        try:
            frames.append(collector.fetch_date(d))
        except Exception as exc:  # noqa: BLE001
            failures.append({"date": d, "error": f"{type(exc).__name__}: {exc}"})
            if len(failures) > max_failures:
                raise LhbSourceError(
                    f"lhb: {len(failures)} failures (> {max_failures}) -- "
                    f"refusing to publish; first: {failures[:3]}"
                ) from exc
    if not frames:
        raise LhbSourceError("lhb: no frames collected")
    out = pd.concat(frames, ignore_index=True)
    out = out[~out["ts_code"].isna()].reset_index(drop=True)
    return {
        "frame": out,
        "failures": failures,
        "rows": len(out),
        "dates_ok": len(frames) - len(failures),
        "dates_failed": len(failures),
        "source": SOURCE_LABEL,
    }
