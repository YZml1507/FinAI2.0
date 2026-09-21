# 交接：Scheduled Full Gate Audit 每日失败修复（主人指派，优先级高）

> 本文档是**任务交接件**：包含已完成的诊断结论、各门禁证据键清单、实施设计与红线。
> 接手方读完本文件 + `CLAUDE.md` 即可开工，无需重走诊断。

## 一、任务原文（主人指派）

修复 Scheduled Full Gate Audit 每日失败。要求：

1. 给回测/registry 链路补 trade 级证据落盘（Ledger/Journal 里已有数据，序列化进 run artifact，即 workflow 注释里的 M4+ 欠账）；
2. 修文档漂移与幽灵引用（合理处用白名单/`gate-doc-ignore` 豁免，**不许删证据**）；
3. legacy 产物补指纹或如实登记处置；
4. 本地跑 `python -m scripts.gates.gate_master_audit --scheduled` 验证阻断清零或大幅下降后再 push；
5. **红线**：不降门禁强度、不改 `--scheduled` 为 `--ci` 糊弄变绿、不伪造证据、测试保持绿。

## 二、诊断实况（2026-10 复测，与原诊断略有出入）

`python -m scripts.gates.gate_master_audit --scheduled` 实测：**29 门 = 14 PASS + 0 FAIL + 1 SKIP(D-4) + 14 INCONCLUSIVE**（非原诊断的 16 项——G-DOC-1 与 G-REF-1 在先前文档治理后已 PASS，**② 子项无需再做**）。

14 项阻断分两类：

### A. 13 门同根因：run 产物缺 trade 级证据

`reporting/registry.py::record_run` 只收 `report`（PerformanceReport/metrics 级），`BacktestResult` 里的 `orders`/`trades`/`journal_entries` **从未序列化进产物**。各门所需 ctx 键（已逐门读源码核实）：

| 门禁 | 所需 ctx 键 | 证据来源 |
|---|---|---|
| D-5 | `orders`（price/volume/side；买单查 >300 元高价股 + 非整手） | `result.orders` 序列化 |
| L-1 | `active_features`、`fee_summary`、`ledger_entries`（声明特性须在账本/费用流水中现身） | journal 序列化 + 声明清单 |
| L-2 | `target_weights`、`actual_values`（重合标的 ≥3 做秩相关/保真度） | 策略末次调仓目标权重 + 成交实现值 |
| L-3 | `executed_calls`（须含 `required_calls`=["BacktestBroker","MatchEngine","compute_metrics"] 字面成员） | 运行期插桩计数 |
| E-3 | `trades`（含 price/side/limit_up/limit_down **价格**字段） | 成交 + 当日板价富化 |
| A-1 | `trades`（含 `fees` dict 七科目） | Trade.fees 序列化 |
| A-2 | `daily_cash_flows`（每行 cash_start/cash_end/trade_in/trade_out/fee_out/dividend_in/dividend_tax_out/other_in/other_out，逐日现金守恒） | journal 按日重放聚合 |
| A-3 | `roundtrip_total_fee`（10 万元往返实测费，基准 112.82/102.00，容差 ±0.05） | `backtest.fees.compute_fees` 实时算（基准独立于产物） |
| A-4 | `trades`（date/side/amount/fees，历史费率分段穿透核验） | 同上 trades |
| S-2 | `index_below_ma200_dates` **或** 宽度路径：`use_breadth_timing`+`breadth_series`+`breadth_defense_threshold`+`daily_positions_ratio`+`run_calendar_bounds`+`timing_grace_dates` | 锚点产物是**宽度择时**配置 → 走宽度路径 |
| S-3 | `orders_adv_ratio`（每单成交额/当日该票成交额，>2% 即 FAIL）、`baseline_return`、`stress_return`（滑点+50% 情景收益，≤0 即 FAIL） | 委托×当日 amount 列 + 配对压测跑批 |
| S-4 | `penalty_tax_amount`（20% 档红利税总额）、`total_dividend_received`（>0 才有证） | journal DIVIDEND_TAX meta **需补档位分解** |
| G-STRESS-1 | `round_trips` + `trading_days`（压测区间须 >0 成交且 ≥200 交易日） | 独立压测窗跑批（如 2015 全年≈244 交易日） |

### B. G-REPRO-1：4 份 legacy 产物缺 `repro_fingerprint`

