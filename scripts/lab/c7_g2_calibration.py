#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C7：G-2 判据本仓标定（纯只读机械统计，⛔ 零回测、零写库、不改仓）。

输入：`experiments/lab/*/experiment.json` + `experiments/lab/leaderboard.jsonl`
（仅读）。输出：`experiments/lab/c7-g2-calibration/result.json`
（``.tmp`` → ``os.replace`` 原子落盘；无时间戳字段 ⇒ 同输入字节级幂等）。

计算内容（对应标定报告 `docs/audit/c7_g2_calibration_20260919.md`）：

a) **Lipschitz 实测**：对 e8b-hard ±20% 8 格网格与 e11-linear 15 格网格
   （另附 e8b ±10% pg2 9 格、e11 16 格含基点两种口径），按「行/列内相邻格点」
   （同一 a 上 d 排序相邻、同一 d 上 a 排序相邻——欧氏/曼哈顿最近邻的轴对齐
   退化形态）枚举邻边，计算 |ΔCAGR|/|Δθ| 分布与 L×10%（pp 口径）。
   应复现 e11 的 L=2.472（worst pair (0.225,0.385)→(0.25,0.385)）。
b) **平台宽度两口径**：每组网格分别按旧口径（CAGR ≥ (2/3)×peak）与新口径
   （|CAGR − peak| ≤ 1.5pp）计算达标格点占比。
c) **噪声/语义扰动地板**：对照扰动组（pg-y080/pg-y120/pg-fee2/pg-cap10）
   与 e8b 基点 CAGR 之 |Δ|；外加「同签名复跑组」（overrides+run_params 规范化
   后完全相同的实验簇）逐指标最大 |Δ|——登记 isst 整批重跑与旧指纹同名实验
   逐值一致（复制噪声≈0）及跨 data_hash 同签名差（isST 语义重建影响，非噪声）。
d) **换手分布**：全部网格格点 annual_turnover 的 min/max/分位，对照 ≤8 判据。

用法：``.venv/bin/python scripts/lab/c7_g2_calibration.py``
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "experiments" / "lab"
LEADERBOARD = LAB / "leaderboard.jsonl"
OUT_DIR = LAB / "c7-g2-calibration"
OUT_FILE = OUT_DIR / "result.json"

#: e8b 冠军构型基点（d=0.25 / a=0.35），±20% 网格围绕它展开。
E8B_BASE = "e8b-gc001-e7-combo"
#: e11-linear 主实验基点（linear 权重映射，同 d/a）。
E11_BASE = "e11-linear"

#: 噪声/语义扰动对照组（相对 e8b 基点，同回测窗）。
CONTROL_NAMES = ("pg-y080", "pg-y120", "pg-fee2", "pg-cap10")
#: 窗口段扰动（不同回测窗 ⇒ 不属噪声地板，单独登记）。
WINDOW_NAMES = ("pg-holdout-e8b", "pg-fit-e8b")

#: 网格成员名正则（坐标一律读 experiment.json overrides 权威值，名正则只定成员资格）。
RE_E8B_PG = re.compile(r"^pg-a\d+d\d+$")
RE_E8B_PG2 = re.compile(r"^pg2-a\d+d\d+$")
RE_E11_PG = re.compile(r"^e11-linear-pg-a\d+d\d+$")
RE_BD = re.compile(r"^bd\d+a\d+m\d+i\d+$")

#: 指纹规范化时忽略的注入字段（breadth_series 为宽度数据注入，非旋钮）。
SIG_EXCLUDE = {"breadth_series"}
#: run_params 缺省（旧实验无该键）时回填的默认值。
RUN_PARAMS_DEFAULT = {
    "backtest_start": None,
    "backtest_end": None,
    "initial_capital": "150000",
    "fee_multiplier": "1",
    "universe_yearly_pool": None,
}
#: 复跑组逐值比对字段。
METRIC_FIELDS = (
    "cagr", "max_drawdown", "annual_turnover", "win_rate",
    "round_trips", "fees_sum", "final_nav", "total_return",
)

PLATFORM_ABS_WINDOW = 0.015  # 1.5pp 绝对窗口（新口径，与「代价过大」线同源）
PLATFORM_REL = 2.0 / 3.0     # (2/3)×peak 相对口径（旧）
G2G_TURNOVER_LIMIT = 8.0


# ----------------------------------------------------------------------
# 载入与规范化
# ----------------------------------------------------------------------
def _norm_overrides(ov: dict) -> tuple:
    """overrides → 规范化签名项（去注入字段、按键排序、值转 str）。"""
    return tuple(sorted(
        (str(k), str(v)) for k, v in ov.items() if k not in SIG_EXCLUDE))


