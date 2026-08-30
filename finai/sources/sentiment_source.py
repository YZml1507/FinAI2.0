"""Sentiment (市场情绪) axis collector for the FinAI data layer.

EXECUTION_PLAN_20260817 §2.3, axis 5 (sentiment). Contract:
``docs/engineering/sentiment_contract.md``.

Primary source: akshare ``stock_market_activity_legu`` (乐咕乐股 lineage,
probed OK 2026-08-17: 12 rows, item/value columns). The interface returns the
current cross-section; historical backfill calls it per date (legu keeps a
~recent window; the actual retro-limit is measured during backfill).

PIT contract: sentiment indicators are final after that session's close, so
T-day values are decision-safe only at T+1. Market-level fields, not per-stock.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

SOURCE_LABEL = "akshare_legu"
SOURCE_ZTPOOL = "akshare_eastmoney_ztpool"

CONTRACT_COLUMNS = ["trade_date", "item", "value", "source", "retrieved_at"]


class SentimentSourceError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_legu() -> pd.DataFrame:
    import akshare as ak

    try:
        df = ak.stock_market_activity_legu()
    except Exception as exc:  # noqa: BLE001
        raise SentimentSourceError(
            f"stock_market_activity_legu failed: {type(exc).__name__}: {exc}"
        ) from exc
    if df is None or df.empty:
        raise SentimentSourceError("stock_market_activity_legu returned 0 rows")
    return df


def normalize_legu_frame(raw: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    missing = [c for c in ("item", "value") if c not in raw.columns]
    if missing:
        raise SentimentSourceError(f"legu frame missing columns {missing}")
    out = pd.DataFrame(index=raw.index)
    out["item"] = raw["item"].astype(str).str.strip()
    # legu 活跃度 is "78.09%" and there is a 统计日期 metadata row -- strip the
    # percent sign and drop the metadata row so the value column stays numeric.
    out["value_raw"] = raw["value"].astype(str).str.replace("%", "", regex=False).str.strip()
    out["value"] = pd.to_numeric(out["value_raw"], errors="coerce")
    out = out[out["item"] != "统计日期"]
    if out.empty:
        raise SentimentSourceError("legu frame has no indicator rows after metadata drop")
    if out["value"].isna().any():
        bad_items = out.loc[out["value"].isna(), "item"].tolist()
        raise SentimentSourceError(
            f"legu frame has non-numeric values for items {bad_items}"
        )
    out["trade_date"] = trade_date
    out["source"] = SOURCE_LABEL
    out["retrieved_at"] = _utc_now()
    return out[CONTRACT_COLUMNS].sort_values("item").reset_index(drop=True)


def fetch_zt_pool(date: str) -> pd.DataFrame:
    """EastMoney limit-up pool for a historical date (probed OK 2026-08-17).

    Used as the historical leg of the sentiment axis: 涨停数 (limit-up count)
    per date is a lineage-independent sentiment indicator (EastMoney datacenter
    vs legu). The legu interface is a current-day snapshot only and CANNOT be
    backfilled (calling it for a past date returns today's values -- a
    look-ahead trap; the collector never stamps legu rows with a past date).
    """
    import akshare as ak

    try:
        df = ak.stock_zt_pool_em(date=date.replace("-", ""))
    except Exception as exc:  # noqa: BLE001
        raise SentimentSourceError(
            f"stock_zt_pool_em({date}) failed: {type(exc).__name__}: {exc}"
        ) from exc
    if df is None or df.empty:
        raise SentimentSourceError(f"stock_zt_pool_em({date}) returned 0 rows")
    return df


def normalize_zt_pool_frame(raw: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    """Collapse the limit-up pool to per-date sentiment counts."""
    rows = [
        {"item": "涨停数", "value": float(len(raw))},
        {"item": "涨停成交额(元)", "value": float(pd.to_numeric(raw.get("成交额"), errors="coerce").sum())},
    ]
    out = pd.DataFrame(rows)
    out["trade_date"] = trade_date
    out["source"] = SOURCE_ZTPOOL
    out["retrieved_at"] = _utc_now()
    return out[CONTRACT_COLUMNS]


def collect_sentiment(
    dates: Iterable[str],
    *,
    include_legu_today: bool = True,
    max_failures: int = 20,
) -> dict[str, Any]:
    """Collect per-date market sentiment cross-sections into one frame.

    - zt_pool (EastMoney limit-up pool) is collected for EVERY requested date
      (historical-capable).
    - legu snapshot is collected only for the *latest* requested date, stamped
      with that date -- never backfilled (legu has no date parameter; stamping
      a past date with today's values would be look-ahead).
    """
    frames: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []
    dates_sorted = sorted(dates)
    for d in dates_sorted:
        try:
            zt = normalize_zt_pool_frame(fetch_zt_pool(d), d)
            frames.append(zt)
        except Exception as exc:  # noqa: BLE001
            failures.append({"date": d, "leg": "zt_pool", "error": f"{type(exc).__name__}: {exc}"})
            if len(failures) > max_failures:
                raise SentimentSourceError(
                    f"sentiment: {len(failures)} failures (> {max_failures}) -- "
                    f"refusing to publish; first: {failures[:3]}"
                ) from exc
    if include_legu_today and dates_sorted:
        latest = dates_sorted[-1]
        try:
            frames.append(normalize_legu_frame(fetch_legu(), latest))
        except Exception as exc:  # noqa: BLE001
            failures.append({"date": latest, "leg": "legu", "error": f"{type(exc).__name__}: {exc}"})
    if not frames:
        raise SentimentSourceError("sentiment: no frames collected")
    out = pd.concat(frames, ignore_index=True)
    out = out[~out["item"].isna()].reset_index(drop=True)
    return {
        "frame": out,
        "failures": failures,
        "rows": len(out),
        "dates_ok": len(dates_sorted) - sum(1 for f in failures if f.get("leg") == "zt_pool"),
        "dates_failed": sum(1 for f in failures if f.get("leg") == "zt_pool"),
        "sources": sorted(out["source"].unique().tolist()),
    }
