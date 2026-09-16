#!/usr/bin/env bash
# FinAI2.0 十二小时持续运行守护：主线自动重试 + 回测间隙填满，跑满整晚算力。
# 用法：由 start_12h.sh 投递到 tmux 会话内执行；窗口总长 12 小时，到点自动收尾。
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
LOGDIR=experiments/lab/_logs
PLOG="$LOGDIR/pipeline_12h.log"
DLOG="$LOGDIR/daemon_12h.log"
WINDOW_SECONDS=$((12 * 3600))
MAINLINE_RETRY_INTERVAL=$((30 * 60))   # 主线失败后再试的间隔
MAINLINE_SUCCESS_INTERVAL=$((60 * 60)) # 主线成功后下一轮完整流水线的间隔
POOL_SESSION_PREFIX='finai-pool-'
RUNNER=scripts/lab/run_experiment.py
BS=experiments/lab/market-breadth-a/breadth20_daily.parquet
MAX_PARALLEL=2                 # 并行回测路上限（8G 内存机器，单回测峰值约 500MB+，2 路留足余量）
BACKTEST_MEM_KB=$((3 * 1024 * 1024))   # 单回测进程虚拟内存上限 3GB，超限即被内核终止防爆仓
mkdir -p "$LOGDIR"

ts() { date '+%F %T'; }
log() { echo "$(ts) $1" | tee -a "$DLOG"; }

DEADLINE=$(( $(date +%s) + WINDOW_SECONDS ))
log "守护启动：连续运行 12 小时（至 $(date -d "@$DEADLINE" '+%F %T')），主线自动重试 + 回测间隙填满模式"

GRID_ROUND=0
grid_running() { tmux ls 2>/dev/null | grep -q "^$POOL_SESSION_PREFIX"; }
grid_running_count() { tmux ls 2>/dev/null | grep -c "^$POOL_SESSION_PREFIX" || true; }

launch_grid_batch() {
  # 在最优点邻域递进扩展参数面，最多 MAX_PARALLEL 路并行 + 单进程内存上限，防打满卡死
  GRID_ROUND=$((GRID_ROUND + 1))
  local d a idx=0
  local ds=(0.23 0.24 0.26 0.27)
  local as=(0.43 0.44 0.46 0.47)
  while [ "$(grid_running_count)" -lt "$MAX_PARALLEL" ] && [ "$idx" -lt "$MAX_PARALLEL" ]; do
    d=${ds[$(( (GRID_ROUND + idx) % 4 ))]}
    a=${as[$(( (GRID_ROUND * 2 + idx) % 4 ))]}
    local n="autoR${GRID_ROUND}D${d/./}A${a/./}"
    tmux new-session -d -s "${POOL_SESSION_PREFIX}${n}" \
      "ulimit -v $BACKTEST_MEM_KB; exec $PY $RUNNER --name '$n' \
        --set use_breadth_timing=true --set use_ma200_timing=false \
        --set breadth_mid_cap=0.0 --set breadth_ice_confirm_days=1 \
        --set breadth_defense_threshold='$d' --set breadth_attack_threshold='$a' \
        >> '$LOGDIR/pool_$n.log' 2>&1"
    log "▸ 回测 $n 已派单（防御 $d / 进攻 $a）"
    idx=$((idx + 1)); sleep 2
  done
}

collect_grid_results() {
  local f cnt=0
  for f in experiments/lab/runs/*/*/metrics.json experiments/lab/runs/*/metrics.json; do
    [ -f "$f" ] || continue; cnt=$((cnt + 1))
  done
  [ "$cnt" -gt 0 ] && log "回测产物累计 $cnt 份"
}

run_mainline() {
  log "▶ 主线流水线启动（A→C→D→E + 填充 POOL）"
  # 主线脚本副本隔离：阶段 E 固化可能改写 pipeline_12h.sh，流式读运行中脚本会按偏移错位（9/16 事故同类根因）
  local MCOPY="scripts/pipeline_12h/.pipeline_12h.run.$.sh"
  cp scripts/pipeline_12h/pipeline_12h.sh "$MCOPY"
  if bash "$MCOPY" >> "$LOGDIR/pipeline_12h_retry.out" 2>&1; then
    rm -f "$MCOPY"
    log "✔ 主线流水线本轮成功收尾"
    return 0
  fi
  rm -f "$MCOPY"
  log "✘ 主线流水线本轮失败（详见 pipeline_12h_retry.out 与 pipeline_12h.log），进入回测填充等待重试"
  return 1
}

NEXT_MAINLINE_AT=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  now=$(date +%s)
  remain=$((DEADLINE - now))

  # 到点检查主线是否该重试了（首轮立即跑；失败后 30 分钟、成功后 60 分钟再试）
  if [ "$now" -ge "$NEXT_MAINLINE_AT" ]; then
    grid_running && log "回测网格在跑（$(grid_running_count) 路），等它们收一批再跑主线"
    while grid_running && [ "$(date +%s)" -lt "$DEADLINE" ]; do sleep 20; done
    [ "$(date +%s)" -ge "$DEADLINE" ] && break
    if run_mainline; then
      NEXT_MAINLINE_AT=$(( $(date +%s) + MAINLINE_SUCCESS_INTERVAL ))
    else
      NEXT_MAINLINE_AT=$(( $(date +%s) + MAINLINE_RETRY_INTERVAL ))
    fi
    continue
  fi

  # 间隙：回测网格喂满（保持并行 4 路，吃完一批补一批）
  if [ "$(grid_running_count)" -lt "$MAX_PARALLEL" ]; then
    launch_grid_batch
  fi
  sleep 60
done

# 收尾：不在时间窗外再派新活，等存量回测自然跑完（最长再等 40 分钟）
log "时间窗用尽，停止派新任务，等待存量回测收尾"
wait_end=$(( $(date +%s) + 2400 ))
while grid_running && [ "$(date +%s)" -lt "$wait_end" ]; do sleep 30; done
grid_running && log "存量回测超时未完成，保留 tmux 会话继续后台跑（不中断）"
collect_grid_results
log "守护结束：12 小时窗口收尾完成"
