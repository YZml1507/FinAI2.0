# 最终交付结论书 — FinAI2.0 红利低波+宽度择时系统（2026-09-20）

> 性质：**定稿结论书**。全部数字引自已有机读产物或文档记录，逐条标注
> 「实测回测 / 影子估计 / 外部数字」与出处；无新实验、无重新计算。
> 权威判据单一事实源：门禁数=`scripts/gates/gate_master_audit.py` 注册表；
> 单测基线=`scripts/gates/constants.py::TEST_BASELINE_PASSED`。

## 一、最终基线（实测回测）

**isst-e8b 构型**（红利 dv-top5 市值加权 + 宽度择时 + GC001 现金计息 +
demote 流动性降级），leaderboard `isst-e8b` / 新机复测
`isst-e8b-fix688-v2`（Δ=0，逐项一致——锚点复测坐实新机环境零漂移）。

| 指标 | 值 | 出处 |
|---|---|---|
| CAGR | 8.5814% | leaderboard `isst-e8b`=`isst-e8b-fix688-v2`，Δ=0 <!-- gate-doc-ignore: 历史快照（锚点实测值引用，非新基线声明），⛔ 不改史 --> |
| MDD | 17.3990% | 同上 <!-- gate-doc-ignore: 历史快照（锚点实测值引用，非新基线声明），⛔ 不改史 --> |
| 年化换手 | 4.6074（≈461%/年） | 同上（实测回测） | <!-- gate-doc-ignore: 历史快照（锚点实测值引用，非新基线声明），⛔ 不改史 -->
| 费用总额 | 22,328.60 元 | 同上（实测回测） | <!-- gate-doc-ignore: 历史快照（锚点实测值引用，非新基线声明），⛔ 不改史 -->
| round_trips | 156 | 同上（实测回测） | <!-- gate-doc-ignore: 历史快照（锚点实测值引用，非新基线声明），⛔ 不改史 -->
| 胜率 | 44.23% | 同上（实测回测） | <!-- gate-doc-ignore: 历史快照（锚点实测值引用，非新基线声明），⛔ 不改史 -->
| 期末 NAV | 341,406.53 元（本金 15 万，总收益 +127.57%） | 同上（实测回测） | <!-- gate-doc-ignore: 历史快照（锚点实测值引用，非新基线声明），⛔ 不改史 -->
| 回测区间 | 2015-01-05 ~ 2024-12-31 | run 日志 `开始回测 2015-01-05 ~ 2024-12-31` |
| universe_hash | `ac50e9da4fdf2942`（data_version=`dividend-stocks-2015-2024`） | `experiments/lab/isst-e8b-fix688-v2/runs/*.json` | <!-- gate-doc-ignore: 历史快照（运行期产物路径，不入库），⛔ 不改史 -->

参数全集（`isst-e8b` run_params 标量项，series 字典从略）：

`min_dividend_yield=0.03, candidate_pool_size=50, default_positions=5,
min_positions=5, max_positions=8, weight_mode=market_cap, rebalance_days=20,
warmup_bars=210, timing_breach_buffer=0.01, timing_breach_confirm_days=2,
timing_rebuild_confirm_days=1, use_ma200_timing=False, use_breadth_timing=True,
breadth_defense_threshold=0.25, breadth_attack_threshold=0.35,
breadth_mid_cap=0, breadth_ice_confirm_days=1, breadth_demote_liquidate=True,
breadth_weight_mode=hard, cash_yield_annual=0,
cash_yield_series=data/rates/gc001_daily.parquet, index_symbol=sh.000300,
use_quality_veto=False, use_landmine_overlay=False, use_pead=False,
use_crowding_breaker=False, pead_max_slots=2, pead_hold_days=30,
pead_reserve_pct=0.4, pead_entry_mode=rebalance,
landmine_cooldown_full=120, landmine_cooldown_half=60, dv_skip_top=0,
max_dividend_yield=None, low_vol_keep_pct=None, attack_instrument=""`
（出处：`experiments/lab/*/runs/*.json::params`，实测回测）

## 二、研究方向决策表

