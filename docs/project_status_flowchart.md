# FinAI2.0 · 项目状态流程图（2026-09-10 22:50 更新）

> 渲染器：支持 mermaid 的 Markdown 查看器（VS Code / Obsidian / GitHub）
> 数据来源：本会话真实命令输出（**非沙箱环境**，`py -3.11`）+ research-finai spec 三件套
> 交互版见：`docs/project_status_flowchart.html`
> ⚠️ 口径纪律：**门禁数量与单测基线一律以单一事实源为准**（`scripts/gates/constants.py` + `gate_master_audit.py` 注册表），⛔ 文档不硬编码；历史快照行以 `gate-doc-ignore` 标注
> ⚠️ 环境坑（会误判）：**沙箱内 `pyarrow` 不可见** ⇒ 全量测试**假报 35 failed**（非回归）、`--ci` 会把 D-1~D-4 从 PASS 误降为 INCONCLUSIVE。跑测试/门禁请用**非沙箱环境**（`py -3.11`）

---

## 一、仓库 / 基建总览

```mermaid
flowchart TD
  subgraph REPO["双仓（Private）"]
    F["代码仓 FinAI2.0<br/>HEAD 以 git log -1 为准（⛔ 不硬编码）<br/>领先 origin 提交数以 git rev-list 为准（⛔ 不硬编码）<br/>⛔ 未 push<br/>origin=github.com/YZml1507/FinAI2.0"]
    R["计划仓 research-finai<br/>HEAD fae9c44 ✅<br/>origin=github.com/YZml1507/research-finai"]
    F -->|镜像逐字节一致| R
    F -->|SHA-256 c89d07b84514b5a2…（两仓相同）| R
  end
  DEL["旧仓 FinAI<br/>已删除 2026-08-29"]
  TASK["10 个 FinAI_* 计划任务<br/>已禁用 ✅"]
  DEL -.->|仅抢救 finai/sources 860 接口| F
  TASK -.->|不复用旧命名| F
  style F fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style R fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style DEL fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style TASK fill:#dcfce7,stroke:#16a34a,color:#0f172a
```

---

## 二、⚠️ 2026-09-10 全仓审计：项目真实状态被推翻并重建

```mermaid
flowchart TD
  AUDIT["全仓矛盾点审计（只读）<br/>docs/audit/consistency_audit.md<br/>20 条矛盾 + N1–N10"] --> P0_1["P0-1 同一回测三套互斥指标<br/>其中两套全仓无出处（编造）"]
  AUDIT --> P0_2["P0-2 ⛔ 最致命：门禁空转<br/>实测 PASS:1 / SKIP:23 却打印「全绿」<br/>S-1~S-5/G-1/G-2 硬编码恒过"]
  AUDIT --> P0_3["P0-3 G4.5 假通过<br/>用 0 成交区间得 MDD=0.00% 判「<35% 通过」<br/>真值 43.08% 从未被校验"]
  AUDIT --> P0_4["P0-4 Phase 4 状态自相矛盾<br/>「6 个月跟踪」实为 1 天 0 成交空跑"]
  AUDIT --> P0_5["P0-5 同一 params_hash 4 份产物 3 种结果<br/>code_version 是手写标签"]
  P0_1 --> VERDICT
  P0_2 --> VERDICT
  P0_3 --> VERDICT
  P0_4 --> VERDICT
  P0_5 --> VERDICT["⚠️ 判定：<br/>工程引擎可信（账本/撮合/费率/红利税 FIFO/数据口径）<br/>⛔ 门禁体系、叙事文档、合规包、准入链必须重做"]
  VERDICT --> ROUTE["路线决策（roadmap_decision.md）<br/>C 治理重做 + 止血 → A 归因实验 → A 失败切 B"]
  VERDICT --> USER["用户拍板 ✅<br/>① 报备材料尚未递交券商 ⇒ 无已发生法律风险<br/>② 接受 C 为 A/B 强制前置<br/>③ 按推荐路线执行"]
  style AUDIT fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style P0_1 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style P0_2 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style P0_3 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style P0_4 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style P0_5 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style VERDICT fill:#fef3c7,stroke:#f59e0b,color:#0f172a
  style ROUTE fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style USER fill:#dcfce7,stroke:#16a34a,color:#0f172a
```

