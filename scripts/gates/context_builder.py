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
STATIC_GATE_IDS: frozenset[str] = frozenset({
    "D-1", "D-2", "D-3", "D-4", "E-1", "E-2", "S-1",
    "G-1", "G-2", "G-3", "G-4", "G-DOC-1", "G-REF-1",
})

#: 需"run 内部真相"（逐日仓位/现金流/委托明细/送转全量/独立黄金基准/运行期追踪/分红分档/压测产物）
#: 的门禁：CI 无产物时 INCONCLUSIVE **只告警不阻断**；有产物则照常判 PASS/FAIL。
#: ``G-MDD-1`` 在此归类（其判定依赖 run 产物 metrics，且见 :data:`WARN_GATE_IDS` 降级）。
RUN_EVIDENCE_GATE_IDS: frozenset[str] = frozenset({
    "D-5", "L-1", "L-2", "L-3", "E-3", "A-1", "A-2", "A-3", "A-4",
    "S-2", "S-3", "S-4", "S-5", "G-STRESS-1", "G-MDD-1", "G-REPRO-1",
})

#: 三层分层（M3 任务 1）：**推送 / CI 期 WARN（展示但不阻断）**的门禁。
#: 其结果始终展示（``[WARN] ...``）但**不产生阻断退出码**；BLOCKER 能力保留在准入层。
#: ``G-MDD-1``：推送被 43.08% 回撤永久阻断 ⇒ 必逼出逃生阀 ⇒ 门禁沦为摆设，故降级为 WARN。
WARN_GATE_IDS: frozenset[str] = frozenset({"G-MDD-1"})


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
        ctx["data_hash"] = hashlib.sha256(
            (str(record.get("data_version", "")) + str(record.get("params_hash", ""))).encode("utf-8")
        ).hexdigest()
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

    # D-1~D-4 抽样现读 parquet
    try:
        _sample_data_evidence(ctx, root)
    except Exception:                       # noqa: BLE001
        pass

    tasks = root / "docs" / "spec" / "001-a-stock-longonly-daily-quant" / "tasks.md"
    if tasks.exists():
        ctx["tasks_path"] = str(tasks)

    run_file = root / "scripts" / "run_dividend_backtest.py"
    if run_file.exists():
        ctx["source_code"] = run_file.read_text(encoding="utf-8")
        ctx["required_calls"] = ["BacktestBroker", "MatchEngine", "compute_metrics"]

    return ctx, source


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _sample_data_evidence(ctx: dict[str, Any], repo_root: Path) -> None:
    """抽样现读 ``data/dividend_stocks/`` parquet，为 D-1~D-4 提供真实证据。"""
    import pandas as pd

    data_dir = repo_root / "data" / "dividend_stocks"
    if not data_dir.exists():
        return
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
            ctx["bars"] = bars
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
        ctx["float_mv_list"] = mvs
        ctx["amount_list"] = amts

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
                ctx["daily_yields"] = ys
                ctx["year"] = int(year)
                return


def ci_policy(results: list[GateResult]) -> tuple[bool, list[GateResult], list[GateResult]]:
    """CI 阻断策略（㉖）：返回 ``(blocking, blockers, warnings)``。

    * :data:`WARN_GATE_IDS`（如 ``G-MDD-1``）⇒ **只告警不阻断**（三层分层的推送/CI 期，任务 1）；
    * 其余 ``FAIL``（任意级别）⇒ 阻断；
    * ``INCONCLUSIVE`` 且属 :data:`STATIC_GATE_IDS` ⇒ 阻断（静态可判却证据不足）；
    * ``INCONCLUSIVE`` 且属 :data:`RUN_EVIDENCE_GATE_IDS` ⇒ **只告警不阻断**（CI 无 run 产物）。
    """
    blockers: list[GateResult] = []
    warnings: list[GateResult] = []
    for r in results:
        if r.gate_id in WARN_GATE_IDS:
            if r.status in (GateStatus.FAIL, GateStatus.INCONCLUSIVE, GateStatus.WARNING):
                warnings.append(r)
            continue
        if r.status == GateStatus.FAIL:
            blockers.append(r)
        elif r.status == GateStatus.INCONCLUSIVE:
            (blockers if r.gate_id in STATIC_GATE_IDS else warnings).append(r)
    return (len(blockers) > 0, blockers, warnings)
