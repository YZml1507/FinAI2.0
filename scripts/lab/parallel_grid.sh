#!/usr/bin/env bash
# T312 择时参数网格 · 并行实验启动器（4 核 8G，taskset 绑核，产物隔离）
#
# 铁律：
#   * 每个实验绑定独立 CPU 核（taskset -c），单回测约 1 核 26 分钟；
#   * 产物/日志全部落在 experiments/lab/<实验名>/，互不覆盖、不碰权威目录；
#   * 同时并行数建议 2~3（留 1~2 核给系统与 IO），脚本默认 3 路并行。
#
# 用法：
#   bash scripts/lab/parallel_grid.sh            # 启动网格（后台）
#   bash scripts/lab/status.sh                   # 查看进度（可选）
set -u
cd "$(dirname "$0")/../.."   # 仓根

PY=.venv/bin/python
RUNNER=scripts/lab/run_experiment.py
LOGDIR=experiments/lab/_logs
mkdir -p "$LOGDIR"

# ---- 实验网格定义：名称|核|参数覆盖（空格分隔多组 k=v） ----
# 围绕当前最优点（buffer=1%, breach=2, rebuild=1）做局部网格探测。
read -r -d '' GRID <<'EOF' || true
buf005-b2-r1|0|timing_breach_buffer=0.005 timing_breach_confirm_days=2 timing_rebuild_confirm_days=1
buf020-b2-r1|1|timing_breach_buffer=0.02 timing_breach_confirm_days=2 timing_rebuild_confirm_days=1
buf010-b3-r1|2|timing_breach_buffer=0.01 timing_breach_confirm_days=3 timing_rebuild_confirm_days=1
EOF

echo "[grid] $(date '+%F %T') 启动并行实验网格（产物隔离于 experiments/lab/）"
pids=()
while IFS='|' read -r name core params; do
  [ -z "${name:-}" ] && continue
  log="$LOGDIR/${name}.log"
  echo "[grid] 实验 $name -> 核 $core | 参数: $params"
  taskset -c "$core" $PY "$RUNNER" --name "$name" \
    $(for kv in $params; do printf -- '--set %s ' "$kv"; done) \
    > "$log" 2>&1 &
  pids+=("$!")
  echo "[grid]   pid=$! log=$log"
done <<< "$GRID"

echo "[grid] 已启动 ${#pids[@]} 路并行实验，等待全部完成..."
fail=0
for p in "${pids[@]}"; do
  if ! wait "$p"; then fail=$((fail+1)); fi
done
echo "[grid] $(date '+%F %T') 全部结束，失败 $fail 路。榜单: experiments/lab/leaderboard.jsonl"
