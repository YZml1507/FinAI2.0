# QA 终验报告：门禁改造（M3）独立复核

> 复核人：严过关（QA，`software-qa-engineer-2`）
> 日期：2026-09-10 ｜ 仓库：`D:\Projects\FinAI2.0`（Windows / bash / `py -3.11`）
> 立场：**证伪优先**。凡无原始命令输出支撑的结论，均标注「未验证」。
> 本轮**只读**：未改动任何代码/测试，未 commit；仅新建本文件。

---

## 1. 结论摘要（≤150 字）

机制大体为真：元测试是真防线（能抓恒过门禁）、逃生阀有醒目告警且只走 stdout、753 连跑两遍一致无残留。但**不可交付**：当前 pre-push 有 **21 项永久阻断**（含 3 项真实 FAIL：MDD 43.08%、文档 29 处不符、14 处幽灵引用），任何推送都会被拒，唯一出路是逃生阀——恰是"门禁变摆设"的失败模式。且 18 项 INCONCLUSIVE 中至少 **5 项（D-1/D-2/D-3/D-4/E-2）推送期明明可取证却未取**。另有 2 处 ⑦ 残留 + 1 处退出码背离 + 1 处 E-1 恒过洞。

---

## 2. A 段：元测试 `test_every_gate_has_at_least_one_failing_input` 是否真防线

**证据文件**：`tests/test_gate_consistency.py:307-309`（白名单）、`:312-355`（`_violating_context`）、`:370-383`（元测试本体）。

### 2.1 是「真注入违规输入」还是「宽松判定」？—— 结论：**介于两者之间，偏真，但严重不特异**

- `_violating_context(gate_id, tmp_path)` 返回的是 **按 `gate_id` 查表**（`:355 mapping.get(gate_id)`）的**每门禁专属 ctx**，**不是**"任何 gate 都会 FAIL 的万能非法值"。这条最坏的担心**不成立**。
- 实测：对单一通用非空 ctx `{"_bogus":1}`，28 道门禁里只有 **6 道 FAIL**（`S-5,G-1,G-4,G-MDD-1,G-DOC-1,G-REF-1`），19 道 INCONCLUSIVE，2 道 PASS。若真是"万能违规值"，应几乎全 FAIL。**故不是假防线**。
- **但**：实测**交叉污染 153 对**（27 个专属 ctx 中，**每一个**都同时让另外 **4–6 道**不相干门禁判 FAIL；详见 §2.4）。也就是说这些 ctx 事实上多数只是"非空、但不含目标门禁所需键"的通用形态，其被判 FAIL 的原因多为 fail-closed 默认（缺键），而非"精准触发该门禁的违规类"。
- 结论：测试实际锁定的性质是「**任何门禁都不得在"非空且不合规"的 ctx 上判 PASS**」——弱于"每道门禁都能识别自己的违规类"，**但不是永真壳**。

### 2.2 是否遍历全部 28 道？

**是。** 实测 `GateMasterAudit.get_standard_gates()` 返回 **28** 项且 `gate_id` 唯一（`gate_master_audit.py:80-118`）；元测试 `:373` 直接遍历该返回值，无子集过滤。

```
registered gates: 28 unique ids: 28
ids: [D-1,D-2,D-3,D-4,D-5,L-1,L-2,L-3,E-1,E-2,E-3,A-1,A-2,A-3,A-4,
      S-1,S-2,S-3,S-4,S-5,G-1,G-2,G-3,G-4,G-MDD-1,G-DOC-1,G-STRESS-1,G-REF-1]
```

### 2.3 漏洞检查（新增门禁 / 蒙混）

