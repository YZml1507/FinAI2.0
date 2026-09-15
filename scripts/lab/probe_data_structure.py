#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""临时探查：日线数据结构 + 日期范围 + 指数覆盖 + 红利池规模。
产物：仅打印，不写任何权威目录。
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import pandas as pd

ROOT = Path("/home/ubuntu/FinAI2.0")
DS = ROOT / "data" / "dividend_stocks"

# 1) 顶层结构
dirs = [p for p in DS.iterdir() if p.is_dir()]
files = [p for p in DS.iterdir() if p.is_file()]
print("[top] dirs=%d files=%d" % (len(dirs), len(files)))
print("[top] first5_dirs=%s" % [d.name for d in sorted(dirs)[:5]])
print("[top] files=%s" % [f.name for f in sorted(files)[:10]])

# 2) 股票日线 parquet（排除 exdiv 与指数）
bar_files = sorted(
    f for f in glob.glob(str(DS / "*" / "*.parquet"))
    if "/exdiv/" not in f and "sh.000300" not in f
)
print("\n[bars] non-exdiv non-index parquet count: %d" % len(bar_files))
for f in bar_files[:3]:
    print("  eg: %s/%s" % (Path(f).parent.name, Path(f).name))

if bar_files:
    df = pd.read_parquet(bar_files[0])
    print("\n[sample] %s" % Path(bar_files[0]).name)
    print("  cols: %s" % list(df.columns))
    print("  rows: %d" % len(df))
    if len(df):
        c0 = df.columns[0]
        print("  col0: %s dtype=%s" % (c0, df[c0].dtype))
        print("  range: %s -> %s" % (df[c0].min(), df[c0].max()))

# 3) 指数数据
idx_files = sorted(glob.glob(str(DS / "sh.000300" / "*.parquet")))
print("\n[index sh.000300] files: %s" % idx_files)
if idx_files:
    idf = pd.read_parquet(idx_files[0])
    print("  cols: %s" % list(idf.columns))
    print("  rows: %d" % len(idf))
    if len(idf):
        c0 = idf.columns[0]
        print("  range: %s -> %s" % (idf[c0].min(), idf[c0].max()))

# 4) meta.json 摘要
meta_p = DS / "meta.json"
if meta_p.exists():
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    keys = list(meta.keys())[:20]
    print("\n[meta.json] nkeys=%d keys=%s" % (len(meta), keys))
    for k in keys[:6]:
        print("  %s: %s" % (k, str(meta[k])[:120]))

# 5) 红利池规模
stock_dirs = [d.name for d in dirs if d.name not in ("exdiv",) and d.name != "sh.000300"]
print("\n[pool] stock dirs: %d" % len(stock_dirs))
print("[pool] sample: %s" % sorted(stock_dirs)[:8])
