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
import hashlib
import json
import sys
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import date as _date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 可复现性（E5 教训）：stock_basic 在线拉取不稳定（实测两跑 2595 vs 986
# ⇒ 池子不同实验不可比）。默认固定缓存文件，外部可用同名环境变量覆盖。
import os as _os
_os.environ.setdefault(
    "FNAI_STOCK_BASIC_CACHE", str(ROOT / "data/stock_basic_cache.parquet"))

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
    # 空仓现金收益（e6 防御资产近似，账户级计息）
    "cash_yield_annual": Decimal,
    "cash_yield_series": str,      # e6b：GC001 日度利率 parquet 路径
    "breadth_demote_liquidate": lambda v: v.lower() in ("1", "true", "yes", "on"),
    "breadth_weight_mode": str,
    "attack_instrument": str,
    # 回测区间覆盖（非 DividendConfig 字段，run_experiment 单独提取传给 runner）
    "backtest_start": lambda v: _date.fromisoformat(v),
    "backtest_end": lambda v: _date.fromisoformat(v),
    # 晋级门禁 G-3（成本/成交约束复核）：本金端点与费率敏感性
    "initial_capital": Decimal,     # 初始本金（默认 150000）
    "fee_multiplier": Decimal,      # 费率全科目缩放（默认 1；2=佣金/印花/过户/经手/证管 ×2）
    # C3：年度池 universe_provider（pool_yearly.parquet 路径；
    # provider(day)=pool[year(day)]∩alive(day)，与 --data-path data/c3_universe 配套）
    "universe_yearly_pool": str,
    "low_vol_keep_pct": Decimal,      # D2 低波翼：dv 合格候选按 trailing-250d vol 升序保留前 pct（None=不启用）
    "dv_skip_top": int,               # e16 剔尾：dv 降序排序后跳过前 N 名（实证逆向选择带）
    "max_dividend_yield": Decimal,    # e16 扰动臂：股息率上限（剔除极端高息尾部）
    "weight_mode": str,               # e17 权重形态：market_cap(默认)/equal/dividend_yield
    "min_positions": int,             # e17 持仓数臂：DividendConfig 下界（配合 default_positions 使用）
    "max_positions": int,             # e17 持仓数臂：DividendConfig 上界
    # e15：ETF 攻击资产的组合层流动性下限覆盖（嵌套 PortfolioConfig 字段——
    # 二级成交额下限对 ETF 不适用：申赎机制兜底，真实约束是参与率上限；
    # ⛔ 只用于 placebo 臂，选股池 hygiene 下限语义不变）
    "portfolio_min_daily_amount": Decimal,
    # e17 持仓数臂：组合层目标数覆盖（select_targets 截断数——硬编码默认 5，
    # pos>5 臂不覆盖此键会被截回 5 ⇒ 嵌套 PortfolioConfig 字段，同上行机制）
    "portfolio_target_count": int,
    # e18 持仓宽度扩展：pos>8 须同时放宽 PortfolioConfig 三约束
    # （max_positions/hard_limit）+ pos×本金跌破 min_position_value 时须降地板
    "portfolio_max_positions": int,
    "portfolio_hard_limit": int,
    "portfolio_min_position_value": Decimal,
    # e19 D7 拥挤度熔断：attack 日 crowd_pct>threshold ⇒ 目标仓位 ×cap
    "use_crowding_breaker": lambda v: v.lower() in ("1", "true", "yes", "on"),
    "crowding_threshold": Decimal,
    "crowding_cap": Decimal,
    # _crowd_file：拥挤度序列 parquet 路径（同 _breadth_file 约定——
    # 不入 DividendConfig，在 runner 层加载为 crowding_series）
    "_crowd_file": str,
}

#: 非策略配置字段——传给 ``run_dividend_backtest_*`` 或本 runner 的运行级参数。
_RUN_LEVEL_KEYS = (
    "backtest_start", "backtest_end", "initial_capital", "fee_multiplier",
    "universe_yearly_pool")


