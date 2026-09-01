#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T206 实验 registry 单测（offline，tmp_path 磁盘隔离，⛔ 无网络 / 无仓库依赖）。

纪律点全部钉死：run_id = 时钟-git版本-种子；canonical 尺与 tx_hash 同宗（float 炸）；
同 run_id 拒重（幂等）；原子写（.tmp→replace，不留残渣）；出处三件套必填。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from reporting.registry import ExperimentRegistry, RegistryError

D = Decimal

_TZ = timezone(timedelta(hours=8))


def _clock(y=2026, mo=9, d=1, h=15, mi=30, s=0):
    return lambda: datetime(y, mo, d, h, mi, s, tzinfo=_TZ)


class _Report:
    """PerformanceReport 鸭子（metrics 模块的字段形状）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _report() -> _Report:
    return _Report(
        start=date(2024, 1, 1), end=date(2024, 12, 31), calendar_days=365,
        trading_days=242, initial_nav=D("100000"), final_nav=D("112345.67"),
        total_return=D("0.123456"), cagr=D("0.122001"), annual_volatility=D("0.18"),
        max_drawdown=D("0.05"), max_dd_peak=date(2024, 3, 1),
        max_dd_trough=date(2024, 4, 1), max_dd_recovery=date(2024, 5, 6),
        sharpe_ratio=D("1.23"), risk_free_annual=D("0.02"),
        annual_turnover=D("2.4"), win_rate=D("0.6"), round_trips=5,
        fees_sum=D("345.67"),
    )


def _reg(tmp_path: Path, **kw) -> ExperimentRegistry:
    kw.setdefault("code_version", "abc1234")
    kw.setdefault("data_version", "sha256:abc")
    kw.setdefault("clock", _clock())
    return ExperimentRegistry(tmp_path / "exp", **kw)


class TestRecord:
    def test_record_writes_json_atomically(self, tmp_path) -> None:
        reg = _reg(tmp_path)
        rid = reg.record_run({"a": 1, "b": D("0.0001"), "when": date(2024, 1, 1)},
                             _report(), seed=7)
        assert rid == "20260901-153000-abc1234-7"
        f = tmp_path / "exp" / "runs" / f"{rid}.json"
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data["run_id"] == rid and data["status"] == "FINISHED"
        assert data["code_version"] == "abc1234" and data["data_version"] == "sha256:abc"
        assert data["seed"] == 7
        assert data["metrics"]["cagr"] == "0.122001"     # Decimal → str
        assert data["metrics"]["max_dd_peak"] == "2024-03-01"
        # .tmp 不留渣
        assert not list((tmp_path / "exp" / "runs").glob("*.tmp"))

    def test_duplicate_run_id_rejected(self, tmp_path) -> None:
        reg = _reg(tmp_path)
        reg.record_run({"a": 1}, _report(), seed=7)
        with pytest.raises(RegistryError):
            reg.record_run({"a": 1}, _report(), seed=7)      # 同时钟+同种子 = 同 rid

    def test_canonical_params_hash_order_invariant(self, tmp_path) -> None:
        reg = _reg(tmp_path)
        r1 = reg.record_run({"a": 1, "b": D("2.50")}, _report(), seed=1)
        reg2 = ExperimentRegistry(tmp_path / "exp2", code_version="abc1234",
                                  data_version="sha256:abc", clock=_clock())
        r2 = reg2.record_run({"b": D("2.50"), "a": 1}, _report(), seed=1)
        p1 = json.loads((tmp_path / "exp" / "runs" / f"{r1}.json").read_text("utf-8"))
        p2 = json.loads((tmp_path / "exp2" / "runs" / f"{r2}.json").read_text("utf-8"))
        assert p1["params_hash"] == p2["params_hash"]

    def test_float_param_rejected(self, tmp_path) -> None:
        reg = _reg(tmp_path)
        with pytest.raises(Exception):                        # canonical 尺炸 float
            reg.record_run({"lr": 0.001}, _report())

    def test_index_jsonl_appended(self, tmp_path) -> None:
        reg = _reg(tmp_path)
        reg.record_run({"x": 1}, _report(), seed=1)
        # 换一个时钟 = 不同 run_id，第二次登记追加
        reg2 = ExperimentRegistry(tmp_path / "exp", code_version="abc1234",
                                  data_version="sha256:abc", clock=_clock(s=1))
        reg2.record_run({"x": 1}, _report(), seed=1)
        lines = (tmp_path / "exp" / "runs" / "index.jsonl").read_text("utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["run_id"] != json.loads(lines[1])["run_id"]


class TestGuards:
    def test_missing_provenance_rejected(self, tmp_path) -> None:
        with pytest.raises(RegistryError):
            ExperimentRegistry(tmp_path / "e", code_version="", data_version="x")
        with pytest.raises(RegistryError):
            ExperimentRegistry(tmp_path / "e", code_version="abc", data_version="")

    def test_dirty_flag_marked(self, tmp_path) -> None:
        reg = _reg(tmp_path)
        rid = reg.record_run({"a": 1}, _report())
        data = json.loads((tmp_path / "exp" / "runs" / f"{rid}.json").read_text("utf-8"))
        assert data["code_version"] == "abc1234"
        reg_dirty = ExperimentRegistry(tmp_path / "exp", code_version="abc1234",
                                       data_version="sha256:abc", clock=_clock(s=2),
                                       dirty=True)
        rid2 = reg_dirty.record_run({"a": 1}, _report())
        data2 = json.loads((tmp_path / "exp" / "runs" / f"{rid2}.json").read_text("utf-8"))
        assert data2["code_version"] == "abc1234+dirty"

    def test_status_machine(self, tmp_path) -> None:
        reg = _reg(tmp_path)
        with pytest.raises(RegistryError):
            reg.record_run({"a": 1}, _report(), status="RUNNING")     # 只允许终态
        with pytest.raises(RegistryError):
            reg.record_run({"a": 1}, _report(), status="FINISHED", error="矛盾")
        with pytest.raises(RegistryError):
            reg.record_run({"a": 1}, None, status="FAILED")           # 失败必须给原因
        rid = reg.record_run({"a": 1}, None, status="FAILED", error="数据缺口")
        data = json.loads((tmp_path / "exp" / "runs" / f"{rid}.json").read_text("utf-8"))
        assert data["status"] == "FAILED" and data["error"] == "数据缺口"
        assert data["metrics"] == {}

    def test_seed_must_be_int(self, tmp_path) -> None:
        with pytest.raises(RegistryError):
            _reg(tmp_path).record_run({"a": 1}, _report(), seed="42")

    def test_list_runs_sorted(self, tmp_path) -> None:
        reg = _reg(tmp_path)
        reg.record_run({"r": 2}, _report(), seed=2)
        reg2 = ExperimentRegistry(tmp_path / "exp", code_version="abc1234",
                                  data_version="sha256:abc",
                                  clock=_clock(h=9))                # 更早时刻
        reg2.record_run({"r": 1}, _report(), seed=1)
        runs = reg.list_runs()
        assert [r["seed"] for r in runs] == [1, 2]                  # 按时间升序非写入序
        assert runs[0]["cagr"] == "0.122001"
        assert runs[0]["max_drawdown"] == "0.05"
