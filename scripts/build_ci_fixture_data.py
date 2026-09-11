#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 **CI 最小数据 fixture**（GATE-R8）。

背景
----
``.gitignore`` 排除了 ``data/dividend_stocks/``（采集落盘、可再生），因此
GitHub Actions 上该目录**完全不存在**（本地 490 个文件 / CI 0 个）⇒
D-1~D-4 判 INCONCLUSIVE、G-1 的 ``data_hash`` 取不到 ⇒ CI **永久红**。
红的是"没数据"而不是"数据有问题"，门禁既没在判、又把流水线堵死 —— 两头落空。

本脚本从**本地真实数据**抽样出一份小样，落到**不受 .gitignore 影响**的
``tests/fixtures/ci_min_data/dividend_stocks/``，供 CI 上的数据类门禁**真检**。

抽样规则（确定性，⛔ 不做"挑好看的"质量筛选）
------------------------------------------
* 标的：``data/dividend_stocks`` 下满足「2024 年 >= 130 个交易日 且 切片末行
  ``market_cap > 0``」的标的，按**代码字典序取前 30 只**；
* 时间：每只取 2024 年**连续前 130 个交易日**（真实行原样拷贝，不改任何数值）；
* 除权 sidecar：``exdiv/<symbol>.parquet`` 原样拷贝（D-1 判除权日需要）。

合成停牌日（⛔ 唯一非真实成分，必须可见）
--------------------------------------
实测真实数据集 **2015-2024 全部 488 只标的、3754 个 symbol-year，``tradestatus``
恒为 ``'1'``（0 个停牌日）** —— 无停牌日可抽样。若 fixture 不含停牌日，D-4 会判
**SKIP（不适用）** ⇒ 等于 CI 上这条门禁没在判。故对字典序**前 3 只**标的
（含 D-1/D-4 实际抽样的第一只）各注入 3 个合成停牌日，严格按停牌语义构造：

    tradestatus='0'、volume=0、amount=0、turn=0、pctChg=0、OHLC=preclose

并为保持价格链自洽，把紧随其后交易日的 ``preclose`` 同步修正为该 ``close``。
注入清单写入 ``fixture_manifest.json``，并在门禁取证来源行**显式打印**。

⛔ 如实登记
----------
CI 上数据类门禁校验的是**这份抽样小样**，**不等于**校验全量真实数据质量；
全量校验仍须在本地 ``data/`` 上跑同一条 ``gate_master_audit --ci`` 命令。
本脚本**不改任何门禁阈值/判据**，只是让 CI 有真东西可判。

用法（本地有真实数据时才可重生成）：
    py -3.11 scripts/build_ci_fixture_data.py            # 生成
    py -3.11 scripts/build_ci_fixture_data.py --check    # 只校验现有 fixture
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_DATA_DIR = REPO_ROOT / "data" / "dividend_stocks"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "ci_min_data"
FIXTURE_DATA_DIR = FIXTURE_DIR / "dividend_stocks"
MANIFEST_PATH = FIXTURE_DIR / "fixture_manifest.json"
PROVENANCE_MD = FIXTURE_DIR / "FIXTURE_PROVENANCE.md"

#: 抽样年份（真实数据 2015-2024；2024 年样本最完整）。
SOURCE_YEAR = "2024"
#: 每标的抽样交易日数（要求 ≥120；D-3 需 ≥60，D-1 需 ≥2，本值留出充裕余量）。
DAYS_PER_SYMBOL = 130
#: 抽样标的只数（D-2 需 ≥30）。
SYMBOL_COUNT = 30
#: 注入合成停牌日的标的只数（字典序前 N 只；**必须含第 1 只**——D-1/D-4 只抽样它）。
SUSPENSION_SYMBOL_COUNT = 3
#: 每只标的注入的合成停牌日数。
SUSPENSION_DAYS_PER_SYMBOL = 3
#: 合成停牌日所在的切片下标（避开前 5 根：D-1 对 i<5 视作除权日而豁免）。
SUSPENSION_INDEXES: tuple[int, ...] = (40, 75, 110)

#: 真实 parquet 的**列名集合**与**规范列顺序**（17 列）。
#:
#: ⚠ 实测真实数据存在**两种列顺序**：426/488 只为 ``...adjust_mode, dividend_yield, market_cap``，
#: 62/488 只为 ``...adjust_mode, market_cap, dividend_yield``（如 ``sh.600210``）。
#: parquet 按列名读取，故该漂移不影响语义；fixture **统一归一到占多数的规范顺序**，
#: 使 CI 小样自身不再携带这一处无义漂移（如实登记于 manifest）。
EXPECTED_COLUMNS: tuple[str, ...] = (
    "date", "open", "high", "low", "close", "preclose", "volume", "amount",
    "turn", "pctChg", "tradestatus", "isST", "code", "source", "adjust_mode",
    "dividend_yield", "market_cap",
)


