# QA 对抗复验报告 — GATE-R5（A-3 阈值对齐 + P3 四项清零）

> 复验编号 **GATE-R6**（任务 #6）｜复验人：QA 严过关（Edward）
> 对象：工程师 **GATE-R5** 交付，自称 `IS_PASS: YES`
> 方法：**只读 + 仅新增探针/测试**；⛔ 未改动任何门禁实现代码。mutation 用「字节级备份 + `cp` 还原」
> （还原后 `sha256` 与改前**逐字节一致**，见 §6）。

---

## 0. 结论

**结论：FAIL**（含未通过项）——**唯一未通过项 = A-3 `GoldenRoundtripGate` 的黄金基准集与引擎权威黄金算例不一致**，
导致 A-3 会**误杀引擎的真实 10 万元往返费用**（且相对 R5 前是 **PASS→FAIL 回归**）。

- ✅ **L-2 / P3 四项全部通过**（端点确定性、消息口径、threshold_desc 等价声明、计数判据）。
- ✅ **重点问**：L-2「非等权目标 + 实际全 0」由 FAIL 改 INCONCLUSIVE **在真实链路中不削弱阻断力**（L-2 当前 latent，无生产路径喂数据；详见 §4）。
- ✅ 回归 997 全绿、`collect==997==基线`、`--ci [CI][PASS]`、`pre_push [ALL PASS]`；既有测试未放宽。
- ❌ **A-3 基准集缺陷**（详见 §2.5）：`GOLDEN_FEE_BASIS=(114.20, 103.22, 102.00)` **不含引擎权威黄金 112.82**，
  喂入引擎真实往返 112.82 → **FAIL（差 1.38 > 公差 0.05）**；R5 前旧实现 `95~125` 区间下 → **PASS**。

> 路由：**A-3 → 工程师（Alex）**修基准集；L-2/P3 无需返工。

---

## 1. 环境与复验范围

| 项 | 值 |
|---|---|
| 仓库 | `D:\Projects\FinAI2.0`（工作树 = R5 未提交改动） |
| Python | `py -3.11`（3.11.5），`PYTHONPATH=<site-packages>` + 探针内 `sys.path.insert(0, repo)` |
| pytest | `-p no:ddtrace -p no:ddtrace.pytest_bdd -q` |
| R5 改动文件（`git status --short`） | `scripts/gates/constants.py`、`gate_a_accounting.py`、`gate_l_liveness.py`、`tests/test_gate_r1_false_pass.py`、`tests/test_gates.py` |

> ⚠️ 澄清主理人背景中的一处：**`tests/test_gate_consistency.py` 并未被 R5 改动**（不在 `git status` 中）；
> 其 A-3 语境 `103.22`(PASS) / `"50"`(FAIL) 在 R5 前后均成立（见 §7）。

---

## 2. 攻击点 1/2：A-3 边界、误杀与假通过

### 2.1 声明↔实现对齐（✅ 通过）
`threshold_desc = "10 万元往返买卖总规费与基准理论值绝对误差 <= 0.05 元"`；
实现 = 与**最近**合法基准的绝对误差 `min_b|val−b| <= 0.05`（`GOLDEN_FEE_ABS_TOLERANCE=Decimal("0.05")`）。
旧的 `val<95 or val>125` 物理区间**已删除**（⑫ 声明↔实现背离已闭合）。

### 2.2 边界（✅ 严格且正确；`<=` 非 `<`）

| 值 | 最近基准 | 误差 | 实测 | 期望 |
|---|---|---|---|---|
| 114.15 / 114.25 | 114.20 | 0.05 | **PASS** | PASS（含端点） |
| 114.14 / 114.26 | 114.20 | 0.06 | **FAIL** | FAIL |
| 103.27 / 103.17 | 103.22 | 0.05 | **PASS** | PASS |
| 103.28 / 103.16 | 103.22 | 0.06 | **FAIL** | FAIL |
| 102.05 / 101.95 | 102.00 | 0.05 | **PASS** | PASS |
| 102.06 / 101.94 | 102.00 | 0.06 | **FAIL** | FAIL |

⇒ **`<=0.05` 严格成立，无端点松口**。

### 2.3 假通过（✅ R4 反例全部封堵）
`95.5`(差6.50) / `100.0`(差2.00) / `125.0`(差10.80) / `108.60`(差5.38) / `110.0`(差4.20) → **全部 FAIL**。
非有限：`NaN / +inf / -inf` → **FAIL**；缺失 `roundtrip_total_fee` → **INCONCLUSIVE**（不自证）。**未发现新假通过**。

