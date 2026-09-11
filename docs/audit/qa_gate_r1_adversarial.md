# GATE-R2 对抗性验证报告（QA 严过关 / software-qa-engineer）

> 任务卡：**GATE-R2**（任务列表 #2）
> 对象：工程师 GATE-R1 交付——「消除门禁『无信息/不可判却返回 PASS』假通过分支」，声称修 **15 处**
> 立场：**对抗性证伪**（目标推翻交付，而非确认）。只读实现 + 只改测试；未修改任何门禁实现代码。
> 环境：`PYTHONPATH=C:/Users/MengLin/AppData/Roaming/Python/Python311/site-packages`，`py -3.11`
> 复现前置（每条命令均在仓库根 `D:\Projects\FinAI2.0` 执行）：
> ```bash
> cd /d/Projects/FinAI2.0
> export PYTHONPATH="C:/Users/MengLin/AppData/Roaming/Python/Python311/site-packages"
> ```

---

## 0. 结论（先给判断）

**结论：FAIL（有条件）** —— 并非「15 处修复无效」，而是：

1. **L-2 的修复方案本身残留可复现的假通过**（n=10 整只漏建 → PASS；NaN/inf 退化输入 → PASS），且其代码注释声明的一条性质被证伪；
2. **扫描发现漏网假通过**：`G-STRESS-1`（缺 trading_days → PASS）、`G-REPRO-1`（空 metrics 平凡一致 → PASS）、`G-2` 模式B（未勾选 → PASS）、`G-1`（实际校验弱于声明阈值）、`A-3`（声明 ≤0.05 实为 95~125 区间，**登记属实**）；
3. **`runner.py` 两处合成 PASS 的登记属实，但被低估**：它在本仓**真实回测路径**（`run_dividend_backtest` 以 `tables=` 调用）上**掩蔽了本轮刚加的 D-1 INCONCLUSIVE / D-4 SKIP**；
4. **未披露的行为回归**：修复后 **E-3 在真实回测路径恒为 INCONCLUSIVE**（`Trade` 无 `limit_up/limit_down`），由「假 PASS」变为「永远不可判」。

**智能路由判定：→ Engineer（源码有 Bug）**。未发现需 QA 自修的测试代码缺陷（38 个新测试的双向锁定经 mutation 验证成立）。

**未通过项清单（可复现）见 §2 / §3；诚实登记的「未能证伪」见 §6。**

---

## 1. 回归与基线（全部无异常）

| 项 | 命令 | 实测 | 判定 |
|---|---|---|---|
| 全量单测 | `py -3.11 -m pytest tests -q -p no:ddtrace -p no:ddtrace.pytest_bdd` | **952 passed**（70.34s） | ✅ |
| 收集数 = 基线 | `py -3.11 -m pytest tests --collect-only -q -p no:ddtrace` | **952 collected == `TEST_BASELINE_PASSED`(952)** | ✅ |
| CI 门禁 | `py -3.11 -m scripts.gates.gate_master_audit --ci` | `[CI][PASS] 无阻断项`；PASS 13 / FAIL 1(G-MDD-1, WARN 降级) / SKIP 1 / INCONCLUSIVE 14 | ✅ |
| pre-push | `py -3.11 -m scripts.hooks.pre_push` | `[ALL PASS]`（回归 952 ≥ 基线 952） | ✅ |

未发现回归、未新增 SKIP 逃逸（SKIP=1 即 D-4，详见 §4）。

---

## 2. 攻击点 1：L-2 等权分支 TV 阈值设计（`scripts/gates/gate_l_liveness.py`）

修复形态：等权目标 ⇒ `TV = 0.5·Σ|s_i − 1/n|`，`TV > 0.10 → FAIL`，`≤0.10 → PASS`。
**生产规模参照**：`strategy/portfolio.py` `default 3/5/8`、`hard_limit=10`（`strategy/candidates.py:204`），故真实 n≤10。

复现命令（两枚探针）：
```bash
py -3.11 C:/Users/MengLin/AppData/Local/Temp/qa_probe_l2.py
py -3.11 C:/Users/MengLin/AppData/Local/Temp/qa_probe_l2b.py
```

### 2.1 ✅ 正面（修复有效，未证伪）
| 输入（等权目标 n=3） | 实测 | 理论 TV |
|---|---|---|
| `{A:900000,B:50000,C:50000}` | **FAIL** | 0.5667 |
| `{A:100000,B:0,C:0}`（只建 1 只） | **FAIL** | 0.6667 |
| `{A:-50000,B:80000,C:70000}`（负值） | **FAIL** | — |
| `{A:0,B:0,C:0}`（未建仓） | **INCONCLUSIVE** | — |
| `{A:34000,B:33000,C:33000}`（近似等权） | **PASS** | 0.0067 |

