# T201 事件驱动引擎核心 — 实现契约

> 依据：research-finai spec SDD-1~3 + plan P-2 + 13 号 5 必挂用例 + data 层接口侦察。
> 本文件是 T201 全部子模块的统一契约，不随实现调整；违约即返工。

## 0. 设计红线

1. **全事件驱动单引擎**（SDD-3）：不做向量化双轨。
2. **四环境同构**（SDD-1）：账本 + 订单状态机 + 事件契约只写一份；差异收敛到 Broker 接口。
3. **七态状态机**（SDD-2）：`PENDING_SUBMIT → SUBMITTED → (PARTIALLY_FILLED)* → FILLED / CANCELLED / EXPIRED / REJECTED`。
4. **双账本**（SDD-2）：append-only 流水（Decimal 字符串存储、tx_hash 幂等键）+ 可重算视图。
5. **幂等键**：trade_id / client_order_id。
6. ⛔ 禁止 `from data import ...` 裸包导入，必须 `from data.collector import ...` 全路径（data/__init__.py 为空）。
7. ⛔ `finai/sources/` 只读不改。
8. 测试命令：`py -3.11 -m pytest tests/ -p no:ddtrace -p no:ddtrace.pytest_bdd -p no:ddtrace.pytest_benchmark -q`

## 1. 模块结构

```
backtest/
├── __init__.py            # 空（与 data/ 一致，不做包级重导）
├── T201_design.md         # 本文件
├── constants.py           # 枚举：OrderSide / OrderType / OrderStatus / FeeItem
├── types.py               # dataclass：Bar / Order / Trade / Position / PortfolioView
├── order_fsm.py           # 七态状态机（合法迁移表 + transition()）
├── ledger.py             # Journal（append-only）+ BookView（推导视图）
├── feed.py                # DataFeed 协议 + ParquetDailyFeed 实现
├── matching.py            # 撮合引擎（A 股语义：涨跌停/T+1/整手/停牌）
├── broker.py              # Broker 协议 + BacktestBroker
├── settle.py              # settle_day（四环境共用）
└── engine.py              # BacktestEngine（事件循环编排）
```

## 2. constants.py

```python
class OrderSide(Enum): BUY / SELL
class OrderType(Enum): MARKET / LIMIT
class OrderStatus(Enum):
    PENDING_SUBMIT / SUBMITTED / PARTIALLY_FILLED
    / FILLED / CANCELLED / EXPIRED / REJECTED
class FeeItem(Enum): COMMISSION / STAMP_TAX / TRANSFER_FEE / HANDLING_FEE / MANAGEMENT_FEE / SLIPPAGE
```

## 3. types.py

```python
@dataclass(frozen=True)
class Bar:
    date: date
    symbol: str          # "sh.600000"
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    preclose: Decimal
    volume: Decimal       # 成交量
    amount: Decimal       # 成交额
    # 清洗派生（由 feed 调 cleaner 后注入，非 parquet 原生列）
    limit_up: bool = False
    limit_down: bool = False
    exdiv: bool = False
    # 元数据
    is_st: bool = False
    adjust_mode: str = "hfq"

@dataclass
class Order:
    client_order_id: str         # 幂等键
    symbol: str
    side: OrderSide
    order_type: OrderType
    volume: int                   # 正整数；买入须 100 的倍数
    price: Decimal | None         # MARKET 单为 None
    status: OrderStatus = OrderStatus.PENDING_SUBMIT
    created_date: date            # 下单日期
    filled_volume: int = 0
    avg_fill_price: Decimal = Decimal("0")
    fills: list[Trade] = field(default_factory=list)
    reject_reason: str = ""       # REJECTED 时填充

@dataclass(frozen=True)
class Trade:
    """一笔成交（幂等键 trade_id）"""
    trade_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    volume: int
    price: Decimal
    date: date
    fees: dict[FeeItem, Decimal]   # 费用明细，键必须有 COMMISSION/STAMP_TAX/TRANSFER_FEE/HANDLING_FEE
    # T+1：卖出可用日 = date + 下一个交易日（由 settle 计算写入）
    sellable_date: date | None = None

@dataclass
class Position:
    symbol: str
    volume: int = 0               # 总持仓
    sellable: int = 0             # 可卖（非当日买入）
    avg_cost: Decimal = Decimal("0")
    last_close: Decimal = Decimal("0")
    market_value: Decimal = Decimal("0")

@dataclass
class PortfolioView:
    """可重算视图 — 从 Journal 推导，不落盘"""
    cash: Decimal = Decimal("0")
    frozen_cash: Decimal = Decimal("0")
    positions: dict[str, Position] = field(default_factory=dict)
    nav: Decimal = Decimal("0")
    date: date
```

