"""e8b-v2（冠军 e8b + e37 事件否决层）晋升门禁电池跑批器。

预登记评估——e37 判强后的标准晋升门禁（PROMOTION_CHECKLIST 复用口径），
回答的不是「veto 在锚点是否有增量」（e37 已答：+0.56pp/+0.07pp 双门过），
而是「该增量在留出/阈值扰动/成本倍增下是否稳健」。

臂阵（各自独立 run，isst-e8b-fix688-v2 冻结构型 + 同数据同费率）：
  verify-veto   —— 全窗 veto on：复现校验臂，对 e37 收单值
                   （CAGR 0.091414 / MDD 0.174709 / rt 155）核验本机
                   再生成否决序列与管线的逐位一致性；
  g1-off/veto   —— G-1 留出稳健：backtest_start=2021-01-01 半窗两臂；
  g2-a315-off/veto、g2-a385-off/veto —— G-2 阈值扰动：
                   breadth_attack_threshold ±10%（0.315/0.385）× veto on/off；
  g3-fee2-off/veto —— G-3 成本倍增：fee_multiplier=2 × veto on/off。

数据：veto = data/e37_veto/veto_daily.parquet（V1 户数激增+V2 重复上榜，
      e35/e37 冻结口径）；冠军构型与 e36_e37_overlay_ab.CHAMPION 逐字一致。

用法：.venv/bin/python -m scripts.lab.e37_promotion_battery --arm <name>|all
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
from scripts.lab.e36_e37_overlay_ab import CHAMPION, _ts2bs  # noqa: E402

DATA_PATH = ROOT / "data" / "dividend_stocks"
VETO_PATH = ROOT / "data" / "e37_veto" / "veto_daily.parquet"
OUT_DIR = ROOT / "experiments" / "lab" / "e37promo"

# e37 收单值（docs/E37_VETO_LAYER_PREREG.md §五 回填）——verify 臂逐值判据
EXPECTED_VERIFY = {
    "cagr": "0.091414",
    "max_drawdown": "0.174709",
    "round_trips": 155,
    "annual_turnover": "4.7408",
}

ARMS = {
    "verify-veto": {"veto": True},
    "g1-off":  {"veto": False, "extra": {"backtest_start": _date(2021, 1, 1)}},
    "g1-veto": {"veto": True,  "extra": {"backtest_start": _date(2021, 1, 1)}},
    "g2-a315-off":  {"veto": False,
                     "extra": {"breadth_attack_threshold": Decimal("0.315")}},
    "g2-a315-veto": {"veto": True,
                     "extra": {"breadth_attack_threshold": Decimal("0.315")}},
    "g2-a385-off":  {"veto": False,
                     "extra": {"breadth_attack_threshold": Decimal("0.385")}},
    "g2-a385-veto": {"veto": True,
                     "extra": {"breadth_attack_threshold": Decimal("0.385")}},
    "g3-fee2-off":  {"veto": False, "extra": {"fee_multiplier": Decimal("2")}},
    "g3-fee2-veto": {"veto": True,  "extra": {"fee_multiplier": Decimal("2")}},
}


def _note(m: str) -> None:
    print(f"[note] {m}", flush=True)


def _universe_symbols() -> set[str]:
    return {p.name for p in DATA_PATH.iterdir()
            if p.is_dir() and not p.name.startswith(('.', 'exdiv'))}


def load_veto() -> dict[str, tuple]:
    df = pd.read_parquet(VETO_PATH)
    uni = _universe_symbols()
    return {str(d): tuple(_ts2bs(s) for s in syms if _ts2bs(s) in uni)
            for d, syms in zip(df['date'], df['symbols'])}


def run_arm(arm: str, veto: dict | None = None) -> dict:
    spec = ARMS[arm]
    ov = dict(CHAMPION)
    ov.update(spec.get("extra", {}))
    if spec["veto"]:
        if veto is None:
            veto = load_veto()
        ov["event_veto_series"] = veto

    name = f"e37promo-{arm}"
    t0 = time.time()
    summary = run_experiment(name, ov, DATA_PATH)
    res = {"arm": arm,
           "elapsed_min": (time.time() - t0) / 60,
           "summary": {k: str(v) for k, v in summary.items()}}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{arm}_result.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2))
    _note(f"{arm} done: cagr={summary.get('cagr')} "
          f"mdd={summary.get('max_drawdown')} "
          f"rt={summary.get('round_trips')} ({res['elapsed_min']:.1f}min)")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True,
                    choices=list(ARMS) + ["all"])
    args = ap.parse_args()
    arms = list(ARMS) if args.arm == "all" else [args.arm]
    veto = load_veto() if any(ARMS[a]["veto"] for a in arms) else None
    for a in arms:
        run_arm(a, veto)
    return 0


if __name__ == "__main__":
    sys.exit(main())
