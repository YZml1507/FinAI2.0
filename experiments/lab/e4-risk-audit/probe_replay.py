"""E4 风控一致性受控重放（探针，非基线实验）。

目的：在真实数据/真实引擎上回答「冰点清仓卖出触发时，PEAD 持仓是否在场、
是否同样被卖出」。等价性锚点：E4 同款 overrides + 同一宽度 parquet + 同一
signal_layers 装载（manifest hash 对照 E4 run 记录 data_version 后缀）。

⛔ 定位：行为探针，report-only；不改变策略源码、不产出基线指标。
"""
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
import sys as _sys
_sys.path.insert(0, str(ROOT))

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
    manifest_ref = {"hash": None, "log": []}

    def _patched_config(**kwargs):
        cfg = orig_config_init(**kwargs)
        if overrides:
            cfg = replace(cfg, **overrides)
        return cfg

    # ---- 插桩：包装 DividendStrategy.on_bar / _submit，记录卖出意图上下文 ----
    from strategy import candidates as cand

    orig_on_bar = cand.DividendStrategy.on_bar
    orig_submit = cand.DividendStrategy._submit
    events = manifest_ref["log"]

    def _spy_submit(self, broker, intents, day):
        for it in intents:
            events.append({
                "type": "intent", "day": day.isoformat(),
                "side": it.side.value, "symbol": it.symbol,
                "volume": int(it.volume),
                "breadth": str(self._breadth_today),
                "ice": bool(self._breadth_ice),
                "pead_held": it.symbol in self._pead_holds,
                "pead_holds": sorted(self._pead_holds),
                "rebalance_due": (
                    self._bar_count - self._last_rebalance_bar == 0),
            })
        return orig_submit(self, broker, intents, day)

    def _spy_on_bar(self, day, bars, book, broker):
        before = bool(self._breadth_ice)
        orig_on_bar(self, day, bars, book, broker)
        if before != bool(self._breadth_ice):
            events.append({
                "type": "ice_transition", "day": day.isoformat(),
                "from": before, "to": bool(self._breadth_ice),
                "breadth": str(self._breadth_today),
                "pead_holds": sorted(self._pead_holds),
            })

    cand.DividendStrategy.on_bar = _spy_on_bar
    cand.DividendStrategy._submit = _spy_submit
    rdb.DividendConfig = _patched_config
    try:
        result = rdb.run_dividend_backtest_2015_2024(
            data_path=ROOT / "data/dividend_stocks",
            initial_capital=Decimal("150000"),
            risk_free_annual=Decimal("0.025"),
            enable_gates=False,                       # 探针不进门禁
            start_date=START, end_date=END,
            registry_root=OUT / "probe-runs",
        )
    finally:
        cand.DividendStrategy.on_bar = orig_on_bar
        cand.DividendStrategy._submit = orig_submit
        rdb.DividendConfig = orig_config_init

    report = result["report"]
    (OUT / "probe_replay_log.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
        encoding="utf-8")

    # ---- 分析：冰点确认日的卖出意图是否覆盖全部持仓（含 PEAD） ----
    ice_days = [e for e in events if e["type"] == "ice_transition" and e["to"]]
    summary = {
        "probe": "e4-risk-audit/controlled replay 2019-01-02..2020-12-31",
        "run_id": result["run_id"],
        "cagr": str(report.cagr), "max_drawdown": str(report.max_drawdown),
        "ice_confirmations": len(ice_days),
        "sell_intents_total": sum(
            1 for e in events if e["type"] == "intent" and e["side"] == "SELL"),
        "buy_intents_total": sum(
            1 for e in events if e["type"] == "intent" and e["side"] == "BUY"),
        "pead_tagged_sell_intents": sum(
            1 for e in events if e["type"] == "intent"
            and e["side"] == "SELL" and e["pead_held"]),
        "pead_tagged_buy_intents": sum(
            1 for e in events if e["type"] == "intent"
            and e["side"] == "BUY" and e["pead_held"]),
        "ice_days_detail": [
            {k: e[k] for k in ("day", "breadth", "pead_holds")}
            for e in ice_days],
    }
    (OUT / "probe_replay_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