### 2.4 非数值兜底（⚠️ 遗留，非 R5 引入）
`roundtrip_total_fee = "abc" / "" / True / [103.22]` → **未捕获 `decimal.InvalidOperation`（门禁抛异常）**；
`expected_fee = NaN` → 同样抛 `InvalidOperation`（R5 把「旧实现下 expected_fee=NaN 会假 PASS」变成「崩溃」——**不是假通过，但仍非优雅**）。
前者 R5 前即存在（旧码也在 `Decimal(str(...))` 处崩），后者为 R5 新引入的崩溃面。**建议：入口对 `expected_fee` 一并做 `is_finite()` 兜底**。

### 2.5 ❌ **A-3 基准集缺陷（本轮 FAIL 根因）**

**证据链（引擎权威黄金 = 112.82，非 114.20）**：

| 来源 | 内容 |
|---|---|
| `tests/test_t203_fees.py:159-172` `test_golden_round_trip_100k` | 断言 buy=31.41、sell=81.41、**合计 112.82**；bundled=102.00 |
| `docs/t305_technical_review.md:33` | 「07 号黄金算例：10 万往返逐项口径 **112.82 元**」 |
| `docs/t207_g3_gate_acceptance.md:32` | 「逐项口径 112.82 元 vs 含规费全佣 102.00 元（差 10.82）」 |
| 独立复算（`compute_fees`，沪深 2024-01-04） | buy=31.41、sell=81.41、**ROUNDTRIP=112.82** |

**A-3 三基准**（`gate_a_accounting.py:257-261`）：`(114.20, 103.22, 102.00)` —— **不含 112.82**。

**实测把引擎真实往返喂入 A-3**：

```
ROUNDTRIP_TOTAL = 112.82
|112.82 − 114.20| = 1.38   (nearest basis=114.20, tol=0.05)
GoldenRoundtripGate.evaluate({roundtrip_total_fee: 112.82}) -> FAIL
旧实现(95~125): 112.82 ∈ [95,125] -> PASS
```

⇒ **R5 引入 PASS→FAIL 回归：引擎自身黄金算例 112.82 现被判 FAIL（差 1.38 远超 0.05）。**

**根因**：A-3 注释的「全拆解口径 114.20」拆解用了 **经手费 4.10**（`gate_a_accounting.py:245-247`），
而引擎费率 `backtest/fees.py:234` 沪深经手费（2023-08-28 起）= `0.0000341` → **3.41**。
两者差 **0.69/边 ×2 边 = 1.38**，恰为 114.20−112.82。⇒ 114.20 的「全拆解口径」标签**名实不符**（引擎真实全拆解 = 112.82）。
`103.22` 亦**不对应引擎任一核算口径**（引擎只有 112.82 / 102.00）。

**这正是主理人点名的「第 4 种被排除的合法基准」——但它不是边缘口径，而是引擎的『主』黄金算例。**

**真实链路影响（限定）**：A-3 当前 **latent** —— `runner.py:548-549` 明示「⛔ 不在此处评估」，
CI 实测 A-3 = `INCONCLUSIVE`（`缺少 roundtrip_total_fee`），`--scheduled` 下 A-3 亦 INCONCLUSIVE（→ 阻断）。
即：**只要未来的『golden fixture』（`runner.py:549` 承诺存在，但代码中查无）注入引擎真实值 112.82，A-3 即把引擎自己的黄金算例挡下**。
⇒ 属**已交付代码的真实缺陷**（基准值错），只是尚未接线。

---

## 3. 攻击点 2：A-3 是否仍存在假通过 → **未发现**

`95.5 / 100.0 / 125.0 / 108.60 / 110.0 / 负值 / 0 / 1e9` → 全部 FAIL；
`114.20/103.22/102.00` → PASS（声明内）；非有限 → FAIL。**无假通过**。

---

## 4. 攻击点 3（⚠️ 重点）：L-2「非等权目标 + 实际全 0」FAIL→INCONCLUSIVE 是否削弱阻断力

### 4.1 事实
- 改前（`git show HEAD:gate_l_liveness.py`）：非等权分支直达 `len(set(v_list))<=1`（全 0 ⇒ set 大小 1）⇒ **FAIL**（报文「完全等权均分」）。
- 改后（`gate_l_liveness.py:434-450`）：先判 `all(v==0.0)` ⇒ **INCONCLUSIVE**（报文「整只未建仓/全为 0，非等权均分」）。

### 4.2 定量（三类拦截路径逐条实测）

