# FinAI2.0 任务跟踪文档（跨窗口唯一事实源）

> 创建：2026-09-15 17:45 ｜ 更新：2026-09-17 10:50（⚠️ E4 裁决作废：插桩审计实锤 _pead_holds 幽灵/僵尸在册 bug——登记于 targets 阶段、与实际持仓无同步；已修在册对账≥3bar 无仓即注销，基线 1067 绿，`e4-rebal-fixed` 干净对照重跑中。详见 experiments/lab/e4-risk-audit/REPORT.md 与 PLAYBOOK 修订）｜ 更新规则：每完成一个小任务立即更新对应复选框与本节时间戳
> ⚠️ 旧交接文档 HANDOFF_20260915.md 已过期（MA200 时代），仅作历史追溯，勿作决策依据

## 当前阶段：Alpha 三层（修池子/排雷/PEAD）落地验证期

> 前阶段产物：宽度网格 24 组完成，冠军 `bd25a35m00i1`（D=0.25/A=0.35/m=0/i=1）CAGR 5.82%/MDD 31.28%，为当前进攻基线。 <!-- gate-doc-ignore: 历史快照（宽度网格实验实测值，非基线声明），⛔ 不改史 -->

**进行中**：①修池子（准入质量否决 Q1-Q4）②排雷 overlay（L1-L5）③PEAD 进攻档候选源（扣非 SUE≥80%+DEMAX）。
- 数据：`data/forecast_pit`（10838行）/`statements_pit`（29996行，income+bs 按 end_date 外合并）/`pool_meta`（行业 5903）已采集；`data/quality_veto`（487 只日频）/`landmine_events`（8198 事件）/`pead_signals`（19229 事件/3575 eligible）已构建，幂等原子写。
- 代码：`strategy/signal_layers.py`（builders+SignalLayers 查询）+ `candidates.py` 扩展（veto/cooldown/landmine/PEAD 消费）+ runner 接线 + `scripts/build_signal_layers.py`。
- 已修两个实测坑：PEAD 行业中性化改中位数+sue_adj>0 符号闸（均值口径把 sue_raw=-68 拉到 0.957 分位）；排雷改事件窗口冷却+L4 年报口径+L1b 降 block_only+同日最强动作（旧版造成卖-买-再卖空转，E3 换手 652%）。
- 已出结果（vs 基线 5.82%/31.28%）：E1 veto 单开 4.80%/29.52%；E2b veto+排雷exit版 3.66%/29.05%；E2c veto+排雷block_only版 4.20%/29.28%；E3 全栈event 0.03%/35.07%；E3b 全栈修订 -0.36%/37.86%。 <!-- gate-doc-ignore: 历史快照（Alpha 层消融实验实测值，非基线声明），⛔ 不改史 -->
- 归因裁决：PEAD event 模式双重拖累（40% reserve 进攻日闲置 + 中途建仓追高）→ 改 rebalance 软叠加（调仓日并入候选源、reserve 恒 0、持有到期日频卖出）；排雷除 L1a/L1c 真卖出外全部 block_only。
- **E4 终局（已出但裁决作废）**：rebalance 版 CAGR 4.02%/MDD 37.78% < E2c 4.20% → ~~③PEAD 判负关闭~~ **该成绩系 _pead_holds 幽灵/僵尸在册 bug 下的产物**（排雷卖出→PEAD 注入买回互搏；幽灵票空挂占槽位），非有效检验；已修复并对照重跑 `e4-rebal-fixed`，裁决以重跑为准。 <!-- gate-doc-ignore: 历史快照（消融实验实测值，非基线声明），⛔ 不改史 -->
- **E4 风控审计（已出）**：探针实证详见 `experiments/lab/e4-risk-audit/REPORT.md`；修复=in `_apply_pead` 在册对账；新增 3 单测；先例教训「实现存疑的实验不许升级为路线判负」已入 PLAYBOOK。
- **治理加固（已落）**：新增 `docs/STRATEGY.md`（项目战略宪章：阅读顺序最高层、瓶颈归因优先、裁决分级、数据引入原则、已封顶路线）+ `tests/test_constitution.py`（硬守卫：防护文档缺失/禁止清单缩水即红）；基线 1067→1073。进行中：`e4-rebal-fixed` 重跑 + `utilization-audit` 仓位归因探针（后台并行）。

**目标**：找到 MDD<35% 且 CAGR>0 的宽度参数组合，通过全部六维门禁后晋级为基线。

## 一、任务状态总览

