"""e37 veto 买侧否决序列装载（e81 并入基线后的共享入口）。

`data/e37_veto/veto_daily.parquet`：date × symbols[]，语义=当日买入否决
名单（不强制卖）。代码格式与分数表一致（'000001.SZ'）——经
``normalize_score_code`` 规整后落 frozenset。
"""
from __future__ import annotations

from datetime import date as _date
from pathlib import Path

import pandas as pd

from strategy.score_basket import normalize_score_code


def load_veto_series(veto_path: Path) -> dict[_date, frozenset[str]]:
    d = pd.read_parquet(veto_path, columns=["date", "symbols"])
    out: dict[_date, frozenset[str]] = {}
    for r in d.itertuples(index=False):
        day = _date.fromisoformat(str(r.date)[:10])
        out[day] = frozenset(
            normalize_score_code(s) for s in r.symbols)
    return out
