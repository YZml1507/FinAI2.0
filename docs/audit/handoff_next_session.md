# 交接说明 · 交给新窗口续做（M6 归因实验第 1 步）

> 生成日期：2026-09-10 22:50 ｜ 生成者：主理人齐活林（交付总监）
> 用途：**整段复制给新会话**，使其在无上下文情况下可直接接手

---

## 交接正文（可整段复制）

接手 FinAI2.0（`D:\Projects\FinAI2.0`）的 **M6 策略归因实验第 1 步（路径 A 影子复算）**。这是一个 A 股长仓日线量化系统（本金 10~15 万、持仓目标 3~8 只、5 元佣金地板），刚完成治理层重做（M1 止血 / M2 复现性 / M3 门禁 P0 / M4′ 口径统一），**唯一事实源是 `experiments/runs/*.json`（带 `anti_tamper_signature`），仓内任何 md 的结论性数字一律不可引用**；当前 HEAD `55e7266`，**本地领先 origin 14 个提交且未 push**，单测基线 **876**（真值只认 `scripts/gates/constants.py::TEST_BASELINE_PASSED`，⛔ 文档不硬编码），门禁 **29 道**（真实 ctx：PASS 14 / FAIL 1 / INCONCLUSIVE 14 / SKIP 0；唯一 FAIL = `G-MDD-1` MDD 43.08% > 35%，属真实策略缺陷），四层行为 = pre-push OK=True、`--ci` exit=0、`--acceptance` exit=1、`--scheduled` exit=1。**你的任务**：按 `docs/audit/m6_attribution_design.md`（1354 行）§3.1.1 路径 A，用 `strategy/portfolio.py::plan_positions` 做**零成本函数级复算**（⛔ 不改代码、不跑回测、不动参数），验证四条假设——**H5**（市值加权 × 单票 2 万下限 × 丢弃后不再分配 ⇒ 断崖式丢弃，选 5 只只建 1–3 只）、**H5b**（默认配置下死价位带 `[151,199]`，占 20–300 区间 17.4%，且 `max_price=300` 恰好没覆盖它想防的整手失真）、**H5c**（死带随 NAV 退化的正反馈螺旋：越亏 ⇒ `base` 越小 ⇒ 死带越宽 ⇒ 越建不满，15 万本金等效触发线 `NAV < 100,000`）、**㊶**（`default_positions` 与 `target_count` 构成隐性双口径，实际 `N = min(len(signals), target_count)` 且全仓无跨配置校验）；做法是同一批候选分别传 `weights=scores` 与 `weights=None`（市值加权 vs 等权）做对照，并叠加"逐日 `(base_i, close_i)` 配对"算死带占比与价位带敏感性。**硬纪律**：⛔ 不改策略参数或代码（先归因、后改参数）、⛔ 不碰 `finai/sources/`（母库 370 行 `FINDING-` 守卫）、⛔ 不改 `experiments/runs/*.json` 历史产物、权威仓 `research-finai` 只能 **append-only 追加**后再同步镜像（`tamper_guard --verify-mirror` 须 PASS）；**路径 A 的结论强度低于路径 B（真实回测），只证"计划层会切断"，⛔ 不得据此宣称"收益会改善"**。**环境坑（必读）**：沙箱内 `pyarrow` 不可见 ⇒ 会**假报 35 个失败**（非回归），跑测试/门禁必须用**非沙箱环境**（`py -3.11`）；`--ci` 同样依赖 `pyarrow` 做 D 维取样，缺它会把 D-1~D-4 从 PASS 误降为 INCONCLUSIVE。**判据纪律**：任何结论必须能由**一条命令复现**；参数生效性用「合法域取值看输出是否变化」判定；正则用「正例全中 + 反例全排」双向锁定；常量用「== 实际收集数」。**参考文档**：`docs/audit/governance_closure_report.md`（治理层收口总报告）、`docs/audit/m6_attribution_design.md`（M6 设计）、`docs/project_status_flowchart.md`（当前状态流程图）、`docs/audit/roadmap_decision.md`（路线决策 C→A→B）。

---

## 附：四条假设的实测证据（交接方已复现，可作起点）

### 精确丢弃条件（主公式）

```python
# strategy/portfolio.py
shares  = int(value / close) // cfg.lot_size * cfg.lot_size   # 整手向下取整
planned = close * Decimal(shares)
若 planned < cfg.min_position_value(20000) ⇒ 丢弃，⛔ 且不再分配给其余标的
⇒ 等价闭式：planned = 100·p·floor(base/(100p)) < 20000
```

### H5 市值加权断崖（本金 15 万 / 5 只候选 / 10 元股）

