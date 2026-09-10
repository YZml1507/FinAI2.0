#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P0 一致性门禁（Consistency Gates）：把"形式全绿"改成"能真正拦住问题"。

依据 `docs/audit/roadmap_decision.md` §4「门禁重做的产品需求」，本模块落地四道 P0 门禁：

* ``G-MDD-1``    回撤上限门禁        —— 读产物 ``metrics.max_drawdown``，``> 0.35`` 判 FAIL(BLOCKER)。
* ``G-DOC-1``    文档数字↔产物一致性 —— 抽取 md 关键指标与权威产物比对，不一致判 FAIL。
* ``G-STRESS-1`` 压测有效性门禁      —— 压测区间 ``round_trips == 0`` 判 INCONCLUSIVE（不得 PASS）。
* ``G-REF-1``    引用路径存在性门禁  —— 抽取 md 中的仓内路径并断言 ``exists()``。

作废文档豁免（``gate-doc-void``）：``G-DOC-1`` / ``G-REF-1`` 跳过含标记
``<!-- gate-doc-void: date=YYYY-MM-DD; reason=<非空说明> -->`` 的文档，但**单独计数并打印**
``void_docs: N``（豁免必须可见，防"把一切标 void 消掉门禁"）；标记格式不合法时不生效。

设计原则（Fail-Closed）：

1. **无证据 = INCONCLUSIVE，绝不等于 PASS**；
2. **0 成交 ⇒ 指标平凡成立 ⇒ 不得据此判定通过**；
3. 报告必须指向**具体文件 + 行号**，可机读、可复现。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence

from .base import BaseGate, GateCategory, GateResult, GateSeverity, GateStatus
from .constants import TEST_BASELINE_PASSED

# ---------------------------------------------------------------------------
# 仓库根定位与通用读取工具
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 白名单：仅允许「运行期生成物」路径缺失（由运行产生，不在版本库中预置）。
_REFERENCE_WHITELIST_PREFIXES: tuple[str, ...] = (
    "experiments/runs/",
    "experiments/acceptance/",
    "runs/paper_trading/",
)

#: 「否定语境」标记：同一行若明确声明该路径**不存在/未实现**，则属**如实披露缺陷**，
#: ⛔ 不得判为"幽灵引用"——否则门禁会惩罚"如实登记"这一被鼓励的行为（反激励）。
#: 例：「飞书链路为 stub（ops/feishu_alert.py 未实现）」。
#: ⚠️ 标记必须**明确表达"不存在"**：⛔ 不得收 `幽灵`（那是门禁术语，描述问题本身，
#:    如「新幽灵 xx/」＝**正在报告**该路径有问题）、⛔ 不收 `stub`（单独出现不足以定性）。
_NEGATED_REFERENCE_MARKERS: tuple[str, ...] = (
    "未实现", "不存在", "未创建", "尚未", "未落盘", "未搬入", "缺失", "MISSING",
    "已删除", "曾引用", "原引用",
)

_FULLWIDTH_MAP = {
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    "．": ".", "％": "%", "－": "-", "＋": "+", "，": ",",
    "：": ":", "。": ".",
    # 各类 Unicode 负号/破折号 → ASCII '-'（否则 "−3.20%" 会被读成 +3.20）
    "−": "-", "–": "-", "—": "-", "―": "-", "‑": "-",
}


def _normalize_text(text: str) -> str:
    """全角 → 半角，去除千分位逗号（仅用于数字抽取）。"""
    out = "".join(_FULLWIDTH_MAP.get(ch, ch) for ch in text)
    return out


