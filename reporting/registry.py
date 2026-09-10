#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T206 实验 registry —— FR-REP-2（参数 / 代码版本 / 数据版本 / 指标 / 时间戳）。

`BacktestEngine.run()` 无参数注入点（T201 契约），故 registry 是**引擎外层包装**——
回测跑完后把「结果快照 + 出处三件套」一次性登记到 ``runs/`` 目录，⛔ 不改引擎一行。

设计口径（显式声明）：

| 字段 | 口径 |
|---|---|
| ``run_id`` | ``YYYYMMDD-HHMMSS-<code_version>-<seed>``（时钟可注入 ⇒ 离线可复现） |
| 参数 ``params`` | 复用 ``backtest.ledger._canonicalize`` 同一把 canonical 尺（Decimal→str、float **显式炸**）；``params_hash`` = SHA-256(canonical) 前 16 hex |
| ``code_version`` | git 短哈希，**注入式**（⛔ 不许内部 subprocess 取 git —— 离线测试可重现）；dirty 工作树须调用方显式传 ``dirty=True`` 留痕 |
| ``data_version`` | 数据快照标识（如 ``data/daily_bars`` 分区哈希），注入式；⛔ 不许默认空 |
| 指标 ``metrics`` | T205 ``PerformanceReport`` 的关键字段摘要（Decimal→str 落 JSON） |
| 时间戳 | 本地时区 ISO 8601 含偏移（``datetime.now().astimezone()``，时钟可注入） |
| 种子 ``seed`` | NFR-5/13 号：钉到 run_id 里，重跑同参同种子 ⇒ 同 run_id ⇒ **拒重**（幂等） |

M2/PM-1 修复补充（内容寻址出处）：

| 字段 | 口径 |
|---|---|
| ``schema_version`` | 现行 = 2；缺该字段的历史产物 = legacy(1)，⛔ 不得当作"检查通过" |
| ``code_hash`` / ``data_hash`` / ``calendar_hash`` / ``universe_hash`` | 注入式**内容哈希**；缺内容哈希时**显式 ``None``**，⛔ 不静默兜底 |
| ``repro_fingerprint`` | ``repro_fingerprint(params_hash, code_hash, data_hash, calendar_hash, universe_hash, seed)``；四要素（code+data）齐备才生成，否则 ``None`` |
| ``gate_statuses`` | 本次回测各门禁 status 快照（报告用；⛔ 不入 params/metrics/hash，避免扰动复现比对） |

落盘纪律：**原子写**（``.<run_id>.tmp`` → ``os.replace``），同 ``run_id`` 重复登记
``RegistryError``（⛔ 不覆盖历史）；``runs/index.jsonl`` 是可选汇总索引（每次登记
追加一行，行键 = run_id）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime as _dt
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping

from backtest.ledger import _canonicalize
from reporting.provenance import repro_fingerprint

__all__ = [
    "RegistryError",
    "RunRecord",
    "ExperimentRegistry",
]

#: 现行产物 schema：2 = 携带内容寻址出处（code_hash/data_hash/.../repro_fingerprint）。
#: 缺该字段的历史产物一律视为 legacy（schema_version=1），⛔ 不得当作"检查通过"。
SCHEMA_VERSION = 2

#: 合法终态（运行只接受这三种；落盘即终态，运行中不落盘 —— 挂在内存里）。
_RUN_STATUSES = ("FINISHED", "FAILED", "KILLED")


class RegistryError(RuntimeError):
    """登记契约违约（重复 run_id / float 参数 / 缺出处 / 脏路径等）。"""


@dataclass(frozen=True)
class RunRecord:
    """一条实验登记记录（JSON 落盘件的内存形态）。"""

    run_id: str
    status: str                # FINISHED / FAILED / KILLED
    timestamp: str             # ISO 8601 带时区偏移
    code_version: str
    data_version: str
    seed: int | None
    params_hash: str
    params: dict
    metrics: dict              # 指标摘要（Decimal→str 后形态）
    error: str | None = None   # FAILED/KILLED 时的原因
    # --- 内容寻址出处（M2/PM-1 修复）：向后兼容，默认 None ---
    schema_version: int = SCHEMA_VERSION
    code_hash: str | None = None        # git HEAD(+dirty) 内容指纹
    data_hash: str | None = None        # data/ 清单内容指纹
    calendar_hash: str | None = None    # 实际消费的交易日历指纹
    universe_hash: str | None = None    # 候选池时点快照指纹
    repro_fingerprint: str | None = None  # 上述 + params_hash + seed 的聚合键
    gate_statuses: dict | None = None    # 本次回测各门禁 status 快照（报告，不阻断）


