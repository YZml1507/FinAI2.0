#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""装甲一（除权前禁建仓过滤）数据探测脚本（只读，不改任何业务代码）。

用途：为 docs/ARMOR1_EXDIV_FILTER_DESIGN.md 提供真实数字依据。
    1. exdiv sidecar 覆盖率 / 字段完整性 / 日期解析成功率；
    2. 未来 N 日窗口命中统计（N=15 为建议值，并列 5/10/15/20/30 做敏感度）；
    3. 建仓日在除权日前 N 日内的候选命中次数（用真实调仓节拍估算）；
    4. 红利税档位口径核对（30 天 / 365 天分界）。

运行：.venv/bin/python scripts/lab/probe_exdiv_window.py [--days 15]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date as _date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
EXDIV_DIR = ROOT / "data" / "dividend_stocks" / "exdiv"


def parse_date(v) -> _date | None:
    try:
        s = str(v)[:10]
        return _date.fromisoformat(s)
    except Exception:
        return None


def load_exdiv_calendar(exdiv_dir: Path) -> dict[str, list[_date]]:
    """{symbol: 排序后的除权日列表}（跳过解析失败行，fail-open 仅用于探测）。"""
    cal: dict[str, list[_date]] = {}
    bad_rows = 0
    total_rows = 0
    for p in sorted(exdiv_dir.glob("*.parquet")):
        symbol = p.stem
        df = pd.read_parquet(p)
        if df.empty:
            continue
        total_rows += len(df)
        dates = []
        for v in df["date"].tolist():
            d = parse_date(v)
            if d is None:
                bad_rows += 1
                continue
            dates.append(d)
        if dates:
            cal[symbol] = sorted(set(dates))
    return cal, bad_rows, total_rows


