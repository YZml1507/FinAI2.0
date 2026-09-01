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
- [x] [T109] 增量更新（日期分区幂等，FR-DATA-6）+ 5 日冒烟 — 2026-08-31 ✅ `data/incremental.py` 实现并入库（IncrementalUpdater：update() 增量续采[last_partition_date 查水位 → last+1 天续采，首次从 2015-01-01 全量] + smoke_test_5d() fail-closed[分区生成/读回非空/无重复日期三查，交易日历可注入离线测]；幂等=同区间重跑 hash_file SHA-256 一致，重叠段按日期去重 keep='last' 吸收）；离线单测 10 绿；commit `c5d75bf`
- [x] [T110] 数据层验收：三源抽样比对 + 停牌命中 100% + 幂等哈希一致（G2） — 2026-08-31 ✅ `data/acceptance.py` 实现并入库（ThreeSourceValidator：validate() 三源抽样比对[FR-DATA-1，阈值 0.2pp，2015 年前不参与]、check_suspension_hit() 停牌命中 100%[FR-DATA-2/R1]、check_idempotency() 幂等哈希一致[FR-DATA-6]）；离线单测 25 绿；commit `2ffbf7b`

## Foundational（P-2 回测引擎）

- [x] [T201] 事件驱动引擎核心：时钟/数据源/撮合/订单状态机抽象（SDD-1~3 回填后实施）（T110） — 2026-09-01 ✅ `backtest/` 引擎核心入库（契约 `backtest/T201_design.md` + constants/types/order_fsm/ledger/feed/matching/broker/settle/engine 九模块：七态状态机迁移表 fail-closed、双账本 Journal append-only[tx_hash 幂等]+BookView 推导、ParquetDailyFeed 停牌=缺席+涨跌停/除权派生+日历注入点、撮合 8 规则按序 fail-closed[停牌/涨停/跌停/T+1/整手/零股/资金/次一开盘成交]、先撮合后信号、settle_day 停牌市值冻结+除权 NAV 无跳变）；离线单测 140 绿；commit `8fca14f`
- [x] [T202] **5 必挂用例测试套件**（涨停买入/跌停卖出/停牌日下单/除权日持仓/T+1 当日买卖）（FR-BT-1~5） — 2026-09-01 ✅ `tests/test_t202_must_fail.py`（17 单测）五用例全过：①涨停买入 REJECTED+NAV 水平线；②跌停卖出 REJECTED+持仓保留；③停牌日 REJECTED+NAV 冻结平直（SETTLE meta 快照核对）；④10送10+派现0.5 → 股数×2/现金+50/成本减半/NAV 无跳变；⑤T+1 当日卖 REJECTED、次日正常成交；commit `8fca14f`
- [x] [T203] 费用模型模块（佣金 5 元最低/印花税分段/过户费/经手费分市场/滑点）（FR-BT-7；07 号核对表） — 2026-09-01 ✅ `backtest/fees.py` 实现并入库（`default_fee_config` 六科目带生效日分段费率唯一登记点 + `compute_fees` 逐项透视[佣金万2.5+¥5最低/印花税仅卖 1‰→0.5‰@2023-08-28/过户费双边 0.02‰→0.01‰@2022-04-29/经手费沪深 0.00487%→0.00341% 与北交所 0.25‰→0.125‰@2023-08-28 独立路径/证管费 0.02‰/滑点 5bps 默认·15bps 压测] + `apply_slippage` + `make_fee_model` 注入 MatchEngine ⇒ 规则 7 资金校验由万三拍数垫切逐项精确费用；金额全 Decimal 逐项 ROUND_HALF_UP 到分；黄金算例：10 万往返逐项口径 112.82 元，与行业含规费全佣 102.00 差=规费双端 10.82；fail-closed：费率限 Decimal/空分段表/未覆盖日期全 raise）；离线单测 33 绿（累计 352）；commit `edd8d2f`。滑点推送成交价属 T204 域；股息红利差别化税登记为 v1 已知简化项（T207 核对期评估）
- [x] [T204] 成交模型默认次一开盘 + 显式声明与敏感度对比（FR-BT-6） — 2026-09-01 ✅ 显式声明与对比报告 `docs/t204_price_model_sensitivity.md` + `backtest/matching.py` 新增 `price_model` 注入点（默认 `bar.open` 次一开盘，T201 契约已预留）+ `backtest/fees.py::make_price_model`（次一开盘 + `apply_slippage` 滑点 + tick 0.01 取整 + 可选涨跌停限幅，13 号红线）；敏感度对比=同一合成策略在 3 价格口径（次一开盘/次一收盘/VWAP 中枢近似）×3 滑点档（0/5/15bps）九宫格终值全出，结论「价格口径是二阶小量、滑点档主导、15bps 压测 −0.16%」；离线单测 18 绿（累计 370）；commit `ccd693c`
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
| TK-5 | 2026-08-31 | **T106/T107 完成并勾选**（commit `5e08574`，FinAI2.0 代码仓）。离线单测累计 **127 passed**（62 原有 + T106×44 + T107×21）。落点：T106 板块档登记表 `BOARD_LIMIT_PCT`(前缀→`LimitFlagsConfig` 字段)+配置覆盖（FR-EXT-6）、除权薄壳走母库既有 kind（adjust_factor+dividend 按年×yearType）、畸形除权日 fail-closed、`exdiv_sources` 血缘=声明非动态推导；T107 `FINANCIAL_TABLES` 唯一登记点、`pit_align` 按 pubDate（⛔非 statDate）PIT 零前视、`collect_financials` (code,pubDate) 保末去重→`FetchResult`。 | FinAI2.0 `git log`/离线 pytest 输出，2026-08-31 |
| TK-6 | 2026-08-31 | **T110 三源验收完成并勾选**（commit `2ffbf7b`，FinAI2.0 代码仓）。离线单测累计 **152 passed**（127 原有 + T110×25）。落点：`ThreeSourceValidator` 三源比对（阈值 0.2pp，2015 年前不参与）+ 停牌命中 100% + 幂等哈希一致（G2 门禁）。**T109 增量更新待实现**（子代理重试中）。 | FinAI2.0 `git log`/离线 pytest 输出，2026-08-31 |
| TK-7 | 2026-08-31 | **T109 增量更新完成并勾选**（commit `c5d75bf`，FinAI2.0 代码仓）。离线单测累计 **162 passed**（152 原有 + T109×10）。落点：`IncrementalUpdater` 增量续采（水位续采 + 首次全量）+ 5 日冒烟 fail-closed。**Phase 1 数据层（T101–T110）至此全部清零，G2 门禁通过**。 | FinAI2.0 `git log`/离线 pytest 输出，2026-08-31 |
| TK-8 | 2026-09-01 | **T201 事件驱动引擎核心 + T202 五必挂用例完成并勾选**（commit `8fca14f`，FinAI2.0 代码仓）。离线单测累计 **319 passed**（162 原有 + T201×140 + T202×17）。落点：SDD-1~3 契约化（`backtest/T201_design.md`）→ 九模块引擎；七态状态机迁移表 fail-closed；append-only 双账本（tx_hash 幂等）+可重算视图；撮合 8 规则按序 fail-closed；先撮合后信号；停牌市值冻结 NAV 平直；除权股数×factor/现金+派现/NAV 无跳变；T+1 可卖校验。五必挂用例全绿。执行方式：DSH 主线程定契约 + 前台串行子代理实现（并行子代理 6/6 死于 API 不稳，串行 5/5 存活）。**T203 费用模型进行中**（`backtest/fees.py` 草稿在库未验证，不提交）。 | FinAI2.0 `git log`/离线 pytest 输出，2026-09-01 |
| TK-9 | 2026-09-01 | **T203 费用模型完成并勾选**（commit `edd8d2f`，FinAI2.0 代码仓）。离线单测累计 **352 passed**（319 原有 + T203×33）。落点：唯一费率登记点 `default_fee_config`（六科目带生效日分段：佣金万2.5+¥5最低 / 印花税仅卖 1‰→0.5‰@2023-08-28 / 过户费双边 0.02‰→0.01‰@2022-04-29 / 经手费沪深 0.00487%→0.00341%·北交所 0.25‰→0.125‰@2023-08-28 **独立路径不可混用** / 证管费 0.02‰ / 滑点 5bps 默认·15bps 压测）；金额全 Decimal 逐项 ROUND_HALF_UP 到分（与券商对账口径一致）；`make_fee_model` 注入 MatchEngine 后规则 7 资金校验由万三拍数垫切逐项精确费用（小单 ¥5 地板效应显著变严）；07 号 §A 黄金算例核对：10 万往返逐项口径 112.82 元，与行业『含规费全佣』102.00 元差=规费双端 10.82 元。显式登记：滑点入成交价属 T204 域；股息红利差别化税为 v1 已知简化项（T207 核对期评估）。 | FinAI2.0 `git log`/离线 pytest 输出，2026-09-01 |
| TK-10 | 2026-09-01 | **T204 成交模型完成并勾选**（commit `ccd693c`，FinAI2.0 代码仓）。离线单测累计 **370 passed**（352 原有 + T204×18）。落点：`matching.py` 新增 `price_model` 注入点（对称 `fee_model`，T201 §7 预留位）；`fees.py::make_price_model` = 次一开盘（`bar.open` 默认）+ `apply_slippage`（BUY 贵/SELL 贱）+ tick 0.01 取整（13 号 tick_size 红线）+ 可选 `limit_pct` 涨跌停限幅（13 号滑点限幅红线：成交价不越涨跌停价）；一字板拒单（规则 2/3）在价格模型**之前**，价格模型带哨兵断言「涨停拒单时不得被调用」。显式声明与敏感度对比报告 `docs/t204_price_model_sensitivity.md`：3 价格口径（次一开盘默认/次一收盘/VWAP 中枢近似）× 3 滑点档（0/5/15bps）九宫格实测，结论=价格口径是二阶小量（极差 0.29 元/12 万盘子）、滑点档主导（15bps 压测 −200 元 ≈ −0.16%）、tick 取整对小滑点有吸收效应；数字由 `TestSensitivityReportFixture` 固化，改口径即红。**T205 绩效指标待启动**。 | FinAI2.0 `git log`/离线 pytest 输出，2026-09-01 |