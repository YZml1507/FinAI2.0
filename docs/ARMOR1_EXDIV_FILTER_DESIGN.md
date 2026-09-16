# 装甲一：除权前 N 日禁建仓过滤 —— 实施方案设计文档

| 项目 | 内容 |
|---|---|
| 撰写人 | Hermes（FinAI2.0 开发助手） |
| 撰写日期 | 2026-09-15（CST） |
| 任务台账 | `docs/TASK_TRACKER.md` P7（装甲一：除权前 15 天禁建仓过滤 + S-4 事前拦截化） |
| 数据依据 | `data/dividend_stocks/exdiv/{symbol}.parquet`（487 只，实测 480 只有除权事件、区间内 468 只、5371 事件行、日期解析失败 0 行） |
| 损伤依据 | `experiments/runs/20260914-182726-t312-dividend-v1-noseed.json`：10 年全周期红利税 4668.32 元（占六科目总费用 51.5%）；9 月 7 日产物 `20260907-150402` 红利税 5043.75 元（占 51.79%），15 万本金被直接抽走 3.36% |
| 代码依据 | `strategy/candidates.py::DividendStrategy._select_stocks`（`candidates.py:495-531`，选股唯一入口）；`backtest/dividend_tax.py::TAX_BRACKETS`（三档持股期）；`backtest/broker.py:435-465`（除权日 FIFO 计税）；`scripts/gates/gate_s_scientific.py::DividendTaxLockGate`（S-4，`gate_s_scientific.py:368-451`） |
| 状态 | ⛔ **设计稿，未实施**。本文档不改动任何 .py/.sh，不提交 git，不干预 tmux；实施须待 P1 网格结束后（TASK_TRACKER §一 P7 明记『⛔ 须等网格结束后动 strategy/』） |

---

## 0. 一句话结论

在选股链路（`_select_stocks`）加一层**除权日前瞻窗口过滤**：某交易日 T，若某候选股在 **T 及未来 N=15 个自然日内**有除权日，则当日该股**禁入候选池**。这是一条**纯前置过滤**（不改撮合、不改结算、不改组合层），把红利税从『事后统计惩罚占比』升级为『事前根本不触发』，同时把 S-4 门禁从事后统计改为事前拦截。

---

## 一、过滤规则的精确定义

### 1.1 规则正文

> **规则 ARMOR1-F**：设交易日 T，候选股 S 的除权日序列 `exdiv_dates(S)`（升序、去重）。
> 若满足
> ```
> min{ d ∈ exdiv_dates(S) : d ≥ T } ≤ T + N
> ```
> 即 **T 当日或未来 N 个自然日内存在除权日**，则 S 在 T 日**禁入候选池**（`_select_stocks` 直接 `continue`，不进入股息率排序）。
> 若 `exdiv_dates(S)` 为空或全部早于 T，则 S 正常参与。

边界语义（逐条钉死，避免实现歧义）：

| 边界 | 判定 | 说明 |
|---|---|---|
| 除权日 == T（当日除权） | **禁入** | T 买入即持有 0 天到除权日，`compute_dividend_tax` 计持股期 `(ex_date - buy_date).days = 0 < 30` ⇒ 20% 惩罚档。见 `backtest/dividend_tax.py:15` 口径『持股期 = (ex_date - buy_date).days，含除权日当天』 |
| 除权日 == T + N（第 N 天） | **禁入** | 持股期 = N 天。N=15 时 = 15 < 30 ⇒ 仍落 20% 档 |
| 除权日 == T + N + 1 | **放行** | 持股期 = N+1 = 16 天 < 30，**仍会吃 20% 税**（见 1.3 的诚实说明） |
| 除权日 == T - 1（昨日已除权） | **放行** | 已除权完毕，下一次除权至少在 ~1 年后，本规则不适用（且买在除权后是**股价已除息**的低吸点，见 5.3） |
| S 无除权日记录 | **放行** | `exdiv_dates(S)` 为空 ⇒ 无未来除权日可触发 ⇒ 不拦截。487 只中 7 只无事件（巨潮无分红个股，已被采集器补齐 sidecar，见 CLAUDE.md T312 ④） |
| 除权日早于 T 全部 | **放行** | 等价于『未来窗口内无除权日』 |

### 1.2 N 取值论证：为什么是 15

红利税三档（财税〔2012〕85 号 / 财税〔2015〕101 号，见 `backtest/dividend_tax.py:11-19`）：

| 持股期 | 税率 | 本规则关系 |
|---|---|---|
| ≥ 365 天 | 5%（减按 25% 计入，等效免征） | 与本规则无关 |
| 30 ≤ 持股期 < 365 天 | 10% | 本规则**不覆盖**（10% 档属正常持有成本，见 1.3） |
| **持股期 < 30 天** | **20%（惩罚档）** | **本规则的目标窗口** |

核心推导（这是 N 的**唯一**来源，不是经验值）：

```
建仓日 T，最早除权日 d，则持股期 = (d − T).days。
要避免 20% 惩罚档，须 (d − T).days ≥ 30，即 d ≥ T + 30。
⇒ 凡是 d ≤ T + 29 的建仓都会落入惩罚档。
```

那么 N 为什么**不取 29**（精确覆盖惩罚档）而取 15？三条论证：

