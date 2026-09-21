# E 路线第二步：29 门审计框架通用化 Spike（设计 + 可行性验证）

> 范围：设计与可行性验证，**不写产品化代码**。本文件交付：29 门键依赖清单、
> 三层通用化设计、`ExternalEvidenceAdapter` 最小接口草案、按文件/行数的改造工作量估算、
> 以及一个 5 行玩具外部账本实跑全部 29 门的实证结果（`experiments/spikes/gate_generalization/`）。
>
> 纪律：如实汇报跑不通的门与卡因。实证结果中 PASS=16 / FAIL=2 / INCONCLUSIVE=11，
> 其中 3 个 PASS 实际吃的是**本仓文件系统**而非玩具证据（见 §4.3），已如实标注。

---

## 1. 现状绑定面

门禁本体协议（`scripts/gates/base.py`）是干净的：`evaluate(context: dict) -> GateResult`，
ctx 是**鸭子类型 dict**——各门一律 `context.get("key", default)` 取值、内部 `Decimal(str(x))`
强制转型。即适配器只需"产出这些键"，不需产出本仓类型（Order/Trade/Ledger 均不需要）。

绑定发生在三处，而非门禁协议本身：

1. **`context_builder.build_repo_context()`**（431 行）：把本仓 artifact / baostock parquet /
   tasks.md / git 状态回填成 ctx。是"本仓 → ctx"的私有装配器。
2. **`reporting/evidence.py::build_run_evidence()`**（599 行）：定义 evidence 键契约
   （`evidence_version: 1`），字段取自本仓 Ledger/Report 类型，并内嵌 A 股规则
   （`board_limit_pct`、`_is_st`、黄金算例 `sh.600000`）。
3. **门禁内的隐式文件系统默认**：`gate_consistency.py::_REPO_ROOT = parents[2]` 等，
   ctx 缺 `run_records`/`artifact_paths`/`doc_paths`/`sources_dir` 时，自动扫描
   **门禁安装目录所在仓**的 `experiments/runs/*.json`、`docs/`、`finai/sources/`。
   这导致产物发现根绑在工具包安装位置而非用户数据目录（§4.3 实证证实）。

关键已通用件（无需改动即可抽出）：

- `tamper_guard.compute_run_signature / verify_run_signature`：签名域仅
  `{run_id, code_version, data_version, params_hash, status, metrics}` 六键——全部是
  通用回测概念。**evidence 在签名域之外**（registry.py 先签名后合并 evidence），
  外部证据注入不破坏签名语义。签名为无密钥 SHA-256（诚实口径已写在 docstring）。
- `provenance.hash_path_manifest / hash_sequence / repro_fingerprint`：纯函数零 IO，
  输入缺失 ⇒ `None`/抛错而非兜底常量——fail-closed 语义可直接对外部数据用。
- `base.py` 的 GateStatus / GateResult / is_blocking_result / `ci_policy` 分类逻辑。

---

## 2. 每门 ctx/evidence 键依赖清单与通用化分级

图例：✅ 直接通用（键是通用回测概念，喂 dict 即跑）｜🔧 需适配器（概念通用，键形/语义/阈值需适配或参数化）｜🏠 本仓特有（语义绑定本仓引擎/文档/数据 schema，不随包走）。