- **新增恒过门禁但忘记加分支** → `_violating_context` 返回 `None` → `:377-378 offenders.append(f"{gate_id}(缺违规输入)")` → **测试失败**。实测 `HOLE-A：ctx is None => True`，**能抓住**。
- **新增恒过门禁并给了分支，但门禁仍 PASS** → `:380-382 res.status != FAIL` → `offenders.append("(got PASS)")` → **测试失败**。实测 `HOLE-B -> offenders gets: PASS`，**能抓住**。
- **能否用"万能违规 ctx"蒙过？** 若有人在 `_violating_context` 里给新门禁塞一个"只对本门禁 FAIL、对别人无害"的**特制** ctx（例如 `if ctx.get("__magic__"): return FAIL` 的门禁），本测试**抓不到**——它只能证明"存在一个输入使该门禁 FAIL"，不能证明该输入是"该门禁的合法违规类"。但这属于**主动造假**，代码评审可查。**风险等级：低。**
- **本测试的两个方法论盲区（重要）**：
  1. **只验"能 FAIL"，从不验"在合法证据下能 PASS"** → **恒 FAIL（过严）门禁不会被发现**。配套 `test_no_gate_passes_on_empty_context_except_self_sourced`（`:361-368`）只查空 ctx 不 PASS，覆盖不到。
  2. **不校验 FAIL 的"原因"** → 门禁可能因无关原因 FAIL 而被误认为"防线有效"（§2.4 交叉污染正是此情）。
- **未被本测试覆盖的真实恒过洞**：`MustFailCasesGate`（E-1）在**任意非空、但不含 `must_fail_results` 键**的 ctx 下返回 **PASS**（详见 §7 第 1 条）。因 E-1 确有"能 FAIL"的输入，元测试**放行**了它。

### 2.4 交叉污染实证（元测试"不特异"的量化证据）

```
per-context contamination (how many OTHER gates also FAIL)
  D-1 -> also-FAIL 6: [S-5, G-1, G-4, G-MDD-1, G-DOC-1, G-REF-1]
  ... （27 个 ctx 全部如此，S-5/G-1/G-4/G-MDD-1/G-DOC-1/G-REF-1 反复出现）
  E-2 -> also-FAIL 6: [S-5, G-1, G-4, G-MDD-1, G-DOC-1, G-REF-1]
  G-REF-1 -> also-FAIL 4: [S-5, G-1, G-4, G-MDD-1]
```
根因：`S-5` 缺 `code_evidence` 即 FAIL；`G-1` 缺合法 `git_commit` 即 FAIL；`G-4` 无有效签名即 FAIL；`G-MDD-1/G-DOC-1/G-REF-1` 会**忽略 ctx 直接扫全仓**（权威产物 MDD 43%、文档 29 处不符、14 幽灵引用）→ 必 FAIL。

### 2.5 `_META_NO_FAIL_WHITELIST`

**只有 1 项**：`{"G-STRESS-1": "有效性门禁只产出 PASS/INCONCLUSIVE（0 成交/样本不足即 INCONCLUSIVE），按设计不产出 FAIL"}`（`:307-309`，实测 `whitelist entries: 1`）。理由**成立**：该类有效性门禁设计上确实只有 PASS/INCONCLUSIVE（`gate_consistency.py:281-335`）。配套 `test_meta_whitelist_has_reasons`（`:385-387`）要求非空理由，OK。**白名单本身无夹带。**

---

## 3. B 段：18 项 INCONCLUSIVE 分类表

**前提证据（实测）**：真实 ctx = 14 键（`pre_push.py:89-172`，`ctx 键 14 个`）；真实分布 **PASS 7 / FAIL 3 / INCONCLUSIVE 18 / SKIP 0**，blockers = 21 —— 与工程师自述**完全一致**。

**可用数据（实测）**：
- `data/dividend_stocks/<sym>/<year>.parquet` **含 `market_cap`、`dividend_yield`、`tradestatus`、`volume`、`close`、`preclose`**（17 列）；`sh.600000` 等 487 个子目录。
- `data/dividend_stocks/exdiv/*.parquet` **487 个**（列：`date,factor,cash_dividend`）。
- `data/etf_bars/sh.510300/{2015..2024}.parquet` 全序列 2431 行（沪深 300 ETF）。
- 权威产物 `metrics.fees_total` 全量：`{COMMISSION:1556.18, DIVIDEND_TAX:5043.75, STAMP_TAX:2651.94, ...}`；`total_return=-0.2772`。