---

## 三、治理层重做（M1–M4′ ✅ 已完成）

```mermaid
flowchart LR
  M1["M1 止血 ✅<br/>6 份失实材料加「作废·待重写」横幅<br/>（原文数字一字未删）<br/>README Phase4 自相矛盾真修"] --> M2["M2 复现性修复 ✅<br/>reporting/provenance.py<br/>RunRecord 加 code/data/calendar/universe hash<br/>缺失 ⇒ 显式 None（⛔ 不静默兜底）"]
  M2 --> M3["M3 门禁 P0 落地 ✅<br/>INCONCLUSIVE 语义 + 4 道新门禁<br/>清 15 处「无证据即通过」兜底<br/>门禁异常 ⇒ 判 FAIL"]
  M3 --> M4["M4′ 文档与合规包口径统一 ✅<br/>真实违规 29→0 ｜ 幽灵引用 11→0<br/>幽灵风控声明 5 处全清<br/>2 处虚假方法论按代码改正"]
  M4 --> M5["M5 合规包重写 ✅（并入 M4′）<br/>T405 全篇「审签通过」清零后按实证恢复"]
  M1 --> TK["权威仓 append-only ✅<br/>TK-30 / TK-31 / TK-32 更正登记<br/>镜像逐字节一致（--verify-mirror PASS）"]
  style M1 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style M2 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style M3 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style M4 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style M5 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style TK fill:#dcfce7,stroke:#16a34a,color:#0f172a
```

---

## 四、门禁体系现状（从「空转装饰」到「真拦」）

```mermaid
flowchart TD
  BEFORE["⛔ 改造前<br/>空 ctx：PASS:1 / SKIP:23 + 打印「全绿」<br/>⛔ 不存在 MDD 门禁、不存在文档↔产物门禁<br/>⛔ 至少 9 道门禁结构上不可能失败"] --> AFTER["✅ 改造后<br/>门禁数量以 gate_master_audit.py 注册表为单一事实源<br/>空 ctx：如实报 SKIP / INCONCLUSIVE<br/>真实 ctx：SKIP = 0"]
  AFTER --> L1["回测/测量期<br/>report-only（不阻断）<br/>『测量仪器不能因测出坏结果而停机』"]
  AFTER --> L2["推送期（pre-push）<br/>静态可判门禁阻断<br/>G-MDD-1 降为 WARN（展示不阻断）<br/>当前 OK = True"]
  AFTER --> L3["准入期（--acceptance）<br/>⛔ BLOCKER<br/>验签 + schema + status + 指标健全 + 阈值<br/>MDD 43.08% ⇒ exit=1"]
  AFTER --> L4["定时全量审计（--scheduled）<br/>run 证据缺失也阻断<br/>cron 每日 02:17 UTC"]
  L3 --> ADOPT["采纳登记（adoption.py）<br/>experiments/acceptance/ADOPTED.json<br/>空登记 ⇒ 无操作放行（⛔ 不让 CI 永久红）"]
  style BEFORE fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style AFTER fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style L1 fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style L2 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style L3 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style L4 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style ADOPT fill:#dcfce7,stroke:#16a34a,color:#0f172a
```

### 门禁归属（29 道，完备无重叠）

| 归属 | 数量 | 门禁 |
|---|---|---|
| **推送期（STATIC）** | 12 | D-1~D-4、E-1、E-2、G-1~G-4、G-DOC-1、G-REF-1 |
| **需 run 证据（RUN_EVIDENCE）** | 17 | A-1~A-4、D-5、E-3、G-MDD-1、G-REPRO-1、G-STRESS-1、L-1~L-3、S-1~S-5 |

> 真实 ctx 下：**PASS 14 / FAIL 1 / INCONCLUSIVE 14 / SKIP 0**；唯一 FAIL = `G-MDD-1`（MDD 43.08% > 35%，**真实策略缺陷**）

---

## 五、⭐ A 路策略病因（本轮最重要产出 · 全部零成本函数级复算）

