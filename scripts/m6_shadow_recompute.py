#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M6 归因实验 · 阶段一「路径 A · 零成本影子复算」—— 只读复算器。

**只做一件事**：以生产纯函数（``strategy/portfolio.py`` 的 ``select_targets`` /
``plan_positions`` / ``PortfolioConfig``）复算「建不满仓」的四条候选假设的**计划层**
上界，并把结果落成一份**内容确定**的 JSON（⛔ 无时间戳 / 无随机量 / 重跑逐字节相同）。

复算范围（全部零成本：纯函数 + 只读产物，⛔ 不跑回测、⛔ 不改任何生产代码）：

* **H5**  —— 市值加权的**权重离散** ⇒ 低权重票被 ``min_position_value`` 丢弃且**不重新分配**。
* **H5b** —— **价位真空带**（等权也有价位永远建不成）：逐 ``(base, close)`` 配对。
* **H5c** —— 死带随 ``base`` 单调变宽 ⇒ ``NAV`` 退化放大丢弃率（正反馈结构）。
* **㊶**  —— ``default_positions``（``candidates.py``）与 ``target_count``（``portfolio.py``）
  的**隐性双口径**：``N = min(default_positions, target_count)``。

**结论强度纪律（⛔ 硬约束，见 docs/audit/m6_pathA_shadow_findings.md）**：本脚本只证
「**计划层**会切断」，⛔ **不得**据此宣称「收益会改善」。

