# M3 门禁 P0 落地 — 交付报告（M1 止血 + M2 未启动）

> 交付人：齐活林（交付总监）｜团队：`software-finai2-governance`｜日期：2026-09-10
> 状态：**M3 已冻结**（门禁逻辑全部可用且经验证）；**改动全部留在工作区、未提交 git**
> 前置：[`overview.md`](overview.md)（审计总报告）｜[`consistency_audit.md`](consistency_audit.md)（架构师审计）｜[`roadmap_decision.md`](roadmap_decision.md)（路线决策）
> 验证报告：[`qa_verification.md`](qa_verification.md)（QA 第一轮）｜[`qa_final_verification.md`](qa_final_verification.md)（QA 终验）｜[`repro_root_cause.md`](repro_root_cause.md)（M2 根因，待实施）

---

## 0. TL;DR

**门禁体系从「空转的装饰品」变成了「只拦真问题、且拦得住」的真实防线。** 测试基线 725 → **760**，连跑两遍一致、无残留；共改动 **28 个文件（+1200 余行）**，未提交。

关键数字：

| 指标 | 改造前 | 现在 |
|---|---|---|
| 门禁总数 | 24 | **28** |
| 空 context 总审计 | `PASS:1 / SKIP:23` + **打印「全绿」** | `FAIL:3 / SKIP:23 / INCONCLUSIVE:1` + **「[警告]」** |
| pre-push 真实阻断项 | **1 道**（仅母库 370 行守卫） | **3 道**——且这 3 道**全是真实缺陷** |
| 已知「无证据即通过」兜底 | ≥ 15 处 | **0 处**（元测试双向锁定） |
| 推送期 SKIP | 23 | **0** |
| CI | 空 ctx + 恒红/恒绿 | 真实 ctx + **策略化**（真问题红、缺证据告警） |

---

## 1. 改了什么

### M1 止血（6 份材料 + README + 权威仓）
- 6 份失实材料顶部加「⛔ 作废·待重写」横幅（**原文与数字一字未删**，保留留痕）：`docs/t313_dividend_stress_report.md`、`T312_FINAL_SUMMARY.md`、`docs/compliance/{strategy_description_template,filing_checklist,T405_COMPLIANCE_AUDIT}.md`、`docs/delivery/PHASE4_ADMISSION_RESOLUTION.md`
- 横幅如实定性：**材料未递交券商（模拟阶段/账户未开通）⇒ 不存在已发生的申报违规，本处置 = 递交前修正 + 显式冻结**
- 额外查出 2 处此前未登记的失实：期末总资产 108,421.32（真值 **108,421.14**）、往返 102 次（真值 **78**）
- `docs/README.md` 单文件自相矛盾已真修（line 4 / line 77 / mermaid 节点统一为「Phase 4 ⏸ 暂停，待策略 v2 落地后重审」）
- **权威仓** `D:\Projects\research-finai\specs\...\tasks.md` 追加 **TK-30** 更正登记（纯追加，历史 34975 字符逐字未变，仅新增 992 字符）

### M3 门禁 P0（核心）
| 改动 | 文件 | 要点 |
|---|---|---|
| 状态语义 | `scripts/gates/base.py` | 新增 `GateStatus.INCONCLUSIVE`；`is_pass` 仅认 PASS；新增 `is_blocking_result()`（FAIL 或 INCONCLUSIVE×≥CRITICAL 均阻断） |
| 汇总与退出码 | `scripts/gates/gate_master_audit.py` | 分行报 `PASS/FAIL/SKIP/INCONCLUSIVE`；仅三者全 0 才打印「全绿」；新增 `--mdd` 与 `--ci` |
| 4 道新门禁 | `scripts/gates/gate_consistency.py`（新） | G-MDD-1（MDD>0.35 FAIL；`round_trips==0` INCONCLUSIVE）、G-DOC-1、G-STRESS-1、G-REF-1 |
| 去兜底 | `scripts/gates/runner.py`、`gate_s_scientific.py`、`gate_g_governance.py`、`gate_d_data.py`、`gate_l_liveness.py`、`gate_a_accounting.py`、`gate_e_engine.py` | 清除 **15 处「无证据即通过」**；`run_pre/post_run_gates(strict)` 改用 `is_blocking_result()` |
| 五必挂真跑 | `scripts/gates/must_fail_probe.py`（新） | E-1 由「预设全 True」改为**真跑 5 个用例**（5/5 true） |
| 上下文取证 | `scripts/gates/context_builder.py`（新） | `build_repo_context()` 从产物/git/探针/parquet 抽样取证；`STATIC_GATE_IDS`(14) / `RUN_EVIDENCE_GATE_IDS`(14)；`ci_policy()` |
| pre-push | `scripts/hooks/pre_push.py` | 改用真实 ctx；逃生阀 `FINAI_SKIP_PUSH_GATES=1`（醒目告警 + **落盘** `runs/gate_bypass_audit.jsonl`） |
| CI | `.github/workflows/ci.yml` | Gate Check 4 由空 ctx `--strict` → **`--ci`**；基线名 725→758 |
| **定时全量 CI** | `.github/workflows/scheduled_audit.yml`（新） | `schedule: cron "17 2 * * *"` + `workflow_dispatch`，承接被移出推送期的门禁 |
| 测试 | `tests/test_gate_consistency.py`（新）+ 3 个既有测试文件 | 双向元测试 + 防回退断言；`test_t405` 的反向锁定错误数字已纠正 |