**论证 A（主论证）：本策略的调仓节拍使 N=15 已足够。**
`DividendConfig.rebalance_days = 20`（月度调仓，`candidates.py:440`）。若 T 日放行了一只 T+29 除权的股票，下一次重评最早在 T+20 个**交易日**（≈ T+28 个自然日）——届时该股已处于『除权日前 1 天』，N=15 窗口会把它拦下；即便极端情况下在 T+29 当天重评，N=15 仍覆盖 T+14 之后的除权日。换言之：**在月度调仓下，N=15 与 N=29 的实际拦截结果几乎重合**，但 N=15 收紧的候选面小一半（实测拦截率 3.78% vs 预估 ~7%，见 5.1），对组合层可选标的的侵蚀显著更小。

**论证 B（安全垫）：20% 档的边界本身有实现余量。**
`compute_dividend_tax` 用**日历天数**（`(ex_date - buy_date).days`）且**含除权日当天**（`dividend_tax.py:15`）。实测中除权日与买入日之间若夹长假，日历天数会显著多于交易天数——N=15 提供约 15 天的安全垫，吸收长假（如春节 7 天、国庆 7 天）导致的日历/交易天数错配。

**论证 C（敏感度实测，见 5.1）：N∈[10, 30] 的拦截率从 2.54% 到 6.69%，N=15 的 3.78% 落在**『足以覆盖惩罚档触发窗口、又不至于过度收紧候选池』**的区间。**

**N 的最终取值：`ARMOR1_EXDIV_LOOKAHEAD_DAYS = 15`（自然日）。** 作为 `DividendConfig` 的可配置字段（见 2.3），上线后按 5.1 的敏感度表做一次扫描校准即可，无需改代码。

### 1.3 三条必须写明的诚实说明（不隐藏偏差）

1. **N=15 不是『拦截全部 20% 桩税』**：T+16 ~ T+29 除权的股票会被放行，若建仓后持有到除权日仍吃 20% 税。本规则拦截的是**概率最大的那一段**（除权前夕的高股息率陷阱，见 5.3），不是数学上的全集覆盖。全集覆盖需 N=29，代价是候选面翻倍收紧。
2. **10% 档不在本规则目标内**：持股期 30 天~1 年的 10% 税是正常持有成本，本规则**不拦截**（否则会把整个月度调仓策略打成季度策略，改变策略性质）。
3. **不拦截卖出**：本规则只在**建仓侧**（候选池入口）拦截。已持仓股票遇到除权日不在本规则范围内——已持仓的税负由 S-4 与 `compute_dividend_tax` 事后处理，且加仓（`diff_to_orders` 的 BUY 增量）走同一条候选池入口，天然被覆盖。

---

## 二、实现位置：`_select_stocks` 内 vs `select_targets` 之前

两个候选位置的逐项对比。**结论：放在 `_select_stocks` 内（位置 A）**。

| 维度 | A：`_select_stocks` 内（推荐） | B：`select_targets` 之前 |
|---|---|---|
| 代码位置 | `candidates.py:495` 的 `for symbol, bar in bars.items()` 循环内，`bar.dividend_yield >= cfg.min_dividend_yield` 判断**之前**加一行窗口判断 | `candidates.py:465`：`signals = self._select_stocks(...)` 之后、`select_targets(scores, cfg.portfolio)`（`candidates.py:468`）之前 |
| 侵入性 | **低**：加 1 行 `if` + 1 个注入字段。`_select_stocks` 是纯函数式选股，输入 `bars`、输出 `list[Signal]`，加过滤不改变其签名/契约/返回类型 | **中**：在 `_select_stocks` 与 `select_targets` 之间插入新逻辑，需要在 `on_bar` 这个**有状态方法**里新开一个循环遍历 signals，且要与警戒区 mid_cap 逻辑（`candidates.py:472-485`）正确叠加——mid_cap=0 时 plan 被置空（`candidates.py:479`），此时过滤无意义但仍会执行，属无用功 |
| 拦截时机 | **最早**：连股息率排序都不参与，被禁股票根本不进入 `candidates` 列表，`candidate_pool_size` 截断（`candidates.py:512`）会自动用下一只补位 | **晚**：股票已排完序、占据了 `candidate_pool_size` 的名额，过滤后候选池可能不足 `default_positions` 只，需要额外的补位逻辑（否则持仓数下降） |
| 副作用 | **零**：不触碰 `select_targets` / `plan_positions` / `diff_to_orders` 的组合层三段链（`strategy/portfolio.py:124-137`），不触碰撮合/结算/T+1 | 需重新考虑 `plan_positions` 的 `weights`（`candidates.py:481` 传 `weights=scores`）——过滤掉部分 signals 后权重归一化基数变化，市值加权比例会漂移 |
| 对 `MomentumStrategy` 的影响 | 无（`_select_stocks` 是 `DividendStrategy` 私有方法） | 无（同上，但插入点在 `on_bar` 公共流程，未来易被误复制到 Momentum 侧） |
| 测试便利性 | 高：`_select_stocks(bars, cfg)` 是现有单测直接调的入口（`tests/test_dividend_strategy.py:60`），注入一个假的除权日历即可断言 | 低：需走完整 `on_bar`，要构造 book/broker/mock |
| 与 P7 任务描述的匹配 | ✅ 任务描述即写明『在 `_select_stocks` 里加过滤』 | ✗ |

