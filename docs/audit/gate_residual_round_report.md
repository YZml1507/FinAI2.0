# 门禁残留假通过清零（GATE-R1 → R4）交付总结

> 交付人：齐活林（交付总监／主理人）｜团队：`software-finai-gate-residual`｜日期：2026-09-11
> 路径：BugFix 快捷路径（工程师修复 → QA 对抗验证 → 主理人统一提交）
> 结论：**QA 第二轮判 PASS**；遗留 5 项如实登记（1 项真实假通过待裁决）

---

## 0. TL;DR

治理层 M3 已清除 15 处「无证据即通过」兜底，但**同一病仍有残留**：本轮以「反例撞击」方式撞出并修复 **15 处假通过**，其中一处（`runner` 合成 PASS）会让既有加固在**真实回测路径上被整体绕开**。

---

## 1. 起点：交接清单被实测推翻

前任交接把两件事列为「剩余待办」，**实测显示均已在 M3 闭环**：

| 条目 | 交接描述 | 实测结论 |
|---|---|---|
| ⑪ | pre-push 用空 context 跑门禁，S/E/A 维不设防 | **已闭环**。`scripts/hooks/pre_push.py:92-96` 已有 `_build_pre_push_context()` 复用 `context_builder.build_repo_context`；实测输出「门禁取证来源: 产物 20260907-150402…（已签名）（ctx 键 21 个）」 |
| ⑩ | D-5/L-1/A-3/A-2/D-2/L-2 六类硬编码兜底 | **5/6 已闭环**。`gate_master_audit --ci` 实测 14 道 run-evidence 门禁全部 INCONCLUSIVE 且带「无证据 ≠ 通过」；D-2 的 `n<30` 分支亦已 INCONCLUSIVE |

⇒ **教训：交接清单本身也会过期；清单必须实测，不得照派**（照派只会得到「已修」式虚报）。

---

## 2. 撞出的真实残留（本轮修复）

### 2.1 L-2 等权短路恒 PASS（起点实例，主理人实测）
`scripts/gates/gate_l_liveness.py`：目标权重等权时**直接 return PASS，完全不检查 `actual_values`**。
反例（修复前全 PASS）：等权目标 + 实际 `90/5/5`；等权目标 + 实际**只建 1 只**；等权目标 + 含负值。
⇒ 策略生产配置实为等权 1/N ⇒ 该门禁**恒 PASS**（空转）。

### 2.2 runner 合成 PASS —— 使既有加固在真实路径失效（最重）
`scripts/gates/runner.py`：D-1/D-4 逐票循环无 FAIL 即**自造 `GateStatus.PASS`**。
实测：**runner 路径 D-1 = PASS 而本体 = INCONCLUSIVE；D-4 = PASS 而本体 = SKIP**。
而 `scripts/run_dividend_backtest.py:480` 的真实回测路径正是以 `tables=` 调用它 ⇒ 加固被绕开。

### 2.3 另 4 处漏网（QA 独立扫描所得）
| 门禁 | 反例 | 修复前 → 修复后 |
|---|---|---|
| G-STRESS-1 | 缺 `trading_days` 仍 PASS 且谎称「压测 ≥200 日」 | PASS → INCONCLUSIVE |
| G-REPRO-1 | 同指纹且 metrics 皆空 ⇒ 平凡一致 | PASS → INCONCLUSIVE |
| G-2 模式B | `is_checked=False` 却谎报验签通过 | PASS → SKIP |
| G-1 | `data_hash` 声明 64-hex，实仅校验 `len>=16` | PASS → FAIL（并修正 desc） |

### 2.4 其他同类加固
A-1 / A-2 / A-4、D-1 / D-4 / D-5、E-1 / E-3、S-2 / S-3 / S-5、G-REF-1 / G-DOC-1 共 15 处。

---

## 3. 判据设计上的关键取舍

**L-2 两轮演化**：
1. R1 采用「归一化总变差 TV > 0.10」。**QA 证伪了其注释**：「0.10 至少捕获 n≤10 整只漏建」——n=10 恰 1 只漏建 **TV 恰为 0.1000**，判据 `>` 为假 ⇒ 仍 PASS。
2. R3 **弃用 TV**，理由（工程师自证）：`max|s_i − 1/n|` 在「某票 k 倍超配」时 = `(k−1)/n`，**仍随 n 缩小**，与 TV 同病；只有**比值**真正尺度不变。
   ⇒ 改为两条与 n 解耦判据：① 计数：任一 `v_i == 0` ⇒ FAIL；② 比值：`r_i = s_i·n ∈ [0.5, 2.0]`。
   并新增有限性校验：目标含 NaN/±inf ⇒ INCONCLUSIVE；实际含 NaN/±inf ⇒ FAIL。

---

## 4. 验收证据（全部实测，主理人独立复跑）

