# GATE-R4 第二轮对抗复验报告（QA 严过关 / software-qa-engineer）

> 任务卡：**GATE-R4**（任务列表 #4，SOP 上限轮）
> 对象：工程师 GATE-R3 交付——「修 L-2 残留 + runner 合成 PASS + 4 处新漏网」，自称 `IS_PASS: YES`
> 立场：对抗性证伪（独立重跑，不采信工程师表格）。只读实现 + 只改测试；**未修改任何门禁实现代码**（mutation 全程字节级备份还原，`git diff --stat` 复原一致）。
> 环境：`export PYTHONPATH="C:/Users/MengLin/AppData/Roaming/Python/Python311/site-packages"`；`py -3.11 … -p no:ddtrace -p no:ddtrace.pytest_bdd`
> ⚠ 本轮 shell stdout 被吞，所有命令输出经临时文件落盘后读取；报告内命令可直接复现。

---

## 0. 结论

**结论：PASS（R3 目标全部达成、经对抗复验无回归）；遗留 5 项如实登记（其中 A-3 为 R2 已登记、本轮未修的真实假通过）。**

**智能路由判定：NoOne（本轮修复通过）**；若要清零遗留，建议 A-3 单开一张 Engineer 小卡（见 §7）。

依据：R2 被攻破的**全部反例已封堵**；runner 两处合成 PASS **已移除**并改为按 `FAIL>INCONCLUSIVE>WARNING>SKIP>PASS` 上抛本体判定（真实入口实测）；4 处漏网已封；26 个新测试中 18 个负向用例经 mutation 必红（其余为正向对照）；回归全绿；**未发现新增同类假通过**。

---

## 1. 回归与基线（全绿，无异常）

| 项 | 命令 | 实测 |
|---|---|---|
| 全量单测 | `py -3.11 -m pytest tests -q …` | **978 passed**（74.15s）|
| 收集数=基线 | `py -3.11 -m pytest tests --collect-only -q -p no:ddtrace` | **978 == `TEST_BASELINE_PASSED`(978)** |
| CI 门禁 | `py -3.11 -m scripts.gates.gate_master_audit --ci` | `[CI][PASS]` exit 0；PASS 13 / FAIL 1(G-MDD-1→WARN) / SKIP 1 / INCONCLUSIVE 14 |
| pre-push | `py -3.11 -m scripts.hooks.pre_push` | `[ALL PASS]` exit 0（回归 978 ≥ 基线 978）|

SKIP 仍为 1（D-4，与 R2 同），未新增 SKIP 逃逸。

---

## 2. 攻击点 1：重放 R2 全部反例（须已封堵）——**全部封堵 ✓**

复现：`py -3.11 C:/Users/MengLin/AppData/Local/Temp/g4_probe_l2.py`（输出 `g4_l2_out.txt`）

| R2 反例 | R2 实测 | R4 实测 | 判定 |
|---|---|---|---|
| n=10 恰 1 只完全未建仓 | PASS（TV=0.1000 边界漏网） | **FAIL**（计数判据：检出 1 只完全未建仓） | ✅ 封堵 |
| n=11 恰 1 只漏建 | PASS | **FAIL** | ✅ |
| n=20 / n=50（5 只漏建） | PASS | **FAIL** | ✅ |
| 目标全 NaN | PASS | **INCONCLUSIVE** | ✅ |
| 目标全 inf | PASS | **INCONCLUSIVE** | ✅ |
| 实际含 NaN | PASS | **FAIL** | ✅ |
| 实际含 inf | PASS | **FAIL** | ✅ |
| 非等权目标 + 实际含 NaN | PASS（见 §6 新增证据） | **FAIL** | ✅ |
| 实际 inf 单只其余 0 | PASS | **FAIL** | ✅ |
| n=3 40/40/20（R2 误杀争议） | FAIL（误杀） | **PASS**（r=[1.2,1.2,0.6] 在带内） | ✅ 误杀解除 |
| n=3 45/45/10 | FAIL | **FAIL**（r=[1.35,1.35,0.3]） | ✅ |
| 边界：负值/全 0/Σw≤0/单标的/长度不匹配/空 dict | 正确 | **正确**（FAIL/INCONCLUSIVE 不变） | ✅ |

> 修正说明：R2 报告中「n=10 恰 1 只漏建旧判据 `TV > 0.10` 为 False ⇒ 假 PASS」——R4 复现一致；R3 已用**计数判据 `v_i==0 ⇒ FAIL`**（与 n 解耦）彻底封堵。

---