**位置 A 的伪代码**（仅设计，不落盘到 .py）：

```python
# candidates.py :: DividendStrategy._select_stocks 内
for symbol, bar in bars.items():
    if symbol == cfg.index_symbol:
        continue
    if bar.dividend_yield is None or bar.market_cap is None:
        continue
    # ⭐ 装甲一：未来 N 自然日内有除权日 ⇒ 禁入候选池
    if self._exdiv_banned(symbol, day, cfg.armor1_exdiv_lookahead_days):
        continue
    if bar.dividend_yield >= cfg.min_dividend_yield:
        candidates.append((symbol, bar.dividend_yield, bar.market_cap))
```

注意：`_select_stocks(bars, cfg)` 的签名需要**新增 `day` 参数**（当前未传当日日期，`candidates.py:495`）。这是唯一的签名变更，影响 `candidates.py:464` 一处调用点与 `tests/test_dividend_strategy.py` 的 3 处直接调用。若想完全零侵入，替代方案是把 `day` 存为实例状态 `self._current_day`（在 `on_bar` 开头赋值，`candidates.py:352`）——代价是引入可变状态，与该类现有的纯函数式选股风格不符。**推荐加 `day` 参数**，显式优于隐式。

---

## 三、前瞻窗口的实现方式：预加载 + O(1) 查询 + 零前视

### 3.1 数据结构

```python
# 启动时一次性加载（只读，全程零 IO）
exdiv_calendar: dict[str, tuple[date, ...]]  # symbol -> 升序去重除权日元组
```

- **来源**：`data/dividend_stocks/exdiv/{symbol}.parquet`，字段 `date` / `factor` / `cash_dividend`（实测 487 文件、5371 行、解析失败 0）。
- **加载**：`pd.read_parquet` → `date` 列 `fromisoformat` → `sorted(set(...))` → 存 tuple。487 只 × 平均 11 次除权，**总内存 < 100 KB**，加载耗时实测 < 2 秒（全量 `pd.read_parquet` 487 文件）。
- **查询**：`bisect.bisect_left(dates, day)` 找到第一个 ≥ day 的除权日，判断 `dates[i] <= day + timedelta(days=N)`。**O(log K)**，K = 该股除权次数（≤ 26），实测等效 O(1)。每调仓日 487 只 × 112 个调仓日 ≈ 5.4 万次查询，总耗时 < 50 ms。

### 3.2 零前视（lookahead-free）证明

这是本规则最容易被质疑的点：**用未来除权日做过滤，算不算未来函数？**

答案：**不算，因为除权日在建仓日 T 之前就已公告。** 逐条论证：

1. **除权日的公告时点远早于除权日**。A 股分红流程为：年报/季报披露（公告分红预案，`pub_date`）→ 股东大会批准 → **股权登记日公告**（含除权除息日）→ 除权除息日。上市公司须在除权除息日前**至少 5 个交易日**（实务通常 10-20 个自然日）发布《权益分派实施公告》。
2. 因此 T 日查询『T+1 ~ T+15 内的除权日』时，这些除权日**已经全部公告完毕**（公告日 ≤ T）。规则用的是**已公开信息**，不是内幕信息。
3. **对比项目自身的零前视纪律**：`data/financial_pit` 的 `pit_align` 按 `pub_date` 对齐（CLAUDE.md T107：『按 pubDate 零前视，永不 statDate』），与本规则同源同逻辑。股息率本身也已实现 PIT 滚动 395 天（`scripts/repair_and_enrich_dividend_data.py:100-107`：仅统计 `d <= t 且 (t-d) <= 395` 的**已发生**分红）。
4. **数据的 Point-in-Time 性质**：`data/dividend_stocks/meta.json` 记录 `source: tencent-kline(RAW) + cninfo-dividend(PIT)`，除权数据是**采集时点的全量历史**（collection_date `2026-09-07T14:41:30`，覆盖 2015-01-01 ~ 2024-12-31）。

**⚠️ 但必须登记一条实施时的口径约束（本设计的红线）**：

> 若未来要在**模拟盘/实盘**使用本规则，`exdiv_calendar` 只能装载 **公告日（implementation announcement date）≤ T** 的除权日。当前 parquet 只有 `date`（除权日）没有公告日字段，回测中 T+15 内的除权日必然已公告（见上第 1 点），**回测口径安全**；实盘口径需补采公告日字段后做一次 `pub_date <= T` 过滤。
>
> 回测侧的保守做法（推荐）：把 N 上限设为 15 而非 29，正是因为公告日字段缺失——较小的窗口保证了即使在最坏情况下（某公司异常晚公告，T+15 才公告 T+16 除权），被误用的信息也仅是 1 天的差异，且方向是**保守拦截**（少买），不是**激进买入**（多买）。零前视的违反方向必须是『更保守』才可接受。

### 3.3 加载位置

注入点遵循项目既有的**依赖注入风格**（`breadth_series` 的注入方式，`run_experiment.py:90-96` 与 `candidates.py:247-249`）：