| 编号 | 门禁 | 推送期能否取证 | 判定 | 建议归属 |
|---|---|---|---|---|
| D-1 | 原始日线跳变 | **能**：`dividend_stocks/*/*.parquet` 有 `close/preclose`，实测喂真实 bars → **PASS** | **软性失败** | 推送期（现成数据，零增量成本） |
| D-2 | 流通市值偏离 | **能**：`market_cap`+`amount` 列，487 标的 ≥30，实测喂 60 标的 → **PASS** | **软性失败** | 推送期 |
| D-3 | PIT 动态股息率 | **能**：`dividend_yield` 全年 242 天/66 变异值，实测 → **PASS** | **软性失败** | 推送期 |
| D-4 | 停牌日成交量 | **能**：`tradestatus`+`volume`，实测 → **PASS** | **软性失败** | 推送期 |
| D-5 | 高价股/整手 | 否：产物**无委托明细**（orders） | 客观不可判 | 回测运行期（落盘委托） |
| L-1 | 特性扣费存活 | 部分：配置可由 `params` 推；**账本流水**产物无 | 客观不可判 | 回测运行期 |
| L-2 | 权重分配保真 | 否：需 `target_weights`/`actual_values`（run 内部） | 客观不可判 | 回测运行期 |
| L-3 | 调用链审计 | 否：需 `executed_calls` 运行期追踪（无 trace 落盘） | 客观不可判 | 回测运行期（插桩落盘） |
| E-2 | 送转 FIFO | **能**：`exdiv/*.parquet` 441+ 在仓，可仿 E-1 加真跑探针 | **软性失败** | 推送期（加探针） |
| E-3 | 滑点板价 | 否：需逐笔 `price/limit_up/limit_down` | 客观不可判 | 回测运行期 |
| A-1 | 七科目费用平衡 | 否：需**逐笔** `trades.fees`；产物仅 `fees_total` 聚合 | 客观不可判 | 回测运行期（落盘逐笔） |
| A-2 | 每日现金流守恒 | 否：需 `daily_cash_flows`/`journal_entries` | 客观不可判 | 回测运行期 |
| A-3 | 黄金算例 | 弱能：可调 `backtest/fees.compute_fees` 独立复算 10 万往返（**与被检引擎同源**，证据强度弱） | 边界：可部分取证 | 推送期（静态金标准常量） |
| A-4 | 分段费率穿透 | 否：需逐笔卖出 `date/amount/stamp_tax` | 客观不可判 | 回测运行期 |
| S-2 | 破 MA200 空仓 | **半能**：指数侧可算（`etf_bars/sh.510300`，实测 1163 个破位日）；**策略逐日仓位**不可得 | 客观不可判（仓位侧） | 指数侧推送期 + 仓位侧回测运行期 |
| S-3 | 滑点抗压 | **半能**：`baseline_return` 可由 `metrics.total_return` 得；`stress_return` 需逐笔名义额 | 客观不可判（压力侧） | 回测运行期 |
| S-4 | 红利税锁定 | **半能**：`fees_total.DIVIDEND_TAX=5043.75` 可得；`total_dividend_received` 不可得 | 客观不可判 | 回测运行期 |
| G-STRESS-1 | 压测有效性 | 否：无压测区间产物（stress run 未落盘 `round_trips`） | 客观不可判 | 回测后（压测 run 落盘） |

**统计**：**软性失败 5 项**（D-1、D-2、D-3、D-4、E-2）；**客观不可判 12 项**（D-5、L-1、L-2、L-3、E-3、A-1、A-2、A-4、S-2、S-3、S-4、G-STRESS-1）；**边界 1 项**（A-3）。

> **关键结论**：D-1/D-2/D-3/D-4 的"客观取不到证"是**假**的——仓库里现成含所需列，实测四道全 **PASS**。这是**取证成本低但没取**的软性失败。E-2 数据在仓（487 exdiv parquet），只是没写探针。

### 建议架构（回应"推送期是否只能在推送期 INCONCLUSIVE"）

**当前设计的真实后果**：21 项阻断**永久存在**（3 项真实 FAIL + 18 项 INCONCLUSIVE）→ `run_master_gate_guard()` 恒 `ok=False`（实测）→ **任何推送都被拒** → 唯一出路是 `FINAI_SKIP_PUSH_GATES=1` → **门禁重新变摆设**。这正是最担心的风险，且**已成立**。

建议分三层：

