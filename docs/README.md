# FinAI2.0 · 文档导航索引

> 更新：2026-09-17 ｜ 状态：Alpha 三层战役收尾 / 仓位管理层修复期
> **读取顺序（后继模型/新窗口必读）：**
> **① `STRATEGY.md`（战略宪章）→ ② `ALPHA3_PLAYBOOK.md`（战役纪律与
> 证据台账）→ ③ `TASK_TRACKER.md`（任务事实源）→ ④ 本索引。**
> 本索引只做导航，不登记指标；指标真值在 TASK_TRACKER 与实验产物。

## 一、治理与当前状态（有效，先读）

| 文档 | 作用 |
|---|---|
| `STRATEGY.md` | 项目宪章：定位/边界、瓶颈归因优先、裁决分级、数据引入原则、已封顶路线 |
| `ALPHA3_PLAYBOOK.md` | Alpha 战役手册：已证负清单、操作铁律、实验流程、决策树 |
| `TASK_TRACKER.md` | 任务级事实源（过程进度、当前阶段记录） |
| `PROJECT_ASSESSMENT.md` | 基准归因与市场定位评估（2026-09-17） |
| `CLAUDE.md`（根目录） | 工程约束总纲 |

## 二、当前阶段工作文档（有效）

| 文档 | 说明 |
|---|---|
| `ARMOR1_EXDIV_FILTER_DESIGN.md` | 除权过滤设计（现行） |
| `gap_slippage_model.md` | 缺口滑点模型（现行） |
| `high_price_exclusion.md` | 高价股过滤（现行） |
| `TARGET_12H.md` | 十二小时流水线目标（进行中） |
| `docs/spec/`、`docs/engineering/`、`docs/diagnosis/`、`docs/compliance/`、`docs/paper_trading/` | 各域现行文档 |

## 三、历史任务文档（已完结，仅供追溯，⛔ 勿作当前决策依据）

- T2xx~T4xx 任务系列：`t204_*`/`t304_*`/`t305_*`/`t309_*`/`T310_*`/
  `t311_*`/`t312_*`/`t313_*`/`t401_*`/`t402_*`/`T403_*`/`t404_*`
- 阶段总结：`task_completion_summary.md`、`bias_audit_report.md`、
  `phase35_recommendation.md`、`phase36_data_source_recon.md`、
  `momentum_backtest_summary.md`（⛔ 已作废，机读标记 gate-doc-void）
- 设计与探索：`PEAD_EXPLORATION_DESIGN.md`（PEAD 路线已终局判负，
  仅留档）、`t204_price_model_sensitivity.md`
- `docs/delivery/`、`docs/audit/`：交付与作废文档归档区

## 四、已过期文档（⛔ 明确不可用作决策依据）

| 文档 | 状态 |
|---|---|
| `HANDOFF_20260915.md` | 已过期（MA200 时代交接），顶部有声明 |
| `ROADMAP.md` | 已过期（2026-09-16 MA200/门禁时代路线图），当前路线以 STRATEGY.md 为准 |
| `project_status_flowchart.*` | 历史流程图，状态以 TASK_TRACKER 为准 |

## 五、实验与审计产物（数据层导航）

- `experiments/lab/`：全部实验目录，`leaderboard.jsonl` 为结果事实源；
- `experiments/lab/e4-risk-audit/`：E4 风控审计（探针+报告，PEAD 判负证据链）；
- `experiments/lab/utilization-audit/`：仓位利用率归因探针；
- `data/etf_bars/`：ETF 日线（含防御资产候选池，meta_defensive.json）；
- `data/macro/`：宏观/基准数据（index_000300、benchmark_510880 等）。

> ⛔ 纪律：新增文档若引用实测指标，须与权威产物一致或加行内
> `gate-doc-ignore: 历史快照…` 豁免（门禁 G-DOC-1 强制）。