### 2.2 ❌ 反例 A：**n=10 恰有 1 只完全未建仓 ⇒ PASS（假通过）**
- 输入：`target={S0..S9: 0.1}`，`actual={S0:0, S1..S9:1/9}` → TV=**0.1000**，`0.1000 > 0.10` 为 `False` ⇒ **PASS**。
- n≤9（含 default max=8）同场景为 FAIL：n=3→0.333、n=5→0.200、n=8→0.125、n=9→0.111。
- **同时证伪工程师代码注释**（`gate_l_liveness.py:162`「0.10 至少可捕获 n<=10 的『整只漏建』」）：**n=10 恰不捕获**（严格 `>` 的边界 off-by-one）。`hard_limit=10` 属可达配置。
- 判定：**构成假通过**（相对其自称的不变式）。

### 2.3 ❌ 反例 B：大 n 下整只漏建全部漏网（超出 default hard_limit 时）
- n=11→TV=0.0909、n=20→0.05、n=30→0.0333、n=50→0.02，**全部 PASS**。
- n=50 有 **5 只**完全未建仓 → TV=0.1 → **PASS**。
- 附加脆弱性：TV 恰为 0.1 时结果由**浮点**决定（n=30/60 的 `3/30`、`6/60` 算出略 >0.1 → FAIL；n=20/40/50 的 `≤0.1` → PASS），阈值判定不稳定。
- 判定：**构成假通过**（n>10 时；改配置放开 hard_limit 即命中）。

### 2.4 ❌ 反例 C：**NaN / inf 退化输入 ⇒ PASS（假通过，与 n 无关）**
| 输入 | 实测 | 说明 |
|---|---|---|
| 目标全 `NaN`（n=3） | **PASS**（TV=0.0） | `set({nan,nan,nan})` 视为「等权」⇒ 目标未校验 |
| 目标全 `inf`（n=3） | **PASS** | 同上 |
| 现值含 `NaN` | **PASS**（TV=`nan`） | `nan > 0.1` 为 False ⇒ PASS |
| 现值含 `inf`（单只） | **PASS**（TV=`nan`） | `inf/inf=nan` ⇒ PASS |
| 目标 `NaN+inf` 混合 | FAIL | 非确定 |

- 判定：**构成假通过**（退化/非法输入 ⇒ PASS），直接违背任务书「NaN/inf 是否都不得 PASS」。

### 2.5 ❌ 反例 D：**n=3 整手约束 ⇒ 误杀（反向）**
- `{A:45000,B:45000,C:10000}`（等权目标，因高价股整手约束被迫偏斜）→ TV=0.2333 **FAIL**；`{40000,40000,20000}`→0.1333 **FAIL**。
- 与 2.2 并存 ⇒ **阈值语义随 n 漂移**：小 n 过严（可能误杀合法的整手约束执行），大 n 过松（漏掉整只漏建）。
- 判定：**误杀嫌疑成立**（需产品确认 n=3 整手偏斜是否可接受；登记为设计缺陷，非硬 Bug）。

### 2.6 边界项（除上述外均正确，未证伪）
目标全非正 / Σw≤0 → INCONCLUSIVE ✅；实际总额 0（含负）→ FAIL ✅；dict 重合<3 / list 长度不匹配 / 单标的 / 空 dict → INCONCLUSIVE ✅；非等权分支未受影响（50/30/20 匹配→PASS，实际等权→FAIL）✅。

**→ 路由：L-2 属源码缺陷（阈值边界 + NaN/inf 未设防）。**

---

## 3. 攻击点 4：全 29 道门禁 PASS 点反例扫描（漏网假通过）

复现命令：
```bash
py -3.11 C:/Users/MengLin/AppData/Local/Temp/qa_probe_residual.py
py -3.11 C:/Users/MengLin/AppData/Local/Temp/qa_probe_e3.py
```