| 门 | 消费的 ctx/evidence 键 | 键性质 | 分级 | 实证 |
|---|---|---|---|---|
| D-1 | `bars[{date,close,is_exdiv}]` 或 `frame`+`exdiv_dates`, `symbol` | 日线序列是通用概念；`is_exdiv`/`frame` schema 是本仓 baostock 约定 | 🔧 | INCONCLUSIVE（账本不含行情序列） |
| D-2 | `float_mv_list`, `amount_list`（≥30 样本） | 流通市值 vs 成交额背离——A 股流通盘概念；两序列分布检验本身通用 | 🔧 | INCONCLUSIVE |
| D-3 | `daily_yields`(≥60d), `year` | 股息率 PIT 序列——红利数据 schema 绑定 | 🔧 | INCONCLUSIVE |
| D-4 | `frame`/`bars[{tradestatus,volume}]` | 停牌/停量通用；`tradestatus=='1'` 是 baostock 字面量约定 | 🔧 | INCONCLUSIVE |
| D-5 | `orders[{side,price,volume}]` | 键通用；但 ¥300 高价线 + 整手 100 股**硬编码在门体内**，非 ctx 参数 | 🔧（需市场规则参数化） | **FAIL**——玩具 150 股委托在本市场合法，被 A 股整手规则拦下 |
| L-1 | `active_features`, `ledger_entries[{entry_type,amount,fees}]`, `fee_summary`, `is_pre_run`, 构造参 `required_features`(默认 `["DIVIDEND_TAX"]`) | "声明特性须在账本产生流水"是通用概念（Knight Capital 案）；键名本仓味但纯 dict | 🔧 | PASS |
| L-2 | `target_weights`, `actual_values`（dict 或等长 list，≥3 标的） | 权重→分配保真（Spearman≥0.90/等权比值带）是通用概念 | 🔧 | PASS |
| L-3 | `required_calls`, `source_code`, `executed_calls` | AST 调用链审计机制通用；必调清单（BacktestBroker/MatchEngine/...）是本仓实现 | 🔧 | INCONCLUSIVE |
| E-1 | `must_fail_results{case:bool}`, `failed_cases`（5 个固定用例名） | 键通用，但用例由 `must_fail_probe.py` 驱动**本仓真引擎**（import backtest.*） | 🏠 | INCONCLUSIVE |
| E-2 | `fifo_errors`, `final_positions` | `final_positions` 语义="送转后全额卖出探针应归零"的探针快照，非"当前持仓"；探针跑在本仓引擎上 | 🏠 | INCONCLUSIVE（如实不喂；伪造 `{CCC:150}` 会被判 FAIL——fail-closed 生效） |
| E-3 | `trades[{side,price,limit_up,limit_down}]` | 成交价不破涨跌停界是通用概念；界值由 adapter 按外部市场规则提供 | 🔧 | PASS |
| A-1 | `trades[{fees{科目:金额}, total_fee}]` | 费用逐笔分解平衡——纯通用会计断言 | ✅ | PASS |
| A-2 | `daily_cash_flows[{date,cash_start,cash_end,trade_in,trade_out,fee_out,dividend_in,dividend_tax_out,other_in,other_out}]` | 逐日现金守恒方程——通用会计断言；`dividend_tax_out` 桶可由 adapter 折入 `other_out` | ✅ | PASS |
| A-3 | `roundtrip_total_fee`（必需）, `expected_fee`（可选覆盖基准） | 内置基准 112.82/102.00 是 A 股口径，但 `expected_fee` 覆盖口是**现成逃生门** | 🔧 | PASS（经 expected_fee=50.00 覆盖） |
| A-4 | `trades[{date,side,price,volume,amount,fees.STAMP*}]` | 2023-08-28 印花税分段是 A 股硬编码；且**只校验 cutoff 前区段**，cutoff 后 STAMP=0 也 PASS（声明口径即"历史穿越"） | 🔧（需可配置分段费率表） | PASS（玩具全在 cutoff 后，实质未检验 post-cutoff 费率） |
| S-1 | `annualized_turnover` | 换手硬顶通用；400% 阈值应参数化 | 🔧 | PASS |
| S-2 | `use_breadth_timing`, `breadth_series`, `breadth_defense_threshold`, `daily_positions_ratio`, `index_below_ma200_dates`, `run_calendar_bounds`, `timing_grace_dates` | 宽度/MA200 择时语义绑定本仓策略 v1 | 🏠 | INCONCLUSIVE |
| S-3 | `orders_adv_ratio`, `baseline_return`, `stress_return` | ADV 容量+压测通用概念；但 `stress_return` 需对被检引擎**重跑压测**（本仓 runner 内 `_compute_stress_return`） | 🔧 | INCONCLUSIVE |
| S-4 | `penalty_tax_amount`, `total_dividend_received` | 红利差别化税 20% 档是 A 股税制 | 🔧 | INCONCLUSIVE（玩具无分红） |
| S-5 | `total_stamp_tax`, `total_commission`, `trades_count`, `code_evidence` | "有成交则规费非零+代码证据"概念通用；但**印花税科目名硬编码** | 🔧（需可配置必非零科目集） | **FAIL**——玩具市场无印花税科目，如实 FAIL |
| G-1 | `git_commit`, `data_hash`, `timestamp` | 出处三件套通用（git_commit 需 `^[0-9a-fA-F]+$` 且 ≥7 位） | ✅ | PASS |
| G-2 | `tasks_path`/`tasks_content` 或 `task_id`,`is_checked`,`gate_signature` | tasks.md ✅/日期/签名勾选格式是本仓纪律约定 | 🏠 | INCONCLUSIVE |
| G-3 | `sources_dir`（默认 `finai/sources`，FINDING- 计数==370） | 本仓母库守卫 | 🏠 | PASS⚠（扫的是宿主仓非玩具证据，见 §4.3） |
| G-4 | `run_record`（或 ctx 本身含 run_id/params_hash/anti_tamper_signature） | 签名域六键全通用，验签纯函数 | ✅ | PASS |
| G-MDD-1 | `run_record`/`run_records`/`artifact_path(s)`/`metrics{max_drawdown,round_trips}` | 回撤上限通用；35% 阈值+空 artifacts 时扫 `_REPO_ROOT` 默认需参数化 | ✅ | PASS（3 份玩具产物） |
| G-DOC-1 | `doc_paths`（缺省扫 `_REPO_ROOT/docs`）, truth artifact, `expected_gate_count`, `expected_test_baseline` | docs↔产物指标一致性声明绑定本仓文档约定与 `TEST_BASELINE_PASSED=1229` SSOT | 🏠 | PASS⚠（扫的是宿主仓文档） |
| G-STRESS-1 | `round_trips`, `trading_days`/`stress_days` | 压测区间有效性（>0 成交、≥200 日）通用 | ✅ | PASS |
| G-REF-1 | `doc_paths`（缺省扫 `_REPO_ROOT/docs`）+ 白名单约定 | 文档路径引用存在性——绑定本仓文档树 | 🏠 | PASS⚠（扫宿主仓文档） |
| G-REPRO-1 | `run_records`/`artifact_path(s)`，逐产物 `repro_fingerprint`+`metrics` | 同指纹分组 metrics 逐字段一致——通用机制；指纹需按 provenance 五要素口径产出 | ✅ | PASS（2 份同指纹玩具产物） |

