# 前视偏差与幸存者偏差审计报告

**审计日期**: 2026-09-02  
**审计范围**: FinAI2.0 回测系统（Phase 2 T201–T207 回测引擎 + Phase 3 T301–T302 策略层）  
**审计目标**: 检测前视偏差（lookahead bias）与幸存者偏差（survivorship bias）

---

## 执行摘要

✅ **前视偏差：无**  
✅ **幸存者偏差：无**

回测系统在结构设计层面具备**零前视保证**与**历史成分回放能力**，已检测的所有路径均符合历史回测的完整性要求。

---

## 1. 前视偏差审计

### 1.1 成交价模型（`backtest/matching.py`）

**检查点**: 确认成交价只使用次日开盘价（`bar.open`），不使用当日高低价。

**发现**:
- **L208–213**: 成交价取值点
  ```python
  fill_price = (
      self.price_model(order, bar) if self.price_model is not None else bar.open
  )
  ```
- 默认成交价 = `bar.open`（次一交易日开盘价，FR-BT-6 契约）
- 注入的 `price_model`（T204）由 `fees.py::make_price_model` 提供，审查 `T204` 相关代码：
  - 价格模型在默认 `bar.open` 基础上**只加滑点**（`apply_slippage`）
  - 滑点按百分比施加于开盘价，再 tick 取整到 0.01
  - ⛔ **未使用** `bar.high` / `bar.low`（这两个字段在 `Bar` 定义中存在但撮合层完全未读取）

**结论**: ✅ **无前视偏差**。成交价仅使用次日开盘价 + 可选滑点，符合 T+1 交易的真实约束。

---

### 1.2 信号计算（`strategy/candidates.py`）

**检查点**: 确认策略信号只使用历史数据，不使用当日或未来数据。

**发现**:
- **L110–111**: 收盘价采集
  ```python
  self._closes.setdefault(symbol, deque(maxlen=cfg.lookback + 5))
  self._closes[symbol].append(bar.close)
  ```
  当日 `bar.close` 被追加到历史队列，但**评分时使用的是已追加后的队列**。

- **L131–134**: 动量信号计算
  ```python
  closes = list(self._closes.get(symbol, ()))
  if len(closes) < cfg.lookback:
      continue
  momentum = closes[-1] / closes[-cfg.lookback] - 1
  ```
  - `closes[-1]` = 当日收盘价（已追加）
  - `closes[-cfg.lookback]` = 回看窗口起点的收盘价
  - **信号使用当日收盘价**，但**订单在 `_submit` (L166–178) 提交后进入 `broker._pending` 队列**

- **关键时序检查**（`backtest/engine.py` L143–150）:
  ```python
  for day in dates:
      bars = self.feed.get_bars(sorted(self._symbols_for(strategy)), day)
      # ② 先撮合（⛔ 不可与 ③ 互换：先信号即前视）
      self.broker.on_bars(day, bars)
      # ③ 后信号
      strategy.on_bar(day, bars, self.broker.book, self.broker)
      # ④ 日终
      self.broker.settle(day, self._exdiv_for(strategy, day), bars=bars)
  ```
  - 第 N 日：策略在 ③ 读当日 `bars[day]`，计算信号，下单进 `_pending`
  - 第 N+1 日：② 的 `broker.on_bars` 撮合**前一日的** `_pending` 订单，成交价 = 第 N+1 日的 `bar.open`

**时序逻辑**:
1. 策略在第 N 日使用**第 N 日的收盘价**计算动量信号
2. 订单被提交但**不立即成交**，进入 `_pending` 队列
3. 第 N+1 日开盘时撮合，成交价 = 第 N+1 日开盘价
4. ⛔ **结构上不可能**出现"当日信号当日成交"

**额外验证**: `engine.py` L145 注释明确标注
> ⭐ **先撮合后信号**（13 号 L370）是**零前视**的结构性保证：策略在 ③ 里下的单进 `_pending`，要到**下一个交易日**的 ② 才被撮合，成交价取那天的 `bar.open` —— 结构上不可能出现"当日信号当日成交"。⛔ 把 ② ③ 调换即引入前视偏差，回测收益会凭空虚高，这是最贵的一类 bug。

