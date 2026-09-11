# 门禁残留假通过清零（GATE-R1 → R4）交付总结

> 交付人：齐活林（交付总监／主理人）｜团队：`software-finai-gate-residual`｜日期：2026-09-11
> 路径：BugFix 快捷路径（工程师修复 → QA 对抗验证 → 主理人统一提交）
> 结论：**QA 第二轮判 PASS**；遗留 5 项如实登记（1 项真实假通过待裁决）

---

## 0. TL;DR

治理层 M3 已清除 15 处「无证据即通过」兜底，但**同一病仍有残留**：本轮以「反例撞击」方式撞出并修复 **15 处假通过**，其中一处（`runner` 合成 PASS）会让既有加固在**真实回测路径上被整体绕开**。

---

## 1. 起点：交接清单被实测推翻

前任交接把两件事列为「剩余待办」，**实测显示均已在 M3 闭环**：

| 条目 | 交接描述 | 实测结论 |
|---|---|---|
| ⑪ | pre-push 用空 context 跑门禁，S/E/A 维不设防 | **已闭环**。`scripts/hooks/pre_push.py:92-96` 已有 `_build_pre_push_context()` 复用 `context_builder.build_repo_context`；实测输出「门禁取证来源: 产物 20260907-150402…（已签名）（ctx 键 21 个）」 |
| ⑩ | D-5/L-1/A-3/A-2/D-2/L-2 六类硬编码兜底 | **5/6 已闭环**。`gate_master_audit --ci` 实测 14 道 run-evidence 门禁全部 INCONCLUSIVE 且带「无证据 ≠ 通过」；D-2 的 `n<30` 分支亦已 INCONCLUSIVE |

⇒ **教训：交接清单本身也会过期；清单必须实测，不得照派**（照派只会得到「已修」式虚报）。

---

## 2. 撞出的真实残留（本轮修复）

### 2.1 L-2 等权短路恒 PASS（起点实例，主理人实测）
`scripts/gates/gate_l_liveness.py`：目标权重等权时**直接 return PASS，完全不检查 `actual_values`**。
反例（修复前全 PASS）：等权目标 + 实际 `90/5/5`；等权目标 + 实际**只建 1 只**；等权目标 + 含负值。
⇒ 策略生产配置实为等权 1/N ⇒ 该门禁**恒 PASS**（空转）。

### 2.2 runner 合成 PASS —— 使既有加固在真实路径失效（最重）
`scripts/gates/runner.py`：D-1/D-4 逐票循环无 FAIL 即**自造 `GateStatus.PASS`**。
实测：**runner 路径 D-1 = PASS 而本体 = INCONCLUSIVE；D-4 = PASS 而本体 = SKIP**。
而 `scripts/run_dividend_backtest.py:480` 的真实回测路径正是以 `tables=` 调用它 ⇒ 加固被绕开。

### 2.3 另 4 处漏网（QA 独立扫描所得）
| 门禁 | 反例 | 修复前 → 修复后 |
|---|---|---|
| G-STRESS-1 | 缺 `trading_days` 仍 PASS 且谎称「压测 ≥200 日」 | PASS → INCONCLUSIVE |
| G-REPRO-1 | 同指纹且 metrics 皆空 ⇒ 平凡一致 | PASS → INCONCLUSIVE |
| G-2 模式B | `is_checked=False` 却谎报验签通过 | PASS → SKIP |
| G-1 | `data_hash` 声明 64-hex，实仅校验 `len>=16` | PASS → FAIL（并修正 desc） |

### 2.4 其他同类加固
A-1 / A-2 / A-4、D-1 / D-4 / D-5、E-1 / E-3、S-2 / S-3 / S-5、G-REF-1 / G-DOC-1 共 15 处。

---

## 3. 判据设计上的关键取舍

**L-2 两轮演化**：
1. R1 采用「归一化总变差 TV > 0.10」。**QA 证伪了其注释**：「0.10 至少捕获 n≤10 整只漏建」——n=10 恰 1 只漏建 **TV 恰为 0.1000**，判据 `>` 为假 ⇒ 仍 PASS。
2. R3 **弃用 TV**，理由（工程师自证）：`max|s_i − 1/n|` 在「某票 k 倍超配」时 = `(k−1)/n`，**仍随 n 缩小**，与 TV 同病；只有**比值**真正尺度不变。
   ⇒ 改为两条与 n 解耦判据：① 计数：任一 `v_i == 0` ⇒ FAIL；② 比值：`r_i = s_i·n ∈ [0.5, 2.0]`。
   并新增有限性校验：目标含 NaN/±inf ⇒ INCONCLUSIVE；实际含 NaN/±inf ⇒ FAIL。