| 方向 | 裁决 | 证据（标签=来源） |
|---|---|---|
| C1/C2 线性/连续宽度映射 | 关闭 | e11 16 组 G-2a/b/c 全 FAIL + 退化 2.26pp（实测回测，`E11_LINEAR_PREREG`）；e13a 恒等式分解闭合：暴露 −1.45pp + 形态 −0.81pp，斜坡形态有害（实测回测，`E13A_EXPOSURE_EQUIV_PREREG`） |
| C3 年度重选池 | 判负关闭 | ΔCAGR=+0.66pp 边际不显著、ΔMDD=+7.80pp 劣化（实测回测，`C3_POOL_RESELECT_PREREG` §五/六，臂 `c3-p1-yearly`） |
| MA200 → breadth 择时演化 | 通过并锁定 | e6b GC001 版 7.64%/28.66%（实测回测，leaderboard `e6-cash-yield`/`e6b-gc001`）；e8b 组合 8.58%/17.40% 为最终构型（实测回测，leaderboard `isst-e8b`） <!-- gate-doc-ignore: 历史快照（择时演化链实测值，非基线声明），⛔ 不改史 --> |
| e11-linear | 判负 | 见 C1/C2 行（`E11_LINEAR_PREREG` §七回填） |
| e13a 暴露等价 | 判负（归因完成） | 同上（`E13A_EXPOSURE_EQUIV_PREREG`） |
| e14-bump mid 带持仓 | 影子判负 | mid 带四次独立判负（影子估计，`DEEP_ANALYSIS_GAP_20260919.md` §五） |
| e15-etfattack | **placebo 通过** | ΔCAGR=−5.7633pp ⇒ 选股层显著正贡献实锤（实测回测，`E15_ETFATTACK_PREREG` §六；指数 placebo 仅 2.82%/yr） |
| e16-dvtail 剔尾/上限 | 判负关闭 | 扰动矩阵 6/6 全臂 ΔCAGR<0 且 ΔMDD>0，跨机制稳健（实测回测，`E16_DVTAIL_PREREG` §八） |
| e17 DOF 矩阵 | 5 判负 + pos8 弱正孤峰不晋级 | pos3 −4.57pp / pos8 +1.05pp / rebal10 −2.15pp / rebal40 −3.96pp / eqw −4.42pp / dvw −5.32pp（实测回测，`E17_DOF_PREREG` §六/七） |
| e18 宽度扩展 | 5 臂全判负，pos8 孤峰证伪 | pos10 −2.85pp / floor10 −0.82pp / pos15f10 −4.82pp / pos20f10 −3.90pp / pos20f10eq −6.21pp（实测回测，`E18_WIDTH_PREREG` §五/六） <!-- gate-doc-ignore: 历史快照（e18 消融臂实测值，非基线声明），⛔ 不改史 --> |
| D1 归因 | 关闭 | 因子载体无残差（影子估计，`DEEP_ANALYSIS_GAP_20260919.md`） |
| D2 低波翼 | 判负关闭 | ΔMDD=+9.29pp 且 ΔCAGR=−4.96pp 双轴全劣（实测回测，`D2_LOWVOL_PREREG` §六） |
| D3 分红稳定性 | 影子判负不预登记 | 被剔票 fwd20 无显著差（n=466 gap −0.31pp t=−0.22），剔除率 100% 系数据墙机械全灭（影子估计，TASK_TRACKER `/tmp/d3_probe` 登记） |
| D5 盈利质量 | 影子判负不预登记 | 边缘阴性 t=−1.86 未达 −2 判据（影子估计，TASK_TRACKER `/tmp/d5_probe` 登记） |
| P-1 华泰配方 | 不预登记 | 本仓复现年化 6.26% < e8b 8.58% 锚定失败（影子估计/外部配方，`DEEP_ANALYSIS_GAP_20260919.md` §五 + TASK_TRACKER） <!-- gate-doc-ignore: 历史快照（外部配方复现值，非基线声明），⛔ 不改史 --> |
| PEAD | 方向留档 | 归母口径信号天花板裁决框架已成文（`PEAD_EXPLORATION_DESIGN.md` §四；未实跑晋级实验） |
| D7 / e19 拥挤度熔断 | **判负/不可分辨→关闭** | 四臂收单：t0.85 −0.696pp 判负、t0.80 −0.655pp 判负、t0.90 +0.009pp 不可分辨、A2 +0.017pp 不可分辨；影子改善未兑现（实测回测，`E19_CROWDING_PREREG` §六） <!-- gate-doc-ignore: 历史快照（e19 实验臂实测值，非基线声明），⛔ 不改史 --> |
| 筛选类六连负 | 纪律登记 | 质量 veto/排雷/D3/D5 等六次筛选型叠加均未传递（综合 TASK_TRACKER + GAP 文档） |
| R9-A 幸存者偏差/池构造成本 | 影子量化留档 | 幸存者偏差 2.45pp/年、现池构造成本 4.27pp/年（影子估计，TASK_TRACKER 2026-09-19 03:2x 三臂影子检验登记） |