1. **推送期（静态可判）**：只跑"仓库现成数据即可判定"的门禁 —— **D-1/D-2/D-3/D-4**（读 `dividend_stocks`）、**E-2**（加真跑探针，仿 E-1）、**A-3**（静态金标准常量）、**S-2 指数侧**；加上已有 PASS 的 E-1/S-1/G-1~G-4。
2. **回测运行期（`run_post_run_gates`）**：D-5/L-1/L-2/L-3/E-3/A-1/A-2/A-4/S-4 需要 run 内部真相 —— 应让**回测落盘**这些证据（逐笔 trades、journal、executed_calls trace、逐日仓位），并在 run 期判定；判定结果写入产物签名。
3. **回测后/定时全量 CI**：G-STRESS-1、S-3（压测/滑点情景）改挂 `run_post_run_gates` + **定时全量 CI**；推送期审计只读产物中"已由 run 期判定通过"的签名记录，而非重跑无证据门禁。

**另**：推送期 ctx 需新增"**读取 run 期已落盘的门禁结论**"路径；否则推送期永远无法确认 run 期门禁是否真跑过。

---

## 4. C 段：逃生阀风险评估

**实现**（`scripts/hooks/pre_push.py:207-216`）：`main()` 中 `os.environ.get("FINAI_SKIP_PUSH_GATES","")` 命中 `1/true/yes/on` 时，打印**醒目告警 + 时间 + 操作者**，然后跳到 `[ALL PASS]`。

实测（monkeypatch `run_pytest_guard` 为 PASS，把 `run_master_gate_guard` 换成 canary）：

```
--- main() WITH escape valve ---
[!!! 紧急逃生阀已开启 !!!] FINAI_SKIP_PUSH_GATES=1 —— 跳过六维门禁总检
[!!! 留痕] 时间=2026-09-10T16:39:10+08:00；跳过项=Gate Master Audit；操作者=MengLin
gate guard called? False  => 逃生阀确实跳过门禁
--- main() WITHOUT escape valve ---
[-] [BLOCKED] should-not-be-called ｜ SystemExit: 1 ｜ gate guard called? True
```

结论与风险：
- **确实只在 stdout 告警，无任何持久化留痕**（无写文件、无日志）。"留痕"是**易失**的——关掉终端即消失。**建议**：跳过时**强制写**一条 `runs/gate_skip_audit.jsonl`（时间/操作者/git SHA/被跳项），否则"留痕"名不副实。
- **别名/隐蔽绕过检查**：`grep -rn "FINAI_SKIP|SKIP_PUSH|environ.get|getenv" scripts/hooks/ scripts/gates/` **仅命中 `pre_push.py:208,215`**（另 1 处为 `.pyc`）。**无第二个别名、无其他 env 后门**。`pre_commit.py` 也无 env 逃生阀（无命中）。
- **域限定正确**：逃生阀**只跳过门禁总检（第 2 步），不跳过单测防倒退（第 1 步 `run_pytest_guard`）** —— 实测逃生阀分支在 pytest 之后。
- **异常吞并无"顺带跳过"**：`run_master_gate_guard` 内 `_build_pre_push_context()` 的取证用 `try/except` 吞异常（`:119-120,129-130,160-161`），但吞掉只会**少填证据 → 更多 INCONCLUSIVE → 更严**，不会"顺带放行"。方向安全。

**综合风险：中。** 机制本身无静默通道；但**因门禁恒不可通过（§3），逃生阀将从"紧急阀"退化为"常规阀"**——这是**设计层**风险，非实现层。

---

## 5. D 段：阻断路径一致性（⑦）

实测 6 条路径（原始命令见下）：

| # | 路径 | INCONCLUSIVE 是否阻断 | 退出码 | 一致？ |
|---|---|---|---|---|
| 1 | `gate_master_audit.py --strict` | 是 | `exit=1` | ✅ |
| 2 | `gate_master_audit.py --strict --mdd <0成交>` | 是 | `exit=1` | ✅ |
| 3 | `pre_push.run_master_gate_guard()` | 是（21 阻断） | `ok=False` | ✅ |
| 4 | `run_post_run_gates(strict=True)` | **否** | 未抛异常 | ❌ **⑦ 残留** |
| 5 | `run_pre_run_gates(strict=True)` | **否** | 未抛异常 | ❌ **⑦ 残留** |
| 6 | `gate_consistency.py --mdd <0成交>` | **否** | **`exit=0`** | ❌ **展示/退出码背离** |

**证据 1/2/3**（工程师自述的 `--strict` 与 pre_push 确实都拦 INCONCLUSIVE）：
```
=== P1: gate_master_audit --strict ===          exit=1
=== P2: gate_master_audit --strict --mdd <0成交> ===  exit=1  [INCONCLUSIVE] G-MDD-1 ...
=== C1: run_master_gate_guard ok= False ; msg head: 检出 21 项阻断性门禁未通过
```

