# FinAI2.0 全仓一致性审计报告（Architect 独立复核）

> 审计人：高见远（软件架构师）　审计日期：2026-09-10
> 审计范围：`D:\Projects\FinAI2.0`（代码仓）+ `D:\Projects\research-finai`（计划/验收权威仓，**存在，已读取**）
> 审计性质：**只读审计，未改动任何生产代码/数据**。审计过程中测试运行产生的临时产物已清理（`git status` 仅剩审计前即存在的未跟踪文件 `1.ipynb`）。
> 复现环境：`py -3.11`（Python 3.11.5，`D:\python3.11.5`）。全仓 pytest 收集数实测 **725**。

---

## 1. 结论摘要

这个项目的**工程引擎是真的，治理与结论是假的**。账本、撮合、费率、红利税 FIFO、数据口径（RAW/停牌/PIT）经得起查，725 个单测也基本是真断言；但**所有对外结论性数字都不可引用**——同一份 T312 回测在仓内存在三套互斥指标，其中两套（换手 92.51%/胜率 46.88%、CAGR 10.72%）在任何回测产物中都不存在，属**无出处的编造**；被吹成"24 道机读防伪门禁"的体系在 CI/Hook 真实调用路径下 **23/24 直接 SKIP、脚本自报"全绿"**；合规报备文档（要交券商的那份）载有 4 处错误指标 + 2 处与代码不符的方法论陈述；Phase 4 模拟盘"已启动 6 个月跟踪"实际只有 1 天、0 笔成交、NAV 平直的空跑记录。**表面上绿灯闪烁，实际是"绿灯机器"本身在发光。**

---

## 2. 矛盾清单总表

| # | 矛盾点 | 涉及文件:行号 | 性质 | 严重度 | 误导决策 |
|---|---|---|---|---|---|
| P0-1 | 同一 T312 回测两套收益：−3.20% vs 10.72%（后者全仓无出处） | `docs/t313_dividend_stress_report.md:110,249,251,273` ↔ `experiments/runs/20260907-150402-*.json` | **门禁失效**（+证据造假） | 致命 | ✅ 是 |
| P0-2 | 风险指标三套并存：换手 201.14%/92.51%、胜率 28.21%/46.88%/67.65%、MDD 43.08%/15.23% | `T312_FINAL_SUMMARY.md:113,114`、`tasks.md:57`、`compliance/strategy_description_template.md:131`、`t313:249-251` | **工程缺陷**（文档数字与产物脱钩） | 致命 | ✅ 是 |
| P0-3 | G4.5 门禁"假通过"：以 0 笔成交的合成压测（MDD=0.00%）判 MDD<35% 通过，真值 43.08% 从未被该门禁校验 | `t313:119-123,139` + `scripts/gates/*`（无 MDD 门禁） | **门禁失效** | 致命 | ✅ 是 |
| P0-4 | Phase 4 状态自相矛盾：已"正式准入/进行中" vs "暂停待策略 v2" | `delivery/PHASE4_ADMISSION_RESOLUTION.md:6,53`、`compliance/filing_checklist.md`、`runs/paper_trading/` ↔ `CLAUDE.md:93`、`docs/README.md:4`、`project_status_flowchart.md:144` | 文档卫生（+工程） | 高 | ✅ 是 |
| P0-5 | "Phase 4 模拟盘进行中"实为 1 天 0 成交空跑，`reconciliation_ok=true` 因无持仓而平凡成立 | `runs/paper_trading/daily_run_2026-09-07.json` | **门禁失效** | 高 | ✅ 是 |
| P1-6 | 测试基线 5 个版本（629/681/699/717/725），实测 725 | `README.md`、`scripts/hooks/pre_push.py:6,35`、`docs/README.md:102`、`delivery/PHASE4_ADMISSION_RESOLUTION.md:22` | 文档卫生 | 中 | 部分 |
| P1-7 | `accounting/` 空包（`__init__.py` 0 字节），README 宣称"会计层双账本" | `accounting/__init__.py` ↔ `README.md` | 文档卫生（+死代码） | 中 | 部分 |
| P1-8 | 合规文档称"数据层自动过滤 ST"，代码从未 drop ST | `compliance/strategy_description_template.md:35` ↔ `data/cleaner.py:305-330` | **工程缺陷**（合规误述） | 高 | ✅ 是 |
| P1-9 | `ops/` 宣称"飞书告警与心跳监控"，实为 4 个台账提醒模块；飞书仅 stub，且被引用的 `ops/feishu_alert.py` 不存在 | `README.md`、`ops/`、`data/collector.py:228`、`docs/t404_ledger_automation.md` | 文档卫生 | 中 | 部分 |
| P1-10 | `data/daily_bars/` 仅 1 文件，动量全周期回测脚本读该目录 → 必然"数据不足" | `data/daily_bars/`（1 文件）↔ `scripts/run_momentum_backtest_full.py:66` | **工程缺陷**（缺数据） | 中 | 部分 |
| P1-11 | 合规文档"自由流通市值 ≥5000 万" vs 代码"当日成交额 5000 万" | `strategy_description_template.md:32` ↔ `strategy/portfolio.py:63` | 文档卫生（合规误述） | 中 | 部分 |
| P2-12 | hook 文档串写 699、常量 725、docs/README 写 717 | `scripts/hooks/pre_push.py:6` vs `:35`；`docs/README.md:102` | 文档卫生 | 低 | 否 |
| P2-13 | 引用 `runs/index.jsonl`，实际为 `experiments/runs/index.jsonl` | `CLAUDE.md:22`、`docs/spec/.../tasks.md:38` | 文档卫生 | 低 | 否 |
| P2-14 | `DividendConfig.min_positions=5` 与全局契约下限 3 冲突（同一次 run 参数两值并存） | `strategy/candidates.py:203`、`strategy/portfolio.py:58` | 工程缺陷（口径） | 低 | 否 |
| P2-15 | R5 砍腿引用（非悬空） | `finai/*` | 非问题 | — | 否 |

