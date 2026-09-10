# QA 终轮独立验证报告（证伪取向）—— 治理层新增代码

- **验证者**：QA 严过关（software-qa-engineer-2）
- **轮次**：本轮为独立第三轮（对提交 `db6d78b` / `bdbf041` 及收口的**从未独立验证**的新增代码做证伪）
- **方式**：**只读**。除本文件外未修改/新建任何代码、测试、文档；未 commit；未碰 `finai/sources/`；未改 `experiments/runs/*.json`。
- **环境**：Windows / bash / `py -3.11`，`HEAD=bdbf041`
- 每条结论均附 `文件:行号` 或原始命令输出。无输出的结论已显式标注为**未验证**。

---

## 1. 结论摘要（≤180 字）

**本批"可交付，但治理承诺未闭合"**。代码自身不回归（`839 passed ×2`、边界条件全部 fail-closed），但"晋升必须留证"在**工程上不可强制**：`run_adoption_gate` 在 CI / pre-push / hook 中**零调用**，另有 3 条路径可绕过 acceptance 直接宣称"可进入 Phase 4"。共 **4 处晋升逃逸口 + 2 处豁免面过宽**。最严重一条：`gate_repro.py:177` 的 `ci_blocking` 是**死字段**（`ci_policy` 从不读它），使设计中的 `ADOPTED_LEGACY_UNVERIFIED` BLOCKER 静默降级为 WARN；叠加 `G-MDD-1` 推送/CI 期仅 WARN（实测 MDD=0.90 仍 exit 0），一份 90% 回撤产物可一路绿到合并。

---

## 2. A 段：准入层与采纳机制绕过分析

### A1. 其它"晋升"路径穷举（逐条 fail-closed 结论）

| # | 路径 | fail-closed？ | 证据 |
|---|---|---|---|
| 1 | `adoption.run_adoption_gate`（准入步） | **❌ 未接线（逃逸口）** | `grep -rn "run_adoption_gate\|load_adopted\|adopt(" .github/ ops/ .git/hooks/ scripts/hooks/` → 生产调用方**仅 0 处**（只有 `scripts/gates/adoption.py` 定义 + `tests/test_gate_adoption.py` 引用）。`ci.yml`/`scheduled_audit.yml` 全文无 `--acceptance`/`adoption`。`.git/hooks/pre-push` 存在且指向 `scripts/hooks/pre_push.py`，而 `pre_push.py` 无任何采纳/准入调用 |
| 2 | `reporting/registry.py::record_run` | ⚠️ **无条件收编，但不判"通过"** | `registry.py:180-261` 只校验 status/error/seed 合法性，**无 acceptance 调用**；且**不注入** `anti_tamper_signature`（签名只在 `run_dividend_backtest.py:230` 的内存 gate ctx 里）→ 新产物天然 `PROV-SIG` FAIL。设计上"登记≠准入"，但该边界**只由文档保证，无代码约束** |
| 3 | `scripts/run_dividend_stress.py` | **❌ 逃逸口** | `:162` 直接打印「✅ G4.5 门禁通过！红利策略显著优于动量策略，**可进入 Phase 4 模拟盘**」；判据为自建的 MDD/换手/胜率三项（`:110-160`），**不查 acceptance、不验签**；`main()` 无 `sys.exit`/`return`（`:44-166`）⇒ **无论通过与否恒 exit 0**。此即 `adoption.py:16` 点名的"解锁 Phase 4"路径 |
| 4 | `scripts/run_paper_trading_daily.py` | **❌ 逃逸口** | `grep -n "adopt\|acceptance\|准入\|采纳"` 仅命中 `:44`（import 签名函数）与 `:90`（文档字符串里的"G5 门禁准入规范"字样）⇒ 启动模拟盘**零准入校验**；`paper_trading/*.py` 同样零命中 |
| 5 | `gate_master_audit` 其它子命令 | ✅ 无晋升语义 | `--acceptance`（`:247-249`）**只评估不登记**——只传 `["--artifact", ...]`，**无 `--adopt`**，故采纳登记无法经此完成；`--ci`/`--strict`/`--mdd`/`--category`/`--report` 均不含准入判定 |
| 6 | `gate_consistency._resolve_truth_artifact` | ⚠️ **"换基准"可操纵** | `:156-170` 按"文件名升序 + `status==FINISHED` + `round_trips>0`"取 `pool[-1]` 为**权威产物**，**无 acceptance**。⇒ 往 `experiments/runs/` 追加一份"外形合格"的 FINISHED 产物，即可把 G-DOC-1 的比对基准换成它 |
| 7 | `scripts/audit_evidence_integrity.py` | ✅ 无判定效力 | `:111-142` 读最新 `experiments/runs/*.json` 判"PASS/FAIL"，但 `main()`（`:145-171`）**只 print，无 sys.exit** ⇒ CI Gate Check 2 永不因它变红 |