## 3. 攻击点 2：L-2 **新**判据是否又开新口子

### 3a. 新比值带 `r_i = s_i·n ∈ [0.5, 2.0]` —— **偏松但有界、未构成硬假通过**

复现同上。构造「分配明显失真但落在带内」的反例：

| 输入（等权目标） | 实测 | r 向量 | 备注 |
|---|---|---|---|
| n=3 实际 60/20/20（3:1:1） | **PASS** | [1.8, 0.6, 0.6] | 单票 1.8× 超配 |
| n=4 实际 50/25/12.5/12.5（4:2:1:1） | **PASS** | [2.0, 1.0, 0.5, 0.5] | **max/min = 4× 极差** |
| n=5 实际 40/30/10/10/10 | **FAIL**（浮点，见下） | [2.0, 1.5, 0.5, 0.5, 0.5] | 名义恰在边界 |
| n=8 单票 2.333×（r_max=2.0 边界） | PASS | [≈2.0, …] | 单票 2× 上界可达 |

- **判定**：相对 R2 的 TV 判据（随 n 漂移、n=10 即漏网），比值带是**尺度不变、与 n 解耦**的**明确改进**；但它**允许单票最高 2× 超配、且最大/最小持仓比可达 4×**。对「等权目标保真度」而言这**偏松**——是否可接受属**策略口径**问题，**建议在 `threshold_desc` 明示「最大/最小持仓比 ≤ 4×」**以消除口径隐藏。
- **不构成硬假通过**：整只漏建（`v_i==0`）、负值、非有限值仍被决定性拦截（见 §2）。
- ⚠ **边界浮点脆弱性**：n=5 的 `r=[2.0,…,0.5]`（名义恰在带端点）实测 **FAIL**，而 n=4 的 `r=[2.0,1.0,0.5,0.5]` 实测 **PASS**。根因：`shares = v_i/Σv` 浮点误差使 `min_ratio = 0.4999… < 0.5` ⇒ 判 FAIL。方向保守（误杀侧非漏网侧），但与声明「含端点 [0.5,2.0]」不符，建议改 `<=`/`>=` 容差或加 epsilon。

### 3b. 新计数判据「任一 `v_i == 0` ⇒ FAIL」是否在真实路径误杀？—— **latent 风险，当前不活动**

证据（`grep -rn "target_weights\|actual_values" scripts/ backtest/ strategy/`）：
- **无任何生产代码向 L-2 提供 `target_weights`/`actual_values`**；`AllocationFidelityGate` 仅在 `gate_master_audit.get_standard_gates()` 注册、`runner` 仅 import（未在 pre/post 求值）。CI 实测 L-2 = INCONCLUSIVE（"重合标的数 0 < 3"）⇒ **L-2 当前完全 latent**。
- **但**：`docs/audit/governance_closure_report.md` / `handoff_next_session.md` 明确记录 A 路**计划层**会丢弃标的（**H5** 市值加权×2 万下限×不重分配 ⇒ 断崖式丢弃「选 5 只只建 1–3 只」；**H5b** 死价位带 `[151,199]`；**H5c** NAV 退化正反馈螺旋）；`strategy/portfolio.py` 的 `plan_positions` 返回 `dropped`，且 `target_shares = int(value/close)//lot_size*lot_size` 可为 0。
- **判定**：若**将来**把 L-2 接线为「选股目标 vs 计划层」（selection→plan），则 `v_i==0` 是**设计行为**⇒ 计数判据会**误杀**；L-2 的**名义职责**是「防**执行层**等权均分作弊」，正确口径应是「**计划层意图 vs 执行层成交**」（plan→execution），此时 `v_i==0` 才代表真实的执行失败。**当前无接线 ⇒ 不活动**；仅当接线口径选错才会踩雷。**建议：接线时明确为 plan→execution，并保留计数判据。**

### 3c. 非等权目标 + 实际全 0 —— **消息与事实不符（口径问题），非假通过**

复现（`g4_probe_l2.py` §D）：`target={A:0.5,B:0.3,C:0.2}, actual={A:0,B:0,C:0}` → **FAIL**，消息=「目标权重存在显著分化，但**实际分配资金完全等权均分**，权重打分在执行层丢失！」。
- 全 0（未建仓）**并非**「完全等权均分」，**消息口径与事实不符**。
- **不构成假通过**（保守判 FAIL）；但本项目对「口径混用」零容忍，**建议修正消息**（如"实际分配无任何建仓（总额 0）"）。严重度：低（诊断诚实性）。

---

## 4. 攻击点 3：runner 已真正上抛本体判定 —— **已确认 ✓**