## 三、容量披露（实测回测）

`capm-{50,100,500,1000}w` × e8b 定稿参数
（`docs/CAPACITY_MATRIX_20260920.md` + leaderboard 同臂记录）：

| 本金档 | CAGR | MDD | 换手 | trips | 费用 | 期末 NAV |
|---|---|---|---|---|---|---|
| 50 万 | 7.58% | 19.78% | 4.88 | 221 | 73,974 | 1,037,374 | <!-- gate-doc-ignore: 历史快照（容量分层实测值引用，非基线声明），⛔ 不改史 --> |
| 100 万 | 7.44% | 19.81% | 4.80 | 248 | 146,657 | 2,048,637 | <!-- gate-doc-ignore: 历史快照（容量分层实测值引用，非基线声明），⛔ 不改史 --> |
| 500 万 | 7.40% | 19.84% | 4.81 | 282 | 734,013 | 10,198,213 | <!-- gate-doc-ignore: 历史快照（容量分层实测值引用，非基线声明），⛔ 不改史 --> |
| 1000 万 | 6.62% | 19.85% | 4.71 | 274 | 1,455,724 | 18,966,093 | <!-- gate-doc-ignore: 历史快照（容量分层实测值引用，非基线声明），⛔ 不改史 --> |

判读（同文档 §三）：**容量上限 ~500 万**；15 万档收益含地板过滤红利
不可外推，对外口径用 50–500 万平台段 **~7.4%**；衰减全部来自成交
约束与组合构成，费用/本金比恒定 ~14.6%（十年）、换手不随规模变化。

## 四、数据缺陷披露

- **688 科创板 volume/amount ×100 单位异构（已修复）**：池内 56/56 只
  688 标的早期段共 49,490 行（其中 24,498 行真实成交额 <5000 万但
  伪过流动性地板）。修复=`scripts/repair_688_unit_fix.py`（腾讯 RAW
  权威源逐行覆盖，原子写回，manifest=`docs/data_repair_688_units_manifest.json`）。
  新机 dry-run 残留 **0 行**。锚点同 SHA 复测逐值 Δ=0（缺陷真实存在
  但本策略未踩中，TASK_TRACKER §⑧ 登记）。
- **isST 语义修正**：namechange 行区间匹配重建，整批同 SHA 重跑 5 组
  逐值一致；`data_hash e150ee29→c7630169` 登记为纯语义修正、度量影响 0
  （TASK_TRACKER 2026-09-19 15:2x，实测回测）。
- **幸存者偏差/静态池成本**：R9-A 三臂影子检验——幸存者偏差
  2.45pp/年、现池构造成本 4.27pp/年（**影子估计**，TASK_TRACKER 登记；
  方向性解读见 §五）。
- **红利税登记日口径 bug**：除权日 FIFO 错配致 fail-closed，修复
  `dbd1b1b`；容量矩阵四档与 e19 全部跑在修复后代码。
- **breadth 文件重建**：新机无 `market-breadth-a` 目录，序列由
  leaderboard `isst-e8b` 记录 `overrides.breadth_series` 逐值重建
  （98 条记录字节一致 + 回读断言相等；`experiments/lab/market-breadth-a/PROVENANCE.md` <!-- gate-doc-ignore: 历史快照（运行期产物路径，不入库），⛔ 不改史 -->
  登记 sha256=`2a220e94…41c00`、2846 行、2015-01-05~2026-09-16）。
  非重新计算，零口径漂移（`E19_CROWDING_PREREG` §六）。

## 五、口径与限制声明

- **PIT**：财务/股息率按公告日对齐、滚动窗口仅用 ≤T 数据；
  宽度/拥挤度序列同约定。
- **执行口径**：T 日决策、T+1 开盘成交；费用六科目逐项 Decimal
  分厘核算 + 滑点；红利税三档 FIFO（<30d 20% / 30–364d 10% /
  ≥365d 5%）实扣入现金与 NAV。