**A1 结论**：准入/采纳层**未接线**（#1），且存在 3 条独立于它的"可上线"宣称路径（#3/#4/#6）。`run_dividend_stress.py` 与 `run_paper_trading_daily.py` 是**真逃逸口**。

### A2. 采纳登记本身能否被绕过

| 问题 | 结论 | 证据 |
|---|---|---|
| `ADOPTED.json` 手工伪造使不合格产物通过？ | **不能** | `run_adoption_gate` 重跑 `evaluate_acceptance(artifact)` 而非信任字段（`adoption.py:123-133`）；实测：指针 `artifact` → 不合格产物 ⇒ `ok=False, "准入 FAIL：['PROV-SIG','SCHEMA','G-MDD-1','ACCEPT-WIN','S-1','ACCEPT-RT']"` |
| `adopt()` 是否校验 acceptance 结果？ | **是（默认）** | `adoption.py:88-92`，未过 ⇒ `PermissionError`。**但** `require_acceptance_pass: bool = True`（`:68`）是**公开参数**，调用方可显式关闭 ⇒ API 级逃逸（当前仅测试用） |
| `artifact` 指向另一个（合格）产物、实际想晋升不合格产物？ | **部分可行（split-brain）** | 指针有**两个键**：`run_adoption_gate` 用 `artifact`（`:125`），`gate_repro._load_adopted_run_id` 用 `run_id`（`gate_repro.py:82,163-166`），**二者无一致性校验**。实测：指针 `artifact=不合格产物` + `run_id="NOT-A-LEGACY-RUN"` ⇒ gate 判 FAIL（好），但 `gate_repro` 的 `adopted_is_legacy=False` ⇒ **改 `run_id` 即可绕过 `ADOPTED_LEGACY_UNVERIFIED`** |
| 采纳目录为空 ⇒ 不阻断？ | **✅ 成立** | 实测 `run_adoption_gate(tmp)` ⇒ `(True, "未采纳任何产物（experiments/acceptance/ 为空）⇒ 准入步无操作（PASS）")`；仓内 `experiments/acceptance/` 确不存在（`ls` 无此目录） |
| 只读调用是否有副作用？ | ✅ 无 | `load_adopted`/`run_adoption_gate` 不创建目录（`tests/test_gate_adoption.py:78-82` 已锁） |

**A2 附加**：`adopt()` 把 `str(art)` 原样落盘（`:98`），若调用方传**相对路径**（CLI 常规用法 `--artifact experiments/runs/x.json`），则指针存相对路径，而 gate 按 **CWD** 解析该字符串 ⇒ 实测指针 `"rel_healthy.json"` 时 gate 报 `准入产物不存在: rel_healthy.json`（fail-closed 但**依赖 CWD**，属健壮性缺陷）。

### A3. 边界条件逐条实测

命令：构造产物 → `sign_run_record` → `evaluate_acceptance`。