| # | 门禁 | 输入 | 实测 | 是否假通过 | 工程师是否登记 |
|---|---|---|---|---|---|
| 1 | **G-STRESS-1** `gate_consistency.py:343` | `{"round_trips":5}`（无 `trading_days`） | **PASS**，且 message 谎称「>= 200 日」 | **是**（阈值声明「round_trips>0 且 ≥200 日」，缺天数时该维被静默跳过） | ❌ 未登记 |
| 2 | **G-REPRO-1** `gate_repro.py:215` | 两产物同 `repro_fingerprint`、`metrics` 皆缺失/皆 `{}` | **PASS**「逐字段一致」 | **是**（空集平凡相等，0 信息即 PASS） | ❌ 未登记 |
| 3 | **G-2** `gate_g_governance.py:174` | `{"task_id":"T1","is_checked":False}` | **PASS**「勾选签名验证通过」 | **是**（未勾选任务谎报验签通过；CI 走 tasks_path 模式规避，仅直调可达） | ❌ 未登记 |
| 4 | **A-3** `gate_a_accounting.py:242/284` | `{"roundtrip_total_fee":95.5}`（diff=7.72）、`125.0`（diff=21.78） | **PASS** | **是**（相对声明阈值 ≤0.05；实现为 95~125 物理区间） | ✅ 已登记，**核实属实** |
| 5 | **G-1** `gate_g_governance.py:55` | `data_hash="zzzz…"`（16 位非 hex） | **PASS** | 阈值/实现不一致（声明 64 位 hex，实现仅 `len>=16`）；弱化非硬假通过 | ❌ 未登记 |
| 6 | **runner.py:322（D-1）** | `tables={sym: bars(close 全 0)}` | runner 路径 **PASS**；门禁本体 **INCONCLUSIVE** | **是**（合成 PASS 掩蔽本轮新增的 D-1 INCONCLUSIVE） | ✅ 已登记 |
| 7 | **runner.py:372（D-4）** | `tables={sym: bars(tradestatus 全 1)}` | runner 路径 **PASS**；门禁本体 **SKIP** | **是**（合成 PASS 掩蔽本轮新增的 D-4 SKIP） | ✅ 已登记 |

### 3.1 runner 两处合成 PASS：登记属实，但「非 evaluate 范围」低估了影响
- **属实性**：`runner.py:322` / `:372` 确为 `status=GateStatus.PASS` 的合成返回；仅 `res.status == FAIL` 才被 `_check_result` 采纳，`INCONCLUSIVE`/`SKIP` 被**静默丢弃**后合成 PASS。
- **「非门禁 evaluate 范围」**：字面成立（代码在 runner，不在 `evaluate`）——**但功能上它替代了 evaluate 的判定**，且：
  - **`run_dividend_backtest.py:480` 真实回测路径即以 `tables=tables` 调用** `run_pre_run_gates`（证据：`grep -n "run_pre_run_gates(" scripts/run_dividend_backtest.py` → `480: pre_results = run_pre_run_gates(… tables=tables …)`）。
  - ⇒ 本轮 D-1/D-4 的 fail-closed **在真实回测路径上被绕开**：D-4 只在 CI（`ctx["bars"]` 直调 evaluate，见 §4）才 SKIP，回测路径恒为合成 PASS。
- **结论**：该登记不是「装饰性」，而是**两个已修门禁在生产路径上的并行逃逸口**，应升级为待修项而非可接受残留。

### 3.2 未发现假通过的 PASS 点（已撞，未证伪）
`D-2`（n<30→INCONCLUSIVE，std<1e10→FAIL）、`D-3`（<60 天→INCONCLUSIVE）、`E-2`（空证据→INCONCLUSIVE）、`S-1`（缺 turnover→INCONCLUSIVE）、`S-4`（无分红/缺分档→INCONCLUSIVE）、`G-3`（≠370→FAIL）、`G-MDD-1`（无产物→INCONCLUSIVE，`round_trips=0`→INCONCLUSIVE）、`G-DOC-1` / `G-REF-1`（无文档/读失败→INCONCLUSIVE，本轮回溯修复成立）。

---

## 4. 攻击点 5：D-4 「PASS → SKIP」是更诚实还是新逃逸口？

复现：`grep -E "^(D-1|D-4)" <ci_full>` ⇒ CI 下 `D-4 | SKIP`。

**判断：方向更诚实，但确认开了一道（可见、不可断）的窄逃逸口，且仅对 CI 生效。**