## 4. order_fsm.py 状态机

```
PENDING_SUBMIT → SUBMITTED          # broker 接收
PENDING_SUBMIT → REJECTED           # broker 校验拒绝
SUBMITTED    → PARTIALLY_FILLED     # 部分成交
SUBMITTED    → FILLED               # 全部成交
SUBMITTED    → CANCELLED            # 撤单成功
SUBMITTED    → EXPIRED              # 日终未成交自动过期
SUBMITTED    → REJECTED             # 撮合阶段发现违规（如停牌/涨跌停）
PARTIALLY_FILLED → FILLED           # 补齐
PARTIALLY_FILLED → CANCELLED        # 撤剩余
PARTIALLY_FILLED → EXPIRED          # 日终剩余过期
```

非法迁移 raise `OrderStateError`（继承 ValueError）。

## 5. ledger.py 双账本

### Journal（append-only 流水）

每条 entry：
```python
@dataclass(frozen=True)
class JournalEntry:
    tx_hash: str           # SHA-256(sorted canonical repr)，幂等重放键
    date: date
    entry_type: JournalType   # TRADE / FEE / DIVIDEND / EXDIV_ADJUST / CASH_IN / SETTLE
    symbol: str              # 非证券类条目可为 ""
    side: OrderSide | None
    volume: int
    price: Decimal
    amount: Decimal           # 发生了多少金额变动（正=流入现金，负=流出）
    fees: dict[FeeItem, Decimal]
    ref_id: str              # trade_id 或 client_order_id
    meta: dict               # 扩展字段
```

- 所有金额字段 Decimal，存储为字符串（__dict__ 里转 str）。
- `append(entry)` → 校验 tx_hash 唯一；重复 → 忽略（幂等重放安全）。
- `replay()` → 从头重放构建 BookView（验证性）。

### BookView（推导视图，不落盘）

- `positions: dict[str, Position]`
- `cash: Decimal`（可用资金）
- `frozen_cash: Decimal`（挂单冻结）
- `total_nav: Decimal`（= cash + Σ market_value）
- 方法：`process_trade(trade)` — 买：扣现金、加持仓、冻结手续费；卖：加现金、减持仓、扣费用
- 方法：`process_exdiv(symbol, factor, cash_dividend)` — 除权日调整：股数 = volume × factor；现金 += cash_dividend × old_volume
- 方法：`settle(date, bars)` — 日终结算：刷新 market_value = volume × close；重新计算 NAV
- 方法：`advance_sellable(trade_date)` — T+1 推进：把 sellable_date == today 的持仓转可卖

## 6. feed.py 数据源

```python
class DataFeed(Protocol):
    def get_bars(self, symbols: list[str], date: date) -> dict[str, Bar]: ...
    def get_trading_dates(self, start: date, end: date) -> list[date]: ...
    def current_date(self) -> date: ...

class ParquetDailyFeed:
    """从 data/daily_bars/{symbol}/{year}.parquet 读数据"""
    def __init__(self, root: Path = Path("data/daily_bars"),
                 trade_calendar=None,      # 可注入交易日历 fn: (start,end)->list[date]
                 limit_config: LimitFlagsConfig | None = None)
```

**注意**：Parquet 里**没有** limit_up/limit_down/exdiv 列 —— 读入后须调 `data.cleaner.mark_limit_flags` + `combine_exdiv_flag` 补齐。若 parquet 中无 preclose（不可能但有防御），该根 Bar limit_up/limit_down = False。trade_calendar 未注入时 get_trading_dates 须 raise（不静默打网）。

## 7. matching.py 撮合引擎

```python
class MatchResult(Enum): FILLED / PARTIAL / NO_FILL / REJECTED

@dataclass
class MatchContext:
    bar: Bar
    order: Order
    sellable: int          # 该 symbol 当前可卖数量
    cash_available: Decimal

class MatchEngine:
    """纯函数撮合，无状态。每次只吃一根 bar + 一笔订单。"""

    def match(self, ctx: MatchContext) -> tuple[MatchResult, Trade | None, str]:
```