| 用例 | 结果 | 结论 |
|---|---|---|
| `max_drawdown` 恰 `0.35` | `ok=True` | ✅ `<=` 语义正确 |
| `max_drawdown` `0.3500000001` | `ok=False failed=['G-MDD-1']` | ✅ |
| `max_drawdown` 恰 `0.35`（float） | `ok=True` | ✅ |
| `win_rate` 恰 `0.35` | `ok=True` | ✅ |
| `win_rate` `0.3499999` | `ok=False failed=['ACCEPT-WIN']` | ✅ |
| `annual_turnover` 恰 `4.0` | `ok=True` | ✅ |
| `annual_turnover` `4.0000001` | `ok=False failed=['S-1']` | ✅ |
| 缺 `round_trips` | `failed=['ACCEPT-RT']` | ✅ |
| `round_trips=0` | `failed=['ACCEPT-RT']` | ✅（含"0 成交⇒平凡结果"提示） |
| `round_trips=-1` | `failed=['ACCEPT-RT']` | ✅ |
| `round_trips="abc"` | `failed=['ACCEPT-RT']` | ✅ |
| `max_drawdown="abc"` | `failed=['MDD-SANITY','G-MDD-1']` | ✅ |
| `max_drawdown=null` | 同上 | ✅ |
| `max_drawdown=-0.30` | 同上 | ✅（堵住 `mdd=-0.30`） |
| `max_drawdown=true`(bool) | 同上 | ✅ |
| `max_drawdown=1.5` | 同上 | ✅ |
| `metrics` 整体缺失 | `failed=['MDD-SANITY','G-MDD-1','ACCEPT-WIN','S-1','ACCEPT-RT']` | ✅ 无证据≠通过 |
| `status` 缺失 / `"FAILED"` | `failed=['STATUS']` | ✅ |
| `schema_version=99`（未来值） | `failed=['SCHEMA']` | ✅ |
| `schema_version` 缺失 | `failed=['SCHEMA']` | ✅ |
| 未签名 | `failed=['PROV-SIG']` | ✅ |
| **`win_rate=9.9`（越界）** | **`ok=True`** | ❌ **异常**：`ACCEPT-WIN` 只判 `>= 0.35`，**无 `[0,1]` 上界** |
| **`annual_turnover=-3.0`（负值）** | **`ok=True`** | ❌ **异常**：`S-1` 只判 `<= 4.0`，**无 `>= 0` 下界** |

**A3 结论**：20/22 用例 fail-closed 正确；**2 处不对称加固缺口**——`MDD-SANITY` 有健全性检查，而 `win_rate`/`annual_turnover` 无对应 SANITY（`acceptance.py:144-154`）。非现实可用的洗白路径，但违背该模块"先验证来源、再判指标"的自述口径。

---

## 3. B 段：门禁数/基线正则漏判与误报

方法：直接调用 `_declaration_violations_in_text(text, 29, 839)`，构造文本撞模式 A/B（`gate_consistency.py:517-534`）。

### 3.1 漏判清单（应判违规却未判）

| 措辞（用错数字 28 / 838） | 结果 | 测试是否覆盖 |
|---|---|---|
| `门禁共 28 个。` | 未命中 | ❌ 未覆盖 |
| `门禁数量:28` / `门禁数量：28` | 未命中 | ❌ 未覆盖 |
| `门禁数 28` | 未命中 | ❌ 未覆盖 |
| `共二十八道门禁。`（中文数字） | 未命中 | ❌ 未覆盖 |
| `门禁总数 28` / `\| 门禁总数 \| 28 \|`（表格） | 未命中 | ❌ 未覆盖 |
| 基线 `基线：838`（无 `passed`） | 未命中 | ❌ 未覆盖 |
| 基线 `passed=838` | 未命中 | ❌ 未覆盖 |
| 基线 `共 838 项测试通过` | 未命中 | ❌ 未覆盖 |

已正确命中的（对照，无需改）：`共 28 个门禁`、`28 道  门禁`（多空格）、`**门禁** 28 道`、`门禁有 28 道`、`目前 28 道门禁有效`、`28 道机读门禁`、`128 道门禁`（**未被子串 `29` 静默放过**，得 128）、`基线 838 passed`、`838 passed（基线）`。
正确数字 29/839 的各措辞（含 `**29** 道门禁`）**均无误报** ✅。

### 3.2 误报清单（不该判却判了）

| 措辞 | 结果 | 原因 | 测试是否覆盖 |
|---|---|---|---|
| `该产物未通过回撤上限门禁，3 道必达指标未满足。` | **误报 `门禁数量=3`** | 模式 B `(?:门禁\|闸门)[^\r\n。；]{0,24}?(\d+)\s*道` **不校验 N 是否为"总数"**，`门禁，3 道` 即命中 | ❌ 未覆盖（反例只用了 `条`） |
| `本次共 4 道门禁未通过，需修复。` | **误报 `=4`** | 同上（子集计数被当总数） | ❌ 未覆盖 |
| `门禁 2 道失败 / 3 道通过。` | **误报 `=2`** | 同上 | ❌ 未覆盖 |

