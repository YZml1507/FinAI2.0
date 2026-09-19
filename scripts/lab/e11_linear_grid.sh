#!/usr/bin/env bash
# e11-linear 并行发车器：主实验 + ±20% 扰动矩阵（15 格），4 核绑核 3 路并行。
# 铁律对齐 parallel_grid.sh：产物隔离 experiments/lab/<name>/，日志 _logs/。
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
RUNNER=scripts/lab/run_experiment.py
LOGDIR=experiments/lab/_logs
mkdir -p "$LOGDIR"

BASE="use_breadth_timing=True breadth_mid_cap=0.0 breadth_ice_confirm_days=1 breadth_demote_liquidate=True breadth_weight_mode=linear cash_yield_series=data/rates/gc001_daily.parquet"

# 名称|参数追加（defense/attack 逐格覆盖）
read -r -d '' GRID <<EOF || true
e11-linear|breadth_defense_threshold=0.25 breadth_attack_threshold=0.35
e11-linear-pg-a28d20|breadth_defense_threshold=0.20 breadth_attack_threshold=0.28
e11-linear-pg-a31d20|breadth_defense_threshold=0.20 breadth_attack_threshold=0.31
e11-linear-pg-a35d20|breadth_defense_threshold=0.20 breadth_attack_threshold=0.35
e11-linear-pg-a385d20|breadth_defense_threshold=0.20 breadth_attack_threshold=0.385
e11-linear-pg-a42d20|breadth_defense_threshold=0.20 breadth_attack_threshold=0.42
e11-linear-pg-a31d225|breadth_defense_threshold=0.225 breadth_attack_threshold=0.31
e11-linear-pg-a35d225|breadth_defense_threshold=0.225 breadth_attack_threshold=0.35
e11-linear-pg-a385d225|breadth_defense_threshold=0.225 breadth_attack_threshold=0.385
e11-linear-pg-a42d225|breadth_defense_threshold=0.225 breadth_attack_threshold=0.42
e11-linear-pg-a385d25|breadth_defense_threshold=0.25 breadth_attack_threshold=0.385
e11-linear-pg-a42d25|breadth_defense_threshold=0.25 breadth_attack_threshold=0.42
e11-linear-pg-a385d275|breadth_defense_threshold=0.275 breadth_attack_threshold=0.385
e11-linear-pg-a42d275|breadth_defense_threshold=0.275 breadth_attack_threshold=0.42
e11-linear-pg-a42d30|breadth_defense_threshold=0.30 breadth_attack_threshold=0.42
e11-linear-pg-a385d30|breadth_defense_threshold=0.30 breadth_attack_threshold=0.385
EOF

echo "[e11] $(date '+%F %T') 启动 e11-linear 16 组（3 路并行）"
pids=()
slot=0
while IFS='|' read -r name extra; do
  [ -z "${name:-}" ] && continue
  core=$((slot % 3))
  log="$LOGDIR/${name}.log"
  echo "[e11] $name -> 核 $core"
  taskset -c "$core" $PY "$RUNNER" --name "$name" \
    $(for kv in $BASE $extra; do printf -- '--set %s ' "$kv"; done) \
    > "$log" 2>&1 &
  pids+=("$!")
  slot=$((slot + 1))
  if [ $((slot % 3)) -eq 0 ]; then
    for p in "${pids[@]: -3}"; do wait "$p"; done
    echo "[e11] $(date '+%F %T') 批次完成（已发 $slot/16）"
  fi
done <<< "$GRID"
for p in "${pids[@]}"; do wait "$p" 2>/dev/null || true; done
echo "[e11] $(date '+%F %T') 全部结束。榜单: experiments/lab/leaderboard.jsonl"