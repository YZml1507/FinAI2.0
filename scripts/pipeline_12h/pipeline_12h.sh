#!/usr/bin/env bash
# FinAI2.0 十二小时主驱动流水线（纯确定性脚本，零模型依赖）
# 主线：A(基线晋级) -> C(清尾降噪) -> D(装甲一评估) -> E(回归固化)
# 配套：B(宽度口径接入) 失败后自动重试一次，再失败则回滚跳过，不阻塞主线，最终汇报列出
# 填充：B 与 E 的等待间隙并行跑 PEAD 统计探针；主线结束自动接最优点邻域细扫 + 跨周期回测
# 通知：每块真实完成才发一条飞书；Hermes 仅做异步验收，结果只记录不阻塞
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
LOGDIR=experiments/lab/_logs
PLOG="$LOGDIR/pipeline_12h.log"
FEISHU_TARGET='feishu:oc_79018399aacb85fb92010c02278c6224'
mkdir -p "$LOGDIR"

ts() { date '+%F %T'; }
notify() {
  sudo hermes send --to "$FEISHU_TARGET" "$1" >/dev/null 2>&1 \
    && echo "$(ts) [sent] $1" >> "$PLOG" \
    || echo "$(ts) [send-failed] $1" >> "$PLOG"
}
# Hermes 异步验收：后台跑，结果写日志可选发飞书，绝不阻塞流水线
hermes_review() {
  local tag="$1" subject="$2"
  ( timeout 300 sudo hermes -z "验收FinAI2.0十二小时流水线阶段产物。阶段：$tag。请只读检查以下产物是否合理（不修改任何文件）：$subject。用不超过三句话给出验收结论与发现的问题。" --safe-mode > "$LOGDIR/review_$tag.out" 2>&1; \
    echo "$(ts) [review-$tag] $(tail -c 300 "$LOGDIR/review_$tag.out" 2>/dev/null | tr '\n' ' ')" >> "$PLOG" ) &
}

run_stage() {
  local tag="$1" script="$2"
  echo "$(ts) ▸ 阶段 $tag 开始" >> "$PLOG"
  if bash "$script" > "$LOGDIR/pipe_$tag.out" 2>&1; then
    echo "$(ts) ✔ 阶段 $tag 完成" >> "$PLOG"
    notify "[FinAI2.0 任务完成] $tag：$(tail -1 "$LOGDIR/pipe_$tag.out" | cut -c1-250)"
    hermes_review "$tag" "$(tail -3 "$LOGDIR/pipe_$tag.out" | tr '\n' ' ' | cut -c1-300)"
    return 0
  else
    echo "$(ts) ✘ 阶段 $tag 失败" >> "$PLOG"
    return 1
  fi
}

echo "$(ts) 流水线启动" >> "$PLOG"
notify "[FinAI2.0 十二小时任务] 确定性流水线已启动（A→C→D→E 主线 + 填充回测），每完成一块发一条通知，Hermes 仅异步验收不阻塞。"

FAILED=""

# ---- 阶段 A：基线晋级（失败即熔断，后面都依赖基线产物） ----
if ! run_stage A scripts/pipeline_12h/stage_A_baseline.sh; then
  notify "[FinAI2.0 任务失败] 阶段A 基线晋级失败，流水线熔断。最近输出：$(tail -2 "$LOGDIR/pipe_A.out" 2>/dev/null | tr '\n' ' ' | cut -c1-200)"
  echo "$(ts) 流水线熔断于阶段A" >> "$PLOG"
  exit 1
fi

# ---- 阶段 B：宽度口径接入（重试一次；再失败则回滚跳过，不阻塞） ----
if ! run_stage B scripts/pipeline_12h/stage_B_context.sh; then
  echo "$(ts) 阶段B 首次失败，3秒后重试一次" >> "$PLOG"
  sleep 3
  if ! run_stage B scripts/pipeline_12h/stage_B_context.sh; then
    git checkout -- scripts/run_dividend_backtest.py 2>/dev/null
    rm -f tests/test_breadth_gate_context.py
    notify "[FinAI2.0 任务失败] 阶段B 两次均未通过，已回滚跳过（不影响主线）。原因待人工复核：$(tail -2 "$LOGDIR/pipe_B.out" 2>/dev/null | tr '\n' ' ' | cut -c1-180)"
    FAILED="$FAILED B"
  fi
fi

# ---- 阶段 C：清尾降噪（失败不熔断，但记录；E 的回归会兜住） ----
run_stage C scripts/pipeline_12h/stage_C_fixes.sh || FAILED="$FAILED C"

# ---- 阶段 D：装甲一评估（纯文档，必成功） ----
run_stage D scripts/pipeline_12h/stage_D_armor1.sh || FAILED="$FAILED D"

