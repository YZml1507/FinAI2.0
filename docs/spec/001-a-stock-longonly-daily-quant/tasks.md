# Tasks · A股中低频量化交易系统 v1（依赖序）

**Feature**: specs/001-a-stock-longonly-daily-quant
**Version**: 1.0.0（与 spec v1.0.0 同步定稿）
**Date**: 2026-08-29
**依据**: constitution v1.0.0 + spec v1.0.0（SDD-1~7）+ plan v1.0.0
**执行规则**（spec-kit lean implement 语义）：按依赖序逐条执行；完成后把 `- [ ]` 改为 `- [x]`；失败即停并报告；每批完成后做章程符合性检查（constitution 治理节）。

> 括号内为依赖项。批量标注：【用户确认】的任务必须在用户拍板后启动。

## Setup（P-M0）

- [x] [T001] 确定告警通道（FR-OPS-3；用既有渠道，不注册新账号）【用户确认】→ 写 `.specify/memory` 备查 — 2026-08-31 ✅ 用户拍板**飞书**（经 hermes_orchestrator MCP），见 `.specify/memory/alert_channel.md`
- [ ] [T002] 项目骨架：`git init` + `.gitignore`（venv/密钥/缓存/私有数据）+ README 段落
- [ ] [T003] 环境变量方案：密钥与账户信息改环境变量；扫描仓库无明文（FR-OPS-4）
- [ ] [T004] 六包骨架：`data/ backtest/ strategy/ accounting/ reporting/ ops/`（模块占位 + 导入冒烟）

## Foundational（P-0、P-1 数据层）

- [x] [T101] 工作站环境清单照 12 号附录 A 再过一遍（Python 3.11/venv/代理）（P-0.1） — 2026-08-31 ✅ 本机复验全绿：Python 3.11.5、pyarrow 25.0.0/pandas 2.2.2/baostock 0.9.2/numpy 2.4.6 齐备、代理 127.0.0.1:7897 走通（curl baidu HTTP 200）、baostock login success + 2026-08-31 为交易日、akshare 1.18.64 修复 bs4/tqdm 依赖后新浪/腾讯校验源连通、东财 stock_zh_a_hist 实测不可达（ConnectionError，符合 A.5.1→主源走 baostock）。环境缺口（akshare 缺 bs4/tqdm）已补装。
- [x] [T102] 东财 push2his 可达性复测（附录 A.5 探测 ×3）（P-0.2）→ 结论写回 12 号附录 A.5 修订日志 — 2026-08-31 ✅ 同项已由 R3 复验关闭（REVALIDATE.md §R3：efinance 37 接口 36 OK、push2his 直连 HTTP 200、12 号 A.5 探测 3 次 ProxyError 属代理噪音），结论已写回 12 号附录 A.5（R-12-11/第四轮修订日志）
- [x] [T103] 复现 12 号 §9-A 四项实测核心结论（停牌脏行/复权一致性/公告日/增量冒烟）（P-0.3） — 2026-08-31 ✅ 经 R1（停牌脏行，`_drop_suspended`+test_baostock_suspension×5）/ R4（复权一致性，adjustment_mode.py+test_adjustment_mode×6）/ 12 号 §9-A 公告日与增量冒烟复现（19 离线单测绿），T104 字典 §1.1/§2.1/§9 已含实测结论
- [x] [T104] 数据字典 v1：字段清单 + 复权语义 + 可得日期 + pubDate 对齐规则（FR-DATA-1~5） — 2026-08-31 ✅ v1.0.0 落盘本目录 `data_dictionary_v1.md`；含 2026-08-31 本机实测（1990 年段 `turn`/`isST` 可用、R1 停牌脏行三组实证）
- [x] [T105] 日线采集器（baostock 主；新浪/腾讯校验；限速四件套）（FR-DATA-7；T101/T104） — 2026-08-31 ✅ `data/collector.py` 实现并入库（DailyCollector 编排 + RateLimiter/CircuitBreaker/指数退避限速四件套 + 幂等分区 Parquet 落盘 + 新浪/腾讯校验腿）；离线单测绿；commit `8282cd4`
- [x] [T106] 停牌/涨跌停/除权清洗入库（FR-DATA-2） — 2026-08-31 ✅ `data/cleaner.py` 重实现并入库（enforce_tradestatus 只过滤不填充[R1] + assert_no_prevfill_suspicious 前收平推签名检测；mark_limit_flags 板块档登记表 BOARD_LIMIT_PCT(前缀→配置字段)+LimitFlagsConfig 覆盖[FR-EXT-6]，主板±10%/创业板30xx/科创板68xx±20%/ST±5%/首日无涨跌停，eps 只吸浮点噪声不改档位归属；fetch_exdiv_events 薄壳走母库 baostock_source.fetch 既有 kind，EMPTY_OK=无事件/FAIL_* 即 raise/畸形除权日 fail-closed；clean_daily_bars 编排+CleanResult 计数自检）；离线单测 44 绿；commit `5e08574`
- [x] [T107] 财务 pubDate 对齐管道（FR-DATA-4） — 2026-08-31 ✅ `data/financial_pit.py` 重实现并入库（_query_financial_once 登录→查询→_drain→登出复用母库原语+universe 同形，run_with_timeout 挂死保护；FINANCIAL_TABLES 表→baostock 接口映射唯一登记点，未登记即 raise；pit_align 纯函数按 pubDate[⛔非 statDate] PIT 对齐零前视；collect_financials 批量 (code,pubDate) 保末去重 → FetchResult，source 进 meta 血缘）；离线单测 21 绿；commit `5e08574`
- [x] [T108] 股票池/成分回放（FR-DATA-5；T104） — 2026-08-31 ✅ `data/universe.py` 实现并入库（`alive_universe` 纯函数：ipoDate/outDate/type=='1'，⛔禁用 status 列防幸存者偏差；指数成分回放默认拒绝未验证 hs300/zz500/sz50，zz1000 无接口抛 IndexNotReplayableError）；离线单测绿；commit `8282cd4`
- [ ] [T109] 增量更新（日期分区幂等，FR-DATA-6）+ 5 日冒烟
- [ ] [T110] 数据层验收：三源抽样比对 + 停牌命中 100% + 幂等哈希一致（G2）