**分级汇总**：✅=7 门（A-1、A-2、G-1、G-4、G-MDD-1、G-STRESS-1、G-REPRO-1），
🔧=15 门（D-1..D-5、L-1..L-3、E-3、A-3、A-4、S-1、S-3、S-4、S-5——键或概念通用，
但阈值/科目名/市场规则/schema 需参数化或适配），🏠=7 门（E-1、E-2、S-2、G-2、G-3、
G-DOC-1、G-REF-1——审计对象本身就是本仓引擎探针/文档纪律/母库）；
另有 context_builder/runner 装配层整体属本仓侧不入包。

---

## 3. 三层通用化设计

```
┌─ (c) 本仓特异层（不随包走）──────────────────────────┐
│  context_builder / runner / gate_master_audit /      │
│  acceptance / adoption / must_fail_probe /           │
│  E-1, E-2, S-2, G-2, G-3, G-DOC-1, G-REF-1           │
├─ (b) A 股特异层（toolkit/profiles/ashare/）─────────┤
│  D-5(整手/¥300), A-3(112.82/102.00), A-4(印花分段), │
│  S-4(红利税档), E-3(涨跌停界), D-4(tradestatus)      │
│  + evidence.py 中 board_limit_pct/_is_st/golden_fee  │
├─ (a) 通用层（toolkit/core/，任何回测可适配）────────┤
│  base.py / tamper_guard.py / provenance.py           │
│  A-1, A-2, G-1, G-4, G-MDD-1, G-STRESS-1, G-REPRO-1 │
│  + 参数化后进入：S-1(阈值), L-1(特性名), L-2,        │
│    D-1(跳变率), S-5(必需科目集), L-3(AST 机制)       │
└──────────────────────────────────────────────────────┘
```

**(a) 通用层**成立的理由（实证支撑）：这些门的输入全部是纯 dict/数值/字符串，
断言是通用会计/出处/复现/容量概念（费用平衡、现金守恒、防篡改验签、同指纹一致性、
回撤/压测有效性、特征存活、分配保真）。29 门中 13 门实证吃了玩具 ctx 出 PASS。

**(b) A 股特异层**不删除而是保留为 profile：门逻辑本身体内嵌市场规则
（整手 100、印花税分段、红利税档、涨跌停）。对外部用户是可选 profile，
对本仓仍是默认启用——两层共用同一 `BaseGate` 协议。

**(c) 本仓特异层**留在本仓：它审计的对象（本仓引擎探针、tasks.md 纪律、
母库守卫、docs↔产物一致性）离开了本仓就没有意义。

---

