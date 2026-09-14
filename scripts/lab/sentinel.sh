#!/usr/bin/env bash
# FinAI2.0 实验哨兵（确定性巡检，零 AI 依赖）
# 职责：
#   1. 监控 experiments/lab/leaderboard.jsonl 行数变化 → 新实验完成即发飞书
#   2. 监控实验日志活性 → 超 30 分钟无更新判卡死，发飞书告警
#   3. 每轮巡检心跳写入 experiments/lab/_logs/sentinel.log
# 铁律：只读 + 发消息，不修改任何项目文件，不杀任何进程。
set -u
cd "$(dirname "$0")/../.."

BOARD=experiments/lab/leaderboard.jsonl
LOGDIR=experiments/lab/_logs
SENTINEL_LOG="$LOGDIR/sentinel.log"
FEISHU_TARGET='feishu:oc_79018399aacb85fb92010c02278c6224'
INTERVAL=300          # 巡检间隔（秒）
STALE_SECONDS=1800    # 日志静默超过 30 分钟判卡死

mkdir -p "$LOGDIR"

tsay() { date '+%F %T'; }

notify() {
  # $1 = 消息体；发送失败不中断哨兵
  sudo hermes send --to "$FEISHU_TARGET" "$1" >/dev/null 2>&1 \
    && echo "$(tsay) [sent] $1" >> "$SENTINEL_LOG" \
    || echo "$(tsay) [send-failed] $1" >> "$SENTINEL_LOG"
}

last_count=0
[ -f "$BOARD" ] && last_count=$(wc -l < "$BOARD")
declared_stale=""

echo "$(tsay) sentinel 启动，初始榜单行数=$last_count" >> "$SENTINEL_LOG"
notify "[FinAI2.0 哨兵] 已启动巡检（间隔 ${INTERVAL}s），当前榜单 $last_count 条实验记录。"

while true; do
  sleep "$INTERVAL"

  # ---- ① 新实验完成检测 ----
  count=0
  [ -f "$BOARD" ] && count=$(wc -l < "$BOARD")
  if [ "$count" -gt "$last_count" ]; then
    new_lines=$(tail -n $((count - last_count)) "$BOARD")
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      summary=$(printf '%s' "$line" | .venv/bin/python -c "
import json,sys
d=json.loads(sys.stdin.read())
ov=d.get('overrides',{})
print('%s | 参数 %s | CAGR %s MDD %s 胜率 %s | 轨迹 %s' % (
  d['experiment'],
  ' '.join(f'{k}={v}' for k,v in ov.items()),
  d['cagr'][:7], d['max_drawdown'][:7], d['win_rate'][:7],
  d['lab_dir']))
" 2>/dev/null || echo "$line")
      notify "[FinAI2.0 实验完成] $summary"
    done <<< "$new_lines"
    last_count=$count
  fi

  # ---- ② 日志卡死检测 ----
  now=$(date +%s)
  stale_found=""
  for log in "$LOGDIR"/*.log; do
    [ -f "$log" ] || continue
    base=$(basename "$log")
    case "$base" in sentinel.log|grid_main.log) continue;; esac
    mtime=$(stat -c %Y "$log")
    age=$((now - mtime))
    # 只关心仍有对应进程在跑、但日志静默的实验
    exp="${base%.log}"
    if pgrep -f "run_experiment.py --name $exp" >/dev/null 2>&1; then
      if [ "$age" -gt "$STALE_SECONDS" ] && [ "$declared_stale" != "$exp" ]; then
        stale_found="$exp"
        declared_stale="$exp"
      fi
    else
      # 进程已退出则重置卡死标记（下次同名实验重新计时）
      [ "$declared_stale" = "$exp" ] && declared_stale=""
    fi
  done
  if [ -n "$stale_found" ]; then
    tail5=$(tail -5 "$LOGDIR/$stale_found.log" 2>/dev/null)
    notify "[FinAI2.0 异常] 实验 $stale_found 日志静默超 30 分钟，疑似卡死。
证据(最后5行):
$tail5
已停止后续动作，等待人工指示。"
  fi

  echo "$(tsay) heartbeat board=$count stale=${declared_stale:-none}" >> "$SENTINEL_LOG"
done
