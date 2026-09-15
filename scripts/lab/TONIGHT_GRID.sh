#!/usr/bin/env bash
# 2026-09-14 夜间择时参数扩展网格（在 tmux finai-grid 会话内运行）
# 围绕当前最优基线（buffer=1%, breach=2, rebuild=1，MDD 39.33%）做局部探测。
# 3 路并行 × 3 批串行 = 9 组，单回测约 26 分钟，全程约 90~100 分钟。
# 产物隔离于 experiments/lab/<实验名>/，结果自动进 leaderboard.jsonl → 监工自动汇报。
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
RUNNER=scripts/lab/run_experiment.py
LOGDIR=experiments/lab/_logs
mkdir -p "$LOGDIR"

run_batch() {
  # 每批 3 组，分别绑核 0/1/2
  local -a names=("$1" "$2" "$3")
  local -a cores=(0 1 2)
  local -a params=("$4" "$5" "$6")
  local pids=()
  for i in 0 1 2; do
    local name="${names[$i]}"
    local core="${cores[$i]}"
    local pa="${params[$i]}"
    [ -z "$name" ] && continue
    echo "[$(date '+%T')] batch 启动 $name -> 核 $core | $pa" >> "$LOGDIR/grid_tonight.log"
    # shellcheck disable=SC2086
    taskset -c "$core" $PY "$RUNNER" --name "$name" \
      $(for kv in $pa; do printf -- '--set %s ' "$kv"; done) \
      > "$LOGDIR/${name}.log" 2>&1 &
    pids+=("$!")
  done
  for p in "${pids[@]}"; do wait "$p"; done
}

echo "[$(date '+%F %T')] 夜间择时网格点火（9 组 / 3 批）" >> "$LOGDIR/grid_tonight.log"

# 批次 1：缓冲带维度（breach=2 rebuild=1 固定，探 0.5%/1.5%/2.5%）
run_batch \
  "tn-buf005-b2r1" "tn-buf015-b2r1" "tn-buf025-b2r1" \
  "timing_breach_buffer=0.005 timing_breach_confirm_days=2 timing_rebuild_confirm_days=1" \
  "timing_breach_buffer=0.015 timing_breach_confirm_days=2 timing_rebuild_confirm_days=1" \
  "timing_breach_buffer=0.025 timing_breach_confirm_days=2 timing_rebuild_confirm_days=1"
echo "[$(date '+%T')] 批次 1 完成" >> "$LOGDIR/grid_tonight.log"

# 批次 2：破位确认维度（buffer=2% rebuild=1 固定，探 1/2/3 天）
run_batch \
  "tn-buf020-b1r1" "tn-buf020-b2r1" "tn-buf020-b3r1" \
  "timing_breach_buffer=0.02 timing_breach_confirm_days=1 timing_rebuild_confirm_days=1" \
  "timing_breach_buffer=0.02 timing_breach_confirm_days=2 timing_rebuild_confirm_days=1" \
  "timing_breach_buffer=0.02 timing_breach_confirm_days=3 timing_rebuild_confirm_days=1"
echo "[$(date '+%T')] 批次 2 完成" >> "$LOGDIR/grid_tonight.log"

# 批次 3：重建确认维度（buffer=2% breach=2 固定，探 1/2/3 天）
run_batch \
  "tn-buf020-b2r1b" "tn-buf020-b2r2" "tn-buf020-b2r3" \
  "timing_breach_buffer=0.02 timing_breach_confirm_days=2 timing_rebuild_confirm_days=1" \
  "timing_breach_buffer=0.02 timing_breach_confirm_days=2 timing_rebuild_confirm_days=2" \
  "timing_breach_buffer=0.02 timing_breach_confirm_days=2 timing_rebuild_confirm_days=3"
echo "[$(date '+%F %T')] 夜间择时网格全部完成（9 组）" >> "$LOGDIR/grid_tonight.log"

# 收尾：飞书发一条总汇报
sudo hermes send --to 'feishu:oc_79018399aacb85fb92010c02278c6224' \
  "[FinAI2.0 夜间网格] 9 组择时参数实验全部完成，榜单见 experiments/lab/leaderboard.jsonl。逐条结果已由监工分别推送。" \
  >/dev/null 2>&1 || true
echo "[$(date '+%T')] 总汇报已发" >> "$LOGDIR/grid_tonight.log"