---

## 4. 验收证据（全部实测，主理人独立复跑）

```
全量回归:      978 passed（== constants.TEST_BASELINE_PASSED 978 == --collect-only 978）
门禁总审计:    --ci ⇒ [CI][PASS] 无阻断（PASS 13 / FAIL 1 / SKIP 1 / INCONCLUSIVE 14）
pre-push:      [ALL PASS]（推送期 13 道；PASS 11，WARN 1 = G-MDD-1 展示不阻断）
L-2 独立复现:  n=10 恰1只漏建 FAIL ｜ n=11 真漏建 FAIL ｜ n=50 5只漏建 FAIL
               目标全 NaN/全 inf ⇒ INCONCLUSIVE ｜ 实际含 NaN/inf ⇒ FAIL
               40/40/20 ⇒ PASS（原误杀解除）｜ 比值界 1.8-0.6 PASS / 2.1-0.45 FAIL
runner 复核:   GateStatus.PASS 仅剩优先级表 + 注释 2 处；聚合按 FAIL>INCONCLUSIVE>WARNING>SKIP>PASS 上抛
既有测试:      test_gate_integration.py 12 增 0 删（纯补 fixture，无断言改动）
```

---

## 5. 🔴 QA 对抗验证是本次质量的关键

| 轮次 | 结论 | 说明 |
|---|---|---|
| **R2** | **FAIL**（攻破实现方自述） | 15 处修复中 14 处本体攻不破（mutation 证实双向锁定为真）；但攻破 L-2 阈值边界逃逸 + NaN/inf 假通过，并**独立撞出 runner 合成 PASS 与 4 处漏网** |
| **R4** | **PASS** | R2 反例全封堵；runner 走真实入口验证已上抛；4 处漏网已封；26 新测试 mutation 必红 |

> **本项目铁律再次被验证**：工程师自述「已修 15 处 / IS_PASS: YES」，QA 撞出其中 1 处**仍有假通过**、并发现 **4 处工程师未发现的同类**。
> ⇒ **自述只可信到「本体已改」，不可信到「范围已覆盖」。**

---

## 6. 遗留清单（如实登记）

| 级别 | 项 | 性质 | 处置建议 |
|---|---|---|---|
| ~~P2~~ | ~~**A-3** 声明「绝对误差 ≤0.05」，实现为「物理区间 95~125 元」~~ | ~~真实假通过 + 契约↔实现漂移~~ | ✅ **已解决**（用户裁定：对齐阈值，更严）⇒ 见 §9。⚠️ 过程中先引入过一次误杀回归，亦见 §9 |
| ~~P3~~ | ~~L-2 比值带偏松（允许单票最高 2× 超配，max/min 极差可达 4×）~~ | | ✅ 已在 `threshold_desc` 明示「最大/最小仓位占比之比 ≤4×，单只最多超配 2 倍」 |
| ~~P3~~ | ~~L-2 端点浮点脆弱~~ | | ✅ 新增 `EQUAL_WEIGHT_RATIO_EPS=1e-9`，端点确定化（n=4/5/7/10/13 名义端点全 PASS、真越界全 FAIL） |
| ~~P3~~ | ~~L-2「非等权目标 + 实际全 0」消息写「完全等权均分」~~ | | ✅ 消息拆分；该情形改判 INCONCLUSIVE，经 QA 定量评估**净影响 = 0**（L-2 全仓 latent，情形生产不可达） |
| ~~P3~~ | ~~`test_uneven_target_with_nan_actual_not_pass` 弱锁定~~ | | ✅ 换用具判别力输入，mutation 已证实必红 |
| 冻结 | **E-3 真实路径恒 INCONCLUSIVE** | `backtest/types.py:94` 的 `Trade` 无 `limit_up/limit_down`，`runner.py:473-474` 以 `Decimal("0")` 兜底 ⇒ 由「假 PASS」变「永不可判」 | 补证据需改 **backtest 引擎**（属证据链工作）。注：`gate_strict` 默认 `False`，默认回测不受阻断，仅 strict 模式受影响 |
| 冻结 | **D-4 SKIP 逃逸** | `context_builder.ci_policy` 对 SKIP **既不阻断也不告警** | 涉及 SKIP 全局语义策略，待裁决 |

---

## 7. Git 处置

