# T305 · Phase 3 技术评审报告（G4 门禁 · 交用户知会）
<!-- gate-doc-void: date=2026-09-10; reason=引用动量/T304/T305 回测结论，但 experiments/runs/ 无对应机读产物 -->

> ### ⛔ 本文状态：**所引产物不存在 —— 全文回测结论不可采信（2026-09-10 登记）**
> **登记日期**：2026-09-10
> **机器可读作废标记**：本文含 `gate-doc-void` 标记（见本块首行与各命中行行尾）。
> **事实**：本文引用的动量策略 / T304 压测 / T305 评审回测结论，在唯一事实源 `experiments/runs/*.json`（带 `anti_tamper_signature`）中**没有对应机读产物**；T304 为测试现场合成数据，仓内亦无独立压测数据目录。故本文全部回测指标**不构成可核验证据**，原文一字未删，仅作留痕。
> **逐条登记**：见 `docs/audit/void_documents.md`（文件 / 行号 / 应有产物名 / 为何不存在 / 后续动作）。
> **后续动作**：如未来重新考虑动量策略，**必须先重跑并落盘产物**再行评审；在此之前，任何基于本文的结论不得用于准入、报备或对外陈述。

> 生成：2026-09-01（本窗全量实测产物，见附录 B）｜ 评审基线：research-finai `specs/001-a-stock-longonly-daily-quant/`（spec v1.0.0 / plan / tasks）
> ❗ **本文档是 G4 门禁的审议物，请用户拍板后才可启动 Phase 4（模拟盘）——在那之前系统停在这里。**

---

## 1. Phase 0–3 完成度总览（按 spec Phase 顺序复审）

| Phase | 范围 | 状态 | 关键证据 |
|---|---|---|---|
| Phase 0 环境 | T101–T104 | ✅ | 环境清单补验 / 数据字典 v1 |
| Phase 1 数据层 | T105–T110 + G2 | ✅ | 离线单测 162 passed baseline；三源比对/幂等/停牌命中全过 |
| Phase 2 回测引擎 | T201–T207 + G3 | ✅ | 五必挂 17 例逐项 PASS、成本六科目逐项核对、394 单测绿 |
| Phase 3 策略层 | T301–T305 | ✅ | 组合管理器 + 候选策略 + 参数扫描 + 跨区间压力 + 本评审 |
| **离线单测当前基线** | — | **435 passed / exit 0** | `py -3.11 -m pytest tests/ -p no:ddtrace -q`（2026-09-01 复跑） |　（历史时点快照 → 门禁数/基线真值以单一事实源为准）
| **FINDING 台账** | — | **370 行** | 母库只读区未被触碰 |

## 2. 引擎与会计层交付（T201–T207，回测基础设施）

| 交付物 | 内容 | 验证锚点 |
|---|---|---|
| 事件驱动引擎（`backtest/`） | 契约 `T201_design.md` + 9 模块（constants/types/order_fsm/ledger/feed/matching/broker/settle/engine） | 七态状态机 fail-closed + 双账本 tx_hash 幂等 + 先撮合后信号 |
| 五必挂用例（T202） | 涨停/跌停/停牌/除权/T+1 全场景 | `test_t202_must_fail.py` 17 例逐项 PASS |
| 费用模型（T203） | 六科目分段费率（佣金万2.5+¥5最低 / 印花仅卖分段 / 过户双边分段 / 经手沪深·北交分离 / 证管费/滑点） | 07 号黄金算例：10 万往返逐项口径 **112.82 元**（与行业含规费全佣 102.00 对账差 10.82 已归因） |
| 成交模型（T204） | 价格模型注入点（默认次一开盘）+ 滑点 5bps 默认/15bps 压测 + tick 0.01 取整 + 可选涨跌停限幅 | 九宫格敏感度对比 + SVG 曲线 `docs/t204_sensitivity_curve.svg` |
| 指标与回测比对（T205） | CAGR / 年化波动 / MDD 三日期 / 夏普 / 换手 / FIFO 胜率 / 费用明细 / 月度热力 | 黄金数字序列逐一复算 |
| 实验 registry（T206） | run_id=时间戳-git hash-seed + 出处三件套注入 + canonical 参数尺 + 原子写 | FR-REP-2 同参重跑一致用例通过 |

## 3. 策略层交付（T301–T304）

| 交付物 | 落点 | 证据 |
|---|---|---|
| T301 组合管理器 | `strategy/portfolio.py` 三段纯函数（`select_targets → plan_positions → diff_to_orders`），择时空仓是一等公民 | 27 单测绿；PortfolioConfig 全字段 fail-closed |
| T302 候选策略 v1 | `strategy/candidates.py::MomentumStrategy`（动量 lookback=20、周线级调仓 rebalance=5、冷启动 warmup=25、时间退出 max_hold=40） | `test_rebalances_and_reports` 断言零成交即失败——回测链路真实跑通 |
| T303 参数稳健性 | `strategy/param_scan.py::ParamScan`（±20% 扰动四类 int 参数 + 悬崖三判据） | 基准悬崖先炸不巡陪；summary_md 供评审直接引用 |
| T304 跨区间压力 | 离线合成 2015 股灾+熔断（40 天） / 2018 熊市（60 天）— **如实呈现** | `docs/t304_stress_report.md`（见 §4 全文引用） |

## 4. 跨区间压力测试如实结论（**本节不可省略，G4 核心证据**）

