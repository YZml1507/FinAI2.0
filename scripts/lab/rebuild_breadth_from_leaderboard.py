#!/usr/bin/env python
"""Rebuild experiments/lab/market-breadth-a/breadth20_daily.parquet exactly
from the breadth_series recorded in experiments/lab/leaderboard.jsonl
(isst-e8b record). No recomputation — exact anchor input reconstruction."""
import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
LB = ROOT / "experiments" / "lab" / "leaderboard.jsonl"
OUT_DIR = ROOT / "experiments" / "lab" / "market-breadth-a"
OUT = OUT_DIR / "breadth20_daily.parquet"

anchor = None
for line in LB.open():
    d = json.loads(line)
    if d.get("experiment") == "isst-e8b":
        anchor = d["overrides"]["breadth_series"]
if anchor is None:
    sys.exit("isst-e8b record with breadth_series not found")

pairs = re.findall(r"'(\d{4}-\d{2}-\d{2})': Decimal\('([^']+)'\)", anchor)
assert len(pairs) == anchor.count("Decimal("), (
    f"parsed {len(pairs)} != Decimal count {anchor.count('Decimal(')}"
)
assert len({d for d, _ in pairs}) == len(pairs), "duplicate dates"

df = pd.DataFrame(
    {"date": pd.to_datetime([d for d, _ in pairs]),
     "breadth20": [float(v) for _, v in pairs]}
)
OUT_DIR.mkdir(parents=True, exist_ok=True)
df.to_parquet(OUT, index=False)

# Round-trip verification through the runner's own loader
sys.path.insert(0, str(ROOT))
from scripts.lab.run_experiment import _load_breadth_series  # noqa: E402

loaded = _load_breadth_series(OUT)
expected = {d: Decimal(v) for d, v in pairs}
assert loaded == expected, "round-trip mismatch"
for k in loaded:
    assert type(loaded[k]) is Decimal and type(expected[k]) is Decimal

sha = hashlib.sha256(OUT.read_bytes()).hexdigest()
prov = f"""# breadth20_daily.parquet provenance

Reconstructed on this machine **without recomputation**:
parsed verbatim from `experiments/lab/leaderboard.jsonl`, record
`experiment == "isst-e8b"`, field `overrides["breadth_series"]`
(the exact series the anchor run consumed; identical string verified in
all 98 leaderboard records carrying breadth_series from e1-veto through
e18-pos20f10eq; the original machine's `isst-e8b-fix688` run has no
record in this committed leaderboard copy — it ran after the snapshot).

- rows: {len(df)}
- date range: {df['date'].min().date()} .. {df['date'].max().date()}
- sha256: {sha}
- round-trip: `_load_breadth_series` == parsed Decimal dict (exact)
- script: scripts/lab/rebuild_breadth_from_leaderboard.py
"""
(OUT_DIR / "PROVENANCE.md").write_text(prov)
print(f"rows={len(df)} range={df['date'].min().date()}..{df['date'].max().date()}")
print(f"sha256={sha}")
print("round-trip: OK")
