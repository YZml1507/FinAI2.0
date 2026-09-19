#!/usr/bin/env bash
# E12-isST 重建后整批同 SHA 重跑（G-REPRO-1 / E12 §四）：
# 基线双锚 + 现役对照共 5 组，4 核绑核 3 路并行。
# ⛔ 前置：rebuild_isst_from_namechange.py 已实际回写 + e12_isst_probe A1~A5 全过。
# 产物隔离 experiments/lab/isst-*/，日志 _logs/。
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
RUNNER=scripts/lab/run_experiment.py
LOGDIR=experiments/lab/_logs
mkdir -p "$LOGDIR"

# 公共：宽度择时 + ice_confirm=1（与既有基线同口径）
BASE="use_breadth_timing=True breadth_mid_cap=0.0 breadth_ice_confirm_days=1 use_ma200_timing=False"
GC001="cash_yield_series=data/rates/gc001_daily.parquet"
DEMOTE="breadth_demote_liquidate=True"

# 名称|参数追加（阈值/开关逐组覆盖）
read -r -d '' RUNS <<EOF || true
isst-baseline|breadth_defense_threshold=0.25 breadth_attack_threshold=0.45
isst-champion|breadth_defense_threshold=0.25 breadth_attack_threshold=0.35
isst-e8b|breadth_defense_threshold=0.25 breadth_attack_threshold=0.35 $DEMOTE $GC001
isst-e11-linear|breadth_defense_threshold=0.25 breadth_attack_threshold=0.35 $DEMOTE $GC001 breadth_weight_mode=linear
isst-e13a|breadth_defense_threshold=0.25 breadth_attack_threshold=0.35 $DEMOTE $GC001 breadth_weight_mode=hard breadth_mid_cap=0.5
EOF

echo "[isst] $(date '+%F %T') 启动整批重跑 5 组（3 路并行）"
pids=()
slot=0
while IFS='|' read -r name extra; do
  [ -z "${name:-}" ] && continue
  core=$((slot % 3))
  log="$LOGDIR/${name}.log"
  echo "[isst] $name -> 核 $core"
  taskset -c "$core" $PY "$RUNNER" --name "$name" \
    $(for kv in $BASE $extra; do printf -- '--set %s ' "$kv"; done) \
    > "$log" 2>&1 &
  pids+=("$!")
  slot=$((slot + 1))
  if [ $((slot % 3)) -eq 0 ]; then
    for p in "${pids[@]: -3}"; do wait "$p"; done
    echo "[isst] $(date '+%F %T') 批次完成（已发 $slot/5）"
  fi
done <<< "$RUNS"
for p in "${pids[@]}"; do wait "$p" 2>/dev/null || true; done
echo "[isst] $(date '+%F %T') 全部结束。榜单: experiments/lab/leaderboard.jsonl"
