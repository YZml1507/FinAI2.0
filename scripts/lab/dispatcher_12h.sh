#!/usr/bin/env bash
# FinAI2.0 十二小时任务跳板调度器
# 职责：按 docs/TARGET_12H.md 的顺序逐块唤起 Hermes，等完成信号后发一条飞书并推进下一块。
# 铁律：只读标记文件 + 发消息 + 起一次性 Hermes 进程；不改主线代码，不杀任何进程。
set -u
cd "$(dirname "$0")/../.."

LOGDIR=experiments/lab/_logs
DLOG="$LOGDIR/dispatcher.log"
FEISHU_TARGET='feishu:oc_79018399aacb85fb92010c02278c6224'
POLL=900            # 轮询间隔（秒）= 15 分钟
mkdir -p "$LOGDIR"

tsay() { date '+%F %T'; }
notify() {
  sudo hermes send --to "$FEISHU_TARGET" "$1" >/dev/null 2>&1 \
    && echo "$(tsay) [sent] $1" >> "$DLOG" \
    || echo "$(tsay) [send-failed] $1" >> "$DLOG"
}

# 任务表：编号|预算分钟|一句话任务内容（发给 Hermes 的指令）
# 说明：每个任务都要求 Hermes 完成后在 experiments/lab/_logs/ 落 <编号>.done 标记文件，内容写一句话结果。
TASKS=(
"A|180|任务A：对宽度最优组合 bd25a45m00i1 做全量六维门禁复核并落盘；若红灯按 TARGET_12H.md 既定顺序换下一组。通过的组合定为候选基线，生成基线产物（含出处三件套），切换 tests 权威指针，写 120 字以内 SELECTION.md 说明选择理由。全部 7 组不过则发升级飞书并停止主线。完成后写 experiments/lab/_logs/A.done，内容为一句话结果。"
"B|150|任务B：宽度口径正式工程接入。B1 在 run_dividend_backtest.py 的 _build_post_run_gate_context 注入 breadth_series 等宽度字段；B2 为 S-2 宽度口径新增测试用例，新测试必须全绿。完成后写 experiments/lab/_logs/B.done，内容为一句话结果。"
"C|90|任务C：清尾降噪。C1 修 scripts/lab/sentinel.sh：榜单行解析失败兜底不得原样透传整行 JSON，实验完成消息剔除巨型 breadth_series 字段只留关键参数，改完用最近一条真实榜单行演练验证消息体干净。C2 修 _git_code_hash：dirty 哈希只覆盖 strategy/scripts/backtest/tests，验证仅榜单追加时指纹不变、代码改动时指纹必变。完成后写 experiments/lab/_logs/C.done，内容为一句话结果。"
"D|45|任务D：装甲一按修正后立项理由（S-4 事前化确定性收益，真实年化仅 +0.012pp~+0.3pp）重新评估。收益不值得则撤销立项写明理由，仍值得则给最小实施方案，两者只选其一。结论写入 docs/ARMOR1_EXDIV_FILTER_DESIGN.md 尾部并同步 TASK_TRACKER。完成后写 experiments/lab/_logs/D.done，内容为一句话结果。"
"E|60|任务E：固化收尾。全量回归 .venv/bin/pytest tests/ -q 全绿；git commit 固化全部改动并在提交信息精准列明；回写 TASK_TRACKER 勾完成项并更新时间戳。完成后写 experiments/lab/_logs/E.done，内容为一句话结果，并评估是否仍有时间启动任务F。"
"POOL-1|150|填充池POOL-1：围绕 bd25a45m00i1 最优点做 ±0.01/±0.02 细颗粒度网格回测（实验名带 pool 前缀），产出最终推荐参数表存 docs/。只读数据+新写产物，不碰主线代码。完成后写 experiments/lab/_logs/POOL-1.done，内容为一句话结果。"
"POOL-3|150|填充池POOL-3：bd25a45m00i1 参数在 2015-2018/2019-2021/2022-2024/2024-至今 四个子区间各跑一次回测，产出子区间指标矩阵存 docs/。完成后写 experiments/lab/_logs/POOL-3.done，内容为一句话结果。"
"POOL-2|120|填充池POOL-2：PEAD 信号面预研。归母口径业绩公告事件信号滚动统计探针，产出年度触发次数与触发后 5/10/20 日平均超额两张表存 experiments/lab/，只产统计不开策略代码。完成后写 experiments/lab/_logs/POOL-2.done，内容为一句话结果。"
"POOL-4|90|填充池POOL-4：装甲一事件研究（仅当任务D结论为值得做时执行）：487 只含除权标记股票除权前 15 天禁建仓的净值对比数据落盘。若 D 撤销立项则本块直接写 POOL-4.done 标记跳过。完成后写 experiments/lab/_logs/POOL-4.done，内容为一句话结果。"
"F|120|任务F（保底）：仅当 A~E 全完成且时间有富余才启动——PEAD 探路实验骨架准备（基于 financial_pit 归母口径）。时间不够则直接写 F.done 标记保留下一阶段，不强推。"
)

echo "$(tsay) dispatcher 启动，共 ${#TASKS[@]} 块任务" >> "$DLOG"
notify "[FinAI2.0 十二小时任务] 调度器已启动，按 TARGET_12H.md 顺序执行 ${#TASKS[@]} 块任务，每块完成发一条通知。"

for item in "${TASKS[@]}"; do
  id="${item%%|*}"
  rest="${item#*|}"
  budget="${rest%%|*}"
  cmd="${rest#*|}"
  done_mark="$LOGDIR/$id.done"
  sess="finai-12h-$id"

  echo "$(tsay) ▸ 开始块 $id（预算 ${budget} 分钟）：$cmd" >> "$DLOG"

  # 起一次性 Hermes 会话执行本块
  tmux new-session -d -s "$sess" "sudo hermes -z \"$cmd\" --safe-mode >> '$LOGDIR/$id.out' 2>&1; touch '$done_mark'"

  waited=0
  alerted=0
  while true; do
    sleep "$POLL"
    waited=$((waited + POLL / 60))
    if [ -f "$done_mark" ]; then
      summary=$(tr -d '\n' < "$done_mark" | cut -c1-300)
      notify "[FinAI2.0 任务完成] $id：$summary"
      echo "$(tsay) ✔ 块 $id 完成（耗时约 ${waited} 分钟）：$summary" >> "$DLOG"
      break
    fi
    if [ "$waited" -ge "$budget" ] && [ "$alerted" -eq 0 ]; then
      alert_tail=$(tail -3 "$LOGDIR/$id.out" 2>/dev/null | tr '\n' ' ' | cut -c1-200)
      notify "[FinAI2.0 卡滞告警] 块 $id 已超预算 ${budget} 分钟未完成，仍在等完成信号。最近输出：${alert_tail:-（暂无输出）}。不自动中断，继续等待。"
      echo "$(tsay) ⚠ 块 $id 超预算 ${budget} 分钟未完成" >> "$DLOG"
      alerted=1
    fi
  done
done

notify "[FinAI2.0 十二小时任务] 全部任务块执行完毕（含主线与填充池），调度结束。"
echo "$(tsay) dispatcher 全部任务执行完毕" >> "$DLOG"