**撮合规则（A 股语义，按序检查）**：

| # | 检查 | 失败 → |
|---|---|---|
| 1 | bar 存在（None = 停牌 = 无行情） | REJECTED("停牌不可下单") |
| 2 | BUY + limit_up → REJECTED("涨停买入不可成交") | REJECTED |
| 3 | SELL + limit_down → REJECTED("跌停卖出不可成交") | REJECTED |
| 4 | SELL && volume > sellable → REJECTED("T+1 可卖不足") | REJECTED |
| 5 | BUY && volume % 100 != 0 → REJECTED("买入须整手 100") | REJECTED |
| 6 | SELL && volume < 100 && volume != position.volume → REJECTED("零股卖出须清仓") | REJECTED |
| 7 | BUY: 需款 = price×volume + 预估费用 > cash_available → REJECTED("资金不足") | REJECTED |
| 8 | 全部通过 → 成交：价 = bar.open（T204 可配置；默认次一开盘，FR-BT-6） | FILLED |

⛔ 不做部分成交（PARTIALLY_FILLED 状态留给大盘冲击模型，v1 全有或全无）。

## 8. broker.py

```python
class Broker(Protocol):
    def submit(self, order: Order) -> Order: ...
    def cancel(self, client_order_id: str) -> Order | None: ...
    def on_bars(self, date: date, bars: dict[str, Bar]) -> None: ...
    def settle(self, date: date, exdiv_events: dict[str, ExdivEvent]) -> None: ...
    @property  def book(self) -> BookView: ...

class BacktestBroker:
    def __init__(self, matcher: MatchEngine, ledger: Ledger, feed: DataFeed)
```

- `submit()` → FSM.transition(PENDING_SUBMIT→SUBMITTED)；check 规则 5（整手）可提前拒。
- `on_bars()` → 遍历 SUBMITTED 订单 → match() → FILLED → ledger.process_trade()；REJECTED → FSM 迁移。
- `settle()` → EXPIRED 未成交 DAY 单 → 红利/除权 → advance_sellable。

## 9. settle.py

```python
def settle_day(book: BookView, date: date, bars: dict[str, Bar],
               exdiv_events: dict[str, ExdivEvent]) -> set[str]  # 返回受影响 symbol 集合
```

- 对每个持仓标的：bar 存在 → 刷新 last_close + market_value；bar 缺失（停牌）→ 市值冻结（沿用 last_close）。
- exdiv_events 中有事件 → 调用 book.process_exdiv()。
- 日终 NAV = cash + Σ market_value。

## 10. engine.py

```python
class BacktestEngine:
    def __init__(self, broker: Broker, feed: DataFeed, calendar=None)
    def run(self, strategy, start: date, end: date) -> BacktestResult

class BacktestResult:
    nav_curve: pd.Series          # 日频净值曲线
    orders: list[Order]           # 全部订单
    trades: list[Trade]           # 全部成交
    journal: list[JournalEntry]   # 完整流水
    final_book: BookView
```

事件循环（每个交易日）：
1. `feeds` 取当日 bars
2. `broker.on_bars(date, bars)` — 撮合所有待成交订单
3. `strategy.on_bar(date, bars, broker.book)` — 策略产生新订单
4. `broker.settle(date)` — 日终结算
5. 记录当日 NAV

**先撮合后信号**（13 号 L370：先撮合已有委托再调 on_bar → 不存在当日信号当日成交）。

## 11. T202 五必挂用例验收锚点

| # | IEEE | 引擎行为锚点 |
|---|---|---|
| 1 | 涨停买入 | matching rule#2 → REJECTED；成交价 ≤ limit_price |
| 2 | 跌停卖出 | matching rule#3 → REJECTED；净值含未卖出持仓 |
| 3 | 停牌日下单 | feed 返回 None bar → REJECTED；settle 市值冻结；NAV 停牌3日水平线 |
| 4 | 除权日持仓 | settle.exdiv → volume×factor + cash += dividend；NAV 曲线无跳变 |
| 5 | T+1 当日买卖 | matching rule#4 sellable 校验 → REJECTED |

## 12. 非目标（YAGNI，SDD-4）

- 事件总线中间件、ORM、MLflow、分布式、多线程撮合
- 做空/负持仓（SDD-7：A 股 v1 long-only）
- 部分成交模型（PARTIALLY_FILLED 状态机留口但撮合逻辑全有或全无）
