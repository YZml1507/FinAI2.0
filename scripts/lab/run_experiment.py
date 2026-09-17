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
from datetime import date as _date
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
    "use_ma200_timing": lambda v: v.lower() in ("1", "true", "yes", "on"),
    "use_breadth_timing": lambda v: v.lower() in ("1", "true", "yes", "on"),
    "breadth_attack_threshold": Decimal,
    "breadth_defense_threshold": Decimal,
    "breadth_mid_cap": Decimal,
    "breadth_ice_confirm_days": int,
    # Alpha 三层（修池子/排雷/PEAD，2026-09-17）
    "use_quality_veto": lambda v: v.lower() in ("1", "true", "yes", "on"),
    "use_landmine_overlay": lambda v: v.lower() in ("1", "true", "yes", "on"),
    "landmine_cooldown_full": int,
    "landmine_cooldown_half": int,
    "use_pead": lambda v: v.lower() in ("1", "true", "yes", "on"),
    "pead_max_slots": int,
    "pead_hold_days": int,
    "pead_reserve_pct": Decimal,
    "pead_entry_mode": str,
    # 回测区间覆盖（非 DividendConfig 字段，run_experiment 单独提取传给 runner）
    "backtest_start": lambda v: _date.fromisoformat(v),
    "backtest_end": lambda v: _date.fromisoformat(v),
}

#: 非策略配置字段——传给 ``run_dividend_backtest_*`` 的回测区间参数。
_RUN_LEVEL_KEYS = ("backtest_start", "backtest_end")


def _load_breadth_series(path: Path) -> dict:
    """加载宽度序列 parquet -> {date_str: Decimal}；缺文件 Fail-Closed 报错。"""
    import pandas as pd
    if not path.exists():
        raise SystemExit(f"宽度序列文件缺失: {path}（⛔ Fail-Closed：无宽度数据不得开启宽度择时）")
    df = pd.read_parquet(path)
    # 日期键只保留 YYYY-MM-DD，与策略 day.isoformat() 查表键对齐（⛔ 禁带时间部分）
    return {str(d)[:10]: Decimal(str(b)) for d, b in zip(df["date"], df["breadth20"])}


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

    # 运行级参数（回测区间）不进 DividendConfig——``dataclasses.replace`` 只认字段名。
    bt_start = overrides.pop("backtest_start", None)
    bt_end = overrides.pop("backtest_end", None)

    orig_config_init = rdb.DividendConfig

    # 宽度择时开启时：自动关 MA200、注入宽度序列（互斥纪律由配置侧校验）
    # 宽度文件路径由环境变量 BREADTH_FILE 指定（⛔ 不进 --set，避免污染配置校验）
    import os
    if overrides.get("use_breadth_timing"):
        overrides.setdefault("use_ma200_timing", False)
        breadth_path = Path(os.environ.get(
            "BREADTH_FILE", str(LAB_ROOT / "market-breadth-a" / "breadth20_daily.parquet")))
        overrides["breadth_series"] = _load_breadth_series(breadth_path)

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
            start_date=bt_start,
            end_date=bt_end,
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
    # 允许 --set _breadth_file=路径 显式指定宽度文件（不入 DividendConfig）
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