### 门禁归属重构（关键设计）
| 归属 | 数量 | 门禁 |
|---|---|---|
| **推送期**（可静态判定 / 可抽样取证） | 14 | D-1~D-4、E-1、E-2、S-1、G-1~G-4、G-MDD-1、G-DOC-1、G-REF-1 |
| **回测后 `run_post_run_gates`** | 8 | E-3、A-1、A-2、A-4、S-2、S-3、S-5（+G 维复跑） |
| **定时全量 CI** | 8 | D-5、L-1、L-2、L-3、A-3、S-4、G-STRESS-1 |

---

## 2. 验了什么（可复现证据）

### 主理人逐轮独立验证（全部亲手执行）
```
pre-push 真实 ctx:   run_master_gate_guard() → ok=False | 检出 3 项阻断
                     （G-MDD-1 MDD=0.4308 / G-DOC-1 / G-REF-1）—— 仅剩真实缺陷
推送期 SKIP:         0（改造前 23）
门禁状态分布:        真实 ctx → PASS 7 / FAIL 3 / INCONCLUSIVE 18 / SKIP 0
MDD 门禁三态:        43.08% → FAIL ｜ 0 成交 → INCONCLUSIVE ｜ 25.37% → PASS
E-1:                 真跑五必挂 5/5 true；{'foo':1} → INCONCLUSIVE（不再 PASS）
--strict:            exit=1 ｜ --strict --mdd <0成交> → exit=1 ｜ --ci → exit=1
run_post_run_gates(strict=True) 空 ctx → 抛 GateBlockerError [E-1]
gate_consistency --mdd <0成交> → exit=1（原 0，已统一）
元测试:              28 道必能判 FAIL + 28 道必能判 PASS + 违规 ctx 特异性
逃生阀:              写 runs/gate_bypass_audit.jsonl
测试:                连跑两遍 760 passed，runs/ 无残留
母库守卫:            G-3 PASS（finai/sources FINDING- 370 行未动）
镜像一致性:          tamper_guard --verify-mirror PASS，双仓 SHA-256 3a90ee9f...
```
`ci_policy()` 六种情形实测：
```
(1) 仅 run-evidence 门禁 INCONCLUSIVE → blocking=False, warn=[G-STRESS-1]   ← CI 不再永久红
(2) 静态门禁 INCONCLUSIVE             → blocking=True
(3) 有 FAIL                           → blocking=True
(4) 全 PASS                           → blocking=False
(6) D-1 PASS + G-STRESS-1 INCONCLUSIVE→ blocking=False, warn=[G-STRESS-1]
```

### 独立 QA 的贡献（两轮对抗性验证）
- **第一轮**（`qa_verification.md`）：查出 **8 类「不可能失败」兜底** + 证实 pre-push 漏 INCONCLUSIVE + 发现 G-DOC-1 的 Unicode 负号（U+2212）解析 bug
- **终验**（`qa_final_verification.md`）：判定「不可交付」并给出**5 项软性失败**（D-1~D-4/E-2 取证其实可行，QA 实测喂真数据即 PASS）+ 指出元测试**不特异且缺反向断言**（交叉污染 153 对）+ 指出 `runner` 未对齐、`--mdd` 退出码背离
- **结论**：这 5 项软性失败与元测试缺陷已全部修复并复验通过

---

## 3. 还剩什么（已知缺口，如实登记）