复现：`py -3.11 C:/Users/MengLin/AppData/Local/Temp/g4_probe_runner.py`（走真实入口 `run_pre_run_gates(tables=…)`，输出 `g4_runner_out.txt`）

| 探针（真实入口 `run_pre_run_gates(tables=…)`） | 实测 | 判定 |
|---|---|---|
| D-1「close 全 0」（本体 INCONCLUSIVE） | **D-1=INCONCLUSIVE**（不再合成 PASS） | ✅ |
| D-4「无停牌日」（本体 SKIP） | **D-4=SKIP** | ✅ |
| D-4「停牌日成交量=0」正向对照 | **D-1=PASS / D-4=PASS** | ✅ |
| D-4「停牌日成交量>0」 | **D-4=FAIL** | ✅ |
| 混合表（1 正常 + 1 close 全 0） | **D-1=INCONCLUSIVE**（取最严重者上抛） | ✅ |
| D-1 INCONCLUSIVE + `strict=True` | **抛 `GateBlockerError(gate_id='D-1')`** | ✅ |

对照：`runner.py` 中 `GateStatus.PASS` 仅剩 2 处（`:195` 优先级表 + `:205` 注释），无合成返回点。原两处 `runner.py:322/:372` 合成 PASS **已删除**。

> ⚠ 观察（非漏网，方向保守）：D-4 现对**每个** symbol 求值后取最严重者聚合；若某 symbol 样本内无停牌日（SKIP），整仓 D-4 聚合即为 **SKIP**（即便其它 symbol 已检且 PASS）。这会令真实回测 D-4 由"合成 PASS"变"多为 SKIP"——**更诚实**，但属保守偏严，供知悉。

---

## 5. 攻击点 4：4 处漏网复验（修复前→修复后）—— **全部封堵 ✓**

复现：`g4_probe_runner.py` §2 / `qa_probe_residual.py`

| 门禁 | 反例输入 | 修复前 | 修复后 | 正向对照 |
|---|---|---|---|---|
| **G-STRESS-1** | `{"round_trips":5}` / `{...,"trading_days":None}` / `{"metrics":{"round_trips":5}}` | PASS（谎称≥200日） | **INCONCLUSIVE** ×3 | `{40,260}`→PASS；`{40,199}`→INCONCLUSIVE ✅ |
| **G-REPRO-1** | 同指纹 + `metrics` 皆 `{}` / 皆缺失 | PASS（平凡一致） | **INCONCLUSIVE** ×2 | 有 metrics 一致→PASS；不一致→FAIL ✅ |
| **G-2** 模式B | `{task_id,is_checked:False}` | PASS（谎报验签通过） | **SKIP** | 已勾选+签名→PASS；已勾选无签名→FAIL ✅ |
| **G-1** | `data_hash="z"*16`（非 hex） | PASS | **FAIL** | 32 位 hex（大小写）→PASS ✅ |

---

## 6. 攻击点 5：mutation 测试（26 个新测试须必红）

方法：逐文件回退到 `git show HEAD:`（修复前形态）→ 跑 `tests/test_gate_r1_false_pass.py` → 记录变红；字节级备份还原（`git diff --stat` 复原一致）。复现：`bash C:/Users/MengLin/AppData/Local/Temp/g4_mut.sh`（输出 `g4_mut.txt`）。

| 回退文件 | 变红数 | 覆盖的新测试 |
|---|---|---|
| `gate_l_liveness.py` | 15（R1 5 + **R3 10**） | n10/n11/n50/unbuilt_count/target_nan/target_inf/actual_nan/actual_inf/severe_skew/heavy_underweight |
| `runner.py` | 3 | d1_surfaces / d4_surfaces / strict_blocks |
| `gate_consistency.py` | 2 | g_stress_missing_days（+R1 的 ref_unreadable） |
| `gate_repro.py` | 3 | repro_empty / repro_missing（+R1 的 single_fingerprint） |
| `gate_g_governance.py` | 2 | g2_unchecked / g1_nonhex |

**26 个新测试的负向用例全部必红**；正向对照（`test_mild_manual_lot_skew_passes`、`test_normal_tables_still_pass`、`test_g_stress_long_window_still_passes`、`test_repro_real_metrics_still_pass`、`test_g2_checked_with_sig_still_pass`、`test_g1_valid_hex_hash_still_pass`、`test_equal_target_near_equal_still_passes`）按设计保持绿。