**新增发现（主理人侦察未覆盖，含 1 条最致命）**：

| # | 新增矛盾/空白 | 证据 | 性质 | 严重度 |
|---|---|---|---|---|
| N1 | **门禁在真实调用路径下是空转**：`pre_push` 用空 context 跑总审计 → 24 门 SKIP 23 / PASS 1，脚本打印"全绿"；SKIP 被 `is_pass` 计为通过 | `py -3.11 -m scripts.gates.gate_master_audit` 输出；`scripts/gates/base.py:79` | **门禁失效** | 致命 |
| N2 | runner 后置门禁大量硬编码"默认通过"兜底，真实回测未传 context → S-2/S-3/S-4/S-5/G-1/G-2 恒过、G-4 根本不执行 | `scripts/gates/runner.py:437-520`、`run_dividend_backtest.py:284-289` | **门禁失效** | 致命 |
| N3 | **不存在任何 MDD/回撤上限门禁**（G4.5 核心判据无机器门禁） | `gate_master_audit.py:75-98` 24 门清单 | **门禁失效** | 高 |
| N4 | **不存在"文档数字 ↔ 回测产物"一致性门禁** | `scripts/audit_evidence_integrity.py`（只读 JSON，不读 md） | **门禁失效** | 致命 |
| N5 | 测试**反向锁定错误数字**：断言合规文档必须含 `92.51%` | `tests/test_t405_compliance_and_paper_e2e.py:66` | 工程缺陷 | 高 |
| N6 | 证据链断裂：文档引用 22 个不存在路径（含 T313 自己的压测数据 `data/stress_test/`） | §4/§6 详列 | 文档卫生 | 高 |
| N7 | 错误数字已进入**权威仓**（镜像 tasks.md 逐字相同） | `diff docs/spec/tasks.md = research-finai/specs/.../tasks.md` | 工程缺陷 | 高 |
| N8 | 合规文档与所引产物**本金口径不一致**：文档 10 万→净值 72,284.85，产物 `initial_nav=150000`/`final_nav=108421.14` | `strategy_description_template.md:126-128` ↔ run JSON | 工程缺陷 | 高 |
| N9 | `T312_FINAL_SUMMARY.md` 费用分项（佣金/印花/滑点）**全部与所引产物不符**，仅合计凑对 | `T312_FINAL_SUMMARY.md:121-126` ↔ run JSON `fees_total` | 工程缺陷（拼凑） | 高 |
| N10 | `data/dividend_stocks/**` 的 `turn`（换手率）列**全为 0** | 抽样 8 标的 parquet | 工程缺陷（数据） | 低 |

> 修正主理人 2 处口径：① `docs/` 下 md 数实测为 **44**，非 51；② P0-3 的机制不是"忽略 43.08%"，而是"用 0 成交合成区间得到 MDD=0.00% 判通过"——结论（假通过）成立，机制需更正。

---

## 3. P0 逐条深挖

### P0-1　同一 T312 回测两套收益，10.72% 全仓无出处

**现象**：`t313_dividend_stress_report.md` 四处断言"T312 长周期回测 CAGR 10.72% / MDD 15.23% / 胜率 67.65%"；而 `tasks.md:57`、`T312_FINAL_SUMMARY.md:110`、`diagnosis/t312_full_period_diagnosis.md:13` 一致为 **总收益 −27.72% / CAGR −3.20%**。