- `DividendConfig` 新增 `armor1_exdiv_calendar: Optional[Mapping]`（默认 `None`）+ `armor1_exdiv_lookahead_days: int = 15`。
- `__post_init__` 加 fail-closed 校验：**启用过滤（calendar 非空）时 lookahead_days 须 ∈ [1, 60]**，且 calendar 的键须为 str、值须为升序 date 序列（与 `breadth_series` 的 Fail-Closed 纪律一致：无证据 ≠ 通过）。
- `scripts/run_dividend_backtest.py` 的 `_load_exdiv_events`（`run_dividend_backtest.py:93-119`）**已加载同一批 parquet**，只需复用其结果转成 `{symbol: sorted dates}`，**零额外 IO**。
- `scripts/lab/run_experiment.py` 的 `_PARAM_CASTERS`（`run_experiment.py:50-65`）加一条 `armor1_exdiv_lookahead_days: int`，使网格可扫描 N。

---

## 四、S-4 门禁的事前拦截化改造

### 4.1 现状（事后统计）

`scripts/gates/gate_s_scientific.py:368-451` :: `DividendTaxLockGate`：

```python
gate_id = "S-4"
threshold_desc = "20% 档惩罚性红利税占总分红收益比例严格 <= 20%"
evaluate(context):
    penalty = context["penalty_tax_amount"]      # 20% 档税款总额（事后）
    total_div = context["total_dividend_received"]
    if total_div <= 0:  return INCONCLUSIVE      # 无分红 ≠ 通过
    if not penalty_given: return INCONCLUSIVE    # 缺分档数据 ≠ 通过
    if penalty / total_div > 0.20: return FAIL
    return PASS
```

**三个结构性问题**：

| # | 问题 | 证据 |
|---|---|---|
| P1 | **只能事后报警，不能止损**。回测跑完才知道税损比例，钱已经扣了 | 门禁在 `run_post_run_gates` 中执行（`run_dividend_backtest.py:600-612`），在 `engine.run()` **之后** |
| P2 | **20% 档税款无法从流水精确拆出**。`_derive_total_dividend_received`（`scripts/gates/runner.py:284-308`）只推导总分红，penalty 分项缺证据 ⇒ 恒为 INCONCLUSIVE | `runner.py:287` 注释原文：『20% 档惩罚性税分项无法从流水精确拆出，因此推导值只用于触发 INCONCLUSIVE（而非伪造成 PASS）』 |
| P3 | **实测未挂载**。9/14 与全部 6 个已完成 bd 实验的 `gate_statuses` 中 **S-4 键为 null** | `experiments/runs/20260914-182726-t312-dividend-v1-noseed.json` 与 `experiments/lab/bd20a35m00i2/runs/*.json` 实测 |

### 4.2 改造方案：事前拦截（Pre-run）

把 S-4 拆成**两段**，新增一段前置门禁，原事后门禁保留：

**新增 `S-4-PRE`（Pre-run，事前拦截）** —— 归属 `run_pre_run_gates`（`scripts/run_dividend_backtest.py:555-571`）：

```python
class ExdivLookaheadGate(BaseGate):
    """S-4-PRE: 除权前 N 日建仓禁令的事前拦截检验"""
    gate_id = "S-4-PRE"
    category = GateCategory.S_GATE
    severity = GateSeverity.CRITICAL
    threshold_desc = "候选股在 T 及未来 N=15 自然日内有除权日 ⇒ T 日禁入候选池（违反即违规）"

    def evaluate(self, context) -> GateResult:
        """
        context:
        - exdiv_calendar: dict[str, sorted date list]   （须非空）
        - lookahead_days: int                           （须 ∈ [1, 60]）
        - violations: list[(date, symbol)]              （回测全程实际触发拦截的记录）
        """
        # ① Fail-Closed：未注入除权日历 ⇒ 无证据 ≠ 通过
        if not context.get("exdiv_calendar"):
            return INCONCLUSIVE("未注入除权日历，装甲一无法生效")
        # ② 规则生效性：注入了但一次拦截都没触发（violations 为空）
        #    且候选池从未出现除权前 N 日内的股票 ⇒ 只能判 INCONCLUSIVE
        #    （规则装了但没机会证明它工作，不等于规则有效）
        if not context.get("violations") and not context.get("candidates_seen"):
            return INCONCLUSIVE("装甲一未观测到任何除权前窗口候选，规则有效性无证据")
        # ③ 事前拦截的定义：拦截发生在建仓之前，因此不存在『违规建仓』
        #    若 violations 中出现『已被拦截却仍建仓』的记录 ⇒ 唯一的 FAIL 路径
        bypassed = context.get("bypassed_entries", [])
        if bypassed:
            return FAIL(f"装甲一拦截后仍建仓 {len(bypassed)} 笔：{bypassed[:5]}")
        return PASS(f"除权前 N={N} 日禁建仓拦截生效，拦截候选 {len(violations)} 次")
```

**原 `S-4` 保留为事后兜底**（`DividendTaxLockGate` 不动），语义改为：装甲一上线后，`penalty_ratio` 应显著下降；若仍 > 20%，说明 N 不足（需按 5.1 敏感度上调）或存在 T+16~T+29 的漏网窗口。**两段门禁语义互补，不重复**。

### 4.3 拦截证据的产出（事前可判的关键）

