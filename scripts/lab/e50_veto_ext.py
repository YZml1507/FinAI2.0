#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e50 veto 扩展电池（docs/E50_VETO_EXT_PREREG.md 冻结）。

V4 = {sev≥2 函件} ∪ {任类函件 30 自然日 ≥2 封}，禁买窗=事件日
起 20 交易日。veto_daily_ext = V1∪V2∪V3∪V4 逐日合并。

用法：.venv/bin/python -m scripts.lab.e50_veto_ext --arm <arm|all>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date as _date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.lab.run_experiment import run_experiment  # noqa: E402
from scripts.lab.e36_e37_overlay_ab import CHAMPION  # noqa: E402
from scripts.lab.e48_letters_screen import load_letters  # noqa: E402
from scripts.lab import e27_insider_screen as e27  # noqa: E402

DATA_PATH = ROOT / "data" / "dividend_stocks"
VETO_BASE = ROOT / "data" / "e37_veto" / "veto_daily.parquet"
VETO_EXT = ROOT / "data" / "e37_veto" / "veto_daily_ext.parquet"
OUT_DIR = ROOT / "experiments" / "lab" / "e50"

BAN_TD = 20
HF_FREQ_DAYS = 30


def _note(m): print(f"[note] {m}", flush=True)


def build_veto_ext() -> Path:
    """V4 事件 → 禁买窗集合，并入既有 veto_daily。"""
    close_w = pd.read_parquet(e27.OUT_DIR / 'panel_close.parquet')
    days = list(close_w.index)
    iloc = {d: i for i, d in enumerate(days)}

    lt = load_letters()
    lt = lt[lt.ann_date <= pd.Timestamp('2024-12-31')]
    sev_hit = set(lt[lt.sev >= 2][['ts_code', 'ann_date']]
                  .itertuples(index=False, name=None))
    # 30 自然日 ≥2 封（任类）
    lt_s = lt.sort_values('ann_date')
    hist = {}
    for t in lt_s.itertuples():
        h = hist.setdefault(t.ts_code, [])
        h.append(t.ann_date)
        h = [d for d in h if (t.ann_date - d).days <= HF_FREQ_DAYS]
        hist[t.ts_code] = h
        if len(h) >= 2:
            sev_hit.add((t.ts_code, t.ann_date))

    ban = {str(pd.Timestamp(d).date()): set() for d in days}
    for sym, ann in sev_hit:
        i = iloc.get(np.datetime64(ann))
        if i is None:
            continue
        for d in days[i:i + BAN_TD]:
            ban[str(pd.Timestamp(d).date())].add(sym)

    base = pd.read_parquet(VETO_BASE)
    ext_rows = []
    for d, syms in zip(base.date, base.symbols):
        s = set(syms) | ban.get(str(d)[:10], set())
        ext_rows.append({'date': str(d)[:10], 'symbols': sorted(s)})
    ext = pd.DataFrame(ext_rows)
    ext.to_parquet(VETO_EXT, index=False)
    _note(f"veto_ext written: {len(ext)} days; "
          f"avg_set={ext.symbols.map(len).mean():.0f} "
          f"(base {base.symbols.map(len).mean():.0f})")
    return VETO_EXT


def load_veto(path: Path, uni: set) -> dict:
    """与 e37 loader 同口径：ts→bs 映射 + 池内过滤。"""
    from scripts.lab.e36_e37_overlay_ab import _ts2bs
    df = pd.read_parquet(path)
    return {str(d)[:10]: tuple(_ts2bs(s) for s in syms if _ts2bs(s) in uni)
            for d, syms in zip(df['date'], df['symbols'])}


ARMS = {
    "verify-off": {},
    "v123": {"veto": "base"},
    "v1234": {"veto": "ext"},
    "g1-v123":  {"veto": "base", "extra": {"backtest_start": _date(2021, 1, 1)}},
    "g1-v1234": {"veto": "ext",  "extra": {"backtest_start": _date(2021, 1, 1)}},
    "g2-a315-v1234": {"veto": "ext",
                      "extra": {"breadth_attack_threshold": Decimal("0.315")}},
    "g3-v1234-fee2": {"veto": "ext",
                      "extra": {"fee_multiplier": Decimal("2")}},
}

EXPECTED_VERIFY = {
    "cagr": "0.085814",
    "max_drawdown": "0.1739899329267204508628679309",
    "round_trips": 156,
}
EXPECTED_V123 = {
    "cagr": "0.091414",
    "max_drawdown": "0.174709",
}


def run_arm(name: str, v_base, v_ext) -> dict:
    ov = dict(CHAMPION)
    spec = ARMS[name]
    extra = spec.get("extra", {})
    if spec.get("veto") == "base":
        ov["event_veto_series"] = v_base
    elif spec.get("veto") == "ext":
        ov["event_veto_series"] = v_ext
    ov.update(extra)
    _note(f"arm={name} start={time.strftime('%H:%M')}")
    t0 = time.time()
    res = run_experiment(f"e50-{name}", ov, DATA_PATH)
    _note(f"arm={name} {time.time()-t0:.0f}s cagr={res.get('cagr')} "
          f"mdd={res.get('max_drawdown')} rt={res.get('round_trips')}")
    (OUT_DIR / f'{name}_result.json').write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=str))
    if name == "verify-off":
        for k, v in EXPECTED_VERIFY.items():
            if str(res.get(k)) != str(v):
                _note(f"⛔ verify-off 不复现 {k}: {res.get(k)} != {v}")
                return {"_verify_failed": True, "res": res}
    if name == "v123":
        cagr = float(res.get("cagr", 0))
        if abs(cagr - 0.091414) > 0.001:
            _note(f"⛔ v123 未复现 e37 锚 {cagr} != 0.091414")
            return {"_verify_failed": True, "res": res}
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--arm', required=True)
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not VETO_EXT.exists():
        build_veto_ext()
    from scripts.lab.e36_e37_overlay_ab import _universe_symbols
    uni = _universe_symbols()
    v_base = load_veto(VETO_BASE, uni)
    v_ext = load_veto(VETO_EXT, uni)
    names = list(ARMS) if args.arm == 'all' else [args.arm]
    out = {}
    for nm in names:
        r = run_arm(nm, v_base, v_ext)
        out[nm] = r
        if r.get("_verify_failed"):
            break
    (OUT_DIR / 'battery_results.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