- [x] P0 宽度择时归因诊断（breadth-v1-default MDD 40.46% 根因） <!-- gate-doc-ignore: 历史快照（宽度实验实测值，非基线声明），⛔ 不改史 -->
- [x] P2-a S-2 门禁宽度口径改造（gate_s_scientific.py，向后兼容，旧测试全绿）
- [x] P3 监工诊断类汇报盲区修复（supervisor_loop.sh +check_diag，已重启生效）
- [x] P4 代码指纹加固（_git_code_hash 并入 dirty 文件内容哈希，格式 `<head>+dirty-<12位摘要>`）
- [x] P5-a HANDOFF 文档 4 处历史快照加行内豁免标注
- [x] P6-a HANDOFF_20260915.md 顶部加过期声明（指向本文档）
- [x] P5-b 豁免守卫测试改造完成：改为按文件分组语义守卫（流程图恰好1处、HANDOFF/TASK_TRACKER 历史快照≥1处、其余文件0处、理由须含『历史快照』）；守卫+漂移两测试均转绿
- [x] P5-c 说明书指标漂移修复：核实说明书值（CAGR -0.32%/MDD 39.33%/换手 225.30%/胜率 42.45%）与 9月14日基线产物 20260914-182726 完全一致，t405 测试权威指针已从 9月7日旧产物更新至该产物，9 个测试全绿
- [~] P1 参数网格大任务（🔄 运行中：24 组，finai-breadth-grid，20:15 启动，预计 ~23:45 跑完）
  - ⚠️ 必经坑已修复：mid_cap=0.0 原是『合法配置+运行时必崩』（资金缩放因子误写），c944917 已改为上限语义（0=警戒区目标零仓，存量随 diff 出清），+2 行为测试锁定，m00 组已验证不再崩溃
- [x] P2-b（流水线B阶段已注入，见 commit） 产出侧供给宽度口径 context（run_dividend_backtest.py 的 _build_post_run_gate_context 注入 breadth_series 等字段）
- [x] P2-c（流水线B阶段已新增测试，见 tests/test_breadth_gate_context.py） S-2 宽度口径新增测试用例
- [x] P7 装甲一（已撤销立项：修正后收益 +0.012pp~+0.3pp/年 不抵实施成本，详见设计文档尾部评估）：除权前 15 天禁建仓过滤 + S-4 事前拦截化（数据已核实：487 只 exdiv 全有 date/factor/cash_dividend；调研实测值 5043 元红利税≈净值 +3pp）⛔ 须等网格结束后动 strategy/
- [x] P8 PEAD 简化探路实验（已执行，commit `62e9634`；归母粗阈值不可用实锤，升级裁决=R5 扣非 SUE 口径）⛔ 启动时机见决策记录
  - 📦 数据就绪增强（2026-09-16）：红利池日线已补到 2026-09-16（含官方 circ_mv 市值口径）；新接口另备 `forecast` 业绩预告全市场拉取能力（633条/日，PEAD 原生底座，实测报告 §六.4）
- [x] 收尾：全量回归 1022/1022 全绿 + commit 已固化（61f553d 核心资产 / b78a6b9 诊断治理 / c944917 mid_cap 修复）
- [x] 收尾-2（2026-09-16 窗口）：流水线可信度 12 文件修复 + 回补脚本重写 + 基线 1028 校准 → commit `2434aa6`，回归 1028 passed / 0 failed，FINDING 台账 370 行守住

## 二、P0 诊断结论（数据已验证，与首实验逐位一致）

诊断产物：`experiments/lab/breadth-v1-diagnosis/v1-default/`（DIAGNOSIS_REPORT.md + breadth_daily.jsonl，带三件套）

**MDD 40.46% 根因**： <!-- gate-doc-ignore: 历史快照（宽度实验实测值，非基线声明），⛔ 不改史 -->
1. **主因 = 冰点确认期暴露**：2024-08-22（ice_pending，持 2 仓 10.4 万）→ 08-23 确认清仓前最后持仓日恰逢小微盘流动性危机暴跌，回撤触顶 -40.46%。2 日确认期 + T+1 宽限 = 危机头 2 天满仓扛跌。
2. **警戒区扛跌被排除**：648 个警戒日中仅 53 天持仓≥3，mid_cap 0.50 在调仓日生效正常；但警戒区日均收益 -0.073%（648 天累计阴跌，是 CAGR 为负的慢性贡献）。
3. 档位分布：attack 1236 天 / mid 648 天 / ice_pending 69 天 / ice 268 天；日均收益 attack +0.068%、ice -0.097%。

**P1 网格重点方向**：defense 阈值上移（0.20→0.22-0.25 提前避险）、ice_confirm_days 降至 1（缩短暴露窗口）、mid_cap 降至 0-0.3（切断警戒区阴跌）。

## 三、关键基线数据（权威产物）

- MA200 基线：MDD 21.43%、CAGR 3.76%（权威产物 20260915-235155-t312-dividend-v1-noseed）
- 9月7日产物 20260907-150402：CAGR -3.20%、MDD 43.08%、换手 201.14%、胜率 28.21%（t405 测试当前基准）
- 宽度 v1 默认：CAGR -0.87%、MDD 40.46%、换手 377.66%、胜率 40.85%、164 笔、费 13945.64 元 <!-- gate-doc-ignore: 历史快照（宽度实验产物 20260915-155314 实测值，非基线声明），⛔ 不改史 -->

