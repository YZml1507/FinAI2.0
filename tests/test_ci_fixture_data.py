#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GATE-R8：CI 最小数据 fixture 与数据取证回退的回归测试。

覆盖三件事（CI 红灯根因的**正解**，⛔ 不是放宽门禁）：

1. fixture 本身成立：规模达标、schema 与真实数据一致、含可供 D-4 判定的停牌日；
2. ``context_builder`` 取样逻辑在该 fixture 上能取出**足够**证据，且 D-1~D-4 真判 PASS；
3. **真实数据优先**：``data/dividend_stocks`` 存在时绝不回退 fixture（本地行为不变）。

⛔ 边界声明（测试同样如实登记）：CI 上校验的是**抽样小样**，
**不等于**校验全量真实数据质量；全量校验须在本地 ``data/`` 上跑同一命令。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "ci_min_data"
FIXTURE_DATA_DIR = FIXTURE_DIR / "dividend_stocks"
MANIFEST_PATH = FIXTURE_DIR / "fixture_manifest.json"

#: 与真实 parquet 一致的规范列顺序（见 scripts/build_ci_fixture_data.py::EXPECTED_COLUMNS）。
EXPECTED_COLUMNS: tuple[str, ...] = (
    "date", "open", "high", "low", "close", "preclose", "volume", "amount",
    "turn", "pctChg", "tradestatus", "isST", "code", "source", "adjust_mode",
    "dividend_yield", "market_cap",
)


def _require_pandas() -> Any:
    """延迟导入 pandas（CI 已装；缺失时本模块整体跳过，不让数据门禁假绿）。"""
    return pytest.importorskip("pandas")


def _symbol_dirs() -> list[Path]:
    return sorted(
        p for p in FIXTURE_DATA_DIR.iterdir()
        if p.is_dir() and p.name.startswith(("sh.", "sz."))
    )


# ---------------------------------------------------------------------------
# 1. fixture 自身成立
# ---------------------------------------------------------------------------

def test_fixture_exists_and_is_version_controlled() -> None:
    """fixture 必须存在于**不受 .gitignore 影响**的路径（⛔ 放 data/ 下等于没提交）。"""
    assert FIXTURE_DATA_DIR.exists(), f"缺 CI fixture: {FIXTURE_DATA_DIR}"
    assert "data" not in FIXTURE_DATA_DIR.relative_to(REPO_ROOT).parts[:1]


def test_manifest_declares_sampling_and_synthetic_parts() -> None:
    """清单必须自述规模 + **合成成分**（⛔ 不得静默混入合成数据）。"""
    assert MANIFEST_PATH.exists(), "缺 fixture_manifest.json"
    m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert m["total_symbols"] >= 30, f"标的数 {m['total_symbols']} < 30（D-2 需 >= 30）"
    assert m["days_per_symbol"] >= 120, f"每标的 {m['days_per_symbol']} 天 < 120"
    assert m["source_data_dir"] == "data/dividend_stocks"
    assert m["warning"], "清单必须写明『CI 校验的是抽样小样』边界"
    susp = m["synthetic_suspension_days"]
    assert susp["count"] > 0
    assert susp["reason"], "合成停牌日必须给出理由（真实集无停牌日可抽样）"
    assert susp["dates"], "合成停牌日必须**逐日登记**（可核、可复现）"


def test_fixture_schema_matches_real_columns() -> None:
    """每个标的 parquet 的**列名集合**必须与真实数据一致（错列即 schema 漂移）。"""
    _require_pandas()
    syms = _symbol_dirs()
    assert len(syms) >= 30, f"fixture 标的数 {len(syms)} < 30"
    for sym in syms:
        parts = sorted(p for p in sym.glob("*.parquet") if p.stem.isdigit())
        assert parts, f"{sym.name}: 无年份 parquet"
        df = _require_pandas().read_parquet(parts[-1])
        assert set(df.columns) == set(EXPECTED_COLUMNS), (
            f"{sym.name}: 列集合漂移\n实际 {sorted(df.columns)}\n期望 {sorted(EXPECTED_COLUMNS)}"
        )
        assert len(df) >= 120, f"{sym.name}: {len(df)} 天 < 120"


