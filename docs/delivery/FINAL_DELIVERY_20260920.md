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
- **静态 487 池的幸存者偏差**：池为「连续 3 年 dv≥3%」事后筛选，
  方向性影响已影子量化（§四 R9-A 行）；未做 PIT 年度重选池的
  全量重跑（C3 判负关闭后不再投入）。
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
