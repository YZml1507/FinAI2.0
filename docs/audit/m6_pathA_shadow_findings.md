# M6 归因实验 · 阶段一「路径 A · 零成本影子复算」——结论

> **性质**：**只读复算**。本阶段不跑回测、不改任何策略参数/行为代码、不新增生产文件。
> **证据源**：① 签名产物 `experiments/runs/20260907-150402-t312-dividend-v1-noseed.json` 的字段；
> ② 本阶段脚本 `scripts/m6_shadow_recompute.py` 的实际输出（`artifacts/m6_attribution/pathA_shadow/shadow_recompute.json`）。
> ⛔ 本文件**不引用任何 `*.md` 作为证据源**。

---

## ⚠️ 强度纪律横幅（先读，贯穿全篇）

1. **路径 A 只证「**计划层**会切断」**——即：给定候选与当日 NAV，生产函数 `plan_positions`
   在**计划阶段**就把若干标的丢弃（`strategy/portfolio.py:158-161`），且**丢弃额度不再分配**
   （`portfolio.py:205-208`）。这是**函数级**事实，不依赖回测。
2. ⛔ **不得据此宣称"收益会改善"**。路径 A 给的是 **H5/H5b/H5c 影响的上界**（"计划层若不切断，
   最多能建几只"），**不是**"若换加权/换价位则十年期指标如何"。后者需要**路径 B 的真实回测
   签名产物**（改 `candidates.py:360` 一行 + 跑对照回测），本阶段未做、也不在阶段一范围。
3. ⛔ **H5c 反因果纪律**：**死带扩张与 NAV 下跌互为因果**（亏 ⇒ `base=NAV/N` 变小 ⇒ 死带变宽
   ⇒ 能建的票更少 ⇒ 现金更多 ⇒ 更难回本）。因此 `dead(base)` 随 `base` 单调变宽、NAV 下行期
   死带占比上升，**只证明"存在正反馈结构"**，⛔ **不证明"该螺旋是收益亏损的主因"**。
   主因判定须由路径 B 的对照回测（以及后续阶段二 T01–T05 的真实 `nav_curve`）承担。

---

## ① 结论摘要（硬数字全部来自脚本实际输出）

### H5 —— 权重离散 × 单票下限 × 丢弃不重分配 ⇒ 断崖式丢弃（**计划层成立**）

同一批 5 只候选（`close=10.00`、`NAV=150000`），仅改权重：

| 加权口径 | 实建仓只数 | `planned_total` | 留存现金（=NAV−Σplanned） | 丢弃原因 |
|---|---|---|---|---|
| 等权 `1:1:1:1:1` | **5** | 150000.00 | **0.00** | — |
| `3:2:2:1:1` | **3** | 116000.00 | **34000.00** | 「低于单票下限」×2 |
| `10:1:1:1:1` | **1** | 107000.00 | **43000.00** | 「低于单票下限」×4 |

⇒ **选 5 只，市值加权（离散）时只建 1–3 只；等权则 5 只全建**。丢弃额度**不回补**（Σplanned < NAV），
被丢的 `base_i` 原封不动留在现金里。⇒ **「建不满仓」的断崖在计划层纯由权重离散 + 不重分配造成**。

### H5b —— 价位真空带（即使等权也有一整段价位建不成）（**计划层成立**）

`base=30000`（等权 5 只 / NAV 15 万）下，`[20,300]` 有效窗口内死价位 **49 个**，占比
**0.1743772241992882562277580071**（≈17.44%），死集**恰为单一段** `[151,199]`。
边界逐点实测（生产函数）：`p=150` 建成（planned=30000）；`p=151` 丢（15100）；
`p=199` 丢（19900）；`p=200` **建成**（planned=20000，**恰等于下限 ⇒ 因判据是严格 `<` 而通过**）；
`p=301` 丢，但原因是 **「超过价格上限」**（`portfolio.py:151-152`，F10c）——⛔ **与 H5b 不同因，不混计**。
⇒ **H5b 的处方是"改价位/改下限"，不是"换加权"**：等权也建不成（`p=151` 在等权下同样丢）。