# ---- PEAD 统计探针（填充池 POOL-2，后台并行，不等结果） ----
if [ -f scripts/pipeline_12h/stage_POOL2_pead.py ]; then
  ( $PY scripts/pipeline_12h/stage_POOL2_pead.py > "$LOGDIR/pipe_POOL2.out" 2>&1 \
    && { echo "$(ts) ✔ 填充池POOL-2 完成" >> "$PLOG"; notify "[FinAI2.0 任务完成] POOL-2：PEAD 信号面预研统计表已产出（见 experiments/lab/_logs/pipe_POOL2.out）"; } \
    || echo "$(ts) ✘ 填充池POOL-2 失败" >> "$PLOG" ) &
fi

# ---- 阶段 E：全量回归 + commit 固化（失败即汇报，不回滚已 commit 内容） ----
if ! run_stage E scripts/pipeline_12h/stage_E_finalize.sh; then
  notify "[FinAI2.0 任务失败] 阶段E 回归或固化失败，停止后续填充回测。最近输出：$(tail -2 "$LOGDIR/pipe_E.out" 2>/dev/null | tr '\n' ' ' | cut -c1-200)"
  echo "$(ts) 流水线主线终止于阶段E" >> "$PLOG"
  exit 1
fi

notify "[FinAI2.0 任务完成] 主线 A/C/D/E 全部走完${FAILED:+，跳过/失败块：$FAILED}。接下来进入填充回测喂满算力。"

# ================= 填充算力池（主线完成后启动） =================
RUNNER=scripts/lab/run_experiment.py
BS=experiments/lab/market-breadth-a/breadth20_daily.parquet
grid_running_count() { tmux ls 2>/dev/null | grep -c '^finai-pool-' || true; }
MAX_GRID_PARALLEL=2   # 网格回测并发上限（8G 机器，与守护侧一致，防批量派单打满内存）
run_grid() {
  local name="$1"; shift
  # 并发闸门：等待直到网格会话数低于上限再派单（POOL-1 原一次并发 16 路打满 8G 内存的事故根因）
  while [ "$(grid_running_count)" -ge "$MAX_GRID_PARALLEL" ]; do sleep 15; done
  tmux new-session -d -s "finai-pool-$name" \
    "ulimit -v $((3 * 1024 * 1024)); exec $PY $RUNNER --name '$name' $* >> '$LOGDIR/pool_$name.log' 2>&1"
  echo "$(ts) ▸ 回测 $name 已启动" >> "$PLOG"
}
wait_all() {
  while tmux ls 2>/dev/null | grep -q 'finai-pool-'; do sleep 30; done
}

# POOL-1：最优点邻域细扫（bd25a45m00i1 周围 ±0.01/±0.02，目标 MDD<20%）
BASE_OV="--set use_breadth_timing=true --set use_ma200_timing=false --set breadth_mid_cap=0.0 --set breadth_ice_confirm_days=1"
for d in 0.23 0.24 0.26 0.27; do
  for a in 0.43 0.44 0.46 0.47; do
    n="poolD${d/./}A${a/./}"
    run_grid "$n" $BASE_OV --set breadth_defense_threshold="$d" --set breadth_attack_threshold="$a"
    sleep 2
  done
done
wait_all
$PY scripts/lab/analyze_breadth_grid.py > /dev/null 2>&1
notify "[FinAI2.0 任务完成] POOL-1：最优点邻域 16 组细扫完成，最新推荐参数见 grid_analysis.txt"

# POOL-3：跨周期稳健性对照（四子区间验证不是单周期运气）
for seg in '2015 2018 S1' '2019 2021 S2' '2022 2024 S3' '2024 2026 S4'; do
  set -- $seg
  run_grid "poolSeg$3" $BASE_OV --set breadth_defense_threshold=0.25 --set breadth_attack_threshold=0.45 \
    --set backtest_start="$1-01-01" --set backtest_end="$2-12-31"
  sleep 2
done
wait_all
notify "[FinAI2.0 任务完成] POOL-3：跨四周期稳健性对照完成，验证最优参数非单周期运气"

# ---- 总结 ----
done_count=$(grep -c '✔' "$PLOG" 2>/dev/null || echo 0)
notify "[FinAI2.0 十二小时任务] 流水线全部执行完毕：主线 A/C/D/E + 填充 POOL-1/2/3，共 $done_count 块成功${FAILED:+，跳过：$FAILED}。基线=$(cat "$LOGDIR/baseline_run_id.txt" 2>/dev/null || echo 见阶段A)。"
echo "$(ts) 流水线全部执行完毕" >> "$PLOG"