- **更诚实的证据**：`GateStatus.SKIP` 语义为「不适用」，`is_pass` 严格 `==PASS`（`base.py:89`）；`gate_master_audit.print_summary` 对 SKIP>0 打印「[未全绿] 存在未检验项」（实测 CI 摘要正是此态，SKIP=1，不再谎报全绿）。此前合成 PASS 会被计入 13 个 PASS。
- **逃逸口的证据**：`context_builder.ci_policy` 中 **SKIP 既不进 blockers 也不进 warnings**（`ci_policy` 只处理 `FAIL`/`INCONCLUSIVE`）⇒ D-4 SKIP 在 CI 只出现在明细表里，不告警、不阻断；`--scheduled`（`run_evidence_blocks=True`）也只升格 INCONCLUSIVE，SKIP 仍不阻断。
- **叠加证据（§3.1）**：真实回测路径（`tables=`）**根本走不到 SKIP 分支**，恒合成 PASS ⇒ 「诚实化」仅覆盖 CI/`ctx["bars"]` 一路，回测一路未受益。
- **一致性**：同一工程师对「无样本」给了两种语义——D-4/D-5/A-4(无卖出) 判 **SKIP**，D-1/E-3/A-1/A-2 判 **INCONCLUSIVE**。判断标准是「数据表明不适用」vs「证据不足」，但**「样本内无停牌日」无法区分『真的没停牌』与『数据源丢了停牌行』** ⇒ SKIP 可掩盖数据缺失。

**结论**：语义比假 PASS 诚实，**可接受但需登记**；建议 D-4 类「样本为空」改用 INCONCLUSIVE（与 E-3/D-1 对齐），或让 ci_policy 对 SKIP 也出告警。

---

## 5. 攻击点 2 & 3：集成测试未放宽 + 38 测试双向锁定（攻击失败，交付可信）

### 5.1 `tests/test_gate_integration.py`：**确未放宽**
- `git diff --stat` ⇒ `12 insertions(+), 0 deletions(-)`；新增**全部是 fixture 字段**（A-1 用例补 `side/price/limit_up/limit_down`；A-4 用例补 `limit_up/limit_down/total_fee`），**未删除、未改动任何断言**。
- 两个用例**仍因预期原因失败**（`-rA` 实测）：
  - `test_a1_fee_sum_discrepancy_blocks_post_run` → log `[A-1] 后置门禁阻断: 检出 1 笔成交七科目费用求和与总费用不平！`，`exc_info.value.gate_id == "A-1"` 真实触发；
  - `test_a4_stamp_tax_time_travel_blocks_post_run` → log `[A-4] …历史分段印花税违规…`，`gate_id == "A-4"` 真实触发。
  - 新增板价只是让 E-3（现要求板价证据）不抢在 A-1/A-4 之前阻断，**未掩盖失败**。
```bash
py -3.11 -m pytest "tests/test_gate_integration.py::TestGateIntegration::test_a1_fee_sum_discrepancy_blocks_post_run" "tests/test_gate_integration.py::TestGateIntegration::test_a4_stamp_tax_time_travel_blocks_post_run" -q -p no:ddtrace -rA
```

### 5.2 38 个新测试：**真双向锁定（mutation 验证）**
方法：逐文件回退到 `git show HEAD:` 旧版，运行 `tests/test_gate_r1_false_pass.py`，观察变红；再逐处单点条件反转。全程用**字节级备份**还原，`git diff --stat` 复原前后完全一致（实现代码零残留改动）。

**逐文件回退 → 变红数（=对应修复点的反向锁定）：**
| 回退文件 | 变红测试数 | 覆盖修复点 |
|---|---|---|
| `gate_l_liveness.py` | 5 | L-2（含正向 near-equal 亦被锁到新指标） |
| `gate_a_accounting.py` | 6 | A-1×2 / A-2×2 / A-4×2 |
| `gate_d_data.py` | 3 | D-1 / D-4 / D-5 |
| `gate_e_engine.py` | 4 | E-1×3 / E-3 |
| `gate_s_scientific.py` | 3 | S-2 / S-3 / S-5 |
| `gate_repro.py` | 1 | G-REPRO-1 |
| `gate_consistency.py` | 1 | G-REF-1 |
| `constants.py` | 0（新文件不覆盖它） | 由 `test_gate_consistency.py::TestGateCountBaselineConsistency::test_baseline_constant_matches_collected_count` 单独锁定（回退 952→914 实测变红：`914 != 952`）✅ |

**单点反转（更细粒度）→ 全部 RED：**
```bash
py -3.11 C:/Users/MengLin/AppData/Local/Temp/qa_single_mut.py
# L-2 `if tv>th` → `if False and …` ⇒ test_equal_target_severe_skew_fails RED
# E-1 `if missing_std:` → `if False and …` ⇒ test_empty_results_inconclusive RED
# D-4 `if suspended_days==0:` → `if False and …` ⇒ test_no_suspension_days_skips RED
# S-3 `if stress_ret<=0:` → `if False and …` ⇒ test_negative_stress_both_negative_fails RED
# A-1 `if total_fee_given or …` → 旧式 `if total_fee_declared>0:` ⇒ test_declared_zero_but_items_nonzero_fails RED
```
**未发现「mutation 后仍绿」的假锁定** ⇒ 38 测试的双向锁定成立（攻击失败）。