**真实文档现状**：当前无活跃误报（`G-DOC-1 = PASS`，`declaration_violations_total = 0`），但两处命中是靠**巧合**压住，非设计正确：
- `docs/compliance/system_architecture_template.md:14`（`门禁数量与状态：原文 \`24 道全绿\``）——靠行内出现 skip 短语**"真值"**被跳过；一旦该行改写去掉"真值"，立即变成误报；
- `docs/delivery/GATE_PHASE3_COMPLETION_SUMMARY.md:20`——靠历史快照白名单豁免。

**B 段额外发现（覆盖缺口）**：`_doc_files`（`gate_consistency.py:440-458`）只扫 `docs/**/*.md` + `tasks.md` + `README.md`/`CLAUDE.md`，**不含 `.github/**` 与 `.yml`**。实测 `含 .github? False / 含 .yml? False`。后果：以下**陈旧数字存活且永不被门禁发现**——
- `.github/workflows/ci.yml:67`：`Offline Pytest Regression Suite (>=758 passed)`（基线实为 **839**）
- `.github/workflows/scheduled_audit.yml:57`：`full 28 gates with real repo ctx`（实际 **29**）

---

## 4. C 段：四条豁免通道风险评估

| 通道 | 可否躲过"当前时态"真缺陷 | 现状计数 | 有无测试锁定 | 风险 |
|---|---|---|---|---|
| `gate-doc-ignore`（行内） | 可躲**本行**（须带非空理由） | `ignored_lines = 1`（仅 `docs/project_status_flowchart.md:117`） | ✅ 有清单守卫（`tests/test_gate_consistency.py:1072-1082` 断言 `hits == [("docs/project_status_flowchart.md", 117)]`）+ 裸标记/空理由/作用域三条契约测试 | **低** |
| `gate-doc-void`（整篇） | **可躲当前时态真缺陷**：同时跳过 `G-DOC-1` 数字比对**与** `G-REF-1` 幽灵路径 | `void_docs = 5` / `MAX_VOID_DOCS = 8` → **已用 62.5%** | ❌ **无**（无测试锁定清单/数量） | **中** |
| `_HISTORICAL_SNAPSHOT_DOCS` | 可躲门禁数/基线声明校验（不影响指标/路径） | `historical_snapshot_docs = 4` | ❌ **无**（grep `tests/` 对 `_HISTORICAL_SNAPSHOT_DOCS` 零命中） | **中** |
| `_EXCLUDED_DOC_DIRS` | **整片目录永久免检**（数字 + 路径都免） | 值 `("docs/audit",)`，当前排除 **10** 篇 | ❌ **无**（见下） | **中** |

**逐项**：

1. **滥用风险**：`gate-doc-void` 是最强的通道——实测 5 篇被整篇跳过（`docs/momentum_backtest_summary.md`、`docs/phase35_recommendation.md`、`docs/t304_stress_report.md`、`docs/t305_technical_review.md`、`docs/task_completion_summary.md`）。这 5 篇都是"非 T312 红利产物"类文档，**当前理由成立**，但 `reason` 字段**只校验非空、不校验内容**（`is_void_doc`，`:480-491`），任何当前时态文档在文首 20 行内加一行合法标记即可对两个门禁**完全隐身**（含幽灵路径）。`MAX_VOID_DOCS = 8` 且判据是 `>`（`:856`、`:1066`）⇒ **第 9 篇才 FAIL**，即还能再加 3 篇而不报警。
2. **`_HISTORICAL_SNAPSHOT_DOCS` 成员**：`docs/delivery/GATE_PHASE1/2/3_COMPLETION_SUMMARY.md` + `CLAUDE.md`，共 4 个；理由（阶段历史快照，不改史）**成立**。**无测试锁定该集合**——新增一个后缀即可豁免任意文档。更糟的是 `_is_historical_snapshot`（`:547-550`）用 **`str.endswith(suffix)`**，实测 `docs/anything/CLAUDE.md` ⇒ **True** ⇒ **`docs/` 下任意目录里放一个叫 `CLAUDE.md` 的文件即被当成历史快照豁免**。
3. **`_EXCLUDED_DOC_DIRS`**：值确为 `("docs/audit",)`（`:424`），**但代码注释 `:422-423` 声称"守卫测试锁定 `_EXCLUDED_DOC_DIRS == ("docs/audit",)`"——实测 `grep -rn "_EXCLUDED_DOC_DIRS" tests/` 零命中，该声称不成立**。⇒ 往该元组加 `"docs/spec"` 之类即可豁免整片文档，**无任何测试会红**。
4. **`MAX_VOID_DOCS=8` 当前用量 = 5**（见上）。**接近上限**，需警惕：豁免正在被较大量使用（占扫描 46 篇的 10.9%）。
5. **计数可见性**：`void_docs` / `historical_snapshot_docs` / `ignored_lines` 三者在 message 与 metrics **均可见** ✅（实测 `G-DOC-1` message 与 metrics 同值）。**唯一不可见的是 `_EXCLUDED_DOC_DIRS`**——被排除的 10 篇在任何门禁输出中**无计数、无清单**，是本组里唯一"静默豁免"通道。

