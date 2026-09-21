#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""外部回测证据审计入口（E 路线第三步：29 门通用化产品化）。

对 ``ExternalEvidenceAdapter`` 产出的 ctx 跑 spike 分级为
**「直接通用 ✅ + 需适配器 🔧」的 22 门**——本仓特有的 🏠 门
（E-1/E-2 引擎探针、S-2 宽度择时、G-2 tasks.md 纪律、G-3 母库守卫、
G-DOC-1/G-REF-1 文档一致性）**显式排除**：它们审计的对象是本仓
引擎/文档/数据 schema，喂外部证据只会误扫工具包安装目录（spike §4.3）。

用法：

    # adapter 方式（推荐）：动态加载 "module:attr"（类/实例/工厂均可）
    .venv/bin/python -m scripts.gates.audit_external \
        --adapter experiments.spikes.gate_generalization.toy_adapter:ToyEvidenceAdapter

    # ctx-json 方式：吃预先 build 好的 ctx（market_rules 以 dict 承载亦可）
    .venv/bin/python -m scripts.gates.audit_external --ctx-json ctx.json

    # --report out.json 落盘 JSON 报告

判定策略（fail-closed）：复用 ``is_blocking_result``——``FAIL`` 或
``INCONCLUSIVE`` 且 severity>=CRITICAL 即阻断并**非零退出**（外部审计无
"只告警"豁免：缺证据就是没检，不得放过）。无阻断 ⇒ 退出码 0。
"""

from __future__ import annotations

import argparse
import datetime
import importlib
import json
import sys
from typing import Any, Sequence

from .adapter import build_external_context
from .base import BaseGate, GateResult, GateStatus, is_blocking_result
from .gate_a_accounting import (
    DailyCashConserveGate,
    FeeSumBalanceGate,
    GoldenRoundtripGate,
    SegmentRateScheduleGate,
)
from .gate_consistency import MaxDrawdownCeilingGate, StressValidityGate
from .gate_d_data import (
    FloatMarketCapGate,
    HighPriceLotGate,
    PitDividendYieldGate,
    RawPriceJumpGate,
    SuspensionVolumeGate,
)
from .gate_e_engine import SlippagePriceCapGate
from .gate_g_governance import AntiTamperSignatureGate, ProvenanceTriadGate
from .gate_l_liveness import (
    AllocationFidelityGate,
    FeatureLivenessGate,
    StaticAstCallGate,
)
from .gate_master_audit import GateMasterAudit
from .gate_repro import ReproducibilityGate
from .gate_s_scientific import (
    AttributionEvidenceGate,
    DividendTaxLockGate,
    DynamicSlippageAdvGate,
    TurnoverCeilingGate,
)
from .market_rules import MarketRules, resolve_market_rules

# ---------------------------------------------------------------------------
# 门集分层（与 docs/E_ROUTE_GATE_GENERALIZATION_SPIKE.md §2 分级表一一对应）
# ---------------------------------------------------------------------------

#: ✅ 直接通用层（7 门）：键是通用回测概念，喂 dict 即跑。
GENERAL_GATE_IDS: tuple[str, ...] = (
    "A-1", "A-2", "G-1", "G-4", "G-MDD-1", "G-STRESS-1", "G-REPRO-1",
)

#: 🔧 需适配器层（15 门）：概念通用，键形/语义/阈值需 adapter 翻译或 MarketRules 参数化。
ADAPTER_GATE_IDS: tuple[str, ...] = (
    "D-1", "D-2", "D-3", "D-4", "D-5",
    "L-1", "L-2", "L-3",
    "E-3",
    "A-3", "A-4",
    "S-1", "S-3", "S-4", "S-5",
)

#: 🏠 本仓特有层（7 门）：审计对象即本仓引擎探针/文档纪律/母库——显式排除并公示。
REPO_BOUND_GATE_IDS: tuple[str, ...] = (
    "E-1", "E-2", "S-2", "G-2", "G-3", "G-DOC-1", "G-REF-1",
)

EXTERNAL_GATE_IDS: frozenset[str] = frozenset(GENERAL_GATE_IDS + ADAPTER_GATE_IDS)

__all__ = [
    "GENERAL_GATE_IDS",
    "ADAPTER_GATE_IDS",
    "REPO_BOUND_GATE_IDS",
    "EXTERNAL_GATE_IDS",
    "get_external_gates",
    "audit_external_context",
    "audit_adapter",
]


def get_external_gates(market_rules: MarketRules | None = None) -> list[BaseGate]:
    """构造外部审计门集（22 门，顺序与本仓 29 门全家桶一致）。

    ``market_rules`` 注入 D-5/S-5 两门构造参（其余门不消费市场规则）；
    ``None`` ⇒ 评估时从 ctx['market_rules'] 读，再缺省回退 A 股默认。
    """
    rules = market_rules
    return [
        # D-Gate（🔧 数据键/schema 需 adapter）
        RawPriceJumpGate(),
        FloatMarketCapGate(),
        PitDividendYieldGate(),
        SuspensionVolumeGate(),
        HighPriceLotGate(market_rules=rules),
        # L-Gate（🔧）
        FeatureLivenessGate(),
        AllocationFidelityGate(),
        StaticAstCallGate(),
        # E-Gate（🔧 E-3；E-1/E-2 属本仓探针层，不入外部集）
        SlippagePriceCapGate(),
        # A-Gate（✅ A-1/A-2；🔧 A-3 逃生门基准、A-4 分段口径）
        FeeSumBalanceGate(),
        DailyCashConserveGate(),
        GoldenRoundtripGate(),
        SegmentRateScheduleGate(),
        # S-Gate（🔧 S-1/S-3/S-4/S-5；S-2 属本仓择时语义，不入外部集）
        TurnoverCeilingGate(),
        DynamicSlippageAdvGate(),
        DividendTaxLockGate(),
        AttributionEvidenceGate(market_rules=rules),
        # G-Gate（✅ G-1/G-4；G-2/G-3 属本仓纪律/母库，不入外部集）
        ProvenanceTriadGate(),
        AntiTamperSignatureGate(),
        # P0 一致性（✅ G-MDD-1/G-STRESS-1；G-DOC-1/G-REF-1 属本仓文档树，不入外部集）
        MaxDrawdownCeilingGate(),
        StressValidityGate(),
        # 复现一致性（✅）
        ReproducibilityGate(),
    ]


def audit_external_context(ctx: dict[str, Any], *, strict: bool = False) -> list[GateResult]:
    """对已装配好的外部 ctx 跑 22 门外部审计集。"""
    rules = resolve_market_rules(ctx)
    gates = get_external_gates(market_rules=rules)
    return GateMasterAudit(gates=gates).audit(context=ctx, strict=strict)


def audit_adapter(adapter: Any, *, strict: bool = False) -> tuple[list[GateResult], dict[str, Any]]:
    """adapter → ctx → 22 门评估；返回 ``(results, ctx)``（ctx 供报告/复现引用）。"""
    ctx = build_external_context(adapter)
    results = audit_external_context(ctx, strict=strict)
    return results, ctx


def _layer_of(gate_id: str) -> str:
    if gate_id in GENERAL_GATE_IDS:
        return "✅直接通用"
    if gate_id in ADAPTER_GATE_IDS:
        return "🔧需适配器"
    return "🏠本仓特有"


def print_external_summary(results: list[GateResult], source: str) -> None:
    """分层打印外部审计结果（PASS/FAIL/INCONCLUSIVE 按 ✅/🔧 两层分组）。"""
    print("=" * 88)
    print("        外部回测证据审计报告（通用层 ✅ + 需适配器层 🔧，排除本仓特有 🏠 门）")
    print("=" * 88)
    print(f"证据来源: {source}")
    print(f"排除门集（本仓特有 {len(REPO_BOUND_GATE_IDS)} 门）: {', '.join(REPO_BOUND_GATE_IDS)}")
    print("-" * 88)
    print(f"{'ID':<10} | {'层':<10} | {'状态':<13} | {'级别':<8} | 摘要")
    print("-" * 88)

    counts: dict[str, dict[str, int]] = {}
    for r in results:
        layer = _layer_of(r.gate_id)
        counts.setdefault(layer, {})
        counts[layer][r.status.value] = counts[layer].get(r.status.value, 0) + 1
        print(f"{r.gate_id:<10} | {layer:<10} | {r.status.value:<13} | {r.severity.value:<8} | {r.message[:60]}")

    print("-" * 88)
    for layer in ("✅直接通用", "🔧需适配器"):
        c = counts.get(layer, {})
        print(f"{layer}: {dict(sorted(c.items()))}")
    total: dict[str, int] = {}
    for c in counts.values():
        for k, v in c.items():
            total[k] = total.get(k, 0) + v
    print(f"合计 {len(results)} 门: {dict(sorted(total.items()))}")
    print("=" * 88)


def _load_adapter(spec: str) -> Any:
    """``module:attr`` 动态加载 adapter（类 ⇒ 无参实例化；实例/工厂 ⇒ 直接用/调用）。"""
    if ":" not in spec:
        raise SystemExit(f"--adapter 需 'module:attr' 形式，实得 {spec!r}")
    mod_name, attr = spec.split(":", 1)
    module = importlib.import_module(mod_name)
    obj = getattr(module, attr)
    if isinstance(obj, type):
        return obj()
    return obj() if callable(obj) and not hasattr(obj, "trades") else obj


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="外部回测证据门禁审计（通用+适配器层 22 门）")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--adapter", type=str, default="",
                     help="adapter 定位 'module:attr'（类无参实例化 / 实例 / 工厂函数）")
    src.add_argument("--ctx-json", type=str, default="",
                     help="已装配好的 ctx JSON 文件（market_rules 可为 dict）")
    parser.add_argument("--report", type=str, default="", help="输出 JSON 审计报告路径")
    args = parser.parse_args(argv)

    if args.adapter:
        adapter = _load_adapter(args.adapter)
        source = f"adapter {args.adapter}"
        results, ctx = audit_adapter(adapter)
    else:
        ctx = json.loads(open(args.ctx_json, encoding="utf-8").read())
        source = f"ctx-json {args.ctx_json}"
        results = audit_external_context(ctx)

    print_external_summary(results, source)

    if args.report:
        report = {
            "title": "External Evidence Gate Audit Report",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source": source,
            "excluded_repo_bound_gates": list(REPO_BOUND_GATE_IDS),
            "summary": {
                "total": len(results),
                "pass": sum(1 for r in results if r.status == GateStatus.PASS),
                "fail": sum(1 for r in results if r.status == GateStatus.FAIL),
                "skip": sum(1 for r in results if r.status == GateStatus.SKIP),
                "inconclusive": sum(1 for r in results if r.status == GateStatus.INCONCLUSIVE),
            },
            "results": [r.to_dict() for r in results],
        }
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"报告已保存至: {args.report}")

    blockers = [r for r in results if is_blocking_result(r)]
    if blockers:
        print(f"[BLOCKED] 检出 {len(blockers)} 项阻断（fail-closed：FAIL/INCONCLUSIVE 且 >=CRITICAL）：")
        for b in blockers:
            print(f"    [{b.gate_id}/{b.status.value}] {b.name}: {b.message[:90]}")
        return 1
    print("[PASS] 外部审计无阻断项。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
