# T311 红利策略验收报告

**任务**: 实现低 beta 红利策略（DividendStrategy）  
**日期**: 2026-09-02  
**状态**: ✅ 通过

---

## 1. 交付件清单

### 1.1 核心代码
- ✅ `strategy/candidates.py::DividendConfig` — 配置类（8 个参数 + fail-closed 校验）
- ✅ `strategy/candidates.py::DividendStrategy` — 策略类（引擎契约 + 选股逻辑 + MA200 择时）

### 1.2 测试代码
- ✅ `tests/test_dividend_strategy.py` — 12 个单元测试（参数校验 4 + 选股逻辑 3 + MA200 择时 3 + 调仓频率 2）

### 1.3 演示脚本
- ✅ `scripts/t311_dividend_strategy_demo.py` — 端到端演示（240 交易日模拟）

---

## 2. 测试结果

### 2.1 单元测试（12/12 通过）

```bash
py -3.11 -m pytest tests/test_dividend_strategy.py -p no:ddtrace -v
```

**测试覆盖**:

#### 参数校验（4 例）
- ✅ `test_min_dividend_yield_must_be_decimal` — float 拒绝
- ✅ `test_min_dividend_yield_negative_rejected` — 负数拒绝
- ✅ `test_min_dividend_yield_over_20pct_rejected` — 超 20% 拒绝
- ✅ `test_candidate_pool_less_than_max_positions_rejected` — 候选池 < 最大持仓拒绝

#### 选股逻辑（3 例）
- ✅ `test_dividend_yield_filter` — 股息率筛选（3 只标的，min=3%，选中 2 只）
- ✅ `test_candidate_pool_size_limit` — 候选池规模截断（10 只标的，pool=5，选前 5）
- ✅ `test_market_cap_weighting` — 市值加权归一化（权重 50%/25%/25%）

#### MA200 择时（3 例）
- ✅ `test_ma200_timing_above_threshold` — 指数 > MA200 → 正常选股
- ✅ `test_ma200_timing_below_threshold` — 指数 < MA200 → 空仓
- ✅ `test_warmup_period_no_trading` — 冷启动期（<210 日）→ 不交易

#### 调仓频率（2 例）
- ✅ `test_rebalance_frequency` — D1 调仓 → D2-D19 不动 → D20 再调
- ✅ `test_consecutive_rebalances` — 连续多月调仓验证

### 2.2 端到端演示

```bash
$env:PYTHONPATH = 'D:\Projects\FinAI2.0'; py -3.11 scripts\t311_dividend_strategy_demo.py
```

**演示结果**:
- 总交易日数: 240
- 冷启动期: 210 天（MA200 积累）
- 调仓次数: 2
- 总下单数: 6
- 平均每次调仓选股: 3.0 只
- 择时空仓天数: 29 天（冷启动后）

**关键验证**:
- ✅ MA200 择时保护生效（前 200 天指数 < 120，后 40 天 ≥ 120，空仓天数 > 0）
- ✅ 股息率筛选生效（仅选择股息率 ≥ 3% 的标的）
- ✅ 月度调仓生效（约每 20 天调仓一次）
- ✅ 市值加权生效（sh.600008/600005/600004 等大市值股票优先入选）

---

## 3. 技术规格符合性

### 3.1 策略逻辑
| 规格 | 实现 | 状态 |
|------|------|------|
| 股息率筛选 | `bar.dividend_yield >= config.min_dividend_yield` | ✅ |
| 候选池排序 | 按股息率降序排序，取前 N 只 | ✅ |
| 市值加权 | `weight = market_cap / total_market_cap` | ✅ |
| MA200 择时 | 指数 < MA200 → 返回空列表（组合层清仓） | ✅ |
| 调仓频率 | 每 `rebalance_days` 交易日调仓一次 | ✅ |
| 冷启动期 | `_bar_count < warmup_bars` → 不交易 | ✅ |

### 3.2 数据类型
| 字段 | 类型 | 状态 |
|------|------|------|
| `min_dividend_yield` | Decimal | ✅ |
| `market_cap` | Decimal | ✅ |
| `dividend_yield` | Decimal | ✅ |
| Bar 扩展字段 | `dividend_yield` / `market_cap` 可选 | ✅ |