**复核命令与输出**：
```bash
# 1) 全仓/双仓搜索 10.72 —— 只存在于 t313 一篇文档
$ grep -rn "10\.72\|67\.65" . /d/Projects/research-finai --include=*.md
docs/t313_dividend_stress_report.md:110: ... 红利策略有大量成交，CAGR 10.72%，胜率 67.65%
docs/t313_dividend_stress_report.md:249:   - CAGR 10.72%（vs 动量 -2.15%）
docs/t313_dividend_stress_report.md:251:   - 胜率 67.65%（vs 动量 0%）
docs/t313_dividend_stress_report.md:273: 2. **长周期验证**：T312 回测（2015-2024）CAGR 10.72% ...
# （另 02 号评估框架文档中的 10%–30% 为通用目标区间，与本结论无关）

# 2) 遍历全部 4 份回测产物 —— 无一为正收益，更无 10.72%
$ for f in experiments/runs/*.json; do python -c "import json,sys;d=json.load(open(sys.argv[1]));m=d['metrics'];print(d['run_id'][:8],m['total_return'],m['cagr'],m['max_drawdown'],m['win_rate'],m['annual_turnover'])"; done
20260903 -0.0           -0.0         -0.0          None     -0.0      ← 0 笔成交（空跑）
20260903 -0.14235       -0.015258    0.25370       0.450382 1.375580
20260906 -0.14235       -0.015258    0.25370       0.450382 1.375580
20260907 -0.2771924     -0.031979    0.4307653     0.282051 2.011435  ← 权威口径
```

**关键反证（时间线不可能）**：`t313` 报告日期为 **2026-09-02**（`t313:5`），而其引用的 T312 首次真实回测产物时间为 **2026-09-07 15:04**（`experiments/runs/20260907-150402-*.json` 的 `timestamp`）。`research-finai/specs/.../tasks.md:105`（TK-20，2026-09-02）明确记载当时 T312"**待执行**…预期 CAGR 5-8%"。即：**T313 在 T312 尚未执行时就"引用"了它的 10.72% 实盘结果**——这是编造，不是误差。

**真实影响**：`t313` 是 G4.5 门禁唯一判据文档、是 `filing_checklist.md:31` 指定的报备附件、是 `PHASE4_ADMISSION_RESOLUTION.md:19` 准入的"G4/G4.5 策略实证 PASS"依据。**Phase 4 的整个准入是建立在编造数字之上的。**

**收敛建议**：
1. 立即将 `t313` 全篇 10.72%/15.23%/67.65% 标注为**已作废/无出处**，或整篇重写为"该文所有 T312 引用数据无效"。
2. `filing_checklist.md:31` 的报备附件必须撤换为 `experiments/runs/20260907-150402-*.json` 的机读产物（该 JSON 有 `anti_tamper_signature`，是唯一可信源）。
3. 该条应在 `RECONCILED/REVALIDATE` 级文档中登记为 **P0 数据造假事件**，而非"文档过期"。

---

### P0-2　风险指标三套并存，且其中一套费用分项系拼凑

**现象**：仓内对"同一次 2015–2024 回测"给出三套互斥指标：

| 口径 | 换手 | 胜率 | MDD | 往返 | 出处 |
|---|---|---|---|---|---|
| **A（权威，产物直读）** | **201.14%** | **28.21%** | **43.08%** | **78** | `experiments/runs/20260907-150402-*.json` |
| B | 92.51% | 46.88% | 43.08% | 102/96 | `T312_FINAL_SUMMARY.md:113,114,115`、`tasks.md:57`、`compliance/strategy_description_template.md:131` |
| C | — | 67.65% | 15.23% | — | `t313:249-251` |

**复核命令与输出**：
```bash
$ cat experiments/runs/20260907-150402-t312-dividend-v1-noseed.json
  "metrics": { "annual_turnover":"2.011435", "cagr":"-0.031979",
    "max_drawdown":"0.43076537...", "win_rate":"0.282051", "round_trips":78,
    "fees_sum":"9738.26", "initial_nav":"150000", "final_nav":"108421.14",
    "fees_total": {"COMMISSION":"1556.18","DIVIDEND_TAX":"5043.75",
      "HANDLING_FEE":"270.97","MANAGEMENT_FEE":"114.56","SLIPPAGE":"0",
      "STAMP_TAX":"2651.94","TRANSFER_FEE":"100.86"} }
```
B/C 两套在**全部 4 份产物中均不存在**。更严重的是 B 套的费用分项与所引产物逐项不符：

| 费用科目 | `T312_FINAL_SUMMARY.md:121-126` | run JSON | 差 |
|---|---|---|---|
| 佣金 | 1,363.34 | **1,556.18** | ✗ |
| 印花税 | 1,840.40 | **2,651.94** | ✗ |
| 滑点 | 1,490.77 | **0** | ✗ |
| 过户/经手/证管 | 0.00 | **270.97 / 100.86** | ✗ |
| **合计** | 9,738.26 | 9,738.26 | ✓ |

即：**分项全错、合计凑对**——典型的"先看总分再编分项"的粉饰手法。

**真实影响**：`92.51%`/`46.88%` 已写入**合规报备策略说明书**并被测试**反向锁定**（见 N5）；`tasks.md:57` 的错误数字已镜像进**权威仓**（见 N7）。任何下游引用都会复现错误。

**收敛建议**：所有文档数字改为**从 `experiments/runs/*.json` 引用**（该产物带签名，可机读）；新增"文档数字==产物"机读门禁（见 §6 空白区）。

---

### P0-3　G4.5 门禁"假通过"：0 成交压测判 MDD<35%

**现象**：`t313:119-123` 以"必须项① MDD<35%"判 **✅ 通过**，两区间实测 MDD 均为 **0.00%**。README/CLAUDE/合规文档据此宣布"G4.5 门禁通过 → Phase 4 解锁"。