- 无指纹产物：`experiments/runs/20260903-135508` / `20260903-142212` / `20260906-184556` / `20260907-150402`（4 份均 params_hash `f54c298d`）。
- 门语义（`scripts/gates/gate_repro.py`）：PASS 需「扫描面内零 legacy **且** ≥1 组同 `repro_fingerprint` 产物的 metrics 逐字段一致」；legacy 存在恒为 INCONCLUSIVE；**adopted 产物若落 legacy 则 BLOCKER**。
- 处置方案（已判定为唯一诚实路径）：指纹要求 5 要素内容哈希（code/data/calendar/universe/params_hash），legacy 时代未记录 → **不可事后补算**（补算=伪造出处）。应 `git mv` 至 `experiments/legacy/`（`runs/` 产物是 git 跟踪的权威产物，移动留痕）+ 写处置说明。注：`experiments/quarantine/` 已有同类先例。
- 同时需要**同配置跑两遍**产生同指纹 artifacts → verified group ≥1 → PASS。

## 三、已查清的架构事实（勿重查）

- 锚定机制：`context_builder.py` 取 `experiments/runs/*.json` 中**最新签名产物**喂 ctx；`gate_consistency._resolve_truth_artifact` 取最新有成交 FINISHED 产物作 G-DOC-1 真值锚。
- 现锚点 `20260915-235155-t312-dividend-v1-noseed`：宽度择时配置（attack 0.45/defense 0.25/mid_cap 0/ice_confirm 1/use_breadth_timing/use_ma200_timing=False + `breadth_series` 内嵌 params），CAGR 3.76%、换手 3.31、往返 100 笔。**证据跑批应复刻此配置**（新 run 会成为真值锚；指标一致则 G-DOC-1 不受影响）。覆盖参数经 `run_experiment.py` 同款 `dataclasses.replace` 注入，`breadth_series` 源文件 `experiments/lab/market-breadth-a/breadth20_daily.parquet` 在盘。
- `run_dividend_backtest.py::_build_post_run_gate_context` **已算出**大半证据（`index_below_ma200_dates`/`timing_grace_dates`/`daily_positions_ratio`/`run_calendar_bounds`/`breadth_*`/`must_fail_results`）——只用于跑后门禁，没落盘。修复核心 = 把这些 + 订单/成交/流水序列化进产物。
- 签名：`tamper_guard.compute_run_signature` 只绑 `run_id/code_version/data_version/params_hash/status/metrics` 六字段 → `evidence` 区块**可安全附加**不破旧签名验证（但也在签名保护之外，属已知口径）。
- `_canonicalize`（ledger.py）**拒绝 float** → evidence 内含 float 比率（`orders_adv_ratio`、`daily_positions_ratio`）→ evidence 走独立 JSON-safe 序列化（Decimal→str、date→iso、enum→value、float 保留），不经过 `_canonicalize`；在 `record_run` 里于 canonical 化 payload 之后并入 `record_payload["evidence"]`。
- Journal 流水类型：`TRADE`（amount：买=−(额+费)/卖=+(额−费)，fees dict 七科目）、`DIVIDEND`(?)—实际现金分红走 `EXDIV_ADJUST`（amount=dividend_cash, meta.cash_dividend）、`DIVIDEND_TAX`（amount=−税, fees={DIVIDEND_TAX}）、`CASH_IN`/`CASH_INTEREST`/`FEE`/`SETTLE`（meta 含 nav/cash/market_value 快照）。`JournalEntry.to_dict()` 已是落盘形态。
- 红利税档位：`backtest/dividend_tax.py::compute_dividend_tax` 内部逐 lot 按 `TAX_BRACKETS`(365→5%/30→10%/0→20%) 计税但**只返回总额**。需在 DIV 循环里加 `by_rate` 累加器，暴露明细函数；`broker.py:484-502` 处把 `meta["tax_by_bracket"]` 写进 DIVIDEND_TAX 流水 → 证据聚合出 20% 档金额。
- L-2 权重捕获点：`strategy/candidates.py` ~L694-744 调仓段（`select_targets`→`plan_positions(weights=scores)`→`diff_to_orders`）。在策略实例上记 `self._evidence_last_rebalance = {"date":..., "target_weights": {...}}`（只读捕获，不改行为）；actual_values 由证据装配侧从该次调仓的成交填充额重建。
- L-3 插桩：required_calls 是字面量 `["BacktestBroker","MatchEngine","compute_metrics"]`（`context_builder.py:277`）。在 runner 内 wrap `broker.on_bars`/`matcher.match`/`compute_metrics` 调用点计数，把被执行的目标名放入 `executed_calls`（如 BacktestBroker 被调用→记 "BacktestBroker"）。
- D-5 注意：订单 `price` 字段——MARKET 单为 None；Order 序列化带 side/volume/price/status。
- E-3 板价：Bar 只有 `limit_up/limit_down` **bool 标记**（cleaner `mark_limit_flags` 按板块阈值算），无板价字段 → 证据装配需用 `board_limit_pct`/`LimitFlagsConfig`（主板±10%/创业科创±20%/ST±5%）由 `preclose` 重算板价并 tick 取整（`TICK_SIZE` ROUND_HALF_UP），与 `make_price_model` 限幅口径一致。
- S-3 `orders_adv_ratio`：bar 帧有 `amount` 列（当日成交额）；逐委托 `volume×price / 当日该票 amount`。
- 跑批耗时：全窗 2015-2024 单次 **~26 分钟**（lab experiment.json 实测 25.8-26.8min）。