### ⚠ 一处**弱锁定**（如实报出）：
`TestL2R3NoResidualFalsePass::test_uneven_target_with_nan_actual_not_pass` —— **回退实现后仍绿**（该用例输入为 `target={A:0.5,B:0.3,C:0.2}` + `actual` 某位 NaN，断言 `!=PASS`；而修复前代码对该输入也算出 Spearman<0.90 ⇒ FAIL，本身即满足 `!=PASS`），**不具判别力**。
- 该守卫**本身是必要的**——我独立证明修复前存在**真实假通过**：非等权目标 + 实际含 NaN 时，若 NaN 的秩恰好与目标序一致，Spearman 可达 **1.000 ⇒ PASS**（穷举出 **9 个** PASS 实例，例如 `target={A:0.2,B:0.3,C:0.5}, actual={A:nan,B:30000,C:60000}` → PASS）。复现：`py -3.11 C:/Users/MengLin/AppData/Local/Temp/g4_nan_matrix.py`（修复前输出 `g4_nan_matrix.txt`）。
- **建议**（受"只改测试不改实现/不触碰 constants"约束，本轮不改）：把该用例输入换成**能判别的** NaN 摆放（如上例），使其回退后必红。（注：新增测试会改变收集数 ⇒ 需同步 constants，故不在本轮直接添加。）

---

## 7. 攻击点 7：独立重扫 29 道门禁 PASS 点——**无新增同类；A-3 为在档遗留**

- `runner.py` PASS 引用仅剩优先级表/注释（§4）✓。
- 重跑 R2 全量残留探针：G-2 / G-STRESS-1 / G-REPRO-1 / G-1 **均已封堵**（§5）。
- **唯一在档漏网：`A-3` GoldenRoundtripGate**（`gate_a_accounting.py:242` 声明 `|diff|<=0.05`，实现为 `val<95 or val>125`）。R4 复现：`roundtrip_total_fee=95.5`(diff=7.72) / `125.0`(diff=21.78) / `100.0`(diff=3.22) → **均 PASS**。
  - 该项 **R2 已由工程师登记**，**不在 R3 的"4 处新漏网"清单内**（R3 清单=G-1/G-2/G-STRESS-1/G-REPRO-1），故**非新增**。
  - 但它是**真实假通过**（相对声明阈值）⇒ **建议单开 Engineer 小卡**：改为「`|val−expected|<=0.05`」或修正 `threshold_desc` 与实现一致。
- 其余 PASS 点（D-2/D-3/E-2/S-1/S-4/G-3/G-4/G-MDD-1/G-DOC-1/G-REF-1）R3 未改动，R4 未发现新漏网。

---

## 8. 诚实登记：**未能证伪**的部分

1. **R2 全部反例**已在 R4 独立复现并确认封堵（§2）。
2. **runner 上抛**经真实入口 `run_pre_run_gates(tables=…)` 证实（§4），非读代码。
3. **4 处漏网**修复前→后经独立探针证实，正向对照仍 PASS（§5）。
4. **26 个新测试**负向用例经 mutation 必红；仅 1 处弱锁定（§6，已报出）。
5. **回归**：978 passed / collect 978 == 常量 / `--ci` 无阻断 / `pre_push [ALL PASS]`，无回归。
6. **R3 未引入新增同类假通过**（§7）。

---

## 9. 遗留登记（供主理人决策，不阻塞本轮）

| 优先级 | 项 | 类型 | 建议 |
|---|---|---|---|
| P2 | **A-3** 声明 `≤0.05` 实为 95~125 区间（假通过，R2 已登记） | 源码 | 单开工程师卡：对齐阈值或修正 desc |
| P3 | L-2 比值带偏松（单票≤2×、最大/最小≤4×） | 设计口径 | `threshold_desc` 明示 4× 极差；如口径更严再收紧 |
| P3 | L-2 边界浮点脆弱（名义恰在 0.5/2.0 端点的结果不稳定） | 实现细节 | 端点比较加 epsilon 或改闭区间 |
| P3 | L-2 非等权 + 实际全 0 的消息口径（"完全等权均分" ≠ 全 0） | 诊断口径 | 修正消息文案 |
| P3 | `test_uneven_target_with_nan_actual_not_pass` 弱锁定 | 测试质量 | 换用可判别的 NaN 摆放 |

> 约束说明：因不得修改 `scripts/gates/constants.py`，本轮**未新增测试**（新增会改变收集数触发基线守卫）；弱锁定问题以报告形式提出，不自行落地。

---

*报告生成：GATE-R4 第二轮 QA 对抗复验（SOP 上限轮）；所有结论均可由 §内命令一条复现。*