**复核命令与输出（读 t313 原文）**：
```
t313:50-68  场景1（2015股灾40天）：总收益 0.00% / MDD 0.00% / "N/A（0 笔成交）"
t313:66     "技术原因：40 天测试区间 < 200 天 warmup_bars → 策略全程冷启动"
t313:81     "60 天测试区间仍未达到 MA200 交易条件 → 全程空仓"
t313:105    "过度保守：40/60 天测试区间 < 200 天冷启动期 → 全程未交易"
```
即：**该"压力测试"里策略一笔都没成交**，MDD 必然为 0，判据①②③全部平凡成立。`t313:110` 自己也承认"本次测试中红利策略 0 笔成交是 warmup_bars=200 的防御性设计导致"。这**不是压力测试**，是**空仓测试**。

**真实影响**：真正需要拦的 43.08% MDD（超 35% 阈值）从未被 G4.5 校验；后续又叠加了编造的 15.23%（P0-1）。**门禁在最关键的一项上失效，且失效方式是"测试用例退化为恒真"。**

**收敛建议**：G4.5 的 MDD/胜率判据必须建立在 **≥200 日、有实际成交**的区间上；压测区间若触发冷启动应判 **INCONCLUSIVE（不确定）** 而非 PASS；并补 MDD 机读门禁（N3）。

---

### P0-4　Phase 4 状态自相矛盾

**现象**：同一时点，仓内对 Phase 4 给出两种状态。

**复核命令与输出**：
```bash
$ grep -rn "Phase 4" CLAUDE.md docs/README.md docs/project_status_flowchart.md docs/delivery/PHASE4_ADMISSION_RESOLUTION.md | grep -iE "暂停|进行中|准入"
CLAUDE.md:93                  : **Phase 4 模拟盘暂停**，待用户拍板 A/B
docs/README.md:4              : ... Phase 4 暂停待策略 v2 用户拍板
docs/README.md:77             : ## 五、 模拟盘常态化运行与合规报备（Phase 4 进行中）   ← 同文件自相矛盾
docs/project_status_flowchart.md:144: Phase 4 ⏸
PHASE4_ADMISSION_RESOLUTION.md:6,53 : 【准予正式准入进入 Phase 4 模拟盘运行期】
compliance/filing_checklist.md:37   : 时间线：2026-09-07 准入启动 → 2027-03-07 满 6 个月 → G5 终审
```
`docs/README.md` **单文件内 line 4 与 line 77 直接互斥**。

**真实影响**：外部（含券商报备）看到的是"已准入 + 6 个月跟踪中"；内部最新结论是"暂停"。报备时间线（2027-03-15 提交）建立在已暂停的运行之上。

**收敛建议**：以 2026-09-10 诊断结论为准，统一全部文档为"Phase 4 **暂停**"；`PHASE4_ADMISSION_RESOLUTION.md` 增加作废/修订批注（保留留痕，不删除）；`filing_checklist` 时间线改为"待策略 v2 重启后再计"。

---

### P0-5　"模拟盘进行中"实为 1 天 0 成交空跑

**复核命令与输出**：
```bash
$ cat runs/paper_trading/daily_run_2026-09-07.json
  "metrics": {"nav":"100000","cash":"100000","positions_count":0,
              "orders_submitted":0,"orders_filled":0,
              "reconciliation_ok":true,"success":true}
$ cat runs/paper_trading/state.json
  {"last_trading_date":"2026-09-07","cash":"100000","positions":{},"nav":"100000"}
```
**唯一一天**（2026-09-07）的模拟盘运行：0 持仓、0 委托、NAV 恒等于初始 10 万。`reconciliation_ok=true` 只因"没有持仓就没有差异"。所谓"6 个月常态化运行（T406）"目前**尚无任何有效交易日**。

**收敛建议**：`filing_checklist.md` 第 5 项（模拟盘运行报告）与 `PHASE4_ADMISSION_RESOLUTION.md` 应显式标注"截至审计日累计有效交易日 = 1、成交 = 0，不构成任何实证"。

---

## 4. P1 逐条

### P1-6　测试基线 5 个版本
```bash
$ py -3.11 -m pytest tests/ --collect-only -q | tail -1
725 tests collected in 1.38s
$ grep -rn "629\|681\|699\|717\|725" README.md scripts/hooks/pre_push.py docs/README.md | grep -i "基线\|passed"
README.md                          : 当前测试基线：629 passed
scripts/hooks/pre_push.py:6        : 通过数不得低于基线 (当前基线 699 passed)   ← 文档串
scripts/hooks/pre_push.py:35       : MIN_TEST_BASELINE = 725                    ← 常量（正确）
docs/README.md:102                 : pre-push（717 单测基线硬拦截）
```
实测 725 → **README 629、pre_push 文档串 699、docs/README 102 的 717 均已过期**；`pre_push` 常量 725 正确。
**附加风险**：`pre_push` 基线=收集数 725，而 `tests/test_t312_dividend_backtest.py` 含 6 处 `pytest.skip`（数据缺失即跳过）。一旦有 skip，`passed < 725` → **hook 会拦下自家推送**。基线应写成"collected − allowed_skip"或改为断言"0 failed"。
**性质**：文档卫生（+ hook 设计缺陷，P2）。

