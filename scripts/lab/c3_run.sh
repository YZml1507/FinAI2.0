#!/usr/bin/env bash
# C3 池重选实验：P1 年度池 × e8b 冠军同参（docs/C3_POOL_RESELECT_PREREG.md）。
# 变量只有「池」——数据面 data/c3_universe + 年度池 provider；
# 对照锚 = isst-e8b（同 SHA 同 isST 数据，旧 487 池）。
# ⛔ 前置：c3_pool_rebuild.py 已产出 data/c3_pool/pool_yearly.parquet
#          c3_data_plane.py 已物化 data/c3_universe。
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
RUNNER=scripts/lab/run_experiment.py
LOGDIR=experiments/lab/_logs
mkdir -p "$LOGDIR"

# e8b 冠军同参（宽度择时 + demote + GC001），⛔ 不动策略参数
E8B="use_breadth_timing=True breadth_defense_threshold=0.25 \
breadth_attack_threshold=0.35 breadth_mid_cap=0.0 \
breadth_ice_confirm_days=1 breadth_demote_liquidate=True \
use_ma200_timing=False cash_yield_series=data/rates/gc001_daily.parquet"

# C3 数据面 + 年度池 provider
C3="universe_yearly_pool=data/c3_pool/pool_yearly.parquet"

log="$LOGDIR/c3-p1-yearly.log"
echo "[c3] $(date '+%F %T') 发车 -> $log"
taskset -c 0 $PY "$RUNNER" --name c3-p1-yearly \
  $(for kv in $E8B $C3; do printf -- '--set %s ' "$kv"; done) \
  --data-path data/c3_universe \
  > "$log" 2>&1
echo "[c3] $(date '+%F %T') 收车"