```
042994f docs(audit): 归档 GATE-R1/R3 两轮 QA 对抗验证报告
bee73f8 fix(gates): 消除门禁「无信息却 PASS」假通过（GATE-R1/R3）——含 runner 合成 PASS 上抛
a5dda7f chore(git): 忽略根目录临时 notebook（1.ipynb 不纳入版本管理）
cd7738c fix(gates): 同步单测基线常量 876→914（M6 新增 38 测试致单一事实源漂移）
```

- **已推送**（`a8928b2..042994f`），远端 `refs/heads/master` = `042994f` = 本地 HEAD（`git ls-remote` 权威确认）
- 修复前**远端 HEAD 是红的**：其常量为 876 而实测收集数 914 ⇒ 守卫测试 `test_baseline_constant_matches_collected_count` 必红。本次推送恢复绿基线。

---

## 8. 下一步

1. **待用户裁决**：D-4 SKIP 逃逸（`ci_policy` 对 SKIP 既不阻断也不告警）。
2. 门禁债务（本轮范围）已清零、CI 已转绿 → 可启动 **M6 归因实验**（设计文档 `docs/audit/m6_attribution_design.md` 已就绪未执行）。

---

## 10. 补记：GitHub Actions 红灯（Run #13~#17）的修复

> 与 §9 并列的第二个高价值事件：**门禁长期红会遮蔽后续步骤**，修好门禁后测试问题才浮出。

### 10.1 现象与定位
- 推送**是成功的**（远端 `532228b` = 本地 HEAD）；红的是 **GitHub Actions**。
- 失败点 **step 8「`gate_master_audit --ci`」exit 1**；Run #9–#12 success，**#13（a8928b2）首次 failure**。
  ⇒ `--ci` 参数在那批提交才引入（在 #12 上实测 `unrecognized arguments: --ci`），**#13 是历史上第一次真正执行新门禁策略的运行**。
- **根因**：`data/**` 被 gitignore ⇒ **CI 上完全没有数据**（本地 490 个文件）。
  **复现方法**（可复用）：`git clone` 一份到临时目录，克隆体天然无数据，精确等于 CI 文件集。
- 6 项阻断同一根因：D-1~D-4 INCONCLUSIVE、G-1 `data_hash` 失效、G-REF-1 报 3 处路径不存在。

### 10.2 处置（用户裁定：提交最小 fixture 让 CI 真检；不加豁免、不改判据、不改文档）
1. `scripts/build_ci_fixture_data.py` —— 确定性生成 CI 最小数据样本（真实数据**逐行原样拷贝**：30 只 × 130 交易日）
2. `tests/fixtures/ci_min_data/`（745 KB）入库
3. `context_builder.resolve_data_root()` —— 真实数据优先、无数据回退 fixture、皆无则报「数据缺失」
4. **`ci.yml` 新增「Materialize CI minimal data sample」步骤** —— ⚠ 这是 G-REF-1 转绿的**必要条件**（G-REF-1 直接查文件系统 `exists()`，不看 ctx）

### 10.3 ⭐ 连撞三次「修好一处暴露下一处」
| # | 暴露出的新问题 | 处置 |
|---|---|---|
| 1 | 物化后 Gate Check 4 转绿 ⇒ **pytest 步骤首次被执行**，暴露 2 条失败（要 ≥300 只 / 2015-2016 分区，而小样只有 30 只 × 2024） | ci.yml 复制 `fixture_manifest.json` 作**显式小样标记**；`_skip_if_no_data()` 增加小样守卫 ⇒ skip 并打印「非全量，⛔ 这不是通过」 |
| 2 | 上述注释写出「数据区/子目录/清单名」**字面量** ⇒ ⭐ **G-REF-1 也会扫 `.yml`（不只 md）**，该路径仅 CI 物化后存在 ⇒ **本地 pre-push 被判幽灵引用并阻断推送** | 改用变量拼接（`DATA_AREA="data"` + `$DATA_AREA/...`），注释记录该坑 |
| 3 | Windows CRLF 用例 `test_on_disk_artifact_matches_build_result` | CRLF 敏感性是**刻意设计**（docstring 明写「连 CRLF 改写也能检出」）；仓库内产物提交为 LF ⇒ ubuntu 不触发，实测确未触发 |

### 10.4 结果
**Run #18 = success**（物化 / Gate Check 1~5 / **pytest** 全绿），#13~#17 全 failure 的历史终结。