| 路径 | 该情形结果 | 是否阻断 |
|---|---|---|
| `is_blocking_result`（runner 回测路径，`runner.py:339/475`） | INCONCLUSIVE | **阻断**（L-2 severity=CRITICAL） |
| `ci_policy` 普通 CI/push（`context_builder.py:274-278`，`run_evidence_blocks=False`） | INCONCLUSIVE | **只告警不阻断**（L-2 ∈ `RUN_EVIDENCE_GATE_IDS`） |
| `ci_policy --scheduled`（`run_evidence_blocks=True`） | INCONCLUSIVE | **阻断** |

### 4.3 结论：**真实链路阻断力「未削弱」（净影响 = 0）**
关键再取证：**L-2 当前完全 latent** —— 全仓 `grep target_weights|actual_values` 仅命中 `gate_l_liveness.py` 自身 + 测试；
`context_builder.build_repo_context` **不注入** L-2 输入。实测 `--ci`：
```
[L-2] 权重与分配资金保真度检验: 重合标的数 0 < 3，样本不足以做秩相关统计（无证据 ≠ 通过）
```
即：**任何生产路径（CI/push/scheduled/runner）里 L-2 都拿不到 `target_weights/actual_values` ⇒ 永远走到 `common_keys<3` 的 INCONCLUSIVE**，
与「全 0 分支」无关。⇒ 「非等权+全 0」在**生产链路中不可达**，R5 的 FAIL→INCONCLUSIVE 改动**没有把任何『原本会被拦』的生产情形变成『不拦』**。

- 理论上限：**仅在** `ci_policy` 普通 CI 路径（warn-only），若 **将来** L-2 被接线喂入分配数据，此情形会由「阻断」变「告警」——属**潜在**风险，非现网退化。
- 且 INCONCLUSIVE **≠ PASS**（Fail-Closed「无证据 ≠ 通过」保留）；与等权分支 `sum_v<=0 ⇒ INCONCLUSIVE` 口径**一致化**，方向自洽。

---

## 5. 攻击点 4：L-2 端点确定性（⚠️ 用可达构造 `s_i=r_i/n`，Σr_i==n）

构造法：令 `actual = r` 向量（Σr=n），则 `s_i=r_i/n`、`r_i=s_i·n=r_i`，避免 Σ 归一化陷阱。

| n | r 向量 | 名义端点 | 实测 |
|---|---|---|---|
| 5 | [2.0, 0.5, 5/6×3] | max=2.0, min=0.5 | **PASS** |
| 4 | [2.0, 0.5, 0.75, 0.75] | 端点 | **PASS** |
| 7 | [2.0, 0.5, 0.9×5] | 端点 | **PASS** |
| 10 | [2.0, 0.5, 0.9375×8] | 端点 | **PASS** |
| 13 | [2.0, 0.5, 10.5/11×11] | 端点 | **PASS** |
| 5 | [2.5, 0.625×4]（真越上界） | — | **FAIL** |
| 5 | [0.4, 1.15×4]（真越下界） | — | **FAIL** |

⇒ P3-①`EQUAL_WEIGHT_RATIO_EPS=1e-9` **使端点判定确定且与「含端点」声明一致**；真越界仍 FAIL（容差仅吸收浮点噪声）。
（R4 曾实测 n=5 名义端点在旧严格 `>2.0` 下 FAIL、n=4 PASS —— 本轮 n=4/5/7/10/13 **全部 PASS**，修复成立。）

---

## 6. 攻击点 5：Mutation（反转实现 ⇒ 新测试必须变红）

方法：`git show HEAD:<file>` 回退 + 针对性剥离守卫；改前/改后 `sha256` 校验。

| 变异 | 目标测试 | 结果 |
|---|---|---|
| A：`gate_a_accounting.py` 回退 HEAD | `TestA3GoldenBasisAlignment`(13) | **8 failed / 5 passed**（95.5/100.0/125.0/message/override×2/constants/nonfinite 变红） |
| B：`gate_l_liveness.py` 回退 HEAD | `TestL2EndpointDeterminismAndWording`(6) | **3 failed / 3 passed**（n5 端点 / 全 0 文案 / threshold_desc 变红） |
| C：剥离 `nonfinite_actual` 守卫（模拟 pre-R3 空洞） | 强化例 `test_uneven_target_with_nan_actual_not_pass` | **1 failed**（`target={A:.2,B:.3,C:.5}`+`actual={A:nan,B:30000,C:60000}` → 旧路径 Spearman=1.0 **假 PASS**，断言 FAIL ⇒ 变红） |

