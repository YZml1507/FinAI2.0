#!/usr/bin/env bash
# 2026-09-15 全夜大网格（在 tmux finai-overnight 会话内运行）
#
# 设计依据（夜间 9 组结果规律）：
#   * 破位确认要慢（b3 最优 MD D29.71%）、重建确认要更慢（r3 最优 MDD 24.80%）；
#   * 缓冲带 2%~2.5% 有效，0.5% 太敏感；
#   * 对称确认（b2r2）最差——非对称是方向。
#
# 结构（30 组 + 冠军复跑，3 路绑核，批内并行、批间串行）：
#   阶段 0：复现验证 2 组（b3r1 / b2r3 原样重跑，同代码基线 metrics 须逐字节一致）
#   阶段 1：最优点加密 9 组（b3~b4 × r2~r3 × buffer 1.5%~3%）
#   阶段 2：结构维度扩展 9 组（择时锁定最优底座，扰动调仓/候选池/股息率/持仓数）
#   阶段 3：组合深挖 9 组（高缓冲 + 慢确认 × 结构参数组合）
#   阶段 4：冠军自动复跑 + 全套门禁验证 + 飞书总结
#
# 铁律：产物只进 experiments/lab/，⛔ 不碰 experiments/runs/；每实验进榜单 → 监工自动汇报。
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
RUNNER=scripts/lab/run_experiment.py
LOGDIR=experiments/lab/_logs
LOG="$LOGDIR/overnight.log"
FEISHU='feishu:oc_79018399aacb85fb92010c02278c6224'
mkdir -p "$LOGDIR"

T() { date '+%F %T'; }
say() { echo "$(T) $1" | tee -a "$LOG"; }
notify() { sudo hermes send --to "$FEISHU" "$1" >/dev/null 2>&1 || true; }

# run_batch <label> 然后 stdin 每行: 实验名|核|参数串
run_batch() {
  local label="$1"
  local pids=()
  say "[$label] 批次启动"
  while IFS='|' read -r name core params; do
    [ -z "${name:-}" ] && continue
    # shellcheck disable=SC2086
    taskset -c "$core" $PY "$RUNNER" --name "$name" \
      $(for kv in $params; do printf -- '--set %s ' "$kv"; done) \
      > "$LOGDIR/${name}.log" 2>&1 &
    pids+=("$!")
    say "[$label]   $name -> 核 $core (pid $!)"
  done
  local fail=0
  for p in "${pids[@]}"; do wait "$p" || fail=$((fail+1)); done
  say "[$label] 批次完成，失败 $fail"
  [ "$fail" -gt 0 ] && notify "[FinAI2.0 告警] 批次 $label 有 $fail 路失败，日志见 experiments/lab/_logs/"
  return 0
}

# 最优底座（夜间冠军区）：buffer=0.02, breach=2, rebuild=3
BASE_B2R3="timing_breach_buffer=0.02 timing_breach_confirm_days=2 timing_rebuild_confirm_days=3"
BASE_B3R1="timing_breach_buffer=0.02 timing_breach_confirm_days=3 timing_rebuild_confirm_days=1"

say "======== 全夜大网格点火（30 组 + 冠军复跑） ========"
notify "[FinAI2.0 全夜网格] 已点火：30 组实验（复现验证 2 + 加密 9 + 扩展 9 + 深挖 9）+ 冠军收官复跑，预计约 5 小时。"

# ============ 阶段 0：复现验证（确认今晚突破不是运气） ============
say "=== 阶段 0：复现验证 ==="
run_batch "S0-复现" <<'EOF'
on-repro-b3r1|0|timing_breach_buffer=0.02 timing_breach_confirm_days=3 timing_rebuild_confirm_days=1
on-repro-b2r3|1|timing_breach_buffer=0.02 timing_breach_confirm_days=2 timing_rebuild_confirm_days=3
EOF

# ============ 阶段 1：最优点加密 ============
say "=== 阶段 1：最优点加密（b3~b4 × r2~r3 × buf 1.5%~3%） ==="
run_batch "S1a" <<'EOF'
on-b3r2-buf020|0|timing_breach_buffer=0.02 timing_breach_confirm_days=3 timing_rebuild_confirm_days=2
on-b3r3-buf020|1|timing_breach_buffer=0.02 timing_breach_confirm_days=3 timing_rebuild_confirm_days=3
on-b4r3-buf020|2|timing_breach_buffer=0.02 timing_breach_confirm_days=4 timing_rebuild_confirm_days=3
EOF
run_batch "S1b" <<'EOF'
on-b3r1-buf025|0|timing_breach_buffer=0.025 timing_breach_confirm_days=3 timing_rebuild_confirm_days=1
on-b3r3-buf025|1|timing_breach_buffer=0.025 timing_breach_confirm_days=3 timing_rebuild_confirm_days=3
on-b2r3-buf025|2|timing_breach_buffer=0.025 timing_breach_confirm_days=2 timing_rebuild_confirm_days=3
EOF
run_batch "S1c" <<'EOF'
on-b3r1-buf015|0|timing_breach_buffer=0.015 timing_breach_confirm_days=3 timing_rebuild_confirm_days=1
on-b3r3-buf015|1|timing_breach_buffer=0.015 timing_breach_confirm_days=3 timing_rebuild_confirm_days=3
on-b2r3-buf030|2|timing_breach_buffer=0.03 timing_breach_confirm_days=2 timing_rebuild_confirm_days=3
EOF

