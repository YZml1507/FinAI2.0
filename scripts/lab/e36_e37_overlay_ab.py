"""e36/e37 冠军构型叠加 A/B 跑批器（shadow A/B，不动权威口径）。

四臂（各自独立 run，复用 T317 anchor_overrides + 同数据同费率）：
  baseline   —— 锚点参数零叠加（须复现锚点 metrics，见 e36 预登记复现判据）
  e36_tilt   —— composite_overlay + overlay_mode='tilt'（λ=0.30 clip±2）
  e36_filter —— composite_overlay + overlay_mode='filter'（z<0 剔除）
  e37_veto   —— event_veto_series（V1 户数激增 + V2 重复上榜）

数据：
  c1 z 分 = data/c1_overlay/c1_scores_daily.parquet（e36_c1_scores.py 产）
  veto    = data/e37_veto/veto_daily.parquet（e37_veto_series.py 产）

用法：
  .venv/bin/python -m scripts.lab.e36_e37_overlay_ab --arm baseline
  .venv/bin/python -m scripts.lab.e36_e37_overlay_ab --arm all   # 顺序跑四臂
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

from scripts import run_dividend_backtest as rdb  # noqa: E402
from scripts.produce_gate_evidence_run import anchor_overrides  # noqa: E402

DATA_PATH = ROOT / "data" / "dividend_stocks"
ANCHOR_PATH = ROOT / "experiments" / "runs" / \
    "20260921-093948-t312-dividend-v1-noseed.json"
C1_PATH = ROOT / "data" / "c1_overlay" / "c1_scores_daily.parquet"
VETO_PATH = ROOT / "data" / "e37_veto" / "veto_daily.parquet"
OUT_DIR = ROOT / "experiments" / "lab" / "e36e37"
SCRATCH = OUT_DIR / "runs"


def _note(m: str) -> None:
    print(f"[note] {m}", flush=True)


def _universe_symbols() -> set[str]:
    return {p.name for p in DATA_PATH.iterdir()
            if p.is_dir() and not p.name.startswith(('.', 'exdiv'))}


def _ts2bs(sym: str) -> str:
    """ts 格式 600000.SH → 引擎 baostock 格式 sh.600000。"""
    c, ex = sym.split('.')
    return f"{ex.lower()}.{c}"


def load_overlay() -> dict[str, dict[str, Decimal]]:
    """c1 长表 → {iso_date: {symbol: Decimal(z)}}（限定池内股票）。"""
    uni = _universe_symbols()
    df = pd.read_parquet(C1_PATH)
    df['symbol'] = df['symbol'].map(_ts2bs)
    df = df[df['symbol'].isin(uni)]
    ov: dict[str, dict[str, Decimal]] = {}
    for d, g in df.groupby('date'):
        ov[str(d)] = {s: Decimal(str(z))
                      for s, z in zip(g['symbol'], g['z'])}
    _note(f"overlay: {len(ov)} 日 × ~{len(df)//max(1,len(ov))} 股 "
          f"（池内限定 {len(uni)} 股）")
    return ov


def load_veto() -> dict[str, tuple]:
    df = pd.read_parquet(VETO_PATH)
    uni = _universe_symbols()
    return {str(d): tuple(_ts2bs(s) for s in syms if _ts2bs(s) in uni)
            for d, syms in zip(df['date'], df['symbols'])}


def run_arm(arm: str) -> dict:
    ov = anchor_overrides(ANCHOR_PATH)
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

    SCRATCH.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    out = rdb.run_dividend_backtest_2015_2024(
        data_path=DATA_PATH,
        registry_root=SCRATCH,
        strategy_overrides=ov,
    )
    rep = out['report']
    res = {
        'arm': arm, 'run_id': out['run_id'],
        'total_return': str(rep.total_return),
        'cagr': str(getattr(rep, 'cagr', '')),
        'mdd': str(getattr(rep, 'max_drawdown', '')),
        'round_trips': getattr(rep, 'round_trips', None),
        'trading_days': getattr(rep, 'trading_days', None),
        'elapsed_min': (time.time() - t0) / 60,
    }
    (OUT_DIR / f'{arm}_result.json').write_text(
        json.dumps(res, ensure_ascii=False, indent=2))
    _note(f"{arm}: {res}")
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
