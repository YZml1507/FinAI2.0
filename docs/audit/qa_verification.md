# QA 对抗性验证报告（门禁"假绿→真拦"改造）

- 审计人：QA 工程师（严过关）
- 审计性质：**只读** + 对抗性证伪（未修改任何代码/测试/文档，仅新建本报告）
- **锚定快照时间：2026-09-10 16:09:12（+0800）**
- ⚠️ 审计期间工程师正在并发改 `scripts/gates/`（`runner.py` mtime 16:07:59、`gate_consistency.py` 16:07:51、`hooks/pre_push.py` 16:09:18）。本报告所有数字均锚定上述快照，快照 md5 见 §7。后续可能漂移。

---

## 1. 结论摘要（≤150 字）

**部分生效。** 门禁从"空转"变为"能拦 FAIL"：`is_pass` 仅认 PASS、总调度器分行报 SKIP/INCONCLUSIVE、G-MDD-1 实测能 FAIL（150402 的 43.08%）。但仍有 **8 类"无证据即通过"兜底**在真实路径生效（最严重 E-2 恒 PASS、L-3 运行期自证式恒 PASS）。**pre-push 只拦 FAIL、完全忽略 INCONCLUSIVE**——把 3 条 FAIL 修绿后，空 ctx 下 pre-push 会放行。

---

## 2. 任务 A：漏网"无证据即通过"兜底清单

> 真实路径是否生效 = 依据 `_build_post_run_gate_context`（scripts/run_dividend_backtest.py:198-245）与 `grep` 提供方确认。

| # | 文件:行号 | 模式 | 真实路径是否生效 | 严重度 |
|---|---|---|---|---|
| A1 | `scripts/gates/runner.py:477-478` → `gate_e_engine.py:149-158` | E-2 `ctx.get("fifo_errors", [])` / `ctx.get("final_positions", {})` 双空 ⇒ PASS("缺股崩溃率 0%") | **✅ 生效**。`_build_post_run_gate_context` 不提供该两键 ⇒ 必然空 ⇒ E-2 恒 PASS | 高 |
| A2 | `scripts/gates/runner.py:410-411` | L-3 `exec_calls = ctx.get("executed_calls", list(req_calls))`，默认**拷贝 required** ⇒ 运行时 `missing_exec` 恒空 | **✅ 生效**（pre-run 路径）。运行时检查不可能失败，只有 AST 检查有牙 | 高 |
| A3 | `scripts/gates/runner.py:392` → `gate_d_data.py:387-397` | D-5 `orders = ctx.get("orders", [])` 空 ⇒ PASS("无委托记录，通过") | **✅ 生效**（真实 pre-run 无 orders ⇒ D-5 恒 PASS，CRITICAL 门禁空转） | 中 |
| A4 | `scripts/gates/runner.py:397` | L-1 `active_features = ctx.get("active_features", ["DIVIDEND_TAX"])`，未启用也判 PASS | **✅ 生效**（pre-run 未传即用默认，掩盖配置缺失） | 中 |
| A5 | `scripts/gates/runner.py:522-526` → `gate_a_accounting.py:240` | A-3 无 `roundtrip_total_fee` 时用 `compute_fees(...)` **现算基准** ⇒ 恒 112.82 ⇒ PASS | **✅ 生效**（自证式：门禁用自己算的常量证明自己通过） | 中 |
| A6 | `scripts/gates/runner.py:516-518` → `gate_a_accounting.py:131-141` | A-2 `journal_entries` 空 ⇒ `_build_daily_cash_flows` 返回 `[]` ⇒ PASS("无现金流水，通过") | **⚠️ 条件生效**（真实回测有流水时不触发；账本异常清空时静默放行） | 中 |
| A7 | `gate_d_data.py:167` (D-2) | 仅 `n>=30 and std<1e10` 才 FAIL ⇒ **n<30 永不 FAIL** | 部分生效（真实全池 >30，风险低） | 中 |
| A8 | `gate_l_liveness.py:176-201, 216-227` (L-2) | `len(common_keys)<3 ⇒ PASS`、`长度不匹配 ⇒ PASS`、`等权 ⇒ PASS` | runner 未调用 L-2（不影响后置链路） | 低 |