### 10.5 两条必须记住的边界与教训
- **唯一非真实成分**：合成停牌日 9 天。真实数据 **2015–2024 全池 `tradestatus` 恒为 `'1'`、0 个停牌日可抽样**；不含则 D-4 判 SKIP（CI 上等于没判）。已逐日登记并在三处可见。
- ⛔ **CI 上的数据门禁校验的是抽样小样，不等于校验全量真实数据质量**；全量校验仍须在本地 `data/` 上跑同一条命令。
- ⭐ **「门禁通过」≠「测试跑过」**：门禁长期红会**遮蔽**后续步骤（pytest 被 skip）。排查 CI 红时要想到——**后面可能还有几步从未被执行过**。

---

## 9. 补记：A-3 基准集事故（R5 → R6 → R7）

> 本节能见度最高，因为它记录了**主理人自己的一次错误**，以及流程如何在 QA 层把它接住。

### 9.1 经过
1. **用户裁定**：A-3 按「对齐阈值（更严）」处理，让**声明**（绝对误差 ≤0.05 元）成为权威。
2. **主理人派工（GATE-R5）时，把 A-3 源码注释 `:244-247` 当成了证据源** —— 那段注释自登记三个合法口径：`114.20 / 102.00 / 103.22`。据此定基准集。
3. **GATE-R5 交付**：`GOLDEN_FEE_BASIS = (114.20, 103.22, 102.00)`，判据 `min|val−g| ≤ 0.05`。
4. **QA（GATE-R6）判 FAIL**：基准集**排除了引擎权威黄金值 112.82**。

### 9.2 事实与根因
| 值 | 真实性质 | 铁证 |
|---|---|---|
| **112.82** | 引擎**权威逐项口径**黄金值 | `tests/test_t203_fees.py:166`：`buy 31.41 + sell 81.41 == 112.82`（主理人另以 `compute_fees` 独立复算一致） |
| **102.00** | 行业「含规费全佣」口径 | 同测试注释；`docs/t207:32`、`docs/t305:33` |
| 114.20 | **注释算错**：用经手费 4.10 | 引擎 `backtest/fees.py:234` 沪深费率 `0.0000341` ⇒ **3.41**（旧率 `0.0000487`⇒4.87）。差 `0.69/边 ×2 = 1.38 = 114.20 − 112.82`，**恰好对上** |
| 103.22 | **全仓无出处** | grep 仅命中 A-3 自身 |

⇒ **R5 引入了 PASS→FAIL 回归**：`A-3(112.82)` 由旧实现的 PASS 变为 FAIL，**会误杀引擎自己的黄金算例**。因 A-3 当前 latent（`runner.py:548` 不评估）未在生产暴露，但 `runner.py:549` 承诺的 golden fixture 一旦接线即会触发。

### 9.3 ⭐ 主理人的错误与教训
**我把「源码注释」当成了证据源。** 本项目铁律明写「不采信自述、结论须能由一条命令复现」，而我用一段**未经验证的过期注释**去定基准集——更讽刺的是，注释里的 `4.10` 本身就是「硬编码会漂移」的又一个活例。
⇒ **注释／文档中的硬编码数字，与任何自述同级，必须独立核算后再采信。** 本例只要拿 `compute_fees` 跑一遍即可发现。

同时也要看到：我在派工前做了影响面分析，但只查了「谁引用 A-3」，**没有核算 A-3 的基准本身**。QA 补上了这一层。⇒ **影响面分析必须包含「值本身是否正确」，不能只查调用方。**

### 9.4 修复（GATE-R7）
- `GOLDEN_FEE_BASIS = (112.82, 102.00)`，112.82 为 primary；新增 `test_engine_authoritative_golden_112_82_passes` 锁定
- **按引擎真实费率改正 A-3 docstring**（消除「注释本身是过期硬编码」这一同病），并同步 `threshold_desc` 与口径标签
- 补兜底：新增 `_parse_finite_decimal`，非法/非有限输入**一律不崩溃**且**无一 PASS**（被检对象非法 ⇒ FAIL；外部参照非法 ⇒ INCONCLUSIVE）

### 9.5 验收
```
独立核算黄金值：buy=31.41 / sell=81.41 / roundtrip=112.82（compute_fees 实算）
A-3: 112.82 PASS ｜ 102.00 PASS ｜ 114.20 FAIL ｜ 103.22 FAIL ｜ 95.5/100.0/125.0 FAIL ｜ 112.80 PASS ｜ 112.88 FAIL
兜底: rt='abc'/True/[..]/NaN ⇒ FAIL（no crash）；rt=None、exp=NaN/'abc' ⇒ INCONCLUSIVE（no crash）
全量：1002 passed ｜ collect 1002 == 常量 1002 ｜ --ci [CI][PASS] ｜ pre_push [ALL PASS]
```