### P1-7　`accounting/` 空包
```bash
$ wc -c accounting/__init__.py    → 0 accounting/__init__.py
$ find accounting -name "*.py"    → accounting/__init__.py   （仅此一个，0 字节）
```
README 宣称"`accounting/` 会计层：双账本流水、日终资产守恒对账"。真实双账本在 `backtest/ledger.py`（`Journal`/tx_hash 幂等）。**空包冒充功能模块**。
**性质**：文档卫生（应为死代码/占位，README 需更正或删除该包）。

### P1-8　合规文档称"数据层自动过滤 ST"，代码从未过滤
```bash
$ sed -n '317p' data/cleaner.py
    st = out["isST"].map(_is_st)          # 仅用于推导 st_pct=5% 涨跌停档
$ grep -rn "drop\|剔除" data/*.py | grep -i "st"   → （无 ST 剔除逻辑）
```
`isST` 仅参与涨跌停幅度判定（`cleaner.py:317-320`），**全仓无任何按 isST 删除行的代码**。而 `compliance/strategy_description_template.md:35` 向监管声明"非 ST / 退市风险股（**数据层自动过滤**）"。这是**向券商报备材料中的方法论虚假陈述**，法律性质高于普通文档错误。
**性质**：工程缺陷（合规误述）。**收敛建议**：要么在数据层真实实现 ST 剔除并留痕，要么修改合规文档为"ST 股不剔除，仅按 ±5% 档处理涨跌停"。

### P1-9　`ops/` 描述失实 + 引用幽灵模块
```bash
$ ls ops/   → check_reminder.py  expiry_reminder.py  ledger_registry.py  update_ledger.py  __init__.py
$ sed -n '228,235p' data/collector.py
  def default_alert_stub(...):  """熔断告警的**预留桥**（飞书 hermes MCP）。⛔ 留 stub 不真发。"""
$ for p in ops/feishu_alert.py; do [ -e $p ] && echo EXISTS || echo MISSING; done  → MISSING
```
README 称 `ops/` = "飞书告警与心跳监控"；实际只有 4 个台账提醒模块，飞书仅 `collector.py:228` 的 stub（只 `logger.warning`），且两份文档引用的 `ops/feishu_alert.py` **不存在**。
（注：`scripts/run_daily_tasks.py:48` 确有可用的飞书 webhook 发送实现，但它在 `scripts/` 而非 `ops/`，且依赖 `FEISHU_WEBHOOK_URL` 环境变量——README 的模块归属描述仍是错的。）
**性质**：文档卫生。

### P1-10　`data/daily_bars/` 仅 1 文件 → 动量全周期回测无数据
```bash
$ find data/daily_bars -type f   → data/daily_bars/sh.600000/2024.parquet   （仅 1 个）
$ grep -n "daily_bars" scripts/run_momentum_backtest_full.py
66:    data_root = _repo_root / "data" / "daily_bars"
$ head -12 docs/momentum_backtest_summary.md
  "Parquet 数据目录 (data/daily_bars/) 仅包含 1 只股票（sh.600000）的 2024 年数据"
```
动量全周期回测读 `data/daily_bars` → 只有 1 只票 1 年 → 必然"数据不足"。**值得肯定**：`momentum_backtest_summary.md` 如实披露了这一点，并未伪造结果（对比 t313 的做法，形成鲜明反差）。红利数据（`data/dividend_stocks/`，487 标的）才是唯一能跑真实十年回测的数据。
**性质**：工程缺陷（数据缺失）。

### P1-11　流动性门槛口径不符
```bash
$ sed -n '63p' strategy/portfolio.py
    min_daily_amount: Decimal = Decimal("50000000")   # 流动性下限（当日成交额 5000 万）
```
合规文档 `strategy_description_template.md:32` 写"**自由流通市值** ≥5000 万元（流动性门槛）"，代码实为"**当日成交额** ≥5000 万"。两者经济含义不同（前者是规模，后者是流动性）。
**性质**：文档卫生（合规误述）。

---

## 5. P2 汇总表