def _load_breadth_series(path: Path) -> dict:
    """加载宽度序列 parquet -> {date_str: Decimal}；缺文件 Fail-Closed 报错。"""
    import pandas as pd
    if not path.exists():
        raise SystemExit(f"宽度序列文件缺失: {path}（⛔ Fail-Closed：无宽度数据不得开启宽度择时）")
    df = pd.read_parquet(path)
    # 日期键只保留 YYYY-MM-DD，与策略 day.isoformat() 查表键对齐（⛔ 禁带时间部分）
    return {str(d)[:10]: Decimal(str(b)) for d, b in zip(df["date"], df["breadth20"])}


def _load_crowding_series(path: Path) -> dict:
    """加载拥挤度分位序列 parquet -> {date_str: Decimal}；缺文件 Fail-Closed。"""
    import pandas as pd
    if not path.exists():
        raise SystemExit(f"拥挤度序列文件缺失: {path}（⛔ Fail-Closed：无拥挤度数据不得开启熔断）")
    df = pd.read_parquet(path)
    return {str(d)[:10]: Decimal(str(p)) for d, p in zip(df["date"], df["crowd_pct"])}


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

    # 运行级参数不进 DividendConfig——``dataclasses.replace`` 只认字段名。
    bt_start = overrides.pop("backtest_start", None)
    bt_end = overrides.pop("backtest_end", None)
    initial_capital = Decimal(overrides.pop("initial_capital", "150000"))
    fee_mult = Decimal(overrides.pop("fee_multiplier", "1"))
    yearly_pool = overrides.pop("universe_yearly_pool", None)
    portfolio_min_amt = overrides.pop("portfolio_min_daily_amount", None)
    portfolio_target_cnt = overrides.pop("portfolio_target_count", None)
    portfolio_max_pos = overrides.pop("portfolio_max_positions", None)
    portfolio_hard_lim = overrides.pop("portfolio_hard_limit", None)
    portfolio_min_pv = overrides.pop("portfolio_min_position_value", None)
    crowd_file = overrides.pop("_crowd_file", None)
    run_params = {
        "backtest_start": str(bt_start) if bt_start else None,
        "backtest_end": str(bt_end) if bt_end else None,
        "initial_capital": str(initial_capital),
        "fee_multiplier": str(fee_mult),
        "universe_yearly_pool": yearly_pool,
        "portfolio_min_daily_amount": (str(portfolio_min_amt)
                                       if portfolio_min_amt is not None else None),
        "portfolio_target_count": (str(portfolio_target_cnt)
                                   if portfolio_target_cnt is not None else None),
        "portfolio_max_positions": (str(portfolio_max_pos)
                                    if portfolio_max_pos is not None else None),
        "portfolio_hard_limit": (str(portfolio_hard_lim)
                                 if portfolio_hard_lim is not None else None),
        "portfolio_min_position_value": (str(portfolio_min_pv)
                                         if portfolio_min_pv is not None else None),
        "crowd_file": crowd_file,
    }

    orig_config_init = rdb.DividendConfig
    orig_make_fee_model = rdb.make_fee_model

    # G-3 费率敏感性：全科目费率 ×fee_mult（佣金/印花/过户/经手/证管；
    # 滑点在价格模型侧不在此重复计）。与 DividendConfig 同款补丁手法，
    # 仅本进程生效，权威脚本与默认费率装配不变。
    if fee_mult != 1:
        from backtest.fees import FeeSchedule, default_fee_config

        def _scaled_schedules(schedules):
            return tuple(
                FeeSchedule(effective_from=s.effective_from,
                            value=s.value * fee_mult)
                for s in schedules)

        def _make_fee_model_scaled(config=None):
            cfg = config if config is not None else default_fee_config()
            cfg = replace(
                cfg,
                commission_rate=cfg.commission_rate * fee_mult,
                min_commission=cfg.min_commission * fee_mult,
                stamp_tax_schedules=_scaled_schedules(cfg.stamp_tax_schedules),
                transfer_fee_schedules=_scaled_schedules(
                    cfg.transfer_fee_schedules),
                handling_fee_schedules_cn=_scaled_schedules(
                    cfg.handling_fee_schedules_cn),
                handling_fee_schedules_bj=_scaled_schedules(
                    cfg.handling_fee_schedules_bj),
                management_fee_rate=cfg.management_fee_rate * fee_mult,
            )
            return orig_make_fee_model(cfg)

        rdb.make_fee_model = _make_fee_model_scaled  # type: ignore[assignment]

    # 宽度择时开启时：自动关 MA200、注入宽度序列（互斥纪律由配置侧校验）
    # 宽度文件路径由环境变量 BREADTH_FILE 指定（⛔ 不进 --set，避免污染配置校验）
    import os
    if overrides.get("use_breadth_timing"):
        overrides.setdefault("use_ma200_timing", False)
        breadth_path = Path(os.environ.get(
            "BREADTH_FILE", str(LAB_ROOT / "market-breadth-a" / "breadth20_daily.parquet")))
        overrides["breadth_series"] = _load_breadth_series(breadth_path)

    # e19 D7：拥挤度熔断开启时注入 roll3y 分位序列——路径优先级
    # --set _crowd_file > CROWD_FILE 环境变量 > 默认 a1 口径文件
    if overrides.get("use_crowding_breaker"):
        crowd_path = Path(crowd_file or os.environ.get(
            "CROWD_FILE",
            str(ROOT / "data" / "macro" / "crowding_roll3y_daily.parquet")))
        overrides["crowding_series"] = _load_crowding_series(crowd_path)

    def _patched_config(**kwargs):
        cfg = orig_config_init(**kwargs)
        if overrides:
            cfg = replace(cfg, **overrides)
        # PortfolioConfig 嵌套覆盖须单批 replace——逐字段 replace 会让
        # __post_init__ 在过渡态上校验（如 target_count=10 撞上
        # 默认 max_positions=8 直接炸），一次性构造才看到最终组合。
        pf_overrides: dict = {}
        if portfolio_min_amt is not None:
            pf_overrides["min_daily_amount"] = portfolio_min_amt
        if portfolio_target_cnt is not None:
            pf_overrides["target_count"] = portfolio_target_cnt
        if portfolio_max_pos is not None:
            pf_overrides["max_positions"] = portfolio_max_pos
        if portfolio_hard_lim is not None:
            pf_overrides["hard_limit"] = portfolio_hard_lim
        if portfolio_min_pv is not None:
            pf_overrides["min_position_value"] = portfolio_min_pv
        if pf_overrides:
            cfg = replace(cfg, portfolio=replace(cfg.portfolio, **pf_overrides))
        return cfg

    # 覆盖配置构造（仅本进程生效，权威脚本的 import 引用不变更）
    rdb.DividendConfig = _patched_config  # type: ignore[misc]

    # C3 年度池 provider（⛔ 不传入则走默认 alive_universe 全量池）
    universe_provider = None
    if yearly_pool:
        import pandas as pd
        from scripts.lab.c3_data_plane import make_yearly_pool_provider
        sb_cache = Path(os.environ["FNAI_STOCK_BASIC_CACHE"])
        stock_basic = pd.read_parquet(sb_cache)
        universe_provider = make_yearly_pool_provider(
            Path(yearly_pool), stock_basic)
        print(f"[lab] C3 年度池 provider ← {yearly_pool}")

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    try:
        result = rdb.run_dividend_backtest_2015_2024(
            data_path=data_path,
            initial_capital=initial_capital,
            risk_free_annual=Decimal("0.025"),
            enable_gates=True,
            start_date=bt_start,
            end_date=bt_end,
            registry_root=lab_dir,
            universe_provider=universe_provider,
        )
    finally:
        rdb.DividendConfig = orig_config_init  # type: ignore[misc]
        rdb.make_fee_model = orig_make_fee_model  # type: ignore[assignment]
    elapsed = time.time() - t0

    report = result["report"]
    summary = {
        "experiment": name,
        "started": started,
        "elapsed_min": round(elapsed / 60, 1),
        "overrides": {k: _compact_override(v) for k, v in overrides.items()},
        "run_params": run_params,
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


def _compact_override(v: object) -> str:
    """序列化 override 值进 summary——大型序列映射（breadth_series /
    crowding_series 等注入的数据面输入）折叠为 <compacted:len=N sha256=..>
    摘要（原始序列的指纹已含在 data_hash，逐值展开只是噪音）。格式与
    9954d51 瘦身改写的存量行保持一致。"""
    if isinstance(v, Mapping):
        digest = hashlib.sha256(
            repr(sorted(v.items(), key=lambda kv: str(kv[0]))).encode()
        ).hexdigest()[:12]
        return f"<compacted:len={len(v)} sha256={digest}>"
    return str(v)


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