### H5c —— 死带随 `base` 单调变宽 ⇒ NAV 退化放大丢弃率（**结构成立**）

等权 `N=5`、`base=NAV/5`，`[20,300]`（281 价位）内：

| NAV | `base` | 死价位数 | 死带占比 |
|---|---|---|---|
| 150000 | 30000 | 49 | 0.1743772241992882562277580071 |
| 130000 | 26000 | 123 | 0.4377224199288256227758007117 |
| 110000 | 22000 | 217 | 0.7722419928825622775800711744 |
| 100000 | 20000 | 275 | 0.9786476868327402135231316726 |
| 90000 | 18000 | 281 | 1 |

- **临界线**：`base < min_position_value` ⇒ 全价位丢弃；等权 `N=5` 时等价触发线 `NAV < 100000`。
  脚本用**生产函数实测**：`base=18000` 时对 `p∈[20,300]` **全体**丢弃（原因恒为「低于单票下限」）。
- **单调性**：脚本对相邻 base 档做**集合包含**断言，`monotonicity_all_pairs_passed = true`
  （base 下降 ⇒ 死集只增不减）⇒ **NAV 退化必然单调放大丢弃率**（正反馈的**结构**存在）。
- ⚠️ **该表是保守下界**：表为**等权**口径（`base=NAV/N`）；基线是**市值加权**
  （`candidates.py:360` `weights=scores`）⇒ 小权重票的 `base_i < NAV/N` ⇒ 基线实际死带**更宽**
  （H5 与 H5c **叠加**）。

### ㊶ —— `default_positions` × `target_count` 隐性双口径（**结构成立**）

`N = min(default_positions, target_count)`（`default_positions` 在 `candidates.py:205`，
**不在** `PortfolioConfig` 里）。实测（`NAV=150000`）：

| `(default_positions, target_count)` | `N` | `base` | 实建 | 丢弃 |
|---|---|---|---|---|
| (5, 5) | 5 | 30000 | 5 | 0 |
| (5, 8) | **5**（不变） | 30000 | 5 | 0 |
| (8, 5) | **5**（不变） | 30000 | 5 | 0 |
| **(8, 8)** | **8** | **18750** | **0** | 8（全部「低于单票下限」） |
| (3, 5) | 3 | 50000 | 3 | 0 |

⇒ **单改一个无效**（因为 `N = min(dp, tc)`）；**同时抬到 8** ⇒ `base=18750 < 20000` ⇒
`plan_positions` **静默返回空计划**（无 raise、无 WARN）。⇒ 一个"想多持几只"的改动
（抬 `default_positions`→8，同时须抬 `target_count`→8）会**从"只建 5 只"直接跳成"完全无法建仓"**，
**意图与后果相反**，且无门禁拦截。

### 端点括号（只读签名产物，两点 bracket）

- 终值：`metrics.final_nav = "108421.14"`，（`N=5`）`base = 21684.228` ⇒ 死带占比
  **0.8113879003558718861209964413**（**228/281**）。
- 谷值：由 `metrics.max_drawdown = "0.4307653722557783501291325324"` 与
  `metrics.initial_nav = "150000"` 反推，谷值 `NAV ∈ [85385.19416163324748063012014, 108421.14]`
  ⇒ `base ∈ [17077.03883232664949612602403, 21684.228]` ⇒ 谷值处死带占比
  **∈ [0.8113879003558718861209964413, 100%]**，其中 `base < 20000`（**全失效**）**不可排除**。
- ⛔ 产物**没有**逐日 NAV 路径（`metrics` 共 20 键，无任何序列字段；`max_dd_peak`/`max_dd_trough`
  是**日期**、`max_dd_recovery = null`）⇒ 只能给**端点 + 区间**，⛔ **不得**断言"曾完全无法建仓"。

### 等价性暴力验证（交叉校验，非替代生产函数）

`[20,300]`（281 价位）× base 网格 `18000..40000` 步长 500（45 档）= **12645 组**：
独立精确式 `100·p·floor(base/(100p)) < 20000` 与**生产函数** `plan_positions` 判定
**不一致 0 组** ⇒ 精确式与实现等价。
已证否的闭式 `(base/200, base/100]` 与生产实测**不一致 5452 组**，反例：
`base=30000, p=200` ⇒ 闭式判死、生产实测**建成** ⇒ **闭式已证否**，⛔ 一律用精确式。

