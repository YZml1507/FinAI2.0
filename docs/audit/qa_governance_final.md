# 治理层新增代码 · 独立验证报告（证伪取向）

> **审计人**：QA 工程师（严过关）
> **审计日期**：2026-09-10
> **审计起点基线**：`b63ea6d8611b5811a44fec8f39863be478dc91aa`（团队-lead 所述「7 提交」版）
> **交付现行版**：`f8f34b3816…`（审计中新增的第 8 提交 `fix(m2)`，见 §0）
> **方法**：只读；仓外临时目录构造探针；`py -3.11`。凡无命令输出支撑的结论均标注「未验证」。

---

## 0. ⚠️ 审计期间代码库被并发推进（必须首先声明）

本报告开工时 `HEAD = b63ea6d`（团队-lead 所述「7 提交」版）。审计进行中，另一并发写入者对 `reporting/provenance.py`、`reporting/registry.py`、`scripts/gates/constants.py`、`tests/test_t206_registry.py` 做修改，并**在审计完成前将其提交为第 8 个提交 `f8f34b3`**（`fix(m2): 根除 provenance 静默兜底`）。因此本报告覆盖**两个修订**：

| 文件 | 起点版 `b63ea6d` `[HEAD]` | 现行版 `f8f34b3` `[CUR]` |
|---|---|---|
| `reporting/provenance.py` | `hash_path_manifest` 对不存在/空目录返回**常量** `sha256("")[:16]`；`repro_fingerprint` 用 `str(None)` 兜底 | 对不存在根 **抛 `MissingDataError`**；空目录/空序列返回 `None`；任一要素 `None` **抛 `ValueError`**（**已修**） |
| `reporting/registry.py` | `if code_hash and data_hash:` + `calendar_hash or "na"` | `all(v is not None ...)`，任一缺失 ⇒ 指纹 `None`（**已修**） |
| `scripts/gates/constants.py` | `TEST_BASELINE_PASSED = 790` | `= 796` |
| `tests/test_t206_registry.py` | 21 用例 | +6 用例（`TestProvenanceNullSafety`） |
| 真实收集数 | **790** | **796** |

**基线裁定**：以**现行交付版 `f8f34b3`** 为主；凡差异项逐条标注 `[HEAD]`（起点）/ `[CUR]`（现行）。审计时文件 md5（与两份修订一致）：
`provenance=078a1bbd… registry=e88a9063… constants=bbafd49c… gate_consistency=a7f187cb… acceptance=04a82279… context_builder=109e638a… gate_repro=c5ad8197…`
⇒ `f8f34b3` **只动了 provenance/registry/constants/test_t206**，`gate_consistency.py`/`acceptance.py`/`context_builder.py`/`gate_repro.py` **两版完全一致**，故 **A/C/D 段结论对两版同时成立**；仅 B 段与 E 段的基线数字/兜底条款需区分版本。

---

## 1. 结论摘要

**可作为「改进型交付」收下，但不能背书为「BLOCKER 不可绕过 / 复现性保护已生效」。** 共识别 **6 处逃逸口**。`repro_fingerprint` **并非恒 None**（团队-lead 假设不成立，已实证）。**最严重一条**：唯一「晋升/准入」路径是**手工 CLI**（未接入 CI/pre-push/pre-commit/registry，`registry.record_run` 无条件接受任何 `FINISHED` 产物），且该层**不校验签名、`schema_version`、`status`** ⇒ 未签名/legacy/`FAILED` 产物只要 `metrics` 数值好看即被放行 ⇒ 治理层承诺的「MDD 超限即 BLOCK 晋升」在工程上**不可强制**。

---

## 2. A 段：三层分层是否不可绕过

### A1. 穷举「读取 `experiments/runs/*.json` 并作通过/录用判定」的代码路径

```
$ grep -rn "record_run|\.list_runs(" --include=*.py .        # 见 §A1 输出
$ grep -rn "_run_artifact_files|_resolve_truth_artifact|_collect_artifacts|_collect_run_records" scripts/
```

