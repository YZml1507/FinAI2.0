#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/audit_evidence_integrity.py — 证据真实性与防伪审计工具。

本脚本对 FinAI2.0 系统执行铁证审计，自动化检测以下 4 类伪造/假象/缺陷：
1. 【死代码与伪集成检测】：声明集成的关键模块（如红利税）是否在主执行链路被真实调用。
2. 【数据物理量纲与代用品检测】：检测 Parquet 中的 market_cap 是否被成交额 amount 冒充。
3. 【未来函数与前视偏差检测】：检测 dividend_yield 是否在全年保持恒定常数（全年分红未来均值泄露）。
4. 【策略宣称与实现一致性检测】：检测宣称的“市值加权”是否在组合管理层被等权除法（total_nav / N）丢弃。
5. 【回测产物非零与逻辑对账】：检测最新实验 runs/*.json 中的费用明细是否与声明相符。
"""
from __future__ import annotations

import json
import sys
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtest.constants import FeeItem


def audit_dividend_tax_call_chain() -> dict:
    """审计 1：红利税调用链审计。"""
    backtest_files = list((REPO_ROOT / "backtest").glob("*.py"))
    callers = []
    for py_file in backtest_files:
        if py_file.name == "dividend_tax.py":
            continue
        content = py_file.read_text(encoding="utf-8")
        if "compute_dividend_tax" in content:
            callers.append(str(py_file.relative_to(REPO_ROOT)))

    runner_content = (REPO_ROOT / "scripts" / "run_dividend_backtest.py").read_text(encoding="utf-8")
    if "compute_dividend_tax" in runner_content:
        callers.append("scripts/run_dividend_backtest.py")

    return {
        "check": "红利税 compute_dividend_tax 生产调用链路",
        "declared": "文档宣称已集成红利税并检查非零",
        "production_callers": callers,
        "is_integrated": len(callers) > 0,
        "verdict": "FAIL - 确认为死代码，主执行链路从未调用" if not callers else "PASS",
    }


def audit_market_cap_and_lookahead() -> dict:
    """审计 2 & 3：Parquet 数据字段量纲（成交额冒充市值）与未来函数泄露。"""
    div_dir = REPO_ROOT / "data" / "dividend_stocks"
    if not div_dir.exists():
        return {"check": "数据层字段审计", "status": "SKIP - data/dividend_stocks 不存在"}

    findings = []
    for sym in div_dir.iterdir():
        if not sym.is_dir() or not sym.name.startswith(("sh.", "sz.")) or sym.name == "sh.000300":
            continue
        parquet_2023 = sym / "2023.parquet"
        if parquet_2023.exists():
            df = pd.read_parquet(parquet_2023)
            if "market_cap" in df.columns and "amount" in df.columns:
                mcap = df["market_cap"].iloc[0]
                avg_amt = df["amount"].mean()
                is_amount_substitute = abs(mcap - avg_amt) < 1.0
                yield_unique = df["dividend_yield"].dropna().nunique()
                findings.append({
                    "symbol": sym.name,
                    "market_cap_sample": float(mcap),
                    "amount_mean": float(avg_amt),
                    "is_amount_substitute": is_amount_substitute,
                    "yield_unique_values_in_year": yield_unique,
                    "is_lookahead_constant": (yield_unique == 1),
                })
            if len(findings) >= 3:
                break

    all_amount_sub = all(f["is_amount_substitute"] for f in findings) if findings else False
    all_lookahead = all(f["is_lookahead_constant"] for f in findings) if findings else False

    return {
        "check": "数据层字段物理量纲与前视偏差",
        "samples": findings,
        "verdict_amount_as_mcap": "FAIL - market_cap 实为日均 amount 成交额（数千万元级而非百亿级）" if all_amount_sub else "PASS",
        "verdict_lookahead": "FAIL - dividend_yield 全年为单一日历均值常数，存在前视偏差（未来函数）" if all_lookahead else "PASS",
    }


def audit_weighting_implementation() -> dict:
    """审计 2：市值加权 vs 等权分配实现一致性。"""
    portfolio_py = (REPO_ROOT / "strategy" / "portfolio.py").read_text(encoding="utf-8")
    candidates_py = (REPO_ROOT / "strategy" / "candidates.py").read_text(encoding="utf-8")

    supports_weights = "weights: Mapping[str, Decimal] | None" in portfolio_py and "target_weights[symbol]" in portfolio_py
    strategy_passes_weights = "weights=scores" in candidates_py

    is_weighted = supports_weights and strategy_passes_weights

    return {
        "check": "市值加权宣传 vs 底层组合逻辑",
        "declared": "DividendStrategy 宣称自由流通市值加权",
        "supports_weights_in_portfolio": supports_weights,
        "strategy_passes_weights": strategy_passes_weights,
        "verdict": "PASS" if is_weighted else "FAIL - 未传入权重或 portfolio.py 不支持权重",
    }


def audit_recent_run_results() -> dict:
    """审计 5：回测产物 JSON 费用对账。"""
    runs_dir = REPO_ROOT / "experiments" / "runs"
    if not runs_dir.exists():
        return {"check": "回测落盘 JSON 审计", "status": "SKIP - experiments/runs 不存在"}

    json_files = sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not json_files:
        return {"check": "回测落盘 JSON 审计", "status": "SKIP - 无 JSON 记录"}

    latest = json_files[0]
    with open(latest, encoding="utf-8") as f:
        data = json.load(f)

    metrics = data.get("metrics", {})
    total_ret = metrics.get("total_return", None)
    cagr = metrics.get("cagr", None)
    fees_sum = metrics.get("fees_sum", None)
    fees_total = metrics.get("fees_total", {})
    div_tax = fees_total.get("DIVIDEND_TAX", "0")
    has_div_tax = float(div_tax) > 0

    return {
        "check": "最新回测产物真实数值与费用明细对账",
        "run_id": latest.name,
        "cagr": f"{float(cagr)*100:.2f}%" if cagr else None,
        "total_return": f"{float(total_ret)*100:.2f}%" if total_ret else None,
        "fees_sum": f"{float(fees_sum):.2f} 元" if fees_sum else None,
        "DIVIDEND_TAX_in_metrics": f"{float(div_tax):.2f} 元",
        "is_dividend_tax_positive": has_div_tax,
        "verdict": "PASS" if has_div_tax else "FAIL - 回测落盘产物中未见真实红利税扣除记录",
    }


def main():
    print("=" * 70)
    print("FinAI2.0 证据真实性与防伪自动审计系统 (Audit Evidence Integrity)")
    print("=" * 70)

    results = [
        audit_dividend_tax_call_chain(),
        audit_weighting_implementation(),
        audit_recent_run_results(),
        audit_market_cap_and_lookahead(),
    ]

    for idx, r in enumerate(results, 1):
        print(f"\n[{idx}] {r['check']}")
        for k, v in r.items():
            if k == "check":
                continue
            if isinstance(v, list) and v and isinstance(v[0], dict):
                print(f"  {k}:")
                for sub in v:
                    print(f"    - {sub}")
            else:
                print(f"  {k}: {v}")

    print("\n" + "=" * 70)
    print("审计完成：证据采集完毕。")
    print("=" * 70)


if __name__ == "__main__":
    main()