def _pick_symbols(real_dir: Path) -> list[str]:
    """按字典序挑选满足硬条件的 ``SYMBOL_COUNT`` 只标的（⛔ 不做质量筛选）。"""
    picked: list[str] = []
    for sym_dir in sorted(p for p in real_dir.iterdir() if p.is_dir() and p.name.startswith(("sh.", "sz."))):
        path = sym_dir / f"{SOURCE_YEAR}.parquet"
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path, columns=["market_cap"])
        except Exception:                       # noqa: BLE001 —— 读不了的标的直接跳过
            continue
        if len(df) < DAYS_PER_SYMBOL:
            continue
        if float(df["market_cap"].iloc[DAYS_PER_SYMBOL - 1]) <= 0:
            continue
        picked.append(sym_dir.name)
        if len(picked) >= SYMBOL_COUNT:
            break
    return picked


def _inject_suspension(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """按停牌语义注入合成停牌日；返回 ``(新表, 注入日期列表)``。

    停牌日：``tradestatus='0'``、``volume=amount=turn=pctChg=0``、``OHLC=preclose``。
    为保持价格链自洽，紧随其后交易日的 ``preclose`` 同步修正为该 ``close``。
    """
    out = df.reset_index(drop=True).copy()
    injected: list[str] = []
    for idx in SUSPENSION_INDEXES:
        if idx >= len(out):
            continue
        preclose = float(out.at[idx, "preclose"])
        out.at[idx, "open"] = preclose
        out.at[idx, "high"] = preclose
        out.at[idx, "low"] = preclose
        out.at[idx, "close"] = preclose
        out.at[idx, "volume"] = 0
        out.at[idx, "amount"] = 0.0
        out.at[idx, "turn"] = 0.0
        out.at[idx, "pctChg"] = 0.0
        out.at[idx, "tradestatus"] = "0"
        injected.append(str(out.at[idx, "date"])[:10])
        if idx + 1 < len(out):
            out.at[idx + 1, "preclose"] = preclose
    return out, injected


def build(real_dir: Path = REAL_DATA_DIR) -> dict[str, Any]:
    """从真实数据抽样生成 fixture；返回清单 dict。"""
    if not real_dir.exists() or not any(real_dir.iterdir()):
        raise SystemExit(
            f"⛔ 真实数据目录不可用: {real_dir}。本脚本只能从**真实数据**抽样，"
            "⛔ 不得合成数据冒充真实数据（那样 CI 绿了也是自欺）。"
        )

    symbols = _pick_symbols(real_dir)
    if len(symbols) < SYMBOL_COUNT:
        raise SystemExit(f"⛔ 合格标的仅 {len(symbols)} 只 < {SYMBOL_COUNT}，无法生成 fixture。")

    if FIXTURE_DATA_DIR.exists():
        for child in sorted(FIXTURE_DATA_DIR.iterdir(), reverse=True):
            if child.is_dir():
                for f in child.iterdir():
                    f.unlink()
                child.rmdir()
            else:
                child.unlink()
    FIXTURE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DATA_DIR / "exdiv").mkdir(parents=True, exist_ok=True)

    real_meta: dict[str, Any] = {}
    meta_path = real_dir / "meta.json"
    if meta_path.exists():
        try:
            real_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:                       # noqa: BLE001
            real_meta = {}

    suspension_symbols = symbols[:SUSPENSION_SYMBOL_COUNT]
    injected_detail: dict[str, list[str]] = {}
    per_symbol_days: dict[str, int] = {}

    for sym in symbols:
        src = real_dir / sym / f"{SOURCE_YEAR}.parquet"
        df = pd.read_parquet(src).head(DAYS_PER_SYMBOL).reset_index(drop=True)
        if set(df.columns) != set(EXPECTED_COLUMNS):
            raise SystemExit(
                f"⛔ {sym} 列名集合与真实 schema 不一致：\n"
                f"  实际 {list(df.columns)}\n  期望 {list(EXPECTED_COLUMNS)}"
            )
        # 归一列顺序（真实数据里 62/488 只标的两列颠倒，见 EXPECTED_COLUMNS 注释）。
        df = df[list(EXPECTED_COLUMNS)]
        if sym in suspension_symbols:
            df, dates = _inject_suspension(df)
            injected_detail[sym] = dates
        out_dir = FIXTURE_DATA_DIR / sym
        out_dir.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out_dir / f"{SOURCE_YEAR}.parquet", index=False)
        per_symbol_days[sym] = int(len(df))

        sidecar = real_dir / "exdiv" / f"{sym}.parquet"
        if sidecar.exists():
            pd.read_parquet(sidecar).to_parquet(FIXTURE_DATA_DIR / "exdiv" / f"{sym}.parquet", index=False)

    injected_count = sum(len(v) for v in injected_detail.values())
    manifest: dict[str, Any] = {
        "fixture": True,
        "purpose": "CI 最小数据 fixture：data/** 被 .gitignore 排除，CI 无真实数据，供 D-1~D-4 与 G-1 data_hash 真检",
        "generated_by": "scripts/build_ci_fixture_data.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_data_dir": "data/dividend_stocks",
        "source_collection_date": real_meta.get("collection_date"),
        "source_adjust_mode": real_meta.get("adjust_mode"),
        "source_total_symbols": real_meta.get("total_symbols"),
        "selection_rule": (
            f"2024 年 >= {DAYS_PER_SYMBOL} 个交易日且切片末行 market_cap > 0 的标的，"
            f"按代码字典序取前 {SYMBOL_COUNT} 只（⛔ 不做质量筛选）"
        ),
        "year": int(SOURCE_YEAR),
        "total_symbols": len(symbols),
        "days_per_symbol": DAYS_PER_SYMBOL,
        "per_symbol_days": per_symbol_days,
        "symbols": symbols,
        "columns": list(EXPECTED_COLUMNS),
        "real_rows_only": True,
        "synthetic_suspension_days": {
            "count": injected_count,
            "symbols": suspension_symbols,
            "dates": injected_detail,
            "construction": "tradestatus='0', volume=amount=turn=pctChg=0, OHLC=preclose；其后一日 preclose 同步修正",
            "reason": (
                "真实数据集 2015-2024（488 标的 / 3754 symbol-year）tradestatus 恒为 '1'，"
                "实测 0 个停牌日可抽样；若 fixture 无停牌日，D-4 会判 SKIP（不适用）⇒ CI 上等于没在判"
            ),
        },
        "warning": (
            "⛔ 本 fixture 是抽样小样：CI 上的数据类门禁校验的是它，"
            "**不等于**校验全量真实数据质量；全量校验须在本地 data/ 上跑同一命令"
        ),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    fixture_meta = {
        "fixture": True,
        "note": (
            "CI 最小 fixture（非全量真实数据）。抽样自 data/dividend_stocks："
            f"{len(symbols)} 只 × {DAYS_PER_SYMBOL} 天（{SOURCE_YEAR} 年），含合成停牌日 {injected_count} 天。"
        ),
        "collection_date": real_meta.get("collection_date"),
        "start_date": f"{SOURCE_YEAR}-01-01",
        "end_date": f"{SOURCE_YEAR}-12-31",
        "total_symbols": len(symbols),
        "done_symbols": len(symbols),
        "successful": len(symbols),
        "failed_this_run": [],
        "adjust_mode": real_meta.get("adjust_mode", "RAW"),
        "index_symbol": real_meta.get("index_symbol", "sh.000300"),
        "source": f"ci-fixture(sampled from {real_meta.get('source', 'data/dividend_stocks')})",
        "synthetic_suspension_days": injected_count,
    }
    (FIXTURE_DATA_DIR / "meta.json").write_text(
        json.dumps(fixture_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_provenance_md(manifest)
    return manifest


def _write_provenance_md(manifest: dict[str, Any]) -> None:
    """写出人类可读的出处说明（⛔ 合成成分必须写在最显眼处）。"""
    susp = manifest["synthetic_suspension_days"]
    text = f"""# CI 最小数据 fixture（tests/fixtures/ci_min_data）

## 它是什么

`data/**` 被 `.gitignore` 排除 ⇒ GitHub Actions 上 `data/dividend_stocks/` **完全不存在**
（本地 {manifest.get('source_total_symbols')} 只标的 / CI 0 个）⇒ D-1~D-4 判 INCONCLUSIVE、
G-1 的 `data_hash` 取不到 ⇒ CI 永久红。**红的是"没数据"，不是"数据有问题"**。

本 fixture 让 CI 有**真东西可判**：⛔ 不是给门禁加豁免，⛔ 没有放宽任何阈值/判据。

## 抽样来源与规模

| 项 | 值 |
|---|---|
| 来源 | `data/dividend_stocks`（真实采集，{manifest.get('source_adjust_mode')} 不复权） |
| 源采集时间 | {manifest.get('source_collection_date')} |
| 抽样规则 | {manifest['selection_rule']} |
| 年份 | {manifest['year']} |
| 标的数 | {manifest['total_symbols']} |
| 每标的交易日 | {manifest['days_per_symbol']}（真实行原样拷贝，未改数值） |
| 列结构 | 与真实 parquet **逐列一致**（17 列，含 `market_cap`/`dividend_yield`/`tradestatus`） |
| 除权 sidecar | `exdiv/<symbol>.parquet` 原样拷贝 |

## ⛔ 合成成分（唯一非真实部分，必须可见）

真实数据 **2015-2024 全部 488 只标的 / 3754 个 symbol-year 的 `tradestatus` 恒为 `'1'`**
—— 实测**没有任何停牌日**可抽样。若 fixture 不含停牌日，D-4 会判 **SKIP（不适用）**，
等于 CI 上这条门禁没在判。故对字典序前 {len(susp['symbols'])} 只标的（含 D-1/D-4 实际抽样的第一只）
各注入 **{susp['count'] // max(len(susp['symbols']), 1)} 个合成停牌日**，按停牌语义构造：

```
tradestatus='0'、volume=0、amount=0、turn=0、pctChg=0、OHLC=preclose
```

（其后一交易日的 `preclose` 同步修正，保持价格链自洽。）

注入标的与日期：见 `fixture_manifest.json::synthetic_suspension_days`。

## ⛔ 边界（不许含糊）

CI 上数据类门禁校验的是**这份抽样小样** —— **不等于**校验全量真实数据质量。
全量校验仍须在本地 `data/` 上跑同一条命令：

```bash
py -3.11 -m scripts.gates.gate_master_audit --ci
```

本地有真实数据时，门禁**优先取真实数据**，本 fixture 不参与（行为完全不变）。

## 重新生成

```bash
py -3.11 scripts/build_ci_fixture_data.py
py -3.11 scripts/build_ci_fixture_data.py --check
```
"""
    PROVENANCE_MD.write_text(text, encoding="utf-8")


def check() -> int:
    """校验现有 fixture：schema 一致 / 规模达标 / 抽样逻辑能取出证据。"""
    errors: list[str] = []
    if not FIXTURE_DATA_DIR.exists():
        print(f"⛔ fixture 不存在: {FIXTURE_DATA_DIR}")
        return 1
    if not MANIFEST_PATH.exists():
        errors.append(f"缺清单 {MANIFEST_PATH}")
    syms = sorted(
        p for p in FIXTURE_DATA_DIR.iterdir()
        if p.is_dir() and p.name.startswith(("sh.", "sz."))
    )
    if len(syms) < 30:
        errors.append(f"标的数 {len(syms)} < 30（D-2 需 >= 30）")
    for sym in syms:
        parts = sorted(p for p in sym.glob("*.parquet") if p.stem.isdigit())
        if not parts:
            errors.append(f"{sym.name}: 无年份 parquet")
            continue
        df = pd.read_parquet(parts[-1])
        if tuple(df.columns) != EXPECTED_COLUMNS:
            errors.append(f"{sym.name}: 列结构漂移 -> {list(df.columns)}")
        if len(df) < 120:
            errors.append(f"{sym.name}: {len(df)} 天 < 120")

    # 用**门禁自己**的抽样逻辑 + 门禁本体做一次端到端自检
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts.gates.context_builder import collect_data_evidence
    from scripts.gates.gate_d_data import (
        FloatMarketCapGate, PitDividendYieldGate, RawPriceJumpGate, SuspensionVolumeGate,
    )

    ev = collect_data_evidence(FIXTURE_DATA_DIR)
    for gate in (RawPriceJumpGate(), FloatMarketCapGate(), PitDividendYieldGate(), SuspensionVolumeGate()):
        res = gate.evaluate(ev)
        flag = "OK " if res.status.value == "PASS" else "!! "
        print(f"  {flag}[{res.status.value}] {gate.gate_id} {res.message[:110]}")
        if res.status.value != "PASS":
            errors.append(f"{gate.gate_id} 在 fixture 上未 PASS: {res.status.value} - {res.message}")

    if errors:
        print("⛔ fixture 自检未通过：")
        for e in errors:
            print("   -", e)
        return 1
    print(f"[OK] fixture 自检通过（{len(syms)} 只标的，D-1~D-4 全部 PASS）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成/校验 CI 最小数据 fixture")
    parser.add_argument("--check", action="store_true", help="只校验现有 fixture（不重新生成）")
    args = parser.parse_args(argv)

    if args.check:
        return check()

    manifest = build()
    print(
        f"[OK] 已生成 {manifest['total_symbols']} 只 × {manifest['days_per_symbol']} 天 fixture -> {FIXTURE_DATA_DIR}"
    )
    print(f"     合成停牌日 {manifest['synthetic_suspension_days']['count']} 天（真实集无停牌日可抽样，已如实登记）")
    return check()


if __name__ == "__main__":
    sys.exit(main())