## 四、建议实施序列（未执行）

1. `backtest/dividend_tax.py`：拆出明细函数返回 `(total, {rate_str: tax_sum})`，`compute_dividend_tax` 保签名作 wrapper。
2. `backtest/broker.py`：DIVIDEND_TAX 流水 meta 写 `tax_by_bracket`。
3. `strategy/candidates.py`：调仓点记 `_evidence_last_rebalance`。
4. 新建 `reporting/evidence.py`：`serialize_orders/trades(富化板价)/journal`、`daily_cash_flows`（journal 按日重放，校验守恒）、`golden_roundtrip_fee()`（compute_fees 实算）、`orders_adv_ratio`、`allocation_evidence`、`dividend_tax_evidence`（聚合 meta 档位）、`build_run_evidence(result, extras)`。
5. `reporting/registry.py::record_run` 加 `evidence: Mapping|None` 形参，canonical payload 之后并入（不进签名域）。
6. `scripts/run_dividend_backtest.py`：组 evidence（复用 `_build_post_run_gate_context` 产物 + result 序列化 + executed_calls 插桩 + `active_features=["DIVIDEND_TAX"]` ⛔ 只声明账本可见特性，否则 L-1 判死代码 FAIL）→ `record_run(evidence=...)`。
7. `scripts/gates/context_builder.py`：`ctx.update(record.get("evidence") or {})`。
8. 新建 `scripts/produce_gate_evidence_run.py`：编排四跑——滑点+50% 全窗（scratch registry）→ 2015 压测窗（scratch）→ baseline×2（authoritative `runs/`，首跑带完整 evidence_extra 含 stress_return/压测窗统计）；跑批前**必须先提交代码**（code_hash 稳定→两跑同指纹）。
9. `git mv` 4 份 legacy → `experiments/legacy/` + README 处置说明 + tracker 登记。
10. 新测试（`tests/test_run_evidence.py` 等）：序列化往返、现金流守恒、evidence 形参、context_builder 回填、档位分解求和、缺证据 fail-closed。
11. 同步 `scripts/gates/constants.py::TEST_BASELINE_PASSED`（当前 1210）。
12. 全量 pytest + `--scheduled` 验证 → commit → push（远端 `ssh://github.com/YZml1507/FinAI2.0.git`）。

## 五、验收口径

`python -m scripts.gates.gate_master_audit --scheduled` 阻断（INCONCLUSIVE+FAIL）应清零或大幅下降；允许残留仅当某门证据在语义上对本配置确不适用且门禁口径本就判 INCONCLUSIVE（如 S-4 在无分红区间）——此时在 tracker 如实登记「结构性不适用」，**不得改门**。

## 六、仓库当前状态

- master @ `4a27b62`（已推送）；1210 测试全绿；工作区干净。
- e26 已收单（M5「强」但评估暂不立项；候选库 M5/E25-F6/E23-H2 登记在 `docs/E26_CATEGORY_ASSESSMENT.md`）。
- 研究面暂停中；本任务是当前唯一高优先级 backlog。
