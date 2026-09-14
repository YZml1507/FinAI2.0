#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T312 并行策略实验运行器（lab runner）。

设计铁律（对齐 G-Gate 治理防伪）：

* **产物完全隔离**：每次实验产物（runs/ 与 universe/ 快照）落到
  ``experiments/lab/<实验名>/``，⛔ 绝不写入权威目录 ``experiments/runs``；
* **参数显式留痕**：实验参数与基准差异写入 ``<实验名>/experiment.json``，
  与产物同目录，构成可审计的实验出处；
* **权威脚本零改动**：通过 ``dataclasses.replace`` 在启动前覆盖配置，
  ⛔ 不修改 ``scripts/run_dividend_backtest.py`` 本体；
* **并行安全**：实验之间相互独立（各自目录），配合 taskset 绑核并行跑。

用法：
    .venv/bin/python scripts/lab/run_experiment.py \
        --name t312-buf005 --set timing_breach_buffer=0.005 \
        --set timing_breach_confirm_days=2 --set timing_rebuild_confirm_days=1
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import run_dividend_backtest as rdb  # noqa: E402
from strategy.candidates import DividendConfig  # noqa: E402

LAB_ROOT = ROOT / "experiments" / "lab"

#: 支持覆盖的参数及类型转换（与 DividendConfig 字段类型对齐）。
_PARAM_CASTERS = {
    "timing_breach_buffer": Decimal,
    "timing_breach_confirm_days": int,
    "timing_rebuild_confirm_days": int,
    "rebalance_days": int,
    "warmup_bars": int,
    "candidate_pool_size": int,
    "default_positions": int,
    "min_dividend_yield": Decimal,
}


def _parse_overrides(pairs: list[str]) -> dict:
    """把 ``--set k=v`` 列表解析为带类型的覆盖字典。"""
    overrides: dict = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"参数格式错误（须 k=v）: {pair!r}")
        key, raw = pair.split("=", 1)
        key = key.strip()
        caster = _PARAM_CASTERS.get(key)
        if caster is None:
            raise SystemExit(
                f"不支持的实验参数: {key!r}（支持: {sorted(_PARAM_CASTERS)}）"
            )
        overrides[key] = caster(raw.strip())
    return overrides


def run_experiment(name: str, overrides: dict, data_path: Path) -> dict:
    """跑一个命名实验：覆盖配置 → 隔离产物目录 → 返回结果摘要。"""
    lab_dir = LAB_ROOT / name
    lab_dir.mkdir(parents=True, exist_ok=True)

    orig_config_init = rdb.DividendConfig

    def _patched_config(**kwargs):
        cfg = orig_config_init(**kwargs)
        if overrides:
            cfg = replace(cfg, **overrides)
        return cfg

    # 覆盖配置构造（仅本进程生效，权威脚本的 import 引用不变更）
    rdb.DividendConfig = _patched_config  # type: ignore[misc]
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    try:
        result = rdb.run_dividend_backtest_2015_2024(
            data_path=data_path,
            initial_capital=Decimal("150000"),
            risk_free_annual=Decimal("0.025"),
            enable_gates=True,
            registry_root=lab_dir,
        )
    finally:
        rdb.DividendConfig = orig_config_init  # type: ignore[misc]
    elapsed = time.time() - t0

    report = result["report"]
    summary = {
        "experiment": name,
        "started": started,
        "elapsed_min": round(elapsed / 60, 1),
        "overrides": {k: str(v) for k, v in overrides.items()},
        "run_id": result["run_id"],
        "lab_dir": str(lab_dir.relative_to(ROOT)),
        "cagr": str(report.cagr),
        "max_drawdown": str(report.max_drawdown),
        "annual_turnover": str(report.annual_turnover or 0),
        "win_rate": str(report.win_rate or 0),
        "round_trips": report.round_trips,
        "fees_sum": str(report.fees_sum),
        "final_nav": str(report.final_nav),
        "total_return": str(report.total_return),
    }
    (lab_dir / "experiment.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _append_leaderboard(summary)
    return summary


def _append_leaderboard(summary: dict) -> None:
    """把实验摘要追加到实验总榜（JSONL，便于横向对比）。"""
    board = LAB_ROOT / "leaderboard.jsonl"
    with board.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(summary, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="T312 并行策略实验运行器")
    ap.add_argument("--name", required=True, help="实验名（目录 + 出处标识）")
    ap.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="k=v",
        help="覆盖策略参数（可多次），支持: " + ", ".join(sorted(_PARAM_CASTERS)),
    )
    ap.add_argument(
        "--data-path",
        default=str(ROOT / "data" / "dividend_stocks"),
        help="红利股数据目录",
    )
    args = ap.parse_args()

    overrides = _parse_overrides(args.overrides)
    data_path = Path(args.data_path)
    summary = run_experiment(args.name, overrides, data_path)

    print("\n" + "=" * 56)
    print(f"实验 {args.name} 完成（耗时 {summary['elapsed_min']} 分钟）")
    print("=" * 56)
    for k in ("cagr", "max_drawdown", "annual_turnover", "win_rate",
              "round_trips", "fees_sum", "final_nav"):
        print(f"  {k:18s}: {summary[k]}")
    print(f"  产物目录          : {summary['lab_dir']}")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
