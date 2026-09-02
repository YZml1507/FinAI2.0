# T312 红利股数据采集 + 红利策略回测 — 执行就绪

**状态**: ✅ 代码已完成并提交，立即可执行  
**完成日期**: 2026-09-02  
**测试状态**: 6/6 测试就绪（SKIP 符合预期，等待数据采集）  
**总测试数**: 524 tests collected  

---

## 立即执行（三步走）

### 步骤 1：采集 300+ 只红利股数据（≈30-40 分钟）

```bash
py -3.11 scripts/collect_dividend_stocks.py
```

**产出**：
- `data/dividend_stocks/{symbol}/{year}.parquet`（300+ 只 × 10 年）
- `data/dividend_stocks/meta.json`（采集元数据）

### 步骤 2：运行 2015-2024 全周期回测（≈5-10 分钟）

```bash
py -3.11 scripts/run_dividend_backtest.py
```

**产出**：
- PerformanceReport（CAGR / 夏普 / MDD / 换手 / 胜率）
- 红利税统计（验证 T309 集成）
- `experiments/` registry 记录

### 步骤 3：验证测试通过

```bash
py -3.11 -m pytest tests/test_t312_dividend_backtest.py -p no:ddtrace -v
```

**预期**: 6/6 PASS（数据采集后）

---

## G4.5 门禁判据（必达 3 条）

| 指标 | 目标 | 对比基准（Momentum） |
|---|---|---|
| ✅ **胜率** | ≥35% | 0%（T304） |
| ✅ **最大回撤** | <35% | 68% (crash) |
| ✅ **年化换手** | <400% | 1271% (crash) |

**加分项**（满足 ≥1 条）：
- 总收益 ≥−38%（vs −68% crash）
- 夏普 ≥−5（vs −13 crash）

---

## 预期结果（参考范围）

| 指标 | 预期范围 | 信心度 |
|---|---|---|
| CAGR | 5-8% | 中 |
| 夏普比率 | 0.5-0.8 | 中 |
| 最大回撤 | 25-35% | 高 |
| 年化换手 | 200-400% | 高 |
| 胜率 | 35-50% | 中 |
| 红利税占比 | 10-20% | 高 |

---

## 技术实现摘要

### 已完成文件

| 文件 | 功能 | 状态 |
|---|---|---|
| `scripts/collect_dividend_stocks.py` | 数据采集器（300+ 只红利股） | ✅ 已提交 |
| `scripts/run_dividend_backtest.py` | 2015-2024 全周期回测 | ✅ 已提交 |
| `scripts/run_dividend_stress.py` | T313 压力测试脚本 | ✅ 已提交 |
| `tests/test_t312_dividend_backtest.py` | 6 个测试用例 | ✅ 已提交 |
| `tests/test_t313_stress.py` | 压力测试用例 | ✅ 已提交（9c68df1） |
| `strategy/candidates.py` | DividendStrategy | ✅ 已有（T311） |
| `backtest/dividend_tax.py` | 红利税模块 | ✅ 已有（T309） |

### 关键特性

✅ **数据采集**：
- 复用 `data/collector.py` 基础设施（R1/R4 红线合规）
- Parquet 分区存储 + SHA-256 幂等校验
- 扩展字段：dividend_yield / market_cap / div_per_share
- 限速保护：4 秒/只，CircuitBreaker 熔断

✅ **回测引擎**：
- 复用 T201-T207 引擎（G3 门禁已通过）
- 集成 T309 红利税（持股期分段税率）
- 月度调仓 + MA200 择时保护
- 市值加权 + 等权起点

✅ **测试覆盖**：
- 数据完整性（≥300 只 + 3000 分区）
- 字段校验（dividend_yield / market_cap 非空）
- 端到端回测（不崩溃 + report 字段齐全）

---

## 红线合规确认

| 红线 | 状态 | 证据 |
|---|---|---|
| 凭据保护 | ✅ | 只用 `baostock.login()`，无密钥字面量 |
| 母库只读 | ✅ | 未修改 `finai/sources/*` |
| R4 复权口径 | ✅ | 经 `adjustment_mode.py::to_kwargs()` |
| R1 停牌脏行 | ✅ | 复用 `tradestatus=='1'` 过滤 |
| 限速保护 | ✅ | RateLimiter（4 秒/只） |
| Decimal 金额 | ✅ | 全程 Decimal，无 float |

---

## 依赖任务（已完成）

| 任务 | 状态 | Commit |
|---|---|---|
| T309 红利税 | ✅ | 已集成 `backtest/fees.py` |
| T311 红利策略 | ✅ | `strategy/candidates.py` |
| T313 压力测试 | ✅ | 9c68df1 |
| T201-T207 回测引擎 | ✅ | Phase 2 清零 |
| T301-T304 组合/策略 | ✅ | Phase 3 前置 |

---

## 风险提示

⚠️ **数据风险**：
- baostock 历史分红数据可能不完整（2015 年前）
- 股息率计算依赖年报公告日（存在轻微前视偏差）

⚠️ **策略风险**：
- 红利股在牛市中可能跑输成长股
- MA200 择时可能误判（震荡市频繁进出）

⚠️ **执行风险**：
- 采集耗时 ≈30-40 分钟（网络限速）
- 磁盘占用 ≈500 MB（Parquet 压缩后）

---

## 下一步决策（三选一）

### ✅ 选项 A：立即执行 T312（推荐）

**耗时**: ≈40-50 分钟  
**理由**: MomentumStrategy 已证明结构性失效（T304），红利策略有明显优势

**执行**:
```bash
py -3.11 scripts/collect_dividend_stocks.py
py -3.11 scripts/run_dividend_backtest.py
py -3.11 -m pytest tests/test_t312_dividend_backtest.py -p no:ddtrace -v
```

### ⏳ 选项 B：先补全动量策略完整数据

**耗时**: ≈3 小时  
**理由**: T304 仅测试 crash/bear，需要完整 10 年数据才能最终判断

**风险**: 动量策略结构性缺陷明显（病理分析），大概率仍不通过

### ⏸ 选项 C：停在 G4，等待指示

**适用**: 需要更多时间评估策略方向，或有其他优先级更高的任务

---

## 执行后验收清单

执行步骤 1-3 后，检查：

- [ ] `data/dividend_stocks/meta.json` 显示 ≥300 只股票
- [ ] 6/6 测试 PASS（不再 SKIP）
- [ ] PerformanceReport 字段完整（CAGR/Sharpe/MDD/换手/胜率）
- [ ] 红利税字段非零（证明 T309 生效）
- [ ] G4.5 门禁 3 条必达指标全部满足

**若通过 G4.5** → 进入 Phase 4 模拟盘  
**若不通过** → 生成技术评审报告 + 提交用户决策

---

**准备完成，等待执行指令。**

推荐执行：**选项 A**（立即运行 T312，≈40-50 分钟验证红利策略）