| # | 代码路径 | 位置 | 是否产出「通过/录用」判定 | fail-closed？ |
|---|---|---|---|---|
| 1 | **准入层** `scripts/gates/acceptance.py::evaluate_acceptance` | `acceptance.py:72-145` | **是**（BLOCKER，`exit≠0`） | 阈值 fail-closed ✓；**provenance/签名/status 不校验 ✗**（见 A2） |
| 2 | CLI 包装 `gate_master_audit --acceptance` | `gate_master_audit.py:247-249` | 委托 #1 | 同上 |
| 3 | `reporting/registry.py::record_run` | `registry.py:155-258` | **否**——**无条件接受任何 `FINISHED` 产物**（不看 `metrics`、不看门禁结果） | **不适用**（记录器非门禁） |
| 4 | `gate_consistency._resolve_truth_artifact` | `gate_consistency.py:149-170` | **是**（选定「权威产物」，G-DOC-1 以它为真值） | 无——**优先取 `round_trips>0` 的 `FINISHED` 产物，不看其 `gate_statuses`** ⇒ 43.08% 回撤的产物即为「权威」 |
| 5 | `MaxDrawdownCeilingGate.evaluate` | `gate_consistency.py:196-254` | 推送/CI 期判定 | ✓（0 成交/缺字段 ⇒ INCONCLUSIVE） |
| 6 | `ReproducibilityGate.evaluate` | `gate_repro.py:86-174` | 复现一致性判定 | ✓（FAIL 可拦；legacy ⇒ INCONCLUSIVE，**但 CI 归 WARN**） |
| 7 | `context_builder.build_repo_context` | `context_builder.py:54-138` | **否**（选最新**已签名**产物喂给门禁） | 无判定；但**只认 `anti_tamper_signature`** ⇒ 新未签名产物被忽略 |
| 8 | `registry.list_runs` | `registry.py:260-275` | **否**（只读列表） | 不适用 |
| 9 | `audit_evidence_integrity.audit_recent_run_results` | `audit_evidence_integrity.py:111-142` | 产出 `PASS/FAIL`（仅「红利税>0」） | 独立审计，**非晋升门禁** |

**结论 A1**：**除 `--acceptance` 外不存在第二条自动「录用」路径**——但这不是「只有一条、很安全」，而是**整条 BLOCKER 层从未接线**：`grep acceptance` 显示它只被 `gate_master_audit` 与测试引用，`.github/workflows/*.yml`、`pre_push.py`、`pre_commit.py`、`registry` **均不调用**。因此「晋升」实际靠人工自觉跑一条命令。`record_run` 对 `FINISHED` 产物**无条件收编**，即「落盘 = 收编」，与「通过」无关。

### A2. 准入层边界条件（逐条实测）

探针：`probe_acceptance.py`（仓外临时产物，仓内 `experiments/runs` 未触碰）。原始输出：

```
用例                                     | ok    | 失败判据
mdd==0.35 (恰好)                         | True  | —
mdd==0.3500000001 (略超)                 | False | ['G-MDD-1']
win_rate==0.35 (恰好)                    | True  | —
annual_turnover==4.0 (恰好)              | True  | —
annual_turnover==4.0000001 (略超)        | False | ['S-1']
缺 round_trips                          | False | ['ACCEPT-RT']
缺 max_drawdown                         | False | ['G-MDD-1']
缺整个 metrics                            | False | ['G-MDD-1','ACCEPT-WIN','S-1','ACCEPT-RT']
无 metrics 键                            | False | ['G-MDD-1','ACCEPT-WIN','S-1','ACCEPT-RT']
legacy(无 schema_version)+健康指标          | True  | —        ← 逃逸
mdd 字符串非数字                             | False | ['G-MDD-1']
mdd 负数 -0.30                           | True  | —        ← 逃逸
mdd null                               | False | ['G-MDD-1']
mdd bool True                          | False | ['G-MDD-1']
round_trips 负数 -5                      | False | ['ACCEPT-RT']
status=FAILED 但指标健康                    | True  | —        ← 逃逸
未签名(无 anti_tamper_signature) 指标健康      | True  | —        ← 逃逸
有 error 字段但 FINISHED                   | True  | —        ← 逃逸
```