---

## 5. D 段：三层分层语义一致性 + STATIC/RUN_EVIDENCE 核对

### 5.1 `ci_policy` 判定 vs 实际退出码

实测：

| 入口 | 结果 | 退出码 |
|---|---|---|
| `gate_master_audit --ci` | `总览: 共 29 项门禁 \| PASS: 14 \| FAIL: 1 \| INCONCLUSIVE: 14`；`[WARN] G-MDD-1 ... MDD=0.4308 > 0.35`；`[CI][PASS] 无阻断项` | **0** |
| `gate_master_audit --strict`（空 ctx） | `共 29 项 \| PASS: 3 \| FAIL: 1 \| SKIP: 23 \| INCONCLUSIVE: 2` | **1** |
| `scripts/hooks/pre_push.run_master_gate_guard()` | `ok=True`；`六维门禁总检通过（推送期 14 道 / 全库 29 道；PASS: 13，WARN 1 项…另有 15 道…）` | **0** |
| `--mdd <0.4308 产物>` | — | **0** |
| `--mdd <0.4308 产物> --strict` | — | **1** |
| `gate_consistency --doc --ref` | 双 PASS | **0** |

**残留不一致（3 处）**：
1. **措辞与退出码背离**：`--ci` 下 `print_summary` 打印 `[警告] 检出未通过门禁！请修复相关缺陷后再行推进！`（因 FAIL=1），紧接着 `[CI][PASS] 无阻断项` 且 exit 0。同一屏内自相矛盾，且未标注"此 FAIL 属 WARN 门禁、不阻断"。
2. **`SKIP` 是 fail-open 状态**：实测 `is_blocking_result(SKIP) == False`（`base.py`）。空 ctx 下 23 道门禁返回 `SKIP`（各 gate 的 `if not context: SKIP` 分支），**不阻断** ⇒ 在 `--strict` 语义下"无证据 ⇒ 不阻断"，与该体系"无证据 ≠ 通过 / Fail-Closed"的自述冲突。
3. **G-MDD-1 的 FAIL 详情丢失产物路径**：`--ci` 与 `pre_push` 均打印 `检出 1 份产物最大回撤超限：<inline> MDD=0.4308 > 0.35`。因 `context_builder` 注入的 `run_record` 无 `_path`（`gate_consistency.py:127` 取 `item.get("_path","<inline>")`）⇒ 违反该模块自订设计原则 3「报告必须指向**具体文件 + 行号**」。

### 5.2 STATIC / RUN_EVIDENCE 划分核对

实测：`STATIC_GATE_IDS` = **13** 项，`RUN_EVIDENCE_GATE_IDS` = **16** 项，`WARN_GATE_IDS` = 1；`STATIC ∪ RUN_EVIDENCE == 全 29 道`、**无遗漏、无重叠** ✅；推送期 = `STATIC ∪ WARN` = **14** 道。

**两处划分可疑（双向各一）**：