def _params_hash(params: Mapping[str, Any]) -> str:
    """参数快照的 SHA-256 前 16 hex（canonical 尺与 ``tx_hash`` 同宗，键序无关）。"""
    canon = _canonicalize(dict(params))
    text = json.dumps(canon, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(text.encode("utf-8")).hexdigest()[:16]


def _metrics_summary(report: Any) -> dict[str, Any]:
    """T205 ``PerformanceReport`` → JSON 可序列化摘要（鸭子类型，⛔ 不 import metrics）。

    只取有决策含义的字段；Decimal/date/None 由 ``_canonicalize`` 归一（float 会炸，
    是**特性**——指标里不许混进 float）。
    """
    keys = (
        "start", "end", "calendar_days", "trading_days",
        "initial_nav", "final_nav", "total_return", "cagr",
        "annual_volatility", "max_drawdown", "max_dd_peak", "max_dd_trough",
        "max_dd_recovery", "sharpe_ratio", "risk_free_annual",
        "annual_turnover", "win_rate", "round_trips", "fees_sum",
        "fees_total",
    )
    return {k: _canonicalize(getattr(report, k)) for k in keys if hasattr(report, k)}


class ExperimentRegistry:
    """``runs/`` 目录登记处（⛔ 构造时就钉出处：代码版本 + 数据版本必须当场给）。"""

    def __init__(
        self,
        root: str | Path,
        *,
        code_version: str,
        data_version: str,
        code_hash: str | None = None,
        data_hash: str | None = None,
        calendar_hash: str | None = None,
        universe_hash: str | None = None,
        clock: Callable[[], _dt] | None = None,
        dirty: bool = False,
    ) -> None:
        if not code_version or not str(code_version).strip():
            raise RegistryError("code_version 必填（注入式 git 短哈希，⛔ 不内部 subprocess）")
        if not data_version or not str(data_version).strip():
            raise RegistryError("data_version 必填（数据快照标识；没有就先造快照再登记）")
        self.root = Path(root)
        self.code_version = str(code_version)
        self.data_version = str(data_version)
        # 内容寻址出处（M2/PM-1）：缺内容哈希时**显式 None**，⛔ 不静默用常量兜底。
        self._code_hash = self._norm_hash(code_hash)
        self._data_hash = self._norm_hash(data_hash)
        self._calendar_hash = self._norm_hash(calendar_hash)
        self._universe_hash = self._norm_hash(universe_hash)
        self.dirty = bool(dirty)
        self._clock = clock or (lambda: _dt.now().astimezone())
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)

    @staticmethod
    def _norm_hash(value: str | None) -> str | None:
        """归一内容哈希：空串/空白 ⇒ ``None``（显式缺失，⛔ 不静默兜底）。"""
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    # -------------------------------------------------------------- 公开 API

    def record_run(
        self,
        params: Mapping[str, Any],
        report: Any,
        *,
        seed: int | None = None,
        status: str = "FINISHED",
        error: str | None = None,
        gate_statuses: dict[str, Any] | None = None,
    ) -> str:
        """登记一次实验。返回 ``run_id``。

        Args:
            params: 策略/引擎参数快照（canonical 化 + hash；⛔ float 会炸）。
            report: T205 ``PerformanceReport``（或任何同名字段鸭子对象）；
                ⛔ status 不是 FINISHED 时可为 ``None``（失败/KILLED 无指标）。
            seed: 随机种子（可空；进入 run_id）。
            status: ``FINISHED`` / ``FAILED`` / ``KILLED``。
            error: 非 FINISHED 时的原因（FINISHED 时必须为 None）。
            gate_statuses: 本次回测各门禁 status 快照（报告用，⛔ 不入 metrics/params/hash）。

        Raises:
            RegistryError: 同 ``run_id`` 已登记（幂等拒重）/ 状态非法 /
                时间与种子不在预期形态 / 指标含 float。
        """
        if status not in _RUN_STATUSES:
            raise RegistryError(f"status 须为 {_RUN_STATUSES} 之一: {status!r}")
        if status == "FINISHED" and error is not None:
            raise RegistryError("FINISHED 运行不准带 error 字段")
        if status != "FINISHED" and report is None and not error:
            raise RegistryError("FAILED/KILLED 运行必须给 error 原因（审计链）")
        if seed is not None and not isinstance(seed, int):
            raise RegistryError(f"seed 须为 int 或 None: {seed!r}")

        now = self._clock()
        ts = now.isoformat(timespec="seconds")
        seed_part = str(seed) if seed is not None else "noseed"
        run_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{self.code_version}-{seed_part}"

        target = self.root / "runs" / f"{run_id}.json"
        if target.exists():
            raise RegistryError(f"run_id {run_id} 已登记（同参同种子同秒 ⇒ 幂等拒重）")

        params_hash = _params_hash(params)
        # 完整出处键：四要素齐备才生成；否则**显式 None**（报告 §6.1(B)，⛔ 不静默兜底）。
        if self._code_hash and self._data_hash:
            fingerprint = repro_fingerprint(
                params_hash=params_hash,
                code_hash=self._code_hash,
                data_hash=self._data_hash,
                calendar_hash=self._calendar_hash or "na",
                universe_hash=self._universe_hash or "na",
                seed=seed,
            )
        else:
            fingerprint = None

        record = RunRecord(
            run_id=run_id,
            status=status,
            timestamp=ts,
            code_version=self.code_version + ("+dirty" if self.dirty else ""),
            data_version=self.data_version,
            seed=seed,
            params_hash=params_hash,
            params=dict(params),
            metrics=_metrics_summary(report) if report is not None else {},
            error=error,
            schema_version=SCHEMA_VERSION,
            code_hash=self._code_hash,
            data_hash=self._data_hash,
            calendar_hash=self._calendar_hash,
            universe_hash=self._universe_hash,
            repro_fingerprint=fingerprint,
            gate_statuses=dict(gate_statuses) if gate_statuses else None,
        )
        payload = json.dumps(
            _canonicalize({
                "run_id": record.run_id, "status": record.status,
                "timestamp": record.timestamp, "code_version": record.code_version,
                "data_version": record.data_version, "seed": record.seed,
                "params_hash": record.params_hash, "params": record.params,
                "metrics": record.metrics, "error": record.error,
                "schema_version": record.schema_version,
                "code_hash": record.code_hash, "data_hash": record.data_hash,
                "calendar_hash": record.calendar_hash, "universe_hash": record.universe_hash,
                "repro_fingerprint": record.repro_fingerprint,
                "gate_statuses": record.gate_statuses,
            }),
            ensure_ascii=False, indent=2, sort_keys=True,
        )
        tmp = self.root / "runs" / f".{run_id}.tmp"
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)

        with (self.root / "runs" / "index.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "run_id": run_id, "status": record.status, "timestamp": ts,
                "code_version": record.code_version, "seed": seed,
                "params_hash": record.params_hash,
                "repro_fingerprint": record.repro_fingerprint,
                "schema_version": record.schema_version,
            }, ensure_ascii=False) + "\n")
        return run_id

    def list_runs(self) -> list[dict]:
        """按时间戳升序返回全部登记的摘要（读盘，⛔ 不缓存）。"""
        runs_dir = self.root / "runs"
        out: list[dict] = []
        for path in sorted(runs_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append({
                "run_id": data["run_id"], "status": data["status"],
                "timestamp": data["timestamp"], "code_version": data["code_version"],
                "data_version": data["data_version"], "seed": data["seed"],
                "params_hash": data["params_hash"],
                "cagr": data.get("metrics", {}).get("cagr"),
                "max_drawdown": data.get("metrics", {}).get("max_drawdown"),
            })
        out.sort(key=lambda r: r["timestamp"])
        return out
