#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T317 证据跑批编排器 —— Scheduled Full Gate Audit 修复的跑批侧（交接件第 8 步）。

编排 4 跑（**跑批前必须先提交全部代码**，保证 baseline×2 同 code_hash ⇒
同 repro_fingerprint ⇒ G-REPRO-1 verified 组）：

1. **滑点 +50% 全窗压测**（scratch）：同锚点配置，``price_model`` 换滑点 ×1.5
   （默认 5bps → 7.5bps）——产出 ``stress_return`` 供 S-3。
2. **2015-2016 压测窗**（scratch）：同锚点配置跑 2015-01-05 ~ 2016-12-31。
   ⛔ 交接件原文「2015 全年」会被 ``warmup_bars=210`` 吃光（2015 仅剩 ~34 个
   可交易日，无往返 ⇒ G-STRESS-1 仍判 INCONCLUSIVE）；扩到 2016 底使窗口
   覆盖 2015 股灾尾部 + 2016 熔断，含真实持仓/往返（约 487 交易日 ≥200）。
3. **baseline×2**（authoritative ``experiments/runs/``）：严格复刻锚点
   ``20260915-235155-t312-dividend-v1-noseed`` 的全部参数——含其**内嵌**
   ``breadth_series``（lab 重建的 breadth20_daily.parquet 与锚点序列在
   1015/2431 日有 ≤1e-4 微差 ⇒ 复刻必须用锚点自带序列，否则择时判据不可
   比特级一致、指标不复现）。两跑 share fingerprint ⇒ verified 组。

证据键来源纪律（fail-closed，全部如实计算/插桩，⛔ 不合成）：
  * ``executed_calls``：对关键函数做模块级包装计数（本进程生效，跑完还原）；
  * ``stress_return`` / ``round_trips`` / ``trading_days``：两压测跑的真实
    ``report.*``（⛔ 不用 runner 的线性折算公式——实测优于近似）。