| # | 问题 | 证据 | 性质 |
|---|---|---|---|
| 12 | hook 文档串 699 / 常量 725 / docs 102 写 717 | `pre_push.py:6` vs `:35`；`docs/README.md:102` | 文档卫生 |
| 13 | 引用 `runs/index.jsonl`（实际 `experiments/runs/index.jsonl`） | `CLAUDE.md:22`、`tasks.md:38`；实测 `experiments/runs/index.jsonl` 存在、`runs/index.jsonl` 不存在 | 文档卫生 |
| 14 | `DividendConfig.min_positions=5` 与全局下限 3 冲突 | `candidates.py:203` vs `portfolio.py:58`；run 参数里 `min_positions:5` 与 `portfolio.min_positions:3` 并存 | 工程缺陷（口径） |
| 15 | `finai/tdx_minute5`、`finai.data_catalog`（R5 砍腿） | 未见悬空调用 | 非问题 |
| 16 | `data/dividend_stocks/**` 的 `turn` 列全 0 | 抽样 8 标的 parquet，`(turn==0).all()==True` | 工程缺陷（数据，低） |
| 17 | 合规文档本金口径与产物不符：文档"10 万→72,284.85"，产物 `initial_nav=150000→final_nav=108421.14` | `strategy_description_template.md:126-128` ↔ run JSON | 工程缺陷 |
| 18 | 合规文档"总交易摩擦 9,738.92" vs 产物 9,738.26 | `strategy_description_template.md:131` ↔ run JSON `fees_sum` | 文档卫生 |
| 19 | `data/collect_full{,2,3,_retry,_v2}.log` 5 份采集日志并存（历史失败留痕，未归档） | `ls data/*.log` | 文档卫生（清理） |
| 20 | 测试向**真实仓库**写文件并靠 unlink 清理，失败即残留 | `test_gate_p3_hardening.py:281-288`（审计中实测残留 `runs/temp_tampered_test.json`） | 工程缺陷（测试卫生） |

---

## 6. 门禁空白区分析（现有 24 道拦不住哪几类矛盾）

### 6.1 实锤一：门禁在真实调用路径下**空转**
```bash
$ py -3.11 -m scripts.gates.gate_master_audit
总览: 共 24 项门禁 | PASS: 1 | FAIL: 0 | SKIP: 23
[全绿] 所有门禁检验通过！符合散户客观物理约束与反欺诈防伪标准！
```
```python
# scripts/gates/base.py:79
@property
def is_pass(self): return self.status in (GateStatus.PASS, GateStatus.SKIP)   # SKIP 计为通过
# scripts/hooks/pre_push.py（实际执行）
master.audit(context={})                     # ← 空 context
blockers = [r for r in results if r.status==FAIL and r.severity in (BLOCKER,CRITICAL)]
```
`pre_push` 用**空 context** 调总审计 → 23 门因"无数据"返回 SKIP（如 `gate_s_scientific.py:39-49`：`if not context: return SKIP`）→ 非 FAIL → 视为通过。**结论：CI/Hook 上的"24 道机读防伪门禁"实际只校验了 1 项（母库 370 行守卫）。**

### 6.2 实锤二：回测主流程的门禁**默认恒过**
`scripts/run_dividend_backtest.py:284-289` 调 `run_post_run_gates(result, report, strategy_config)`，**未传** `ctx`。而 `runner.py` 的兜底逻辑：
```python
# runner.py:447-451  S-2 择时空仓生存（策略核心论据）
s2_gate.evaluate({"index_below_ma200_dates": ctx.get(..., []),
                  "daily_positions_ratio":   ctx.get(..., {})})   # 空→gate 返回 PASS"无数据通过"
# runner.py:437-445  S-1 换手
else: s1_gate.evaluate({"annualized_turnover": 0.0})             # 无数据→用 0.0→必过
# runner.py:459-466  S-3 滑点压测：stress_return = base*0.95     # 与 base 同号→永不"由正转负"→结构上不可失败
# runner.py:473-475  S-4 红利税：penalty=0/total_div=0→"无分红入账,通过"
# runner.py:495      S-5 code_evidence 默认硬编码 "backtest/metrics.py:L142"  # 证据链要求永真
# runner.py:501-511  G-1 data_hash = sha256(b"FinAI2.0-provenance")  # 常量假哈希
# runner.py:513-517  G-2 gate_signature 默认 "audit_report_sha256_pass"  # 硬编码通过
# runner.py:519-522  G-4 防篡改：仅当 ctx 含 run_record 才执行 → 真实回测路径根本不跑
```
**结论**：被宣传为"执行流前置/后置 Fail-Closed 阻断"的 18 门，在真实回测里 **S-2/S-3/S-4/S-5/G-1/G-2/G-4 全部空转或恒过**。

### 6.3 现有门禁拦不住的矛盾类型（空白区）

| 空白区 | 拦不住的具体矛盾 | 需补的门禁（可机读断言） |
|---|---|---|
| **无 MDD/回撤上限门禁** | P0-3（43.08% 超 35% 无人拦） | `gate_s_drawdown_ceiling`：读产物 `max_drawdown`，`>0.35` 判 FAIL（BLOCKER） |
| **无"文档数字↔产物"一致性门禁** | P0-1、P0-2、N9（三套指标/编造数字） | `gate_g_doc_number_consistency`：正则抽取 md 中带单位的关键指标，与 `experiments/runs/*.json` 比对，不一致即 FAIL |
| **无"引用产物存在性"门禁** | N6（22 个不存在路径） | `gate_g_evidence_path_exists`：抽取 md 中的仓内路径，`exists()` 断言（白名单允许生成物） |
| **无"跨文档矛盾"门禁** | P0-4（Phase 4 两种状态）、P1-6（5 版基线） | `gate_g_status_singleton`：关键状态词（Phase 4 状态、测试基线）全仓唯一取值 |
| **无"压测有效性"门禁** | P0-3（0 成交判通过） | 门禁应断言压测区间 `round_trips>0`，否则判 INCONCLUSIVE 而非 PASS |
| **无"合规文档↔代码"一致性门禁** | P1-8、P1-11（ST/流动性口径） | `gate_g_compliance_code_parity`：合规文档关键参数与 `PortfolioConfig`/`cleaner.py` 实测值比对 |
| **门禁 SKIP 语义** | N1、N2 | `SKIP` 不应计入 `is_pass`；在 CI 场景"应检而未检"应判 FAIL（fail-closed） |