## 4. `ExternalEvidenceAdapter` 最小接口草案

```python
class ExternalEvidenceAdapter(Protocol):
    """外部回测 → 通用层门禁 ctx 键集合的翻译协议。

    只要求产出普通 dict/list/str/Decimal 可转型值；不要求导入本仓任何业务类型。
    缺失键不是错误：返回空 dict ⇒ 对应门 INCONCLUSIVE（fail-closed 语义保留）。
    """

    def trades(self) -> list[dict]: ...
        # {date, side, price, volume, amount, fees{ITEM:amt}, total_fee,
        #  limit_up?, limit_down?}  → A-1, A-4, E-3
    def orders(self) -> list[dict]: ...
        # {side, price, volume}  → D-5(仅 A 股 profile)
    def daily_cash_flows(self) -> list[dict]: ...
        # {date, cash_start, cash_end, trade_in, trade_out, fee_out,
        #  dividend_in, dividend_tax_out, other_in, other_out}  → A-2
    def ledger_entries(self) -> list[dict]: ...
        # {entry_type, amount, fees{}} + fee_summary{} + active_features[] → L-1
    def allocation(self) -> tuple[dict, dict]: ...
        # (target_weights, actual_values) ≥3 标的 → L-2
    def run_records(self) -> list[dict]: ...
        # 每条 {run_id, code_version, data_version, params_hash, status,
        #       metrics{}, timestamp, anti_tamper_signature?, repro_fingerprint?}
        # 签名可由 toolkit 侧 sign_run_record() 代产（签名域外字段不影响签名）
        # → G-4, G-MDD-1, G-STRESS-1, G-REPRO-1, S-1(metrics.annual_turnover)
    def provenance(self) -> dict: ...
        # {git_commit(hex≥7), data_hash(hex≥16), timestamp} → G-1
    def roundtrip_fee_probe(self) -> dict: ...
        # {roundtrip_total_fee, expected_fee} → A-3（expected_fee 为逃生门）
    def market_bars(self) -> dict: ...          # 可选，D 系门另需行情适配段
        # {bars/frame, float_mv_list, amount_list, daily_yields, ...}
```

跑通通用层所需的**最小键集**（本 spike 实证）：`trades`、`daily_cash_flows`、
`run_records`（内含签名）、`provenance` 三件套、`metrics`（mdd/round_trips/trading_days/
annual_turnover）。配齐即覆盖 G-1/G-4/G-MDD-1/G-STRESS-1/G-REPRO-1/A-1/A-2/S-1 等 8 门；
再补 `ledger_entries`/`allocation`/`roundtrip_fee_probe` 覆盖到 13 门。

### 4.3 实证暴露的三个绑定/坑（诚实记录）

1. **文件系统默认扫宿主仓**：G-3/G-DOC-1/G-REF-1 在未喂 `sources_dir`/`doc_paths` 时
   扫描 `_REPO_ROOT`（=门禁文件安装位置）。本 spike 中它们 PASS——但扫的是 FinAI2.0
   自己的 `finai/sources` 与 `docs/`，不是玩具证据。部署到外部后默认会扫**工具包的
   site-packages 安装目录**。通用化必须显式注入这些路径键，或把"默认扫描"改造为
   "显式传入否则 INCONCLUSIVE"（与 fail-closed 口径一致）。
2. **市场规则硬编码在门体内**：D-5（整手/¥300）、S-5（STAMP_TAX 科目名）、
   A-4（分段费率 cutoff）的 FAIL/通过语义不是键的问题，是**断言常量**的问题。
   需抽 `MarketRules` 参数对象（lot_size/price_cap/required_fee_items/rate_schedule）。
3. **探针类门不可外部化**：E-1/E-2 的证据来自对被检引擎跑破坏性探针
   （T+1 拒单/FIFO 零股清算）。外部 adapter 只能**自报**结果（弱证据），
   或 toolkit 暴露探针接口由外部引擎实现——属 🏠 层，建议移出通用层的
   "默认必跑集"，改作可选 `EngineProbeSuite` 扩展点。

另：A-4 只校验 2023-08-28 **前**的印花税率（声明口径即"防历史穿越"），
cutoff 后的低税率不校验——通用化时若要求"当前费率也对账"需补断言。

## 5. 工作量估算（按文件/行数）