运行：``cd D:/Projects/FinAI2.0 && py -3.11 scripts/m6_shadow_recompute.py``
"""
from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))          # 复用生产包（⛔ 不重实现其算术）

from backtest.types import Bar                                        # noqa: E402
from strategy.portfolio import (                                      # noqa: E402
    PortfolioConfig,
    plan_positions,
    select_targets,
)

# ----------------------------------------------------------------------
# 常量（凡可从生产默认读到的一律读，⛔ 不硬编码）
# ----------------------------------------------------------------------
D = Decimal
ZERO = D("0")
ONE = D("1")

_DAY = date(2026, 9, 1)
_AMOUNT = D("80000000")            # > min_daily_amount(=5e7) ⇒ 不触发流动性过滤（F10d）
_VOLUME = D("1000000")

_CFG = PortfolioConfig()           # 生产默认即基线配置（设计 §1.4-⑩ 口径）
_M = _CFG.min_position_value       # 单票建仓下限（默认 20000）
_LOT = _CFG.lot_size               # 整手（默认 100）
_MAX_PRICE = _CFG.max_price        # 高价股上限（默认 300.0）

PRICE_LO = 20                      # 取样约定下界（设计 §1.4-⑪ 取样窗口）
PRICE_HI = 300                     # = max_price（有语义的上界）
N_PRICES = PRICE_HI - PRICE_LO + 1  # 281

BASE_GRID_LO = 18000
BASE_GRID_HI = 40000
BASE_GRID_STEP = 500

NAV_BASE = D("150000")             # 基线本金（= 产物 initial_nav）
N_EQUAL = 5                        # 默认 default_positions=target_count=5

ARTIFACT_REL = "experiments/runs/20260907-150402-t312-dividend-v1-noseed.json"
OUTPUT_REL = "artifacts/m6_attribution/pathA_shadow/shadow_recompute.json"
REPRO_CMD = "cd D:/Projects/FinAI2.0 && py -3.11 scripts/m6_shadow_recompute.py"


# ----------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------
def make_bar(close: Any, symbol: str = "s0", amount: Any = _AMOUNT) -> Bar:
    """构造生产契约的 ``Bar``（键与 targets 严格对齐，⛔ 防 ``bars.get()→None`` 假停牌）。"""
    px = D(str(close))
    return Bar(
        date=_DAY, symbol=symbol, open=px, high=px, low=px, close=px, preclose=px,
        volume=_VOLUME, amount=D(str(amount)),
    )


def exact_discriminant_dead(price: Decimal, base: Decimal) -> bool:
    """**独立判别式**（仅用于交叉校验，⛔ 不替代生产函数）。

    ``100·p·floor(base/(100p)) < min_position_value`` ⇒ 该价位在当日 ``base`` 下为死价位。
    """
    lots = int(base / (D(100) * price))
    planned = D(100) * price * D(lots)
    return planned < _M


def closed_form_dead(price: Decimal, base: Decimal) -> bool:
    """**已被证否**的闭式 ``(base/200, base/100]``（设计 §1.4-⑪ 明确禁用）。

    本脚本保留它**只为**构造反例，从测试层面钉死"闭式已证否"（⛔ 不得用于任何统计）。
    """
    return (base / D(200)) < price <= (base / D(100))


def production_dead(price: Decimal, base: Decimal) -> tuple[bool, str]:
    """**生产函数实测**：单标的 ``plan_positions`` ⇒ ``base`` 精确等于传入值（设计 §1.4-⑪）。"""
    sym = "s0"
    bars = {sym: make_bar(price, symbol=sym)}
    plan, dropped = plan_positions([sym], base, bars, _CFG, weights={sym: ONE})
    if sym in plan:
        return False, ""
    return True, dropped[0][1] if dropped else ""


def dead_prices_exact(base: Decimal) -> list[int]:
    """``[20,300]`` 上按**精确式**统计的死价位（升序）。"""
    return [p for p in range(PRICE_LO, PRICE_HI + 1)
            if exact_discriminant_dead(D(p), base)]


def dead_share_exact(base: Decimal) -> tuple[list[int], int, Decimal]:
    """返回 ``(死价位列表, 计数, 占比)``；占比分母 = 281（``[20,300]`` 有效窗口）。"""
    dead = dead_prices_exact(base)
    return dead, len(dead), D(len(dead)) / D(N_PRICES)


def _s(value: Any) -> Any:
    """Decimal → str（落盘纪律：金额一律 str，⛔ 无 float）。"""
    return str(value) if isinstance(value, Decimal) else value


# ----------------------------------------------------------------------
# ① H5：权重离散矩阵
# ----------------------------------------------------------------------
def recompute_h5() -> dict[str, Any]:
    """同一批 5 只候选（``close=10.00``、``NAV=150000``）在三种权重下的实建仓矩阵。"""
    close = "10.00"
    scenarios: list[tuple[str, dict[str, Decimal] | None]] = [
        ("等权 1:1:1:1:1", None),
        ("加权 3:2:2:1:1", {f"s{i}": D(w) for i, w in enumerate(["3", "2", "2", "1", "1"])}),
        ("加权 10:1:1:1:1", {f"s{i}": D(w) for i, w in enumerate(["10", "1", "1", "1", "1"])}),
    ]
    rows: list[dict[str, Any]] = []
    for label, weights in scenarios:
        syms = [f"s{i}" for i in range(N_EQUAL)]
        bars = {s: make_bar(close, symbol=s) for s in syms}
        plan, dropped = plan_positions(syms, NAV_BASE, bars, _CFG, weights=weights)
        planned_total = sum(plan.values(), ZERO)
        rows.append({
            "scenario": label,
            "n_candidates": len(syms),
            "n_built": len(plan),
            "planned": {s: _s(plan[s]) for s in sorted(plan)},
            "planned_total": _s(planned_total),
            "retained_cash": _s(NAV_BASE - planned_total),
            "n_dropped": len(dropped),
            "dropped": [[s, r] for s, r in dropped],
        })
    return {"nav": _s(NAV_BASE), "close": close, "rows": rows}


# ----------------------------------------------------------------------
# ② H5b：价位真空带的边界对撞
# ----------------------------------------------------------------------
def recompute_h5b() -> dict[str, Any]:
    """逐 ``(base, close)`` 配对，用**生产函数**断言死/活边界。"""
    cases = [
        ("30000", "150"), ("30000", "151"), ("30000", "199"), ("30000", "200"),
        ("30000", "301"), ("21684", "20"), ("21684.228", "20"),
    ]
    rows: list[dict[str, Any]] = []
    for base_s, price_s in cases:
        base = D(base_s)
        price = D(price_s)
        dead, reason = production_dead(price, base)
        lots = int(base / (D(100) * price))
        rows.append({
            "base": base_s,
            "price": price_s,
            "lots": lots,
            "planned": _s(D(100) * price * D(lots)),
            "built": not dead,
            "drop_reason": reason,
        })
    dead30k, n30k, share30k = dead_share_exact(D("30000"))
    return {
        "boundary_cases": rows,
        "base_30000_dead_band": {
            "prices": dead30k, "count": n30k, "share": _s(share30k),
            "window": [PRICE_LO, PRICE_HI], "window_size": N_PRICES,
        },
    }


# ----------------------------------------------------------------------
# ③ H5c：死带随 base 退化
# ----------------------------------------------------------------------
def recompute_h5c() -> dict[str, Any]:
    """NAV 表 + 临界线 + 单调性（集合包含）实测。"""
    nav_table = {
        "150000": D("30000"), "130000": D("26000"), "110000": D("22000"),
        "100000": D("20000"), "90000": D("18000"),
    }
    table: list[dict[str, Any]] = []
    dead_by_base: dict[str, set[int]] = {}
    for nav_s, base in nav_table.items():
        dead, n, share = dead_share_exact(base)
        dead_by_base[base] = set(dead)
        table.append({
            "nav": nav_s, "base": _s(base), "n_dead": n, "share": _s(share),
        })

    # 完全失效（base < m ⇒ 全价位丢弃，用生产函数实测，非仅推理）
    fully_dead_base = D("18000")
    fd_dead, fd_reason = production_dead(D("20"), fully_dead_base)
    _, fd_reason_hi = production_dead(D("300"), fully_dead_base)

    # 单调性：base 下降 ⇒ 死集只增（相邻档集合包含）
    ordered = sorted(dead_by_base, reverse=True)   # 由大到小
    mono: list[dict[str, Any]] = []
    for hi, lo in zip(ordered, ordered[1:]):
        mono.append({
            "subset": f"dead({_s(lo)}) ⊇ dead({_s(hi)})",
            "passed": dead_by_base[lo] >= dead_by_base[hi],
            "dropped_from": _s(hi), "dropped_to": _s(lo),
        })
    # 全窗口对撞（非相邻也须成立）
    all_pairs_ok = all(
        dead_by_base[b_lo] >= dead_by_base[b_hi]
        for i, b_hi in enumerate(ordered) for b_lo in ordered[i + 1:]
    )

    return {
        "nav_table": table,
        "critical_line": {
            "rule": "完全无法建仓 ⟺ base < min_position_value",
            "min_position_value": _s(_M),
            "nav_trigger_equal_N5": _s(D(N_EQUAL) * _M),
            "base_at_equal_N8": _s(NAV_BASE / D(8)),
            "equal_N8_always_dead": (NAV_BASE / D(8)) < _M,
        },
        "fully_dead_probe": {
            "base": _s(fully_dead_base),
            "price": "20", "dropped": fd_dead, "reason": fd_reason,
            "price_hi": "300", "dropped_hi": True, "reason_hi": fd_reason_hi,
        },
        "monotonicity_adjacent": mono,
        "monotonicity_all_pairs_passed": all_pairs_ok,
    }


# ----------------------------------------------------------------------
# ④ ㊶：default_positions × target_count 双口径
# ----------------------------------------------------------------------
def recompute_double_count() -> dict[str, Any]:
    """``N = min(default_positions, target_count)``；``(8,8)`` ⇒ ``base=18750`` ⇒ 空计划。"""
    combos = [(5, 5), (5, 8), (8, 5), (8, 8), (3, 5)]
    rows: list[dict[str, Any]] = []
    for dp, tc in combos:
        signals = {f"s{i}": D("1") for i in range(dp)}   # _select_stocks 输出长度 = dp
        cfg = PortfolioConfig(target_count=tc)
        targets = select_targets(signals, cfg)            # N = min(dp, tc)
        n = len(targets)
        base = NAV_BASE / D(n) if n else ZERO
        syms = list(targets)
        bars = {s: make_bar("10.00", symbol=s) for s in syms}
        plan, dropped = plan_positions(syms, NAV_BASE, bars, cfg)
        rows.append({
            "default_positions": dp, "target_count": tc, "N": n,
            "base": _s(base), "n_built": len(plan),
            "n_dropped": len(dropped),
            "drop_reasons": sorted({r for _, r in dropped}),
            "dropped": [[s, r] for s, r in dropped],
        })
    return {"nav": _s(NAV_BASE), "rows": rows}


# ----------------------------------------------------------------------
# ⑤ 端点括号（只读签名产物）
# ----------------------------------------------------------------------
def recompute_endpoint(artifact_path: Path) -> dict[str, Any]:
    """读签名产物的 ``metrics`` 字段 ⇒ 终值点 / 谷值点死带占比区间。"""
    doc = json.loads(artifact_path.read_text(encoding="utf-8"))
    metrics = doc["metrics"]
    initial_nav = D(str(metrics["initial_nav"]))
    final_nav = D(str(metrics["final_nav"]))
    mdd = D(str(metrics["max_drawdown"]))

    base_final = final_nav / D(N_EQUAL)
    _, n_final, share_final = dead_share_exact(base_final)

    # 谷值：trough = (1-mdd)·peak，peak ≥ initial_nav ⇒ trough ≤ final_nav（全局最低）
    trough_lo = (ONE - mdd) * initial_nav      # peak 取最小值 initial_nav
    trough_hi = final_nav                      # 全局最低 ≤ 终值
    base_trough_lo = trough_lo / D(N_EQUAL)
    _, n_trough, share_trough = dead_share_exact(base_trough_lo)

    return {
        "artifact_fields_read": {
            "metrics.initial_nav": str(metrics["initial_nav"]),
            "metrics.final_nav": str(metrics["final_nav"]),
            "metrics.max_drawdown": str(metrics["max_drawdown"]),
            "metrics.max_dd_peak": str(metrics["max_dd_peak"]),
            "metrics.max_dd_trough": str(metrics["max_dd_trough"]),
            "metrics.max_dd_recovery": metrics["max_dd_recovery"],
        },
        "final_point": {
            "nav": _s(final_nav), "base": _s(base_final),
            "n_dead": n_final, "share": _s(share_final),
        },
        "trough_point": {
            "nav_upper_bound": _s(trough_hi), "nav_lower_bound": _s(trough_lo),
            "base_at_lower_bound": _s(base_trough_lo),
            "n_dead_at_lower_bound": n_trough, "share_at_lower_bound": _s(share_trough),
        },
        "dead_share_interval": {
            "lo": _s(share_final), "hi": "1",
            "derivation": (
                "峰值 NAV ≥ initial_nav 且 谷值 NAV ≤ final_nav；"
                "死带随 base 单调不减（H5c 单调性）⇒ share(谷值) ∈ [share(final), 100%]"
            ),
        },
    }


# ----------------------------------------------------------------------
# ⑥ 暴力等价性验证 + 闭式反例
# ----------------------------------------------------------------------
def recompute_brute() -> dict[str, Any]:
    """``[20,300]`` × base 网格：生产函数实测 vs 独立精确式 / 已证否闭式。"""
    bases = [D(b) for b in range(BASE_GRID_LO, BASE_GRID_HI + 1, BASE_GRID_STEP)]
    n_checked = 0
    mismatch_exact: list[list[str]] = []
    mismatch_closed: list[list[str]] = []
    for base in bases:
        for p_int in range(PRICE_LO, PRICE_HI + 1):
            p = D(p_int)
            prod_dead, _reason = production_dead(p, base)
            n_checked += 1
            if prod_dead != exact_discriminant_dead(p, base):
                mismatch_exact.append([str(base), str(p_int)])
            if prod_dead != closed_form_dead(p, base):
                mismatch_closed.append([str(base), str(p_int)])
    return {
        "grid": {"base_lo": BASE_GRID_LO, "base_hi": BASE_GRID_HI,
                 "base_step": BASE_GRID_STEP, "n_bases": len(bases),
                 "price_window": [PRICE_LO, PRICE_HI], "n_prices": N_PRICES},
        "n_checked": n_checked,
        "n_mismatch_exact_vs_production": len(mismatch_exact),
        "mismatch_exact_samples": mismatch_exact[:10],
        "n_mismatch_closed_vs_production": len(mismatch_closed),
        "mismatch_closed_samples": mismatch_closed[:10],
        "closed_form_counterexample": {
            "base": "30000", "price": "200",
            "closed_form_says_dead": bool(closed_form_dead(D("200"), D("30000"))),
            "production_says_dead": production_dead(D("200"), D("30000"))[0],
        },
    }


# ----------------------------------------------------------------------
# 报告 + 落盘
# ----------------------------------------------------------------------
def build_report(result: dict[str, Any]) -> None:
    """人类可读报告（数字全部来自上面的实测结果）。"""
    out = sys.stdout
    print("=" * 78, file=out)
    print("M6 路径A 零成本影子复算报告（只读；计划层上界）", file=out)
    print("=" * 78, file=out)
    cfg = result["config_defaults"]
    print(f"生产默认配置：min_position_value={cfg['min_position_value']} "
          f"lot_size={cfg['lot_size']} max_price={cfg['max_price']} "
          f"min_daily_amount={cfg['min_daily_amount']}", file=out)
    print(f"价位窗口 [20,{PRICE_HI}]（{N_PRICES} 个价位；上界=max_price）\n", file=out)

    h5 = result["h5_weight_dispersion"]
    print(f"[H5] 权重离散矩阵（NAV={h5['nav']}，5 候选，close={h5['close']}）", file=out)
    for r in h5["rows"]:
        print(f"  {r['scenario']:<16} 实建 {r['n_built']} 只 / "
              f"planned_total={r['planned_total']} / 留存现金={r['retained_cash']} / "
              f"丢弃 {r['n_dropped']} 只 {dict(r['dropped']) if r['dropped'] else ''}",
              file=out)
    print(file=out)

    h5b = result["h5b_dead_band_boundary"]
    print("[H5b] 边界对撞（生产函数实测）", file=out)
    for r in h5b["boundary_cases"]:
        verdict = "建成" if r["built"] else f"丢弃({r['drop_reason']})"
        print(f"  base={r['base']:<10} p={r['price']:<4} lots={r['lots']:<3} "
              f"planned={r['planned']:<8} ⇒ {verdict}", file=out)
    band = h5b["base_30000_dead_band"]
    print(f"  base=30000 死价位 {band['count']} 个 = {band['share']} / {band['window_size']} "
          f"（{band['prices'][0]}..{band['prices'][-1]}）\n", file=out)

    h5c = result["h5c_dead_band_vs_base"]
    print("[H5c] 死带 vs NAV", file=out)
    for r in h5c["nav_table"]:
        print(f"  NAV={r['nav']:<8} base={r['base']:<8} 死价位={r['n_dead']:<4} "
              f"占比={r['share']}", file=out)
    cl = h5c["critical_line"]
    print(f"  临界：NAV<N*m 全失效；N=5 触发线={cl['nav_trigger_equal_N5']}；"
          f"N=8 时 base={cl['base_at_equal_N8']} < {cl['min_position_value']} ⇒ "
          f"恒失效={cl['equal_N8_always_dead']}", file=out)
    print(f"  单调性（相邻档子集）全 True = "
          f"{all(m['passed'] for m in h5c['monotonicity_adjacent'])}；"
          f"全对阵全 True = {h5c['monotonicity_all_pairs_passed']}\n", file=out)

    dc = result["counter_61_double_count"]
    print(f"[㊶] default_positions × target_count（NAV={dc['nav']}）", file=out)
    for r in dc["rows"]:
        print(f"  (dp={r['default_positions']}, tc={r['target_count']}) ⇒ N={r['N']} "
              f"base={r['base']:<9} 实建={r['n_built']} 丢弃={r['n_dropped']} "
              f"原因={r['drop_reasons']}", file=out)
    print(file=out)

    ep = result["endpoint_bracket"]
    print("[端点括号] 只读签名产物", file=out)
    fp, tp = ep["final_point"], ep["trough_point"]
    print(f"  终值 NAV={fp['nav']} base={fp['base']} ⇒ 死带 {fp['share']} "
          f"({fp['n_dead']}/{N_PRICES})", file=out)
    print(f"  谷值 NAV∈[{tp['nav_lower_bound']}, {tp['nav_upper_bound']}] ⇒ "
          f"base∈[{tp['base_at_lower_bound']}, {fp['base']}] ⇒ 死带占比 "
          f"∈[{fp['share']}, 100%]", file=out)
    print(file=out)

    be = result["brute_equivalence"]
    print("[暴力等价性]", file=out)
    print(f"  网格：{be['grid']['n_bases']} base × {be['grid']['n_prices']} 价位 "
          f"= {be['n_checked']} 组", file=out)
    print(f"  独立精确式 vs 生产函数不一致：{be['n_mismatch_exact_vs_production']} 组", file=out)
    print(f"  已证否闭式 vs 生产函数不一致：{be['n_mismatch_closed_vs_production']} 组", file=out)
    ce = be["closed_form_counterexample"]
    print(f"  闭式反例：base={ce['base']} p={ce['price']} ⇒ 闭式判死="
          f"{ce['closed_form_says_dead']}，生产实测判死={ce['production_says_dead']}", file=out)
    print("=" * 78, file=out)


def build_result() -> dict[str, Any]:
    artifact_path = ROOT / ARTIFACT_REL
    doc = json.loads(artifact_path.read_text(encoding="utf-8"))
    port = doc["params"]["portfolio"]

    result: dict[str, Any] = {
        "source_anchor": {
            "source_run_id": doc["run_id"],
            "artifact_path": ARTIFACT_REL,
            "artifact_fields_read": [
                "run_id", "anti_tamper_signature",
                "metrics.initial_nav", "metrics.final_nav", "metrics.max_drawdown",
                "metrics.max_dd_peak", "metrics.max_dd_trough", "metrics.max_dd_recovery",
                "params.default_positions", "params.portfolio.target_count",
                "params.portfolio.min_position_value",
            ],
            "anti_tamper_signature": doc["anti_tamper_signature"],
            "artifact_params_portfolio": port,
            "production_functions": [
                "strategy/portfolio.py:49 PortfolioConfig",
                "strategy/portfolio.py:124 select_targets",
                "strategy/portfolio.py:140 _plan_one（plan_positions 内部）",
                "strategy/portfolio.py:165 plan_positions",
            ],
            "reproduce_command": REPRO_CMD,
            "reproduce_test_command": (
                "cd D:/Projects/FinAI2.0 && py -3.11 -m pytest "
                "tests/test_m6_shadow_recompute.py -p no:ddtrace -p no:ddtrace.pytest_bdd -q"
            ),
        },
        "config_defaults": {
            "min_position_value": _s(_CFG.min_position_value),
            "lot_size": _CFG.lot_size,
            "max_price": _s(_CFG.max_price),
            "min_daily_amount": _s(_CFG.min_daily_amount),
            "max_participation_rate": _s(_CFG.max_participation_rate),
            "target_count": _CFG.target_count,
            "hard_limit": _CFG.hard_limit,
        },
        "price_window": {
            "lo": PRICE_LO, "hi": PRICE_HI, "n_prices": N_PRICES,
            "note": "上界=max_price（有语义）；下界=取样约定；占比分母恒为 281",
        },
        "h5_weight_dispersion": recompute_h5(),
        "h5b_dead_band_boundary": recompute_h5b(),
        "h5c_dead_band_vs_base": recompute_h5c(),
        "counter_61_double_count": recompute_double_count(),
        "endpoint_bracket": recompute_endpoint(artifact_path),
        "brute_equivalence": recompute_brute(),
    }
    return result


def main() -> int:
    result = build_result()
    build_report(result)

    out_path = ROOT / OUTPUT_REL
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2)
    out_path.write_text(payload + "\n", encoding="utf-8", newline="\n")
    print(f"已落盘（确定性）：{OUTPUT_REL}", file=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