---

## 6. 攻击点 6/7 补充 + 未披露行为回归

### 6.1 E-3 行为回归（**未披露**，需工程师确认）
- 真实 `backtest.types.Trade` **无 `limit_up/limit_down` 字段**（`grep limit_up backtest/` 无结果；`Trade` 定义见 `backtest/types.py:94`）。
- `runner.run_post_run_gates` 对非 dict 成交补 `limit_up=getattr(t,"limit_up",Decimal("0"))=0` ⇒ `checkable=0` ⇒ **E-3 恒 INCONCLUSIVE**。
- 实测（`qa_probe_e3.py`）：`E-3（真实回测 enrichment 后）状态 = INCONCLUSIVE`。
- 影响：真实回测（`strict=gate_strict` 默认 False）虽不 raise，但**每次产物的 `gate_statuses` 里 E-3 由 PASS 变 INCONCLUSIVE**；若以 `gate_strict=True` 运行则**回测将被 E-3 阻断**。方向比假 PASS 诚实，但**修复并未让 E-3 真正执行校验**，只把「假 PASS」换成「永不可判」，应显式登记。

### 6.2 A-3 不一致（**登记属实**）
- `threshold_desc="|diff|<=0.05 元"`，实现为 `val<95.0 or val>125.0`（`gate_a_accounting.py:242 vs 284`）；`diff` 算出后只进 metrics、**从未参与判定**。
- 反例：`roundtrip_total_fee=95.5`（diff=7.72）、`125.0`（diff=21.78）→ 均 **PASS**。⇒ 属「声明阈值 vs 实际阈值背离」的假通过类缺陷。

---

## 7. 诚实登记：我**未能证伪**的部分

1. A-1/A-2/A-4/D-1/D-4/D-5/E-1/E-3/S-2/S-3/S-5/G-REPRO-1/G-REF-1 的**修复点本体**：在各自设计范围内，穷举/随机反例均**未**造出「无信息却 PASS」；mutation 证明其锁定为真。
2. `test_gate_integration.py` 的 12 处新增：**确为纯 fixture 补充**，未放宽断言。
3. 38 个新测试：**双向锁定成立**（逐文件 + 单点 mutation 均变红）。
4. 回归：952 passed / collect 952 == 常量 / `--ci` 无阻断 / `pre_push [ALL PASS]`，**无回归**。
5. `constants.py` 952：被既有守卫锁定（回退即红）。
6. A-3、runner 双合成 PASS：工程师**已如实登记**，我仅补充了「回测路径可达」这一加重情节。
7. §3.2 所列其余 PASS 点：已反例撞击，**未发现**假通过。

---

## 8. 待办（建议转 Engineer）

| 优先级 | 项 | 期望修复 |
|---|---|---|
| P1 | L-2 NaN/inf 未设防（§2.4） | 入口对 `w_list/v_list` 做有限性校验，非有限 ⇒ INCONCLUSIVE/FAIL |
| P1 | L-2 阈值边界（§2.2/2.3） | 改判据为「未建仓标的数 ≥1 即 FAIL」或阈值随 n 自适应（如 `max(0.10, 某函数)`），并修正注释「n<=10」的假声明 |
| P1 | runner.py:322/372 合成 PASS（§3.1） | 让 INCONCLUSIVE/SKIP 结果上抛（至少不静默丢弃），消除回测路径逃逸 |
| P2 | E-3 真实路径恒 INCONCLUSIVE（§6.1） | runner 为 E-3 提供真实板价，或显式登记该门禁在无板价数据时不可判 |
| P2 | G-STRESS-1 缺 trading_days → PASS（§3 #1） | 缺天数 ⇒ INCONCLUSIVE |
| P2 | D-4 类 SKIP 逃逸（§4） | 或改 INCONCLUSIVE，或 ci_policy 对 SKIP 出告警 |
| P3 | G-REPRO-1 空 metrics 平凡一致（§3 #2） | 空 metrics 组 ⇒ INCONCLUSIVE |
| P3 | G-2 模式B / G-1 data_hash / A-3 阈值（§3 #3/#4/#5） | 对齐声明阈值或修正 threshold_desc |

---

*报告生成：GATE-R2 QA 对抗性验证；所有结论均可由 §内命令一条复现。*