| 边界用例 | 期望 | 实测 | 结论 |
|---|---|---|---|
| `max_drawdown == 0.35` | 通过（`>0.35` 才 FAIL，文档口径 `acceptance.py:15`） | **True** | ✅ 与文档一致；阈值语义 = `<=` 通过 |
| `max_drawdown == 0.3500000001` | FAIL | False/G-MDD-1 | ✅ |
| `win_rate == 0.35` | 通过（`>=`，`acceptance.py:16`） | **True** | ✅ |
| `annual_turnover == 4.0` | 通过（`<=`，`acceptance.py:17`） | **True** | ✅ |
| 缺 `round_trips` | FAIL（`acceptance.py:128-130`） | False/ACCEPT-RT | ✅ |
| 缺 `max_drawdown` | FAIL | False/G-MDD-1 | ✅ |
| 缺整个 `metrics` | FAIL | False/4 项全挂 | ✅ |
| **legacy（无 `schema_version`）+ 健康指标** | 应至少要求 schema≥2 | **True（放行）** | ❌ **逃逸**：`evaluate_acceptance` 从不读 `schema_version` |
| `max_drawdown` 字符串 `"high"` | FAIL | False/G-MDD-1 | ✅（`_to_decimal`→None） |
| **`max_drawdown = -0.30`（负数）** | 应拒（回撤无负值） | **True（放行）** | ❌ **逃逸**：无下界 sanity，负回撤恒 `< 0.35` |
| `max_drawdown = null` | FAIL | False/G-MDD-1 | ✅ |
| `max_drawdown = true`(bool) | FAIL | False/G-MDD-1 | ✅（bool 显式排除） |
| `round_trips = -5` | FAIL | False/ACCEPT-RT | ✅（`>0` 判据） |
| **`status = FAILED` + 健康指标** | 应拒 | **True（放行）** | ❌ **逃逸**：不校验 `status` |
| **未签名产物 + 健康指标** | BLOCKER 层应要求签名 | **True（放行）** | ❌ **逃逸**：**不验 `anti_tamper_signature`** ⇒ 直接改 `metrics` 数字即可洗白一份超限产物 |
| `error` 非空但 `FINISHED` | 应拒 | True（放行） | ❌ 轻微 |

### A3. 回测 report-only 是否留下空洞

- 回测不 `raise` 属设计（`run_dividend_backtest.py:380-389` `gate_strict=False`），且**确实把门禁结果写进产物**：`gate_statuses`（`run_dividend_backtest.py:479/490/524/547` → `RunRecord.gate_statuses` → `registry.py:229/242` 落盘）。⇒ **新产物会带 `G-MDD-1: FAIL` 痕迹**（代码级判定；未实跑回测以避免写入 `experiments/runs`）。
- **空洞**：(a) 现有 **4 份产物全部是 legacy**，**无 `gate_statuses` 字段**（实测 keys 无该键）⇒ 对存量产物没有「未通过」痕迹；(b) 即便新产物带 FAIL 痕迹，`registry` 仍按 `FINISHED` 收编、`_resolve_truth_artifact` 仍可能把它选为「权威产物」⇒ 痕迹可见但**不影响收编**。

---

## 3. B 段：M2 复现性是否真的有效

探针：`probe_repro.py`。

### B1. `repro_fingerprint` 对「数据内容/代码内容」的敏感性 —— ✅ 有效

```
base            : ad8ad308e5d53e7b2e12838b2158f392
data_hash 变    : 3b4264c7a221079ff2c4399c9266bd33 → 变化
code_hash 变    : c0119f2f7a188975bfd0137805ee73c7 → 变化
params_hash 变  : 6ce5a430b7e074dd8aff5d6f046bbf1d → 变化
seed=None       : ba428efa3dd524cfa229d84985fe6e3b
幂等（同输入两次）: True
```
`repro_fingerprint` 签名**不含 `metrics`** ⇒ 同输入必得同键、异果即暴露。✅

