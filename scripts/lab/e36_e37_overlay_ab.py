"""e36/e37 冠军构型叠加 A/B 跑批器（shadow A/B）。

四臂（各自独立 run，同 isst-e8b-fix688-v2 冻结构型 + 同数据同费率）：
  baseline   —— 冠军构型零叠加（复现判据：CAGR 0.085814 / MDD
               0.1739899329267204508628679309 / rt 156 逐位一致——
               code_hash 异属正常，输出逐位一致证默认路径未变）
  e36_tilt   —— composite_overlay + overlay_mode='tilt'（λ=0.30 clip±2）
  e36_filter —— composite_overlay + overlay_mode='filter'（z<0 剔除）
  e37_veto   —— event_veto_series（V1 户数激增 + V2 重复上榜）

数据：c1 z = data/c1_overlay/c1_scores_daily.parquet；
      veto = data/e37_veto/veto_daily.parquet。

用法：.venv/bin/python -m scripts.lab.e36_e37_overlay_ab --arm baseline|all
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
C1_PATH = ROOT / "data" / "c1_overlay" / "c1_scores_daily.parquet"
VETO_PATH = ROOT / "data" / "e37_veto" / "veto_daily.parquet"
OUT_DIR = ROOT / "experiments" / "lab" / "e36e37"

# isst-e8b-fix688-v2 冻结构型（与 scripts/repro/reproduce_final_delivery.py
# COMMON_SETS 逐字一致——改这里=破坏对照锚）
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

# 复现判据（leaderboard 权威记录逐字值）
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
    df = pd.read_parquet(C1_PATH)
    df['symbol'] = df['symbol'].map(_ts2bs)
    df = df[df['symbol'].isin(uni)]
    ov: dict[str, dict[str, Decimal]] = {}
    for d, g in df.groupby('date'):
        ov[str(d)] = {s: Decimal(str(z))
                      for s, z in zip(g['symbol'], g['z'])}
    _note(f"overlay: {len(ov)} 日 × ~{len(df)//max(1,len(ov))} 股（池内）")
    return ov


def load_veto() -> dict[str, tuple]:
    df = pd.read_parquet(VETO_PATH)
    uni = _universe_symbols()
    return {str(d): tuple(_ts2bs(s) for s in syms if _ts2bs(s) in uni)
            for d, syms in zip(df['date'], df['symbols'])}


def run_arm(arm: str) -> dict:
    ov = dict(CHAMPION)
    if arm == 'e36_tilt':
        ov['composite_overlay'] = load_overlay()
        ov['overlay_mode'] = 'tilt'
    elif arm == 'e36_filter':
        ov['composite_overlay'] = load_overlay()
        ov['overlay_mode'] = 'filter'
    elif arm == 'e37_veto':
        ov['event_veto_series'] = load_veto()
    elif arm != 'baseline':
        raise ValueError(f"unknown arm {arm}")

    name = f"e36e37-{arm}"
    t0 = time.time()
    summary = run_experiment(name, ov, DATA_PATH)   # 返回 summary dict
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
                    choices=['baseline', 'e36_tilt', 'e36_filter',
                             'e37_veto', 'all'])
    args = ap.parse_args()
    arms = (['baseline', 'e36_tilt', 'e36_filter', 'e37_veto']
            if args.arm == 'all' else [args.arm])
    for a in arms:
        run_arm(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