- **静态 487 池的幸存者偏差**：池 = `scripts/collect_dividend_stocks.py`
  `fetch_all_a_stocks`（akshare 采集当日在市全 A 名单）→ `pick_sample`
  等距抽样 500 → 去指数/采集失败 = 487；**无任何股息率/质量过滤**，
  生存条件化来自「采集当日在市」名单（退市票不可能入池）。R9 证据：
  487 与 2024 年 dv≥3% 集合重叠仅 9.74%，池内每年仅 10–20% 满足
  dv≥3%（`docs/audit/r9_pool_spec_and_survivorship_20260919.md`）。
  `data_version=dividend-stocks-2015-2024` 为目录名，非选股语义。
  方向性影响已影子量化（§四 R9-A 行）；PIT 年度重选池全量重跑
  **已做**——即 C3 规则池臂 c3-p1-yearly，幸存者偏差实测披露见 §十。
  <!-- gate-doc-ignore: 历史快照（2026-09-20 更正登记，含引用指标） -->
  （本节原表述「连续 3 年 dv≥3% 事后筛选」经代码核实为误记，
  `pick_sample` 为等距抽样，已于 2026-09-20 更正。）
- **现金计息**：空仓现金按 GC001 真实日利率序列计息
  （`data/rates/gc001_daily.parquet`）。
- **回测≠实盘**：全部为单次全窗历史回测；无实盘、无模拟盘验证；
  成交假设、参与率、停牌处理均为模型化近似。
- **未做的事**：熔断触发计数 `_crowd_break_count` 未持久化（e19
  以 fees/trips 偏离锚点作间接证据）；>1000 万容量外推未实测；
  PEAD 未实跑晋级实验。

## 六、最终结论

**研究面全闭。** e8b 构型为经全维度消融证实的局部最优
（e11~e19 + C 系 + D 系 + P-1 全链判负或不可分辨），最终基线
CAGR 8.58% / MDD 17.40%（实测回测，isst-e8b=fix688-v2 Δ=0）， <!-- gate-doc-ignore: 历史快照（最终基线实测值汇总引用，非新声明），⛔ 不改史 -->
容量口径对外报 50–500 万平台段 ~7.4%。e8b 不晋级 Phase 4 的
历史判据不变；是否重启模拟盘路径属用户决策。
**（2026-09-20 追加：本节中「可进入 T4xx 模拟盘评估」一类表述
已被 §七 e20 样本外裁决取代——OOS 红旗触线，不建议启动模拟盘。）**

**可售交付物清单**：
- 代码仓：`github.com/YZml1507/FinAI2.0`（交付 SHA 见本分支 commit）。
- 数据：Release `data-20260920`，`finai_data_20260920.tar.gz`，
  MD5 `0017521be62787034114ab1f1b63c278`。
- 机读账本：`experiments/lab/leaderboard.jsonl`（全实验矩阵逐臂
  记录，含 overrides/breadth_series 出处指纹）。
- 预登记文档集：`docs/{C3,E7,E11,E13A,E15,E16,E17,E18,E19,D2}_*.md`
  + `PEAD_EXPLORATION_DESIGN.md` + `CAPACITY_MATRIX_20260920.md`
  + `DEEP_ANALYSIS_GAP_20260919.md`。
- 门禁/测试：`scripts/gates/`（门禁数以注册表为准）+
  `tests/`（基线以 `TEST_BASELINE_PASSED` 为准，交付时点 1127 绿）。

**建议的后续**（只列可辩护项）：
- 若用户拍板进入实盘验证，走 T4xx 模拟盘路径
  （`scripts/run_paper_trading_daily.py` 日终执行器 + T402 偏差
  容忍带监控已就绪），先做纸面跟单再评估。
- **不做**新的参数搜索/新信号叠加——全维度消融已证实局部最优，
  再开的任何方向须先过预登记+影子探针双闸。

## 七、样本外留出检验（e20，2026-09-20 追加）

冻结预登记 `docs/E20_OOS_HOLDOUT_PREREG.md`（判据未放松）。两臂同一 e8b
冻结构型，OOS 窗 2025-01-05→2026-09-16（数据端到 2026-09-16）。