### B2. `code_hash` 能否感知代码变更 —— ⚠️ 部分 / 被夸大

`_git_code_hash()`（`run_dividend_backtest.py:267-288`）= `git rev-parse --short HEAD` + （`git status --porcelain` 非空 ⟹ `"+dirty"`）。实测当前 = **`'b63ea6d+dirty'`**。

| 场景 | 是否变化 | 说明 |
|---|---|---|
| 已提交改动 | ✅ | HEAD 短哈希变 |
| 已提交 vs 未提交 | ✅ | `abc123` vs `abc123+dirty` |
| **脏树内任意两次**不同编辑 | ❌ | **两者都是 `HEAD+dirty`，内容不入哈希** |
| 无关未跟踪文件（当前 `1.ipynb`/`t206.txt`） | ⚠️ 误触发 | 使 `+dirty` 置位，与业务代码无关 |

⇒ 文档所称「**代码内容指纹**」名不副实：它只是**提交级 + 脏标志**，**不哈希代码内容**。

### B3. G-REPRO-1 能否 FAIL —— ✅ 能

```
同指纹异果 => FAIL | 检出 1 组产物违反'同参同输入必得同结果'…
同指纹同果 => PASS
真实仓库现状(默认扫描) => INCONCLUSIVE | LEGACY_UNVERIFIED: 检出 4 份 legacy 产物…
```

### B4. ⚠️ 新真实回测的 `repro_fingerprint` 是否恒 None —— **明确结论：不恒为 None**

| 证据 | 值 |
|---|---|
| `run_dividend_backtest._git_code_hash()` | `'b63ea6d+dirty'`（非 None） |
| `hash_path_manifest("data/dividend_stocks")` | `'d2da9bfe5f2a1575'`（非 None；该目录含 490 项） |
| 临时 registry 落盘 `repro_fingerprint` | `'a595071262d76c9bf8b03b21c1c1d45e'`（**非 None**） |
| `code_hash=None` 时 | `None`（守卫按预期短路） |

⇒ **团队-lead 最关心的假设「复现保护形同虚设（指纹恒 None）」不成立**：`run_dividend_backtest.py:536-539` 同时注入 `code_hash=_git_code_hash()` 与非空 `data_hash`，且 `universe_codes` 即便候选池侧车未落盘也会回退为数据目录 symbol 集合（`run_dividend_backtest.py:291-307`）⇒ 指纹**会生成**。

**但真正的复现性缺口在别处**（这才是需要修的点）：
1. **存量 4 份产物全部 legacy**（无 `repro_fingerprint`/`schema_version`）⇒ G-REPRO-1 对**当前仓库**恒 `INCONCLUSIVE(LEGACY_UNVERIFIED)`；且该门禁属 `RUN_EVIDENCE_GATE_IDS`（`context_builder.py:43-46`）⇒ CI 下 INCONCLUSIVE **只告警不阻断**（实测 CI exit=0）。
2. `[HEAD]`（**`f8f34b3` 已修**）`hash_path_manifest` 对**不存在/空目录**返回常量 `sha256("")[:16]` ⇒ 两个不同缺失路径坍缩为**同一** `data_hash` ⇒ 能骗过 `registry` 真值守卫（`registry.py:200` `if self._code_hash and self._data_hash:`）产出「合法外观」指纹。
3. `[HEAD]`（**`f8f34b3` 已修**）`calendar_hash or "na"` / `universe_hash or "na"` 兜底 ⇒ 两个不同的缺失分量共享同一指纹。
4. **`data_hash` 双口径（`[CUR]` 仍存）**：registry 用内容哈希 `d2da9bfe…`，而 G-1 出处 ctx 用「文件名+字节数」`_compute_data_hash` = `ee043c4b…`（实测两者不等）⇒ 同一 run 存在两个互不相同的 `data_hash`。
5. **`code_hash` 不哈希代码内容（`[CUR]` 仍存）**：见 B2。

