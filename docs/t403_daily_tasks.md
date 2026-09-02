# T403 日终任务：对账/净值/报告

## 概述

T403 实现模拟盘日终自动化任务，包括对账、净值计算、绩效报告生成。复用 Phase 2 回测引擎的全部抽象（SDD-1 四环境同构）。

## 架构设计

### 四环境同构（SDD-1）

```
┌─────────────────────────────────────────────────────────────┐
│                  四环境共用抽象                                │
├─────────────────────────────────────────────────────────────┤
│  · 撮合规则（matching.py）—— 8 条规则完全一致                  │
│  · 账本（ledger.py）—— 双账本 + 幂等键                         │
│  · 状态机（order_fsm.py）—— 七态迁移表                         │
│  · 结算（settle.py）—— 刷市值 + NAV                            │
│  · 指标（metrics.py）—— 绩效计算                               │
└─────────────────────────────────────────────────────────────┘
                             ↓
┌──────────────┬──────────────┬──────────────┬──────────────┐
│   Research   │   Backtest   │  Paper Trade │  Live Trade  │
│  (历史数据)   │  (事件驱动)   │  (模拟盘)     │  (实盘)       │
└──────────────┴──────────────┴──────────────┴──────────────┘
    向量化       Bar 级撮合      影子撮合       券商 API
```

### 模块职责

| 模块 | 文件 | 职责 |
|---|---|---|
| **对账** | `reconciliation.py` | 账本自对账（持仓市值 + 现金 = NAV）+ 不变式检查 + 差异检测 |
| **净值** | `nav.py` | 净值计算（复用 `settle_day`）+ 时间序列持久化（Parquet） |
| **报告** | `reporting.py` | 复用 T205 `compute_metrics` → JSON/HTML 双份输出 |
| **编排** | `daily_tasks.py` | 10 步日终任务编排 + 告警 + 幂等重跑 |
| **触发** | `scripts/run_daily_tasks.py` | 手动触发脚本（接受命令行参数） |

## 执行顺序（⛔ 不可颠倒）

```
1. 数据更新           T109 增量更新 → 确保盘后数据齐备
2. 策略信号生成        strategy.generate_signals(date)
3. 订单提交           PaperBroker.submit_orders() → _pending 队列
4. 撮合执行           次日开盘，复用 T201 撮合引擎
5. 除权处理           ledger.process_exdiv()（必须先于结算）
6. 结算              settle_day() → 刷市值 + NAV
7. 对账              reconcile_account() → 验证账本一致性
8. 净值记录           append_nav() → Parquet 幂等写入
9. 报告生成           每周一/每月 1 日生成 JSON + HTML
10. 告警             对账失败/NAV 异常下跌 → 飞书 webhook
```

## 关键技术点

### 1. 对账逻辑（FR-ACC-2）

**v1 简化路径**：只做账本自对账（模拟盘无券商回报）。

**检查项**：
- **不变式**（致命，直接 raise）：
  - 现金 ≥ 0（爆仓检测）
  - 冻结资金 ≥ 0 且 ≤ 现金
  - 持仓数量 ≥ 0（v1 不支持空头）
- **NAV 一致性**：持仓市值 + 现金 = NAV（差异 > 0.01 元 raise）
- **单调性**（非致命告警）：NAV 下跌 > 5% 且非分红日 → 告警

**停牌处理**：bars 中无对应标的 → 使用账本 `last_close` 计算市值（市值冻结）。

**代码示例**：
```python
from paper_trading.reconciliation import reconcile_account

report = reconcile_account(book, trade_date, bars, prev_nav=prev_nav)

if not report.ok:
    raise ReconciliationError(report.discrepancies)

if report.warnings:
    send_alert(report.warnings)  # NAV 异常下跌告警
```

### 2. 净值存储（FR-ACC-3）

**格式**：Parquet 时间序列（`date[date], nav[Decimal as str], cash, market_value`）。

**幂等性**：
```python
# 同日期重跑 → 覆盖旧值（keep='last'）
append_nav(nav_file, [record], idempotent=True)

# 算法：
# 1. 读已有数据
# 2. 合并新记录（concat）
# 3. 按日期去重（keep='last'）
# 4. 按日期排序
# 5. 原子写（.tmp → os.replace）
```

**读取**：
```python
from paper_trading.nav import load_nav_series

series = load_nav_series(nav_file)
# → [(date, Decimal), ...] 按日期升序
```

### 3. 报告生成（FR-REP-1）

**频率**：
- **每日**：更新 NAV 时间序列
- **每周一 / 每月 1 日**：生成完整 JSON + HTML 报告

**复用 T205**：
```python
from backtest.metrics import compute_metrics
from paper_trading.reporting import generate_reports

# 构造伪 BacktestResult（鸭子类型）
result = _build_backtest_result(book, nav_series)

# 生成双份报告
json_path, html_path = generate_reports(
    result,
    report_dir,
    "20260902",
    risk_free_annual=Decimal("0.02"),
)
```

