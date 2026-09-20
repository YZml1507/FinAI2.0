#!/usr/bin/env python3
"""GC001 逆回购日度利率序列延长器（e20 前置，append-only）。

数据源：腾讯 kline sh204001（ifzq.gtimg.cn，收盘=年化利率 %），与
``scripts/build_gc001_series.py`` 同口径。纪律：
  * 只追加 date > 现有最大日的行；存量行**逐字节保留**，锚点不变性
    由「前缀行逐值相等」验证（见 manifest prefix_identity_ok）。
  * 重叠段比对（默认 2024 全年）：仅作数据源可信度证据，⛔ 不写回。
  * 空洞断言：延长后 2025-01-01..end 段相邻日期间隔 ≤45 自然日
    （ledger._cash_rate_for 的 ffill 上限），fail-closed。

输出：data/rates/gc001_daily.parquet（原子写 tmp→rename）+
      data/rates/gc001_extend_manifest.json（出处+比对+sha256 三件套）。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date as _date
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
PARQUET = ROOT / "data" / "rates" / "gc001_daily.parquet"
MANIFEST = ROOT / "data" / "rates" / "gc001_extend_manifest.json"
BACKUP = ROOT / "data" / "rates" / "gc001_daily.parquet.bak_20260920"

KLINE = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
SYMBOL = "sh204001"
OVERLAP_START = "2024-01-01"
FETCH_END = _date.today().isoformat()
FFILL_MAX_GAP_DAYS = 45
TOL_PP = 0.005        # 年化百分点容差（PREREG §三.1）


def fetch_gc001(start: str, end: str) -> pd.DataFrame:
    """腾讯 kline sh204001 日线 → (date, rate_annual)。count 覆盖全段。"""
    days = (_date.fromisoformat(end) - _date.fromisoformat(start)).days
    resp = requests.get(
        KLINE,
        params={"param": f"{SYMBOL},day,{start},{end},{days + 10},"},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0:
        raise SystemExit(f"腾讯 param error: {payload.get('msg')}")
    rows = payload.get("data", {}).get(SYMBOL, {}).get("day", [])
    if not rows:
        raise SystemExit("腾讯返回空序列（⛔ Fail-Closed：不静默降级）")
    df = pd.DataFrame(
        {"date": [r[0] for r in rows],
         "rate_annual": [float(r[2]) for r in rows]}
    )
    df = df[(df["date"] >= start) & (df["date"] <= end)]
    return df.drop_duplicates(subset=["date"], keep="last").sort_values("date").reset_index(drop=True)


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    old = pd.read_parquet(PARQUET)
    sha_before = sha256_file(PARQUET)
    if not BACKUP.exists():
        BACKUP.write_bytes(PARQUET.read_bytes())
    last = str(old["date"].max())

    fetched = fetch_gc001(OVERLAP_START, FETCH_END)
    print(f"fetched {len(fetched)} rows {fetched['date'].min()}..{fetched['date'].max()}")

    # ① 重叠段一致性（只比对，⛔ 不写回）
    m = fetched.merge(old, on="date", suffixes=("_new", "_old"))
    m = m[m["date"] <= last]
    m["d"] = (m["rate_annual_new"] - m["rate_annual_old"]).abs()
    ok = m[m["d"] <= TOL_PP]
    share = len(ok) / len(m) if len(m) else 0.0
    diffs = m[m["d"] > TOL_PP][["date", "rate_annual_old", "rate_annual_new", "d"]]
    print(f"overlap days={len(m)} within {TOL_PP}pp: {len(ok)} ({share:.2%})")
    if len(diffs):
        print(diffs.to_string(index=False))

    # ② 追加新行（严格 > last）
    new_rows = fetched[fetched["date"] > last]
    out = pd.concat([old, new_rows[["date", "rate_annual"]]], ignore_index=True)
    assert out["date"].is_unique and out["date"].is_monotonic_increasing
    bad = out[(out["rate_annual"] <= 0) | (out["rate_annual"] > 60)]
    if len(bad):
        raise SystemExit(f"利率越界 {len(bad)} 行: {bad.head()}")

    # ③ 空洞断言（延长段）
    ds = [_date.fromisoformat(d) for d in out["date"] if d >= "2025-01-01"]
    gaps = [(b, a, (b - a).days) for a, b in zip(ds, ds[1:]) if (b - a).days > FFILL_MAX_GAP_DAYS]
    if gaps:
        raise SystemExit(f"空洞 >{FFILL_MAX_GAP_DAYS}d: {gaps}")

    tmp = PARQUET.with_suffix(".parquet.tmp")
    out.to_parquet(tmp, index=False)
    tmp.replace(PARQUET)
    sha_after = sha256_file(PARQUET)

    # ④ 锚点不变性：前缀逐值相等
    reread = pd.read_parquet(PARQUET)
    prefix_identity = bool(
        (reread.iloc[: len(old)]["date"].values == old["date"].values).all()
        and (reread.iloc[: len(old)]["rate_annual"].values == old["rate_annual"].values).all()
    )
    assert prefix_identity, "前缀行不一致——append-only 被破坏"

    MANIFEST.write_text(json.dumps({
        "source": "tencent kline sh204001 (ifzq.gtimg.cn), close=年化利率%",
        "fetched_range": [str(fetched["date"].min()), str(fetched["date"].max())],
        "previous_max_date": last,
        "rows_appended": int(len(new_rows)),
        "rows_total": int(len(out)),
        "sha256_before": sha_before,
        "sha256_after": sha_after,
        "backup": str(BACKUP.relative_to(ROOT)),
        "overlap_check": {
            "range": [OVERLAP_START, last],
            "days": int(len(m)),
            "within_tol": int(len(ok)),
            "share": round(share, 6),
            "tol_pp": TOL_PP,
            "mismatches": diffs.to_dict("records"),
        },
        "gap_check_2025plus": "PASS（无 >45 自然日空洞）",
        "prefix_identity_ok": prefix_identity,
        "timestamp": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2))
    print(f"appended {len(new_rows)} rows → {PARQUET.name} total {len(out)} "
          f"(…{out['date'].max()}) sha {sha_after[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