---

## 4. C 段：`gate-doc-void` 是否成为新逃逸口

### C1. 标记格式校验 —— ✅ 契约严谨（实测）

```
合法                      => is_void_doc=True
缺 date                   => False
reason 为空 / 只有空格      => False
date 非法(2026/09/10)     => False
date 只有年月(2026-09)     => False
跨行折断(date 与 reason 分行) => False
```
实现 `gate_consistency.py:437-452`：正则含 `date=(\d{4}-\d{2}-\d{2})` 且 `m.group(2).strip()` 非空。**非法标记不生效** ✓。

### C2. 滥用风险 —— ❌ 显著

- **豁免粒度是「整份文档」而非「标记行」**：`evaluate` 里 `if is_void_doc(text): void_docs+=1; continue`（`gate_consistency.py:667-669`、`888-890`）⇒ 文档内**任一**合法标记即让**全文**免于 G-DOC-1 + G-REF-1。行内标记给人「只豁免这一行」的错觉，实则全文放行。
- **无上限、无强制登记、无守卫测试**：`grep _HISTORICAL_SNAPSHOT_DOCS`/`void_documents` 在 `tests/` 无任何引用 ⇒ 加标记是**纯本地动作**，不经过 `void_documents.md` 登记表（该登记表由人维护，机器**不校验**其完整性）。
- **`docs/audit/` 整目录豁免**：`_is_excluded_doc`（`gate_consistency.py:406-411`）把 `docs/audit/**` 全排除 ⇒ 任何「审计/说明」文档只要放进来即自动免检（**本报告本身即在此目录，不被 G-DOC-1/G-REF-1 校验**）。
- ⇒ **任意 `docs/`（非 audit）文档，加一个合法标记即可躲开数字/路径双门禁**；未来的合规材料同样可这么躲。

**建议控制措施**：① `void_docs` 设上限（如 ≤8）超限判 FAIL；② 反向校验：`docs/` 中被标 void 的文件必须出现在 `void_documents.md` 表格里，否则 FAIL（把登记表变成机读契约）；③ 把标记粒度改为「文件头**且** reason 命中白名单关键词（如"无产物"/"待重写"）」；④ 给 `docs/audit/` 的豁免加理由与计数。

### C3. 现有 5 份 void 文档逐份正当性判定

带标记的实际生效文档 = **5 份**（G-DOC-1 `void_docs: 5`）：

| 文档 | 标记理由 | 正当性判定 |
|---|---|---|
| `docs/momentum_backtest_summary.md` | 引用动量/T304/T305 回测结论，`experiments/runs/` 无对应产物 | **正当**——实测 `experiments/runs/` 仅 4 份 t312 产物，确无动量产物；且 `data/daily_bars/` 仅 1 标的（README:29） |
| `docs/t304_stress_report.md` | 同上 | **正当**（合成数据由 `tests/` 现场生成，从未落盘） |
| `docs/t305_technical_review.md` | 同上 | **正当**（其数字即 T304 转述） |
| `docs/phase35_recommendation.md` | 同上 | **正当**（决策依据同上，虽「决策建立在无产物数字上」本身是重大问题，但已如实登记） |
| `docs/task_completion_summary.md` | 同上 | **正当** |
| `docs/README.md` / `tasks.md` / `docs/audit/void_documents.md` | 仅**提及**该机制，未构成合法标记 | 不生效（`is_void_doc=False`） |

⇒ 5 份**豁免理由均正当**（所引产物确实不存在）；**风险在机制**（无上限/无机读登记）而非本批用法。

---

## 5. D 段：G-DOC-1 门禁数/基线校验能否被规避

探针：`probe_doc.py`（`expected_gate_count=29`）。正则 `_GATE_COUNT_RE = (\d+)\s*道` + 同行含 `_GATE_COUNT_KEYWORDS`（`gate_consistency.py:460-475`）。

### D1. 规避样例（错误值应命中却漏判 = 漏洞）

