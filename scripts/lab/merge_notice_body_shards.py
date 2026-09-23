"""scripts/lab/merge_notice_body_shards.py —— e62 公告正文分片合并。

把子会话上传到 Release `notice-body-shards` 的分片 parquet 与本地
data/notice_body/<year>.parquet 合并，按 art_code 去重（keep='first'，
分片内容一致时顺序无关），写回 data/notice_body/<year>.parquet。

用法：
  python scripts/lab/merge_notice_body_shards.py --shard-dir /path/to/shards
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data/notice_body"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard-dir", required=True)
    args = ap.parse_args()

    shards = sorted(glob.glob(str(Path(args.shard_dir) / "*.parquet")))
    if not shards:
        print(f"no shards in {args.shard_dir}")
        return 1
    frames = []
    for s in shards:
        d = pd.read_parquet(s)
        frames.append(d)
        print(f"{Path(s).name}: {len(d)} rows")
    new = pd.concat(frames, ignore_index=True)
    new["year"] = new["ann_date"].astype(str).str[:4]
    for year, grp in new.groupby("year"):
        target = OUT_DIR / f"{year}.parquet"
        if target.exists():
            old = pd.read_parquet(target)
            merged = pd.concat([old, grp], ignore_index=True)
        else:
            merged = grp
        before = len(merged)
        merged = merged.drop_duplicates(subset="art_code", keep="first")
        merged = merged.sort_values(["ann_date", "art_code"])
        merged.to_parquet(target, index=False)
        print(f"{year}: {before} -> {len(merged)} "
              f"(-{before - len(merged)} dupes) -> {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