| 权重分布 | 实际建仓 | 留存现金 |
|---|---|---|
| 等权 1:1:1:1:1 | 5 只 | 0 |
| 温和不均 3:2:2:1:1 | 3 只 | 34,000 |
| 头部集中 10:1:1:1:1 | 1 只 | 43,000 |

> ⚠️ 触发量是 **top-5 内的市值离散度**，⛔ 不是「小市值」本身（等权时零丢弃）

### H5b 死价位带（等权 base=30000）

| close | 建仓结果 | 整手后金额 |
|---|---|---|
| 150 | 5 只 ✓ | 30,000 |
| **151–199** | **0 只 ⛔** | 15,100 ~ 19,900 |
| 200 | 5 只 ✓（恰触界） | 20,000 |
| 301+ | 0 只（原因：超过价格上限，⛔ 勿与上混） | — |

> 死带 [151,199] = 49 个价位 / 20–300 区间 281 个 ≈ **17.4% 价位真空**
> ⚠️ 死带**随 `base` 漂移**（35000→[176,199]、25000→[63,300]、≥40000→无死带）
> ⇒ **必须逐日 `(base_i, close_i)` 配对**，⛔ 不得用固定区间

### H5c 正反馈螺旋（15 万本金 / 目标 5 只）

| NAV | base | 死带区间 | 占 20–300 |
|---|---|---|---|
| 150,000 | 30,000 | [151, 199] | 17.4% |
| 130,000 | 26,000 | [66, 300] | 43.8% |
| 110,000 | 22,000 | [28, 300] | 77.2% |
| 100,000 | 20,000 | [21, 300] | 97.9% |
| 90,000 | 18,000 | [20, 300] | **100%（完全失效）** |

> **临界点**：`base < min_position_value` ⇒ 整手后金额恒小于下限 ⇒ **任何价位都建不成仓**
> 实际产物终值 108,421 ⇒ base=21,684 ⇒ 死带 ≈ **81.1%**（十年后已近"半失效"）
> **单调性定理**：`base` 下降 ⇒ 死集只增（⛔ 不缩）

### ㊶ 隐性双口径

```
实际 N = min(len(signals), target_count)
其中 len(signals) ≤ DividendConfig.default_positions（candidates.py:396 截断）
⇒ default_positions 是隐性上限，且**不在 PortfolioConfig 里**（住在 candidates.py）
⇒ 全仓无跨配置一致性校验
⚠️ 抬 default_positions → 8（意图"多持几只"）⇒ base=18,750 < 20,000 ⇒ 恒失效
   ——「想多买」的改动会导致「完全买不了」
```

---

## 附：当前门禁与仓库状态（实测快照）

```
仓库：HEAD 55e7266 ｜ 领先 origin 14 提交 ｜ ⛔ 未 push ｜ 工作区仅 1.ipynb 未跟踪
计划仓：D:\Projects\research-finai ｜ HEAD fae9c44
镜像：docs/spec/.../tasks.md SHA-256 c89d07b84514b5a2…（两仓一致）

单测：876 passed（非沙箱，py -3.11）｜ 常量 TEST_BASELINE_PASSED = 876 == 收集数
门禁（--ci 真实 ctx）：PASS 14 / FAIL 1 / INCONCLUSIVE 14 / SKIP 0 ｜ 共 29 道
门禁归属：STATIC 12（D-1~D-4/E-1/E-2/G-1~G-4/G-DOC-1/G-REF-1）
          RUN_EVIDENCE 17（A-1~A-4/D-5/E-3/G-MDD-1/G-REPRO-1/G-STRESS-1/L-1~L-3/S-1~S-5）
四层行为：pre-push OK=True ｜ --ci exit=0 ｜ --acceptance(超限) exit=1 ｜ --scheduled exit=1
```

---

## 附：已知缺口（如实登记，未解决）

| # | 缺口 | 归属 |
|---|---|---|
| 1 | `G-MDD-1` FAIL（MDD 43.08% > 35%）——真实策略缺陷 | M7 策略改进后自然转绿 |
| 2 | 回测仅有 metrics 级产物，**无 trade 级明细** ⇒ 部分门禁在定时 CI 仍 WARN | M4+ 落盘该 artifact |
| 3 | **飞书告警仍是 stub**（`ops/feishu_alert.py` 未实现） | 需用户决定是否接真实通道 |
| 4 | H1–H5c **全部为待验证假设** | M6（本次任务） |
| 5 | `nav_curve` **内存有、未落盘**（阻断 H5c 时间演化验证） | 已纳入 C-01 规格 |
| 6 | 诊断 md 的持仓/现金三数**无产物支持**（证据链第一环靠人眼） | C-01 落地后闭环 |
| 7 | 单票集中度上限 / ST 剔除**均未实现**（文档已如实标注待办） | M7 评估 |
| 8 | `accounting/` 为 0 字节占位包 | M4 文档已如实描述 |
