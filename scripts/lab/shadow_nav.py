"""scripts/lab/shadow_nav.py —— 实盘对照追踪器（shadow NAV）。

把 experiments/live/basket_<date>_top<N>.csv 每个已发射清单按
每日收盘推进假想净值：NAV(t) = Σ shares_i·close_i(t) + cash_left，
归一化到清单日=1.0。用途：积累真 OOS 对照证据（发射后每日记分，
不经任何回测口径修饰），回答"清单口径离回测多远"。

输出 ``experiments/live/shadow_nav.parquet``：
  basket_id, date, nav, ret, priced_n, n
幂等：全量重写（basket 文件数小，重写成本可忽略）。
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "experiments" / "live"
BARS = ROOT / "data" / "daily_bars"
OUT = LIVE / "shadow_nav.parquet"


def _close_series(sym: str) -> pd.Series:
    """该股 RAW close 全日序列（多年分片拼接）。"""
    frames = []
    for f in sorted((BARS / sym).glob("*.parquet")):
        d = pd.read_parquet(f, columns=["date", "close"])
        frames.append(d)
    if not frames:
        return pd.Series(dtype=float)
    s = pd.concat(frames).drop_duplicates("date").set_index(
        pd.to_datetime(pd.concat(frames)["date"]))["close"]
    return s.sort_index()


def _basket_nav(csv_path: Path) -> pd.DataFrame | None:
    rows = pd.read_csv(csv_path)
    meta_p = csv_path.with_name(csv_path.stem + "_meta.json")
    cash_left = 0.0
    if meta_p.exists():
        cash_left = float(json.loads(meta_p.read_text()).get("cash_left", 0))
    entry = pd.Timestamp(csv_path.stem.split("_")[1])
    px = {}
    for r in rows.itertuples(index=False):
        s = _close_series(str(r.symbol))
        if s.empty:
            continue
        px[r.symbol] = (s, float(r.shares))
    if not px:
        return None
    days = sorted({d for s, _ in px.values() for d in s.index})
    days = [d for d in days if d >= entry]
    if not days:
        return None
    out_rows = []
    for t in days:
        nav = cash_left
        n_priced = 0
        for sym, (s, shares) in px.items():
            upto = s[s.index <= t]
            if len(upto):
                nav += shares * float(upto.iloc[-1])
                n_priced += 1
        out_rows.append((csv_path.stem, t.date().isoformat(),
                         nav, n_priced, len(px)))
    df = pd.DataFrame(out_rows, columns=["basket_id", "date", "nav",
                                         "priced_n", "n"])
    base = df["nav"].iloc[0]
    df["ret"] = df["nav"] / base - 1
    return df


def main() -> int:
    frames = []
    for f in sorted(glob.glob(str(LIVE / "basket_*.csv"))):
        df = _basket_nav(Path(f))
        if df is not None:
            frames.append(df)
    if not frames:
        print("no baskets with prices yet")
        return 0
    nav = pd.concat(frames, ignore_index=True)
    nav.to_parquet(OUT, index=False)
    for bid, g in nav.groupby("basket_id"):
        last = g.iloc[-1]
        print(f"{bid}: {last['date']} nav={last['nav']:.0f} "
              f"ret={last['ret']:+.2%} ({len(g)}d priced {last['priced_n']}/{last['n']})")
    print(f"-> {OUT} ({len(nav)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