def _to_decimal(value: Any) -> Decimal | None:
    """尽力把产物字段/文本数字转为 Decimal；失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = _normalize_text(str(value)).replace(",", "").strip()
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                       # noqa: BLE001 —— 脏产物按"无证据"处理
        return None
    return data if isinstance(data, dict) else None


def _run_artifact_files(repo_root: Path) -> list[Path]:
    """``experiments/runs/*.json``（``index.jsonl`` 除外）按名升序。"""
    runs_dir = repo_root / "experiments" / "runs"
    if not runs_dir.exists():
        return []
    return sorted(p for p in runs_dir.glob("*.json") if p.is_file())


def _artifact_from_path(path: Path) -> dict[str, Any] | None:
    data = _read_json(path)
    if data is None:
        return None
    return {"_path": str(path), "metrics": data.get("metrics", {}) or {}, "record": data}


def _collect_artifacts(context: Any) -> list[dict[str, Any]]:
    """从 context 解析待评估的产物集合；缺省扫描 ``experiments/runs/*.json``。"""
    ctx: dict[str, Any] = context if isinstance(context, dict) else {}

    explicit: list[Any] = []
    if ctx.get("artifact_path"):
        explicit.append(ctx["artifact_path"])
    if ctx.get("artifact_paths"):
        explicit.extend(ctx["artifact_paths"])
    if ctx.get("run_record"):
        explicit.append(ctx["run_record"])
    if ctx.get("run_records"):
        explicit.extend(ctx["run_records"])
    if ctx.get("metrics") and not explicit:
        explicit.append({"_path": "<inline-metrics>", "metrics": ctx["metrics"]})

    artifacts: list[dict[str, Any]] = []
    for item in explicit:
        if isinstance(item, dict):
            if "metrics" in item:
                artifacts.append({"_path": item.get("_path", "<inline>"),
                                  "metrics": item["metrics"] or {},
                                  "record": item})
            else:
                artifacts.append({"_path": item.get("_path", "<inline>"),
                                  "metrics": item, "record": item})
        else:
            art = _artifact_from_path(Path(str(item)))
            if art is not None:
                artifacts.append(art)

    if artifacts:
        return artifacts

    # 缺省：扫描全仓产物（无参自检既反映真实仓库状态）
    for path in _run_artifact_files(_REPO_ROOT):
        art = _artifact_from_path(path)
        if art is not None:
            artifacts.append(art)
    return artifacts


def _resolve_truth_artifact(context: Any) -> dict[str, Any] | None:
    """解析「权威产物」：优先显式给定，否则取最新的、有成交的 FINISHED 产物。"""
    ctx: dict[str, Any] = context if isinstance(context, dict) else {}
    for key in ("truth_run_path", "run_record_path"):
        if ctx.get(key):
            return _artifact_from_path(Path(str(ctx[key])))

    candidates: list[dict[str, Any]] = []
    for path in _run_artifact_files(_REPO_ROOT):
        art = _artifact_from_path(path)
        if art is None:
            continue
        record = art["record"]
        if str(record.get("status", "")).upper() != "FINISHED":
            continue
        candidates.append(art)
    if not candidates:
        return None
    # 优先取有成交（round_trips > 0）者，其次取最新
    with_trades = [a for a in candidates if int(a["metrics"].get("round_trips") or 0) > 0]
    pool = with_trades or candidates
    return pool[-1]


# ---------------------------------------------------------------------------
# G-MDD-1 回撤上限门禁
# ---------------------------------------------------------------------------

class MaxDrawdownCeilingGate(BaseGate):
    """G-MDD-1: 最大回撤上限硬门禁（BLOCKER）。

    判定规则：

    * ``round_trips == 0`` ⇒ **INCONCLUSIVE**（0 成交时 MDD 恒为 0，属平凡成立，严禁判 PASS）；
    * ``max_drawdown > 0.35`` ⇒ **FAIL(BLOCKER)**；
    * 数据缺失 ⇒ **INCONCLUSIVE**。
    """

    gate_id = "G-MDD-1"
    name = "最大回撤上限门禁"
    category = GateCategory.G_GATE
    severity = GateSeverity.BLOCKER
    evidence = "roadmap_decision.md §4 G-MDD-1：MDD 43.08% 超 35% 阈值却无门禁可拦（审计 P0）"
    threshold_desc = "metrics.max_drawdown 必须 <= 0.35（且 round_trips > 0 方为有效判定）"

    MAX_MDD = Decimal("0.35")

    def evaluate(self, context: Any = None) -> GateResult:
        artifacts = _collect_artifacts(context)
        if not artifacts:
            return self._make(GateStatus.INCONCLUSIVE, "未找到任何回测产物，无法判定回撤上限")

        failures: list[dict[str, Any]] = []
        inconclusives: list[dict[str, Any]] = []
        evaluated = 0

        for art in artifacts:
            metrics = art.get("metrics", {}) or {}
            path = art.get("_path", "<inline>")
            if "max_drawdown" not in metrics:
                inconclusives.append({"artifact": path, "reason": "产物缺少 max_drawdown 字段"})
                continue
            mdd = _to_decimal(metrics.get("max_drawdown"))
            if mdd is None:
                inconclusives.append({"artifact": path, "reason": "max_drawdown 非法或缺失"})
                continue
            round_trips = int(metrics.get("round_trips") or 0)
            if round_trips == 0:
                inconclusives.append({
                    "artifact": path,
                    "reason": "round_trips=0（0 笔成交），MDD 恒为 0 属平凡成立，不得据此判定通过",
                    "max_drawdown": str(mdd),
                })
                continue
            evaluated += 1
            if mdd > self.MAX_MDD:
                failures.append({
                    "artifact": path,
                    "max_drawdown": str(mdd),
                    "max_drawdown_pct": round(float(mdd) * 100, 2),
                    "round_trips": round_trips,
                })

        if failures:
            first = failures[0]
            return self._make(
                GateStatus.FAIL,
                (
                    f"检出 {len(failures)} 份产物最大回撤超限：{first['artifact']} "
                    f"MDD={_to_decimal(first['max_drawdown']):.4f} > 0.35 "
                    f"（{first['max_drawdown_pct']}%，往返 {first['round_trips']} 笔）"
                ),
                metrics={"failures": failures, "threshold": "0.35"},
            )
        if inconclusives:
            inc = inconclusives[0]
            return self._make(
                GateStatus.INCONCLUSIVE,
                f"回撤判定证据不足（未通过项 {len(inconclusives)} 份）：{inc['artifact']} —— {inc['reason']}",
                metrics={"inconclusive": inconclusives, "evaluated": evaluated},
            )
        return self._make(
            GateStatus.PASS,
            f"{evaluated} 份产物最大回撤均 <= 35% 上限",
            metrics={"evaluated": evaluated},
        )

    def _make(self, status: GateStatus, message: str, metrics: dict[str, Any] | None = None) -> GateResult:
        return GateResult(
            gate_id=self.gate_id, name=self.name, category=self.category,
            status=status, severity=self.severity, message=message,
            metrics=metrics or {}, threshold=self.threshold_desc, evidence=self.evidence,
        )


# ---------------------------------------------------------------------------
# G-STRESS-1 压测有效性门禁
# ---------------------------------------------------------------------------

class StressValidityGate(BaseGate):
    """G-STRESS-1: 压力测试有效性门禁（CRITICAL）。

    * 压测区间 ``round_trips == 0`` ⇒ **INCONCLUSIVE**（0 成交 → MDD/胜率判据平凡成立）；
    * 压测区间接 < 200 交易日 ⇒ **INCONCLUSIVE**（样本不足，受 warmup 支配）；
    * 数据缺失 ⇒ **INCONCLUSIVE**；
    * 有成交且区间 >= 200 交易日 ⇒ **PASS**（允许进入 MDD / 胜率判定）。
    """

    gate_id = "G-STRESS-1"
    name = "压力测试有效性门禁"
    category = GateCategory.G_GATE
    severity = GateSeverity.CRITICAL
    evidence = "roadmap_decision.md §4 G-STRESS-1：T313 以 0 笔成交的 40/60 天空仓，判 MDD<35% 通过属失效"
    threshold_desc = "压测区间必须 round_trips > 0 且 >= 200 交易日，方可进入 MDD/胜率判定"

    MIN_TRADING_DAYS = 200

    def evaluate(self, context: Any = None) -> GateResult:
        ctx = dict(context) if isinstance(context, dict) else {}
        round_trips = ctx.get("round_trips")
        days = ctx.get("trading_days")
        if days is None:
            days = ctx.get("stress_days")

        # 允许直接传入产物路径
        if round_trips is None and (ctx.get("artifact_path") or ctx.get("run_record_path")):
            art = _artifact_from_path(Path(str(ctx.get("artifact_path") or ctx.get("run_record_path"))))
            if art:
                metrics = art["metrics"]
                round_trips = metrics.get("round_trips")
                days = days if days is not None else metrics.get("trading_days")
        if round_trips is None and ctx.get("metrics"):
            metrics = ctx["metrics"]
            round_trips = metrics.get("round_trips")
            days = days if days is not None else metrics.get("trading_days")

        if round_trips is None:
            return self._make(
                GateStatus.INCONCLUSIVE,
                "缺少压测区间的 round_trips 数据，无法判定压测是否有效（无证据 ≠ 通过）",
            )

        round_trips = int(round_trips)
        if round_trips <= 0:
            return self._make(
                GateStatus.INCONCLUSIVE,
                (
                    f"压测区间 round_trips=0（0 笔成交），区间长度 {days if days is not None else '未知'} 日；"
                    "MDD/胜率判据在该区间平凡成立，不得据此判定策略通过"
                ),
                metrics={"round_trips": round_trips, "trading_days": days},
            )

        if days is not None and int(days) < self.MIN_TRADING_DAYS:
            return self._make(
                GateStatus.INCONCLUSIVE,
                (
                    f"压测区间接仅 {int(days)} 交易日（< {self.MIN_TRADING_DAYS}），"
                    f"受 warmup 冷启动支配、样本不足，虽有 {round_trips} 笔成交仍不足以进入 MDD/胜率判定"
                ),
                metrics={"round_trips": round_trips, "trading_days": int(days)},
            )

        return self._make(
            GateStatus.PASS,
            (
                f"压测区间有效：{round_trips} 笔成交"
                + (f"、{int(days)} 交易日" if days is not None else "")
                + "（>= 200 日且 round_trips > 0），允许进入 MDD/胜率判定"
            ),
            metrics={"round_trips": round_trips, "trading_days": days},
        )

    def _make(self, status: GateStatus, message: str, metrics: dict[str, Any] | None = None) -> GateResult:
        return GateResult(
            gate_id=self.gate_id, name=self.name, category=self.category,
            status=status, severity=self.severity, message=message,
            metrics=metrics or {}, threshold=self.threshold_desc, evidence=self.evidence,
        )


# ---------------------------------------------------------------------------
# G-DOC-1 文档数字 ↔ 产物一致性门禁
# ---------------------------------------------------------------------------

#: 关键指标：(指标名, 行内关键词正则, 产物 metrics 键, 单位要求)。
#: 关键词刻意收紧（"回撤"裸词、"换手"裸词会命中需求/审计散文，造成误报泛滥）。
#: 单位要求：百分比指标必须紧跟 ``%``，金额指标必须紧跟 ``元``——只有"声明为实测值"才比对。
_DOC_METRIC_SPECS: tuple[tuple[str, str, str, str | None], ...] = (
    ("换手率", r"换手率|年化换手", "annual_turnover", "%"),
    ("最大回撤", r"最大回撤|MDD", "max_drawdown", "%"),
    ("年化收益", r"CAGR|年化收益", "cagr", "%"),
    ("胜率", r"胜率", "win_rate", "%"),
    ("总费用", r"总费用|总交易摩擦", "fees_sum", "元"),
    ("初始资金", r"初始资金|初始本金", "initial_nav", "元"),
    ("最终净值", r"最终净值", "final_nav", "元"),
)

#: 行内出现以下短语时跳过（预期/容忍/需求描述/对照/比较类，不是"实测结论"）。
#: ⛔ ``gate-doc-ignore`` **不在**此列——它必须带**非空理由**方生效（见
#: :func:`line_ignore_reason`），否则"随手写个裸标记就整行免检"会成为逃逸口。
_DOC_LINE_SKIP_PHRASES: tuple[str, ...] = (
    "预期", "容忍", "熔断", "压力测试结果", "测试区间", "股灾", "熊市",
    "判据", "验收", "注入", "示例", "例如", "→", "≠", "↔", "vs",
    "真值", "本文写", "作废", "待重写", "冻结",
    # 目标/阈值/预期类（是"要求"不是"实测结论"，与权威产物不可比）
    "目标", "预期", "阈值", "容忍", "门槛", "准入", "建议", "要求",
)

#: 行内豁免标记：``<!-- gate-doc-ignore: <非空理由> -->``（⛔ 只豁免**本行**，不扩散）。
#: 契约与 ``gate-doc-void`` 同类：**理由非空**方生效；裸标记 ``<!-- gate-doc-ignore -->``
#: 或缺理由（``: -->``）⇒ **不生效**（该行照常校验），防"拿它消掉真缺陷"。
_IGNORE_MARKER_RE = re.compile(r"<!--\s*gate-doc-ignore\s*:\s*(.+?)\s*-->")


def line_ignore_reason(line: str) -> str | None:
    """行内 ``<!-- gate-doc-ignore: <理由> -->`` 的**非空**理由；无标记/理由为空 ⇒ ``None``。

    ⛔ 作用域**仅本行**（调用方逐行判定），不扩散到整段/整文件——
    文件级作废请用 :func:`is_void_doc`（``gate-doc-void``，需文首标记 + 计数上限）。
    """
    m = _IGNORE_MARKER_RE.search(line)
    if not m:
        return None
    reason = m.group(1).strip()
    return reason or None

#: 数值前若紧跟比较符，则视为"阈值/约束"而非实测值。
_COMPARISON_OPS = ("<", ">", "≤", "≥", "≈", "=")

#: 行内全部数字（千分位/全角已归一）。
_NUMBER_TOKEN_RE = re.compile(r"[+\-]?\d[\d,]*(?:\.\d+)?")


def _line_numbers(line: str) -> list[Decimal]:
    out: list[Decimal] = []
    for tok in _NUMBER_TOKEN_RE.findall(_normalize_text(line)):
        try:
            out.append(Decimal(tok.replace(",", "")))
        except InvalidOperation:
            continue
    return out

#: 百分比/数值容差（容纳四舍五入）。
_DOC_PCT_TOL = Decimal("0.06")
_DOC_ABS_TOL = Decimal("0.5")

#: 形如 ``20260907-150402`` 的 run 标识：行内若引用**非权威** run，则跳过（该行在描述别的产物）。
_RUN_ID_RE = re.compile(r"\b(\d{8}-\d{6})\b")


#: 默认排除的**目录**白名单（相对仓根）。`docs/audit/**` 本身就是"引述缺陷证据"的报告，
#: 默认排除，否则审计文档会被自己的证据触发（自指误报），门禁永远无法转绿。
#: ⛔ 只允许**这一个**（守卫测试 `tests/test_gate_consistency.py::TestExemptionGuards
#: ::test_excluded_doc_dirs_is_locked` 锁定 `_EXCLUDED_DOC_DIRS == ("docs/audit",)`，
#: 防止将来有人往白名单加目录以扩大逃逸面，QA ㉚/㊲）。
_EXCLUDED_DOC_DIRS: tuple[str, ...] = ("docs/audit",)
#: 排除面的**可见计数**必须打印（QA ㊲：`_EXCLUDED_DOC_DIRS` 曾是唯一"静默豁免"通道）。
_EXCLUDED_DOC_DIR_PATHS: tuple[Path, ...] = tuple(
    (_REPO_ROOT / d).resolve() for d in _EXCLUDED_DOC_DIRS
)


def _is_excluded_doc(path: Path) -> bool:
    for excluded in _EXCLUDED_DOC_DIR_PATHS:
        try:
            path.resolve().relative_to(excluded)
            return True
        except ValueError:
            continue
    return False


def _excluded_doc_files() -> list[Path]:
    """被 `_EXCLUDED_DOC_DIRS` 排除的文档（⛔ 仅用于**可见计数**，不参与校验）。"""
    docs_dir = _REPO_ROOT / "docs"
    if not docs_dir.exists():
        return []
    return sorted(
        p for p in docs_dir.glob("**/*.md") if p.is_file() and _is_excluded_doc(p)
    )