## 四、测试现状

全量 1018 通过 / 2 失败：
- 失败① test_repo_ignore_usage_is_only_the_historical_snapshot_line：守卫锁死全仓 1 处豁免，新增 4 处 HANDOFF 豁免触发 → 修断言放行
- 失败② test_strategy_description_template_complete：说明书值（-0.32%/39.33%）≠ 9月7日产物的值（-3.20%/43.08%）→ 先核实出处再定改哪边

## 五、环境与进程

- tmux：finai-sentinel（巡检）、finai-supervisor（监工）、finai-breadth-grid（24 组宽度网格，20:15 启动，预计约 23:45 跑完）
- ⚠️ 已知问题（网格结束后修）：_git_code_hash 的 dirty 哈希会吃到 leaderboard.jsonl 的追加（它是跟踪文件），批间哈希标签会漂移（代码本体同为 c944917，结果可比性不受影响）；修复方向=dirty 哈希只覆盖代码路径（strategy/scripts/backtest/tests）
- ⚠️ Hermes 提到的两组旧结果（bd20a35m30i1/i2：CAGR -2.05%/-1.51%、MDD 41%/36%）是修复前旧代码产物，已从榜单剔除、将在新网格重跑，勿引用 <!-- gate-doc-ignore: 修复前旧代码被剔除结果的历史快照登记，非基线声明，不改史 -->
- 服务器：114.67.65.24（本机）；数据源新浪 akshare；腾讯接口对本机封禁待探测
- 测试命令：`cd /home/ubuntu/FinAI2.0 && .venv/bin/pytest <目标> -v`

## 六、决策记录（用户授权 AI 全权执行 2026-09-15 17:43）

| 决策点 | 选择 | 理由 |
|---|---|---|
| 失败①修法 | 更新守卫断言放行 4 处 HANDOFF 历史快照豁免 | 豁免语义正确（不改史），守卫本意是防滥用而非禁历史快照 |
| 失败②修法 | B 方案（核实出处后更新测试权威指针） | 文档已多轮更正且值与当前基线一致，回退旧值是把文档改错 |
| HANDOFF 处置 | 保留+过期声明 | 保留 S-2 违规实验追溯链，全仓库无引用无断链风险 |
| P1 启动时机 | 修复 2 个测试失败并全绿后启动 | 7 小时大任务必须在干净基线上跑，避免产物携带失败状态 |
| mid_cap=0 崩溃处置 | B 方案（停网格→修复→测试→commit→全量同 SHA 重跑） | 保证 G-REPRO-1 指纹一致性，杜绝新旧代码混杂污染产物 |
| mid_cap=0 语义 | 保留为合法配置=『警戒区目标零仓』，存量随调仓 diff 出清 | 符合『上限』语义直觉；资金缩放写法在 0 处与上限语义不等价（0 资金触发守卫必崩） |
| 三层对账·第一层（阈值语义） | 有意探索、非口径错位：代码 defense=19号冰点线、attack=19号进攻线（默认 0.20/0.40 与调研对齐）；网格 defense 0.25 与 attack 0.35/0.45 是主动偏离探测 | 网格本就是参数空间探索；但基线选定后必须把代码阈值↔19号语义的映射写入文档，偏离组合须回调研环登记（保 G-REPRO 可解释性） |
| 三层对账·第二层（PEAD） | Phase D 升级为三选一（红利宽度 / 512890 ETF / PEAD），PEAD 简化探路立项为 P8 | 唯一有独立年化论证（广发11年20%、中泰29.88%）的路径，数据底座已核实。启动时机：网格出结果后——无达标组合则升最高优先，有则作 alpha 增强排下阶段 |
| 三层对账·第三层（装甲一） | 批准立项为 P7，网格结束后第一时间实施 | 成本最低收益最明确的增量（数据在盘上，只差候选过滤+15天前瞻窗口），实测值 5043 元红利税 |
| 网格运行期纪律（20:15~约23:45） | ⛔ 禁止改动代码文件（strategy/scripts/backtest/tests 及回测依赖）；文档/榜单 dirty 漂移属已知问题，网格后统一修哈希口径 | P4 指纹加固后任何 dirty 文件都会改变后续批次 code_hash；experiments/lab/*/ 已 gitignore，可安全落盘 |

## 七、接续指引（新窗口读取本文后）

0. **先读 `docs/ALPHA3_PLAYBOOK.md`**——已裁决证据台账+铁律+决策树，
   与本文件冲突时以 PLAYBOOK 为准
1. 先看『一、任务状态总览』找到第一个未勾选项继续执行
2. 执行前跑 `git status --short` 与 `tmux ls` 确认现场
3. 每完成一项立即回来打勾并更新顶部时间戳
