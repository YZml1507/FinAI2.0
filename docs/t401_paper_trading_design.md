# T401 模拟盘执行器设计文档

> **任务**: T401 模拟盘执行器（Paper Trading Executor）
> **依据**: spec SDD-1（四环境同构）+ T201 引擎契约
> **完成日期**: 2026-09-02

---

## 0. 设计目标

实现模拟盘执行器，复用 Phase 2 回测引擎的全部抽象（SDD-1 四环境同构），从信号生成到模拟成交的完整链路。

**核心约束**:
- **同构原则**: 回测与模拟盘行为必须一致（SDD-1），唯一差异=数据源
- **T+1 约束**: 今日信号 → 明日开盘成交（与回测完全一致）
- **撮合规则复用**: 8 条规则（停牌/涨停/跌停/T+1/整手/零股/资金/成交价）完全继承 `backtest/matching.py`
- **账本复用**: 双账本 Journal（append-only）+ BookView（推导视图）完全继承 `backtest/ledger.py`

---

## 1. 架构设计

### 1.1 模块结构

```
paper_trading/
├── __init__.py         # 包说明（SDD-1 同构原则）
├── config.py           # PaperTradingConfig（初始资金/路径/策略参数）
├── state.py            # PaperTradingState（持仓/资金/订单状态持久化）
├── broker.py           # PaperBroker（继承 BacktestBroker，零重写撮合）
└── runner.py           # PaperTradingRunner（日终任务编排）
```

### 1.2 依赖关系

```
PaperTradingRunner
    ├── PaperTradingConfig     （配置）
    ├── PaperBroker            （经纪商）
    │   ├── MatchEngine        （撮合引擎，复用 backtest.matching）
    │   ├── Ledger             （账本，复用 backtest.ledger）
    │   └── ParquetDailyFeed   （行情源，复用 backtest.feed）
    ├── PaperTradingState      （状态持久化）
    └── IncrementalUpdater     （增量数据采集，复用 data.incremental）
```

### 1.3 与回测引擎的关系

| 组件 | 回测 | 模拟盘 | 说明 |
|---|---|---|---|
| **Broker** | `BacktestBroker` | `PaperBroker`（继承） | 模拟盘零重写撮合逻辑 |
| **撮合引擎** | `MatchEngine` | `MatchEngine`（同一份） | 8 条规则完全一致 |
| **账本** | `Ledger` | `Ledger`（同一份） | 双账本+幂等键 |
| **状态机** | `OrderStateMachine` | `OrderStateMachine`（同一份） | 七态迁移表 |
| **数据源** | `ParquetDailyFeed`（历史） | `ParquetDailyFeed`（盘后采集） | **唯一差异** |
| **执行模式** | 一次性跑完整个区间 | 每日增量执行+状态持久化 | 结构同构，时序不同 |

---

## 2. 核心组件设计

### 2.1 PaperTradingConfig（配置）

**职责**: 集中管理模拟盘运行参数

**字段**:
- `initial_capital: Decimal`  —— 初始资金（≥10000 RMB）
- `strategy_params: dict`     —— 策略配置（可序列化）
- `data_root: Path`           —— Parquet 数据根目录
- `state_path: Path`          —— 状态持久化路径（JSON）
- `dry_run: bool`             —— 干跑模式（v1=True，不真实下单）
- `max_orders_per_day: int`   —— 单日最大下单次数（防御失控策略）

**校验纪律** (fail-closed):
- 初始资金 ≥10000
- 路径存在且可写
- 策略配置可 JSON 序列化
- 风控参数 >0

### 2.2 PaperTradingState（状态持久化）

**职责**: 封装模拟盘运行状态，支持保存/加载/恢复

**字段**:
- `last_trading_date: str`     —— 最后执行日期（幂等保护）
- `cash / frozen_cash: str`    —— 现金（Decimal → str）
- `positions: dict`            —— 持仓快照 `{symbol: {volume, sellable, cost, ...}}`
- `pending_orders: list`       —— 活动委托（仅 SUBMITTED）
- `nav: str`                   —— 净值
- `state_hash: str`            —— 状态哈希（校验完整性）

**关键方法**:
- `save(path)` —— 原子写（.tmp → os.replace）
- `load(path)` —— 加载+哈希校验（损坏即 raise）
- `from_broker(broker, date)` —— 从 BacktestBroker 构建快照
- `restore_to_broker(broker)` —— 恢复到 Broker（持仓/现金/挂单）

