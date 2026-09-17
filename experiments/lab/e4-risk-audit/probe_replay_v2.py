"""E4 风控一致性受控重放 v2——幽灵在册（ghost registration）实证探针。

v1 只记录了 intent 流与 pead_holds 快照；v2 追加每日 book.positions 快照，
回答三个决定性问题：

Q1  是否存在「_pead_holds 在册但从未发出 BUY intent / 从未实际持仓」的票？
Q2  幽灵在册是否挤占 pead_max_slots、并在后续调仓日通过 scores 注入挤占
    真实候选？
Q3  被 pop 的票（如 600446）如何重新出现在在册列表？

⛔ 定位：行为探针，report-only；不改变策略源码、不产出基线指标。
"""
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
import os as _os
import sys as _sys
_sys.path.insert(0, str(ROOT))
# 可复现性：固定 stock_basic 缓存
_os.environ.setdefault(
    "FNAI_STOCK_BASIC_CACHE", str(ROOT / "data/stock_basic_cache.parquet"))

from scripts.lab.run_experiment import _parse_overrides, _load_breadth_series, LAB_ROOT

OUT = ROOT / "experiments/lab/e4-risk-audit"
E4_OVERRIDES = {
    "use_breadth_timing": "True", "use_quality_veto": "True",
    "use_landmine_overlay": "True", "use_pead": "True",
    "pead_entry_mode": "rebalance", "breadth_defense_threshold": "0.25",
    "breadth_attack_threshold": "0.35", "breadth_mid_cap": "0.0",
    "breadth_ice_confirm_days": "1",
}
START, END = date(2019, 1, 2), date(2020, 12, 31)


def main() -> int:
    import scripts.run_dividend_backtest as rdb
    from dataclasses import replace

    overrides = _parse_overrides([f"{k}={v}" for k, v in E4_OVERRIDES.items()])
    overrides.setdefault("use_ma200_timing", False)
    overrides["breadth_series"] = _load_breadth_series(
        LAB_ROOT / "market-breadth-a" / "breadth20_daily.parquet")

    orig_config_init = rdb.DividendConfig
    events: list[dict] = []

    def _patched_config(**kwargs):
        cfg = orig_config_init(**kwargs)
        if overrides:
            cfg = replace(cfg, **overrides)
        return cfg

    from strategy import candidates as cand

    orig_on_bar = cand.DividendStrategy.on_bar
    orig_submit = cand.DividendStrategy._submit

    def _spy_submit(self, broker, intents, day):
        for it in intents:
            events.append({
                "type": "intent", "day": day.isoformat(),
                "side": it.side.value, "symbol": it.symbol,
                "volume": int(it.volume),
                "pead_held": it.symbol in self._pead_holds,
            })
        return orig_submit(self, broker, intents, day)

    def _spy_on_bar(self, day, bars, book, broker):
        holds_before = dict(self._pead_holds)
        orig_on_bar(self, day, bars, book, broker)
        # 每日终态快照：在册 vs 实际持仓
        pos = getattr(book, "positions", {}) or {}
        snap = {
            "type": "snapshot", "day": day.isoformat(),
            "pead_holds": {s: bc for s, bc in self._pead_holds.items()},
            "positions_vol": {s: int(p.volume) for s, p in pos.items()
                              if int(p.volume) > 0},
            "ice": bool(self._breadth_ice),
            "bar_count": self._bar_count,
        }
        # 登记/注销事件
        for s in self._pead_holds:
            if s not in holds_before:
                events.append({"type": "pead_register", "day": day.isoformat(),
                               "symbol": s, "bar_count": self._bar_count})
        for s in holds_before:
            if s not in self._pead_holds:
                events.append({"type": "pead_unregister", "day": day.isoformat(),
                               "symbol": s})
        events.append(snap)

    cand.DividendStrategy.on_bar = _spy_on_bar
    cand.DividendStrategy._submit = _spy_submit
    rdb.DividendConfig = _patched_config
    try:
        result = rdb.run_dividend_backtest_2015_2024(
            data_path=ROOT / "data/dividend_stocks",
            initial_capital=Decimal("150000"),
            risk_free_annual=Decimal("0.025"),
            enable_gates=False,
            start_date=START, end_date=END,
            registry_root=OUT / "probe-runs",
        )
    finally:
        cand.DividendStrategy.on_bar = orig_on_bar
        cand.DividendStrategy._submit = orig_submit
        rdb.DividendConfig = orig_config_init

    (OUT / "probe_replay_v2_log.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
        encoding="utf-8")

    # ---- 分析：在册期间 vs 实际持仓 / BUY intent 覆盖 ----
    snaps = [e for e in events if e["type"] == "snapshot"]
    intents = [e for e in events if e["type"] == "intent"]
    regs = [e for e in events if e["type"] == "pead_register"]

    all_reg_syms = sorted({r["symbol"] for r in regs})
    buy_days = {}
    sell_days = {}
    for it in intents:
        key = it["symbol"]
        (buy_days if it["side"] == "BUY" else sell_days).setdefault(
            key, []).append(it["day"])

    ghost_report = {}
    for s in all_reg_syms:
        # 该票所有在册日
        held_days = [sp for sp in snaps if s in sp["pead_holds"]]
        days_with_pos = [sp["day"] for sp in held_days
                         if sp["positions_vol"].get(s, 0) > 0]
        ghost_report[s] = {
            "register_events": sum(1 for r in regs if r["symbol"] == s),
            "days_on_book": len(held_days),
            "days_with_position": len(days_with_pos),
            "buy_intents": buy_days.get(s, []),
            "sell_intents": sell_days.get(s, []),
        }

    # 幽灵定义：在册期间 0 个 BUY intent 且全程无实际持仓
    ghosts = {s: r for s, r in ghost_report.items()
              if not r["buy_intents"] and r["days_with_position"] == 0}
    # 部分幽灵：有 BUY 但登记日在 BUY 之前（先上车后补票不可能——登记即应已买）
    # 以及：BUY 存在但登记后若干日仍无持仓（被拒单）

    summary = {
        "probe": "e4-risk-audit/v2 ghost-registration 2019-2020",
        "run_id": result["run_id"],
        "registered_symbols": len(all_reg_syms),
        "ghost_symbols": ghosts,
        "per_symbol": ghost_report,
        "ice_day_ghost_overlap": [
            {"day": sp["day"],
             "holds": sorted(sp["pead_holds"]),
             "ghost_holds": [s for s in sp["pead_holds"] if s in ghosts]}
            for sp in snaps if sp["ice"] and sp["pead_holds"]],
    }
    (OUT / "probe_replay_v2_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "per_symbol"},
                     ensure_ascii=False, indent=2))
    print("PER-SYMBOL:", json.dumps(ghost_report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