| 样例（`N`=真值 29，错误值用 36） | 判定 | 结论 |
|---|---|---|
| `本仓共 36 道门禁。` | 命中 | ✅ |
| `本仓共 **36** 道机读门禁。` | **漏判** | ❌ markdown 加粗打断 `\d+\s*道` |
| `本仓共 36 个门禁。` | **漏判** | ❌ 用「个」不用「道」 |
| `门禁数量 36。` | **漏判** | ❌ 无数词 |
| `实现 23 项机读门禁`（真实存在于 `tasks.md:63`） | **漏判** | ❌ 用「项」 |
| `总门禁达 24 项`（真实存在于 `tasks.md:65/129`） | **漏判** | ❌ 用「项」 |
| `共 129 道门禁。` | **漏判** | ❌ 真值 `29` 是子串 ⇒ 整行跳过（`gate_consistency.py:539` `if str(expected_gate_count) not in norm`） |
| `本仓共 36\n道门禁。` | **漏判** | ❌ 逐行处理，换行折断 |
| `29 道门禁。`（正确值） | 未判 | ✅（正确值不报） |
| `门禁 29 道。`（正确值） | 未判 | ✅ |

**根因**：`gate_consistency.py:539` 用 `str(expected) not in norm` 做「本行已含真值则跳过」，**子串包含**（`"29" in "129"`）导致带前缀的非法数字整行放过。**实测：`tasks.md` 里 `23 项机读门禁` / `24 项` 这类真正过期的门禁数全部漏判**。

### D2. `_HISTORICAL_SNAPSHOT_DOCS` 白名单

白名单 3 份（`gate_consistency.py:488-492`）：`GATE_PHASE1/2/3_COMPLETION_SUMMARY.md`，理由=「阶段历史快照，不随门禁演进同步」。**能否永久豁免？** —— **能**：`_is_historical_snapshot`（`:495-498`）纯后缀匹配，**无守卫测试、无审查约束**（`tests/` 无任何引用）⇒ 把文件名加进该 dict 即永久免检；且它是**路径后缀匹配**，同名前缀文件也被命中。

### D3. 放宽正则的误报样例 —— ❌ 存在

| 样例 | 判定 |
|---|---|
| `系统设计为 6 道防御关卡（投资流程）。` | **误报**（关键词「防御」+「6 道」） |
| `本系统自动执行 5 道工序。` | **误报**（关键词「自动」+「5 道」） |
| `六维体系含 6 道闸门。` | **误报** |
| `共有 12 道机读校验步骤。` | **误报** |

当前仓库未触发是巧合（现无此类行）；关键词 `("门禁","闸门","防御","机读","六维","自动")` 过宽，「**N 道工序/关卡**」类散文会被当门禁数漂移。

### D4. 基线声明的同类规避

| 样例（真值 790@HEAD） | 判定 |
|---|---|
| `当前基线 793 passed。` | 命中（正确 fail-closed） |
| `1 790 passed 基线。` | **漏判**（`"790" in "1790"` 子串跳过） |
| `当前基线为 7790 passed。` | **漏判** |

---

## 6. E 段：`constants.py` 守卫是否可靠

- **守卫本身可靠（无静默通过）**：`tests/test_gate_consistency.py:823-847`
  ```
  m = _re.search(r"(\d+)\s+tests?\s+collected", output)
  assert m, f"未能从 pytest --collect-only 解析收集数…"     # 解析不到 ⇒ 断言失败（非静默）
  assert TEST_BASELINE_PASSED == actual_collected            # 允许 ==，⛔ 不软化
  ```
  ⇒ 若常量被改成错误值，`assert TEST_BASELINE_PASSED == actual_collected` **会红**（读代码判定）；若子进程/解析失败，`assert m` **会红**。**不会静默通过** ✓。