#: 仓根级文档（历史"无人管"的漏网之鱼）：纳入 G-DOC-1 扫描（QA ㉘）。
_ROOT_DOCS: tuple[str, ...] = ("README.md", "CLAUDE.md")

#: CI / 钩子工作流（㊲）：其中的**步骤名**同样载有单测基线与门禁数声明
#: （如旧文 `Offline Pytest Regression Suite (>=758 passed)` / `full 28 gates`）。
#: 曾因 `_doc_files` 不扫 `.yml` 而**永远无法被 G-DOC-1 发现**，故纳入扫描。
_WORKFLOW_DOC_GLOBS: tuple[str, ...] = (".github/workflows/*.yml", ".github/workflows/*.yaml")


def _doc_files(context: Any) -> list[Path]:
    ctx: dict[str, Any] = context if isinstance(context, dict) else {}
    if ctx.get("doc_paths"):
        # 显式给定路径时尊重调用方（测试注入用），不做默认排除。
        return [Path(str(p)) for p in ctx["doc_paths"]]
    repo_root = _REPO_ROOT
    docs_dir = repo_root / "docs"
    files: list[Path] = []
    if docs_dir.exists():
        files.extend(sorted(p for p in docs_dir.glob("**/*.md") if p.is_file() and not _is_excluded_doc(p)))
    tasks = docs_dir / "spec" / "001-a-stock-longonly-daily-quant" / "tasks.md"
    if tasks.exists() and tasks not in files:
        files.append(tasks)
    # 仓根 README.md / CLAUDE.md（曾漏网；纳入扫描使基线/门禁数声明无死角）。
    for name in _ROOT_DOCS:
        root_doc = repo_root / name
        if root_doc.exists() and root_doc not in files:
            files.append(root_doc)
    # ㊲ GitHub Actions 工作流（步骤名含单测基线/门禁数声明）——曾因不扫 .yml 而永不曝光。
    for pattern in _WORKFLOW_DOC_GLOBS:
        files.extend(sorted(p for p in repo_root.glob(pattern) if p.is_file() and p not in files))
    return files


# ---------------------------------------------------------------------------
# gate-doc-void 标记机制（任务 4）：作废文档豁免
# ---------------------------------------------------------------------------

#: 作废文档标记（须出现在**文首前 N 行**）：
#: ``<!-- gate-doc-void: date=YYYY-MM-DD; reason=<非空说明> -->``
#: 契约精确：``date`` 必须为 ``YYYY-MM-DD``；``reason`` 必须**非空**，否则标记**不生效**。
_VOID_MARKER_RE = re.compile(
    r"<!--\s*gate-doc-void\s*:\s*date=(\d{4}-\d{2}-\d{2})\s*;\s*reason=(.+?)\s*-->"
)

#: 标记必须落在文首前 N 行（``.splitlines()[:N]``）——把豁免收紧为**文档级声明**，
#: ⛔ 不再是"行内随手加个注释就整篇免检"（QA ㉚ 粒度收紧）。
_VOID_MARKER_HEADER_LINES = 20

#: 豁免文档数**上限**：超过即判 FAIL（防"把一切标 void 来消掉门禁"）；`void_docs` 始终可见计数。
MAX_VOID_DOCS = 8


def is_void_doc(text: str) -> bool:
    """文档是否被 ``<!-- gate-doc-void: date=...; reason=... -->`` 标记为**已作废**。

    ⛔ 标记格式不合法（缺 ``date=`` 或 ``reason=`` 为空）时**不生效**；
    ⛔ 标记必须出现在文首前 :data:`_VOID_MARKER_HEADER_LINES` 行内（否则不生效）。
    该文档仍照常被 G-DOC-1 / G-REF-1 校验——防止"随手写个残缺/深处标记消掉门禁"。
    """
    for line in text.splitlines()[:_VOID_MARKER_HEADER_LINES]:
        m = _VOID_MARKER_RE.search(line)
        if m and m.group(2).strip():
            return True
    return False


# ---------------------------------------------------------------------------
# 门禁数量 / 单测基线声明一致性（任务 3 补充）：动态核 doc 声称数 vs 单一事实源
# ---------------------------------------------------------------------------