- **`S-1 ∈ STATIC` 应为 RUN_EVIDENCE（会误伤）**：`TurnoverCeilingGate`（`gate_s_scientific.py:47`）消费 `annualized_turnover`，而该值**只由 run 产物提供**（`context_builder.py:90-91`：`ctx["annualized_turnover"] = float(metrics["annual_turnover"])`）。这与 G-MDD-1 被移出 STATIC 的理由（"判定依赖 run 产物 metrics"，`context_builder.py:32-34`）**完全同构**。实测后果：`S-1` 的 INCONCLUSIVE ⇒ `ci_policy blocking=True, blockers=['S-1']`，而 `G-MDD-1` 的 INCONCLUSIVE ⇒ `blocking=False, warnings=['G-MDD-1']`。⇒ `experiments/runs/` 一旦为空，CI 会因 S-1 而**永久红**，正是该改造想消除的情形。
- **`L-3 ∈ RUN_EVIDENCE` 应为 STATIC（会漏挡）**：`StaticAstCallGate`（`gate_l_liveness.py:288-334`）对 `source_code` 做 `ast.parse` 静态解析，其证据 `source_code` + `required_calls` 在**推送期已由 `context_builder` 备齐**（`:143-146`）。⇒ 静态可判的门禁被放进"仅告警"桶，`INCONCLUSIVE` 时只 WARN（实测 `--ci` 日志：`[L-3] … 必调链路 [...] 静态可达，但缺少运行期调用追踪` 被列入"只告警不阻断"清单）。

其余 14 项 RUN_EVIDENCE（D-5/L-1/L-2/E-3/A-1~A-4/S-2~S-5/G-STRESS-1/G-MDD-1/G-REPRO-1）确需 run 内部真相，划分**合理**。

### 5.3 WARN 是否掩盖本该 FAIL 的情形

**成立**。实测：构造 `max_drawdown="0.90"` 且已签名/现行 schema 的产物 ⇒ `G-MDD-1 status=FAIL`，但 `ci_policy` ⇒ `blocking=False, warnings=['G-MDD-1']` ⇒ `--ci` exit 0。而 `G-MDD-1` 的 BLOCKER 能力只在 `acceptance.py`，该层**手工调用且未接线**（A1#1）⇒ **MDD=0.90 的产物在推送/CI 期无任何阻断**。

### 5.4 附带发现：`ci_blocking` 是死字段（最严重一条）

`gate_repro.py:167-183` 在 `ADOPTED_LEGACY_UNVERIFIED` 分支设置 `metrics["ci_blocking"] = True` 并注释「ci_policy 据此把它当 BLOCKER（非 WARN）」。但 `grep -n "ci_blocking" scripts/gates/*.py` ⇒ **仅 1 处命中（就是写它的那行）**，`ci_policy`（`context_builder.py:231-250`）**从不读取该字段**。实测：

```
status = INCONCLUSIVE | severity = BLOCKER
metrics.ci_blocking = True
ci_policy -> blocking = False
            blockers = []
            warnings = ['G-REPRO-1']
```

⇒ 设计的"被采纳 legacy 产物 ⇒ BLOCKER"被**静默降级为 WARN**。叠加 A2 的 split-brain（改 `run_id` 即可让 `adopted_is_legacy=False`），该 ㉛ 守卫**双重失效**。

---

## 6. E 段：回归结果与空壳断言抽查

### 6.1 回归（连跑两遍）

```
py -3.11 -m pytest tests/ -p no:ddtrace -p no:ddtrace.pytest_bdd -q
RUN1: 839 passed, 1 warning in 47.94s   RUN1_EXIT=0
RUN2: 839 passed in 56.53s              RUN2_EXIT=0
```

- **839/839 ×2** ✅ 与 `TEST_BASELINE_PASSED = 839`（`scripts/gates/constants.py`）一致；
- **`runs/` 无残留** ✅（`diff` 前后 `ls runs/` 无变化）；
- **`git status` 无变化** ✅（除我本轮新建的本文件与既有未跟踪文件）。

### 6.2 空壳/永真断言抽查

- `tests/test_gate_adoption.py`：`19 tests collected` / `19 passed in 1.76s`；15 个 `def test` 含 **31 条 assert**；无 `assert True` / 无 `pytest.skip` / 无 `xfail` / 无裸 `pass`。
- `TestGateCountStructuredPatterns` + `TestGateDocIgnoreScope` + `TestPrePushGateCountWording`：**24 passed in 11.54s**。
- 断言实质性强（如 `test_ignore_is_line_scoped_not_file_wide` 同时断言 `status==FAIL` + 被豁免行不报 + `ignored_lines==1`；`test_bare_marker_without_reason_is_not_effective` 断言 `FAIL` 且 `ignored_lines==0`）。
- **未发现空壳或永真断言** ✅。