```mermaid
flowchart TD
  SYMPTOM["已观测现象（诊断 md，⛔ 尚无产物支持）<br/>日均持仓 0.5–1.8 只（目标 5）<br/>零持仓日 54.7% ｜ 平均现金 65.4%<br/>后期比前期更差"] --> H5["H5 市值加权 × 单票 2 万下限 × 无再分配<br/>⇒ 断崖式丢弃<br/>选 5 只只建 1–3 只"]
  SYMPTOM --> H5B["H5b 死价位带 [151,199]（等权 base=30000）<br/>⇒ 无法持有中等价位标的<br/>占 20–300 区间 17.4%"]
  SYMPTOM --> H5C["H5c ⭐ 死带随 NAV 退化的正反馈螺旋<br/>越亏 ⇒ base 越小 ⇒ 死带越宽<br/>⇒ 能建的越少 ⇒ 现金越多 ⇒ 越难回本"]
  SYMPTOM --> H41["㊶ default_positions 与 target_count 隐性双口径<br/>N = min 二者 ｜ ⛔ 无跨配置校验"]
  H5C --> CRIT["临界点（硬数学）<br/>base &lt; min_position_value(20000) ⇒ 全价位丢弃<br/>15 万本金等效触发线 NAV &lt; 100,000<br/>实际终值 108,421 ⇒ 死带已达 81.1%"]
  H5B --> MISMATCH["⚠️ 参数意图错位<br/>max_price=300 声称防「整手失真」<br/>而真正失真区间 [151,199] 全在 300 之下<br/>⇒ 该过滤器没覆盖它想防的问题"]
  H41 --> TRAP["⚠️ 陷阱<br/>抬 default_positions 到 8（意图「多持几只」）<br/>⇒ base=18750 &lt; 20000 ⇒ 恒失效<br/>⇒「想多买」的改动导致「完全买不了」"]
  H4["H4 min_positions 只校验不执行<br/>契约下限 3 运行时零约束<br/>⇒ 没有任何机制把持仓拉回 3"] --> SYMPTOM
  style SYMPTOM fill:#fef3c7,stroke:#f59e0b,color:#0f172a
  style H5 fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style H5B fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style H5C fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style H41 fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style H4 fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style CRIT fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style MISMATCH fill:#fef3c7,stroke:#f59e0b,color:#0f172a
  style TRAP fill:#fef3c7,stroke:#f59e0b,color:#0f172a
```

### 死带随 NAV 退化（15 万本金 / 目标 5 只，实测）

| NAV | base | 死带区间 | 占 20–300 区间 |
|---|---|---|---|
| 150,000 | 30,000 | [151, 199] | 17.4% |
| 130,000 | 26,000 | [66, 300] | 43.8% |
| 110,000 | 22,000 | [28, 300] | 77.2% |
| 100,000 | 20,000 | [21, 300] | 97.9% |
| 90,000 | 18,000 | [20, 300] | **100%（完全失效）** |

> 精确丢弃条件：`planned = 100·p·floor(base/(100p)) < min_position_value`
> **结论强度**：以上均为**路径 A 影子复算**（零成本）⇒ 只证「计划层会切断」，⛔ **不得据此宣称「收益会改善」**

---

## 六、Phase 3.5 红利策略执行现状

```mermaid
flowchart LR
  T309["T309 红利税真集成 ✅<br/>FIFO 扣税 + 拆股因子适配 + 流水入账"] --> T310["T310 跳空缺口滑点 ✅<br/>26 单测全绿"]
  T310 --> T311["T311 红利策略实现 ✅<br/>市值加权生效 + MA200 择时（指数 sh.000300）"]
  T311 --> T313["⛔ T313 压力测试（结论已推翻）<br/>0 成交空测，不构成压力测试<br/>原「G4.5 防守通过」已撤回"]
  T311 --> T312["T312 真实 10 年全周期回测<br/>Run 20260907-150402（唯一带签名权威产物）<br/>CAGR −3.20% / 总收益 −27.72% / 换手 201.14%<br/>胜率 28.21% / 往返 78 笔 / 红利税 5,043.75 元<br/>⛔ MDD 43.08% 超 35% 上限（G-MDD-1 FAIL）<br/>净值 150,000 → 108,421.14"]
  T312 --> AUDIT["防伪审计工具（仅凭据面）<br/>audit_evidence_integrity.py 5/5 PASS"]
  AUDIT --> G45["G4.5 / Phase 4 状态<br/>⛔ ⏸ 暂停（待策略 v2 落地后重审）<br/>准入层 --acceptance ⇒ exit=1（真实拦住）"]
  style T309 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style T310 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style T311 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style T313 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style T312 fill:#fef3c7,stroke:#f59e0b,color:#0f172a
  style AUDIT fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style G45 fill:#fee2e2,stroke:#dc2626,color:#0f172a
```