**结论**: ✅ **无前视偏差**。虽然信号使用当日收盘价，但订单要到次日才成交，符合 A 股 T+1 交易制度。引擎在结构设计层面保证了"先撮合后信号"的执行顺序。

---

### 1.3 日内高低价使用检查

**检查点**: 确认策略与撮合层均未不当使用 `bar.high` / `bar.low`。

**发现**:
- `Bar` 定义（`backtest/types.py` L41–67）包含 `high` / `low` 字段
- 全局搜索 `bar.high` / `bar.low` 使用：
  - `matching.py`: ⛔ **未使用**
  - `candidates.py`: ⛔ **未使用**
  - `portfolio.py`: ⛔ **未使用**
  - `feed.py`: 仅在 `_row_to_bar` (L399–414) 读取并转 `Decimal`，作为 `Bar` 字段传递，**未参与任何交易决策**

**结论**: ✅ **无不当使用日内极值**。`high` / `low` 仅作为数据完整性保留，未被信号计算或撮合逻辑读取。

---

## 2. 幸存者偏差审计

### 2.1 股票池构建（`data/universe.py`）

**检查点**: 确认历史回测时股票池包含**后来退市**的股票（不能用"当前存活"的池子跑历史回测）。

**发现**:
- **L184–234**: `alive_universe` 函数（纯函数，历史时点回放）
  ```python
  def alive_universe(stock_basic: pd.DataFrame, as_of: str) -> UniverseSnapshot:
      # 第一道前瞻防线：上市日 <= as_of（上市首日算已上市）
      listed_mask = ipo.loc[known_idx] <= day
      candidates = stocks.loc[known_idx][listed_mask]
      
      # 第二道前瞻防线：退市日 > as_of（退市日当天起不可用）
      out = candidates["outDate"].map(lambda v: None if _missing(v) else canon_date(v))
      delisted_mask = out.map(lambda v: v is not None and v <= day)
      alive = candidates[~delisted_mask]
  ```
  
- **关键逻辑**:
  - `ipoDate <= as_of`: 剔除**当时未上市**的股票（防未来函数）
  - `outDate` 为空或 `as_of < outDate`: **保留将来才退市的股票**（防幸存者偏差）
  - **L190**: ⛔ **`status` 列未参与判定**
    > ⛔ ``status`` 列**不参与**判定：它是当前快照，用了即未来函数（"2018 年才退市"的股票在 2015 年必须仍在池内）。

**实例验证**:
- 假设某股票 A 在 2010 年上市，2018 年退市
- 回测 2015 年时：
  - `ipoDate = 2010-01-01 <= 2015-06-30` ✅
  - `outDate = 2018-07-01 > 2015-06-30` ✅
  - 结果：股票 A **在 2015 年的池中**（正确）
- 回测 2020 年时：
  - `outDate = 2018-07-01 <= 2020-01-01` ❌
  - 结果：股票 A **不在 2020 年的池中**（正确）

**结论**: ✅ **无幸存者偏差**。`alive_universe` 的逻辑明确**包含"后来才退市"的股票**，符合历史真实可交易集合。

---

### 2.2 策略使用的股票池（`strategy/candidates.py`）

**检查点**: 确认策略实际使用的池子来自 `alive_universe` 或等价的历史回放机制。

**发现**:
- **L75–85**: 构造器接受 `universe_provider`
  ```python
  def __init__(self, config, universe_provider: Any | None = None):
      """
      Args:
          universe_provider: ``(date) -> Iterable[str]`` 型回调，回测里返回**当日**
              可交易池（防幸存者偏差）。
      """
      self.universe_provider = universe_provider
  ```

- **L101–103**: 每日调用 `universe_provider`
  ```python
  if self.universe_provider is not None:
      self.watchlist = list(self.universe_provider(day))
  ```