def test_fixture_contains_suspension_days_for_d4() -> None:
    """必须含 ``tradestatus != '1'`` 且 ``volume == 0`` 的停牌日，否则 D-4 会 SKIP（等于没检）。

    D-1/D-4 只抽样**字典序第一只**标的，故该标的必须命中。
    """
    pd = _require_pandas()
    first = _symbol_dirs()[0]
    parts = sorted(p for p in first.glob("*.parquet") if p.stem.isdigit())
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    susp = df[df["tradestatus"].astype(str) != "1"]
    assert len(susp) > 0, f"{first.name}: 无停牌日 ⇒ D-4 将判 SKIP（CI 上等于没在判）"
    assert float(susp["volume"].astype(float).abs().sum()) == 0.0, "停牌日成交量必须恒为 0"


# ---------------------------------------------------------------------------
# 2. 取样逻辑 + 门禁本体在 fixture 上真判 PASS
# ---------------------------------------------------------------------------

def _collect() -> dict[str, Any]:
    from scripts.gates.context_builder import collect_data_evidence

    return collect_data_evidence(FIXTURE_DATA_DIR)


def test_collect_data_evidence_meets_minimum_samples() -> None:
    """取样结果必须满足各门禁的**最低样本**要求，否则仍会 INCONCLUSIVE。"""
    ev = _collect()
    assert len(ev.get("bars", [])) >= 2, "D-1 需 >= 2 根日线"
    assert len(ev.get("float_mv_list", [])) >= 30, "D-2 需 >= 30 只标的"
    assert len(ev.get("amount_list", [])) == len(ev["float_mv_list"])
    assert len(ev.get("daily_yields", [])) >= 60, "D-3 需 >= 60 天"


def test_data_gates_pass_on_fixture_evidence() -> None:
    """D-1~D-4 在 fixture 证据上必须**真判 PASS**（⛔ 不是 SKIP/INCONCLUSIVE）。"""
    from scripts.gates.base import GateStatus
    from scripts.gates.gate_d_data import (
        FloatMarketCapGate,
        PitDividendYieldGate,
        RawPriceJumpGate,
        SuspensionVolumeGate,
    )

    ev = _collect()
    for gate in (
        RawPriceJumpGate(),
        FloatMarketCapGate(),
        PitDividendYieldGate(),
        SuspensionVolumeGate(),
    ):
        res = gate.evaluate(ev)
        assert res.status == GateStatus.PASS, (
            f"{gate.gate_id} 在 CI fixture 上判 {res.status.value}: {res.message}"
        )


def test_data_hash_computable_from_fixture() -> None:
    """G-1 的 data_hash 必须能从 fixture 算出有效值（``MissingDataError`` 会导致 G-1 FAIL）。"""
    from reporting.provenance import hash_path_manifest

    h = hash_path_manifest(FIXTURE_DATA_DIR)
    assert h is not None, "fixture 的 data_hash 为 None ⇒ G-1 必 FAIL"
    assert len(h) >= 16 and all(c in "0123456789abcdef" for c in h), f"data_hash 非法: {h!r}"


# ---------------------------------------------------------------------------
# 3. 真实数据优先（⛔ 本地行为必须完全不变）
# ---------------------------------------------------------------------------

def _make_tmp_root(tmp_path: Path, *, with_real_data: bool) -> Path:
    """构造临时仓库根：可选 ``data/dividend_stocks``（真实数据）+ 必有 CI fixture。"""
    fixture_dst = tmp_path / "tests" / "fixtures" / "ci_min_data"
    fixture_dst.mkdir(parents=True, exist_ok=True)
    shutil.copy(MANIFEST_PATH, fixture_dst / MANIFEST_PATH.name)
    shutil.copytree(FIXTURE_DATA_DIR, fixture_dst / "dividend_stocks")
    if with_real_data:
        real_dst = tmp_path / "data" / "dividend_stocks"
        real_dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(FIXTURE_DATA_DIR, real_dst, dirs_exist_ok=True)
    return tmp_path


