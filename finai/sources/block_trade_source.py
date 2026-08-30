"""Block-trade (大宗交易) axis collector for the FinAI data layer.

EXECUTION_PLAN_20260817 §2.3, axis 4 (block_trade). Contract:
``docs/engineering/block_trade_contract.md``.

Primary source: akshare ``stock_dzjy_mrmx(symbol="A股", start_date, end_date)``
(EastMoney datacenter lineage, probed OK 2026-08-17: 98 rows for 2026-08-14).

PIT contract: block-trade detail is published after that session's close, so
T-day rows are decision-safe only at T+1.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

SOURCE_LABEL = "akshare_eastmoney"

COLUMN_MAP = {
    "交易日期": "trade_date",
    "证券代码": "code",
    "证券简称": "name",
    "涨跌幅": "pct_change",
    "收盘价": "close",
    "成交价": "deal_price",
    "折溢率": "premium_discount_pct",
    "成交量": "volume",
    "成交额": "amount",
    "成交额/流通市值": "amount_mv_ratio",
    "买方营业部": "buyer_broker",
    "卖方营业部": "seller_broker",
}

CONTRACT_COLUMNS = [
    "trade_date",
    "ts_code",
    "name",
    "pct_change",
    "close",
    "deal_price",
    "premium_discount_pct",
    "volume",
    "amount",
    "amount_mv_ratio",
    "buyer_broker",
    "seller_broker",
    "source",
    "retrieved_at",
]

# FINDING-556: the source emits rows that are identical in EVERY column
# (e.g. 002717 *ST岭南, 4 identical rows on 2026-08-14). Such rows are not
# distinguishable events and must be deduped (keep first, count). Rows that
# differ only in broker are legitimate multiple trades and are kept.
DEDUP_KEY = [
    "trade_date", "ts_code", "deal_price", "volume", "amount",
    "buyer_broker", "seller_broker",
]


class BlockTradeSourceError(RuntimeError):
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


def fetch_eastmoney(start_date: str, end_date: str) -> pd.DataFrame:
    import akshare as ak

    try:
        df = ak.stock_dzjy_mrmx(
            symbol="A股",
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
        )
    except Exception as exc:  # noqa: BLE001
        raise BlockTradeSourceError(
            f"stock_dzjy_mrmx({start_date},{end_date}) failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if df is None or df.empty:
        raise BlockTradeSourceError(f"stock_dzjy_mrmx({start_date},{end_date}) returned 0 rows")
    return df


def normalize_frame(raw: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    missing = [c for c in COLUMN_MAP if c not in raw.columns]
    if missing:
        raise BlockTradeSourceError(f"block-trade frame missing columns {missing}")
    out = pd.DataFrame(index=raw.index)
    out["code"] = raw["证券代码"].astype(str).str.strip()
    for src_col, tgt in COLUMN_MAP.items():
        if tgt == "code":
            continue
        if tgt == "trade_date":
            out[tgt] = pd.to_datetime(raw[src_col], errors="coerce").dt.strftime("%Y-%m-%d")
        elif tgt in ("name", "buyer_broker", "seller_broker"):
            out[tgt] = raw[src_col].astype(str)
        else:
            out[tgt] = pd.to_numeric(raw[src_col], errors="coerce")
    out["trade_date"] = out["trade_date"].fillna(trade_date)
    out["ts_code"] = out["code"].map(_suffix_code)
    out["source"] = SOURCE_LABEL
    out["retrieved_at"] = _utc_now()
    out = out[CONTRACT_COLUMNS].sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    # FINDING-556: drop rows identical in every dedup column (keep first, count).
    before = len(out)
    out = out.drop_duplicates(subset=DEDUP_KEY, keep="first").reset_index(drop=True)
    if len(out) != before:
        out.attrs["deduped_rows"] = before - len(out)
    return out


def collect_block_trade(
    dates: Iterable[str],
    *,
    max_failures: int = 30,
) -> dict[str, Any]:
    """Collect per-date block-trade detail into one frame; fails loudly on gaps."""
    frames: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []
    for d in dates:
        try:
            raw = fetch_eastmoney(d, d)
            frames.append(normalize_frame(raw, d))
        except Exception as exc:  # noqa: BLE001
            failures.append({"date": d, "error": f"{type(exc).__name__}: {exc}"})
            if len(failures) > max_failures:
                raise BlockTradeSourceError(
                    f"block_trade: {len(failures)} failures (> {max_failures}) -- "
                    f"refusing to publish; first: {failures[:3]}"
                ) from exc
    if not frames:
        raise BlockTradeSourceError("block_trade: no frames collected")
    out = pd.concat(frames, ignore_index=True)
    out = out[~out["ts_code"].isna()].reset_index(drop=True)
    # FINDING-556: count + drop source-exact duplicate rows at the merge level
    # (pandas .attrs do not survive concat, so count explicitly here).
    before = len(out)
    out = out.drop_duplicates(subset=DEDUP_KEY, keep="first").reset_index(drop=True)
    deduped = before - len(out)
    return {
        "frame": out,
        "failures": failures,
        "rows": len(out),
        "deduped_rows": deduped,
        "dates_ok": len(frames) - len(failures),
        "dates_failed": len(failures),
        "source": SOURCE_LABEL,
    }