---

## ② 复现命令

```bash
# 脚本（打印报告 + 落盘确定性 JSON）
cd D:/Projects/FinAI2.0 && py -3.11 scripts/m6_shadow_recompute.py

# 测试（判据化双向断言）
cd D:/Projects/FinAI2.0 && py -3.11 -m pytest tests/test_m6_shadow_recompute.py -p no:ddtrace -p no:ddtrace.pytest_bdd -q
```

---

## ③ 产物路径 + 每个数字的 JSON 字段出处

**产物**：`artifacts/m6_attribution/pathA_shadow/shadow_recompute.json`
（内容确定：无时间戳/无随机量；`json.dumps(..., sort_keys=True, ensure_ascii=False, indent=2)`；
重跑**逐字节相同**；金额一律 Decimal→str，⛔ 无 float。）

**出处锚定块**：`source_anchor.*`
- `source_anchor.source_run_id` = `20260907-150402-t312-dividend-v1-noseed`
- `source_anchor.artifact_path` = `experiments/runs/20260907-150402-t312-dividend-v1-noseed.json`
- `source_anchor.anti_tamper_signature` = `055cb1d6b2bedbb88aa73d107396982b49f829a6d57236d4d16b921f4c2982d5`
- `source_anchor.artifact_fields_read` = 所读字段清单（含 `metrics.final_nav` 等）
- `source_anchor.production_functions` = 所调生产函数 `文件:行号`
  （`strategy/portfolio.py:49 PortfolioConfig` / `:124 select_targets` / `:140 _plan_one` / `:165 plan_positions`）
- `source_anchor.reproduce_command` / `source_anchor.reproduce_test_command` = 复现命令
- `source_anchor.artifact_params_portfolio` = 产物 `params.portfolio` 原值快照

| 结论数字 | JSON 字段路径 |
|---|---|
| H5 实建只数 / 留存现金 / 丢弃原因 | `h5_weight_dispersion.rows[].n_built` / `.retained_cash` / `.dropped` |
| H5b 边界建成/丢弃 + 原因 | `h5b_dead_band_boundary.boundary_cases[].built` / `.drop_reason` / `.planned` |
| H5b base=30000 死带 49 / 占比 | `h5b_dead_band_boundary.base_30000_dead_band.count` / `.share` / `.prices` |
| H5c NAV 表死价位数 / 占比 | `h5c_dead_band_vs_base.nav_table[].n_dead` / `.share` |
| H5c 临界线与恒失效 | `h5c_dead_band_vs_base.critical_line.*`（`.equal_N8_always_dead` / `.nav_trigger_equal_N5`） |
| H5c 全失效实测（base<20000） | `h5c_dead_band_vs_base.fully_dead_probe.*` |
| H5c 单调性 | `h5c_dead_band_vs_base.monotonicity_all_pairs_passed` / `.monotonicity_adjacent[]` |
| ㊶ 组合 | `counter_61_double_count.rows[].N` / `.base` / `.n_built` / `.drop_reasons` |
| 端点终值/谷值 | `endpoint_bracket.final_point.share` / `.trough_point.share_at_lower_bound` / `.dead_share_interval` |
| 等价性验证 | `brute_equivalence.n_checked` / `.n_mismatch_exact_vs_production` / `.n_mismatch_closed_vs_production` / `.closed_form_counterexample` |
| 生产默认配置基线 | `config_defaults.*`；价位窗口 `price_window.*` |

---

## ④ 未证事项清单（阶段一**不能**回答，须阶段二 T01–T05 真实回测 + `nav_curve` 落盘）

1. **逐年死带演化曲线**：产物无逐日 NAV 序列（`metrics` 无任何序列字段）⇒ 只有端点 + 区间，
   给不出"哪一年死带到几成"。须阶段二把 `BacktestResult.nav_curve`（`backtest/engine.py:74` 内存已有）
   落盘为派生轨迹后才可算。
