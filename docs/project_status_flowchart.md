# FinAI2.0 · 项目状态流程图（2026-08-31 复核）

> 渲染器：支持 mermaid 的 Markdown 查看器（VS Code / Obsidian / GitHub）
> 数据来源：本会话真实命令输出 + research-finai spec 三件套
> 交互版见：`docs/project_status_flowchart.html`

## 仓库 / 基建总览

```mermaid
flowchart TD
  subgraph REPO["双仓（Private）"]
    F[代码仓 FinAI2.0<br/>HEAD 3fcfeaf ✅] 
    R[计划仓 research-finai<br/>HEAD 56a511b ✅]
    F -->|git fetch research| R
    F -->|docs/spec 快照| R
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
  G0[G0 spec 三件套齐备 ✅] --> R15[R1–R5 清零 ✅ af20d85] --> UT[19 离线单测全绿 ✅ 2026-08-31]
  UT --> P1[Phase 1 数据层<br/>T104 → T105 → … → T110 当前]
  P1 --> G2[G2 三源验收]
  style G0 fill:#dcfce7,stroke:#16a34a
  style R15 fill:#dcfce7,stroke:#16a34a
  style UT fill:#dcfce7,stroke:#16a34a
  style P1 fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style G2 fill:#f3f4f6,stroke:#9ca3af,color:#0f172a
```

## Phase 1 数据层（当前工作带）

```mermaid
flowchart LR
  T101[T101 环境清单 ✅] --> T102[T102 push2his 可达性 ✅]
  T102 --> T103[T103 四项结论复现 ✅]
  T103 --> T104[T104 数据字典 v1 ⚠️ 未勾选]
  T104 --> T105[T105 日线采集器]
  T105 --> T106[T106 停牌/涨跌停/除权清洗]
  T105 --> T107[T107 财务 pubDate 对齐]
  T105 --> T108[T108 股票池/成分回放]
  T106 --> T109[T109 增量更新 + 5 日冒烟]
  T107 --> T109
  T108 --> T109
  T109 --> T110[T110 数据层验收 → G2]
  style T101 fill:#dcfce7,stroke:#16a34a
  style T102 fill:#dcfce7,stroke:#16a34a
  style T103 fill:#dcfce7,stroke:#16a34a
  style T104 fill:#fef9c3,stroke:#f59e0b
  style T105 fill:#f3f4f6,stroke:#9ca3af
  style T106 fill:#f3f4f6,stroke:#9ca3af
  style T107 fill:#f3f4f6,stroke:#9ca3af
  style T108 fill:#f3f4f6,stroke:#9ca3af
  style T109 fill:#f3f4f6,stroke:#9ca3af
  style T110 fill:#f3f4f6,stroke:#9ca3af
```

> ⚠️ **依赖缺口**：tasks.md 把 T104 列为 T105 的显式前置（`T105 ... （FR-DATA-7；T101/T104）`），但 T104 至今未勾选。严格按依赖序，Phase 1 的第一步是 **T104 数据字典**，随后才是 T105。

## Phase 2–6 路线（未解锁）

```mermaid
flowchart LR
  P2[Phase 2 回测引擎<br/>T201–T207 / G3] --> P3[Phase 3 策略与组合<br/>T301–T305 / G4]
  P3 --> P4[Phase 4 模拟盘 ≥6 个月<br/>T401–T406 / G5 前半]
  P4 --> P5[Phase 5 小额实盘<br/>T501–T505 / G6]
  P5 --> P6[Phase 6 运营迭代<br/>T601–T605]
  P5 -.->|T501 用户书面确认| USER[用户确认]
  style P2 fill:#f3f4f6,stroke:#9ca3af
  style P3 fill:#f3f4f6,stroke:#9ca3af
  style P4 fill:#f3f4f6,stroke:#9ca3af
  style P5 fill:#fee2e2,stroke:#dc2626
  style P6 fill:#f3f4f6,stroke:#9ca3af
  style USER fill:#fee2e2,stroke:#dc2626
```

## 待用户确认决策点

| 任务 | 内容 | 阻塞？ |
|---|---|---|
| **T001** | 告警通道（用既有渠道，不注册新账号）→ 写 `.specify/memory` | 阻塞 Phase 1 之前全部 |
| **T501 / P-5.0** | 用户书面确认实盘（券商 / 金额 / 日期） | 远期，2027 年段 |

## 复核摘要（2026-08-31）

| 项目 | 结果 | 证据 |
|---|---|---|
| 代码仓 HEAD | ✅ `3fcfeafdb6…` | 本地 `git rev-parse HEAD` |
| 计划仓 HEAD | ✅ `56a511b3b0…` | 本地 `git rev-parse HEAD` |
| 远端默认分支 == 本地 | ✅ 一致 | `git ls-remote origin HEAD` 输出相同哈希 |
| GitHub Private | ✅ 两仓均 Private | 凭证凭据（user=YZml1507）+ API 返回 `private=true`；公开 404 |
| 旧仓 FinAI | ✅ 已删除 | `Test-Path D:\Projects\FinAI = False` |
| 10 个计划任务 | ✅ 全部 Disabled | `Get-ScheduledTask` 输出 |
| FINDING 台账 | ✅ 370 行 | `Select-String -Pattern "FINDING-"` |
| 红线路径 | ✅ 7/7 存在 | `Test-Path` 逐个核对 |
| 占位包 6 个 | ✅ 存在 | `accounting/backtest/ops/reporting/strategy/data` |
| 离线单测 | ✅ 19 passed | `py -3.11 -m pytest tests/ …` |
| 测试产物残留 | ⚠️ `.pytest_cache` 存在 | 用完即删纪律未执行 |
| research-finai 工作树 | ⚠️ `.gitignore` 一处未提交修改 | 补 `.claude` 忽略 + 去行尾注释 |

---

**维护者备注**：流程图按 CLAUDE.md §5 路线图与 tasks.md 依赖序绘制；节点状态色标：绿=完成、蓝=当前、黄=部分/有条件、灰=待启动、红=阻塞/需用户确认。