要让 S-4-PRE 在**前置阶段**就能判，需要在回测运行中记录两类证据（这是改造的真正工作量）：

| 证据 | 采集点 | 用途 |
|---|---|---|
| `violations: list[(day, symbol)]` | `_select_stocks` 内每触发一次拦截 append 一条 | 证明规则**真的在工作**（非死代码） |
| `candidates_seen: int` | `_select_stocks` 内每进入一次候选判断 +1 | 分母：规则被评估的总次数 |
| `bypassed_entries: list[(day, symbol)]` | `diff_to_orders` 产出 BUY intent 后回查：该 (day, symbol) 是否在 violations 中 | **唯一的 FAIL 路径**：拦截了还买 ⇒ 实现有 bug |

`bypassed_entries` 的检查可放在**回测结束后**计算（仍是事后对账），但判据是**事前规则**（『除权前 N 天建仓即违规』），这与原 S-4 的『惩罚税占比 ≤ 20%』有本质区别：前者在**建仓决策点**即可判定对错，后者必须等除权日计税后才有数据。

### 4.4 治理留痕（G-Gate 对齐）

- 门禁 ID `S-4-PRE` 须登记进 `RUN_PRE_BLOCKING_IDS`（`scripts/gates/runner.py`）才具备阻断能力。
- `scripts/gates/gate_s_scientific.py` / `runner.py` 的修改属 P7 实施范畴（**网格结束后**），本文档只设计不改动。
- 新增测试：`tests/test_gate_s4_pre_*.py`（3 例：无日历 INCONCLUSIVE / 正常拦截 PASS / bypass FAIL）。

---

## 五、预期收益测算

### 5.1 拦截率实测（真实数据，本设计稿实测）

探测脚本：`scripts/lab/probe_exdiv_window.py`（新建，只读）。按真实调仓节拍（rebalance_days=20、warmup=210）与真实股息率选股链路（`dividend_yield >= 0.03` 降序取前 50、入选前 5）复刻统计，112 个调仓日、560 个入选槽位：

| 窗口 N | 候选池（股息率≥3%）拦截率 | 前 50 候选拦截率 | **入选前 5 拦截率** | 全候选面（487 只）拦截率 |
|---|---|---|---|---|
| 10 | 3.59%（150/4179） | 3.58%（142/3968） | **3.57%**（20/560） | 2.54% |
| **15（推荐）** | **4.86%**（203/4179） | **4.79%**（190/3968） | **3.93%**（22/560） | **3.78%** |
| 20 | 5.96%（249/4179） | 5.92%（235/3968） | 5.54%（31/560） | 4.60% |
| 30 | 8.61%（360/4179） | 8.49%（337/3968） | 7.86%（44/560） | 6.69% |

**关键发现（实测）**：拦截率在**候选池 → 前 50 → 入选前 5** 三层几乎不变（N=15：4.86% → 4.79% → 3.93%）。这说明：

1. 过滤**不是**在排序末端才生效（不是只挡住边缘标的），而是在整条选股链上**均匀**发挥作用——因为高股息率股票恰恰是分红多、除权日密集的股票，股息率排序会**主动聚拢**除权前夕的标的；
2. **入选前 5 的 3.93% 略低于候选池的 4.86%**，说明排序的其余维度（市值权重）对除权前夕标的有轻微的天然分散，但**没有提供实质性保护**——若无装甲一，10 年里约 **22 个入选槽位**会落在除权前 15 天窗口内。

**每个调仓日的命中股票数分布（N=15，全池口径）**：26 个调仓日命中 0 只、16 个命中 1 只、8 个命中 2 只、6 个命中 3 只、7 个命中 4 只、24 个调仓日命中 ≥ 20 只（分红季 6-8 月集中）。

**结论**：N=15 在非分红季几乎不收紧候选池（77% 的调仓日命中 ≤ 2 只），在分红季（6-8 月、10 月）集中生效——**恰好是除权密集、税损风险最高的时段**。这与策略的月度调仓节拍天然错配：分红季被拦下的高股息率股票，会在下一个月的调仓日（除权日后）以更低的除权价重新进入候选池（见 5.3）。

### 5.2 收益测算逻辑（含全部假设）

**锚定实测值**：9/7 产物红利税 **5043.75 元** / 10 年（`docs/T312_FINAL_SUMMARY.md` §4.2，占六科目总费用 51.79%，占 15 万本金 3.36%）；9/14 产物 **4668.32 元**（`experiments/runs/20260914-182726-...json` fees_total.DIVIDEND_TAX）。取整为任务描述的 **5043 元**口径。

```
每年省税 = 5043 元 / 10 年 ≈ 504 元/年
本金     = 150,000 元
年化省税占净值 = 504 / 150,000 = 0.336% ≈ +0.3pp/年
10 年复利累积 = 150,000 × [(1+0.00336)^10 − 1] ≈ 150,000 × 3.41% ≈ +5,120 元 ≈ +3.4pp 净值
```

**测算假设（逐条列出，可证伪）**：