- **守卫在并发推进中被验证**：审计期间另一写入者新增 6 用例并将常量同步改为 `796`，随后提交为 `f8f34b3`；实测收集数 `796` ⇒ 守卫绿。说明该守卫「**任何一次加用例都必须同改常量**」，是高频人肉同步点（脆弱但方向正确）。
- **引用单一事实源？** `pre_push.py:37-39` 已改为 `from scripts.gates.constants import TEST_BASELINE_PASSED` ✓。
- **⚠️ 仍有硬编码/过期基线数字，且不被门禁覆盖**：
  - `README.md:7/27/42`：**「当前测试基线：760 passed（2026-09-10 实测）」**——真值 `790`(b63ea6d)/`796`(f8f34b3) ⇒ **陈旧 30+，且 README.md 不在 G-DOC-1 扫描范围**（见 §7 逃逸口 E5）。而 `constants.py:5-6` 自称「全仓任何单测基线数只能引用本模块」——已被 README 违反。
  - `.github/workflows/ci.yml` Gate Check 5 步骤名「`>=758 passed`」且命令 `pytest tests/ -q` **无 passed 计数断言**（仅靠退出码）；`scheduled_audit.yml:57` 写「full **28** gates」（真值 29）。
  - `CLAUDE.md` 含大量历史 `passed` 数字（19/62/…/426），未被校验（根级文档）。

---

## 7. F 段：回归与诚实边界

### F1. 回归（连跑两遍）

```
$ py -3.11 -m pytest tests/ -p no:ddtrace -p no:ddtrace.pytest_bdd -q
RUN1 exit=0 → 796 passed, 1 warning in 29.78s
RUN2 exit=0 → 796 passed, 1 warning in 29.64s
$ ls runs/           # 无残留
ledger_reports  paper_trading
$ ls experiments/runs/   # 前后一致，未新增
（4 份 json + index.jsonl）
```
⇒ **现行版 `f8f34b3` 下 796/796 ×2、`runs/` 无残留**（`runs/gate_bypass_audit.jsonl` 未生成 = 未使用逃生阀）。起点版 `b63ea6d` 为 **790**。t312 系列有 **6 处 data-dependent `pytest.skip`**（`test_t312_dividend_backtest.py:42/63/78/89/99/115`），本次未 skip（数据在）。

### F2. 空壳/永真断言抽查

`grep "assert True|assert 1 == 1|or True"` → **0 命中**；无空函数体测试。新增测试（`test_gate_consistency.py` 965 行、`TestProvenanceNullSafety`）断言具体、含正反例。**未发现空壳** ✓。
反向锁定抽查：`test_gate_count_no_false_positive`（:816-821）只测了「有词无数字」「有数字无词」两形态，**未覆盖** §D 的「个/项/加粗/子串」漏判形态 ⇒ 测试对真实漏洞「看不见」。

### F3. 复核团队-lead 自验项

| 自述 | 复核 | 结论 |
|---|---|---|
| pre-push OK=True（阻断 0，G-MDD-1 WARN） | 读 `pre_push.py:117-136` + CI 实测 `[WARN] G-MDD-1` | ✅ 成立 |
| CI `--ci` exit=0 | 实测 `CI_EXIT=0`，`PASS:14 FAIL:1 INCONCLUSIVE:14` | ✅ |
| 准入 `--acceptance` exit=1 | 实测 exit=1（G-MDD-1/ACCEPT-WIN 命中） | ✅ |
| G-DOC-1/G-REF-1 PASS（SKIP 0） | 实测均 PASS，SKIP 0 | ✅（但 PASS 依赖 5 void + 3 白名单 + 54 skip-phrase） |
| 790 passed ×2 无残留 | **现行 `f8f34b3` = 796×2**；起点 `b63ea6d` = 790 | ⚠️ 数值随并发推进变化 |
| 镜像 `--verify-mirror` PASS | 实测 PASS（SHA-256 `c89d07b8…`） | ✅ |
| `TEST_BASELINE_PASSED=790 == 收集数 790` | `b63ea6d` 成立；`f8f34b3` 已 = **796**（收集数同步 796） | ✅（现行版一致） |

---

## 8. 与工程师 / 产品经理自述不符之处（逐条，附证据）

