#!/usr/bin/env bash
# FinAI2.0 AI 监工循环（Hermes 复核层，跑在 tmux 会话 finai-supervisor 内）
#
# 设计哲学（模型偏弱、幻觉严重 ⇒ 不信任其自由发挥）：
#   1. 验证动作（回归测试、门禁复核）由本脚本确定性执行，⛔ 不让模型执行；
#   2. 模型只做一件事：照抄给定的数字、按模板组织汇报；
#   3. 模型超时/失败 ⇒ 自动降级为脚本直发，保证飞书消息必达；
#   4. 所有交互输出落盘 experiments/lab/_logs/supervisor.log，全程可审计。
set -u
cd "$(dirname "$0")/../.."

BOARD=experiments/lab/leaderboard.jsonl
LOGDIR=experiments/lab/_logs
LOG="$LOGDIR/supervisor.log"
FEISHU_TARGET='feishu:oc_79018399aacb85fb92010c02278c6224'
INTERVAL=120           # 轮询间隔（秒）
HERMES_TIMEOUT=150     # 模型单次响应超时（秒）

mkdir -p "$LOGDIR"
ts() { date '+%F %T'; }

send_feishu() {
  sudo hermes send --to "$FEISHU_TARGET" "$1" >/dev/null 2>&1 \
    && echo "$(ts) [sent]" >> "$LOG" || echo "$(ts) [send-failed] $1" >> "$LOG"
}

verify_repo() {
  # 确定性验证：回归测试 + 文档门禁；输出两行结果
  local t g
  t=$(.venv/bin/pytest tests/test_gate_consistency.py tests/test_dividend_strategy.py -q 2>&1 | tail -1)
  g=$(.venv/bin/python -c "from scripts.gates.gate_consistency import DocMetricConsistencyGate; print(DocMetricConsistencyGate().evaluate({}).status.value)" 2>/dev/null)
  echo "回归: $t"
  echo "文档门禁: $g"
}

last_count=0
[ -f "$BOARD" ] && last_count=$(wc -l < "$BOARD")
echo "$(ts) supervisor 启动，初始榜单行数=$last_count" >> "$LOG"

while true; do
  sleep "$INTERVAL"
  count=0
  [ -f "$BOARD" ] && count=$(wc -l < "$BOARD")
  [ "$count" -le "$last_count" ] && continue

  # ---- 新实验完成：逐条处理 ----
  new_lines=$(tail -n $((count - last_count)) "$BOARD")
  last_count=$count

  while IFS= read -r line; do
    [ -z "$line" ] && continue
    exp=$(printf '%s' "$line" | .venv/bin/python -c 'import json,sys; d=json.loads(sys.stdin.read()); print(d["experiment"])' 2>/dev/null)
    [ -z "$exp" ] && continue
    echo "$(ts) 检测到新实验完成: $exp，开始验证流程" >> "$LOG"

    metrics=$(printf '%s' "$line" | .venv/bin/python -c '
import json,sys
d=json.loads(sys.stdin.read())
ov=d.get("overrides",{})
print("参数: " + " ".join(f"{k}={v}" for k,v in ov.items()))
for k in ("cagr","max_drawdown","annual_turnover","win_rate","round_trips","fees_sum","final_nav"):
    print(f"{k}: {d.get(k)}")
' 2>/dev/null)

    verify=$(verify_repo)
    echo "$(ts) 验证结果: $verify" >> "$LOG"

    # ---- 唤起 Hermes 复核并拟稿（指令内嵌，不让其读文件） ----
    hermes_out=$(timeout "$HERMES_TIMEOUT" sudo hermes -z "$(printf '你是 FinAI2.0 监工，禁止编造数字，只能照抄。请把以下实验结果按模板整理成一条飞书汇报（不要多余的话）：
模板: [FinAI2.0 实验完成] <实验名> / <参数行> / <指标行> / <验证行> / 轨迹: experiments/lab/<实验名>/
实验名: %s
%s
%s' "$exp" "$metrics" "$verify")" --safe-mode 2>/dev/null)

    if [ -n "$hermes_out" ] && ! printf '%s' "$hermes_out" | grep -qiE 'error|sorry|无法'; then
      send_feishu "$hermes_out"
      echo "$(ts) $exp 汇报已签发（Hermes 拟稿）" >> "$LOG"
    else
      # 降级：模型不可用，脚本直发原始数据
      send_feishu "[FinAI2.0 实验完成] $exp
$metrics
$verify
轨迹: experiments/lab/$exp/
（本条由监工脚本降级直发，AI 复核未通过）"
      echo "$(ts) $exp 汇报已签发（降级直发）" >> "$LOG"
    fi
  done <<< "$new_lines"
done