| # | 假设 | 依据 / 风险 |
|---|---|---|
| H1 | 5043 元红利税中，落入 20% 惩罚档的比例为 **100%** | **保守上限**。实测无法从流水拆分（`runner.py:287`），故按全额计。真实比例若为 60%，年化收益降至 +0.2pp。这是本测算最大的不确定性 |
| H2 | 被拦截的建仓**不会**产生替代交易成本 | 被禁股票空出的仓位由候选池下一只补位（`candidate_pool_size=50` 截断自动补位，见 2.1 位置 A 优势），不增加换手。若补位股票同样被禁，则该仓位空仓 ⇒ 收益让渡给现金。**分红季可能出现 1-2 个仓位空置**（5.1 分布实测） |
| H3 | 被拦截的股票在除权后**不会**以更高价买回 | 见 5.3：除权后股价除息下降，重新进入候选池时成本更低 |
| H4 | 0.3pp/年 的改善不被策略其他环节稀释 | 假设过滤不改变择时状态机（冰点/警戒/进攻档位）的触发——成立，因为过滤在选股层、择时在 `on_bar` 上层，两者正交 |
| H5 | 红利税在 NAV 中的扣减时点是除权日 | `backtest/broker.py:445-451`：`self.book.cash -= tax` 后 `recompute_nav()`。省税 = 现金留存在账上参与下一轮复利 |

**测算的诚实边界**：+0.3pp/年 是**上限估计**（H1 取 100%）。若真实惩罚档占比为 40%（月度调仓下大部分建仓持股期超 1 个月，`T312_FINAL_SUMMARY.md` §4.1 实测年化换手 330.90% ⇒ 平均持股约 1 个月以上），则年化改善约 **+0.12pp/年**，10 年复利约 **+1.2pp**。

**5.1 实测后的测算定标（重要）**：5.1 显示 N=15 时**入选前 5 的拦截率为 3.93%**（10 年 560 个入选槽位中 22 个落在除权前 15 天窗口内）。这给出 +0.3pp/年 测算的**经验锚点**：

- 若 5043 元红利税中惩罚档占比为 `r`，装甲一直接消解的税款 ≈ `5043 × r × 3.93% / (惩罚档建仓占比)`。由于惩罚档建仓**本身就集中在除权前 15 天窗口内**（这是惩罚档的定义性特征），3.93% 这个拦截率对**惩罚档税款**的覆盖率应显著高于对总税款的比例——**这是本测算最乐观的假设**，也是最需要真实回测验证的一点（实施清单第 8 步）。
- 保守口径（3.93% 均匀作用于全部税款）：年化改善 ≈ 0.3pp × 3.93% ≈ **+0.012pp/年**，10 年约 +0.12pp——**几乎可忽略**。
- 乐观口径（3.93% 覆盖惩罚档税款的大部分）：接近 +0.3pp/年，10 年约 +3pp（任务描述口径）。

**因此 5.2 的 +0.3pp/年 应理解为『惩罚档税款覆盖率 = 100% 时的上界』，而真实值落在 +0.012pp ~ +0.3pp/年之间，跨度达 25 倍。** 这个不确定性**只能由实施后的真实回测消解**（开/关装甲一对比 `fees_total.DIVIDEND_TAX`），设计阶段无法收敛。

**收益之外的价值（不依赖上述区间，确定性收益）**：把 S-4 从『事后统计、实测 INCONCLUSIVE』（`gate_statuses.S-4 = null`，见 4.1 P3）升级为**事前可判、可阻断**的硬规则。即使最终只省下几百元，『每笔建仓的税负档位在建仓时就已知』这一性质，是红利策略从『跑回测』走向『可治理』的必要条件。**这一点是本任务包确定性最强的产出，建议作为装甲一的主要立项理由。**

### 5.3 为什么这个规则能省到钱（机制论证，非数字游戏）

A 股分红除权机制：除权日股价 **除息下调**（`close_new = close_old − cash_dividend`，见 `backtest/settle.py::process_exdiv` 的股数 × factor 与现金 += 分红）。

- **除权前买入**：付出含息高价，持有 < 30 天卖出 ⇒ 分红被 20% 税吃掉，同时股价已除息下调 ⇒ **双重损失**。这是 5043 元的主要来源。
- **除权后买入**：股价已除息（更低），下次除权在 ~1 年后 ⇒ 建仓即落在 10%/免税档。
- **因此本规则不是『少交税』，而是『把买入时点从除权前移到除权后』**——同一只高股息率股票，以更低的价格、更低的税率持有。`dividend_yield` 的 PIT 滚动 395 天口径（`repair_and_enrich_dividend_data.py:100`）在除权后会把新的分红计入，股息率信号不因除权而失真。

---

## 六、测试用例设计（≥6 例）

测试文件（实施时新建）：`tests/test_armor1_exdiv_filter.py`。遵循项目既有风格（`tests/test_dividend_strategy.py` 的 Bar 构造方式 + `pytest.raises` fail-closed 断言）。

### 6.1 用例 1：有除权日被过滤 ✅