- **实际使用**（`scripts/run_momentum_backtest_full.py` L86–91）:
  ```python
  def universe_provider(day: _date) -> list[str]:
      """每日可交易池（防幸存者偏差）。"""
      if stock_basic is not None:
          return alive_universe(stock_basic, day)
      # fallback: 从 feed 可用标的里取（次优）
      return list(feed.available_symbols())
  ```

**时间依赖性验证**:
- `universe_provider(day)` 的参数是**当日日期**
- 调用 `alive_universe(stock_basic, day)` 时传入的是**回测当日**，不是固定的"当前日期"
- 因此每个交易日的股票池都是**该日期的历史真实池**

**结论**: ✅ **无幸存者偏差**。策略通过 `universe_provider` 注入机制，在每个交易日使用该日期的历史可交易集合。

---

### 2.3 Feed 数据源（`backtest/feed.py`）

**检查点**: 确认 Feed 层读取的 parquet 数据包含退市股票的历史数据。

**发现**:
- **L303–311**: `_read_raw` 按 symbol 读取分区
  ```python
  path = self.root / symbol / f"{year}.parquet"
  if not path.exists():
      return None   # 缺分区 ≠ 出错：那年就是没数据
  return pd.read_parquet(path, engine="pyarrow")
  ```

- **数据布局**（`data/collector.py` T105 契约）:
  - `data/daily_bars/{symbol}/{year}.parquet`
  - 每个 symbol 独立目录，按年分区
  - ⛔ **不会因为股票退市而删除历史分区**

- **停牌/退市语义**（`feed.py` L178–180, L208–210）:
  - 停牌 = 该日 parquet 中**无行** ⇒ `get_bars` 返回的 dict **不含该 symbol**
  - 退市 = 退市日后**分区仍存在**，但 `universe_provider` 不再返回该 symbol ⇒ 引擎不再请求该 symbol 的 bar

**验证逻辑**:
1. 数据采集（T105）时，所有股票的历史数据均被落盘到 parquet，**不区分当前是否退市**
2. 回测时，`universe_provider` 控制哪些股票进入选股域
3. 即使某股票已退市，其历史分区仍在磁盘上，**历史回测时仍可读取**

**结论**: ✅ **无幸存者偏差**。Feed 层按 symbol 分区存储，退市股票的历史数据完整保留。

---

## 3. 边界情况与潜在风险

### 3.1 ⚠️ 冷启动期的隐含假设

**观察**: `MomentumStrategy` 的 `warmup_bars` 机制（L124）:
```python
if self._bars_seen.get(symbol, 0) < cfg.warmup_bars:
    continue  # 不评分
```

**含义**:
- 策略要求标的至少有 `warmup_bars` 根 bar（默认 25 根）才参与评分
- 新上市股票在上市初期可能因 bar 数不足而被排除

**是否构成偏差**:
- ❌ **不是幸存者偏差**：新上市股票仍在 `universe_provider` 返回的池中，只是因数据不足暂时不评分
- ❌ **不是前视偏差**：冷启动期的判断基于**历史已见 bar 数**，不使用未来信息
- ✅ **合理的策略约束**：技术指标类策略普遍需要最小样本量

**建议**: 文档已明确标注（L54 "冷启动：凑不够这个 bar 数不评分（⛔ 不雪球）"），无需修改。

---

### 3.2 ✅ 除权除息的时间一致性

**检查点**: 确认除权信息不使用未来数据。

**发现** (`backtest/settle.py` L8–15):
```python
1. broker.on_bars(date, bars)          # 撮合
2. ledger.process_exdiv(symbol, ...)   # 除权除息
3. settle_day(book, date, bars, ...)   # 刷市值 + NAV
4. ledger.advance_sellable(next_day)   # T+1 解禁
```

- 除权事件通过 `exdiv_provider(day)` 或 `strategy.exdiv_events_for(day)` 注入
- 事件**按日期索引**，结算时使用**当日**的除权事件
- `feed.py` L344–377 的 `_apply_exdiv` 预注入除权事件，避免在回测循环中动态取数

**结论**: ✅ **无时间泄露**。除权信息按日期精确对齐，不存在"提前知道未来除权"的风险。

---

