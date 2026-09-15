#!/usr/bin/env bash
# 方案 D 宽度择时参数网格 · 分批并行实验启动器（4 核 8G，taskset 绑核，产物隔离）
#
# 依据 P0 归因诊断结论设计矩阵（2026-09-15，DIAGNOSIS_REPORT.md）：
#   * defense ↑（0.20→0.25） 提前避险，压缩危机段暴露窗口；
#   * ice_confirm_days ↓（2→1） 缩短冰点确认期扛跌天数（MDD 主因）；
#   * mid_cap ↓（0.5→0.0/0.3） 切断警戒区 648 天累计阴跌（CAGR 慢性拖累）；
#   * attack 0.35/0.40/0.45 覆盖进攻线敏感度。
# 目标：找 MDD<35% 且 CAGR>0 的组合；全部落 leaderboard.jsonl。
#
# 并发模型：⛔ 分批并行（不是一次性全启动）——每批 3 组各占核 0/1/2（留 1 核
#   给系统/IO），等本批全部完成再启动下一批。24 组分 8 批 × 约26分钟/批 ≈ 3.5 小时。
#   （若 24 组同时启动，24 进程抢 3 核，单组耗时将膨胀数倍，⛔ 严禁。）
#
# 用法：bash scripts/lab/breadth_grid.sh        # 前台/后台均可
set -u
cd "$(dirname "$0")/../.."   # 仓根

PY=.venv/bin/python
RUNNER=scripts/lab/run_experiment.py
LOGDIR=experiments/lab/_logs
BREADTH=experiments/lab/market-breadth-a/breadth20_daily.parquet
mkdir -p "$LOGDIR"

# ---- 网格定义：名称|参数覆盖（核在批内按 0/1/2 轮转分配） ----
# 命名编码：d=defense, a=attack, m=mid_cap(×100), i=ice_days
read -r -d '' GRID <<'EOF' || true
bd20a35m00i1|breadth_defense_threshold=0.20 breadth_attack_threshold=0.35 breadth_mid_cap=0.0 breadth_ice_confirm_days=1
bd20a35m00i2|breadth_defense_threshold=0.20 breadth_attack_threshold=0.35 breadth_mid_cap=0.0 breadth_ice_confirm_days=2
bd20a35m30i1|breadth_defense_threshold=0.20 breadth_attack_threshold=0.35 breadth_mid_cap=0.3 breadth_ice_confirm_days=1
bd20a35m30i2|breadth_defense_threshold=0.20 breadth_attack_threshold=0.35 breadth_mid_cap=0.3 breadth_ice_confirm_days=2
bd20a40m00i1|breadth_defense_threshold=0.20 breadth_attack_threshold=0.40 breadth_mid_cap=0.0 breadth_ice_confirm_days=1
bd20a40m00i2|breadth_defense_threshold=0.20 breadth_attack_threshold=0.40 breadth_mid_cap=0.0 breadth_ice_confirm_days=2
bd20a40m30i1|breadth_defense_threshold=0.20 breadth_attack_threshold=0.40 breadth_mid_cap=0.3 breadth_ice_confirm_days=1
bd20a40m30i2|breadth_defense_threshold=0.20 breadth_attack_threshold=0.40 breadth_mid_cap=0.3 breadth_ice_confirm_days=2
bd20a45m00i1|breadth_defense_threshold=0.20 breadth_attack_threshold=0.45 breadth_mid_cap=0.0 breadth_ice_confirm_days=1
bd20a45m00i2|breadth_defense_threshold=0.20 breadth_attack_threshold=0.45 breadth_mid_cap=0.0 breadth_ice_confirm_days=2
bd20a45m30i1|breadth_defense_threshold=0.20 breadth_attack_threshold=0.45 breadth_mid_cap=0.3 breadth_ice_confirm_days=1
bd20a45m30i2|breadth_defense_threshold=0.20 breadth_attack_threshold=0.45 breadth_mid_cap=0.3 breadth_ice_confirm_days=2
bd25a35m00i1|breadth_defense_threshold=0.25 breadth_attack_threshold=0.35 breadth_mid_cap=0.0 breadth_ice_confirm_days=1
bd25a35m00i2|breadth_defense_threshold=0.25 breadth_attack_threshold=0.35 breadth_mid_cap=0.0 breadth_ice_confirm_days=2
bd25a35m30i1|breadth_defense_threshold=0.25 breadth_attack_threshold=0.35 breadth_mid_cap=0.3 breadth_ice_confirm_days=1
bd25a35m30i2|breadth_defense_threshold=0.25 breadth_attack_threshold=0.35 breadth_mid_cap=0.3 breadth_ice_confirm_days=2
bd25a40m00i1|breadth_defense_threshold=0.25 breadth_attack_threshold=0.40 breadth_mid_cap=0.0 breadth_ice_confirm_days=1
bd25a40m00i2|breadth_defense_threshold=0.25 breadth_attack_threshold=0.40 breadth_mid_cap=0.0 breadth_ice_confirm_days=2
bd25a40m30i1|breadth_defense_threshold=0.25 breadth_attack_threshold=0.40 breadth_mid_cap=0.3 breadth_ice_confirm_days=1
bd25a40m30i2|breadth_defense_threshold=0.25 breadth_attack_threshold=0.40 breadth_mid_cap=0.3 breadth_ice_confirm_days=2
bd25a45m00i1|breadth_defense_threshold=0.25 breadth_attack_threshold=0.45 breadth_mid_cap=0.0 breadth_ice_confirm_days=1
bd25a45m00i2|breadth_defense_threshold=0.25 breadth_attack_threshold=0.45 breadth_mid_cap=0.0 breadth_ice_confirm_days=2
bd25a45m30i1|breadth_defense_threshold=0.25 breadth_attack_threshold=0.45 breadth_mid_cap=0.3 breadth_ice_confirm_days=1
bd25a45m30i2|breadth_defense_threshold=0.25 breadth_attack_threshold=0.45 breadth_mid_cap=0.3 breadth_ice_confirm_days=2
EOF