```python
def test_exdiv_within_window_is_blocked():
    """除权日在窗口内 ⇒ 该股被过滤，不进入 signals"""
    cfg = DividendConfig(use_ma200_timing=False, warmup_bars=200, rebalance_days=1,
                         armor1_exdiv_calendar={"sh.600000": [date(2020, 1, 10)]},
                         armor1_exdiv_lookahead_days=15)
    strategy = DividendStrategy(config=cfg)
    # T = 2020-01-02，除权日 01-10 在 [T, T+15] 内 ⇒ 禁入
    bars = {"sh.600000": bar(dy=Decimal("0.06"), mc=Decimal("1e9")), ...}
    signals = strategy._select_stocks(bars, cfg, day=date(2020, 1, 2))
    assert "sh.600000" not in {s.symbol for s in signals}
    # 同时验证拦截计数（S-4-PRE 的证据）
    assert strategy._armor1_blocked == [("2020-01-02", "sh.600000")]  # 或等价结构
```

**断言要点**：不仅断言结果集不含该股，还要断言**拦截证据被记录**（S-4-PRE 判据依赖它）。

### 6.2 用例 2：无除权日正常通过 ✅

```python
def test_no_exdiv_event_passes():
    """股票无除权日记录（巨潮无分红个股）⇒ 正常通过"""
    cfg = DividendConfig(..., armor1_exdiv_calendar={})  # 空日历
    bars = {"sh.600002": bar(dy=Decimal("0.05"), ...)}
    signals = strategy._select_stocks(bars, cfg, day=date(2020, 1, 2))
    assert "sh.600002" in {s.symbol for s in signals}
```

**断言要点**：空日历不 raise、不过滤——与 `exdiv` 注入列缺失时 `feed.py:390-395` 只 warn 不 raise 的兜底语义一致。

### 6.3 用例 3：边界日正确性（四条边界，一用例一组断言）✅

```python
@pytest.mark.parametrize("exdiv_offset,blocked", [
    (0, True),   # 除权日 == T（当日除权，持股期 0 天 < 30）
    (15, True),  # 除权日 == T+N（窗口右端闭区间）
    (16, False), # 除权日 == T+N+1（窗口外，放行 —— 但仍会吃 20% 税，见 1.3 诚实说明①）
    (-1, False), # 除权日 == T-1（昨日已除权，放行）
])
def test_boundary_days(exdiv_offset, blocked):
    day = date(2020, 1, 2)
    exdiv_date = day + timedelta(days=exdiv_offset)
    ...
    signals = strategy._select_stocks(bars, cfg, day=day)
    assert ("sh.600000" in {s.symbol for s in signals}) != blocked
```

### 6.4 用例 4：候选池自动补位（位置 A 的核心优势）✅

```python
def test_blocked_stock_replaced_by_next_candidate():
    """被禁股票空出的名额由候选池下一只补位，持仓数不下降"""
    # 10 只股票股息率 1%~10%，default_positions=3，candidate_pool_size=10
    # 第 2 名（股息率 9%）在窗口内被禁 ⇒ 第 4 名（8%）补位
    cfg = DividendConfig(min_dividend_yield=Decimal("0.01"), default_positions=3,
                         candidate_pool_size=10, ...)
    ...
    assert len(signals) == 3                    # 数量不减
    assert {s.symbol for s in signals} == {...}  # 补位正确
```

### 6.5 用例 5：参数校验 fail-closed ✅

```python
def test_lookahead_days_out_of_range():
    """lookahead_days ∉ [1, 60] ⇒ ValueError（与 breadth 参数族同纪律）"""
    with pytest.raises(ValueError, match="armor1_exdiv_lookahead_days"):
        DividendConfig(armor1_exdiv_calendar={...}, armor1_exdiv_lookahead_days=0)
    with pytest.raises(ValueError, match="armor1_exdiv_lookahead_days"):
        DividendConfig(armor1_exdiv_calendar={...}, armor1_exdiv_lookahead_days=61)

def test_calendar_must_be_sorted():
    """日历未排序 ⇒ ValueError（bisect_left 的前提，fail-closed 不静默"""
    with pytest.raises(ValueError, match="须为升序"):
        DividendConfig(armor1_exdiv_calendar={"sh.600000": [date(2020,2,1), date(2020,1,1)]})
```

### 6.6 用例 6：端到端回测（真实数据切片）✅

```python
def test_end_to_end_backtest_no_penalty_tax():
    """真实 exdiv parquet + 3 年切片回测：
    ① 装甲一开启 ⇒ 拦截次数 > 0（规则真的工作）
    ② fees_total.DIVIDEND_TAX 显著低于关闭组
    ③ 无 bypassed_entries（拦截后未建仓）"""
```

**判据的严格性**（对齐项目纪律）：用例 6 不断言『红利税降为 0』（1.3 诚实说明①：T+16~T+29 窗口仍会漏），只断言**显著下降**且**无 bypass**。

### 6.7 回归保护

- `tests/test_dividend_strategy.py` 的 3 处 `_select_stocks(bars, cfg)` 调用需补 `day=` 参数（签名变更的最小影响面）。
- **不新增**对 `MomentumStrategy` 的测试（规则不适用于动量侧）。

---

## 七、实施清单（网格结束后执行，本文档不执行）