def _norm_run_params(rp: dict | None) -> tuple:
    rp = dict(RUN_PARAMS_DEFAULT) | dict(rp or {})
    return tuple(sorted((str(k), str(v)) for k, v in rp.items()))


def load_experiments(lab_dir: Path = LAB) -> dict:
    """扫描 ``<lab_dir>/*/experiment.json`` → {name: 规范化记录}。fail-closed。"""
    recs: dict = {}
    for f in sorted(lab_dir.glob("*/experiment.json")):
        j = json.loads(f.read_text(encoding="utf-8"))
        name = j["experiment"]
        ov = j.get("overrides") or {}
        rec = {
            "name": name,
            "cagr": float(j["cagr"]),
            "max_drawdown": float(j["max_drawdown"]),
            "annual_turnover": float(j["annual_turnover"]),
            "win_rate": float(j.get("win_rate") or 0),
            "round_trips": int(j.get("round_trips") or 0),
            "fees_sum": float(j.get("fees_sum") or 0),
            "final_nav": float(j.get("final_nav") or 0),
            "total_return": float(j.get("total_return") or 0),
            "run_id": j.get("run_id"),
            "overrides": _norm_overrides(ov),
            "run_params": _norm_run_params(j.get("run_params")),
            "raw_overrides": ov,
        }
        rec["signature"] = (rec["overrides"], rec["run_params"])
        recs[name] = rec
    if not recs:
        raise SystemExit(f"[FAIL] {lab_dir} 下无 experiment.json（fail-closed）")
    return recs


def _da(rec: dict) -> tuple[float, float]:
    """从 overrides 权威读 (defense, attack)；缺键 fail-closed。"""
    ov = rec["raw_overrides"]
    try:
        return float(ov["breadth_defense_threshold"]), float(ov["breadth_attack_threshold"])
    except KeyError as exc:
        raise SystemExit(f"[FAIL] {rec['name']} 缺 {exc}（fail-closed）") from exc


def _grid_cells(recs: dict, names: list[str]) -> list[dict]:
    cells = []
    for n in names:
        if n not in recs:
            raise SystemExit(f"[FAIL] 网格成员 {n} 无 experiment.json（fail-closed）")
        d, a = _da(recs[n])
        cells.append({
            "name": n, "d": d, "a": a,
            "cagr": recs[n]["cagr"],
            "annual_turnover": recs[n]["annual_turnover"],
        })
    return cells


# ----------------------------------------------------------------------
# a) Lipschitz：行/列内相邻格点
# ----------------------------------------------------------------------
def neighbor_pairs(cells: list[dict]) -> list[tuple]:
    """轴对齐最近邻：同一 a 内 d 排序相邻、同一 d 内 a 排序相邻的格点对。"""
    pts = {(c["d"], c["a"]) for c in cells}
    pairs: set = set()
    by_a: dict[float, list] = {}
    by_d: dict[float, list] = {}
    for d, a in pts:
        by_a.setdefault(a, []).append(d)
        by_d.setdefault(d, []).append(a)
    for a, ds in by_a.items():
        ds.sort()
        for i in range(len(ds) - 1):
            pairs.add(((ds[i], a), (ds[i + 1], a)))
    for d, as_ in by_d.items():
        as_.sort()
        for i in range(len(as_) - 1):
            pairs.add(((d, as_[i]), (d, as_[i + 1])))
    return sorted(pairs)


def lipschitz(cells: list[dict]) -> dict:
    grid = {(c["d"], c["a"]): c["cagr"] for c in cells}
    if len(grid) != len(cells):
        raise SystemExit("[FAIL] 网格 (d,a) 坐标重复（fail-closed）")
    rows = []
    for p1, p2 in neighbor_pairs(cells):
        dd = abs(round(p1[0] - p2[0], 6))
        da = abs(round(p1[1] - p2[1], 6))
        dtheta = (dd * dd + da * da) ** 0.5
        if dtheta == 0:
            continue
        lv = abs(grid[p1] - grid[p2]) / dtheta
        rows.append({
            "p1": list(p1), "p2": list(p2),
            "d_theta": round(dtheta, 6),
            "abs_d_cagr": round(abs(grid[p1] - grid[p2]), 8),
            "l": lv,
        })
    if not rows:
        raise SystemExit("[FAIL] 网格无相邻对（fail-closed）")
    rows.sort(key=lambda r: -r["l"])
    vals = sorted(r["l"] for r in rows)
    n = len(vals)

    def _q(p: float) -> float:
        k = (n - 1) * p
        f = int(k)
        c = k - f
        return vals[f] + (vals[f + 1] - vals[f]) * c if f + 1 < n else vals[f]

    return {
        "n_pairs": n,
        "L": round(rows[0]["l"], 6),
        "L_x10pct_pp": round(rows[0]["l"] * 0.10 * 100, 6),
        "worst_pair": [rows[0]["p1"], rows[0]["p2"]],
        "l_min": round(vals[0], 6),
        "l_p25": round(_q(0.25), 6),
        "l_p50": round(_q(0.50), 6),
        "l_p75": round(_q(0.75), 6),
        "l_max": round(vals[-1], 6),
        "pairs": rows,
    }


