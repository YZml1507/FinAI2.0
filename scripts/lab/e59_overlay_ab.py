"""e59 I3 叠加 A/B 跑批器（shadow A/B，冻结预登记 E59_I3_OVERLAY_PREREG）。

两臂（各自独立 run，同 isst-e8b-fix688-v2 冻结构型 + 同数据同费率）：
  baseline  —— 冠军构型零叠加（复现判据同 e36：逐位一致）
  e59_tilt  —— composite_overlay + overlay_mode='tilt'，信号源=I3 单信号
              （λ=0.30 clip±2，build_signals 已定向高=好）

数据：i3 z = data/i3_overlay/i3_scores_daily.parquet。

用法：.venv/bin/python -m scripts.lab.e59_overlay_ab --arm baseline|all
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.lab.run_experiment import run_experiment  # noqa: E402

DATA_PATH = ROOT / "data" / "dividend_stocks"
I3_PATH = ROOT / "data" / "i3_overlay" / "i3_scores_daily.parquet"
OUT_DIR = ROOT / "experiments" / "lab" / "e59"

CHAMPION = {
    "use_breadth_timing": True,
    "use_ma200_timing": False,
    "breadth_defense_threshold": Decimal("0.25"),
    "breadth_attack_threshold": Decimal("0.35"),
    "breadth_mid_cap": Decimal("0.0"),
    "breadth_ice_confirm_days": 1,
    "breadth_demote_liquidate": True,
    "cash_yield_series": "data/rates/gc001_daily.parquet",
}

EXPECTED_BASELINE = {
    "cagr": "0.085814",
    "max_drawdown": "0.1739899329267204508628679309",
    "round_trips": 156,
    "fees_sum": "22328.60",
    "annual_turnover": "4.607400",
}


def _note(m: str) -> None:
    print(f"[note] {m}", flush=True)


def _universe_symbols() -> set[str]:
    return {p.name for p in DATA_PATH.iterdir()
            if p.is_dir() and not p.name.startswith(('.', 'exdiv'))}


def _ts2bs(sym: str) -> str:
    c, ex = sym.split('.')
    return f"{ex.lower()}.{c}"


def load_overlay() -> dict[str, dict[str, Decimal]]:
    uni = _universe_symbols()
    df = pd.read_parquet(I3_PATH)
    df['symbol'] = df['symbol'].map(_ts2bs)
    df = df[df['symbol'].isin(uni)]
    ov: dict[str, dict[str, Decimal]] = {}
    for d, g in df.groupby('date'):
        ov[str(d)] = {s: Decimal(str(z))
                      for s, z in zip(g['symbol'], g['z'])}
    _note(f"overlay: {len(ov)} 日 × ~{len(df)//max(1,len(ov))} 股（池内）")
    return ov


def run_arm(arm: str) -> dict:
    ov = dict(CHAMPION)
    if arm == 'e59_tilt':
        ov['composite_overlay'] = load_overlay()
        ov['overlay_mode'] = 'tilt'
    elif arm != 'baseline':
        raise ValueError(f"unknown arm {arm}")

    name = f"e59-{arm}"
    t0 = time.time()
    summary = run_experiment(name, ov, DATA_PATH)
    res = {'arm': arm,
           'elapsed_min': (time.time() - t0) / 60,
           'summary': {k: str(v) for k, v in summary.items()}}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f'{arm}_result.json').write_text(
        json.dumps(res, ensure_ascii=False, indent=2))
    _note(f"{arm} done: cagr={summary.get('cagr')} "
          f"mdd={summary.get('max_drawdown')} "
          f"rt={summary.get('round_trips')} ({res['elapsed_min']:.1f}min)")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--arm', required=True,
                    choices=['baseline', 'e59_tilt', 'all'])
    args = ap.parse_args()
    arms = (['baseline', 'e59_tilt'] if args.arm == 'all' else [args.arm])
    for a in arms:
        run_arm(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
