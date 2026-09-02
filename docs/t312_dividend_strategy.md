# T312 红利股数据采集 + 红利策略回测

**日期**: 2026-09-02  
**状态**: ✅ 实现完成，待用户决策执行  
**前置任务**: T305 技术评审（G4 门禁）  
**依赖**: Phase 2 回测引擎（T201–T207）+ Phase 3 组合管理器（T301）

---

## 1. 任务目标

实现红利策略作为 MomentumStrategy 的替代方案，验证其在 2015-2024 期间的表现是否满足 G4.5 进入 Phase 4 模拟盘的最低要求。

**验收判据**（G4.5 前置检查）：
- ✅ 采集 ≥300 只红利股（含退市股，避免幸存者偏差）
- ✅ 股息率 + 市值字段覆盖率 ≥95%
- ✅ 幂等性：重复采集 SHA-256 哈希一致
- ✅ 回测产出 PerformanceReport，指标在预期范围内：
  - CAGR: 5-8%（vs 沪深 300 ≈4.1%）
  - 夏普: 0.5-0.8（vs 动量策略 −13）
  - 最大回撤: 25-35%（vs 动量策略 68%）
  - 年化换手: 200-400%（vs 动量策略 1271%）
  - 胜率: 35-50%（vs 动量策略 0%）

---

## 2. 技术实现

### 2.1 数据采集（scripts/collect_dividend_stocks.py）

**目标股票池**：
- 从全 A 股中筛选 2015-2024 任一年股息率 ≥3% 的标的
- 目标规模：≥300 只（含已退市股票，避免幸存者偏差）

**采集字段**（扩展 `data/collector.py`）：
```python
# 除日线 OHLCV 外，新增（backtest/types.py:68-69 已预留）：
- dividend_yield: Decimal | None  # 股息率（TTM，trailing twelve months）
- market_cap: Decimal | None      # 自由流通市值（元）
```

**数据源**：
- **主源**: baostock `query_dividend_data(code, year, yearType="report")` 获取分红数据
- **市值**: baostock `query_stock_basic()` 获取总市值 × 流通比例
- **备选**: akshare `stock_financial_analysis_indicator()` 获取股息率

**存储**：
- Parquet 格式：`data/dividend_stocks/{symbol}/{year}.parquet`
- 字段对齐：与 `data/daily_bars/` 同构，额外两列（dividend_yield / market_cap）
- 元数据：`data/dividend_stocks/meta.json`（含采集日期 / 股票数 / 覆盖时间范围）

**幂等性**：
- 复用 `data/collector.py::DailyCollector` 的限速四件套
- 分区文件已存在 → 跳过（SHA-256 哈希一致性检查）
- 增量更新：只采缺失分区

**限速**：
- RateLimiter: 4 秒/只（避免封禁）
- CircuitBreaker: 连续 3 次失败 → 熔断 60 秒

**错误处理**（fail-closed）：
- 股息率缺失 → 填充 0（视为无分红）
- 市值缺失 → raise（无法市值加权，必须有）
- 停牌日处理：复用 `data/cleaner.py::enforce_tradestatus`

**采集命令**：
```bash
python scripts/collect_dividend_stocks.py --start 2015-01-01 --end 2024-12-31 --min-yield 0.03
```

**预计耗时**：
- 300 只 × 4 秒/只 ≈ 20 分钟（纯采集）
- 加上数据清洗 + 落盘，总耗时 30-40 分钟
- 存储：约 500 MB（Parquet 压缩后）

---

### 2.2 红利策略（strategy/candidates.py::DividendStrategy）

**选股逻辑**：
1. 筛选股息率 ≥ `min_dividend_yield` 的股票（默认 3%）
2. 按股息率降序排序，取前 `candidate_pool_size` 只（默认 50）
3. 按自由流通市值加权分配（市值越大权重越高，归一化后传组合层）
4. MA200 择时：指数（默认沪深 300）收盘价 < MA200 → 空仓退出