**gate 内部"空数据⇒PASS"清单**（仅在 runner 传入非空 dict 而子列表为空时触发；真实回测有数据时不触发，列为潜在项）：
`gate_d_data.py:58`（D-1 样本<2⇒PASS）、`:251`（D-3 样本<60⇒PASS）、`:320`（D-4 无 bars⇒PASS）、`:394`（D-5 无 orders⇒PASS）；`gate_a_accounting.py:53`（A-1 无 trades⇒PASS）、`:138`（A-2 无 flows⇒PASS）、`:302`（A-4 无 trades⇒PASS）；`gate_e_engine.py:194`（E-3 无 trades⇒PASS）。

**已修复项（审计中途）**：任务书点名的 E-1 兜底 `{c: True for c in STANDARD_CASES}` 在我**首次读取时仍存在于 `runner.py:461-463`**；工程师于 16:07:59 改为"无证据⇒INCONCLUSIVE"。当前实测 `run_post_run_gates({})` → E-1 = `INCONCLUSIVE`（已修）。`grep -rn "True for" scripts/gates/` 现无输出。

---

## 3. 任务 B：SKIP / INCONCLUSIVE 漏洞结论（含 pre-push 实际行为）

**pre-push 阻断逻辑（scripts/hooks/pre_push.py:95-98，快照 md5 `bc8ddd09…`）：**
```python
blockers = [r for r in results
            if r.status == GateStatus.FAIL and r.severity in (BLOCKER, CRITICAL)]
```
→ **只认 FAIL**。`INCONCLUSIVE` 与 `SKIP` 完全不被判定。

**实测结论（`GateMasterAudit().audit(context={})`）：**
- 状态分布：`SKIP:23 / PASS:1 / FAIL:3 / INCONCLUSIVE:1`（total 28）
- FAIL 3 条：`G-MDD-1`(BLOCKER)、`G-DOC-1`(CRITICAL)、`G-REF-1`(CRITICAL) → 均为阻断级
- INCONCLUSIVE 1 条：`G-STRESS-1`(CRITICAL)

| 问题 | 结论 |
|---|---|
| pre-push 现在会拦 FAIL 吗？ | **会**。当前 3 条 FAIL 全为 BLOCKER/CRITICAL ⇒ `blockers` 非空 ⇒ `sys.exit(1)` |
| pre-push 会拦 INCONCLUSIVE 吗？ | **不会**。`G-STRESS-1` INCONCLUSIVE 被完全忽略 |
| 空 context 下 pre-push 会放行吗？ | **会（在 FAIL 修绿后）**。把 G-MDD-1/G-DOC-1/G-REF-1 修绿后，空 ctx 只剩 `SKIP:23 + INCONCLUSIVE:1`，`blockers=[]` ⇒ `[ALL PASS]` 放行 |
| 门禁用 SKIP 逃逸？ | 空 ctx 下 23 道门禁全部走 `if not context: SKIP`，即 S/E/A 维在 pre-push 路径上**等于不设防** |

**其它入口是否同步了新语义：**
- `.githooks/pre-push`：仅 wrapper 调 `scripts/hooks/pre_push.py` ⇒ 语义同上（未同步）。
- `.github/workflows/ci.yml` Gate Check 4：`gate_master_audit --strict`；`gate_master_audit.py:250` 亦为 `any(r.status == GateStatus.FAIL)` ⇒ **同样只拦 FAIL，忽略 INCONCLUSIVE**。
- `.github/workflows/ci.yml` Gate Check 5：`pytest tests/ -q -p no:cacheprovider`；步骤名写 ">=725 passed" 但**命令本身无 passed 计数断言**，仅靠 `set -euo pipefail` 要求退出码 0。

> 结论：**"改了 A（汇总/语义）没改 B（阻断器）"成立**。`is_pass` 与 `print_summary` 已正确区分，但**所有"阻断/放行"决策点（pre_push.py、gate_master_audit --strict、CI）仍只认 FAIL**。