2. **真实候选命中率**：本轮 H5b/H5c 的价位尺寸是**构造**（`close` 从 20 扫到 300），
   **不是**真实红利候选的 `close` 分布 ⇒ 无法回答"真实候选是否落在当日死带内"
   （即 H5b 是否"实践命中"）。须影子漏斗的 `selected_close` + 逐日 `base_i` 配对。
3. **13 环节漏斗摊分**：本轮只覆盖**计划层**（F9–F11 的丢弃）；F1–F8（warmup / rebalance /
   MA200 / 股息率阈值 / 候选池 / top-N 截断）与 F12–F13（撮合）**未摊分** ⇒ 无法给出
   "零持仓日 / 现金占比"里各环节各占多少。
4. **收益影响**：路径 A 全部结论均为计划层上界，⛔ **不能**推出"改加权/改下限/改价位后收益改善"。
5. **真实的 `N` 与 `base` 时序**：本轮 `N=5` 为配置推断值；真实运行里 `N = min(len(signals), N)`
   随候选枯竭波动 ⇒ 真实 `base` 序列待影子复算/`nav_curve` 落地后才有。

---

## ⑤ 与前任表的差异对照（逐项）

| 前任表（设计文档 / 交接数字） | 本阶段复算 | 一致？ | 说明 |
|---|---|---|---|
| H5：等权建 5 只 / 留存 0 | 5 只 / 0.00 | ✅ 一致 | 逐点吻合 |
| H5：`3:2:2:1:1` 建 3 只 / 留存 34,000 | 3 只 / 34000.00 | ✅ 一致 | 丢弃 `s3,s4`（「低于单票下限」） |
| H5：`10:1:1:1:1` 建 1 只 / 留存 43,000 | 1 只 / 43000.00 | ✅ 一致 | 丢弃 `s1..s4` |
| H5b：`[151,199]` = 49 个价位 = 17.4% | 49 个 / 0.1743772241992882562277580071 | ✅ 一致 | 死集恰为单一段 `[151,199]` |
| H5b：`p=200` 恰触界建成 / `p=301` 因「超过价格上限」 | planned=20000 建成 / `p=301` 原因「超过价格上限」 | ✅ 一致 | 二者不混计 |
| H5c NAV 表 17.4% / 43.8% / 77.2% / 97.9% / 100% | 0.1744 / 0.4377 / 0.7722 / 0.9786 / 1 | ✅ 一致 | 逐点吻合（counts 49/123/217/275/281） |
| H5c 终值点死带 **81.1%** | 0.8113879003558718861209964413（228/281） | ✅ 一致 | 精确复算 |
| H5c 谷值点死带 ∈ [81.1%, 100%] | ∈ [0.8113879003558718861209964413, 1] | ✅ 一致 | 端点 bracket 推导；全失效不可排除 |
| ㊶：(5,8)/(8,5) 不失效；(8,8) 恒失效；(3,5) 死带 0% | N=5/5/8/3；base=30000/30000/18750/50000；(8,8) 实建 0 | ✅ 一致 | `(8,8)` 返回空计划、8 只全丢 |
| 闭式 `(base/200, base/100]` 已证否 | 与生产实测不一致 5452 组；反例 `base=30000,p=200` | ✅ 一致 | 反例方向与设计一致 |
| 精确式与实现等价 | 12645 组 0 不一致 | ✅ 一致（**新增量化**） | 前任表未给等价性验证规模 |
| H5c 临界 `base < min_position_value` ⇒ 全价位丢 | `base=18000` 全体丢（实测） | ✅ 一致（**新增实测**） | 前任为代数推理，本轮补生产函数实测 |
| H5c 单调性（`base` 下降 ⇒ 死集只增） | `monotonicity_all_pairs_passed = true` | ✅ 一致（**新增实测**） | 前任为证明，本轮补实测 |

**结论：本阶段复算与前任表在各可对照项上逐项一致，未发现数值冲突或行号漂移。**
新增的三项（等价性规模、`base<20000` 全丢实测、单调性实测）为前任表未量化的交叉校验，
均支持前任结论，无差异登记。