| 动作 | 涉及文件 | 规模 |
|---|---|---|
| 直接搬入 `toolkit/core/`（零改动） | `base.py`(130) + `tamper_guard.py`(341) + `provenance.py`(~200) | ~670 行 |
| 通用层门禁抽出（键名/阈值参数化） | `gate_a_accounting.py` 中 A-1/A-2(~250)，`gate_s_scientific.py` 中 S-1(~60)，`gate_g_governance.py` 中 G-1/G-4(~200)，`gate_consistency.py` 中 G-MDD-1/G-STRESS-1(~250)，`gate_repro.py`(261)，`gate_l_liveness.py` 中 L-1/L-2(~350)，`gate_e_engine.py` 中 E-3(~80)，`gate_d_data.py` 中 D-1(~100) | ~1550 行移动+参数化 |
| `MarketRules` 参数对象 + 注入 | D-5/A-4/S-4/S-5/E-3 各 ~20-40 行改动 | ~150-200 行 |
| `ExternalEvidenceAdapter` 协议 + `run_gates(ctx, gate_ids)` 入口 | 新文件 | ~150 行 |
| A 股 profile 归位 + registry 分层 | `gate_master_audit.get_standard_gates()` 拆 core/ashare/repo 三集 + context_builder 归属 repo 层 | ~80 行改动，319 行文件拆分 |
| 本仓保留（不动） | `context_builder.py`(431) `runner.py`(652) `acceptance.py`(268) `adoption.py`(178) `must_fail_probe.py`(263) + 🏠 门 | 0 改动 |
| evidence/registry 产品化 | `build_run_evidence` 拆「通用骨架（签名+指纹+落盘）」与「A 股填充器」 | ~200 行重构（registry.py 295 行本身基本通用，仅 SCHEMA_VERSION/evidence merge 顺序文档化） |

**合计**：净新写约 350-400 行（adapter 协议 + MarketRules + 入口），搬迁/参数化约 1550 行。
量级 = 一次中型 refactor，**1 个 Devin session 可完成 core 层抽出 + adapter 首适配**；
profile 拆分与 evidence.py 重构再加半个 session。**风险不在工程量**，在 §4.3 第 1、3 条
（隐式文件系统默认、探针证据强度）需要产品决策。

## 6. 实证结果（`experiments/spikes/gate_generalization/`）

输入：`toy_ledger.csv`（5 行：date/asset/qty/price，qty 正负表买/卖），
`toy_adapter.py`（上述协议的玩具实现），`run_spike.py` 对全部 29 门逐一 `evaluate(ctx)`。
原始输出：`RESULTS.txt`。汇总：**PASS 16 / FAIL 2 / INCONCLUSIVE 11 / ERROR 0**。

- **真实吃玩具 ctx 的 PASS（13）**：L-1、L-2、E-3、A-1、A-2、A-3(expected_fee 逃生门)、
  S-1、G-1、G-4、G-MDD-1、G-STRESS-1、G-REPRO-1，以及 A-4（cutoff 后区段，见 §4.3 注）。
- **扫宿主仓文件系统的 PASS（3，非可移植性证据）**：G-3、G-DOC-1、G-REF-1。
- **FAIL（2，均为市场规则耦合示范）**：D-5（A 股整手规则拦下玩具合法委托）、
  S-5（印花税科目名硬编码，玩具市场无此科目）。
- **INCONCLUSIVE（11，fail-closed 正常生效——缺证据≠通过）**：D-1..D-4（账本不含行情
  序列）、L-3（required_calls/AST）、E-1/E-2（引擎探针）、S-2（宽度择时语义）、
  S-3（需重跑压测）、S-4（无分红）、G-2（tasks.md 约定）。
- **ERROR（0）**：鸭子类型 ctx.get() 机制无一门抛异常——协议本身对外部 dict 健壮。

复跑：`.venv/bin/python experiments/spikes/gate_generalization/run_spike.py`

## 7. 结论

通用化**可行且分层清晰**：协议层（dict ctx + fail-closed）已经是对的抽象，
绑定集中在装配层与断言常量。核心建议：

1. 抽 `toolkit/core`（~670 行零改动 + ~1550 行参数化）+ `ExternalEvidenceAdapter` 协议；
2. 引入 `MarketRules` 参数对象消除门体内市场规则硬编码；
3. 把文件系统默认扫描改为显式键注入（或 INCONCLUSIVE），否则外部部署会审计错对象；
4. E-1/E-2 探针类门改为可选扩展点，不进通用默认集；
5. A 股逻辑整体降为 `ashare` profile，本仓行为不变。