**幂等保护**:
- `last_trading_date` 记录最后执行日期
- Runner 在执行前检查是否已处理过该日期

### 2.3 PaperBroker（模拟盘经纪商）

**职责**: 实现 `Broker` 协议，完全复用回测撮合逻辑

**设计**:
```python
class PaperBroker(BacktestBroker):
    """模拟盘经纪商（完全复用回测撮合，SDD-1 同构）。
    
    ⛔ 不重写任何撮合逻辑 —— 全部继承 BacktestBroker
    submit / cancel / on_bars / settle / _match_one 全部复用
    """
    def __init__(self, matcher, ledger, feed):
        super().__init__(matcher, ledger, feed)
    
    # 新增辅助方法（非撮合逻辑）
    def get_summary(self) -> dict:
        """返回账户摘要（现金/持仓/NAV/挂单数）"""
```

**关键点**:
- **零重写撮合**: 8 条规则（停牌/涨停/跌停/T+1/整手/零股/资金/成交价）全部继承
- **数据源注入**: v1 用 `ParquetDailyFeed`（盘后采集），未来可扩展实时源
- **状态恢复**: 通过 `PaperTradingState.restore_to_broker()` 恢复持仓/挂单

### 2.4 PaperTradingRunner（日终任务编排）

**职责**: 编排模拟盘日终任务流程

**日内时序** (与回测引擎对齐):
```
T 日盘后:
  ① 采集 T 日数据（增量更新）
  ② 策略看 T 日行情
  ③ 生成信号 → 下单（SUBMITTED）

T+1 日盘后:
  ④ 采集 T+1 日数据
  ⑤ 先撮合（T 日挂单用 T+1 开盘价成交）
  ⑥ 后信号（策略看 T+1 日行情，生成新委托）
  ⑦ 日终结算（过期/除权/市值/T+1推进）
  ⑧ 状态保存
```

**关键方法**:
```python
def run_daily(self, date: str | _date) -> DailyRunResult:
    """执行单日任务（幂等，可重复调用）"""
    # ① 幂等检查（已执行过该日期 → 跳过）
    # ② 状态加载/初始化（冷启动入金，热启动恢复）
    # ③ 数据更新（增量采集）
    # ④ 策略 watchlist（取数范围）
    # ⑤ 先撮合（昨日挂单用今日开盘价成交）
    # ⑥ 后信号（策略看今日行情，下单进 pending）
    # ⑦ 日终结算
    # ⑧ 状态保存
```

**幂等保护**:
- `_is_already_run(date)` —— 检查 `last_trading_date`
- 重复日期直接返回"跳过"结果

**风控**:
- `max_orders_per_day` —— 单日下单次数上限（防御失控策略）

---

## 3. v1 简化路径

**设计选择**: 盘后采集+次日回放（与回测同构）

**原因**:
1. **避免复杂度**: 不引入实时行情（WebSocket/消息队列）
2. **结构同构**: 数据源=历史 Parquet，与回测一致
3. **幂等保证**: 盘后采集天然幂等（同日重跑不重复采集）

**v1 不做**:
- 实时行情接入（WebSocket/消息队列）
- 实盘桥接（券商 API）
- 日内多次执行

**v2 扩展路径** (预留):
- 替换 `feed` = 实时行情源
- 替换 `matcher` = 券商 API 桥接
- 保留 `Ledger / OrderStateMachine` 不变（SDD-1 同构）

---

## 4. 与回测一致性验证

**目标**: 同数据同参数 → NAV 曲线误差 <0.01%

**验证方法**:
1. 准备测试数据（固定区间，如 2026-09-01 ~ 2026-09-05）
2. 回测模式：`BacktestEngine.run(strategy, "2026-09-01", "2026-09-05")`
3. 模拟盘模式：逐日调用 `PaperTradingRunner.run_daily("2026-09-01")` ... `run_daily("2026-09-05")`
4. 比对：
   - 每日 NAV 曲线
   - 成交记录（trade_id / price / volume / fees）
   - 持仓状态（volume / sellable / cost_basis）
   - 订单状态（FILLED / REJECTED 数量）

**测试用例**: `test_backtest_paper_parity`（在 `test_t401_paper_trading.py`）

---

## 5. 使用示例

### 5.1 首次运行（冷启动）