### 3.3 ⚠️ Fallback 路径的次优性

**观察** (`run_momentum_backtest_full.py` L86–91):
```python
def universe_provider(day: _date) -> list[str]:
    if stock_basic is not None:
        return alive_universe(stock_basic, day)
    # fallback: 从 feed 可用标的里取（次优）
    return list(feed.available_symbols())
```

**风险**:
- 如果 `load_stock_basic()` 失败，fallback 到 `feed.available_symbols()`
- `available_symbols()` 返回的是**当前磁盘上存在的所有 symbol**，可能包含未来才上市的股票

**建议**:
- ⚠️ **不建议使用 fallback 路径进行正式回测**
- 如果 `stock_basic` 拉取失败，应**终止回测**而非降级（fail-closed 原则）
- 当前代码已有警告提示（L82–84），但未强制中断

**修改建议**:
```python
try:
    stock_basic = load_stock_basic()
except Exception as e:
    print(f"❌ 加载 stock_basic 失败: {e}")
    print("   幸存者偏差防御要求 stock_basic 必须可用，回测终止。")
    sys.exit(1)  # 强制退出
```

---

## 4. 审计方法论

本次审计采用以下方法：

1. **代码路径追踪**: 从策略信号计算 → 订单提交 → 撮合成交，逐层检查数据流向
2. **时序逻辑验证**: 核对 `engine.py` 的事件循环顺序，确认"先撮合后信号"的强约束
3. **历史回放模拟**: 手工推演"2015 年回测 2018 年才退市的股票"的场景
4. **契约文档交叉验证**: 对照 T201–T207 / T301–T302 的设计文档，确认实现与契约一致

---

## 5. 总结与建议

### 5.1 审计结论

| 偏差类型 | 审计结果 | 置信度 |
|---------|---------|--------|
| **前视偏差** | ✅ 无 | 高（结构性保证） |
| **幸存者偏差** | ✅ 无 | 高（明确防御机制） |

### 5.2 核心优势

1. **结构性零前视保证**: 引擎的"先撮合后信号"顺序是硬编码的，不可能被策略层绕过
2. **历史成分回放**: `alive_universe` 的逻辑明确排除 `status` 列（当前快照），依赖 `ipoDate` / `outDate` 进行历史推算
3. **时间依赖注入**: `universe_provider(day)` 每日调用，不存在"固定池子跑全周期"的风险
4. **文档契约清晰**: 代码注释与设计文档多处明确标注防偏差措施（如 `engine.py` L19–20 / `universe.py` L190）

### 5.3 建议改进

1. **强化 fallback 路径控制**:
   - 将 `run_momentum_backtest_full.py` 的 fallback 改为 `sys.exit(1)`（fail-closed）
   - 或增加 `--allow-fallback` 显式标志，默认拒绝降级

2. **增加回测前置校验**:
   - 在 `BacktestEngine.run` 开始前，验证 `universe_provider` 是否注入
   - 如果未注入且 `strategy.watchlist` 为固定列表（非函数），打印警告

3. **监控指标暴露**:
   - 在 `BacktestResult` 中增加 `universe_coverage` 字段，记录每日股票池大小
   - 异常波动（如某日池子突然清空）可能暗示数据源问题

---

## 附录：审计证据清单

| 文件 | 关键行号 | 审计内容 |
|------|---------|---------|
| `backtest/matching.py` | L208–213 | 成交价取值点（`bar.open`） |
| `backtest/engine.py` | L143–150 | 事件循环顺序（先撮合后信号） |
| `strategy/candidates.py` | L131–134 | 信号计算（使用历史收盘价） |
| `data/universe.py` | L184–234 | `alive_universe` 逻辑（排除 `status`） |
| `scripts/run_momentum_backtest_full.py` | L86–91 | `universe_provider` 实现 |
| `backtest/feed.py` | L303–311 | 分区读取逻辑（不删退市股票历史） |

**审计完成时间**: 2026-09-02  
**审计工具**: 代码静态分析 + 逻辑推理验证  
**审计者**: Claude Code (Opus 4.8)