### 6.3 镜像一致性（独立复核）

```
verify_tasks_mirror('../research-finai/specs/001-a-stock-longonly-daily-quant/tasks.md',
                    'docs/spec/001-a-stock-longonly-daily-quant/tasks.md')
-> True / 两仓 tasks.md 镜像一致性核验完全吻合 (SHA-256: c89d07b84514b5a2...)
```

✅ 复核通过。**但**：该检查**无任何闸口调用**——`grep -rn "verify-mirror\|verify_tasks_mirror" .github/ scripts/hooks/` 仅命中 CLI 定义（`tamper_guard.py:226,274`）与单测（`tests/test_gate_p3_hardening.py:217`）；`ci.yml` 的 `tamper_guard.py --verify-all`（`:291-330`）只做母库 + tasks 验签 + **一个硬编码 run 文件**验签，**不含镜像**。⇒ 镜像一致性目前靠人工执行。

### 6.4 未能验证的项

1. `finai/sources/` 370 行母库守卫 —— 本轮硬约束"不碰 `finai/sources/`"，未跑 `tamper_guard.py --check-mother-library`（**未验证**）。
2. `.github/workflows/*.yml` 的**真实执行**（无 GitHub runner），仅做静态阅读（**未验证**）。
3. `_EXCLUDED_DOC_DIRS` 排除的 10 篇 `docs/audit/**` 内容中是否藏真缺陷 —— 该通道**无计数、无清单**，从门禁输出无法判断（**无法验证**，本身即 C 段结论 5）。
4. `MAX_VOID_DOCS` 超限行为（第 9 篇 ⇒ FAIL）—— 需在仓内制造第 9 篇 void 文档，受"只读"约束**未实测**，仅按 `:856`/`:1066` 的 `void_docs > MAX_VOID_DOCS` 代码路径判读（**代码级结论，未实测**）。

---

## 7. 与工程师/主理人自述不符之处（逐条附证据）

| # | 自述 | 实测 | 证据 |
|---|---|---|---|
| 1 | 「`context_builder.py` — `STATIC_GATE_IDS`**(14)** / `RUN_EVIDENCE_GATE_IDS`**(15)**」 | **13 / 16** | `py -3.11 -c "from scripts.gates.context_builder import STATIC_GATE_IDS, RUN_EVIDENCE_GATE_IDS; print(len(STATIC_GATE_IDS), len(RUN_EVIDENCE_GATE_IDS))"` → `13 16`（13+16=29 完备；推送期 14 = 13+WARN(1)、deferred 15 = 16-1，故 14/15 两个数字对，但**集合大小说反**） |
| 2 | 「采纳登记 + 准入层已接线（㉗）」 | **仅手工入口**：`gate_master_audit --acceptance`（且**不支持 `--adopt`**）；`run_adoption_gate` 在 `.github/**`、`.git/hooks/pre-push`、`pre_push.py` 中**零调用** | `grep -rn "run_adoption_gate\|load_adopted\|adopt(" .github/ ops/ .git/hooks/ scripts/hooks/` → 无命中；`gate_master_audit.py:247-249` 只传 `--artifact` |
| 3 | `gate_repro.py:177` 注释「ci_policy 据此把它当 BLOCKER（非 WARN）」 | **`ci_policy` 从不读 `ci_blocking`**；实测落入 `warnings` | `grep -n "ci_blocking" scripts/gates/*.py` → 仅 `gate_repro.py:177`；实测 `blocking=False, warnings=['G-REPRO-1']` |
| 4 | `gate_consistency.py:422-423` 注释「守卫测试锁定 `_EXCLUDED_DOC_DIRS == ("docs/audit",)`」 | **不存在该测试** | `grep -rn "_EXCLUDED_DOC_DIRS" tests/` → 零命中 |
| 5 | 「`_HISTORICAL_SNAPSHOT_DOCS` 有测试锁定不被随意扩充」（隐含于"豁免通道可控"） | **无测试锁定**；且 `endswith` 后缀匹配过宽（`docs/anything/CLAUDE.md` ⇒ True） | `grep -rni "historical_snapshot" tests/` 仅命中 `tests/test_gate_consistency.py:875`（断言计数，不断言集合）；`_is_historical_snapshot(Path("docs/anything/CLAUDE.md"))` → `True` |
| 6 | `pre_push.py:142` 措辞「另有 15 道需回测证据的门禁不在推送期校验」 vs `scheduled_audit.yml:8` 注释「承接被移出推送期的 **14 道**需 run 证据门禁」 | **两者互相矛盾**（实际 deferred = 15） | `scheduled_audit.yml:8-9` 列举 14 个 ID（漏 G-REPRO-1）；`pre_push` 实测输出「另有 15 道」 |
| 7 | 「正则双向：8 正例全中 / 5 反例全不中」 | **8 漏判 + 3 误报未被覆盖** | 见 B 段 §3.1/§3.2 实测表 |
| 8 | 「`--ci exit=0`、`准入(超限) exit=1`、`准入(健康签署) exit=0`、`839 passed×2`、`常量 839 == 收集数`、`镜像双 PASS`、`G-DOC-1 / G-REF-1 PASS`、`ignore 清单仅 flowchart.md:117`」 | **全部复核一致** ✅ | 见 §5.1 / §6.1 / §6.3 / §4 |
| 9 | 「采纳目录为空 ⇒ 不阻断」 | **成立** ✅ | 实测 `ok=True, "未采纳任何产物…无操作（PASS）"` |
| 10 | 「准入层未验签已修」 | **成立** ✅ | `acceptance.py:105-111` 引入 `PROV-SIG`；实测未签名产物 `failed=['PROV-SIG']` |

