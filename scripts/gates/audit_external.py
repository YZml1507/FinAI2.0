#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""audit_external：外部回测账本的门禁审计入口（E 路线通用化产品化）。

链路：``外部 adapter → assemble_context/build_context → 通用门集合``。

* 门集合 = spike 分级的「✅直接通用 + 🔧需适配器」共 22 门——即
  ``GateMasterAudit.get_standard_gates()`` 剔除 🏠 本仓特有 7 门
  （E-1/E-2 引擎探针、S-2 宽度择时、G-2 tasks.md 纪律、G-3/G-DOC-1/G-REF-1
  文件系统扫描——离开本仓即无审计对象，见 spike §4.3-1/3）。
* 市场规则经 ``ctx["market_rules"]`` 注入（adapter 自供或回落 A 股默认档）。
* 缺证据键 ⇒ INCONCLUSIVE（fail-closed，缺证据 ≠ 通过）；FAIL 一律 exit≠0。

用法（玩具账本实证，见 ``experiments/spikes/gate_generalization/``）::

    .venv/bin/python -m scripts.gates.audit_external \
        --adapter experiments.spikes.gate_generalization.toy_adapter:ToyEvidenceAdapter \
        --input experiments/spikes/gate_generalization/toy_ledger.csv

``--adapter`` 接受 ``module:attr`` 或 ``/path/to/file.py:attr``；``attr`` 可为
adapter 类（``--input`` 提供时以路径实例化，否则无参构造）、无参工厂函数，
或已就位的 ctx dict 生产者。
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .adapter import assemble_context
from .base import BaseGate, GateResult, GateStatus, is_blocking_result
from .gate_master_audit import GateMasterAudit
from .market_rules import MarketRules, resolve_market_rules

#: 🏠 本仓特有门（审计对象离开本仓即无意义）——外部审计默认排除。
REPO_ONLY_GATE_IDS: frozenset[str] = frozenset({
    "E-1", "E-2", "S-2", "G-2", "G-3", "G-DOC-1", "G-REF-1",
})

#: 外部可跑门集合（29 − 7 = 22）：✅ 直接通用 + 🔧 需适配器两类全收。
EXTERNAL_GATE_IDS: frozenset[str] = frozenset(
    g.gate_id for g in GateMasterAudit.get_standard_gates()
) - REPO_ONLY_GATE_IDS


def get_external_gates() -> list[BaseGate]:
    """外部审计门集合（标准门中剔除 ``REPO_ONLY_GATE_IDS``，顺序不变）。"""
    return [g for g in GateMasterAudit.get_standard_gates() if g.gate_id in EXTERNAL_GATE_IDS]


def run_external_audit(context: Any) -> list[GateResult]:
    """对外部 ctx 跑通用门集合，原样返回每门 ``GateResult``（PASS/FAIL/INCONCLUSIVE 分层如实呈现）。"""
    return GateMasterAudit(get_external_gates()).audit(context=context)


def _import_adapter_attr(spec: str) -> Any:
    """加载 ``module:attr`` 或 ``/path/file.py:attr`` 形式的 adapter 引用。"""
    module_ref, sep, attr = spec.rpartition(":")
    if not sep or not module_ref or not attr:
        raise TypeError(f"--adapter 需为 'module:attr' 或 'file.py:attr' 形式，收到 {spec!r}")
    if module_ref.endswith(".py") or "/" in module_ref:
        path = Path(module_ref).resolve()
        module_spec = importlib.util.spec_from_file_location(path.stem, path)
        if module_spec is None or module_spec.loader is None:
            raise ImportError(f"无法从文件加载 adapter 模块: {path}")
        module = importlib.util.module_from_spec(module_spec)
        sys.modules.setdefault(path.stem, module)
        module_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(module_ref)
    obj: Any = module
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def load_adapter(spec: str, input_path: str | None = None) -> Any:
    """实例化 adapter：类⇒构造（``--input`` 有则传路径），无参可调用⇒调用，其余原样返回。"""
    obj = _import_adapter_attr(spec)
    if isinstance(obj, type):
        return obj(input_path) if input_path else obj()
    if callable(obj):
        try:
            return obj(input_path) if input_path else obj()
        except TypeError:
            if input_path:
                raise
            return obj()
    return obj


def _print_external_summary(results: list[GateResult], rules: MarketRules) -> None:
    """外部审计报告头 + 复用主调度器摘要格式。"""
    print("=" * 80)
    print(f"外部回测门禁审计（市场规则档: {rules.name} | lot_size={rules.lot_size} "
          f"max_buy_price={rules.max_buy_price} required_fee_totals={[k for k, _ in rules.required_fee_totals]}）")
    print(f"门集合: {len(results)} 门（已排除本仓特有 {sorted(REPO_ONLY_GATE_IDS)}）")
    GateMasterAudit(get_external_gates()).print_summary(results)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="FinAI2.0 外部回测账本门禁审计入口")
    parser.add_argument("--adapter", required=True,
                        help="adapter 引用 'module:attr' 或 '/path/file.py:attr'")
    parser.add_argument("--input", type=str, default="",
                        help="adapter 输入路径（如外部账本 CSV），透传给 adapter 构造器")
    parser.add_argument("--strict", action="store_true",
                        help="严格模式：FAIL 或 INCONCLUSIVE（应检未检）任一即非零退出")
    parser.add_argument("--report", type=str, default="", help="输出 JSON 审计报告路径")
    args = parser.parse_args(argv)

    adapter = load_adapter(args.adapter, args.input or None)
    ctx = assemble_context(adapter)
    rules = resolve_market_rules(ctx)

    results = run_external_audit(ctx)
    _print_external_summary(results, rules)

    if args.report:
        report = GateMasterAudit(get_external_gates()).generate_json_report(results, args.report)
        report["market_rules"] = rules.name
        report["excluded_repo_only_gates"] = sorted(REPO_ONLY_GATE_IDS)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已保存至: {args.report}")

    fails = [r for r in results if r.status == GateStatus.FAIL]
    if fails or (args.strict and any(is_blocking_result(r) for r in results)):
        sys.exit(1)


if __name__ == "__main__":
    main()