---

## 4. 任务 C：测试基线复跑 + 反向锁定验证

### 4.1 复跑结果
```
第 1 遍: 742 passed, 1 warning in 17.93s
第 2 遍: 742 passed, 1 warning in 16.50s
```
- 两遍均 **742 passed**，无差异。
- `ls runs/*.json` ⇒ `No such file or directory`（**无残留**）；`experiments/runs/*.json` 稳定 4 份。
- shim 判断：唯一 warning 是 `PytestWarning: (rm_rf) error removing …pytest-of-…` +
  `[safe-delete][SAFE_DELETE_FAIL_CLOSED] / [SAFE_DELETE_BULK_CONFIRM_REQUIRED]`（`@pytest` 临时目录清理阶段）。
  判据：① 警告类型为 tmpdir 清理（`rm_rf`），② 测试计数 **742 passed / 0 failed**，③ 退出码 0。
  ⇒ **属 shim 环境问题，非真回归**。

### 4.2 反向锁定验证（test_t405_compliance_and_paper_e2e.py）
- 现断言（第 89-91 行）：`assert _pct(metrics["annual_turnover"]) in text`，其中 `_pct()` 读权威产物 = `"201.14%"`。
- 实测文档：`strategy_description_template.md:17` 含 `201.14%`；**`line:155` 仍含 `92.51%`**（`grep -c "92.51%"` = 2）。
- **推理结论（未改文件，破坏性验证未执行）**：
  - 若把 `201.14%` 删除/改错 ⇒ `"201.14%" in text` 为假 ⇒ **测试变红** ⇒ 断言具备"真值缺失→红"性质。
  - 但文档**同时保留 `92.51%`** 时测试仍绿 ⇒ **不具备"错误值存在→红"性质**。
  - 即该断言只保证"真值在场"，不保证"旧值退场"；这正是需要 G-DOC-1 独立把关的原因。
- 空壳/永真断言扫描：`grep -rn "assert True" tests/` **无输出**；`pytest.skip` 仅 `tests/test_t312_dividend_backtest.py` 6 处（数据未采集守卫，合理）；`tests/test_gate_consistency.py` 17 例（SkipSemantics 3 / MDD 4 / Stress 4 / DOC 3 / REF 3）**均有实质断言**。

---

## 5. 任务 D：4 道新门禁的"能 FAIL"证据 + 逃逸口

### G-MDD-1
| 注入式反例 | 实测结果 | 证据 |
|---|---|---|
| 权威 150402（MDD 43.08%，rt=78） | **FAIL**（rc=1） | `--mdd …150402…json` → `MDD=0.4308 > 0.35` |
| 0 成交产物 20260903-135508（rt=0） | **INCONCLUSIVE** | `round_trips=0 ⇒ 平凡成立，不得据此判 PASS` |
| inline `round_trips=5, MDD=0.50` | **FAIL** | `MDD=0.5000 > 0.35（往返 5 笔）` |
| 正常产物（rt=131, MDD=0.2537） | PASS（能真绿，非只会 FAIL） | 全仓扫描时该两份 0.2537 ⇒ 不进入 FAIL |

**回答任务书反问**：`round_trips=5 且 MDD=0.50` ⇒ **判 FAIL**（实测，无小样本逃逸）。
真正的边界是反向的：门禁**无最小样本量要求**，`rt=1 & MDD=0.10` 也会 PASS（弱证据），但这是"证据弱"而非"逃逸 FAIL"。

### G-STRESS-1
| 注入 | 结果 |
|---|---|
| 空 ctx（`--stress-rt 0`） | **INCONCLUSIVE**（`round_trips=0`） |
| `rt=78, days=2420`（≥200 日且有成交） | **PASS** ✔（非"只会 INCONCLUSIVE"） |
| `rt=15, days=60`（<200 日） | INCONCLUSIVE（样本不足） |