## Foundational（P-2 回测引擎）

- [ ] [T201] 事件驱动引擎核心：时钟/数据源/撮合/订单状态机抽象（SDD-1~3 回填后实施）（T110）
- [ ] [T202] **5 必挂用例测试套件**（涨停买入/跌停卖出/停牌日下单/除权日持仓/T+1 当日买卖）（FR-BT-1~5）
- [ ] [T203] 费用模型模块（佣金 5 元最低/印花税分段/过户费/经手费分市场/滑点）（FR-BT-7；07 号核对表）
- [ ] [T204] 成交模型默认次一开盘 + 显式声明与敏感度对比（FR-BT-6）
- [ ] [T205] 绩效与风控指标模块（收益/波动/回撤/夏普/换手/费用/胜率/月度热力图）（FR-REP-1）
- [ ] [T206] 实验 registry（参数/代码版本/数据版本/指标/时间戳）（FR-REP-2）
- [ ] [T207] 门禁：5 必挂用例全绿 + 成本逐项核对通过（G3）

## Story（P-3 策略与组合）

- [ ] [T301] 组合管理器：3-8 只/默认 5/硬上限 10/单票 ≥2 万/流动性过滤（FR-PM-1/2/4）
- [ ] [T302] 候选策略 v1（日线中低频；如多因子/动量组合）
- [ ] [T303] 参数稳健性 ±20% 邻域扫描（spec §6.1）
- [ ] [T304] 跨区间压力：2015 股灾+熔断 / 2018 熊市样外如实呈现（spec §6.1）
- [ ] [T305] 进入模拟盘前技术评审汇报（G4）→ **生成报告交用户知会**

## Story（P-4 模拟盘 · ≥6 个月）