| 指标 | e20-oos-2025（cold，2025-01-06 起） | e20-oos-warm（2024-07-01 起，OOS 段 ≥2025-01-05） |
|---|---|---|
| 总收益 | −15.43% | OOS 段 −10.40% |
| CAGR | −9.43% | OOS 段 −6.29% | <!-- gate-doc-ignore: 历史快照（e20 OOS 两臂实测值引用，非锚点口径声明），⛔ 不改史 -->
| MDD | 19.47% | OOS 段 12.69% | <!-- gate-doc-ignore: 历史快照（e20 OOS 两臂实测值引用，非锚点口径声明），⛔ 不改史 -->
| 年化换手 | 2.96 | 4.41（全段） |
| round_trips | 19 | 28（全段） |
| 空仓日占比 | 83.1% | 69.4% |

 <!-- gate-doc-ignore: 历史快照（e20 两臂 run artifact 实测值汇总引用，非锚点口径声明），⛔ 不改史 -->

基准（外部/估算）：512890 同窗价格收益 +9.48%（分红未取全→低估）；
510300 +18.09%（价格）/ +23.82%（含分红估算）。
 <!-- gate-doc-ignore: 历史快照（外部行情实测/估算基准值，非本项目回测声明），⛔ 不改史 -->

**红旗判定**（PREREG §六/§九.5）：R1 未触；**R2 触线**（cold −24.91pp /
warm −19.88pp vs 512890）；R3 未触；**R4 触线**（post-warmup 口径
65.5% / 50.0%，仅披露不翻案）。

**裁决**（PREREG §九.6 verbatim）：R2+R4 同触 ⇒ 登记「因子层跑输 +
择时层失效红旗」，**T4xx 模拟盘不建议启动**；2025-01-05 之后区间永久
保留为 OOS。机制解释：cold 臂 warmup_bars=210 自 start 起算 ⇒ 真实
持仓暴露仅 2025-11-19→2026-09-11（70 持仓日），实际持仓段 2026-03~05
连续三月 −2.6%/−5.6%/−9.0% 回撤而同期基准为正——「样本内最优 ≠
可交易」的直接证据。 <!-- gate-doc-ignore: 历史快照（OOS 裁决与月度实测值引用，非新声明），⛔ 不改史 -->

**结论修订**：在此前证据下**不建议进入模拟盘**。数据面 GC001 已续至
2026-09-18（append-only、重叠 242/242、锚点行恒等），引擎修复一处
（`_load_index_frame` 年度分区硬编码→全分区 glob，锚点区间不受影响）。

## 八、复现路径与数据 Release（2026-09-20 追加）

**数据包**：
- `data-20260920`：`finai_data_20260920.tar.gz`，MD5 `0017521be62787034114ab1f1b63c278`（原始交付包）。
- `data-20260920b`：`finai_data_20260920b.tar.gz`，MD5 `db22da0cb6ae750341c921ddf65234cd`，SHA256 `269b209b1eb2a2c68dee69dce95c97db2a19c896c46cd1c5957bea132abb2334`。 <!-- gate-doc-ignore: 历史快照（发布包校验值快照，非指标声明），⛔ 不改史 -->
  增量 vs a 版：GC001 续至 2026-09-18（腾讯 sh204001 append-only + manifest 出处）、
  ETF 510300/512890 2025/2026 分区（价格收益口径）。

**复现入口**：`scripts/repro/reproduce_final_delivery.sh`（薄封装）/
`reproduce_final_delivery.py`——前置检查（数据恢复/宽度序列在则跳过重建、
GC001 覆盖 ≥2026-09-16、leaderboard 在位，fail-closed）→ 并行复跑三臂
（`*-repro` 命名，不覆盖权威记录）→ 与 leaderboard 冻结期望**容差 0** 比对，
打印 PASS/FAIL 表，任一不符 exit 1。支持 `--only anchor|cold|warm`、
`--dry-run`（只打印命令）、`--serial`。

**期望输出**（逐字比对，取 leaderboard `isst-e8b-fix688-v2` /
`e20-oos-2025` / `e20-oos-warm` 记录）：

| 臂 | 期望（容差 0） |
|---|---|
| anchor | CAGR 8.5814% / MDD 17.3990% / RT 156 / fees 22328.60 / 换手 4.6074 | <!-- gate-doc-ignore: 历史快照（复现期望值引用权威记录，非新声明），⛔ 不改史 -->
| cold | 总收益 −15.43% / MDD 19.47% / RT 19 | <!-- gate-doc-ignore: 历史快照（复现期望值引用权威记录，非新声明），⛔ 不改史 -->
| warm | 总收益 −9.54% / MDD 12.69% / RT 28 | <!-- gate-doc-ignore: 历史快照（复现期望值引用权威记录，非新声明），⛔ 不改史 -->

## 九、e22 立项评估（未发车，2026-09-20）