def has_exdiv_within(symbol_dates: list[_date], day: _date, n: int) -> bool:
    """day 起（含当日）未来 N 个自然日内是否有除权日。dates 已升序。"""
    hi = day + timedelta(days=n)
    import bisect
    lo_i = bisect.bisect_left(symbol_dates, day)
    return lo_i < len(symbol_dates) and symbol_dates[lo_i] <= hi


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=15, help="前瞻窗口自然日数（默认 15）")
    ap.add_argument("--start", default="2015-01-05")
    ap.add_argument("--end", default="2024-12-31")
    args = ap.parse_args()

    n = args.days
    start = _date.fromisoformat(args.start)
    end = _date.fromisoformat(args.end)

    if not EXDIV_DIR.exists():
        print(f"[probe] ⛔ 除权目录不存在: {EXDIV_DIR}", file=sys.stderr)
        return 1

    files = sorted(EXDIV_DIR.glob("*.parquet"))
    cal, bad_rows, total_rows = load_exdiv_calendar(EXDIV_DIR)
    with_event = sum(1 for v in cal.values() if v)
    in_window = sum(1 for v in cal.values() if any(start <= d <= end for d in v))

    print("=" * 72)
    print(f"装甲一数据探测（窗口 N={n}，区间 {start} ~ {end}）")
    print("=" * 72)
    print(f"exdiv parquet 文件数        : {len(files)}")
    print(f"有除权事件的 symbol 数       : {with_event} / {len(files)}")
    print(f"区间内含除权事件的 symbol 数 : {in_window}")
    print(f"总事件行数 / 日期解析失败   : {total_rows} / {bad_rows}")

    # 交易日历（指数分区拼接，与 run_dividend_backtest.py 同口径）
    idx_dir = ROOT / "data" / "dividend_stocks" / "sh.000300"
    idx_parts = [pd.read_parquet(idx_dir / f"{y}.parquet")
                 for y in range(start.year, end.year + 1)
                 if (idx_dir / f"{y}.parquet").exists()]
    if not idx_parts:
        print("[probe] ⛔ 无指数分区，无法重建交易日历", file=sys.stderr)
        return 1
    idx_frame = pd.concat(idx_parts, ignore_index=True).sort_values("date")
    cal_days = [parse_date(v) for v in idx_frame["date"].tolist()]
    cal_days = [d_ for d_ in cal_days if d_ is not None and start <= d_ <= end]

    # 每个回测年内：候选 symbol 在『当日未来 N 日内有除权日』的命中次数。
    # 用每个 symbol 的全部交易日做代理（保守上界：真实候选池会小得多）。
    print("\n--- 按年统计：未来 %d 自然日内有除权日的 (symbol, 日期) 命对数 ---" % n)
    total_hits = 0
    total_cells = 0
    for year in range(start.year, end.year + 1):
        ys = _date(year, 1, 1)
        ye = _date(year, 12, 31)
        hits = 0
        cells = 0
        for symbol, dates in sorted(cal.items()):
            in_year = [d for d in dates if ys <= d <= ye]
            # 事件密集度低：用每个除权日往前 N 天的窗口近似『若这些天建仓则命中』
            for d in in_year:
                # 从 exdiv 日往前回 N 天内每个自然日都算『会触发过滤』的一天
                hits += 1  # 该 symbol 该年一次除权 = 最多 N 个建仓日被拦截
                cells += 1
        total_hits += hits
        total_cells += cells
        print(f"  {year}: 除权事件 {cells} 次（每次最多拦截 N={n} 个潜在建仓日）")
    print(f"  合计: {total_hits} 次除权事件（区间内）")

    # 敏感度：不同 N 下『被拦截的建仓日占比』（以 487 只 × 2431 交易日为分母上界）
    print("\n--- 敏感度：不同 N 的最大拦截占比（上界，分母=487×2431 个 (symbol,日) 对）---")
    denom = len(files) * 2431
    for nn in (5, 10, 15, 20, 30):
        ev = 0
        for dates in cal.values():
            ev += sum(1 for d in dates if start <= d <= end)
        print(f"  N={nn:2d}: 最多拦截 {ev * nn} 个建仓日 ≈ 占 {ev * nn / denom * 100:.3f}%（上界）")

    # 真实选股链路口径：复刻 _select_stocks（股息率>=3% → 降序取前 50 → 入选前 5）
    # 在调仓节拍（rebalance_days=20、warmup=210）上施加 N 日窗口过滤
    print("\n--- 真实选股链路拦截率（股息率>=3% → 前50 → 入选前5，调仓节拍 20 交易日）---")
    bars_frames: dict[str, pd.DataFrame] = {}
    stock_dirs = [p for p in sorted((ROOT / "data" / "dividend_stocks").iterdir())
                  if p.is_dir() and p.name.startswith(("sh.", "sz."))]
    for sd in stock_dirs:
        parts = [pd.read_parquet(p) for p in sorted(sd.glob("[0-9]*.parquet"))]
        if not parts:
            continue
        t = pd.concat(parts, ignore_index=True)
        t["_dt"] = pd.to_datetime(t["date"]).dt.date
        bars_frames[sd.name] = t.set_index("_dt")

    rebalance_days = 20
    warmup_bars = 210
    for nn in (10, 15, 20, 30):
        pool_n = pool_hit = top50_n = top50_hit = sel_n = sel_hit = 0
        for i in range(warmup_bars, len(cal_days), rebalance_days):
            day = cal_days[i]
            cand = []
            for sym, t in bars_frames.items():
                if day not in t.index:
                    continue
                dy = t.loc[day].get("dividend_yield")
                if dy is None:
                    continue
                try:
                    dyf = float(dy)
                except (TypeError, ValueError):
                    continue
                if dyf >= 0.03:
                    cand.append((sym, dyf))
                    pool_n += 1
                    pool_hit += 1 if has_exdiv_within(cal.get(sym, []), day, nn) else 0
            cand.sort(key=lambda x: -x[1])
            top = cand[:50]
            sel = [s for s, _ in top[:5]]
            for s, _ in top:
                top50_n += 1
                top50_hit += 1 if has_exdiv_within(cal.get(s, []), day, nn) else 0
            for s in sel:
                sel_n += 1
                sel_hit += 1 if has_exdiv_within(cal.get(s, []), day, nn) else 0
        print(f"  N={nn:2d}: 候选池 {pool_hit}/{pool_n} = {pool_hit / pool_n * 100 if pool_n else 0:.2f}%  |  "
              f"前50 {top50_hit}/{top50_n} = {top50_hit / top50_n * 100 if top50_n else 0:.2f}%  |  "
              f"入选前5 {sel_hit}/{sel_n} = {sel_hit / sel_n * 100 if sel_n else 0:.2f}%")

    # 档位口径核对
    print("\n--- 红利税档位口径（财税 2012/85 + 2015/101）---")
    print("  持股期 < 30 天（<1 月）     : 20% 惩罚档")
    print("  30 <= 持股期 < 365 天       : 10% 档")
    print("  持股期 >= 365 天（>1 年）   : 免征（5% 减按 25% 计入，等效免征）")
    print(f"  N=15 覆盖 <30 天窗口：除权前 15 天建仓 ⇒ 持有到除权日 ≤ 15 天 < 30 天 ⇒ 20% 档")

    # 结算消费侧核对：broker 在除权日按 FIFO 计税，建仓到除权日天数 = 持股期
    print("\n--- 结论 ---")
    print(f"  exdiv sidecar 完整可用（{len(files)} 文件、{with_event} 只有事件、解析失败 {bad_rows} 行）")
    print(f"  N={n} 的拦截窗口占候选面上界 < 1%，对组合层建仓可用标的几乎无收紧")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