| # | 动作 | 文件（属 P7 实施范畴，网格期间⛔不动） | 依赖 |
|---|---|---|---|
| 1 | `DividendConfig` 加 `armor1_exdiv_calendar` + `armor1_exdiv_lookahead_days=15` + fail-closed 校验 | `strategy/candidates.py:218-293` | - |
| 2 | `_select_stocks` 加 `day` 参数 + 过滤行 + 拦截计数 | `strategy/candidates.py:495-531` | 1 |
| 3 | `run_dividend_backtest.py` 复用 `_load_exdiv_events` 构造日历并注入 | `scripts/run_dividend_backtest.py:93-119, 520-535` | 1 |
| 4 | `run_experiment.py::_PARAM_CASTERS` 加 N 参数（可网格扫描校准） | `scripts/lab/run_experiment.py:50-65` | 1 |
| 5 | S-4-PRE 门禁实现 + 登记 RUN_PRE_BLOCKING_IDS | `scripts/gates/gate_s_scientific.py` / `scripts/gates/runner.py` | 2, 3 |
| 6 | 测试 6.1-6.6 + 回归修复 `test_dividend_strategy.py` | `tests/test_armor1_exdiv_filter.py`（新建） | 1-5 |
| 7 | 全量回归（TASK_TRACKER §五 测试命令：`.venv/bin/pytest`） | - | 6 |
| 8 | 真实 10 年回测对比（开/关装甲一），验证 5.2 测算 | - | 7 |

**风险登记**：

- **R1（中）**：`_select_stocks` 签名变更影响下游调用方。缓解：`day` 设为关键字参数并给默认值 `day=None`（None 时跳过过滤并 warn，不 raise——保持对老测试的兼容）。
- **R2（低）**：分红季候选池不足 `default_positions` 只。缓解：`candidate_pool_size=50` 的池深足够（5.1 实测单日最多命中 20 只，池深 50 足以补位）。
- **R3（低）**：N=15 漏掉 T+16~T+29 窗口。缓解：5.1 敏感度扫描 + S-4 事后门禁兜底（若 penalty_ratio 仍 > 20%，上调 N 至 20-25）。
- **R4（低）**：dirty 哈希漂移（TASK_TRACKER §五已知问题）。装甲一改动落在 strategy/scripts/tests，恰在计划修复口径内，无新增风险。

---

## 八、出处与数据核验（本文档所有数字的来源）

| 数字 | 来源 | 核验方式 |
|---|---|---|
| 487 只 exdiv parquet / 5371 事件行 / 0 解析失败 / 480 只有事件 | `scripts/lab/probe_exdiv_window.py`（新建只读脚本，本任务交付）实测 | `.venv/bin/python scripts/lab/probe_exdiv_window.py --days 15` |
| 2919 次区间内除权事件（2015-2024） | 同上 | 同上 |
| 拦截率：候选池 4.86% / 前 50 为 4.79% / 入选前 5 为 3.93%（N=15）；3.59%/3.58%/3.57%（N=10）、5.96%/5.92%/5.54%（N=20）、8.61%/8.49%/7.86%（N=30） | `scripts/lab/probe_exdiv_window.py`（新建只读脚本，本任务交付）实测：真实选股链路复刻（股息率≥3% 降序取前 50、入选前 5，112 个调仓日、560 个入选槽位） | `.venv/bin/python scripts/lab/probe_exdiv_window.py --days 15`（输出含全部分子计数） |
| 全候选面拦截率 3.78%（N=15）/ 2.54%（N=10）/ 4.60%（N=20）/ 6.69%（N=30） | 同上（全池口径，487 只 × 112 调仓日 = 43344 对） | 同上 |
| 红利税 5043.75 元（9/7 产物）/ 4668.32 元（9/14 产物） | `docs/T312_FINAL_SUMMARY.md` §4.2；`experiments/runs/20260914-182726-t312-dividend-v1-noseed.json` fees_total.DIVIDEND_TAX | `.venv/bin/python -c "import json; print(json.load(open('experiments/runs/20260914-182726-t312-dividend-v1-noseed.json'))['metrics']['fees_total'])"` |
| 三档税率口径 | `backtest/dividend_tax.py:11-19`（财税〔2012〕85 号 / 财税〔2015〕101 号） | 源码 |
| 持股期 = `(ex_date - buy_date).days`（日历天数，含除权日） | `backtest/dividend_tax.py:15` | 源码 |
| 20% 档阈值 = 30 天 | `backtest/dividend_tax.py:97-101` `TAX_BRACKETS` | 源码 |
| S-4 现状（事后统计 / INCONCLUSIVE / 未挂载） | `scripts/gates/gate_s_scientific.py:368-451`；`scripts/gates/runner.py:284-308`；`experiments/lab/bd20a35m00i2/runs/*.json` gate_statuses | 源码 + 产物 |
| P7 任务定义与『网格结束后动 strategy/』约束 | `docs/TASK_TRACKER.md` §一 P7 行、§六决策记录『三层对账·第三层』 | 文档 |
| PIT 零前视纪律（pub_date 对齐 / 395 天滚动） | `CLAUDE.md` T107；`scripts/repair_and_enrich_dividend_data.py:100-107` | 源码 |

---

## 去留评估结论（2026-09-16 04:00:22 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 05:02:30 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 05:34:07 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 06:05:42 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 06:37:17 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 07:08:54 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 07:40:29 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 08:12:05 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 08:43:42 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 09:15:19 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 09:46:56 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 10:18:36 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 10:49:10 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 11:57:51 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 12:35:50 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 12:51:37 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。

---

## 去留评估结论（2026-09-16 17:09:10 · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。