- **强化例的区分力已独立证实**：改前那版输入（`0.5/0.3/0.2`+nan）在旧实现下亦 FAIL（无区分力）；R5 换的新输入在**剥离守卫后**⇒假 PASS ⇒ 断言反转，**证明该测试真正锁定守卫**（修复前 red、修复后 green，双向可验证）。
- 未随变异变红的用例为**控制组/守卫组**（应在前后都成立），非弱锁：
  A 中 5 例=`basis_*_passes`(旧 95~125 下本就 PASS)+`missing_inconclusive`(R1 既有)；
  B 中 3 例=`n4 端点`(旧实现恰好 PASS)+`just_beyond_still_fails`+`all_equal_nonzero_fail`。
- **还原校验**：`sha256` 改前==改后：
  `gate_a_accounting.py = 2833e6ae…bd3e91e3`；`gate_l_liveness.py = f61e613b…55d155329`；`git diff --stat` 与 R5 一致，无残留（`.qabak` 已删）。

---

## 7. 攻击点 6：既有测试未放宽

- `tests/test_gates.py`：仅 A-3 FAIL **报文字符串**由「严重偏离 A 股真实费率物理区间」→「超出容许容差」；
  **`assert res.status == GateStatus.FAIL` 未动**（`:395` 仍喂 `50.00` ⇒ FAIL）。
- `tests/test_gate_consistency.py`：**未被 R5 改动**（不在 `git status`）；其 A-3 PASS 语境 `103.22`、FAIL 语境 `"50"` 在 R5 前后均成立。
⇒ **既有测试未放宽**（✅）。

---

## 8. 攻击点 7：回归

| 项 | 结果 |
|---|---|
| 全量单测 | **997 passed** in 85.40s（0 failed） |
| `collect-only` | **997 tests collected** == `constants.TEST_BASELINE_PASSED = 997`（G-DOC-1 守卫通过） |
| `gate_master_audit --ci` | `[CI][PASS] 无阻断项` |
| `scripts/hooks/pre_push.py` | `[ALL PASS]`，exit 0（报「实际通过: 997 passed，高于基线 997」） |
| `gate_master_audit --scheduled` | `[CI][BLOCKED]` 14 项（含 A-3 INCONCLUSIVE）—— 因 run 证据缺失，属设计预期，**非 R5 回归** |

---

## 9. 遗留 / 未能证伪（诚实登记）

| # | 项 | 性质 | 严重度 | 影响 |
|---|---|---|---|---|
| **A-3-1** | **基准集 `(114.20,103.22,102.00)` 排除引擎权威黄金 112.82** → 引擎真实往返 FAIL（旧实现 PASS） | **交付缺陷（基准值错）** | **高**（潜在误杀+回归） | 现 latent；接线后 A-3 挡下引擎黄金 |
| A-3-2 | `expected_fee=NaN` 未做 `is_finite()` → `InvalidOperation` 崩溃 | 健壮性 | 低 | 调用方误传才触发；非假通过 |
| A-3-3 | 非数值 `actual`（str/bool/list）→ 未捕获 `InvalidOperation` | 健壮性（R5 前即有） | 低 | fail-loud，非假通过 |
| L-2-1 | 比值带 `[0.5,2.0]` 仍允许 max/min 极差 4×（R4 遗留，R5 仅补端点确定性+声明，**未收紧**） | 设计取舍（已声明） | 低 | 声明内，非假通过 |
| L-2-2 | 「非等权+全 0」FAIL→INCONCLUSIVE 的**潜在** ci_policy 降级 | 潜在（当前不可达） | 低 | 生产净影响 0（§4） |
| — | L-2/A-3 均为 **latent**（无生产路径喂数据） | 接线缺口 | 中 | 门禁「在档但未生效」 |

---

## 10. 路由与结论

- **A-3 → 工程师（Alex）**：将 `GOLDEN_FEE_BASIS` 的「全拆解口径」基准由 **114.20 更正为 112.82**（或至少纳入 112.82；
  并核对 `103.22` 的来源），使 A-3 不误杀引擎 `test_t203_fees.py::test_golden_round_trip_100k` 锁定的 112.82。
  同时补 `expected_fee` 的有限性兜底（A-3-2）。
- **L-2 / P3 → 无需返工**：端点确定性、消息口径、threshold_desc 等价声明、计数/比值判据均通过；FAIL→INCONCLUSIVE 在生产链路无净阻断力损失。
- **本报告独立复现命令**（截断日志见 `%TEMP%\g6_*.txt`）：`g6_probe_a3.py`、`g6_probe_l2.py`、`g6_probe_a3_engine.py`、`g6_mut.sh`、`g6_mut.log`。

**结论：FAIL**（未通过项 = A-3 基准集）。