用法：``python -m scripts.produce_gate_evidence_run [--slip-only|--stress-only|--baseline-only]``
"""
from __future__ import annotations

import argparse
import functools
import json
import sys
import time
from dataclasses import fields as _dc_fields
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 与 run_experiment 同口径：stock_basic 离线缓存（在线拉取不稳会破坏池子可比性）
import os as _os
_os.environ.setdefault(
    "FNAI_STOCK_BASIC_CACHE", str(ROOT / "data" / "stock_basic_cache.parquet"))

import backtest.dividend_tax as _divtax_mod   # noqa: E402
import backtest.fees as _fees_mod            # noqa: E402
import backtest.ledger as _ledger_mod        # noqa: E402
import backtest.settle as _settle_mod        # noqa: E402
import scripts.run_dividend_backtest as rdb  # noqa: E402
import strategy.candidates as _cand_mod      # noqa: E402
from backtest.fees import default_fee_config, make_price_model  # noqa: E402
from strategy.candidates import DividendConfig  # noqa: E402
from strategy.portfolio import PortfolioConfig  # noqa: E402

ANCHOR_PATH = ROOT / "experiments" / "runs" / "20260915-235155-t312-dividend-v1-noseed.json"
DATA_PATH = ROOT / "data" / "dividend_stocks"
SCRATCH_ROOT = ROOT / "experiments" / "lab" / "gate-evidence-scratch"
SUMMARY_PATH = ROOT / "experiments" / "lab" / "gate-evidence-scratch" / "produce_summary.json"

#: 压测窗（交接件 2015 窗 + 延展到 2016 覆盖 warmup 与 2016-01 熔断）
STRESS_WINDOW = (_date(2015, 1, 5), _date(2016, 12, 31))


# ------------------------------------------------------------------ 插桩

class CallTracer:
    """模块级函数/类包装计数器（L-3 executed_calls 证据）。

    包的是**模块属性**：被调方经模块 globals/运行时局部 import 解到的是
    包装层 ⇒ 调用计入集合。仅本进程生效，``restore()`` 全还原。
    """

    def __init__(self) -> None:
        self.calls: set[str] = set()
        self._undo: list[tuple[Any, str, Any]] = []

    def wrap(self, module: Any, name: str) -> None:
        orig = getattr(module, name)
        calls = self.calls

        @functools.wraps(orig)
        def _w(*args: Any, **kwargs: Any) -> Any:
            calls.add(name)
            return orig(*args, **kwargs)

        setattr(module, name, _w)
        self._undo.append((module, name, orig))

    def restore(self) -> None:
        for module, name, orig in reversed(self._undo):
            setattr(module, name, orig)
        self._undo.clear()


#: 插桩点表：rdb 模块全局名（其函数体内调用点解 globals）+ 各底层模块实现。
_TRACE_POINTS: tuple[tuple[Any, str], ...] = (
    (rdb, "DividendConfig"),
    (rdb, "PortfolioConfig"),
    (rdb, "ParquetDailyFeed"),
    (rdb, "Ledger"),
    (rdb, "MatchEngine"),
    (rdb, "BacktestBroker"),
    (rdb, "BacktestEngine"),
    (rdb, "DividendStrategy"),
    (rdb, "compute_metrics"),
    (rdb, "make_fee_model"),
    (rdb, "make_price_model"),
    (rdb, "hash_path_manifest"),
    (rdb, "run_pre_run_gates"),
    (rdb, "run_post_run_gates"),
    (_cand_mod, "select_targets"),
    (_cand_mod, "plan_positions"),
    (_cand_mod, "diff_to_orders"),
    (_divtax_mod, "compute_dividend_tax_detail"),
    (_fees_mod, "compute_fees"),
    (_fees_mod, "apply_slippage"),
    (_ledger_mod, "compute_tx_hash"),
    (_settle_mod, "settle_day_detail"),
)


# ------------------------------------------------------------------ 锚点复刻

_DECIMAL_KEYS = {
    "min_dividend_yield", "timing_breach_buffer", "breadth_attack_threshold",
    "breadth_defense_threshold", "breadth_mid_cap", "pead_reserve_pct",
    "cash_yield_annual", "crowding_threshold", "crowding_cap",
    "low_vol_keep_pct", "max_dividend_yield",
}
_INT_KEYS = {
    "candidate_pool_size", "min_positions", "max_positions", "default_positions",
    "rebalance_days", "warmup_bars", "timing_breach_confirm_days",
    "timing_rebuild_confirm_days", "breadth_ice_confirm_days",
    "landmine_cooldown_full", "landmine_cooldown_half",
    "pead_max_slots", "pead_hold_days", "dv_skip_top",
}
_BOOL_KEYS = {
    "use_ma200_timing", "use_breadth_timing", "use_quality_veto",
    "use_landmine_overlay", "use_pead", "breadth_demote_liquidate",
    "use_crowding_breaker",
}
_STR_KEYS = {
    "index_symbol", "breadth_weight_mode", "attack_instrument",
    "weight_mode", "cash_yield_series", "pead_entry_mode",
}
_SERIES_KEYS = {"breadth_series", "crowding_series"}   # {iso: str} → {iso: Decimal}

_PF_DECIMAL = {"min_position_value", "min_daily_amount",
               "max_participation_rate", "max_price"}
_PF_INT = {"target_count", "min_positions", "max_positions",
           "hard_limit", "lot_size"}


def _typed_portfolio(p: Mapping[str, Any]) -> PortfolioConfig:
    kw: dict[str, Any] = {}
    for k, v in p.items():
        if k in _PF_DECIMAL:
            kw[k] = Decimal(str(v)) if v is not None else None
        elif k in _PF_INT:
            kw[k] = int(v)
        else:
            kw[k] = v
    return PortfolioConfig(**kw)


def anchor_overrides(anchor_path: Path = ANCHOR_PATH) -> dict[str, Any]:
    """锚点产物 params → ``DividendConfig`` 覆盖 kwargs（类型逐项还原）。

    ⛔ Fail-Closed：锚点含本配置不认识的键 ⇒ raise（参数面漂移不许静默丢键）。
    锚点缺失的新字段（cash_yield_series / use_pead 等）取现行默认——其默认值
    语义与锚点时代一致（全部关闭/空），字段引入属向后兼容扩展。
    """
    record = json.loads(anchor_path.read_text(encoding="utf-8"))
    params = record["params"]
    cfg_fields = {f.name for f in _dc_fields(DividendConfig)}
    unknown = set(params) - cfg_fields - {"portfolio"}
    if unknown:
        raise ValueError(f"锚点参数含现行 DividendConfig 不认识的键: {sorted(unknown)}")
    ov: dict[str, Any] = {}
    for k, v in params.items():
        if k == "portfolio":
            ov["portfolio"] = _typed_portfolio(v)
        elif k in _SERIES_KEYS:
            ov[k] = {str(d)[:10]: Decimal(str(x)) for d, x in v.items()}
        elif k in _DECIMAL_KEYS:
            ov[k] = Decimal(str(v)) if v is not None else None
        elif k in _INT_KEYS:
            ov[k] = int(v)
        elif k in _BOOL_KEYS:
            ov[k] = bool(v)
        elif k in _STR_KEYS:
            ov[k] = str(v)
        else:
            ov[k] = v
    return ov


# ------------------------------------------------------------------ 跑批

def _run_traced(
    tag: str,
    overrides: Mapping[str, Any],
    *,
    start: _date | None = None,
    end: _date | None = None,
    price_model: Any = None,
    registry_root: Path | None,
    evidence_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """一次带插桩的回测执行：tracer 全程包裹，executed_calls 注入证据。"""
    tracer = CallTracer()
    for mod, name in _TRACE_POINTS:
        tracer.wrap(mod, name)
    t0 = time.time()
    try:
        out = rdb.run_dividend_backtest_2015_2024(
            data_path=DATA_PATH,
            start_date=start,
            end_date=end,
            registry_root=registry_root,
            strategy_overrides=dict(overrides),
            price_model=price_model,
            evidence_extra=evidence_extra,
            executed_calls=tracer.calls,
        )
    finally:
        tracer.restore()
    elapsed = time.time() - t0
    rep = out["report"]
    print(f"[{tag}] run_id={out['run_id']} 用时 {elapsed/60:.1f}min "
          f"total_return={rep.total_return} round_trips={rep.round_trips} "
          f"trading_days={rep.trading_days} executed_calls={len(tracer.calls)}")
    return {
        "run_id": out["run_id"],
        "report": rep,
        "elapsed_min": elapsed / 60,
        "executed_calls": sorted(tracer.calls),
    }


def _load_stress_stats() -> tuple[float, int, int, dict[str, str]]:
    """从既有 scratch 压测产物读回 stress_return / 压测窗统计（补跑 baseline 时用）。"""
    slip_dir = SCRATCH_ROOT / "slip50" / "runs"
    s15_dir = SCRATCH_ROOT / "stress2015" / "runs"
    slip_js = sorted(slip_dir.glob("*.json"))
    s15_js = sorted(s15_dir.glob("*.json"))
    if not slip_js or not s15_js:
        raise FileNotFoundError(
            f"--baseline-only 需要既有压测产物: {slip_dir} / {s15_dir}")
    slip = json.loads(slip_js[-1].read_text(encoding="utf-8"))
    s15 = json.loads(s15_js[-1].read_text(encoding="utf-8"))
    stress_return = float(slip["metrics"]["total_return"])
    rt = int(s15["metrics"]["round_trips"])
    days = int(s15["metrics"]["trading_days"])
    ids = {"slip50": slip["run_id"], "stress2015": s15["run_id"]}
    print(f"[produce] 读回压测统计: slip={ids['slip50']} "
          f"stress_return={stress_return:.6f} 2015窗={ids['stress2015']} rt={rt} days={days}")
    return stress_return, rt, days, ids


def _promote_baselines(scratch: Path, run_ids: list[str]) -> list[str]:
    """把本批 scratch baseline 产物升格进权威 ``experiments/runs/``。

    只升格 ``run_ids`` 指定的产物（scratch 目录可能残留上一批文件——
    glob 全量会撞已升格产物导致中断漏升格）。index 行只追加本批 run_id 对应的。
    """
    import shutil
    auth_runs = ROOT / "experiments" / "runs"
    promoted: list[str] = []
    src_runs = scratch / "runs"
    index_lines = [
        line for line in
        (src_runs / "index.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line)["run_id"] in set(run_ids)]
    for rid in run_ids:
        js = src_runs / f"{rid}.json"
        dst = auth_runs / js.name
        if dst.exists():
            raise FileExistsError(f"升格冲突：{js.name} 已存在于权威 runs/")
        shutil.copy2(js, dst)
        promoted.append(js.name)
        print(f"[promote] {js.name} → experiments/runs/")
    with (auth_runs / "index.jsonl").open("a", encoding="utf-8") as fh:
        for line in index_lines:
            fh.write(line + "\n")
    return promoted


def _slippage_price_model(mult: Decimal) -> Any:
    """滑点 ×mult 的成交价模型（只动价格侧滑点，费率六科目不动——S-3 语义）。"""
    from dataclasses import replace as _dreplace
    cfg = default_fee_config()
    cfg = _dreplace(cfg, slippage_rate=cfg.slippage_rate * mult)
    return make_price_model(config=cfg)


def main() -> int:
    ap = argparse.ArgumentParser(description="T317 证据跑批（滑点压测/2015窗/baseline×2）")
    ap.add_argument("--slip-only", action="store_true")
    ap.add_argument("--stress-only", action="store_true")
    ap.add_argument("--baseline-only", action="store_true")
    args = ap.parse_args()

    overrides = anchor_overrides()
    print(f"[produce] 锚点参数还原: {len(overrides)} 键 "
          f"(breadth_series {len(overrides.get('breadth_series', {}))} 日)")

    summary: dict[str, Any] = {"anchor": ANCHOR_PATH.name, "runs": {}}
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)

    stress_return: float | None = None
    s15_rt = s15_days = None

    # ① 滑点 +50% 全窗压测（scratch registry）
    if not args.stress_only and not args.baseline_only:
        slip = _run_traced(
            "slip50", overrides,
            price_model=_slippage_price_model(Decimal("1.5")),
            registry_root=SCRATCH_ROOT / "slip50")
        stress_return = float(slip["report"].total_return)
        summary["runs"]["slip50"] = {
            "run_id": slip["run_id"], "stress_return": stress_return,
            "elapsed_min": round(slip["elapsed_min"], 1)}

    # ② 2015-2016 压测窗（scratch registry）
    if not args.slip_only and not args.baseline_only:
        s15 = _run_traced(
            "stress-2015-2016", overrides,
            start=STRESS_WINDOW[0], end=STRESS_WINDOW[1],
            registry_root=SCRATCH_ROOT / "stress2015")
        s15_rt = s15["report"].round_trips
        s15_days = s15["report"].trading_days
        summary["runs"]["stress2015"] = {
            "run_id": s15["run_id"], "round_trips": s15_rt,
            "trading_days": s15_days,
            "total_return": str(s15["report"].total_return),
            "window": [STRESS_WINDOW[0].isoformat(), STRESS_WINDOW[1].isoformat()],
            "elapsed_min": round(s15["elapsed_min"], 1)}

    # ③④ baseline×2（先跑 scratch，跑完再升格进权威 experiments/runs/）。
    # ⛔ 顺序关键：authoritative 落盘会弄脏 git status ⇒ 第二跑 code_hash 变
    #   ``head+dirty-…`` ⇒ 指纹不同 ⇒ G-REPRO-1 verified 组失败。两跑必须先
    #   在同一份干净树上完成（scratch 在 experiments/lab/ 下已 gitignore），
    #   再统一复制 artifact + 追加 index 行——产物内容与落盘目录无关。
    if not args.slip_only and not args.stress_only:
        # --baseline-only 补跑路径：压测统计从既有 scratch 产物读回（不补跑）。
        if stress_return is None or s15_rt is None or s15_days is None:
            stress_return, s15_rt, s15_days, s15_ids = _load_stress_stats()
            summary["runs"].setdefault("slip50", {"run_id": s15_ids.get("slip50"),
                                                  "stress_return": stress_return})
            summary["runs"].setdefault("stress2015", {
                "run_id": s15_ids.get("stress2015"), "round_trips": s15_rt,
                "trading_days": s15_days})
        extras: dict[str, Any] = {
            "_evidence_notes": (
                "顶层 round_trips/trading_days 承载压测窗统计（G-STRESS-1 契约）；"
                "本产物自身指标见 metrics.*；stress_return 为滑点+50% 实跑总收益。"),
            "baseline_return": None,  # rdb 侧以 report.total_return 覆盖
            "stress_return": stress_return,
            "stress_window": {
                "start": STRESS_WINDOW[0].isoformat(),
                "end": STRESS_WINDOW[1].isoformat(),
                "round_trips": s15_rt,
                "trading_days": s15_days,
            },
            # G-STRESS-1 ctx 契约：顶层 round_trips/trading_days = 压测窗值
            "round_trips": s15_rt,
            "trading_days": s15_days,
            "stress_run_ids": {
                "slip50": summary["runs"].get("slip50", {}).get("run_id"),
                "stress2015": summary["runs"].get("stress2015", {}).get("run_id"),
            },
            "stress_registry_root": str(SCRATCH_ROOT.relative_to(ROOT)),
        }
        baseline_scratch = SCRATCH_ROOT / "baseline"
        produced_ids: list[str] = []
        for i in (1, 2):
            b = _run_traced(
                f"baseline-{i}", overrides,
                registry_root=baseline_scratch,
                evidence_extra=extras)
            produced_ids.append(b["run_id"])
            summary["runs"][f"baseline_{i}"] = {
                "run_id": b["run_id"], "elapsed_min": round(b["elapsed_min"], 1),
                "total_return": str(b["report"].total_return),
                "cagr": str(b["report"].cagr),
                "max_drawdown": str(b["report"].max_drawdown)}
        _promote_baselines(baseline_scratch, produced_ids)

    SUMMARY_PATH.write_text(
        json.dumps({k: ({kk: vv for kk, vv in v.items()} if isinstance(v, dict) else v)
                    for k, v in summary.items()},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"[produce] 摘要落盘: {SUMMARY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