1. **「790 == 真实收集数」**：对起点版 `b63ea6d` 成立；审计中另一写入者新增 6 用例并把常量改为 **796**（提交 `f8f34b3`，收集数 796）⇒ 「7 提交冻结版」与现行版口径不同，团队-lead 自述需按现行版更新。**证据**：`git log`、`constants.py:21`、`--collect-only`。
2. **「单一事实源 = `constants.TEST_BASELINE_PASSED`，全仓只此一处」**：不实。`README.md:7/27/42` 三处仍写 **760 passed**（陈旧），且 README 不在 G-DOC-1 扫描范围 ⇒ **无门禁覆盖**。**证据**：§E、§7 扫描文件数=44 且不含 README。
3. **「代码内容指纹 `code_hash`」**：夸大。实为 `git 短哈希 + "+dirty"` 标志，**不哈希代码内容**；脏树内任意两次不同改动**不可区分**。**证据**：`run_dividend_backtest.py:267-288`。
4. **「G-DOC-1/G-REF-1 PASS（SKIP 0）⇒ 文档口径已统一」**：PASS 成立但**结论被豁免放大**——`void_docs:5` + `historical_snapshot_docs:3` + `③ 非实测值排除 54 处`；其中「**23 处**所引产物不存在」被 5 份 void 文档整体豁免（`void_documents.md` 自述 23 处）。**证据**：G-DOC-1 message 原文。
5. **「门禁数量动态校验，杜绝写死」**：机制在，但**实际漏判真实存量漂移**——`tasks.md:63/65/129` 的 `23 项机读门禁` / `总门禁达 24 项` 全部**未命中**（用「项」不用「道」）。**证据**：`probe_doc.py` 实测 `_declaration_violations(...) == []`。
6. **「`schema_version` 缺该字段的历史产物 ⛔ 不得当作检查通过」**（`registry.py:24`）：对 G-REPRO-1 成立（legacy⇒INCONCLUSIVE），但**准入层完全不读 `schema_version`** ⇒ legacy 健康产物被准入**放行**。**证据**：§A2 legacy 用例 `ok=True`。
7. **「新回测产物带签名」隐含预期**：`record_run` 落盘字段**不含 `anti_tamper_signature`**（`grep` 0 命中）；签名只加在回测喂给门禁的 **ctx 合成 `run_record`**（`run_dividend_backtest.py:232-234`，`run_id="pending-registry"`）上 ⇒ **真实落盘产物未签名**，且 `context_builder.py:83-84` 优先只认已签名产物 ⇒ 新未签名产物**不会被当作权威**。**证据**：§A1 #3/#7、落盘 keys 实测。

---

## 9. 诚实边界（未验证 / 只读所致）

1. **未实跑真实回测**（会写入 `experiments/runs/` 与 `experiments/universe/`，违反只读约束）⇒ A3「新产物含 `gate_statuses: G-MDD-1: FAIL`」与 B4「指纹非 None」均为**代码级判定 + 部件级实证**（临时 registry + 真实哈希函数取值），**非端到端回测输出**。
2. **未改代码验证常量守卫转红**（只读约束）⇒ §E「常量错则测试红」为**读代码判定**，非实测红。
3. **未验证** `experiments/universe/` 侧车落盘（当前目录不存在）；`_snapshot_universe_sidecar` 的 best-effort 失败路径未触发。
4. **未穷举全部 `docs/**` 内容**（44 份 + 根级未扫文档），仅验证机制与代表性样例；G-DOC-1 当前 0 违规的**真值状态**未逐行人工复核。
5. **起点版与现行版口径差异**：审计中另一写入者提交了 `f8f34b3`；本报告对 `provenance.py`/`registry.py`/`constants.py` 的结论按 `[HEAD]/[CUR]` 双标注，**其余文件两版一致**。WT 未提交态未被单独审计（其内容与 `f8f34b3` 相同）。
6. **未验证** `f8f34b3` 的 `TestProvenanceNullSafety` 6 个新用例之外的连带影响（仅跑过全量 796×2 全绿）。
7. 未覆盖 `finai/sources/`（按约束不触碰）。