**证据 4/5（新发现的 ⑦ 残留）**：
```
post_run(code_evidence) dist: {'INCONCLUSIVE': 15, 'PASS': 2}   FAIL: []
post_run(strict=True, only-INCONCLUSIVE) -> did NOT raise  *** ⑦ GAP ***
pre_run(strict=True) did NOT raise despite blockers: ['D-5','L-1','L-3']  *** ⑦ GAP ***
```
根因：`runner.py:272` 与 `:449` 的 `_check_result` 只在 `res.status == GateStatus.FAIL` 时抛出，**未纳入 INCONCLUSIVE**。而这两处正是**回测执行流**（run 期）实际调用的入口。即：**回测跑完 18 项 INCONCLUSIVE 不会被 run 期拦下，只有推送期才拦**——这正是 §3"架构风险"的机制根源。

**证据 6（展示/退出码背离）**：
```
=== P4: gate_consistency --mdd <0成交> ===  exit=0
[INCONCLUSIVE] G-MDD-1 最大回撤上限门禁   <-- 展示了 INCONCLUSIVE，退出码仍 0
```
根因：`gate_consistency.py:800,804,808,812` 均写 `rc |= 0 if res.status != GateStatus.FAIL else 1`，**只把 FAIL 记为失败**。这是**残留的"展示 INCONCLUSIVE 但退出码 0"路径**。

**D 段结论**：工程师自述的"`--strict` 与 pre_push 都拦 FAIL+INCONCLUSIVE"**成立**；但"⑦ fail-closed 覆盖所有拦截路径"**不成立**——存在 **3 处残留**（runner ×2 + consistency CLI ×1）。

---

## 6. E 段：独立复跑结果