### G-DOC-1
- 实测：`检出 89 处`（快照）。**工程师报的"50"= 读了 `metrics.violations[:50]` 的截断长度**，非真实总数。
- **确证的规则误报（Unicode 负号）**：
  - `_FULLWIDTH_MAP` 未映射 `U+2212 MINUS SIGN`（`"−" in map` = `False`）。
  - 实测：`CAGR −3.20%`(U+2212) ⇒ `_line_numbers` 得 `3.20`（正数）、`first_after_CAGR` = `3.20`；`CAGR -3.20%`(ASCII) ⇒ 正确得 `-3.20`。
  - 后果：`docs/diagnosis/t312_full_period_diagnosis.md:13`（原文含 `CAGR −3.20%`，**正确值**）与 `docs/project_status_flowchart.md:162`（`CAGR −3.20%`，**正确值**）被误报为"doc=27.72/3.20 ≠ exp=-3.20"。
- **三类计数（88 处可归类 + 我按语义裁定）**：

| 类别 | 约数 | 例子 |
|---|---|---|
| ① 合规包/权威仓待重写（**预期红**） | ~29 | `compliance/strategy_description_template.md`(9)、`compliance/filing_checklist.md`(3)、`spec/…/tasks.md`(7)、`T312_FINAL_SUMMARY.md`(2)、`t312_dividend_strategy.md`(5)、`t312_implementation_summary.md`(3) |
| ② 历史文档残留 | ~22 | `delivery/GATE_PHASE1/2_*`(3)、`delivery/PHASE4_*`(1)、`delivery/T312_EXECUTION_READY.md`(6)、`phase35_recommendation.md`(5)、`T312_READY_FOR_EXECUTION.md`(4)、`task_completion_summary.md`(2)、`paper_trading/paper_trading_ledger.md`(1) |
| ③ **规则误报 / 口径越界** | ~38 | **Unicode 负号误报 2 例**（上）；跨产品：`momentum_backtest_summary.md`(12)、`t304_stress_report.md`(4)、`t305_technical_review.md`(7)（讲的是**动量策略**，却比对红利 T312 真值）；跨场景：`t313_dividend_stress_report.md`(13)（**压测窗口**指标却比对全周期真值，skip 短语未覆盖） |

### G-REF-1
- 实测：**14 处**（任务书给 15；审计初期我实测亦为 15，其中 `data/collector._atomic_write_parquet` 函数名误报已被工程师于审计中途通过收紧 `_ALLOWED_REF_EXTS` 修掉 ⇒ 15→14）。
- 逐条裁定：

| 引用 | 裁定 | 依据 |
|---|---|---|
| `runs/20260907-150402-…json`（T405:40） | **真缺陷/写法错** | 实际在 `experiments/runs/`，`runs/<file>.json` 不存在 |
| `runs/index.jsonl`（tasks.md:38） | **真缺陷/写法错** | 实际在 `experiments/runs/index.jsonl` |
| `ops/feishu_alert.py`（T404:238） | **真缺陷（幽灵引用）** | 不存在 |
| `data/stress_test/`（t313:39） | **真缺陷（幽灵引用）** | 不存在 |
| `experiments/t312-dividend-v1/`、`…/latest_run.json` | **真缺陷（幽灵引用）** | 不存在 |
| `scripts/collect_full_market_data.py`、`scripts/run_momentum_backtest.py` | **真缺陷（幽灵引用）** | 不存在 |
| `data/fundamentals.py`、`data/feed.py` | **真缺陷（幽灵引用）** | 不存在 |
| `paper_trading/paper_trading_ledger.md`（README:88） | **误报** | 系 docs/README.md 的**相对链接**，实际文件 `docs/paper_trading/paper_trading_ledger.md` **存在**；门禁按仓库根解析，未按包含文档目录解析 |
| `reporting/metrics.py`（t309:175） | **误报/待裁定** | 实际文件为 `backtest/metrics.py`；行文指代 `FeeItem` 汇总位置，非严格路径 |
| `paper_trading/state.json`、`paper_trading/data/nav_series.parquet` | **误报（白名单缺口）** | 运行期生成物，但白名单只放了 `experiments/runs/`、`runs/paper_trading/` |

---

## 6. 我报的数字 vs 任务书给的数字