**基准参数**：`MomentumConfig(lookback=8, rebalance_days=5, warmup_bars=8, max_holding_days=25)`，初始资金 50 万，两个合成区间各自独立运行：

| 指标 | 2015-crash 段（40 天） | 2018-bear 段（60 天） |
|---|---|---|
| 期末净值 | 158,368.91（自 50 万） | 445,930.59 |
| **总收益** | **−68.33%** | **−10.81%** |
| CAGR（年化） | ≈ −100% | −50.76% |
| **最大回撤** | **68.38%** | **10.81%** <!-- gate-doc-void: date=2026-09-10; reason=引用动量/T304/T305 回测结论，但 experiments/runs/ 无对应机读产物 --> |
| 夏普（R_f=2%） | −13.17 | −6.27 |
| **胜率** | **0.00%** | **0.00%** <!-- gate-doc-void: date=2026-09-10; reason=引用动量/T304/T305 回测结论，但 experiments/runs/ 无对应机读产物 --> |
| 年化换手 | 1,271% | 668% <!-- gate-doc-void: date=2026-09-10; reason=引用动量/T304/T305 回测结论，但 experiments/runs/ 无对应机读产物 --> |
| 费用合计 | 590.43 元 | 818.57 元 |

**病理分析（如实版）**：

- **2015 crash 段**：动量信号在赶顶段（前 9 天 +3%/日）持续给出买入信号，买入即接跳水段（−4.5%/日 → −9.9%/日），时间退出 12 个交易日一次排干 12 轮的亏损——胜率 0% 属必然。趋势类信号在 V 型反转与熔断叠加的市场里没有右侧机会，这不是代码缺陷，是**该信号类在极端市况下的结构性失效**。
- **2018 bear 段**：阴跌市里每次反弹都是逃命波，动量策略永远在反弹之后入场、在下跌中被动减仓，胜率 0%。 <!-- gate-doc-void: date=2026-09-10; reason=引用动量/T304/T305 回测结论，但 experiments/runs/ 无对应机读产物 -->
- **两区间合计 18 笔完整往返，胜率 0%**。如实结论：**当前候选策略（动量+时间退出的骨架）不满足「上模拟盘」的最低门槛**——Phase 3 的工程链路是好的，但策略内容必须更换后再评审。

## 5. 待定/遗留事项（不影响 Phase 3 工程验收，但影响 Phase 4 启动决策）

| 项 | 状态 | 说明 |
|---|---|---|
| **红利税简化** | 🟡 登记 | v1 不建模（T207 评估 ≈0.4%/年上限）；如采用分红/红利风格策略须**先补**该模块 |
| **当前动量候选策略** | 🔴 **不通过** | Phase 4 模拟盘不准用它；进入 Phase 4 前必须替换为结构合理的策略（见 §6 建议） |
| Phase 3 工程层 | ✅ | T301–T304 全部入库且全链可复跑 |

## 6. 对"Phase 4 是否启动"的建议（**仅技术建议，非拍板**）

1. ❌ **不建议**以当前 MomentumStrategy 进入 Phase 4 模拟盘：两个极端样外区间胜率 0%，结构班不符合任何可交易标准（spec §6.1 的"如实呈现"已给出充分否决证据）。
2. ⭐ 建议**先换策略**再评审：考虑低 beta 红利风格（公用事业/银行/煤炭类市值加权）或大盘价值择时（指数 MA200 下方空仓）。任职要求：回测 2015–2024 全期至少包含两段样外，胜率 / 夏普 / 回撤达标。**替换策略后需重新走** T302 迭代一次。
3. ✅ **工程全部就绪**：引擎/费用/指标/registry/组合器都已按四环境同构标准写好并可复现，可以直接承载新策略。

## 7. 附录 A · 证据链（本窗全真实命令）

```
[2026-09-01] pytest tests/ → 435 passed（exit 0）
[2026-09-01] FINDING- 行匹配数 = 370（基线不变）
[2026-09-01] FinAI2.0 HEAD = 539f534 + docs 标记；research-finai HEAD = ffbff48（两仓推送干净）
[2026-09-01] docs/t304_stress_report.md（跨区间如实指标）；docs/t207_g3_gate_acceptance.md（门禁 G3）
```

## 8. 附录 B · 仓库导航（评审后查阅用）

| 读本 | 路径 |
|---|---|
| 回测引擎契约 | `backtest/T201_design.md` |
| 费用模型手册落地 | `backtest/fees.py` + `tests/test_t203_fees.py` |
| 价格模型声明+对比 | `docs/t204_price_model_sensitivity.md` + `docs/t204_sensitivity_curve.svg` |
| 指标 | `backtest/metrics.py` |
| 组合管理器 | `strategy/portfolio.py` |
| 候选策略 | `strategy/candidates.py` |
| 参数扫描 | `docs/t305_technical_review.md` §3 T303 段 + `strategy/param_scan.py` |
| 跨区间压力 | `docs/t304_stress_report.md` |
| G3 门禁 | `docs/t207_g3_gate_acceptance.md` |
| registry | `reporting/registry.py` |
| **当前流程图**（会自己更新到最新） | `docs/project_status_flowchart.md` / `.html` |

---

**本报告到此交给用户审阅——G4 门禁待用户回复「同意进入 Phase 4 模拟盘」或「先换策略再评审」或「停在此」三者之一（或别的决定）。**