#: Markdown 强调符（``*`` / 反引号 / ``~``）会打断 ``\d+\s*道``（如 ``**36** 道``），
#: 匹配前先剥离（QA ㉘ D1：加粗打断导致漏判）。
_MD_EMPHASIS_RE = re.compile(r"[*`~]+")


def _strip_md(text: str) -> str:
    """剥离 markdown 强调符，使 ``**36** 道门禁`` 归一为 ``36 道门禁``。"""
    return _MD_EMPHASIS_RE.sub("", text)


#: 中文数字 → 阿拉伯数字（㊲ 漏判修复：``共二十八道门禁`` 原先完全漏判）。
#: 仅支持 ``零一二两三四五六七八九十百千`` 的常规组合（≤9999），覆盖"门禁数/基线"量级。
_CN_DIGITS: dict[str, int] = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS: dict[str, int] = {"十": 10, "百": 100, "千": 1000}
_CN_NUMERAL_RUN_RE = re.compile(r"[零一二两三四五六七八九十百千]+")


def _cn_to_int(token: str) -> int | None:
    """中文数字串 → int；含未知字符/空串 ⇒ ``None``。"""
    if not token:
        return None
    total = 0
    section = 0
    number = 0
    for ch in token:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            section += (number or 1) * unit
            number = 0
        else:
            return None
    return total + section + number


def _normalize_cn_numerals(text: str) -> str:
    """把中文数字串转成阿拉伯数字（仅用于"门禁数/基线"声明匹配，⛔ 不改原行）。"""
    def _sub(m: re.Match[str]) -> str:
        value = _cn_to_int(m.group(0))
        return str(value) if value is not None else m.group(0)

    return _CN_NUMERAL_RUN_RE.sub(_sub, text)


#: 门禁数量声明的**结构式**模式（⛔ 不再"同行含关键词即命中"——那会命中
#: 「G4.5 门禁 3 条必达指标」「52 个单测」「≥6 个月」等"量词修饰他物"的散文）。
#:
#: * **模式 A（数字在前、直接修饰门禁/闸门）**：``N 道/项/个 [机读|六维|…] 门禁``；
#: * **模式 B1（门禁在前、计数名词连词）**：``门禁数量/总数/数目/个数/数 [:：=] N``；
#: * **模式 B2（门禁在前、动词连词 + 量词）**：``门禁[≤8 字]共/总计/重做为…[≤12 字]N 道/项/个``
#:   ——覆盖「门禁包已重做为 28 道」这类数字与"门禁"不相邻的当前时态结论。
#:   ⛔ 连接词**必需**：否则「门禁，3 道必达指标」「门禁 2 道失败」会把**子集计数**
#:   误当总数（QA ㊲ 误报修复）。
#:
#: 三条模式均**不跨行/不跨句**（``[^\r\n。；]``）：旧实现 ``\s*`` 可吞换行，
#: 曾把「…回撤上限门禁\n3 项…」误连成 ``门禁\n3``，造成跨行误报。
_GATE_COUNT_RES: tuple[re.Pattern[str], ...] = (
    # 模式 A：数字直接修饰门禁/闸门（量词 道/项/个 + 可选限定词）。
    re.compile(
        r"(\d+)\s*[道项个]\s*"
        r"(?:机读|自动|六维|防伪|质量|防御|一致|P0)*\s*"
        r"(?:门禁|闸门)"
    ),
    # 模式 B1：门禁 + 计数名词（数量/总数/数目/个数/数）+ 可选 [为|是|：|:|=] + N（量词可省）。
    re.compile(r"(?:门禁|闸门)\s*(?:数量|总数|数目|个数|数)\s*(?:为|是|：|:|=)?\s*(\d+)\s*[道项个]?"),
    # 模式 B2：门禁 + [≤8 字] + **动词连词** + [≤12 字] + N + 量词（量词必需）。
    re.compile(
        r"(?:门禁|闸门)[^\r\n。；]{0,8}?"
        r"(?:共计|总计|共|已达|达|为|有|是|重做为|重做|改为|调整为)"
        r"[^\r\n。；]{0,12}?(\d+)\s*[道项个]"
    ),
)

#: **子集/非总数**尾标记：紧跟"门禁/闸门"出现即说明该数字是"通过/未通过"的**子集计数**，
#: 不是门禁总数 ⇒ 跳过（QA ㊲ 误报修复：「本次共 4 道门禁未通过」不得报 4）。
_GATE_COUNT_SUBSET_TAIL_RE = re.compile(r"^\s*(?:未通过|不通过|失败|通过|必达|其中)")

#: **带日期的任务/修订日志行**：`tasks.md` 与纪事类文件按约定是 **append-only 的日期化
#: 提交日志**，其门禁数/基线是**当时快照**（如 2026-09-07 记「总门禁达 24 项」）。
#: 两种形态：
#:
#: * 复选框条目：``- [x] [T-GATE-P3] … — 2026-09-07 ✅ …``
#: * 修订登记表行：``| TK-27 | 2026-09-07 | …``
#:
#: 此类行豁免当前时态声明校验，但**单独计数可见**（``dated_task_log_lines``，⛔ 不得静默）。
#: ⛔ 匹配刻意收窄到"带日期的日志形态"：散文式当前时态声明（如
#: 「本仓共 24 道门禁（2026-09-10 实测）」）**不带复选框/修订表结构**，
#: 普通数据表行（如 ``| 门禁总数 | 29 |`` 无日期）也**不匹配**，因而照常校验。
_DATED_TASK_LOG_RE = re.compile(
    r"^\s*(?:"
    r"-\s*\[[ xX]\][^\r\n]*?—\s*\d{4}-\d{2}-\d{2}"              # - [x] … — YYYY-MM-DD
    r"|\|\s*[A-Za-z][\w\-]*\s*\|\s*\d{4}-\d{2}-\d{2}\s*\|"       # | TK-27 | 2026-09-07 | …
    r")"
)

#: 「N passed」/「基线 N」中的**基线声明**——必须"基线"与"passed"语义成对且**同行**，
#: 以免把历史进度计数（如流程图里 19/162/…/681 的单测演进、子集运行 "18 passed"）
#: 误判为"当前基线声明"（该门禁刻意收紧，⛔ 不允许误报泛滥；历史快照走行内豁免）。
#: ㊲ 补：「基线：N」（无 passed）、「passed=N（基线）」、「共 N 项测试通过」三种等价措辞。
_TEST_BASELINE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"基线[^\r\n]{0,20}?(\d+)\s*passed"),
    re.compile(r"(\d+)\s*passed[^\r\n]{0,10}?基线"),
    re.compile(r"基线[^\r\n]{0,6}?[:：=]\s*(\d+)"),            # 基线：838
    re.compile(r"passed\s*[=＝:：]\s*(\d+)[^\r\n]{0,10}?基线"),  # passed=838（基线）
    re.compile(r"共\s*(\d+)\s*项测试通过"),                     # 共 838 项测试通过
)

#: 历史归档快照白名单（带理由、可审查）：其门禁数量/基线是**历史阶段快照**，
#: 不随门禁演进同步；⛔ 不允许"顺手把历史数字改成现值"（等于改史）。
#: 如需豁免其它历史件，请用 ``gate-doc-void`` 标记（契约见 ``is_void_doc``）。
#: ⚠ 键为**仓根相对路径**（精确匹配）：旧实现用 ``endswith`` ⇒ ``docs/anything/CLAUDE.md``
#: 会被误判为仓根 ``CLAUDE.md`` 而整篇豁免（QA ㊲ 白名单过宽）。守卫测试锁定该集合。
_HISTORICAL_SNAPSHOT_DOCS: dict[str, str] = {
    "docs/delivery/GATE_PHASE1_COMPLETION_SUMMARY.md": "阶段一历史快照（23 道门禁 / 681 passed 等当时值）",
    "docs/delivery/GATE_PHASE2_COMPLETION_SUMMARY.md": "阶段二历史快照（门禁数与单测基线为当时值）",
    "docs/delivery/GATE_PHASE3_COMPLETION_SUMMARY.md": "阶段三历史快照（24 道机读门禁 / 717 passed 等当时值）",
    "CLAUDE.md": "仓根工程纪事（含 Phase 0–6 历史阶段基线快照，如 19/62/…/629 passed）；⛔ 不改史",
}