| 门禁 | 任务书 | 我实测（快照 16:09:12） | 差异说明 |
|---|---|---|---|
| G-DOC-1 | 89 | **89**（message）；metrics 截断 50 | 工程师报"50"= 误读 `violations[:50]` 截断长度 |
| G-REF-1 | 15 | **14**（审计初期 15） | `collector._atomic_write_parquet` 函数名误报被工程师于我审计中途修复（`_ALLOWED_REF_EXTS` 收紧） |
| pytest | 742 | **742 / 742**（两遍） | 一致 |
| 总门禁 | 28 | **28**（SKIP23/PASS1/FAIL3/INCONCLUSIVE1） | 一致 |

---

## 7. 诚实边界

1. **未做端到端真实回测**：因离线数据与耗时，`_build_post_run_gate_context` 的真实 ctx 只做**静态审读 + grep 提供方**，未实际验证 S/G 维在真实数据流下的最终 status（如 S-4 是否 INCONCLUSIVE、E-2 是否恒 PASS 的运行时表现）。
2. **目标在漂移**：`runner.py`/`gate_consistency.py`/`pre_push.py` 在我审计期间被改。所有数字锚定 16:09:12 快照；快照指纹：
   `base.py=5dd3195d…  gate_consistency.py=385d2610…  gate_e_engine.py=df380252…  runner.py=c0566edc…  gate_master_audit.py=d9f76cd7…  pre_push.py=bc8ddd09…`（md5，见 §附录命令）。快照后若再改，结论需重跑。
3. **未执行破坏性验证**：任务 C 的"把文档改回 92.51%"仅做只读推理（受硬约束限制），未真实改文件。
4. **G-DOC-1 三类划分非机读**：①②③ 的归属是我按语义人工裁定，边界可争议；其中"Unicode 负号 2 例"为**机读可复现的铁证**，其余"跨产品/跨场景"属设计口径争议。
5. **未验证 CI 远端真实执行**：只读 `.github/workflows/ci.yml`，未跑 GitHub Actions。
6. **未验证新增临时文件影响**：工作区存在未跟踪的 `.diag_doc.py`、`.probe.out`、`.junit.xml`、`1.ipynb` 等，未评估其是否干扰门禁或提交。

---

## 附录：复现命令与原始输出

```bash
# 门禁总数/状态分布（空 ctx）
py -3.11 -c "from scripts.gates.gate_master_audit import GateMasterAudit;from collections import Counter;print(Counter(r.status.value for r in GateMasterAudit().audit({},strict=False)))"
# => Counter({'SKIP': 23, 'PASS': 1, 'FAIL': 3, 'INCONCLUSIVE': 1})

# G-MDD-1
py -3.11 -m scripts.gates.gate_consistency --mdd experiments/runs/20260907-150402-t312-dividend-v1-noseed.json   # FAIL, rc=1
py -3.11 -m scripts.gates.gate_consistency --mdd experiments/runs/20260903-135508-t312-dividend-v1-noseed.json   # INCONCLUSIVE

# G-STRESS-1
py -3.11 -m scripts.gates.gate_consistency --stress-rt 0                                            # INCONCLUSIVE
py -3.11 -m scripts.gates.gate_consistency --stress-rt 78 --stress-days 2420                        # PASS

# 反向锁定证据
grep -c "92.51%"  docs/compliance/strategy_description_template.md   # => 2 (line17 对比表 / line155 正文)
grep -c "201.14%" docs/compliance/strategy_description_template.md   # => 1 (line17)

# Unicode 负号误报铁证
py -3.11 -c "from scripts.gates.gate_consistency import _line_numbers,_FULLWIDTH_MAP;print(_line_numbers('CAGR −3.20%'), '−' in _FULLWIDTH_MAP)"
# => [Decimal('3.20')] False   （U+2212 未归一化 ⇒ 负号丢失）

# pre-push 阻断器（只认 FAIL）
sed -n '95,98p' scripts/hooks/pre_push.py

# 快照指纹
md5sum scripts/gates/*.py scripts/hooks/pre_push.py
```
