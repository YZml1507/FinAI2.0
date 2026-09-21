"""e8b-v2（冠军构型+事件否决层）2025+ 永久 OOS 诊断跑批器。

纪律红线：2025-01-01 起为永久 OOS——本脚本产物**只用于观察诊断**，
禁止据此回调任何参数（TASK_TRACKER / ALPHA3_PLAYBOOK 条款）。
两臂（同 isst-e8b-fix688-v2 冻结构型 + 同数据同费率）：
  oos_baseline —— 冠军构型零叠加（2025-01-01 → 数据末日）
  oos_veto     —— + event_veto_series（V1 户数激增 + V2 重复上榜）

veto 序列由 2015-2024 存量 + 2025+ 续采段拼接（V1 gdhs-qoq、
V2 lhb-10td≥2，schema 与 e37_veto_series.py 一致）。

用法：.venv/bin/python -m scripts.lab.e37_oos_diagnostic --arm all
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date as _date
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.lab.run_experiment import run_experiment  # noqa: E402

DATA_PATH = ROOT / "data" / "dividend_stocks"
VETO_HIST = ROOT / "data" / "e37_veto" / "veto_daily.parquet"
VETO_2025 = ROOT / "data" / "e37_veto" / "veto_daily_2025plus.parquet"
OUT_DIR = ROOT / "experiments" / "lab" / "e37_oos"

OOS_START = "2025-01-01"
OOS_END = "2026-09-16"   # dividend_stocks 日线数据末日

# isst-e8b-fix688-v2 冻结构型（与 e36_e37_overlay_ab.py::CHAMPION 逐字一致）
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


def _note(m: str) -> None:
    print(f"[note] {m}", flush=True)


def _universe_symbols() -> set[str]:
    return {p.name for p in DATA_PATH.iterdir()
            if p.is_dir() and not p.name.startswith(('.', 'exdiv'))}


def _ts2bs(sym: str) -> str:
    c, ex = sym.split('.')
    return f"{ex.lower()}.{c}"


def load_veto_full() -> dict[str, tuple]:
    """拼接 2015-2024 存量 + 2025+ 续采段（后者缺失时只用存量）。"""
    uni = _universe_symbols()
    frames = [pd.read_parquet(VETO_HIST)]
    if VETO_2025.exists():
        frames.append(pd.read_parquet(VETO_2025))
        _note("veto: 已拼接 2025+ 续采段")
    else:
        _note("veto: 缺 veto_daily_2025plus.parquet，2025+ 段无否决覆盖"
              "（=该段两臂等价，诊断无意义，请先取回续采产物）")
    df = pd.concat(frames).drop_duplicates(subset=['date'])
    return {str(d): tuple(_ts2bs(s) for s in syms if _ts2bs(s) in uni)
            for d, syms in zip(df['date'], df['symbols'])}


def run_arm(arm: str) -> dict:
    ov = dict(CHAMPION)
    ov["backtest_start"] = _date.fromisoformat(OOS_START)
    ov["backtest_end"] = _date.fromisoformat(OOS_END)
    if arm == 'oos_veto':
        ov['event_veto_series'] = load_veto_full()
    elif arm != 'oos_baseline':
        raise ValueError(f"unknown arm {arm}")

    name = f"e37oos-{arm}"
    t0 = time.time()
    summary = run_experiment(name, ov, DATA_PATH)
    res = {'arm': arm, 'oos_start': OOS_START,
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
                    choices=['oos_baseline', 'oos_veto', 'all'])
    args = ap.parse_args()
    arms = (['oos_baseline', 'oos_veto'] if args.arm == 'all' else [args.arm])
    for a in arms:
        run_arm(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