```
全量回归:      978 passed（== constants.TEST_BASELINE_PASSED 978 == --collect-only 978）
门禁总审计:    --ci ⇒ [CI][PASS] 无阻断（PASS 13 / FAIL 1 / SKIP 1 / INCONCLUSIVE 14）
pre-push:      [ALL PASS]（推送期 13 道；PASS 11，WARN 1 = G-MDD-1 展示不阻断）
L-2 独立复现:  n=10 恰1只漏建 FAIL ｜ n=11 真漏建 FAIL ｜ n=50 5只漏建 FAIL
               目标全 NaN/全 inf ⇒ INCONCLUSIVE ｜ 实际含 NaN/inf ⇒ FAIL
               40/40/20 ⇒ PASS（原误杀解除）｜ 比值界 1.8-0.6 PASS / 2.1-0.45 FAIL
runner 复核:   GateStatus.PASS 仅剩优先级表 + 注释 2 处；聚合按 FAIL>INCONCLUSIVE>WARNING>SKIP>PASS 上抛
既有测试:      test_gate_integration.py 12 增 0 删（纯补 fixture，无断言改动）
```

---

## 5. 🔴 QA 对抗验证是本次质量的关键

| 轮次 | 结论 | 说明 |
|---|---|---|
| **R2** | **FAIL**（攻破实现方自述） | 15 处修复中 14 处本体攻不破（mutation 证实双向锁定为真）；但攻破 L-2 阈值边界逃逸 + NaN/inf 假通过，并**独立撞出 runner 合成 PASS 与 4 处漏网** |
| **R4** | **PASS** | R2 反例全封堵；runner 走真实入口验证已上抛；4 处漏网已封；26 新测试 mutation 必红 |

> **本项目铁律再次被验证**：工程师自述「已修 15 处 / IS_PASS: YES」，QA 撞出其中 1 处**仍有假通过**、并发现 **4 处工程师未发现的同类**。
> ⇒ **自述只可信到「本体已改」，不可信到「范围已覆盖」。**

---

## 6. 遗留清单（如实登记）

| 级别 | 项 | 性质 | 处置建议 |
|---|---|---|---|
| **P2** | **A-3** 声明「绝对误差 ≤0.05」，实现为「物理区间 95~125 元」；实测 95.5 / 125.0 / 100.0 均 PASS | **真实假通过 + 契约↔实现漂移** | **待用户裁决**：对齐阈值（更严，可能拦真实产物）或改 desc 对齐实现（不改变严苛度）。涉 BLOCKER 级门禁严苛度，⛔ 不由 AI 独断 |
| P3 | L-2 比值带偏松（允许单票最高 2× 超配，max/min 极差可达 4×） | 设计取舍，有界 | 建议 `threshold_desc` 明示「最大/最小持仓比 ≤4×」 |
| P3 | L-2 端点浮点脆弱：n=5 的 `r=[2.0,…,0.5]` ⇒ FAIL，n=4 同构 ⇒ PASS | 端点确定性 | 建议显式处理端点（保守方向，非假通过） |
| P3 | L-2「非等权目标 + 实际全 0」消息写「完全**等权均分**」 | 消息与事实不符（口径问题） | 改文案 |
| P3 | `test_uneven_target_with_nan_actual_not_pass` 回退后仍绿（弱锁定） | 测试判别力不足；QA 已独立证明该守卫**必要**（修复前存在 9 个 PASS 实例） | 强化该测试输入使其有判别力 |
| 冻结 | **E-3 真实路径恒 INCONCLUSIVE** | `backtest/types.py:94` 的 `Trade` 无 `limit_up/limit_down`，`runner.py:473-474` 以 `Decimal("0")` 兜底 ⇒ 由「假 PASS」变「永不可判」 | 补证据需改 **backtest 引擎**（属证据链工作）。注：`gate_strict` 默认 `False`，默认回测不受阻断，仅 strict 模式受影响 |
| 冻结 | **D-4 SKIP 逃逸** | `context_builder.ci_policy` 对 SKIP **既不阻断也不告警** | 涉及 SKIP 全局语义策略，待裁决 |

---

## 7. Git 处置

```
042994f docs(audit): 归档 GATE-R1/R3 两轮 QA 对抗验证报告
bee73f8 fix(gates): 消除门禁「无信息却 PASS」假通过（GATE-R1/R3）——含 runner 合成 PASS 上抛
a5dda7f chore(git): 忽略根目录临时 notebook（1.ipynb 不纳入版本管理）
cd7738c fix(gates): 同步单测基线常量 876→914（M6 新增 38 测试致单一事实源漂移）
```

- **已推送**（`a8928b2..042994f`），远端 `refs/heads/master` = `042994f` = 本地 HEAD（`git ls-remote` 权威确认）
- 修复前**远端 HEAD 是红的**：其常量为 876 而实测收集数 914 ⇒ 守卫测试 `test_baseline_constant_matches_collected_count` 必红。本次推送恢复绿基线。

---

## 8. 下一步

1. **待用户裁决** A-3（P2）与 D-4 SKIP 逃逸；
2. 可选：单开小卡清零 P3 四项（含强化 1 处弱锁定测试）；
3. 门禁债务清零后 → 启动 **M6 归因实验**（设计文档 `docs/audit/m6_attribution_design.md` 已就绪未执行）。
