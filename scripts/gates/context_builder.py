#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁上下文构建器（推送期 / CI 共用）——从**仓库现状**真实取证。

`pre_push.run_master_gate_guard()` 与 CI（`.github/workflows/*.yml` → `gate_master_audit --ci`）
复用本模块，避免"推送口真取证、CI 空 context"两套语义（否则 CI 会永久红 ⇒ 被绕过）。

取证来源：

1. ``experiments/runs/*.json`` 最新（优先带 ``anti_tamper_signature``）产物 →
   ``run_record`` + ``annualized_turnover`` + 费用分项 + ``data_hash``；
2. Git HEAD + ISO 时间戳（出处三件套）；
3. E-1 五必挂探针 / E-2 送转全额卖出探针（真跑引擎）；
4. ``docs/spec/.../tasks.md`` → G-2 全文档验签；``scripts/run_dividend_backtest.py`` → L-3 静态源；
5. ``data/dividend_stocks/`` 抽样现读 parquet → D-1~D-4 真实证据。

仍取不到证的（逐日仓位/逐日现金流/委托明细/分红分档/独立黄金基准/压测产物）**不填**，
交由门禁判 INCONCLUSIVE，并按 ``ci_policy`` 决定阻断或只告警。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .base import GateResult, GateStatus

#: 可"静态/推送期"取证的门禁：FAIL 或 INCONCLUSIVE 都应阻断。
#: ⚠ M3 门禁改造（任务 1 三层分层）：``G-MDD-1`` **移出**本集合——推送代码不产生回撤，
#: 它是**研究质量**门禁而非**工程诚实性**门禁；其 BLOCKER 能力下移到准入层
#: （``scripts/gates/acceptance.py``）。（``G-REPRO-1`` 消费 run 产物，归 RUN_EVIDENCE。）
#: ⚠ ㊱：``S-1``（换手硬顶）**移出**本集合至 RUN_EVIDENCE——其判据 ``annualized_turnover``
#: **只由 run 产物提供**（``ctx["annualized_turnover"]`` 来自产物 metrics），与 G-MDD-1
#: 被移出的理由同构；留在 STATIC 会让 ``experiments/runs/`` 一空就 INCONCLUSIVE ⇒ CI 永久红。
#: 注：``L-3`` **保留**在 RUN_EVIDENCE——其 ``FAIL``（静态 AST 缺关键调用）在 ci_policy 里
#: **本就阻断**（FAIL 不受分类影响），只有"静态可达但无 ``executed_calls``"这一**确需运行期
#: 追踪**的情形为 INCONCLUSIVE；改判 STATIC 会让该情形永久红，属误伤。
STATIC_GATE_IDS: frozenset[str] = frozenset({
    "D-1", "D-2", "D-3", "D-4", "E-1", "E-2",
    "G-1", "G-2", "G-3", "G-4", "G-DOC-1", "G-REF-1",
})

#: 需"run 内部真相"（逐日仓位/现金流/委托明细/送转全量/独立黄金基准/运行期追踪/分红分档/压测产物）
#: 的门禁：CI 无产物时 INCONCLUSIVE **只告警不阻断**；有产物则照常判 PASS/FAIL。
#: ``G-MDD-1`` 在此归类（其判定依赖 run 产物 metrics，且见 :data:`WARN_GATE_IDS` 降级）。
RUN_EVIDENCE_GATE_IDS: frozenset[str] = frozenset({
    "D-5", "L-1", "L-2", "L-3", "E-3", "A-1", "A-2", "A-3", "A-4",
    "S-1", "S-2", "S-3", "S-4", "S-5", "G-STRESS-1", "G-MDD-1", "G-REPRO-1",
})

#: 三层分层（M3 任务 1）：**推送 / CI 期 WARN（展示但不阻断）**的门禁。
#: 其结果始终展示（``[WARN] ...``）但**不产生阻断退出码**；BLOCKER 能力保留在准入层。
#: ``G-MDD-1``：推送被 43.08% 回撤永久阻断 ⇒ 必逼出逃生阀 ⇒ 门禁沦为摆设，故降级为 WARN。
WARN_GATE_IDS: frozenset[str] = frozenset({"G-MDD-1"})

# ---------------------------------------------------------------------------
# 数据取证根：真实数据优先，CI 最小 fixture 回退（GATE-R8）
# ---------------------------------------------------------------------------
#: CI 最小数据 fixture 相对仓库根的路径。
#:
#: **为什么需要它**：``.gitignore`` 排除了 ``data/dividend_stocks/``（采集落盘、可再生），
#: 于是 GitHub Actions 上该目录**完全不存在**（本地 490 个文件 / CI 0 个）⇒
#: D-1~D-4 无处取证判 INCONCLUSIVE、G-1 的 ``data_hash`` 取不到 ⇒ CI **永久红**。
#: 红的是"没数据"而不是"数据有问题"，门禁既没在判、又堵住流水线 —— 两头落空。
#:
#: **它是什么**：抽样自 ``data/dividend_stocks`` **真实数据**的小样
#: （30 只标的 × 130 个交易日，生成脚本 ``scripts/build_ci_fixture_data.py``，
#: 详见 ``tests/fixtures/ci_min_data/FIXTURE_PROVENANCE.md``）。
#:
#: ⛔ **如实登记**：CI 上数据类门禁校验的是**这份抽样小样**，
#: **不等于**校验全量真实数据质量；全量校验仍须在本地 ``data/`` 上跑同一条命令。
#: ⛔ **不豁免、不放宽策略**：门禁阈值与判据一行未改，只是让 CI 有**真东西可判**。
CI_FIXTURE_DATA_REL: tuple[str, ...] = ("tests", "fixtures", "ci_min_data", "dividend_stocks")

#: fixture 清单文件名（位于 fixture 根的**上一级**目录）。
CI_FIXTURE_MANIFEST_NAME: str = "fixture_manifest.json"


def _has_symbol_dirs(data_dir: Path) -> bool:
    """``data_dir`` 下是否存在至少一个 ``sh.*`` / ``sz.*`` 标的目录（有数据可取证）。"""
    if not data_dir.exists() or not data_dir.is_dir():
        return False
    return any(p.is_dir() and p.name.startswith(("sh.", "sz.")) for p in data_dir.iterdir())


def _fixture_label(fixture_root: Path, repo_root: Path) -> str:
    """读取 fixture 清单，生成**自述式**取证来源标签（规模与合成成分必须可见）。"""
    try:
        rel = "/".join(fixture_root.relative_to(repo_root).parts)
    except ValueError:                          # noqa: BLE001 —— 无法取相对路径则退回绝对路径
        rel = str(fixture_root)
    manifest_path = fixture_root.parent / CI_FIXTURE_MANIFEST_NAME
    base = f"CI 最小 fixture {rel}"
    if not manifest_path.exists():
        return f"{base}（⛔ 缺清单 {CI_FIXTURE_MANIFEST_NAME}，规模不可自述）"
    try:
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:                           # noqa: BLE001
        return f"{base}（⛔ 清单解析失败）"
    syms = m.get("total_symbols")
    days = m.get("days_per_symbol")
    synth = (m.get("synthetic_suspension_days") or {}).get("count", 0)
    return (
        f"{base}（抽样自真实数据 {syms} 只 × {days} 天"
        f"；含合成停牌日 {synth} 天 —— 真实集 2015-2024 全池 tradestatus 均为 '1'，无停牌日可抽样）"
    )


def resolve_data_root(repo_root: Path) -> tuple[Path, str]:
    """确定数据取证根：**真实数据优先，缺失才回退 CI 最小 fixture**。

    ⛔ 本地 ``data/dividend_stocks`` 存在时行为**完全不变**（真实数据优先）；
    fixture **仅**在该目录不存在/无标的目录时启用，且来源**显式可见**（不得静默）。

    Returns:
        ``(data_root, 取证来源标签)``。两者皆无数据时返回真实数据路径 +
        "数据缺失"标签 —— **不编造任何路径**，交由各门禁判 INCONCLUSIVE。
    """
    real_dir = repo_root / "data" / "dividend_stocks"
    if _has_symbol_dirs(real_dir):
        n = sum(1 for p in real_dir.iterdir() if p.is_dir() and p.name.startswith(("sh.", "sz.")))
        return real_dir, f"真实数据 data/dividend_stocks（{n} 只标的）"

    fixture_dir = repo_root.joinpath(*CI_FIXTURE_DATA_REL)
    if _has_symbol_dirs(fixture_dir):
        return fixture_dir, _fixture_label(fixture_dir, repo_root)

    return real_dir, "数据缺失（真实数据 data/dividend_stocks 与 CI fixture 均无标的目录）"


def build_repo_context(repo_root: Path | str | None = None) -> tuple[dict[str, Any], str]:
    """从仓库现状构建门禁 ctx；返回 ``(ctx, 取证来源说明)``。"""
    root = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parents[2]
    ctx: dict[str, Any] = {}
    source = "无回测产物（仅静态证据）"

    # 出处三件套：Git SHA + Timestamp
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(root), text=True, stderr=subprocess.DEVNULL
        ).strip()
        if len(out) >= 7:
            ctx["git_commit"] = out
    except Exception:                       # noqa: BLE001
        pass
    ctx["timestamp"] = _now_iso()

    # 数据取证根（GATE-R8）：真实数据优先，CI 无 data/ 时回退最小 fixture。
    # ⛔ 取证来源必须显式可见（打印 + 写进 ctx + 并入 source），不得静默切换。
    data_root, data_source = resolve_data_root(root)
    ctx["data_snapshot_root"] = str(data_root)
    ctx["data_evidence_source"] = data_source
    print(f"[数据取证] 门禁取证来源: {data_source}")
    if data_root != root / "data" / "dividend_stocks":
        # 非真实数据时**再重复一次**到 stdout：CI 日志里这条必须一眼可见，
        # 防止"CI 绿了"被误读为"已校验全量真实数据质量"。
        print(
            "[数据取证] ⚠ 注意：本次数据类门禁（D-1~D-4 / G-1 data_hash）校验的是 "
            "**抽样小样**，⛔ 不等于校验全量真实数据质量。"
        )

    # 最新签名产物
    runs_dir = root / "experiments" / "runs"
    artifacts: list[tuple[str, Path, dict]] = []
    if runs_dir.exists():
        for p in sorted(runs_dir.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:               # noqa: BLE001
                continue
            if isinstance(d, dict) and d.get("metrics"):
                artifacts.append((str(d.get("timestamp", "")), p, d))

    signed = [a for a in artifacts if a[2].get("anti_tamper_signature")]
    pool = signed or artifacts
    if pool:
        pool.sort(key=lambda a: a[0])
        _, path, record = pool[-1]
        metrics = record.get("metrics", {}) or {}
        ctx["run_record"] = record
        if metrics.get("annual_turnover") is not None:
            ctx["annualized_turnover"] = float(metrics["annual_turnover"])
        ctx["total_return"] = metrics.get("total_return")
        ctx["code_evidence"] = f"experiments/runs/{path.name}::metrics"
        fees_total = metrics.get("fees_total") or {}
        if fees_total:
            ctx["total_stamp_tax"] = str(fees_total.get("STAMP_TAX", "0"))
            ctx["total_commission"] = str(fees_total.get("COMMISSION", "0"))
        # 出处 data_hash 统一口径（QA ㉙）：优先取产物的内容寻址 data_hash；
        # legacy 无该字段时，退回与 registry **同一函数** provenance.hash_path_manifest
        # （⛔ 不再用「data_version+params_hash」的第三种合成值——同名不同算法无法互认）。
        data_hash = record.get("data_hash")
        if not data_hash:
            try:
                from reporting.provenance import hash_path_manifest

                # 与 D-1~D-4 **同一**数据根（真实数据优先 / CI fixture 回退），
                # 保证 data_hash 描述的正是本次被门禁实际校验的那份快照。
                data_hash = hash_path_manifest(data_root)
            except Exception:                # noqa: BLE001 —— 取不到则交门禁判 INCONCLUSIVE
                data_hash = None
        if data_hash:
            ctx["data_hash"] = str(data_hash)
        source = f"产物 {path.name}（{'已签名' if record.get('anti_tamper_signature') else '未签名'}）"

    # E-1 五必挂真跑
    try:
        from .must_fail_probe import run_must_fail_cases

        outcome = run_must_fail_cases()
        ctx["must_fail_results"] = outcome
        ctx["failed_cases"] = [k for k, v in outcome.items() if not v]
    except Exception:                       # noqa: BLE001
        pass

    # E-2 送转全额卖出探针
    try:
        from .must_fail_probe import run_split_fifo_probe

        fifo_errors, final_positions = run_split_fifo_probe()
        ctx["fifo_errors"] = fifo_errors
        ctx["final_positions"] = final_positions
    except Exception:                       # noqa: BLE001
        pass

    # D-1~D-4 抽样现读 parquet（数据根已在上方解析：真实数据优先 / CI fixture 回退）
    try:
        _sample_data_evidence(ctx, data_root)
    except Exception:                       # noqa: BLE001
        pass

    tasks = root / "docs" / "spec" / "001-a-stock-longonly-daily-quant" / "tasks.md"
    if tasks.exists():
        ctx["tasks_path"] = str(tasks)

    run_file = root / "scripts" / "run_dividend_backtest.py"
    if run_file.exists():
        ctx["source_code"] = run_file.read_text(encoding="utf-8")
        ctx["required_calls"] = ["BacktestBroker", "MatchEngine", "compute_metrics"]

    # 取证来源合并进 source：``gate_master_audit --ci`` 与 ``pre_push`` 都会打印它。
    source = f"{source}；数据取证: {data_source}"
    return ctx, source


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def collect_data_evidence(data_dir: Path) -> dict[str, Any]:
    """从**给定数据根**抽样现读 parquet，返回 D-1~D-4 的证据片段。

    抽取逻辑与 ``data/dividend_stocks/`` 完全一致（同一函数、同一阈值口径），
    使 "真实数据" 与 "CI 最小 fixture" 两条路径**判的是同一件事**——
    ⛔ 不得为 fixture 单独放宽样本口径（那样 CI 绿了也是在自欺）。
    """
    import pandas as pd

    evidence: dict[str, Any] = {}
    if not data_dir.exists():
        return evidence
    sym_dirs = sorted(d for d in data_dir.iterdir() if d.is_dir() and d.name.startswith(("sh.", "sz.")))

    # D-1 / D-4：取前 1 只标的的日线
    for sym_dir in sym_dirs:
        parts = sorted(p for p in sym_dir.glob("*.parquet") if p.stem.isdigit())
        if not parts:
            continue
        df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
        if "date" not in df.columns:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        exdiv_dates: set[str] = set()
        sidecar = data_dir / "exdiv" / f"{sym_dir.name}.parquet"
        if sidecar.exists():
            ev = pd.read_parquet(sidecar)
            if "date" in ev.columns:
                exdiv_dates = {str(x)[:10] for x in ev["date"].tolist()}
        bars = []
        for i, row in enumerate(df.itertuples()):
            d_str = str(getattr(row, "date"))[:10]
            bars.append({
                "date": d_str,
                "close": float(getattr(row, "close")),
                "is_exdiv": (d_str in exdiv_dates) or i < 5,
                "tradestatus": str(getattr(row, "tradestatus", "1")),
                "volume": float(getattr(row, "volume", 0)),
            })
        if len(bars) >= 2:
            evidence["bars"] = bars
        break

    # D-2：抽样 >= 30 只标的的最后一日 market_cap / amount
    mvs: list[float] = []
    amts: list[float] = []
    for sym_dir in sym_dirs:
        parts = sorted(p for p in sym_dir.glob("*.parquet") if p.stem.isdigit())
        if not parts:
            continue
        df = pd.read_parquet(parts[-1])
        if df is None or df.empty or "market_cap" not in df.columns or "amount" not in df.columns:
            continue
        mvs.append(float(df["market_cap"].iloc[-1]))
        amts.append(float(df["amount"].iloc[-1]))
        if len(mvs) >= 35:
            break
    if len(mvs) >= 30:
        evidence["float_mv_list"] = mvs
        evidence["amount_list"] = amts

    # D-3：抽样"某自然年内股息率有真实 PIT 变异"的标的序列
    for sym_dir in sym_dirs[:60]:
        parts = sorted(p for p in sym_dir.glob("*.parquet") if p.stem.isdigit())
        if not parts:
            continue
        df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
        if "dividend_yield" not in df.columns or "date" not in df.columns:
            continue
        df = df.sort_values("date")
        for year in sorted({str(d)[:4] for d in df["date"].tolist()}):
            sub = df[[str(d)[:4] == year for d in df["date"].tolist()]]
            ys = [float(y) for y in sub["dividend_yield"].dropna().tolist()]
            if len(ys) >= 60 and len({round(y, 4) for y in ys}) >= 50:
                evidence["daily_yields"] = ys
                evidence["year"] = int(year)
                return evidence
    return evidence


def _sample_data_evidence(ctx: dict[str, Any], data_dir: Path) -> None:
    """把 :func:`collect_data_evidence` 的取证结果并入门禁 ctx。

    Args:
        ctx: 门禁上下文（原地更新）。
        data_dir: 数据根——**真实数据优先**，CI 上无 ``data/`` 时为最小 fixture
            （由 :func:`resolve_data_root` 决定，调用方无需关心）。
    """
    ctx.update(collect_data_evidence(data_dir))


def ci_policy(
    results: list[GateResult],
    *,
    run_evidence_blocks: bool = False,
) -> tuple[bool, list[GateResult], list[GateResult]]:
    """CI 阻断策略（㉖）：返回 ``(blocking, blockers, warnings)``。

    * 门禁在 ``metrics["ci_blocking"]`` 显式声明 ``True`` ⇒ **该非 PASS 结果一律阻断**
      （㉝：让"门禁自己知道这条证据不足不得放过"的声明真正生效——如
      ``G-REPRO-1`` 的 ``ADOPTED_LEGACY_UNVERIFIED``，被采纳产物落 legacy ⇒ BLOCKER）。
      ⛔ 该声明**优先于** ``WARN_GATE_IDS`` 降级与 STATIC/RUN_EVIDENCE 分类，
      否则字段就是"看起来生效、实际不生效"的死字段；
    * :data:`WARN_GATE_IDS`（如 ``G-MDD-1``）⇒ **只告警不阻断**（三层分层的推送/CI 期，任务 1）；
    * 其余 ``FAIL``（任意级别）⇒ 阻断；
    * ``INCONCLUSIVE`` 且属 :data:`STATIC_GATE_IDS` ⇒ 阻断（静态可判却证据不足）；
    * ``INCONCLUSIVE`` 且属 :data:`RUN_EVIDENCE_GATE_IDS` ⇒ 默认**只告警不阻断**
      （CI 无 run 产物；push 期本就取不到这些证据）。

    Args:
        run_evidence_blocks: ㊳ **定时全量审计**（``--scheduled``）用。置 ``True`` 时
            「需 run 产物门禁的 INCONCLUSIVE」**也阻断**——定时场景本就该拿到 run 产物，
            再拿不到就说明证据链断了，继续只告警会让该 workflow **永不变红、等于没在判**
            （⛔ WARN 降级仍优先：G-MDD-1 属研究质量门禁，不在定时场景升格为阻断）。
    """
    blockers: list[GateResult] = []
    warnings: list[GateResult] = []
    for r in results:
        # ㉝ 显式升级：门禁自报 ci_blocking ⇒ 非 PASS 即阻断（不得被分类/降级静默吞掉）。
        if r.status != GateStatus.PASS and bool((r.metrics or {}).get("ci_blocking")):
            blockers.append(r)
            continue
        if r.gate_id in WARN_GATE_IDS:
            if r.status in (GateStatus.FAIL, GateStatus.INCONCLUSIVE, GateStatus.WARNING):
                warnings.append(r)
            continue
        if r.status == GateStatus.FAIL:
            blockers.append(r)
        elif r.status == GateStatus.INCONCLUSIVE:
            if r.gate_id in STATIC_GATE_IDS or run_evidence_blocks:
                blockers.append(r)
            else:
                warnings.append(r)
    return (len(blockers) > 0, blockers, warnings)