def test_resolve_data_root_prefers_real_data(tmp_path: Path) -> None:
    """``data/dividend_stocks`` 存在时**必须**用它 —— ⛔ 不得被 fixture 抢占。"""
    from scripts.gates.context_builder import resolve_data_root

    root = _make_tmp_root(tmp_path, with_real_data=True)
    data_root, label = resolve_data_root(root)
    assert data_root == root / "data" / "dividend_stocks"
    assert "真实数据" in label


def test_resolve_data_root_falls_back_to_fixture(tmp_path: Path) -> None:
    """``data/dividend_stocks`` 缺失（= CI 场景）时回退 fixture，且来源**自述可见**。"""
    from scripts.gates.context_builder import resolve_data_root

    root = _make_tmp_root(tmp_path, with_real_data=False)
    data_root, label = resolve_data_root(root)
    assert data_root == root / "tests" / "fixtures" / "ci_min_data" / "dividend_stocks"
    assert "CI 最小 fixture" in label
    assert "抽样自真实数据" in label
    assert "合成停牌日" in label, "合成成分必须在取证来源里可见（⛔ 不得静默）"


def test_resolve_data_root_reports_missing_when_both_absent(tmp_path: Path) -> None:
    """两者皆无 ⇒ 明确报『数据缺失』，⛔ 不编造路径、不假装通过。"""
    from scripts.gates.context_builder import resolve_data_root

    data_root, label = resolve_data_root(tmp_path)
    assert data_root == tmp_path / "data" / "dividend_stocks"
    assert "数据缺失" in label


def test_local_repo_prefers_real_data_when_present() -> None:
    """本地仓库若有真实数据，取证来源必须是真实数据（回归守卫）。"""
    from scripts.gates.context_builder import resolve_data_root

    real = REPO_ROOT / "data" / "dividend_stocks"
    if not real.exists() or not any(
        p.is_dir() and p.name.startswith(("sh.", "sz.")) for p in real.iterdir()
    ):
        pytest.skip("本地无真实数据 data/dividend_stocks，跳过（CI 场景不适用本守卫）")
    data_root, label = resolve_data_root(REPO_ROOT)
    assert data_root == real
    assert "真实数据" in label
    assert "CI 小样" not in label


# ---------------------------------------------------------------------------
# 4. CI 物化（追加项 (a)）：小样落进 data/ 后必须被识别为「CI 小样」
# ---------------------------------------------------------------------------

def _materialize_like_ci(tmp_path: Path) -> Path:
    """模拟 ci.yml 的物化步骤：fixture → ``<root>/data/``（含清单随数据落盘）。"""
    data_dir = tmp_path / "data" / "dividend_stocks"
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE_DATA_DIR, data_dir, dirs_exist_ok=True)
    shutil.copy(MANIFEST_PATH, data_dir / MANIFEST_PATH.name)   # ci.yml 的显式标记
    (tmp_path / "data" / "daily_bars").mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE_DIR / "daily_bars", tmp_path / "data" / "daily_bars", dirs_exist_ok=True)
    return tmp_path


def test_materialized_sample_is_flagged_as_ci_sample(tmp_path: Path) -> None:
    """物化后 ``data/dividend_stocks`` 带清单 ⇒ 必须识别为 CI 小样（⛔ 不冒充全量真实数据）。"""
    from scripts.gates.context_builder import is_ci_sample, resolve_data_root

    root = _materialize_like_ci(tmp_path)
    assert is_ci_sample(root / "data" / "dividend_stocks") is True
    data_root, label = resolve_data_root(root)
    assert data_root == root / "data" / "dividend_stocks"
    assert "CI 小样" in label
    assert "真实数据" not in label
    assert "30 只标的" in label, f"取证来源必须自述规模（一眼看出是小样）: {label}"


