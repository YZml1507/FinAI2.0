"""e31 候选库信号交叉相关诊断（描述性统计，非假设筛选）。

目的：候选库 S2(consec_down)/S5(holders_z4)/M5(short_chg20)/F6(roe_std8)/
H2(rev_1m) 五信号的截面秩相关矩阵 + top 五分位重合度（Jaccard）。
用途：范畴评估/M5 重启条件②交叉验证底数——若两候选秩相关 |ρ|>0.5
则实质同一信号，合成加权无意义。

口径：月末信号日；宇宙=上市≥120 日有效股（同 e23-e28）；所有信号归一
为「越大越好」方向后算 Spearman；M5 用 margin 次日披露口径（last margin
day < T）。margin_detail 未采全时 M5 臂自动跳过并如实标注。

用法：.venv/bin/python -m scripts.lab.e31_candidate_crosscorr [--limit-days N]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.lab import e23_shadow_screen as e23  # noqa: E402
from scripts.lab import e25_factor_screen as e25  # noqa: E402
from scripts.lab import e26_margin_screen as e26  # noqa: E402
from scripts.lab import e27_insider_screen as e27  # noqa: E402
from scripts.lab import e28_gdhs_screen as e28  # noqa: E402

OUT_DIR = ROOT / 'experiments' / 'lab' / 'e31'
MARGIN_DIR = ROOT / 'data' / 'margin_detail'
MARGIN_MIN_DAYS = 2300  # 全窗 ~2431 日；低于此 M5 跳过

# (name, 越大越好归一化 flip)
CANDS = ['S2', 'S5', 'M5', 'F6', 'H2']


def _note(m: str) -> None:
    print(f"[note] {m}")


def build_signals(T: pd.Timestamp, ctx: dict) -> dict[str, pd.Series]:
    """各候选信号在信号日 T 的截面（越大越好口径已归一）。缺失→None。"""
    out: dict[str, pd.Series] = {}
    # S2/S5：gdhs 可见特征（低好→取负）
    snap = ctx['gdhs_vis'].get(T)
    if snap is not None and len(snap):
        snap = snap.copy()
        snap.index = snap.index.map(lambda c6: ctx['six2ts'].get(c6, c6))
        out['S2'] = snap['consec_down'].astype(float)   # e28: 高好(连降2档最强)
        out['S5'] = -snap['holders_z4'].astype(float)   # e28: 低好
    # F6：roe_std8（低好→取负）
    pit_snap = ctx['pit'].get(T)
    if pit_snap is not None:
        out['F6'] = -pit_snap['roe_std8']
    # H2：rev_1m = 21 日 log 收益（低好→取负）
    out['H2'] = -ctx['h2'].loc[T]
    # M5：short_chg20（低好→取负；margin 日 < T）
    if ctx['m5'] is not None:
        md = ctx['margin_dates']
        j = int(md.searchsorted(T, side='left')) - 1
        if j >= 0:
            out['M5'] = -ctx['m5'].loc[md[j]]
    return out


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else np.nan


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit-days', type=int, default=None)
    args = ap.parse_args()
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    suf = f"_lim{args.limit_days}" if args.limit_days else ""
    close_w = pd.read_parquet(e27.OUT_DIR / f'panel_close{suf}.parquet')
    r = e23.daily_returns(close_w)
    valid = (~close_w.isna()).cumsum().ge(e23.MIN_LISTED_DAYS)
    Ts = e23.month_ends(r.index)
    _note(f"panel {close_w.shape} month_ends={len(Ts)}")

    # gdhs 可见特征（e28 冻结过滤器口径）
    long = e28.load_gdhs_records()
    gdhs_vis = e28.visible_signal(long, list(Ts))
    _note(f"gdhs events={len(long)}")

    # F6 roe_std8（e25 PIT 财报快照）
    pit_cache = e25.load_pit()
    pit = {T: e25.pit_snapshot(T, pit_cache) for T in Ts}

    # H2 反转
    h2 = np.log1p(r.fillna(0.0)).rolling(e23.SKIP).sum()

    # M5 margin（次日披露口径）
    m5 = None
    margin_dates = None
    n_margin = len(list(MARGIN_DIR.glob('*.parquet')))
    if n_margin >= MARGIN_MIN_DAYS:
        P26 = e26.load_panels(args.limit_days)
        mframes = e26.margin_signal_frames(P26)
        m5 = mframes['short_chg20']
        margin_dates = P26['fin_balance'].index
        _note(f"margin_days={len(margin_dates)}")
    else:
        _note(f"margin_detail 仅 {n_margin} 日 <{MARGIN_MIN_DAYS}，M5 臂跳过")

    ctx = {'gdhs_vis': gdhs_vis, 'pit': pit, 'h2': h2, 'm5': m5,
           'margin_dates': margin_dates,
           'six2ts': {t.split('.')[0]: t for t in r.columns}}

    # 逐月相关矩阵 + top-quintile 重合
    pairs = [(a, b) for i, a in enumerate(CANDS) for b in CANDS[i + 1:]]
    corr_ts: dict[tuple, list] = {p: [] for p in pairs}
    jac_ts: dict[tuple, list] = {p: [] for p in pairs}
    months_used = []
    for T in Ts:
        sigs = build_signals(T, ctx)
        base_ok = valid.loc[T]
        months_used.append(str(T.date()))
        for a, b in pairs:
            sa, sb = sigs.get(a), sigs.get(b)
            if sa is None or sb is None:
                continue
            ok = base_ok & sa.notna() & sb.notna()
            n = int(ok.sum())
            if n < 200:
                continue
            rho = sa[ok].rank().corr(sb[ok].rank())  # Spearman=秩皮尔逊
            corr_ts[(a, b)].append((str(T.date()), float(rho), n))
            qa = set(sa[ok].nlargest(max(1, n // 5)).index)
            qb = set(sb[ok].nlargest(max(1, n // 5)).index)
            jac_ts[(a, b)].append((str(T.date()), jaccard(qa, qb)))

    summary = {}
    for p in pairs:
        cs = [c for _, c, _ in corr_ts[p]]
        js = [j for _, j in jac_ts[p]]
        summary[f"{p[0]}×{p[1]}"] = {
            'months': len(cs),
            'rho_mean': float(np.mean(cs)) if cs else None,
            'rho_std': float(np.std(cs)) if cs else None,
            'jaccard_mean': float(np.mean(js)) if js else None,
            'dup_flag': bool(cs and abs(np.mean(cs)) > 0.5),
        }
        _note(f"{p[0]}×{p[1]}: rho={summary[f'{p[0]}×{p[1]}']['rho_mean']} "
              f"±{summary[f'{p[0]}×{p[1]}']['rho_std']} "
              f"jaccard={summary[f'{p[0]}×{p[1]}']['jaccard_mean']}")

    out = {'meta': {'months': len(months_used), 'margin_skipped': m5 is None,
                    'elapsed_s': time.time() - t0},
           'summary': summary,
           'corr_ts': {f"{a}×{b}": v for (a, b), v in corr_ts.items()},
           'jac_ts': {f"{a}×{b}": v for (a, b), v in jac_ts.items()}}
    (OUT_DIR / 'e31_results.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=1))
    _note(f"done {time.time()-t0:.0f}s -> {OUT_DIR/'e31_results.json'}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