def _relpath_posix(path: Path | str) -> str:
    """把**仓内**路径归一为仓根相对 POSIX 路径；仓外路径原样返回（调用方自行处理）。"""
    try:
        return Path(path).resolve().relative_to(_REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return Path(path).as_posix()


def _is_historical_snapshot(path: Path) -> bool:
    """是否历史归档快照（其门禁数量/单测基线声明豁免同步校验）。

    匹配口径（⛔ 收紧，修复 QA ㊲「白名单过宽」）：

    * **仓内路径** ⇒ 必须**精确等于**白名单键：旧实现用 ``endswith`` ⇒
      ``docs/anything/CLAUDE.md`` 会被误判为仓根 ``CLAUDE.md`` 而整篇豁免；
    * **仓外路径**（测试隔离目录 / 外部归档）⇒ 允许"路径段对齐的后缀"匹配，
      便于白名单文件被移出仓后仍可识别（⛔ 无仓内相对路径可言，且不构成逃逸面）。
    """
    p = Path(path)
    try:
        rel = p.resolve().relative_to(_REPO_ROOT).as_posix()
    except (ValueError, OSError):
        posix = p.as_posix()
        return any(posix == key or posix.endswith("/" + key) for key in _HISTORICAL_SNAPSHOT_DOCS)
    return rel in _HISTORICAL_SNAPSHOT_DOCS


def expected_gate_count(context: Any = None) -> int:
    """门禁数量期望值：**动态**取 ``GateMasterAudit.get_standard_gates()`` 长度（⛔ 不写死）。

    允许 ctx 注入 ``expected_gate_count``（测试用）。
    """
    ctx: dict[str, Any] = context if isinstance(context, dict) else {}
    injected = ctx.get("expected_gate_count")
    if isinstance(injected, int):
        return injected
    from .gate_master_audit import GateMasterAudit    # 延迟导入：避免与总调度器循环依赖

    return len(GateMasterAudit.get_standard_gates())


def expected_test_baseline(context: Any = None) -> int:
    """单测基线期望值：单一事实源 ``constants.TEST_BASELINE_PASSED``（允许 ctx 注入，测试用）。"""
    ctx: dict[str, Any] = context if isinstance(context, dict) else {}
    injected = ctx.get("expected_test_baseline")
    if isinstance(injected, int):
        return injected
    return TEST_BASELINE_PASSED


def _declaration_violations_in_text(
    text: str,
    expected_gate_count: int,
    expected_baseline: int,
    stats: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """全文级「门禁数量 / 单测基线」声明漂移（结构化匹配，返回**带行号**条目）。

    口径：

    * 匹配前剥离 markdown 强调符（``**36** 道`` ⇒ ``36 道``）并把**中文数字**归一
      （``共二十八道门禁`` ⇒ ``共28道门禁``，㊲ 漏判修复；⛔ 不改原行，仅用于匹配）；
    * 门禁数量只认**结构化**模式 A/B1/B2（见 :data:`_GATE_COUNT_RES`）——⛔ 不再"同行含
      关键词即命中"，故「G4.5 门禁 3 条必达指标」「52 个单测」「≥6 个月」不再误报；
    * 紧跟"门禁/闸门"出现"未通过/失败/通过/必达/其中" ⇒ 该数字是**子集计数**，跳过
      （㊲：「本次共 4 道门禁未通过」不得报 4）；
    * 基线只认**同行**语义成对（见 :data:`_TEST_BASELINE_RES`）——⛔ 不跨行；
    * 「本行已含真值 ⇒ 跳过」必须用**数字集合比对**（⛔ 不用子串——``"29" in "129"``
      会让 ``129 道门禁`` 整行被静默放过：这是 QA ㉘ 点名的最坏漏判）；
    * 复用 ``_DOC_LINE_SKIP_PHRASES`` 排除预期/阈值/更正类散文（与指标比对同口径），
      历史快照行以**带非空理由**的行内 ``<!-- gate-doc-ignore: <理由> -->`` 标注豁免；
    * ``stats["ignored_linenos"]`` 收集被行内 ignore 豁免的**行号**（豁免必须**可见**，⛔ 不得静默；
      调用方按 ``(文件, 行号)`` 去重，避免同一行被多处扫描重复计数）。
    """
    orig_lines = text.splitlines()
    norm = _normalize_cn_numerals(_normalize_text(_strip_md(text)))

    ignored_lines: set[int] = set()
    dated_log_lines: set[int] = set()

    def _lineno(pos: int) -> int:
        return norm.count("\n", 0, pos) + 1

    def _orig_line(ln: int) -> str:
        return orig_lines[ln - 1] if 1 <= ln <= len(orig_lines) else ""

    def _line_nums(ln: int) -> set[Decimal]:
        return set(_line_numbers(_orig_line(ln)))

    def _skipped(ln: int) -> bool:
        line = _orig_line(ln)
        if any(p in line for p in _DOC_LINE_SKIP_PHRASES):
            return True
        if line_ignore_reason(line) is not None:
            ignored_lines.add(ln)
            return True
        if _DATED_TASK_LOG_RE.match(line):      # 日期化提交日志 ⇒ 当时快照（可见计数）
            dated_log_lines.add(ln)
            return True
        return False

    out: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()

    def _emit(ln: int, metric: str, val: int, expected: int) -> None:
        key = (ln, metric, str(val))
        if key in seen:
            return
        seen.add(key)
        out.append({
            "lineno": ln, "metric": metric, "doc_value": str(val),
            "expected": str(expected), "line": _orig_line(ln).strip()[:120],
        })

    # ① 门禁数量：结构化匹配（模式 A/B，均不跨行/不跨句 ⇒ 消除"同行含关键词即命中"误报）
    for pat in _GATE_COUNT_RES:
        for m in pat.finditer(norm):
            ln = _lineno(m.start())
            if _skipped(ln):
                continue
            # ㊲：紧跟上文出现"未通过/失败/通过/必达/其中"⇒ 该数字是**子集计数**，非总数。
            if _GATE_COUNT_SUBSET_TAIL_RE.match(norm[m.end():m.end() + 4]):
                continue
            val = int(m.group(1))
            if val == expected_gate_count or Decimal(expected_gate_count) in _line_nums(ln):
                continue
            _emit(ln, "门禁数量", val, expected_gate_count)

    # ② 单测基线
    for pat in _TEST_BASELINE_RES:
        for m in pat.finditer(norm):
            ln = _lineno(m.start())
            if _skipped(ln):
                continue
            val = int(m.group(1))
            if val == expected_baseline or Decimal(expected_baseline) in _line_nums(ln):
                continue
            _emit(ln, "单测基线", val, expected_baseline)

    if stats is not None:
        stats.setdefault("ignored_linenos", set()).update(ignored_lines)   # type: ignore[union-attr]
        stats.setdefault("dated_log_linenos", set()).update(dated_log_lines)   # type: ignore[union-attr]

    out.sort(key=lambda e: (e["lineno"], e["metric"], e["doc_value"]))
    return out


#: 关键词前若出现这些词，说明该数字是"约束口径"而非实测值。
_THRESHOLD_WORDS = ("超过", "低于", "高于", "不到", "至少", "至多", "约")


def _first_number_after(
    text: str,
    keyword_re: re.Pattern[str],
    unit: str | None,
    stats: dict[str, int] | None = None,
) -> Decimal | None:
    """取关键词之后出现的第一个"带单位声明"的数字（支持千分位/全角/百分号）。

    * ``unit == "%"`` —— 数字后 4 字符内必须出现 ``%``（否则不是"实测百分比"声明）；
    * ``unit == "元"`` —— 数字后 4 字符内必须出现 ``元``；
    * ``unit is None`` —— 不校验单位。

    ``stats["excluded"]`` 统计因"阈值/区间/缺单位"被排除的候选（③ 非实测值）。
    """
    def _bump() -> None:
        if stats is not None:
            stats["excluded"] = stats.get("excluded", 0) + 1

    m = keyword_re.search(text)
    if not m:
        return None
    tail = _normalize_text(text[m.end():])
    nm = re.search(r"([+\-]?\d[\d,]*(?:\.\d+)?)\s*(.{0,4})", tail)
    if not nm:
        return None
    before = tail[max(0, nm.start(1) - 4): nm.start(1)]
    if any(op in before for op in _COMPARISON_OPS) or any(w in before for w in _THRESHOLD_WORDS):
        _bump()
        return None                        # 阈值/约束（如 "<35%"、"超过 400%"）非实测值
    after = tail[nm.end(1): nm.end(1) + 2]
    if re.match(r"\s*[-~]\s*\d", after):
        _bump()
        return None                        # 区间（如 "5-8%"、"25-35%"）非单点实测值
    if unit and unit not in nm.group(2):
        _bump()
        return None
    raw = nm.group(1).replace(",", "")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _doc_references_other_strategy(path: Path, text: str) -> bool:
    """判断文档是否在讲"非 T312 红利权威产物"的其它策略/区间。

    若是，则其指标应对照**它自己的产物**（若在 ``experiments/runs/`` 不存在，
    归入"所引产物不存在"类别），而不是对照 T312 红利权威产物。
    """
    has_dividend_authority = ("红利" in text) or ("T312" in text) or ("20260907-150402" in text)
    has_momentum = ("动量" in text) or ("Momentum" in text) or ("momentum" in text)
    other_named = any(k in path.name.lower() for k in (
        "momentum", "t304", "t305", "phase35", "task_completion_summary",
    ))
    return other_named or (has_momentum and not has_dividend_authority)


class DocMetricConsistencyGate(BaseGate):
    """G-DOC-1: 文档关键指标 ↔ 权威产物一致性门禁（CRITICAL）。

    抽取 ``docs/**/*.md``（含计划仓 ``tasks.md`` 镜像）中形如
    ``年化换手率 92.51%`` / ``最大回撤 MDD 15.23%`` 的关键指标，
    与 ``experiments/runs/*.json``（权威产物）真值比对；不一致即 FAIL，
    并指向**具体文件与行号**。行尾 ``<!-- gate-doc-ignore: <非空理由> -->`` 可显式豁免
    **本行**（⛔ 理由为空/裸标记不生效；豁免行数经 ``ignored_lines`` 可见计数）。

    此外（任务 3 补充）核验**文档声称的门禁数量 / 单测基线**：
    ``N 道门禁``（含机读/自动闸门/六维等变体）必须等于
    ``GateMasterAudit.get_standard_gates()`` 的**动态长度**；``N passed`` / ``基线 N``
    必须等于单一事实源 ``constants.TEST_BASELINE_PASSED``。历史归档快照走白名单并可见计数。
    """

    gate_id = "G-DOC-1"
    name = "文档数字与产物一致性门禁"
    category = GateCategory.G_GATE
    severity = GateSeverity.CRITICAL
    evidence = "roadmap_decision.md §4 G-DOC-1 + 审计 §6.3：同一 T312 回测在仓内存在三套互斥指标"
    threshold_desc = "md 中关键指标（CAGR/MDD/换手/胜率/费用/本金/净值）必须与权威产物一致"

    def evaluate(self, context: Any = None) -> GateResult:
        truth = _resolve_truth_artifact(context)
        if truth is None:
            return self._make(GateStatus.INCONCLUSIVE, "未找到权威产物（experiments/runs/*.json），无法比对文档指标")
        truth_metrics = truth["metrics"]
        truth_path = truth["_path"]

        # 三类：① 与所引（存在的）产物不符 = 真实违规；② 所引产物不存在；③ 非实测值（阈值/区间）排除。
        violations: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        declarations: list[dict[str, Any]] = []   # ④ 门禁数量/单测基线声明漂移（任务 3 补充）
        stats: dict[str, int] = {}
        files = _doc_files(context)
        void_docs = 0                            # 作废文档（gate-doc-void 标记）⛔ 必须可见计数
        historical_docs = 0                      # 历史归档快照（显式白名单）⛔ 必须可见计数
        ignored_keys: set[tuple[str, int]] = set()   # 行内 ignore 豁免（(文件,行号) 去重，必须可见）
        dated_log_keys: set[tuple[str, int]] = set()  # 带日期的任务日志行（当时快照，(文件,行号) 去重）
        exp_gate_count = expected_gate_count(context)        # 动态：GateMasterAudit 实际长度
        exp_baseline = expected_test_baseline(context)       # 单一事实源常量
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:                # noqa: BLE001
                continue
            if is_void_doc(text):            # 已作废文档：跳过数字比对（但计数可见）
                void_docs += 1
                continue
            is_hist = _is_historical_snapshot(path)
            if is_hist:
                historical_docs += 1
            else:
                # ④ 门禁数量 / 单测基线声明漂移（结构化匹配；历史快照豁免；ignore/void 计数可见）。
                decl_stats: dict[str, Any] = {}
                for decl in _declaration_violations_in_text(
                    text, exp_gate_count, exp_baseline, decl_stats,
                ):
                    declarations.append({"file": str(path), **decl})
                for ln in decl_stats.get("ignored_linenos", set()):
                    ignored_keys.add((str(path), int(ln)))
                for ln in decl_stats.get("dated_log_linenos", set()):
                    dated_log_keys.add((str(path), int(ln)))
            lines = text.splitlines()
            other_strategy = _doc_references_other_strategy(path, text)
            truth_id = _RUN_ID_RE.search(Path(truth_path).name)
            truth_id_token = truth_id.group(1) if truth_id else None
            for lineno, line in enumerate(lines, start=1):
                if any(phrase in line for phrase in _DOC_LINE_SKIP_PHRASES):
                    continue
                if line_ignore_reason(line) is not None:    # 行内豁免（须带非空理由）
                    ignored_keys.add((str(path), lineno))
                    continue
                # 行内若引用别的 run 标识（描述的是另一份产物），跳过避免误报。
                line_ids = set(_RUN_ID_RE.findall(line))
                if truth_id_token and line_ids and truth_id_token not in line_ids:
                    continue
                for metric_name, kw, key, unit in _DOC_METRIC_SPECS:
                    if key not in truth_metrics:
                        continue
                    kw_re = re.compile(kw)
                    if not kw_re.search(line):
                        continue
                    doc_val = _first_number_after(line, kw_re, unit, stats)
                    if doc_val is None:
                        continue
                    truth_val = _to_decimal(truth_metrics.get(key))
                    if truth_val is None:
                        continue
                    is_pct = unit == "%"
                    expect = truth_val * 100 if is_pct else truth_val
                    tol = _DOC_PCT_TOL if is_pct else _DOC_ABS_TOL
                    # 行内若已出现真值（如"本文写 92.51% / 真值 201.14%"），说明该行
                    # 已在陈述真值，不构成"文档载有与产物不符的换手率"，跳过。
                    if any(abs(n - expect) <= tol for n in _line_numbers(line)):
                        continue
                    if abs(doc_val - expect) > tol:
                        entry = {
                            "file": str(path),
                            "lineno": lineno,
                            "metric": metric_name,
                            "doc_value": str(doc_val),
                            "expected": str(expect.quantize(Decimal("0.01"))),
                            "line": line.strip()[:120],
                        }
                        # ② 文档讲的是"别的策略/区间"，其自身产物在 experiments/runs 不存在
                        if other_strategy:
                            entry["referenced_artifact"] = "非 T312 红利产物（experiments/runs/ 无对应产物）"
                            unresolved.append(entry)
                        else:
                            violations.append(entry)

        excluded = stats.get("excluded", 0)
        ignored_lines = len(ignored_keys)         # (文件,行号) 去重后可见计数
        # 去重（同文同值的门禁数量/基线声明只保留首处行号）。
        seen_decl: set[tuple[str, str, str]] = set()
        unique_decl: list[dict[str, Any]] = []
        for d in declarations:
            key = (d["file"], d["metric"], d["doc_value"])
            if key in seen_decl:
                continue
            seen_decl.add(key)
            unique_decl.append(d)
        declarations = unique_decl

        dated_log_lines = len(dated_log_keys)     # 带日期的任务日志行（当时快照，可见计数）
        # ㊲ 排除面**可见计数**：`_EXCLUDED_DOC_DIRS` 曾是唯一"静默豁免"通道（无计数、无清单）。
        excluded_dir_files = len(_excluded_doc_files())

        head = (
            f"void_docs: {void_docs}（gate-doc-void 豁免，必须可见）；"
            f"historical_snapshot_docs: {historical_docs}（历史归档快照白名单，必须可见）；"
            f"ignored_lines: {ignored_lines}（行内 gate-doc-ignore 豁免，必须可见）；"
            f"dated_task_log_lines: {dated_log_lines}（带日期的提交日志行＝当时快照，必须可见）；"
            f"excluded_dir_files: {excluded_dir_files}（目录白名单 {_EXCLUDED_DOC_DIRS} 整片免检，必须可见）；"
            f"④ 门禁数量/单测基线声明漂移 {len(declarations)} 处"
            f"（期望 门禁数={exp_gate_count}、基线={exp_baseline}）"
            f"；① 真实违规 {len(violations)} 处（M4/M5 待重写）；"
            f"② 所引产物不存在 {len(unresolved)} 处；③ 非实测值（阈值/区间）排除 {excluded} 处"
        )

        # ⛔ 豁免上限（QA ㉚）：豁免文档数超阈值 ⇒ 判 FAIL（防"把一切标 void 来消掉门禁"）。
        if void_docs > MAX_VOID_DOCS:
            return self._make(
                GateStatus.FAIL,
                f"豁免文档数 {void_docs} 超过上限 {MAX_VOID_DOCS}（gate-doc-void 滥用嫌疑）—— {head}",
                metrics={
                    "void_docs": void_docs,
                    "max_void_docs": MAX_VOID_DOCS,
                    "historical_snapshot_docs": historical_docs,
                    "ignored_lines": ignored_lines,
                    "dated_task_log_lines": dated_log_lines,
                    "excluded_dir_files": excluded_dir_files,
                    "truth_run": Path(truth_path).name,
                },
            )

        if violations or declarations:
            first = violations[0] if violations else declarations[0]
            kind = "① 指标" if violations else "④ 声明"
            return self._make(
                GateStatus.FAIL,
                (
                    f"检出文档与权威产物 {Path(truth_path).name} / 单一事实源不符 —— {head}。"
                    f"{kind} 首例：{Path(first['file']).name}:{first['lineno']} {first['metric']} "
                    f"文档={first['doc_value']} ≠ 期望={first['expected']}"
                ),
                metrics={
                    "void_docs": void_docs,
                    "historical_snapshot_docs": historical_docs,
                    "ignored_lines": ignored_lines,
                    "dated_task_log_lines": dated_log_lines,
                    "expected_gate_count": exp_gate_count,
                    "expected_test_baseline": exp_baseline,
                    "excluded_dir_files": excluded_dir_files,
                    "violations_total": len(violations),
                    "declaration_violations_total": len(declarations),
                    "unresolved_artifacts_total": len(unresolved),
                    "excluded_nonmeasure_total": excluded,
                    "violations": violations[:50],
                    "declaration_violations": declarations[:50],
                    "unresolved_artifacts": unresolved[:50],
                    "truth_run": Path(truth_path).name,
                },
            )

        if unresolved:
            first = unresolved[0]
            return self._make(
                GateStatus.INCONCLUSIVE,
                (
                    f"未发现与权威产物直接冲突的指标，但检出文档引用了不存在的其它策略产物 —— {head}。"
                    f"② 首例：{Path(first['file']).name}:{first['lineno']} {first['metric']}="
                    f"{first['doc_value']}（{first.get('referenced_artifact')}）"
                ),
                metrics={
                    "void_docs": void_docs,
                    "historical_snapshot_docs": historical_docs,
                    "ignored_lines": ignored_lines,
                    "dated_task_log_lines": dated_log_lines,
                    "expected_gate_count": exp_gate_count,
                    "expected_test_baseline": exp_baseline,
                    "excluded_dir_files": excluded_dir_files,
                    "violations_total": 0,
                    "declaration_violations_total": 0,
                    "unresolved_artifacts_total": len(unresolved),
                    "excluded_nonmeasure_total": excluded,
                    "unresolved_artifacts": unresolved[:50],
                    "truth_run": Path(truth_path).name,
                },
            )

        return self._make(
            GateStatus.PASS,
            f"已扫描 {len(files)} 份文档，关键指标与权威产物 {Path(truth_path).name} 全部一致（{head}）",
            metrics={
                "void_docs": void_docs,
                "historical_snapshot_docs": historical_docs,
                "ignored_lines": ignored_lines,
                "dated_task_log_lines": dated_log_lines,
                "expected_gate_count": exp_gate_count,
                "expected_test_baseline": exp_baseline,
                "excluded_dir_files": excluded_dir_files,
                "scanned_files": len(files),
                "excluded_nonmeasure_total": excluded,
                "truth_run": Path(truth_path).name,
            },
        )

    def _make(self, status: GateStatus, message: str, metrics: dict[str, Any] | None = None) -> GateResult:
        return GateResult(
            gate_id=self.gate_id, name=self.name, category=self.category,
            status=status, severity=self.severity, message=message,
            metrics=metrics or {}, threshold=self.threshold_desc, evidence=self.evidence,
        )


# ---------------------------------------------------------------------------
# G-REF-1 引用路径存在性门禁
# ---------------------------------------------------------------------------

#: 仓内路径前缀（顶层目录白名单）。
_REPO_PATH_PREFIXES = (
    "data", "scripts", "tests", "docs", "ops", "backtest",
    "strategy", "reporting", "paper_trading", "finai", "experiments", "runs",
)

_REPO_PATH_RE = re.compile(
    r"(?<![\w/.\-])(" + "|".join(_REPO_PATH_PREFIXES) + r")/[A-Za-z0-9_\-./%]+"
)

_TRAILING_JUNK = " \t\"'`),;:。，、）】]}>|*"

#: 认可的仓内文件扩展名。⛔ 只认这些扩展名，避免把 ``data/collector._atomic_write_parquet``
#: 这类"模块.方法名"误判成路径。
_ALLOWED_REF_EXTS: frozenset[str] = frozenset({
    ".py", ".md", ".json", ".jsonl", ".parquet", ".html", ".htm", ".txt",
    ".yml", ".yaml", ".csv", ".toml", ".cfg", ".ini", ".ipynb",
})


def _clean_reference(raw: str) -> str:
    ref = raw.strip()
    while ref and ref[-1] in _TRAILING_JUNK:
        ref = ref[:-1]
    while ref.startswith("./"):
        ref = ref[2:]
    while ref.startswith("../"):
        ref = ref[3:]
    return ref


def _is_whitelisted(ref: str) -> bool:
    return any(ref.startswith(prefix) for prefix in _REFERENCE_WHITELIST_PREFIXES)


def _is_low_risk_path_drift(ref: str) -> bool:
    """低危：设计文档代码片段里的路径与实现不一致（如 `paper_trading/state.json`
    实为 `runs/paper_trading/state.json`）——真缺陷但性质为"文档↔实现不一致"。
    """
    return ref.startswith("paper_trading/")


class DocPathReferenceGate(BaseGate):
    """G-REF-1: 文档引用路径存在性门禁（CRITICAL）。

    抽取 md 中形如 ``data/...`` / ``scripts/...`` / ``tests/...`` / ``docs/...``
    的**仓内路径**并断言 ``exists()``。白名单仅允许「运行期生成物」
    （``experiments/runs/*.json``、``runs/paper_trading/*``）。不存在即 FAIL。
    """

    gate_id = "G-REF-1"
    name = "文档引用路径存在性门禁"
    category = GateCategory.G_GATE
    severity = GateSeverity.CRITICAL
    evidence = "roadmap_decision.md §4 G-REF-1 + 审计 N6：22 处幽灵引用（data/stress_test/ 等）"
    threshold_desc = "md 引用的仓内路径必须 exists()；白名单仅允许运行期生成物"

    def evaluate(self, context: Any = None) -> GateResult:
        repo_root = _REPO_ROOT
        violations: list[dict[str, Any]] = []
        whitelisted_hits = 0
        negated_hits = 0                         # 否定语境豁免行数（如实披露"某路径不存在"，⛔ 必须可见）
        void_docs = 0                            # 作废文档（gate-doc-void 标记）⛔ 必须可见计数
        ignored_lines = 0                        # 行内 ignore 豁免行数（⛔ 必须可见，不得静默隐藏）
        files = _doc_files(context)

        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:                # noqa: BLE001
                continue
            if is_void_doc(text):            # 已作废文档：跳过路径存在性比对（但计数可见）
                void_docs += 1
                continue
            lines = text.splitlines()
            for lineno, line in enumerate(lines, start=1):
                if line_ignore_reason(line) is not None:    # 行内豁免（须带非空理由，仅本行）
                    ignored_lines += 1
                    continue
                for m in _REPO_PATH_RE.finditer(line):
                    ref = _clean_reference(m.group(0))
                    if not ref or "..." in ref or "YYYY" in ref:
                        continue
                    if "*" in ref or "{" in ref or "}" in ref or "<" in ref:
                        continue
                    # 仅接受"目录（以 / 结尾）"或"带认可扩展名的文件"，
                    # ⛔ 排除 `模块.方法名`（如 collector._atomic_write_parquet）与散文误报。
                    last_seg = ref.rstrip("/").split("/")[-1]
                    is_dir = ref.endswith("/")
                    ext = ("." + last_seg.rsplit(".", 1)[1].lower()) if "." in last_seg else ""
                    has_ext = ext in _ALLOWED_REF_EXTS
                    if not (is_dir or has_ext):
                        continue
                    if _is_whitelisted(ref):
                        whitelisted_hits += 1       # 档 C：运行期产物（白名单）
                        continue
                    # 否定语境：同行明确声明该路径不存在/未实现 ⇒ 属如实披露，⛔ 不算幽灵引用。
                    if any(marker in line for marker in _NEGATED_REFERENCE_MARKERS):
                        negated_hits += 1
                        continue
                    target = repo_root / ref
                    if not target.exists():
                        violations.append({
                            "file": str(path),
                            "lineno": lineno,
                            "reference": ref,
                            "line": line.strip()[:120],
                            # 档 A 幽灵证据引用(高危) / 档 B 文档-实现路径不一致(低危)
                            "tier": "LOW_RISK_PATH_DRIFT" if _is_low_risk_path_drift(ref) else "PHANTOM_EVIDENCE_REF",
                        })

        # 去重（同一引用多处出现只报首个行号）
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for v in violations:
            if v["reference"] in seen:
                continue
            seen.add(v["reference"])
            unique.append(v)

        elevated = [v for v in unique if v["tier"] == "PHANTOM_EVIDENCE_REF"]
        low_risk = [v for v in unique if v["tier"] == "LOW_RISK_PATH_DRIFT"]

        # ⛔ 豁免上限（QA ㉚）：豁免文档数超阈值 ⇒ 判 FAIL（防"把一切标 void 来消掉门禁"）。
        if void_docs > MAX_VOID_DOCS:
            return self._make(
                GateStatus.FAIL,
                (
                    f"豁免文档数 {void_docs} 超过上限 {MAX_VOID_DOCS}（gate-doc-void 滥用嫌疑）—— "
                    f"void_docs: {void_docs}（gate-doc-void 豁免，必须可见）；"
                    f"ignored_lines: {ignored_lines}（行内豁免，必须可见）；max={MAX_VOID_DOCS}"
                ),
                metrics={
                    "void_docs": void_docs,
                    "max_void_docs": MAX_VOID_DOCS,
                    "ignored_lines": ignored_lines,
                    "violations_total": len(unique),
                    "scanned_files": len(files),
                },
            )

        if unique:
            return self._make(
                GateStatus.FAIL,
                (
                    f"检出 {len(unique)} 处文档引用了不存在的仓内路径 —— "
                    f"void_docs: {void_docs}（gate-doc-void 豁免，必须可见）；"
                    f"ignored_lines: {ignored_lines}（行内 gate-doc-ignore 豁免，必须可见）；"
                    f"档A 幽灵证据引用(高危) {len(elevated)} 处；"
                    f"档B 文档-实现路径不一致(低危) {len(low_risk)} 处；"
                    f"档C 运行期产物(白名单) {whitelisted_hits} 处已豁免；"
                    f"档D 否定语境(如实披露该路径不存在) {negated_hits} 处已豁免（必须可见）。"
                    f"首例：{unique[0]['reference']}（{unique[0]['tier']}）"
                ),
                metrics={
                    "void_docs": void_docs,
                    "ignored_lines": ignored_lines,
                    "violations_total": len(unique),
                    "elevated_total": len(elevated),
                    "low_risk_total": len(low_risk),
                    "whitelisted_skipped_total": whitelisted_hits,
                    "negated_skipped_total": negated_hits,
                    "violations": unique[:50],
                    "elevated": elevated[:50],
                    "low_risk": low_risk[:50],
                    "scanned_files": len(files),
                },
            )
        return self._make(
            GateStatus.PASS,
            f"已扫描 {len(files)} 份文档，引用路径全部存在"
            f"（白名单豁免 {whitelisted_hits} 处；否定语境豁免 {negated_hits} 处；"
            f"void_docs: {void_docs}（gate-doc-void 豁免，必须可见）；"
            f"ignored_lines: {ignored_lines}（行内 gate-doc-ignore 豁免，必须可见））",
            metrics={
                "void_docs": void_docs,
                "ignored_lines": ignored_lines,
                "scanned_files": len(files),
                "whitelisted_skipped_total": whitelisted_hits,
                "negated_skipped_total": negated_hits,
            },
        )

    def _make(self, status: GateStatus, message: str, metrics: dict[str, Any] | None = None) -> GateResult:
        return GateResult(
            gate_id=self.gate_id, name=self.name, category=self.category,
            status=status, severity=self.severity, message=message,
            metrics=metrics or {}, threshold=self.threshold_desc, evidence=self.evidence,
        )


# ---------------------------------------------------------------------------
# CLI 演示入口（验收用）
# ---------------------------------------------------------------------------

def _print_result(res: GateResult) -> None:
    print(f"[{res.status.value}] {res.gate_id} {res.name}")
    print(f"  message : {res.message}")
    if res.metrics:
        print(f"  metrics : {json.dumps(res.metrics, ensure_ascii=False)[:800]}")


def main(argv: Sequence[str] | None = None) -> int:
    from .base import is_blocking_result

    parser = argparse.ArgumentParser(description="FinAI2.0 P0 一致性门禁（G-MDD-1/G-DOC-1/G-STRESS-1/G-REF-1）")
    parser.add_argument("--mdd", type=str, default="", help="对指定回测产物运行 G-MDD-1")
    parser.add_argument("--doc", action="store_true", help="运行 G-DOC-1（文档↔产物一致性）")
    parser.add_argument("--ref", action="store_true", help="运行 G-REF-1（引用路径存在性）")
    parser.add_argument("--stress-rt", type=int, default=None, help="G-STRESS-1 压测 round_trips")
    parser.add_argument("--stress-days", type=int, default=None, help="G-STRESS-1 压测交易日数")
    parser.add_argument("--strict", action="store_true",
                        help="阻断模式（默认即 fail-closed：FAIL/INCONCLUSIVE 均非零退出；此参数为对称兼容）")
    args = parser.parse_args(argv)

    # ⛔ 统一 fail-closed：FAIL **或** INCONCLUSIVE 均非零退出（与 gate_master_audit --strict 对齐）
    results: list[GateResult] = []
    if args.mdd:
        results.append(MaxDrawdownCeilingGate().evaluate({"artifact_path": args.mdd}))
    if args.doc:
        results.append(DocMetricConsistencyGate().evaluate({}))
    if args.ref:
        results.append(DocPathReferenceGate().evaluate({}))
    if args.stress_rt is not None or args.stress_days is not None:
        results.append(StressValidityGate().evaluate({"round_trips": args.stress_rt, "trading_days": args.stress_days}))

    if not results:
        parser.print_help()
        return 0

    blocking = False
    for res in results:
        _print_result(res)
        if is_blocking_result(res):
            blocking = True
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
