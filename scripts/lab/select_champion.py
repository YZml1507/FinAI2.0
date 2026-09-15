#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从实验榜单选冠军（确定性逻辑，零 AI 依赖）。

规则：
* 主排序：max_drawdown 升序（MDD 越小越好，对齐 G-MDD-1 红线目标）；
* 次排序：cagr 降序（MDD 相同时收益高者优）；
* 仅选择 metrics 完整（cagr 与 max_drawdown 均可解析为数值）的记录。

输出（stdout，两行，供 shell 读取）：
    第 1 行：实验名
    第 2 行：--set k=v 形式的参数串（可直接拼进 run_experiment.py 命令行）
    第 3 行：MDD 值
    第 4 行：CAGR 值
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

BOARD = Path(__file__).resolve().parents[2] / "experiments" / "lab" / "leaderboard.jsonl"


def main() -> int:
    records = []
    for line in BOARD.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        try:
            mdd = Decimal(str(d["max_drawdown"]))
            cagr = Decimal(str(d["cagr"]))
        except Exception:
            continue
        records.append((mdd, cagr, d))

    if not records:
        print("ERROR: 榜单为空", file=sys.stderr)
        return 1

    records.sort(key=lambda t: (t[0], -t[1]))
    mdd, cagr, champ = records[0]
    ov = champ.get("overrides", {})
    param_str = " ".join(f"--set {k}={v}" for k, v in sorted(ov.items()))

    print(champ["experiment"])
    print(param_str)
    print(str(mdd))
    print(str(cagr))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
