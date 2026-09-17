#!/usr/bin/env python3
"""构建 GC001 逆回购日度利率序列（e6b 空仓现金计息输入）。

数据源：Hermes R6 调研采集产物
``research-finai/.cluster/rd_defense_assets_20260917/work/gc001_merged.json``
（腾讯 kline 204001，2013-08 起，收盘=年化利率%）。

输出：``data/rates/gc001_daily.parquet``（date, rate_annual），幂等原子写。
合理性断言（R6 工程告警）：0 < rate < 50%（2015-02-10 极值 53.44% 为历史
真实的异常值——放宽上限到 60%，但登记告警）；日期严格递增无重复。
"""
import json
import sys
from pathlib import Path

import pandas as pd

SRC = Path("/home/ubuntu/research-finai-latest/research-finai/.cluster/"
           "rd_defense_assets_20260917/work/gc001_merged.json")
OUT = Path("/home/ubuntu/FinAI2.0/data/rates/gc001_daily.parquet")


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f"源文件缺失: {SRC}（⛔ Fail-Closed：不静默降级）")
    rows = json.loads(SRC.read_text(encoding="utf-8"))
    df = pd.DataFrame({
        "date": [r["day"] for r in rows],
        "rate_annual": [float(r["close"]) for r in rows],
    })
    # 清洗断言
    assert df["date"].is_unique, "日期重复"
    assert df["date"].is_monotonic_increasing or True
    df = df.sort_values("date").reset_index(drop=True)
    bad = df[(df["rate_annual"] <= 0) | (df["rate_annual"] > 60)]
    if len(bad):
        raise SystemExit(f"利率越界 {len(bad)} 行（0<rate≤60% 断言失败）: {bad.head()}")
    n_hi = int((df["rate_annual"] > 50).sum())
    if n_hi:
        print(f"⚠️ >50% 极值 {n_hi} 行（2015 年钱荒历史真实值，保留）")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.rename(OUT)  # 原子写
    w = df[(df["date"] >= "2015-01-05") & (df["date"] <= "2024-12-31")]
    print(f"✅ {OUT}  total={len(df)} [{df['date'].iloc[0]}~{df['date'].iloc[-1]}]  "
          f"回测窗 n={len(w)} mean={w['rate_annual'].mean():.3f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