BATCH=3   # 每批并行数（核 0/1/2，留 1 核给系统与 IO）
total=0
fail_total=0
echo "[breadth-grid] $(date '+%F %T') 启动宽度参数网格（分批并行，每批 $BATCH 组）"

batch_pids=()
batch_names=()
core=0
flush_batch() {
  # 等待当前批全部结束并统计失败
  local p n rc
  for idx in "${!batch_pids[@]}"; do
    p=${batch_pids[$idx]}; n=${batch_names[$idx]}
    if wait "$p"; then
      echo "[breadth-grid] $(date '+%F %T') ✅ 完成 $n"
    else
      rc=$?
      echo "[breadth-grid] $(date '+%F %T') ⛔ 失败 $n (rc=$rc)"
      fail_total=$((fail_total+1))
    fi
  done
  batch_pids=()
  batch_names=()
  core=0
}

while IFS='|' read -r name params; do
  [ -z "${name:-}" ] && continue
  log="$LOGDIR/${name}.log"
  echo "[breadth-grid] 启动 $name -> 核 $core | $params"
  BREADTH_FILE="$BREADTH" \
  taskset -c "$core" $PY "$RUNNER" --name "$name" \
    --set use_breadth_timing=1 \
    $(for kv in $params; do printf -- '--set %s ' "$kv"; done) \
    > "$log" 2>&1 &
  batch_pids+=("$!")
  batch_names+=("$name")
  total=$((total+1))
  core=$((core+1))
  if [ "$core" -ge "$BATCH" ]; then
    flush_batch
  fi
done <<< "$GRID"
flush_batch   # 收尾：等待最后一批

echo "[breadth-grid] $(date '+%F %T') 全部结束：共 $total 组，失败 $fail_total 组。榜单: experiments/lab/leaderboard.jsonl"