**额外（自述未提及但影响口径）**：`.github/workflows/ci.yml:67` 声明 `>=758 passed`（基线 839）、`scheduled_audit.yml:57` 声明 `full 28 gates`（实际 29）——因 `_doc_files` 不扫 `.yml`/`.github/**`（实测 `含 .github? False`），这两处陈旧数字**永远不会被 G-DOC-1 发现**。

---

## 8. 诚实边界

1. **本轮为只读验证**，除本文件外未改任何代码/测试/文档；所有实测均在**系统临时目录**（`%TEMP%\qa_*`）构造产物，未写入仓内 `experiments/`。故 `MAX_VOID_DOCS` 超限、以及"往 `docs/` 放 `CLAUDE.md` 骗过历史白名单"等结论为**代码路径判读 + 单元级实测**，未在真实仓结构上端到端复现。
2. **A1#1（未接线）的证据是"grep 零命中"**，属否定性证据。若存在未纳入 `git ls-files` 的本机 hook 或仓外编排（如外部 CI 服务），本结论不覆盖。
3. **B 段正则结论**基于 `_declaration_violations_in_text` 的**函数级**调用；`G-DOC-1` 端到端当前为 PASS，故漏判/误报均为**潜在**（latent）而非已发生。
4. **`--strict` 与 `--ci` 的差异**部分是**设计意图**（全库审计 vs CI 策略），我未主张其为缺陷，仅主张"同产物同门禁的退出码随入口而变"这一事实应显式文档化。
5. 未能验证项见 §6.4（母库 370 行、workflow 真实执行、`docs/audit/**` 内容、void 上限实测）。
6. 本报告**不评价**策略本身的有效性（MDD 43.08% 是否为真值），只评价治理闸口的可绕过性与口径一致性。

---

## 附：交付判定建议

| 项 | 判定 |
|---|---|
| 代码回归 | ✅ 可交付（839×2 全绿、无残留） |
| 边界条件健壮性 | ✅ 基本可交付（2 处 SANITY 缺口待补：`win_rate` 上界、`annual_turnover` 下界） |
| 治理闸口可强制性 | ❌ **不可交付**（准入/采纳未接线；4 处晋升逃逸口） |
| 口径一致性 | ⚠️ **有保留**（`ci_blocking` 死字段致 BLOCKER 降级；`--ci` 措辞与退出码背离；STATIC/RUN_EVIDENCE 双向各一误分类） |

**建议阻断项（P0）**：① 把 `run_adoption_gate` 接入 `pre_push` 与 `scheduled_audit`；② 修 `ci_policy` 读取 `ci_blocking`（或把 G-REPRO-1 的 legacy-adopted 分支改为 `FAIL`）；③ 给 `run_dividend_stress.py` 补 `sys.exit` 并在输出"可进入 Phase 4"前调用 `evaluate_acceptance`。