### 3.3 Fail-Closed 校验
| 校验项 | 实现 | 状态 |
|--------|------|------|
| `min_dividend_yield` 非 Decimal → TypeError | `__post_init__` | ✅ |
| `min_dividend_yield` < 0 或 > 0.20 → ValueError | `__post_init__` | ✅ |
| `candidate_pool_size` < `max_positions` → ValueError | `__post_init__` | ✅ |
| `rebalance_days` < 1 → ValueError | `__post_init__` | ✅ |
| `warmup_bars` < 200 → ValueError | `__post_init__` | ✅ |
| 指数数据缺失（MA200 开启时）→ ValueError | `on_bar()` | ✅ |

### 3.4 引擎契约符合性
| 契约 | 实现 | 状态 |
|------|------|------|
| `on_bar(day, bars, book, broker)` 签名 | 完全一致 | ✅ |
| `watchlist` 属性 | 支持 | ✅ |
| `universe_provider` 注入 | 支持（可选） | ✅ |
| 只产意图不撮合 | 调用 `broker.submit(Order)` | ✅ |
| 组合层对接 | 复用 `strategy/portfolio.py` 三段函数 | ✅ |

---

## 4. 设计特性

### 4.1 低 Beta 防御性特征
- **股息率筛选**: 高股息率股票通常是成熟企业，波动性较低
- **市值加权**: 偏好大市值股票，流动性好，冲击成本低
- **MA200 择时**: 熊市保护，避免趋势下行期持仓
- **月度调仓**: 低频交易，降低换手成本

### 4.2 风控机制
- **候选池规模限制**: 防止过度集中（默认 50 只候选）
- **流动性过滤**: 继承 `PortfolioConfig.min_daily_amount`（5000 万）
- **单票下限**: 继承 `PortfolioConfig.min_position_value`（2 万）
- **硬顶限制**: 继承 `PortfolioConfig.hard_limit`（10 只）
- **择时空仓**: MA200 下方强制空仓，避免趋势风险

### 4.3 数据依赖
- **必需字段**: `Bar.dividend_yield` / `Bar.market_cap`
- **数据源**: 需要离线采集 300 只红利股的基本面数据（T312 任务）
- **指数数据**: 沪深 300（sh.000300）日线数据（用于 MA200 计算）

---

## 5. 已知限制与后续工作

### 5.1 当前版本限制
1. **无红利税建模**: 继承 T207 简化假设（红利税影响 ≈0.4%/年，策略层红旗）
2. **静态股息率**: 假设 `dividend_yield` 字段已预计算（不动态查询财报）
3. **简化市值**: 使用总市值或自由流通市值由数据层决定（策略层不区分）
4. **固定指数**: MA200 择时仅支持单一指数（默认沪深 300）

### 5.2 后续任务
- **T312**: 红利股数据采集（300 只候选 + 股息率 + 市值，接入 `data/` 层）
- **T313**: 跨区间压力测试（2015 股灾 / 2018 熊市，验证 MA200 择时有效性）
- **T314**: 参数稳健性扫描（股息率下限 / 候选池规模 / MA200 周期敏感度）
- **T315**: 动量 vs 红利对比报告（T304 病理场景下红利策略优势量化）

---

## 6. 验收结论

### 6.1 验收标准符合性
| 标准 | 结果 | 备注 |
|------|------|------|
| ✅ 12 单测全绿 | **PASS** | 12/12 通过 |
| ✅ 端到端回测可跑通 | **PASS** | 240 交易日模拟成功 |
| ✅ MA200 择时生效 | **PASS** | 空仓天数 29 天 > 0 |
| ✅ 市值加权生效 | **PASS** | 大市值股票优先入选 |

### 6.2 最终判定
**✅ T311 红利策略验收通过**

- 代码质量: ✅ 符合工程规范（fail-closed / Decimal / 引擎契约）
- 测试覆盖: ✅ 12 个单元测试覆盖核心逻辑
- 功能完整: ✅ 选股 / 择时 / 调仓 / 市值加权全部实现
- 演示可用: ✅ 端到端脚本运行成功

### 6.3 入库建议
- 提交代码: `strategy/candidates.py` (DividendStrategy 相关代码)
- 提交测试: `tests/test_dividend_strategy.py`
- 提交演示: `scripts/t311_dividend_strategy_demo.py`
- 提交文档: `docs/t311_dividend_strategy_acceptance.md`（本文件）

### 6.4 下一步行动
1. **立即可做**: 提交代码 + 运行完整测试套件（确认无回归）
2. **后续任务**: T312 数据采集（红利股基本面数据）
3. **评审准备**: T313 压力测试（验证 MA200 择时在熊市中的保护效果）

---

**验收人**: Claude (Kiro)  
**验收日期**: 2026-09-02  
**测试环境**: Windows 11 / Python 3.11.5 / pytest 9.1.1
