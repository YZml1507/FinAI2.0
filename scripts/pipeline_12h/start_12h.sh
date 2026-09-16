#!/usr/bin/env bash
# FinAI2.0 十二小时流水线 tmux 入口：start 启动 / status 查看 / stop 停止 / log 跟踪日志
# 用法：bash scripts/pipeline_12h/start_12h.sh [start|status|stop|log]
set -u
cd "$(dirname "$0")/../.."
SESSION=finai-12h
DAEMON=scripts/pipeline_12h/daemon_12h.sh
LOGDIR=experiments/lab/_logs
DLOG="$LOGDIR/daemon_12h.log"
PLOG="$LOGDIR/pipeline_12h.log"

cmd="${1:-start}"

case "$cmd" in
  start)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "已在运行：tmux 会话 $SESSION 存在，不重复启动。查看：bash $0 status"
      exit 0
    fi
    mkdir -p "$LOGDIR"
    # 运行副本隔离：守护读的是同目录临时副本，避免阶段 E 固化改写工作区脚本导致 bash 按偏移错位执行（9/16 事故根因）；副本与原文件同目录以保持 cd 定位正确，守护退出时自删
    RUN_COPY="$(dirname "$DAEMON")/.daemon_12h.run.$.sh"
    cp "$DAEMON" "$RUN_COPY"
    tmux new-session -d -s "$SESSION" "bash $RUN_COPY; rm -f $RUN_COPY"
    echo "已启动：tmux 会话 $SESSION 连续运行 12 小时（主线自动重试 + 回测间隙填满）。"
    echo "  查看状态：bash $0 status"
    echo "  跟踪日志：bash $0 log"
    echo "  停止任务：bash $0 stop"
    ;;
  status)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "运行中：tmux 会话 $SESSION"
      echo "---- 守护日志最近 5 行 ----"
      tail -5 "$DLOG" 2>/dev/null || echo '(暂无守护日志)'
      echo "---- 流水线日志最近 5 行 ----"
      tail -5 "$PLOG" 2>/dev/null || echo '(暂无流水线日志)'
    else
      echo "未运行：tmux 会话 $SESSION 不存在。启动：bash $0 start"
    fi
    ;;
  stop)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      tmux kill-session -t "$SESSION"
      echo "已停止：tmux 会话 $SESSION 已终止。"
    else
      echo "无需停止：tmux 会话 $SESSION 不存在。"
    fi
    ;;
  log)
    echo "跟踪守护日志（Ctrl+C 退出，不影响任务）：$DLOG"
    tail -f "$DLOG"
    ;;
  *)
    echo "未知命令：$cmd。用法：bash $0 [start|status|stop|log]"
    exit 1
    ;;
esac