def test_real_data_without_manifest_is_not_flagged(tmp_path: Path) -> None:
    """无清单标记的 ``data/dividend_stocks``（= 本地真实数据）⇒ 不得被误判为小样。"""
    from scripts.gates.context_builder import is_ci_sample

    data_dir = tmp_path / "data" / "dividend_stocks"
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE_DATA_DIR, data_dir, dirs_exist_ok=True)   # 故意**不放**清单
    assert is_ci_sample(data_dir) is False


def test_materialized_paths_satisfy_g_ref_1(tmp_path: Path) -> None:
    """G-REF-1 报的 3 处路径在物化后必须**真实存在**（该门禁直接查文件系统 exists()）。"""
    root = _materialize_like_ci(tmp_path)
    for rel in (
        ("data", "dividend_stocks", "meta.json"),
        ("data", "dividend_stocks", "sh.600000", "2024.parquet"),
        ("data", "daily_bars", "sh.600000", "2024.parquet"),
    ):
        assert (root.joinpath(*rel)).exists(), f"物化后仍缺: {'/'.join(rel)}"


def test_daily_bars_fixture_is_derived_from_real_sample() -> None:
    """``daily_bars/`` 必须是**真实派生行**：15 列 / ≥120 天 / RAW / 与同源标的逐行一致。

    ⛔ 回归目标：本地 ``data/daily_bars`` 只有 T105 **合成冒烟产物**
    （5 行 / HFQ / 价格 10.0→12.2 等距常量），绝不可被搬进 fixture 冒充真实抽样。
    """
    pd = _require_pandas()
    daily_dir = FIXTURE_DIR / "daily_bars"
    assert daily_dir.exists(), "缺 daily_bars/（G-REF-1 引用 data/daily_bars/）"
    expect_cols = [c for c in EXPECTED_COLUMNS if c not in ("dividend_yield", "market_cap")]
    checked = 0
    for sym in sorted(p for p in daily_dir.iterdir() if p.is_dir()):
        parts = sorted(p for p in sym.glob("*.parquet") if p.stem.isdigit())
        assert parts, f"daily_bars/{sym.name}: 无年份 parquet"
        d = pd.read_parquet(parts[-1])
        assert list(d.columns) == expect_cols, f"daily_bars/{sym.name}: 列结构漂移 -> {list(d.columns)}"
        assert len(d) >= 120, f"daily_bars/{sym.name}: {len(d)} 天 < 120（疑似 5 行合成冒烟产物）"
        assert set(d["adjust_mode"].astype(str).unique()) == {"RAW"}, "daily_bars 必须是 RAW 不复权"
        src_parts = sorted(p for p in (FIXTURE_DATA_DIR / sym.name).glob("*.parquet") if p.stem.isdigit())
        src = pd.read_parquet(src_parts[-1])
        left = list(zip(d["date"].astype(str), d["close"].astype(float).round(6)))
        right = list(zip(src["date"].astype(str), src["close"].astype(float).round(6)))
        assert left == right, f"daily_bars/{sym.name}: 与 dividend_stocks 同源标的逐行不一致 ⇒ 非真实派生"
        checked += 1
    assert checked > 0


def test_manifest_registers_daily_bars_provenance() -> None:
    """清单必须登记 ``daily_bars`` 的**派生方式与禁用来路**（⛔ 不得静默混入合成数据）。"""
    m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    db = m.get("daily_bars")
    assert db, "清单缺 daily_bars 登记"
    assert db["days_per_symbol"] >= 120
    assert "dividend_yield" in db["derivation"] or "剥离" in db["derivation"]
    assert "T105" in db["why_not_sampled_from_local"], "必须说明为何不从本地 data/daily_bars 抽样"