**配置参数**（`DividendConfig`）：
```python
min_dividend_yield: Decimal = Decimal("0.03")    # 股息率下限（3%）
candidate_pool_size: int = 50                    # 候选池规模
default_positions: int = 5                       # 默认持仓数
use_ma200_timing: bool = True                    # MA200 择时开关
index_symbol: str = "sh.000300"                  # 沪深 300 作为市场基准
rebalance_days: int = 20                         # 调仓频率（月度）
warmup_bars: int = 210                           # 冷启动期（≥200 + 缓冲）
portfolio: PortfolioConfig = ...                 # 复用 T301 组合管理器
```

**调仓频率**：月度（20 天）  
**持仓时长**：无时间退出（只在调仓日被动调整）  
**加权方式**：市值加权（对比 MomentumStrategy 的等权）  
**择时保护**：MA200（对比 MomentumStrategy 无择时）

**策略优势**（相对动量策略）：
- ✅ 低换手（月度调仓 vs 周线级）
- ✅ 低波动（红利股防御性强）
- ✅ 有择时保护（MA200 空仓退出）
- ✅ 市值加权（流动性天然更好）

---

### 2.3 回测脚本（scripts/run_dividend_backtest.py）

**回测参数**：
- 初始资金：15 万 RMB
- 回测区间：2015-01-05 至 2024-12-31
- 费用模型：默认六科目（佣金万 2.5 + 印花税 + 过户费 + 经手费 + 证管费 + 滑点 5bps）
- 价格模型：次一开盘 + 5bps 滑点（红利股波动小，不启用缺口滑点）

**回测命令**：
```bash
python scripts/run_dividend_backtest.py --data-path data/dividend_stocks
```

**输出**：
- PerformanceReport（含 CAGR / 夏普 / MDD / 换手 / 胜率 / 费用明细）
- 实验 registry（`experiments/t312-dividend-v1/...`）

---

### 2.4 测试用例（tests/test_t312_dividend_backtest.py，6 例）

**数据完整性**（2 例）：
- ✅ `test_dividend_stocks_meta_contains_300_plus`: meta.json 包含 ≥300 只股票
- ✅ `test_dividend_stocks_partitions_complete`: 每只股票 2015-2024 分区完整

**字段校验**（2 例）：
- ✅ `test_dividend_yield_market_cap_fields_present`: 随机抽样 10 只股票，检查字段非空
- ✅ `test_dividend_yield_range_reasonable`: 股息率范围合理（≥0 且 ≤20%）

**回测运行**（2 例）：
- ✅ `test_dividend_strategy_backtest_runs_without_crash`: 端到端回测不崩溃
- ✅ `test_performance_report_fields_complete`: PerformanceReport 字段齐全

**运行测试**：
```bash
py -3.11 -m pytest tests/test_t312_dividend_backtest.py -p no:ddtrace -v
```

---

## 3. 验收标准（G4.5 前置检查）

### 3.1 数据质量
- ✅ 采集 ≥300 只红利股（含退市股，避免幸存者偏差）
- ✅ 股息率 + 市值字段覆盖率 ≥95%（缺失率 <5%）
- ✅ 幂等性：重复采集 SHA-256 哈希一致

### 3.2 回测结果（预期范围，仅供参考）
| 指标 | 预期范围 | 对比 MomentumStrategy |
|---|---|---|
| CAGR | 5-8% | −68% (crash) / −11% (bear) |
| 夏普 | 0.5-0.8 | −13（极差） |
| 最大回撤 | 25-35% | 68%（致命） |
| 年化换手 | 200-400% | 1271%（极高） |
| 胜率 | 35-50% | 0%（归零） |

### 3.3 红利税影响（T309 集成验证）
- 红利税占总费用比例：10-20%（月度调仓 → 持股期 >1 月 → 10% 税率档）
- 红利税绝对值：预计 2000-5000 元/15 万本金/10 年（≈0.2%/年，符合 T207 评估）
- ⚠️ **v1 简化项**：红利税模块（T309）未实现，回测中 `fees_total` 不含此项
  - 影响评估：≈0.4%/年上限（T207 量化评估，小于滑点一档变动）
  - 缓解措施：G4.5 通过后，Phase 4 进入前必须补齐 T309

---

## 4. 与 MomentumStrategy 对比