```python
from paper_trading.runner import PaperTradingRunner
from paper_trading.config import PaperTradingConfig
from strategy.candidates import MomentumStrategy
from decimal import Decimal
from pathlib import Path

# 配置
config = PaperTradingConfig(
    initial_capital=Decimal("100000"),
    data_root=Path("data/daily_bars"),
    state_path=Path("paper_trading/state.json"),
    dry_run=True,  # v1 模拟盘，不真实下单
)

# 策略
strategy = MomentumStrategy(lookback=20, rebalance_days=5)

# 执行器
runner = PaperTradingRunner(config, strategy)

# 首次运行（冷启动：创建新账本，入金 10 万）
result = runner.run_daily("2026-09-02")
print(f"NAV: {result.nav}, 持仓: {result.positions_count}, 新单: {result.orders_submitted}")
```

### 5.2 次日运行（热启动）

```python
# 次日运行（热启动：自动加载已有状态）
result = runner.run_daily("2026-09-03")
print(f"NAV: {result.nav}, 持仓: {result.positions_count}, 成交: {result.orders_filled}")
```

### 5.3 幂等重跑

```python
# 重复运行同一日期（幂等保护：跳过）
result = runner.run_daily("2026-09-03")
assert result.error == "幂等跳过"
```

---

## 6. 测试覆盖

**测试文件**: `tests/test_t401_paper_trading.py`

**测试用例** (≥10 个):

1. **配置测试** (`TestPaperTradingConfig`):
   - `test_default_config_valid` —— 默认配置通过校验
   - `test_initial_capital_below_minimum_raises` —— 初始资金低于下限 raise
   - `test_data_root_not_exist_raises` —— 数据目录不存在 raise
   - `test_strategy_params_serializable` —— 策略参数可序列化
   - `test_strategy_params_non_serializable_raises` —— 不可序列化参数 raise
   - `test_config_to_dict_and_back` —— 配置序列化往返一致

2. **状态测试** (`TestPaperTradingState`):
   - `test_state_save_and_load_roundtrip` —— 状态保存/加载往返一致
   - `test_state_hash_mismatch_raises` —— 状态被篡改（哈希不匹配）raise
   - `test_state_from_broker_snapshot` —— 从 Broker 构建快照
   - `test_state_restore_to_broker` —— 状态恢复到 Broker

3. **Broker 测试** (`TestPaperBroker`):
   - `test_paper_broker_inherits_backtest_broker` —— PaperBroker 是 BacktestBroker 子类
   - `test_paper_broker_submit_and_fill` —— 下单 → 撮合成交（复用回测逻辑）
   - `test_paper_broker_get_summary` —— 账户摘要返回正确

4. **Runner 测试** (`TestPaperTradingRunner`):
   - `test_runner_cold_start` —— 冷启动（首次运行，创建新账本）
   - `test_runner_idempotent_skip` —— 幂等保护（重复日期跳过）
   - `test_runner_hot_start_restores_state` —— 热启动（加载已有状态）
   - `test_runner_strategy_without_on_bar_raises` —— 策略缺 on_bar 方法 raise

5. **一致性测试** (`TestBacktestPaperParity`):
   - `test_same_data_same_result` —— 同数据同参数：回测 vs 模拟盘 NAV 一致

---

## 7. 交付物清单

| 文件 | 说明 | 状态 |
|---|---|---|
| `paper_trading/__init__.py` | 包说明（SDD-1 同构原则） | ✅ |
| `paper_trading/config.py` | PaperTradingConfig（配置） | ✅ |
| `paper_trading/state.py` | PaperTradingState（状态持久化） | ✅ |
| `paper_trading/broker.py` | PaperBroker（继承 BacktestBroker） | ✅ |
| `paper_trading/runner.py` | PaperTradingRunner（日终任务编排） | ✅ |
| `tests/test_t401_paper_trading.py` | 测试套件（≥10 单测） | ✅ |
| `docs/t401_paper_trading_design.md` | 本文档（架构与使用） | ✅ |

---

## 8. 后续任务

- **T402**: 回测-模拟偏差监控（deviation / tolerance / monitor）
- **T403**: 日终任务（对账/净值/报告）
- **T404**: 台账保鲜与到期提醒
- **T405**: 报备材料清单核对

---

## 9. 修订记录

| 日期 | 内容 |
|---|---|
| 2026-09-02 | v1.0 初稿：架构设计 + 核心组件 + 测试覆盖 |
