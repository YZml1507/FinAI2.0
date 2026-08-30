# FinAI2.0 · 数据层工作指令（替代旧版 DATA_LAYER_DESIGN_PRINCIPLES.md）

> **删除声明**：旧版 `DATA_LAYER_DESIGN_PRINCIPLES.md`（2026-08-17/23/25，旧仓未调研时期 AI 代笔，含 FINDING-xxx 台账引用与旧仓现态快照）经用户 2026-08-29 判定为无效输入，已从本仓删除（归档区留有原件）。
> **本文效力**：本仓数据层工作的**唯一指令来源**是 research-finai 调研仓的 spec 三件套 + 本文件的母库工程约束。两个来源谁管什么，见下表。

---

## 1. 指令来源分工（谁是权威）

| 层面 | 权威文件 | 管什么 |
|---|---|---|
| **做什么、验收标准** | `D:\Projects\research-finai\specs\001-a-stock-longonly-daily-quant\`（spec / plan / tasks）+ constitution | 系统范围、数据层 FR-DATA 需求、Phase 0-6 门禁、验收判据 |
| **用什么调研依据** | research-finai 00-16 号文档（尤其 12 号数据源实测、13 号回测框架、11 号监管） | 数据源选型（baostock 主 + 新浪/腾讯校验）、复权实测结论、停牌脏行等一手实测 |
| **母库怎么用、工程红线** | 本文 §3-§4 | 860 接口母库的布局约束、复权口径、凭据、探针产物依赖 |

⛔ 旧版文档的「P1-P6 原则 / 终态判据 / FINDING 台账 / 完成度 40%/60%」**全部作废**。母库取数能力照用，但旧仓自定的目标体系不继承。

---

## 2. 本仓数据层任务（来自 spec tasks，唯一口径）

见 `research-finai\specs\...\tasks.md` Foundational 段（T101-T110）：

- T101-T103：Phase 0 环境与 12 号 §9-A 结论复现（停牌脏行 / 复权一致性 / 公告日 / 增量冒烟）
- T104 数据字典；T105 日线采集器（baostock 主 + 新浪/腾讯校验）；T106 停牌/涨跌停/除权清洗；T107 财务 pubDate 对齐；T108 股票池回放；T109 增量更新；T110 三源验收

**范围纪律**：v1 只做日线 + 季频财报 + 股票池，spec §2.2 明确不做两融/资金流/龙虎榜等信息轴。旧仓那套「7 条信息轴终态」不执行。

---

## 3. 母库工程红线（从旧仓实测抢救出的硬约束，依然有效）

这些是**代码本身的实测行为**，不是旧仓目标体系，母库照用就必须遵守：

1. **布局约束**：`finai/` 必须在仓库根下两层、`scripts/` 必须是兄弟目录（`segmented_pull.py:380` 无条件导入 `scripts/auto_probe_interfaces.py` 并注入 `sys.path`）。
2. **产物依赖**：`catalog_source.py:58-59` 读 `artifacts/interface_matrix/auto_probe_results.json`；`overseas_registry.py:32-33` 读 `interfaces_raw.json`。删掉即挂。
3. **复权口径（最贵陷阱）**：`akshare adjust=''`=不复权，`efinance fqt=1`=前复权，`mootdx/tdxpy` 无参数=不复权。**禁止默认调用**，跨源合并前先比对口径；落盘列含 `adjust_mode`。详见 `REVALIDATE.md` R4。
4. **停牌脏行**：baostock 返回 OHLC=前收的前值填充行，须过滤（R1 / 12 号 §9-A ②同源实测）。
5. **静默截断**：TDX 腿返回行数 < 请求行数时无告警，必须校验覆盖率（R2）。
6. **凭据**：统一走 `finai/credentials.py` 常量，⛔ 不写字符串字面量；密钥原件在 `_archive\FinAI_20260829\env\`。

---

## 4. 已知缺陷登账（R1-R5，修母库时处理）

见 `REVALIDATE.md`。修复顺序建议：R4（复权口径）→ R1（停牌脏行）→ R2（截断校验）→ R3（东财可达性复测，与 12 号附录 A.5 同项）→ R5（TDX 腿二选一：砍腿或裁剪 data_catalog）。

---

## 5. 修订规则

本文替代旧版设计文档；后续修订直接改本文并在下方记录。

| 日期 | 修订 | 依据 |
|---|---|---|
| 2026-08-29 | 删除旧版 DATA_LAYER_DESIGN_PRINCIPLES.md（用户判定：未调研时期 AI 乱写）；以 spec 三件套 + 本文接管指令来源 | 用户指令（C 选项对话） |