# ============ 阶段 2：结构维度扩展（择时锁定 b2r3 底座） ============
say "=== 阶段 2：结构维度扩展（择时底座 b2r3） ==="
run_batch "S2a-调仓频率" <<EOF
on-rb10-b2r3|0|$BASE_B2R3 rebalance_days=10
on-rb40-b2r3|1|$BASE_B2R3 rebalance_days=40
on-rb60-b2r3|2|$BASE_B2R3 rebalance_days=60
EOF
run_batch "S2b-候选池" <<EOF
on-pool30-b2r3|0|$BASE_B2R3 candidate_pool_size=30
on-pool80-b2r3|1|$BASE_B2R3 candidate_pool_size=80
on-pool100-b2r3|2|$BASE_B2R3 candidate_pool_size=100
EOF
run_batch "S2c-股息与持仓" <<EOF
on-dy025-b2r3|0|$BASE_B2R3 min_dividend_yield=0.025
on-dy035-b2r3|1|$BASE_B2R3 min_dividend_yield=0.035
on-pos3-b2r3|2|$BASE_B2R3 default_positions=3
EOF

# ============ 阶段 3：组合深挖（高缓冲慢确认 × 结构参数） ============
say "=== 阶段 3：组合深挖（b3r3 底座 × 结构参数） ==="
B33="timing_breach_buffer=0.025 timing_breach_confirm_days=3 timing_rebuild_confirm_days=3"
run_batch "S3a" <<EOF
on-b3r3-rb10|0|$B33 rebalance_days=10
on-b3r3-rb40|1|$B33 rebalance_days=40
on-b3r3-pool80|2|$B33 candidate_pool_size=80
EOF
run_batch "S3b" <<EOF
on-b3r3-dy025|0|$B33 min_dividend_yield=0.025
on-b3r3-pos3|1|$B33 default_positions=3
on-b3r3-buf030|2|timing_breach_buffer=0.03 timing_breach_confirm_days=3 timing_rebuild_confirm_days=3
EOF
run_batch "S3c" <<EOF
on-b4r4-buf025|0|timing_breach_buffer=0.025 timing_breach_confirm_days=4 timing_rebuild_confirm_days=4
on-b3r2-buf030|1|timing_breach_buffer=0.03 timing_breach_confirm_days=3 timing_rebuild_confirm_days=2
on-b2r3-rb15|2|$BASE_B2R3 rebalance_days=15
EOF

# ============ 阶段 4：冠军收官 ============
say "=== 阶段 4：冠军复跑 + 全门禁验证 ==="
mapfile -t CHAMP < <($PY scripts/lab/select_champion.py)
C_NAME="${CHAMP[0]}"
C_PARAMS="${CHAMP[1]}"
C_MDD="${CHAMP[2]}"
C_CAGR="${CHAMP[3]}"
say "冠军: $C_NAME (MDD $C_MDD, CAGR $C_CAGR)，复跑验证"
# shellcheck disable=SC2086
taskset -c 0 $PY "$RUNNER" --name "on-champion-repro" $C_PARAMS > "$LOGDIR/on-champion-repro.log" 2>&1
say "冠军复跑完成"

# 验证：冠军复跑结果与原结果一致性
REPRO_CHECK=$($PY -c "
import json, pathlib
lines = [json.loads(l) for l in pathlib.Path('experiments/lab/leaderboard.jsonl').read_text().splitlines() if l.strip()]
orig = [r for r in lines if r['experiment'] == '$C_NAME']
repro = [r for r in lines if r['experiment'] == 'on-champion-repro']
if orig and repro:
    o, r = orig[-1], repro[-1]
    keys = ['cagr','max_drawdown','win_rate','round_trips','final_nav','fees_sum']
    same = all(str(o.get(k)) == str(r.get(k)) for k in keys)
    print('REPRO_PASS' if same else 'REPRO_FAIL')
    for k in keys: print(f'  {k}: {o.get(k)} vs {r.get(k)}')
else:
    print('REPRO_MISSING')
" 2>&1)
say "复现校验: $REPRO_CHECK"

# 全量测试
TEST_OUT=$($PY -m pytest tests/test_gate_consistency.py tests/test_dividend_strategy.py -q 2>&1 | tail -1)
say "回归测试: $TEST_OUT"
DOCGATE=$($PY -c "from scripts.gates.gate_consistency import DocMetricConsistencyGate; print(DocMetricConsistencyGate().evaluate({}).status.value)" 2>/dev/null)
say "文档门禁: $DOCGATE"

# 发总结
notify "[FinAI2.0 全夜网格完成] 30 组实验 + 冠军复跑全部结束。
冠军: $C_NAME (MDD $C_MDD, CAGR $C_CAGR)
复现校验: $(echo "$REPRO_CHECK" | head -1)
回归: $TEST_OUT | 文档门禁: $DOCGATE
榜单: experiments/lab/leaderboard.jsonl"

say "======== 全夜大网格收官 ========"