- **动机**：§五 幸存者偏差条目；探索目标 = PIT 年度重选「原池规则」。
- **发现**：原池无规则可复刻（`pick_sample` 等距抽样），故「PIT 化原规则」
  命题不成立；本节 §五 旧表述已更正（见上）。
- **备选「cont3 池规格 PIT 版」precheck**（`experiments/lab/e22-precheck/`，
  未提交）：2015–2017 因 `daily_basic_alla` 自 2015 起、无前 3 年年末快照
  ⇒ 空池；2018–2024 逐年入选 14/27/31/75/86/124/139（dv_ratio 口径），
  85% 在 `c3_universe` 692 数据面内，缺口 52 只（仅 1 只退市）；PIT 并集
  215 只，与 487 重叠仅 23 只。
  <!-- gate-doc-ignore: 历史快照（e22 precheck 实测计数，非回测指标） -->
- **裁决：不发车**。理由：① 它测的是新池规格而非原池的幸存者偏差；
  ② R9 已评 cont3 为多年度规格中近似全收益最差之一（7.76%）、C3 PIT
  年度池已实测 ΔMDD +7.80pp 越红线；③ 2015–2017 空池使可比窗仅 7 年；
  ④ 任何新池规格 = 新方向，须过预登记+影子探针双闸，且当前研究面全闭
  结论不支持继续投入。
  <!-- gate-doc-ignore: 历史快照（引用 R9/C3 已登记实测值） -->
- **幸存者偏差量化维持 R9-A 影子估计**（−2.45pp/年，未扣成本）作为披露口径。
  <!-- gate-doc-ignore: 历史快照（R9-A 影子估计引用） -->
- **数据面事实登记**：`c3_universe` 已含 7 只窗内退市票；`delisted_bars`/
  `daily_bars` 全市场 bars 不在 Release 内（旧机产物）。⛔ 本节原判
  「退市票补采为去偏前置」已被 `experiments/lab/e22b-precheck/c3_gap.md`
  盘点否定——C3 管线已含退市票，详见 §十。

## 十、池依赖披露：PIT 规则池对照（含去偏，不可分离幸存者单项）（2026-09-20）

- **(a) 487 池不可去偏**：池为 `pick_sample` 等距抽样（§五更正），无规则
  可复刻 ⇒ 「PIT 化原池」命题不成立，补采退市票也无法生成对照臂。
- **(b) 去偏参照已存在**：`c3-p1-yearly`（run 20260919-162125，leaderboard
  在册）——规则池（R1–R7，年首快照 PIT，候选集=daily_basic_alla 全 A 日
  截面，含 7 只窗内退市成员）、**同 params_hash 0adb13ff、同 calendar_hash**
  ⇒ 与 isst-e8b-fix688-v2 的差值=池重选合成效应
  （规则筛选+PIT 年度重选+去除幸存者条件化，三者不可分离）：CAGR 9.2422% vs 8.5814%
  （**+0.66pp**）、MDD 25.2032% vs 17.3990%（**+7.80pp**）、换手 4.8980 /
  RT 183。早年池仅 21–49 只，集中度风险已在 2015–16 实变现。
  <!-- gate-doc-ignore: 历史快照（leaderboard 实测记录引用） -->
- **(c) 解读**：红利低波方向在干净全市场规则池上**收益不消失但回撤显著
  劣化**——e8b 的 17.40% MDD 是「487 抽样池 × 现行规则」组合的池依赖样本特性，不可作为
  方向性 MDD 预期。对外披露口径改为：「CAGR 8.6–9.2% / MDD 17–25%
  （池依赖）」。 <!-- gate-doc-ignore: 历史快照（池依赖区间披露口径） -->
- **(d) 口径层级**：R9-A 影子 −2.45pp/年 是**唯一**幸存者偏差专项估计
  （等权年度近似，未扣成本、未含择时）；本节 (b) 的引擎实测差值为
  池重选合成效应，不能归因于幸存者偏差单项。
  <!-- gate-doc-ignore: 历史快照（R9-A 影子估计引用） -->
- **(e) e22b 评估=不发车**：数据面缺口=0（`experiments/lab/e22b-precheck/
  c3_gap.md`，运行期产物不入库）——692 物化并集已覆盖 C3 池全部成员，
  退市票仅截断于退市前整理期（属正常间隙）；255 只退市票补采取消，理由
  同 (a)：只服务于无规则可复刻的 487 池，不可能去偏。