**一句话**：现有 24 门守的是"引擎内部账目对不对"，**完全没守"对外结论对不对、文档有没有撒谎"**——而这恰恰是本次全部 P0 的所在。

---

## 7. 测试真实性抽查结论

**抽查样本**：`test_gates.py`(644行/52例)、`test_gate_integration.py`(18例)、`test_t405_compliance_and_paper_e2e.py`、`test_t312_dividend_backtest.py`，加全仓 grep。

**(1) 结论：绝大多数测试是真断言，不存在空壳/`pass` 占位。**
```bash
$ grep -rn "assert True\|assert 1 == 1\|pass\s*$\|NotImplementedError" tests/ | grep -v noqa
tests/test_t302_candidates.py:141:  pass          # 异常分支吞异常，正常
tests/test_t313_stress.py:312:      pass          # 同上
```
仅 2 处 `pass`，均为异常处理分支，非空壳。**"725 个测试都是假的"这一怀疑不成立。**

**(2) 但存在三类"测试绿却给假保证"的结构性缺陷：**

① **测试只验门禁逻辑、不验门禁接线**。`test_gates.py` 52 例全部**手工喂 context**（`test_d1_raw_price_jump_pass` 等）；`test_gate_integration.py` 的失败用例亦手工传 `context={"annualized_turnover":4.85}`、`context={"trades":invalid_trades}`（`:219,:175`）。**没有任何一个测试断言"真实回测路径 `run_dividend_backtest.py` 会拦下失败"** → 52+18 例全绿，而真实路径门禁空转（N1/N2），测试无法发现。

② **测试反向锁定错误数字**：
```python
# tests/test_t405_compliance_and_paper_e2e.py:63-66
assert "-3.20%" in text
assert "43.08%" in text
assert "92.51%" in text, "未载明年化换手率 92.51%"      # ← 强制合规文档保留错误换手率
```
`:64` 断言正确值（43.08%），`:66` 断言**错误值**（92.51%，真值 201.14%）。该测试现在成了错误数字的**防倒退锁**：改对文档，测试反而变红。

③ **E2E 测试跑的是空组合**。`test_cold_start_daily_pipeline` 用**空 `data/daily_bars`** 跑 `run_daily_pipeline`，断言 `nav=="100000"`、`reconciliation_ok is True`、签名长度 64。**它验证的是文件 I/O 与签名，不是交易逻辑**——与 `runs/paper_trading` 的"空跑"完全同构（P0-5）。

**(3) 我的实测结果与"725 passed / 0 failed"声明不符**：
```bash
$ py -3.11 -m pytest tests/ -q -p no:ddtrace -p no:ddtrace.pytest_bdd
1 failed, 724 passed
FAILED tests/test_gate_p3_hardening.py::test_run_pre_commit_catches_tampered_run
```
**诚实标注**：该失败经排查为**本审计沙箱的环境产物**（shim 拦截 `Path.unlink()`，报 `OSError: windows-sandbox-recycle-bin-unavailable`），断言部分（篡改检测）本身通过，**不是仓库缺陷**。但由此暴露一个**真实测试卫生缺陷**（P2-20）：该用例向**真实仓库** `runs/` 写入 `temp_tampered_test.json` 并依赖 `finally: unlink()` 清理，清理失败即残留脏文件（审计中已实测残留并清理）。

---

## 8. 架构层面判断

**总判：工程底座（引擎/账本/数据）可信；治理层与结论层不可信，必须重做。两者要分开对待，不能一并否定，也不能一并采信。**

### 8.1 可信（可复用、可留存）

| 模块 | 可信依据 |
|---|---|
| **回测引擎** `backtest/`（T201–T207） | 事件驱动九模块、七态状态机 fail-closed、先撮合后信号（`matching.py` 只用次日 `bar.open`）、撮合 8 规则、五必挂用例；319 单测覆盖契约。**结构性零前视**（`bias_audit_report.md` 佐证）。 |
| **双账本 & 费用** `backtest/ledger.py`、`fees.py` | append-only + tx_hash 幂等 + 可重算视图；六科目**分段费率带生效日**（印花税 1‰→0.5‰@2023-08-28 等），金额 Decimal 逐项到分。这是全项目最扎实的部分。 |
| **红利税** `backtest/dividend_tax.py` | 三档税率 + FIFO 持股期追溯，纯函数、黄金算例（200/125 元）入库；`audit_evidence_integrity.py` 实测 `broker.py` 为唯一生产调用方。 |
| **数据层** `data/`（dividend_stocks） | 实测抽样 8 标的：`adjust_mode=RAW`、**0 重复日期**、**0 非正价**、OHLC 一致性 100%、`dividend_yield` 无缺失、`market_cap` 量纲正确（PIT 无前视）。487 标的 × 10 年，**是全项目唯一能支撑真实回测的数据资产**。 |
| **Registry** `reporting/registry.py` | 原子写 + 幂等拒重 + `params_hash` canonical + SHA-256 `anti_tamper_signature`。**`experiments/runs/*.json` 是本项目唯一的"事实源"，其余一律不可采信。** |

