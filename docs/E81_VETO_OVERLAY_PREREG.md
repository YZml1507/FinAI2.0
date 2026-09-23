# E81 预登记 —— e37 否决层并入 top40 分数篮（veto overlay）

> 前置条件已满足：e37 否决层当初在红利池载体上判负（池内命中率低），
> tracker 登记的宽域前置=「全 A 多头基座晋级件出现」——e63/top40
> 构型（052313 gated: 32.42%/0.2291）即该基座。本臂测 veto 对全 A
> 分数篮的增量。

## 1. 假设与机制

e37 veto_daily（V1 户数激增 + V2 重复上榜，~300 只/日禁买）剔除拥挤
风险标的 → MDD 改善为主，CAGR 允许小幅代价。
机制：**买侧否决**——调仓日 veto 集内代码不得入选；从分数降序
回填补足 target_count；已持仓不强制卖出（与 e37 原口径一致，
否决=禁买非强平）。

## 2. 臂设计

| 臂 | 配置 | 说明 |
|---|---|---|
| V-base | 现行基线逐位复跑 | 对照（scores_label150x top40 eq reb60 amt5M no-timing ¥3M） |
| V-on | 基线 + veto_daily.parquet 注入 | 唯一差异：买侧否决 |

## 3. 判据（判强线）

- **判强**：ΔMDD ≤ −3pp 且 ΔCAGR ≥ −1pp；或 ΔCAGR ≥ +1pp
- **判弱**：ΔMDD 不足 −1pp 且无 CAGR 改善 → 登记关闭（veto 对分数篮
  无增量，e37 轴全闭）
- 附加诊断：veto 命中率（篮内候选被否决占比）、换手 Δ

## 4. PIT 与实现

- veto_daily.parquet 每行 (date, symbols[]) 为**当日可见**禁买名单
  （e37_veto_series 已按披露日对齐）；策略仅取 `date == 当日` 一行。
- 数据覆盖 2015→2024-12-31 = 回测窗全域；2025+ 延伸段另有
  veto_daily_2025plus 但本臂窗口只用主序列。
- 实现：ScoreBasketStrategy 增 `veto_series: Mapping[date, frozenset]`
  注入位（默认 None=关闭，锚点逐位不变）；`run_score_basket_backtest
  --veto-path` 装载。
- 测试：veto 排除命中代码 + 回填补位 + 缺省关闭三条单测。
- gated run：`--registry-root experiments` 走 29 门。

## 5. 流程门

本文件 commit 后再 --run；结果登记 docs/TASK_TRACKER.md +
RESULT 简表。
