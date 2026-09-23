"""scripts/daily_ops.py —— DEPLOY_LIVE 三步走的一键编排（日频运维管线）。

把「产分数 → 产清单」的手工链固化成可定时执行的单一入口：

  1. bars    增量续采（extend_bars_2025 --end 今天；幂等水位）
  2. lhb     龙虎榜日增量（pull_lhb_daily；veto V2 与日历延伸的新鲜度源）
  3. veto    否决序列延伸（e37_veto_series --run --extend-calendar
             --min-date 2025-01-01 --out veto_daily_2025plus.parquet）
  4. features 2025 特征矩阵重建（e63_build_2025 → e63_Xlab_2025.parquet）
  5. score   2025 分数重算（e63_score_2025 → scores_label150_2025.parquet）
  6. emit    实盘篮（emit_live_basket --veto-path ×2）
  7. nav     shadow NAV 实盘对照（shadow_nav → experiments/live/shadow_nav.parquet）

纪律：fail-closed——任一步非零退出即停，宁可不发清单不发半新鲜数据；
每步日志落 ``experiments/live/ops_log/<stamp>_<step>.log`` 可回查。

用法：
  python -m scripts.daily_ops                    # 全链
  python -m scripts.daily_ops --steps veto,emit  # 子集
  # 注：veto 依赖 lhb 最新分片；跨日跑 veto 前先跑 lhb
  python -m scripts.daily_ops --topn 20 --capital 150000
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
LAB = ROOT / "scripts" / "lab"
LOG_DIR = ROOT / "experiments" / "live" / "ops_log"

VETO_HIST = ROOT / "data" / "e37_veto" / "veto_daily.parquet"
VETO_25 = ROOT / "data" / "e37_veto" / "veto_daily_2025plus.parquet"
SCORES_25 = ROOT / "experiments" / "lab" / "e63" / "scores_label150_2025.parquet"

STEPS = ("bars", "lhb", "veto", "features", "score", "emit", "nav")


def _commands(today: str, topn: int, capital: int) -> dict[str, list[str]]:
    return {
        "bars": [PY, str(LAB / "extend_bars_2025.py"), "--end", today],
        "lhb": [PY, str(LAB / "pull_lhb_daily.py"), "--end",
                today.replace("-", "")],
        "veto": [PY, str(LAB / "e37_veto_series.py"), "--run",
                 "--extend-calendar", "--min-date", "2025-01-01",
                 "--out", str(VETO_25)],
        "features": [PY, str(LAB / "e63_build_2025.py")],
        "score": [PY, str(LAB / "e63_score_2025.py")],
        "emit": [PY, str(ROOT / "scripts" / "emit_live_basket.py"),
                 "--scores", str(SCORES_25),
                 "--topn", str(topn), "--capital", str(capital),
                 "--veto-path", str(VETO_HIST),
                 "--veto-path", str(VETO_25)],
        "nav": [PY, str(LAB / "shadow_nav.py")],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default=",".join(STEPS),
                    help="逗号子集（默认全链 bars,veto,features,score,emit）")
    ap.add_argument("--topn", type=int, default=20)
    ap.add_argument("--capital", type=int, default=150000)
    ap.add_argument("--date", default=date.today().isoformat(),
                    help="bars 续采的目标端日（默认今天）")
    args = ap.parse_args()

    want = [s.strip() for s in args.steps.split(",") if s.strip()]
    bad = [s for s in want if s not in STEPS]
    if bad:
        print(f"unknown steps: {bad}（可选 {STEPS}）")
        return 2

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    cmds = _commands(args.date, args.topn, args.capital)
    t0 = time.time()
    for step in STEPS:
        if step not in want:
            continue
        log_f = LOG_DIR / f"{stamp}_{step}.log"
        print(f"[daily_ops] {step} → {log_f.name}", flush=True)
        with log_f.open("w") as fh:
            p = subprocess.run(cmds[step], cwd=ROOT,
                               stdout=fh, stderr=subprocess.STDOUT)
        if p.returncode != 0:
            tail = log_f.read_text().splitlines()[-15:]
            print(f"[daily_ops] {step} FAILED rc={p.returncode}\n"
                  + "\n".join(tail))
            return p.returncode
    print(f"[daily_ops] all steps ok ({time.time()-t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