### 8.2 不可信（必须重做/推翻）

| 模块 | 判决 | 依据 |
|---|---|---|
| **六维门禁体系** `scripts/gates/` | **重做** | 真实路径 23/24 SKIP、S/G 维默认恒过、G-4 不执行、无 MDD/文档一致性门禁。当前状态下它提供的是**虚假安全感**，比没有门禁更危险。 |
| **全部叙事性文档** `docs/**` + `CLAUDE.md` | **重做** | 三套互斥指标、编造 10.72%、拼凑费用分项、22 处幽灵引用、状态自相矛盾。**结论只能从 `experiments/runs/*.json` 重算。** |
| **合规报备包** `docs/compliance/` | **推翻重写** | 载有错误换手率/胜率、错误本金口径、错误流动性口径、虚假 ST 过滤声明。**这是要交券商的文件，错误性质最严重。** |
| **策略验证链** T313 / G4.5 / Phase4 准入 | **推翻** | T313 数据无出处、G4.5 退化为空仓测试、Phase 4 在未验证策略上"准入"。**Phase 4 的合法性不成立。** |
| **Phase 4 运行** `runs/paper_trading/` | **重置** | 1 天、0 成交、NAV 平直，"6 个月跟踪"无现实基础。 |

### 8.3 对"选 A 还是选 B"的直接含义

- 此决策**当前无可靠依据**：A（修仓位+降频）的依据来自 `diagnosis`（这部分数字与产物一致，**可用**：日均持仓 0.5–1.8、零持仓 54.7%、现金 65.4%、CAGR −3.20%）；B（ETF 增强）的依据 `data/etf_bars/sh.512890` **数据齐备**（2019–2024，6 年，见 `find data/etf_bars`），可立即回测验证。
- **但两条路都不能引用任何 md 结论**：A 的"满仓后收益"必须新跑并落 `experiments/runs/*.json`；B 必须新跑 512890 增强回测并落盘。
- 在门禁重做之前，**新产出的任何结论同样缺乏机器保护**——这是必须先补的短板。

---

## 附录 A　核心复现命令清单

```bash
cd /d/Projects/FinAI2.0
# 1 权威回测产物（唯一事实源）
cat experiments/runs/20260907-150402-t312-dividend-v1-noseed.json
# 2 遍历 4 份产物对比
for f in experiments/runs/*.json; do py -3.11 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d['run_id'],d['metrics']['total_return'],d['metrics']['cagr'])"; done
# 3 测试收集数
py -3.11 -m pytest tests/ --collect-only -q | tail -1
# 4 门禁真实行为
py -3.11 -m scripts.gates.gate_master_audit
# 5 防伪审计（只读 JSON，不读 md）
py -3.11 scripts/audit_evidence_integrity.py
# 6 空包/空目录
wc -c accounting/__init__.py ; find data/daily_bars -type f
# 7 幽灵引用
for p in data/stress_test ops/feishu_alert.py experiments/t312-dividend-v1 runs/index.jsonl scripts/run_momentum_backtest.py data/feed.py; do [ -e "$p" ] && echo "EXISTS $p" || echo "MISSING $p"; done
# 8 数据真值抽样
py -3.11 -c "import pandas as pd,glob;f=sorted(glob.glob('data/dividend_stocks/sh.600000/*.parquet'))[0];d=pd.read_parquet(f);print(d.shape,d['adjust_mode'].iloc[0],int(d['date'].duplicated().sum()))"
```

## 附录 B　本次审计未覆盖 / 存疑项（诚实边界）

1. **沙箱导致 1 例测试失败**（`test_run_pre_commit_catches_tampered_run`），已判定为环境产物，未能在纯净 Windows 环境复验"725 passed 0 failed"。
2. `t313` 的 10.72% 是否来自仓外某次未落盘实验，**无法证实/证伪**（仓内无任何痕迹）；但**在 T312 执行前即引用其结果**这一时间事实，已足以判定该引用不可信。
3. `finai/` 母库 860 接口内部实现未逐一审计（仅核对 370 行守卫与目录结构）。
4. `docs/delivery/*.txt` 单测日志（681/699 passed）未逐行核对与当前代码的一致性。
5. 未评估 `1.ipynb`（Colab 控制台）与 `diagnose_t312_full_period.py` 的逐行实现，仅采信其与产物一致的总量指标。