**唯一事实源铁律**：`experiments/runs/*.json`（带 `anti_tamper_signature`）。
仓内任何 md 的结论性数字一律**不可引用**。

---

## 七、测试基线演进

```mermaid
flowchart LR
  B0["Phase 0 起点<br/>19 passed"] --> B1["Phase 1 数据层<br/>162 passed"]
  B1 --> B2["Phase 2 回测引擎<br/>394 passed"]
  B2 --> B3["Phase 3 策略层<br/>435 passed"]
  B3 --> B35["Phase 3.5 红利切换<br/>524 passed"]
  B35 --> B402["T402 偏差容忍带<br/>616 passed"]
  B402 --> B404["T401/403/404 修复与台账<br/>619 passed"]
  B404 --> B312_OLD["T312 离线测试补充<br/>626 passed"]
  B312_OLD --> B312_NOW["T312 审计与红利税/拆股加权修复<br/>629 passed"]
  B312_NOW --> GATES1["阶段一：六维防御门禁工具包<br/>23 项门禁 + 52 单测<br/>681 passed<!-- gate-doc-ignore: 阶段一历史快照（23 项门禁 / 681 passed 为当时值），⛔ 不改史 -->"]
  GATES1 --> GATES2["阶段二：执行流前置/后置闸门植入<br/>runner.py + 18 集成单测<br/>699 passed"]
  GATES2 --> GATES3["阶段三：CI / Git Hooks 硬化与验签<br/>tamper_guard + 24 门禁 + 18 单测<br/>717 passed"]
  GATES3 --> P4_OLD["Phase 4 准入基建与 T405 报备<br/>725 passed"]
  P4_OLD --> M3_NOW["M3 门禁 P0 落地<br/>760 → 786 passed"]
  M3_NOW --> M2_NOW["M2 复现性 + 三层分层<br/>796 → 828 → 850 passed"]
  M2_NOW --> CUR["M4′ + ㉜–㊴ 收口 ✅（当前）<br/>单测基线真值以单一事实源为准<br/>（scripts/gates/constants.py::TEST_BASELINE_PASSED）<br/>⛔ 文档不硬编码"]
  style B0 fill:#e0e7ff,stroke:#818cf8
  style B1 fill:#e0e7ff,stroke:#818cf8
  style B2 fill:#e0e7ff,stroke:#818cf8
  style B3 fill:#e0e7ff,stroke:#818cf8
  style B35 fill:#e0e7ff,stroke:#818cf8
  style B402 fill:#e0e7ff,stroke:#818cf8
  style B404 fill:#e0e7ff,stroke:#818cf8
  style B312_OLD fill:#e0e7ff,stroke:#818cf8
  style B312_NOW fill:#e0e7ff,stroke:#818cf8
  style GATES1 fill:#e0e7ff,stroke:#818cf8
  style GATES2 fill:#e0e7ff,stroke:#818cf8
  style GATES3 fill:#e0e7ff,stroke:#818cf8
  style P4_OLD fill:#e0e7ff,stroke:#818cf8
  style M3_NOW fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style M2_NOW fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style CUR fill:#dcfce7,stroke:#16a34a,color:#0f172a
```

---

## 八、当前位置与下一步