**HTML 审美**（design_sense）：
- 深色调背景（slate #0B0E14 / #0F172A）
- Inter 字体 + 等宽数字（`font-variant-numeric: tabular-nums`）
- 数据密度优化（网格布局 + 表格）

### 4. 日终任务编排

**依赖注入**：
```python
from paper_trading.daily_tasks import DailyTaskRunner

runner = DailyTaskRunner(
    updater=IncrementalUpdater(...),
    broker=PaperBroker(...),
    strategy=DividendStrategy(...),
    alert_fn=send_feishu_alert,
    nav_file=Path("paper_trading/data/nav_series.parquet"),
    report_dir=Path("paper_trading/reports"),
    risk_free_annual=Decimal("0.02"),
)

report = runner.run_daily_tasks(trade_date, symbols=None)
```

**错误恢复**：
- 对账失败 → 立即终止，不执行后续任务（fail-closed）
- 数据更新失败 > 50% → raise RuntimeError
- 任何异常 → 发送飞书告警 + 记录 `report.error`

**幂等性**：
- 同日期重跑 → NAV 覆盖旧值（`append_nav idempotent=True`）
- 数据更新走 T109 增量逻辑（水位检查）

## 使用指南

### 手动触发

```bash
# 基本用法
py -3.11 scripts/run_daily_tasks.py --date 2026-09-02

# 强制重新采集数据
py -3.11 scripts/run_daily_tasks.py --date 2026-09-02 --force-update

# 指定股票池
py -3.11 scripts/run_daily_tasks.py --date 2026-09-02 --symbols sz.000001,sz.000002

# 自定义数据根目录
py -3.11 scripts/run_daily_tasks.py --date 2026-09-02 --root paper_trading/data
```

### Windows 任务计划程序调度

**创建任务**（每日 17:00 触发）：
```powershell
$action = New-ScheduledTaskAction `
    -Execute "py" `
    -Argument "-3.11 scripts/run_daily_tasks.py --date $(Get-Date -Format yyyy-MM-dd)" `
    -WorkingDirectory "D:\Projects\FinAI2.0"

$trigger = New-ScheduledTaskTrigger -Daily -At 17:00

Register-ScheduledTask `
    -TaskName "FinAI_DailyTasks" `
    -Action $action `
    -Trigger $trigger `
    -Description "模拟盘日终任务：对账/净值/报告"
```

### 环境变量

| 变量 | 用途 | 默认值 |
|---|---|---|
| `PAPER_TRADING_ROOT` | 模拟盘数据根目录 | `paper_trading/data` |
| `FEISHU_WEBHOOK_URL` | 飞书告警 webhook（可选） | 无 |

### 告警配置

飞书 webhook 配置（复用 T001）：
```bash
# Windows 系统环境变量
setx FEISHU_WEBHOOK_URL "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"

# 或在脚本中直接设置
$env:FEISHU_WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
```

## 测试验收

### 测试覆盖

| 测试文件 | 覆盖模块 | 测试数量 |
|---|---|---|
| `test_reconciliation.py` | 对账逻辑 | 12 |
| `test_nav.py` | 净值计算与存储 | 11 |
| `test_reporting.py` | 报告生成 | 10 |
| `test_daily_tasks.py` | 日终任务编排 | 7 |
| **合计** |  | **40** |

### 运行测试

```bash
# 全部 T403 测试
py -3.11 -m pytest tests/test_reconciliation.py tests/test_nav.py tests/test_reporting.py tests/test_daily_tasks.py -v

# 快速验证（禁用 ddtrace）
py -3.11 -m pytest tests/test_reconciliation.py -p no:ddtrace -p no:ddtrace.pytest_bdd

# 单个测试类
py -3.11 -m pytest tests/test_reconciliation.py::TestReconciliationBasic -v
```

### 验收判据

- [ ] 对账逻辑测试 12 个全绿（不变式 + 差异容差 + 停牌处理）
- [ ] 净值计算测试 11 个全绿（幂等写入 + Decimal 精度保留）
- [ ] 报告生成测试 10 个全绿（JSON + HTML + design_sense）
- [ ] 日终任务测试 7 个全绿（编排 + 错误恢复 + 告警）
- [ ] 手动触发脚本可执行（--help 输出正确）
- [ ] 本文档说明任务流程与使用

## 已知限制

1. **PaperBroker / Strategy 未实现**：
   - `run_daily_tasks.py` 当前返回占位错误
   - 待 T401/T402 完成后取消注释

2. **券商对账未实现**（v1 范围外）：
   - 当前只做账本自对账
   - 实盘时需扩展：券商三报告（成交/持仓/资金）→ 匹配 → 差异进人工确认

3. **行情加载占位**：
   - `_load_bars` 当前返回空字典
   - 待 Feed 层集成后实现

4. **除权事件占位**：
   - `_get_exdiv_events` 当前返回空字典
   - 待数据层除权表集成后实现

## 修订记录

| 日期 | 内容 |
|---|---|
| 2026-09-02 | 初版：T403 技术设计 + 实现文档 + 使用指南 |