# ----------------------------------------------------------------------
# b) 平台宽度两口径
# ----------------------------------------------------------------------
def platform_width(cells: list[dict]) -> dict:
    vals = [c["cagr"] for c in cells]
    peak = max(vals)
    peak_names = [c["name"] for c in cells if c["cagr"] == peak]
    n = len(vals)
    old_ok = [c["name"] for c in cells if c["cagr"] >= PLATFORM_REL * peak]
    new_ok = [c["name"] for c in cells if abs(c["cagr"] - peak) <= PLATFORM_ABS_WINDOW]
    return {
        "n_cells": n,
        "peak_cagr": peak,
        "peak_cells": sorted(peak_names),
        "old_rel_2_3": {
            "cutoff": round(PLATFORM_REL * peak, 8),
            "n_ok": len(old_ok),
            "ratio": round(len(old_ok) / n, 6),
            "ok_cells": sorted(old_ok),
        },
        "new_abs_1p5pp": {
            "window": PLATFORM_ABS_WINDOW,
            "n_ok": len(new_ok),
            "ratio": round(len(new_ok) / n, 6),
            "ok_cells": sorted(new_ok),
        },
    }


# ----------------------------------------------------------------------
# c) 噪声/语义扰动地板 + 同签名复跑组
# ----------------------------------------------------------------------
def _mechanism(rec: dict, base: dict) -> str:
    """对照扰动相对基点的机制标签（由 overrides/run_params 差集机械推断）。"""
    ov, rp = dict(rec["raw_overrides"]), dict(rec["run_params"])
    bov, brp = dict(base["raw_overrides"]), dict(base["run_params"])
    if ov.get("cash_yield_series") != bov.get("cash_yield_series"):
        tag = str(ov.get("cash_yield_series", ""))
        m = re.search(r"_x(\d+)", tag)
        if m:
            return f"GC001 现金利率 ×{int(m.group(1)) / 100:.2f}"
        return f"现金利率序列变更 {tag}"
    if rp.get("fee_multiplier") != brp.get("fee_multiplier"):
        return f"六科目费率 ×{rp.get('fee_multiplier')}"
    if rp.get("initial_capital") != brp.get("initial_capital"):
        return (f"初始本金 {brp.get('initial_capital')}→"
                f"{rp.get('initial_capital')}")
    diff = {k for k in set(ov) | set(bov) if ov.get(k) != bov.get(k)}
    return f"overrides 差集 {sorted(diff)}"


def perturbation_floor(recs: dict) -> dict:
    base = recs.get(E8B_BASE)
    if base is None:
        raise SystemExit(f"[FAIL] 缺基点 {E8B_BASE}（fail-closed）")
    controls = []
    for n in CONTROL_NAMES:
        if n not in recs:
            raise SystemExit(f"[FAIL] 缺对照扰动 {n}（fail-closed）")
        rec = recs[n]
        controls.append({
            "name": n,
            "mechanism": _mechanism(rec, base),
            "cagr": rec["cagr"],
            "delta_cagr_pp": round((rec["cagr"] - base["cagr"]) * 100, 6),
            "abs_delta_cagr_pp": round(abs(rec["cagr"] - base["cagr"]) * 100, 6),
        })
    windows = []
    for n in WINDOW_NAMES:
        if n in recs:
            rec = recs[n]
            rp = dict(rec["run_params"])
            windows.append({
                "name": n,
                "window": [rp.get("backtest_start"), rp.get("backtest_end")],
                "cagr": rec["cagr"],
                "delta_cagr_pp": round((rec["cagr"] - base["cagr"]) * 100, 6),
            })
    return {
        "base": E8B_BASE,
        "base_cagr": base["cagr"],
        "controls": controls,
        "window_segments": windows,
    }