```
=== RUN 1 ===  ... [100%] exit=0
=== RUN 2 ===  exit=0
753 passed, 1 warning in 15.81s
```
- **753/753，连跑两遍一致，退出码 0**。✅ 与工程师自述一致。
- **残留检查**：`git status --short runs/ experiments/runs/` → **空**；`find runs -newermt "-30 minutes" -type f` → **空**。**无残留**。✅
- 环境坑确认：输出出现 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]` 与 `PytestWarning: (rm_rf) error removing ...Temp\garbage-...`。**判定为 shim 噪声，非回归**：pytest 汇总仍 `753 passed`、退出码 0、`passed` 计数不受影响。
- `tests/test_gate_consistency.py` 单文件 **28 passed**；其中 `TestNoSilentPassMeta` + `TestInconclusiveBlocksEverywhere` **6 passed**。
- **空壳/永真断言抽查**：对 4 个新增/改动测试文件做 AST 扫描——**无任何 `test_*` 函数缺断言**（`assert` 或 `pytest.raises` 均计）；无 `assert True / 1==1 / ... or True` 模式。✅
- **但发现一处"测试被放宽"**（非空壳，但守恒需知悉）：`tests/test_t405_compliance_and_paper_e2e.py` diff 把
  `assert "717 passed" in text` → `assert re.search(r"\d+\s+passed", text)`、
  `assert "24" in text` → `assert re.search(r"\d+\s*(项|道)[^\n]{0,12}门禁", text)`，
  **由"核对具体数字"放宽为"存在任意数字"**（CAGR/MDD 两处则相反，改为从产物动态复算，**变严**）。净效果：文档计数不再被核对；但该职责已由门禁 G-DOC-1 承担，**可接受但应记录**。

---

## 7. 与工程师自述不符之处

1. **【新】E-1 `MustFailCasesGate` 存在恒过（fail-open）洞**——自述"⑩ 无证据 ⇒ INCONCLUSIVE"未覆盖此门禁类。实测：`MustFailCasesGate().evaluate({"_x":1})` / `{"trades":[]}` / `{"orders":[]}` / `{"note":"anything"}` **全部返回 PASS「5 必挂极限用例全部检验通过」**（`gate_e_engine.py:52` `results=context.get("must_fail_results",{})` 缺省空 dict → 无 failed → PASS）。**缓解**：runner 在 `must_fail_results` 缺失时注入 INCONCLUSIVE（`runner.py:467-478`，实测 `via runner -> INCONCLUSIVE`），且 pre_push ctx 必带 `must_fail_results`（实测全 True）。**但门禁类本身仍恒过**，元测试（因 E-1 有能 FAIL 的输入）与空 ctx 测试（空 → SKIP）**均未拦住**。
2. **【新】⑦ 未覆盖全部拦截路径**：自述"⑦ fail-closed …`--strict` 与 pre_push 都拦 FAIL+INCONCLUSIVE"——这两条**属实**；但 `run_pre_run_gates(strict=True)` 与 `run_post_run_gates(strict=True)` **不拦 INCONCLUSIVE**（`runner.py:272,449` 仅 FAIL），属**未声明的残留**。
3. **【新】退出码背离残留**：`gate_consistency.py --mdd <0成交>` 展示 INCONCLUSIVE 却 `exit=0`（`gate_consistency.py:800-812`）。
4. **【新】逃生阀"留痕"易失**：自述"时间/操作者留痕"——实测仅 stdout，**无持久化**，关终端即失。
5. **【新】测试被放宽 1 处**：`test_t405…` 将 `717 passed`/`24 道门禁` 的**具体数字核对**放宽为**任意数字正则**（见 §6）。
6. **【相符项，予以确认】**：753 两遍一致无残留 ✅；真实 ctx 分布 PASS7/FAIL3/INCONCLUSIVE18/SKIP0 ✅；blockers 21 ✅；`_build_pre_push_context()` 返回 `(dict[14键], str)` ✅；`must_fail_results` 在 ctx 中 ✅；D-1 `len(None)` 崩溃已修（实测 `{"bars":None}` → INCONCLUSIVE，无崩溃）✅；逃生阀无别名后门 ✅。

---

## 8. 诚实边界（未能验证 / 未验证项）

1. **未真跑 `git push`**：未复现真实 git hook 触发链（缺远端与推送条件），"推送被拒"由 `run_master_gate_guard()=False` 推得，**非端到端实测**。
2. **B 段"客观不可判"的判定基于"产物内不含所需证据 + 该类证据需 run 内部真相"的推断**，未逐条构造 run 期探针验证其"在 run 期确实可判"。A-3/S-2/S-3/S-4 的"半可判"为**部分取证**，未做定量。
3. **元测试"能否被特制 ctx 蒙过"仅做逻辑推演**（§2.3 第 3 点），未写对抗门禁实测。
4. **交叉污染的"上下文语义"未逐条归因**（只统计了 FAIL 集合），未验证每个 FAIL 是否"因缺目标键"而非"因真实违规"。
5. **`data/dividend_stocks` 仅抽验 `sh.600000`/`sh.600016` 等少量标的**；"487 标的全部可判定"为外推。
6. **未审计 `scripts/gates/tamper_guard.py` 的 `verify_run_signature` 细节**（G-4），仅由返回值判定。
7. **本报告全部命令输出为 2026-09-10 单次会话结果**；未在其他机器/时区复核 `git rev-parse`、时间戳类取证的可复现性。

---

## 附录：本次关键原始输出（可复现）

```
# 真实 ctx 分布
DISTRIBUTION: {'INCONCLUSIVE': 18, 'PASS': 7, 'FAIL': 3}   blockers: 21
SOURCE: 产物 20260907-150402-t312-dividend-v1-noseed.json（已签名）
CTX KEYS(14): [... total_return, total_stamp_tax, ...]

# 元测试防线实证
registered gates: 28 unique ids: 28
whitelist entries: 1 ['G-STRESS-1']
gates failing to FAIL: []
HOLE-A new恒过 gate, no branch -> ctx: None => offenders appended? True
HOLE-B 恒过 gate + 有分支 -> offenders gets: PASS

# D-1..D-4 真实数据可判
D-1 real: PASS     D-2 real: PASS (60 symbols)
D-3 real: PASS (242 days/66 unique)   D-4 real: PASS
S-2 data rows: 2431  below-MA200 days computable: 1163  (2015-01-05..2024-12-31)

# 复跑
753 passed, 1 warning in 15.81s   (×2, exit=0)

# 阻断路径
P1 --strict exit=1 | P2 --strict --mdd exit=1 | P4 gate_consistency --mdd exit=0(展示INCONCLUSIVE)
post_run(strict, only-INCONCLUSIVE) did NOT raise  |  pre_run(strict) did NOT raise
```