| # | 缺口 | 影响 | 处置 |
|---|---|---|---|
| 1 | **3 条真实 FAIL 仍在**（MDD 43.08% / 文档 53 处不符 / 14 处幽灵引用） | pre-push 当前会阻断推送（**这是正确行为**） | M4/M5 修完后自然转绿 |
| 2 | **回测只落 metrics 级产物，无 trade 级明细**（trades / daily_cash_flows / orders） | E-3/A-1/A-2/A-4/S-2/S-3/S-5 在定时 CI 仍为 **WARN-only** | 已在 `scheduled_audit.yml` 注释中**如实登记**；待 M4+ 落盘该 artifact 后无需改门禁 |
| 3 | **飞书告警仍是 stub**（`ops/feishu_alert.py` 不存在） | 定时审计失败**不会实际推送通知**，仅 job 变红 | 已在 workflow 注释显式标注「不伪造已接」，需用户决定是否接真实通道 |
| 4 | `ci_policy()` **无「门禁分类完备性」断言** | 若新增门禁忘记登记进 `STATIC_/RUN_EVIDENCE_GATE_IDS`，其 SKIP/INCONCLUSIVE 会被忽略（当前 14+14=28 完备） | 建议补一条元测试：`STATIC ∪ RUN_EVIDENCE == 全部注册门禁` |
| 5 | `gate_master_audit` **空 ctx 下仍 SKIP:23** | 属设计（无 context 无法判定）；真实判定须走 pre-push / `--ci` 路径 | 已在文档口径上区分，需持续注意不要混用两个口径 |
| 6 | 元测试的 6 道「关联 FAIL」为**自声明** | 特异性未完全收紧 | 可后续逐条构造隔离 ctx |
| 7 | **G-DOC-1「所引产物不存在」23 处** | 动量/T304/T305/phase35 的回测结论**在 `experiments/runs/` 无任何机读产物** | ⚠️ 这是新发现的上游问题：当初「动量→红利」的转向本身建立在无产物的数字上。需在 M4 登记 |
| 8 | `accounting/` 为 0 字节空包、`data/daily_bars/` 仅 1 文件 | README 描述失实 / 动量脚本必然「数据不足」 | 归 M4 文档统一 |

---

## 4. Git 处置建议（需你拍板）

**现状**：28 个文件改动 + 4 个新文件，**全部未提交**；`git status` 另有既存的未跟踪 `1.ipynb`。

**建议：分两个提交，本地提交，**暂不推送****
1. `docs(compliance): 标注失实材料作废 + Phase 4 状态统一为暂停 + 权威仓 TK-30 更正`（纯文档，低风险）
2. `feat(gates): 门禁 P0 落地——SKIP/INCONCLUSIVE 语义、4 道一致性门禁、去 15 处兜底、真实 ctx、双向元测试`（代码）

**为什么不建议现在推送**：pre-push 会因那 3 条真实 FAIL 而阻断（**这是门禁在正确工作，不是故障**）。可选：
- **A（推荐）**：先本地提交；待 M4/M5 把 3 条 FAIL 清零后自然推送成功。**不动逃生阀。**
- **B**：急需推送时用 `FINAI_SKIP_PUSH_GATES=1`，但它会落盘留痕 `runs/gate_bypass_audit.jsonl`——**请留意这笔记录本身就是"未经门禁保护"的证据**。

---

## 5. 下一步（按依赖顺序，未开工）

| 里程碑 | 内容 | 前置 |
|---|---|---|
| **M4 文档口径统一** | 全仓文档数字改为从 `experiments/runs/*.json` 重算；G-DOC-1 / G-REF-1 **转绿**；登记 23 处「所引产物不存在」 | M3 ✅ |
| **M5 合规包重写** | 按产物重写 `docs/compliance/` 全套（含 2 处虚假方法论）；撤销冻结标记 | M4 |
| **M2 复现性修复** | 依 [`repro_root_cause.md`](repro_root_cause.md)：新增 `reporting/provenance.py`、`RunRecord` 加 `code_hash/data_hash/calendar_hash/universe_hash/repro_fingerprint/schema_version`；历史 4 份产物标 `LEGACY::` | M3 ✅ |
| **M6 策略 A · 归因实验** | 拆解 MA200 空仓 vs 过滤器空仓；参数敏感性。**先归因，后改参数** | M3 ✅（可提前） |
| **M7/M8** | 修满仓+降频 / ETF 增强验证 | M6 |
| **M9/M10** | Phase 4 准入重审 / 重启计时 | M5 + (M7 或 M8) |

---

## 6. 本次协作最重要的方法论收获

1. **不采信实现方自述**。工程师报「三处已修」，实测为 **1 通过 / 2 未达标 / 另 2 处新问题**；其报的 G-DOC-1「50 处」实为 `metrics[:50]` **截断值**，真值 89。**每条结论都必须能被一条命令复现。**
2. **每加深一层验证，就在新一层发现同一个病**：SKIP 放行 → INCONCLUSIVE 放行 → 门禁类内部恒过 → 另一个 CLI 退出码背离 → runner 未对齐。根因是**这套门禁体系此前从未被真正验证过**。
3. **元测试是终结「打地鼠」的正确武器**。逐条修是治标；「逐门禁双向锁定（必能 FAIL + 必能 PASS）」才能让恒过/恒不过门禁永久无法回归。
4. **「移交给 X」必须先验证 X 存在**。本轮查出「归属定时全量 CI」而 CI 里**没有 schedule** —— 与旧仓教训 5（ROADMAP 引用不存在的 `specs/009`）**同源**。这个项目两次栽在同一件事上。
5. **修复代码本身也会带上同一个病**。`--strict` 刚确立「展示与退出码不得背离」，`gate_consistency --mdd` 立刻违反了它。**修复者必须接受与被审计对象同样的验证强度。**