def replication_groups(recs: dict) -> list[dict]:
    """同签名（overrides+run_params 规范化全同）实验簇 → 逐指标最大 |Δ|。"""
    groups: dict[tuple, list] = {}
    for rec in recs.values():
        groups.setdefault(rec["signature"], []).append(rec)
    out = []
    for sig, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: r["name"])
        deltas = {}
        for f in METRIC_FIELDS:
            vals = [m[f] for m in members]
            deltas[f] = round(max(vals) - min(vals), 8)
        sig_hash = hashlib.sha256(
            json.dumps(sig, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        out.append({
            "members": [m["name"] for m in members],
            "run_ids": {m["name"]: m["run_id"] for m in members},
            "signature_hash": sig_hash,
            "max_abs_delta": deltas,
            "all_metrics_identical": all(v == 0 for v in deltas.values()),
        })
    out.sort(key=lambda g: g["members"])
    return out


# ----------------------------------------------------------------------
# d) 换手分布
# ----------------------------------------------------------------------
def turnover_stats(cells: list[dict]) -> dict:
    vals = sorted(c["annual_turnover"] for c in cells)
    n = len(vals)

    def _q(p: float) -> float:
        k = (n - 1) * p
        f = int(k)
        c = k - f
        return vals[f] + (vals[f + 1] - vals[f]) * c if f + 1 < n else vals[f]

    return {
        "n": n,
        "min": round(vals[0], 6),
        "p25": round(_q(0.25), 6),
        "p50": round(_q(0.50), 6),
        "p75": round(_q(0.75), 6),
        "max": round(vals[-1], 6),
        "n_gt_8": sum(1 for v in vals if v > G2G_TURNOVER_LIMIT),
        "all_le_8": vals[-1] <= G2G_TURNOVER_LIMIT,
    }


# ----------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------
def build_grids(recs: dict) -> dict:
    """三组任务网格 + 一组补充细网格（成员由名正则定、坐标读 overrides）。"""
    pg_names = sorted(n for n in recs if RE_E8B_PG.match(n))
    pg2_names = sorted(n for n in recs if RE_E8B_PG2.match(n))
    e11pg_names = sorted(n for n in recs if RE_E11_PG.match(n))
    bd_names = sorted(n for n in recs if RE_BD.match(n))
    grids = {}
    g = _grid_cells(recs, pg_names + [E8B_BASE])
    grids["e8b_hard_pm20"] = {
        "desc": "e8b-hard ±20%（基点 0.25/0.35 + 7 扰动格，d/a 步 0.05/0.07）",
        "cells": g,
        "lipschitz": lipschitz(g),
        "platform": platform_width(g),
    }
    g = _grid_cells(recs, pg2_names + [E8B_BASE])
    grids["e8b_hard_pm10_pg2"] = {
        "desc": "e8b-hard ±10% 内圈（基点 + 8 扰动格，d/a 步 0.025/0.035；补充证据）",
        "cells": g,
        "lipschitz": lipschitz(g),
        "platform": platform_width(g),
    }
    g = _grid_cells(recs, e11pg_names)
    grids["e11_linear_pm20_15"] = {
        "desc": "e11-linear ±20% 15 扰动格（d 步 0.025 / a 步 0.03~0.04；复现口径）",
        "cells": g,
        "lipschitz": lipschitz(g),
        "platform": platform_width(g),
    }
    g16 = _grid_cells(recs, e11pg_names + [E11_BASE])
    grids["e11_linear_pm20_16"] = {
        "desc": "e11-linear 15 扰动格 + 基点 (0.25,0.35)（补充口径）",
        "cells": g16,
        "lipschitz": lipschitz(g16),
        "platform": platform_width(g16),
    }
    g = _grid_cells(recs, bd_names)
    grids["bd24"] = {
        "desc": "bd 24 粗格（d×a×mid_cap×ice 四维；只算平台宽度/换手，不算 L）",
        "cells": g,
        "platform": platform_width(g),
    }
    return grids


def _inputs_fingerprint() -> str:
    """输入指纹（全部 experiment.json 原文 sha256 聚合）——幂等性自检锚。"""
    h = hashlib.sha256()
    for f in sorted(LAB.glob("*/experiment.json")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    h.update(LEADERBOARD.read_bytes() if LEADERBOARD.exists() else b"")
    return h.hexdigest()[:16]


def compute_all() -> dict:
    recs = load_experiments()
    grids = build_grids(recs)
    # d) 换手：全部网格格点并集（按 name 去重）
    seen: dict[str, dict] = {}
    for g in grids.values():
        for c in g["cells"]:
            seen[c["name"]] = c
    union_cells = [seen[k] for k in sorted(seen)]
    result = {
        "schema": "c7-g2-calibration/v1",
        "inputs": {
            "lab_dir": str(LAB.relative_to(ROOT)),
            "leaderboard": str(LEADERBOARD.relative_to(ROOT)),
            "n_experiments": len(recs),
            "fingerprint": _inputs_fingerprint(),
        },
        "a_lipschitz": {
            k: v["lipschitz"] for k, v in grids.items() if "lipschitz" in v
        },
        "b_platform_width": {k: v["platform"] for k, v in grids.items()},
        "c_perturbation_floor": perturbation_floor(recs),
        "c_replication_groups": replication_groups(recs),
        "d_turnover": turnover_stats(union_cells),
        "grids_meta": {k: {"desc": v["desc"], "n_cells": v["platform"]["n_cells"]}
                       for k, v in grids.items()},
    }
    return result


def write_result(result: dict, out_file: Path = OUT_FILE) -> Path:
    """原子落盘：``.tmp`` → ``os.replace``（幂等：同输入 ⇒ 同字节）。"""
    out_file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, indent=1,
                         sort_keys=True) + "\n"
    tmp = out_file.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, out_file)
    return out_file


def _pp(x: float) -> str:
    return f"{x:.4f}pp"


def print_summary(result: dict) -> None:
    print("=" * 64)
    print("C7 G-2 判据本仓标定（只读机械统计）")
    print(f"输入: {result['inputs']['n_experiments']} 组 experiment.json "
          f"指纹 {result['inputs']['fingerprint']}")
    print("=" * 64)
    print("\n[a] Lipschitz（|ΔCAGR|/|Δθ|，轴对齐最近邻）")
    for k, l in result["a_lipschitz"].items():
        print(f"  {k:22s} pairs={l['n_pairs']:2d} L={l['L']:.4f} "
              f"L×10%={_pp(l['L_x10pct_pp'])} worst={l['worst_pair']}")
        print(f"  {'':22s} min={l['l_min']:.3f} p50={l['l_p50']:.3f} "
              f"p75={l['l_p75']:.3f} max={l['l_max']:.3f}")
    print("\n[b] 平台宽度两口径（占比）")
    for k, p in result["b_platform_width"].items():
        o, nw = p["old_rel_2_3"], p["new_abs_1p5pp"]
        print(f"  {k:22s} n={p['n_cells']:2d} peak={p['peak_cagr']:.4%} "
              f"旧(≥2/3peak)={o['n_ok']}/{p['n_cells']}={o['ratio']:.1%} "
              f"新(|Δ|≤1.5pp)={nw['n_ok']}/{p['n_cells']}={nw['ratio']:.1%}")
    print("\n[c] 噪声/语义扰动地板（vs e8b 基点 "
          f"{result['c_perturbation_floor']['base_cagr']:.4%}）")
    for c in result["c_perturbation_floor"]["controls"]:
        print(f"  {c['name']:14s} {c['mechanism']:26s} "
              f"CAGR={c['cagr']:.4%} Δ={c['delta_cagr_pp']:+.4f}pp")
    for w in result["c_perturbation_floor"]["window_segments"]:
        print(f"  {w['name']:14s} 窗口 {w['window'][0]}~{w['window'][1]} "
              f"CAGR={w['cagr']:.4%} Δ={w['delta_cagr_pp']:+.4f}pp")
    print("  同签名复跑组（逐指标最大|Δ|）：")
    for g in result["c_replication_groups"]:
        tag = "全等" if g["all_metrics_identical"] else "有差"
        print(f"    [{tag}] {g['members']} "
              f"max|Δcagr|={g['max_abs_delta']['cagr']:.6f} "
              f"max|Δround_trips|={g['max_abs_delta']['round_trips']}")
    t = result["d_turnover"]
    print(f"\n[d] 换手分布 n={t['n']} min={t['min']:.3f} p25={t['p25']:.3f} "
          f"p50={t['p50']:.3f} p75={t['p75']:.3f} max={t['max']:.3f} "
          f">8={t['n_gt_8']} ⇒ {'全≤8' if t['all_le_8'] else '超限'}")


def main() -> int:
    result = compute_all()
    out = write_result(result)
    print_summary(result)
    print(f"\n[OK] 落盘 {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
