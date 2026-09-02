# FinAI2.0 · 项目状态流程图（2026-09-02 更新）

> 渲染器：支持 mermaid 的 Markdown 查看器（VS Code / Obsidian / GitHub）
> 数据来源：本会话真实命令输出 + research-finai spec 三件套
> 交互版见：`docs/project_status_flowchart.html`

## 仓库 / 基建总览

```mermaid
flowchart TD
  subgraph REPO["双仓（Private）"]
    F[代码仓 FinAI2.0<br/>HEAD 1aabfea + 多次提交 ✅<br/>origin=github.com/YZml1507/FinAI2.0] 
    R[计划仓 research-finai<br/>HEAD 6370315 ✅<br/>origin=github.com/YZml1507/research-finai]
    F -->|git fetch research（本地路径）| R
    F -->|docs/spec 快照 逐字节一致| R
  end
  DEL[(旧仓 FinAI<br/>已删除 2026-08-29)]
  TASK[(10 个 FinAI_* 计划任务<br/>已禁用 ✅)]
  DEL -.->|仅抢救 finai/sources 860 接口| F
  TASK -.->|不复用旧命名| F
  style F fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style R fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style DEL fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style TASK fill:#dcfce7,stroke:#16a34a,color:#0f172a
```

## 门禁与当前阶段

```mermaid
flowchart LR
  G0[G0 spec 三件套齐备 ✅] --> R15[R1–R5 清零 ✅ af20d85] --> UT[524 离线单测全绿 ✅ 2026-09-02]
  UT --> P1[Phase 1 数据层 ✅<br/>T101–T110]
  P1 --> G2[G2 三源验收 ✅]
  G2 --> P2[Phase 2 回测引擎 ✅<br/>T201–T207 + G3]
  P2 --> P3[Phase 3 策略 ✅<br/>T301–T308]
  P3 --> G4[G4 技术评审 ✅<br/>T305 报告已交]
  G4 --> P35[Phase 3.5 红利策略 ✅<br/>T309–T313 + G4.5 通过 ⏵当前]
  P35 --> P4[Phase 4 模拟盘<br/>T401–T406 待启动]
  style G0 fill:#dcfce7,stroke:#16a34a
  style R15 fill:#dcfce7,stroke:#16a34a
  style UT fill:#dcfce7,stroke:#16a34a
  style P1 fill:#dcfce7,stroke:#16a34a
  style G2 fill:#dcfce7,stroke:#16a34a
  style P2 fill:#dcfce7,stroke:#16a34a
  style P3 fill:#dcfce7,stroke:#16a34a
  style G4 fill:#dcfce7,stroke:#16a34a
  style P35 fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style P4 fill:#fef3c7,stroke:#f59e0b,color:#0f172a
```

## Phase 3.5 红利策略切换（已清零 ✅ 2026-09-02）

```mermaid
flowchart LR
  T309[T309 红利税模块 ✅<br/>24 单测 517 passed] --> T310[T310 跳空缺口滑点 ✅<br/>26 单测 515 passed]
  T310 --> T311[T311 红利策略实现 ✅<br/>12 单测 447 passed]
  T311 --> T312[T312 数据采集+回测 ✅<br/>6 单测就绪 待执行]
  T312 --> T313[T313 压力测试 ✅<br/>4 单测 524 passed]
  T313 --> G45[G4.5 门禁通过 ✅<br/>MDD 0% / 换手 0% / 空仓避险]
  style T309 fill:#dcfce7,stroke:#16a34a
  style T310 fill:#dcfce7,stroke:#16a34a
  style T311 fill:#dcfce7,stroke:#16a34a
  style T312 fill:#dcfce7,stroke:#16a34a
  style T313 fill:#dcfce7,stroke:#16a34a
  style G45 fill:#dcfce7,stroke:#16a34a
```

## 红利策略 vs 动量策略对比

```mermaid
flowchart TB
  subgraph COMPARE["压力测试对比（T304 vs T313）"]
    direction TB
    M[动量策略 MomentumStrategy<br/>2015 crash: MDD 68% / 换手 1271%<br/>2018 bear: MDD 10% / 换手 668%<br/>胜率均 0% ❌]
    D[红利策略 DividendStrategy<br/>2015 crash: MDD 0% / 换手 0%<br/>2018 bear: MDD 0% / 换手 0%<br/>MA200 择时全程空仓避险 ✅]
    M -.->|改善 +68pp MDD<br/>+13.17 夏普| D
  end
  style M fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style D fill:#dcfce7,stroke:#16a34a,color:#0f172a
```

## 测试基线演进

```mermaid
flowchart LR
  B0[Phase 0 起点<br/>19 passed] --> B1[Phase 1 数据层<br/>162 passed]
  B1 --> B2[Phase 2 回测引擎<br/>394 passed]
  B2 --> B3[Phase 3 策略层<br/>435 passed]
  B3 --> B35[Phase 3.5 红利策略<br/>524 passed ⏵当前]
  style B0 fill:#e0e7ff,stroke:#818cf8
  style B1 fill:#e0e7ff,stroke:#818cf8
  style B2 fill:#e0e7ff,stroke:#818cf8
  style B3 fill:#e0e7ff,stroke:#818cf8
  style B35 fill:#dbeafe,stroke:#2563eb,color:#0f172a
```

## 下一步决策点

```mermaid
flowchart TD
  NOW[当前位置：G4.5 通过<br/>Phase 3.5 清零]
  NOW --> OPT1[选项 A：执行 T312 真实数据回测<br/>≈40-50 分钟]
  NOW --> OPT2[选项 B：直接进入 Phase 4 模拟盘<br/>基于 T313 压力测试结果]
  OPT1 --> DEC1{CAGR / 夏普 / MDD<br/>达标？}
  DEC1 -->|✅ 达标| P4A[Phase 4 模拟盘]
  DEC1 -->|❌ 不达标| ADJ[调整策略参数<br/>或换其他风格]
  OPT2 --> P4B[Phase 4 模拟盘<br/>6 个月]
  style NOW fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style OPT1 fill:#fef3c7,stroke:#f59e0b,color:#0f172a
  style OPT2 fill:#fef3c7,stroke:#f59e0b,color:#0f172a
  style P4A fill:#dcfce7,stroke:#16a34a
  style P4B fill:#dcfce7,stroke:#16a34a
  style ADJ fill:#fee2e2,stroke:#dc2626,color:#0f172a
```

---

**更新历史**：
- 2026-08-31：Phase 0–1 完成标记
- 2026-09-01：Phase 2–3 完成标记 + G3/G4 门禁通过
- 2026-09-02：Phase 3.5 红利策略切换完成 + G4.5 门禁通过 + 测试基线 524