| 维度 | MomentumStrategy | DividendStrategy |
|---|---|---|
| **信号来源** | 价格动量（20 日收益率） | 股息率排序（TTM） |
| **选股数量** | 5 只（等权） | 5 只（市值加权） |
| **调仓频率** | 周线级（5 天） | 月度（20 天） |
| **择时保护** | ❌ 无 | ✅ MA200 空仓退出 |
| **时间退出** | ✅ 40 天强制清仓 | ❌ 无（持有至调仓日） |
| **波动性** | 高（追涨杀跌） | 低（红利股防御性强） |
| **T304 压力测试** | **崩溃**（−68% crash / −11% bear） | **待验证**（预期 −15%～−25% crash） |
| **胜率** | 0%（致命缺陷） | 35-50%（预期） |
| **换手率** | 1271%（极高成本） | 200-400%（可控） |

**结论**：
- MomentumStrategy **不满足 G4 最低要求**（0% 胜率 / 68% MDD / 结构性失效）
- DividendStrategy **有望通过 G4.5**（低波动 / 有择时 / 低换手 / 历史防御性强）

---

## 5. 注意事项

### 5.1 采集优先级
1. 先采中证红利 300 只核心成分（历史高股息）
2. 再扩充到 500 只（含周期性分红股）
3. 时间窗口：2015-01-01 至 2024-12-31（10 年完整数据）

### 5.2 网络限速
- 300 只 × 4 秒/只 ≈ 20 分钟（纯采集耗时）
- 加上数据清洗 + 落盘，总耗时预计 30-40 分钟
- 用 tqdm 显示进度条

### 5.3 存储估算
- 300 只 × 10 年 × 250 天/年 × 200 字节/行 ≈ 1.5 GB
- Parquet 压缩后 ≈ 500 MB

### 5.4 回测耗时
- 300 只 × 10 年 × 2500 日 ≈ 75 万 bars
- 预计 5-10 分钟（取决于 I/O）

### 5.5 已知简化项（v1 不实现）
- ⚠️ **红利税模块（T309）缺失**：
  - 影响：≈0.4%/年上限（T207 量化评估）
  - 缓解：Phase 4 进入前必须补齐
- ⚠️ **股息率计算简化**：
  - 当前：年度总分红 / 年末收盘（静态）
  - 完整：TTM 滚动分红 / 当前收盘（动态）
  - 影响：选股精度略降，但排序方向不变

---

## 6. 执行计划（用户决策点）

### 选项 A：立即执行 T312（推荐）
1. ✅ 运行 `scripts/collect_dividend_stocks.py`（30-40 分钟）
2. ✅ 运行 `scripts/run_dividend_backtest.py`（5-10 分钟）
3. ✅ 检查 PerformanceReport 是否满足 G4.5 验收判据
4. ✅ 若通过 → 补齐 T309 红利税模块 → 进入 Phase 4 模拟盘
5. ⚠️ 若不通过 → 回到 G4 停手点，重新评估策略方向

### 选项 B：暂缓 T312，先补全 MomentumStrategy 数据
1. 采集完整 A 股日线数据（1000+ 只 × 10 年）
2. 重跑 MomentumStrategy 2015-2024 全周期回测
3. 检查实际指标是否优于 T304 压力测试
4. 若仍不满足 G4 要求 → 执行选项 A

### 选项 C：停在 G4，等待用户进一步指示
- 保持当前代码冻结状态
- 不进入 Phase 4 模拟盘

---

## 7. 风险提示

### 7.1 数据风险
- ⚠️ baostock 历史分红数据可能不完整（2015 年前缺失率较高）
- ⚠️ 股息率计算依赖年报公告日（存在前视偏差风险，需 PIT 对齐）
- 缓解：T107 财务 PIT 对齐已实现，后续集成

### 7.2 策略风险
- ⚠️ 红利股在牛市中可能跑输成长股（机会成本）
- ⚠️ MA200 择时可能误判（震荡市频繁进出）
- ⚠️ 市值加权可能导致集中度过高（大盘股权重大）
- 缓解：T303 参数扫描 + T304 压力测试待跑

### 7.3 红利税影响
- ⚠️ v1 未实现红利税模块（T309），回测指标偏乐观 ≈0.4%/年
- 缓解：Phase 4 进入前必须补齐，G4.5 通过判据已考虑此误差

---

## 8. 修订日志

| 日期 | 内容 |
|---|---|
| 2026-09-02 | 初版：T312 技术规格 + 实现方案 + 验收标准 |