```mermaid
flowchart TD
  NOW["📍 当前位置（2026-09-10）<br/>治理层 M1–M4′ 全部闭环 ✅<br/>门禁 29 道 · SKIP 0 · 准入层真拦<br/>HEAD 与领先提交数以 git 为准（⛔ 不硬编码）"] --> GATE_OK["门禁四层行为 ✅<br/>pre-push OK=True ｜ --ci exit=0<br/>准入(超限) exit=1 ｜ --scheduled exit=1"]
  NOW --> M6_DESIGN["M6 归因设计 ✅ 已完成（只读）<br/>docs/audit/m6_attribution_design.md<br/>13 环节漏斗 + 生效性四分类<br/>C-01 产物 schema 扩展规格"]
  NOW --> PENDING["⏳ 待用户拍板<br/>① 是否接受当前单测基线并 push<br/>② M6 是否即刻开工"]
  M6_DESIGN --> STEP1["M6 第 1 步（建议先做）<br/>路径 A 影子复算 · ⛔ 零成本<br/>等权 vs 市值加权 ｜ 价位带敏感性<br/>死带随 NAV 演化"]
  STEP1 --> STEP2["第 2 步：C-01 扩展产物 schema<br/>metrics.positioning + nav_curve<br/>（并纳入签名域）"]
  STEP2 --> STEP3["第 3 步：用真实产物复算<br/>给出逐年死带占比曲线<br/>⇒ 验证 H5c 是否已启动"]
  STEP3 --> STEP4["第 4 步：仅在 1–3 显著改善后<br/>才做路径 B（真实回测对照）"]
  STEP4 --> M7["M7 策略修法评估（⛔ 本轮不实施）<br/>min_position_value 改与 base 联动 / 手数下限<br/>㊶ 双口径一致性校验 / min_positions 补足逻辑"]
  M7 --> PHASE4["达标后重启 Phase 4 准入"]
  style NOW fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style GATE_OK fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style M6_DESIGN fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style PENDING fill:#fef3c7,stroke:#f59e0b,color:#0f172a
  style STEP1 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style STEP2 fill:#f1f5f9,stroke:#94a3b8,color:#64748b
  style STEP3 fill:#f1f5f9,stroke:#94a3b8,color:#64748b
  style STEP4 fill:#f1f5f9,stroke:#94a3b8,color:#64748b
  style M7 fill:#f1f5f9,stroke:#94a3b8,color:#64748b
  style PHASE4 fill:#f1f5f9,stroke:#94a3b8,color:#64748b
```

---

## 九、⚠️ 已知缺口（如实登记，未解决）

| # | 缺口 | 性质 | 归属 |
|---|---|---|---|
| 1 | `G-MDD-1` FAIL（MDD 43.08% > 35%） | **真实策略缺陷**，非门禁缺陷 | M7 策略改进后自然转绿 |
| 2 | 回测仅有 metrics 级产物，**无 trade 级明细** | 定时 CI 中 A/S 维共 7 项仍 WARN | M4+ 落盘该 artifact |
| 3 | **飞书告警仍是 stub**（`ops/feishu_alert.py` 未实现） | 定时审计失败仅让 job 变红 | 需用户决定是否接真实通道 |
| 4 | H1–H5c **全部为待验证假设** | M6 尚未执行 | 见 §八 |
| 5 | `nav_curve` **内存有、未落盘** | 阻断 H5c 时间演化验证 | 已纳入 C-01 规格 |
| 6 | 诊断 md 的持仓/现金三数**无产物支持** | 证据链第一环靠人眼 | C-01 落地后闭环 |
| 7 | 单票集中度上限 / ST 剔除 **均未实现** | 文档已如实标注为待办 | M7 评估 |
| 8 | `accounting/` 为 0 字节占位包 | 双账本实在 `backtest/ledger.py` | M4 文档已如实描述 |

---

## 十、更新历史

| 日期 | 内容 |
|---|---|
| 2026-09-07 | T312 硬伤根治 + 审计 5/5 + 10 年回测（CAGR −3.20%）+ 门禁三阶段 + Phase 4 准入基建 |
| 2026-09-10 上午 | Colab 云端验收 + T312 全周期诊断（P0 仓位不足）+ **Phase 4 暂停** |
| 2026-09-10 下午 | ⚠️ **全仓矛盾点审计**：门禁空转被揭穿（23 SKIP 仍报全绿）+ 编造数字 + 合规包失实；用户拍板 C→A→B |
| 2026-09-10 晚 | **M1 止血 / M2 复现性 / M3 门禁 P0 / M4′ 口径统一** 全部完成；门禁 29 道、SKIP 0、准入层真拦；新增 H5/H5b/H5c 病因发现；M6 归因设计完成（只读） |