- [ ] [T401] 模拟盘执行器（信号 → 模拟成交；复用回测抽象）（P-4.1）
- [ ] [T402] 回测-模拟偏差容忍带量化（P-4.2）
- [ ] [T403] 日终任务：对账/净值/报告（FR-ACC-2/3，FR-REP-1；P-4.3）
- [ ] [T404] 台账保鲜与到期提醒纳入调度（FR-REP-3；14 号 SOP）
- [ ] [T405] 报备材料清单核对（FR-COMP-1；11 号；模拟盘期间准备齐）（P-4.5）
- [ ] [T406] 模拟盘运行 ≥6 个月 + 偏差在容忍带内（G5 前半）

## Story（P-5 小额实盘）

- [ ] [T501] 用户书面确认实盘（券商/金额/日期）【用户确认】（G5 后半；P-5.0）
- [ ] [T502] 券商开通 + 报备提交（先报告后交易，FR-COMP-1/2）
- [ ] [T503] 实盘桥（复用回测同构抽象，SDD-1） + 实盘对账（P-5.2）
- [ ] [T504] 最小可验证资金首周运行 + 首日对账零未解释差异（G6）
- [ ] [T505] 用户确认后加仓（每次升级均需确认）（P-5.3）

## Polish（P-6 运营迭代）

- [ ] [T601] 月度台账复查例行（2026-09-28 首轮，14 号 SOP）
- [ ] [T602] 季度台账复查例行（2026-11-28 首轮）
- [ ] [T603] 策略迭代流程文档化（研究→回测→模拟→实盘小步验证，constitution 原则八）
- [ ] [T604] 复盘与文档修订纪律例行（修订日志登记）
- [ ] [T605] 多市场扩展评估触发机制（用户决策时启动调研；FR-EXT 六留口检查）

## 关卡说明

- **G0**：本 tasks 与 spec/plan 齐备 → **停下征得用户同意**（2026-08-29 用户指令；constitution 原则 VIII 第二节）。
- 每批任务结束执行"章程符合性检查"（对照十项原则逐条勾选）。
- 所有 `- [ ]` 在完成时改 `[x]` 并留痕（日期）。

## 修订日志

| 编号 | 日期 | 内容 | 依据 |
|---|---|---|---|
| TK-1 | 2026-08-29 | v0.1 草案（依赖序任务清单） | constitution + spec v0.1 + plan v0.1 |
| TK-2 | 2026-08-29 | **v1.0.0 定稿**：T201 明确按 SDD-1~3 落地（Broker 接口 / TimeSource / 七态状态机 / append-only 双账本）；T401 复用 SDD-1 PaperBroker；新增 T110 验收含"SDD-5 实验 registry 一次性"；T603 策略迭代流程按 SDD-3 全事件驱动单引擎；T605 扩市场触发机制按 SDD-7（韩股反向 ETF 视普通多头 / 美股留口低成本档） | spec v1.0.0（SDD-1~7），检索日 2026-08-29 |
| TK-3 | 2026-08-31 | **T101 环境清单补验通过**：Phase 0（T101–T104）至此全部清零。本机复验 12 号附录 A 各项全绿——Python 3.11.5、pyarrow 25.0.0/pandas 2.2.2/baostock 0.9.2/numpy 2.4.6、代理 7897 走通、baostock login 成功（2026-08-31=交易日）、akshare 1.18.64 修复 bs4/tqdm 依赖后新浪/腾讯校验源连通、东财 stock_zh_a_hist 实测不可达（符合 A.5.1→主源走 baostock）。**Phase 1 数据层（T105–T110）全部解锁**。 | 12 号附录 A（A.1–A.5）逐项复验，本机第一手实测，2026-08-31 |
| TK-4 | 2026-08-31 | **T105 日线采集器 + T108 股票池/成分回放完成并勾选**（commit `8282cd4`，FinAI2.0 代码仓）。离线单测累计 **62 passed**（19 原有 + T105×28 + T108×15），全程零网络调用。技术口径锁死：Parquet 落盘 `data/daily_bars/{symbol}/{year}.parquet`；baostock 复权只经 `to_kwargs(mode,"baostock")` 映射（⛔禁手写字面量）；R1 停牌滤 `tradestatus=='1'`+记 `meta['suspended_rows']`。⛔ T106/T107 上批 workflow 子代理中途死亡（worktree 空），**本次未勾选**，另起子代理重实现。 | FinAI2.0 `git log`/离线 pytest 输出，2026-08-31 |