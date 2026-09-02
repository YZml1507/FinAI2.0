# Gap Slippage Model（缺口滑点模型）

> 生成：2026-09-02 ｜ 测试文件：`tests/test_gap_slippage.py`（26 例离线单测全绿）
> 实现位置：`backtest/fees.py::make_price_model`（新增参数 `gap_slippage_pct` / `gap_threshold_pct`）

---

## 1. 动机与背景

### 1.1 问题

**隔夜跳空缺口（±3% / ±5%）时，真实成交价会因流动性枯竭、挂单稀疏而额外偏离开盘价**。

现有 T204 成交模型仅建模**基础滑点**（默认 5bps，压测 15bps）——这是正常市况下、流动性充裕时的成本。但在以下场景，基础滑点会系统性低估真实成本：

- **利好公告后跳空高开 +3%**：散户追涨涌入，卖盘稀疏，买单实际成交价远高于开盘价。
- **利空公告后跳空低开 -5%**：恐慌盘抛售，买盘稀疏，卖单实际成交价远低于开盘价。
- **A 股特有的 T+1 制度**：隔夜跳空无法盘中对冲，必须次日开盘接受市价，流动性风险放大。

中低频日线策略（周调仓、月调仓）在这类极端日成交占比不高，但**单次拖累可达数十 bps**，累积影响年化收益 0.1–0.3%。

### 1.2 解决方案

在 `make_price_model` 新增**可选**缺口滑点参数：

- `gap_slippage_pct`（缺口滑点率）：如 `Decimal("0.0010")` = 10bps。`None` ⇒ 不启用（向后兼容）。
- `gap_threshold_pct`（缺口检测阈值）：默认 3%。当 `abs(open - preclose) / preclose >= 阈值` 时触发。

**叠加逻辑**：基础滑点 × 缺口滑点（复利，非线性叠加）——这反映「流动性枯竭会放大每一 bps 的成本」。

---

## 2. 模型定义

### 2.1 数学形式

设 `p0 = bar.preclose`（前收价），`p_open = bar.open`（开盘价），`side = BUY/SELL`。

**步骤 1：基础滑点**（T204 原有逻辑）

```
slipped = p_open × (1 + sign × base_slip)
其中 sign = +1（BUY 更贵）/ -1（SELL 更贱）
     base_slip = config.slippage_rate（默认 0.0005 = 5bps）
```

**步骤 2：缺口检测与额外滑点**（新增）

```
gap = abs(p_open - p0) / p0

if gap_slippage_pct is not None and gap >= gap_threshold_pct:
    slipped = slipped × (1 + sign × gap_slippage_pct)
```

**步骤 3：tick 取整 + 涨跌停限幅**（T204 原有逻辑）

```
price = round_to_tick(slipped, tick_size=0.01)
price = clamp(price, limit_down, limit_up)  # 可选
```

### 2.2 方向性

| 场景 | 开盘价变化 | BUY 方向 | SELL 方向 |
|---|---|---|---|
| 跳空高开 +3% | `open > preclose` | **更贵**（追涨成本 ↑） | 保守估计仍加滑点 |
| 跳空低开 -3% | `open < preclose` | 保守估计仍加滑点 | **更贱**（恐慌成本 ↑） |

**保守原则**：无论跳空方向，只要超阈值即触发额外滑点——因为流动性枯竭是**双向**的（挂单稀疏 ⇒ 买卖价差扩大）。

---

## 3. 参数建议

| 参数 | 默认值 | 压测档 | 说明 |
|---|---|---|---|
| `gap_slippage_pct` | `None`（不启用） | `Decimal("0.0010")`（10bps） | 缺口滑点率。中性建议 10bps；保守 20bps。 |
| `gap_threshold_pct` | `Decimal("0.03")`（3%） | `Decimal("0.05")`（5%） | 缺口阈值。A 股主板 ±3% 已属较大波动；创业板/科创板可放宽至 5%。 |

### 3.1 校准思路

缺口滑点**难以从公开数据直接校准**（需 Tick 级订单簿 + 冲击成本模型），建议采用**敏感度分析 + 历史极端日抽检**：

1. **回测历史跳空日**：抽取沪深 300 成分股近 3 年内跳空 ≥3% 的日期，统计当日成交加权均价（VWAP）与开盘价偏离。
2. **敏感度测试**：0bps / 10bps / 20bps 三档跑同一策略，观察年化收益变化。若 10bps ⇒ −0.2% 年化、20bps ⇒ −0.4%，取中性档。
3. **对比实盘**：若有模拟盘 / 小资金实盘，记录跳空日实际成交价与开盘价偏离，反推滑点率。

---

## 4. 验收结果（`test_gap_slippage.py`）

### 4.1 覆盖矩阵

| 测试类 | 用例数 | 目标 |
|---|---|---|
| `TestGapSlippageParamValidation` | 5 | fail-closed：类型 / 负值 / 超界全拒 |
| `TestBackwardCompatibility` | 2 | `gap_slippage_pct=None` ⇒ 恒等 T204 原逻辑 |
| `TestGapDetection` | 4 | 阈值逻辑：< 3% 不触发 / ≥ 3% 触发 / 自定义阈值 |
| `TestDirectionality` | 4 | 方向性：BUY/SELL × 跳空↑/↓ 四象限 |
| `TestStressScenarios` | 6 | 3% / 5% 缺口 × 10bps / 20bps 滑点 + 单调性 |
| `TestEdgeCasesAndInvariants` | 4 | 边界：preclose=0 / 无缺口 / 涨跌停限幅 / tick 红线 |
| `TestMultiDayGapStress` | 1 | 5 日连续跳空累积拖累 |

**全部 26 例离线单测绿**（`pytest tests/test_gap_slippage.py`）。

### 4.2 锚点用例（手算复核）

#### 用例 1：3% 跳空 + 10bps 缺口滑点（BUY）

```python
pm = make_price_model(gap_slippage_pct=Decimal("0.0010"))
bar = Bar(open=Decimal("10.30"), preclose=Decimal("10.00"), ...)  # +3.0%
order = Order(side=OrderSide.BUY, ...)

# 手算：
# 基础滑点：10.30 × 1.0005 = 10.30515
# 缺口滑点：10.30515 × 1.0010 = 10.31546
# tick 取整：→ 10.32
assert pm(order, bar) == Decimal("10.32")
```

#### 用例 2：5% 跳空 + 20bps 缺口滑点（SELL）

```python
pm = make_price_model(gap_slippage_pct=Decimal("0.0020"))
bar = Bar(open=Decimal("9.50"), preclose=Decimal("10.00"), ...)  # -5.0%
order = Order(side=OrderSide.SELL, ...)

# 手算：
# 基础滑点：9.50 × 0.9995 = 9.49525
# 缺口滑点：9.49525 × 0.9980 = 9.47626
# tick 取整：→ 9.48
assert pm(order, bar) == Decimal("9.48")
```

#### 用例 3：单调性（缺口滑点 0 < 10bps < 20bps ⇒ BUY 价格递增）

```python
bar = Bar(open=Decimal("10.30"), preclose=Decimal("10.00"), ...)
pm0 = make_price_model(gap_slippage_pct=None)           # 无缺口滑点
pm10 = make_price_model(gap_slippage_pct=Decimal("0.0010"))
pm20 = make_price_model(gap_slippage_pct=Decimal("0.0020"))

p0 = pm0(_order(OrderSide.BUY), bar)   # 10.31
p10 = pm10(_order(OrderSide.BUY), bar) # 10.32
p20 = pm20(_order(OrderSide.BUY), bar) # 10.33

assert p0 < p10 < p20  # 滑点档递增 ⇒ 成交价单调递增（BUY 更贵）
```

---

## 5. 接入方式

### 5.1 策略层接入

```python
from decimal import Decimal
from backtest.fees import default_fee_config, make_fee_model, make_price_model

# 默认口径（无缺口滑点，向后兼容 T204）
fee_model = make_fee_model(default_fee_config())
price_model = make_price_model()

# 启用缺口滑点（10bps，阈值 3%）
price_model_with_gap = make_price_model(
    gap_slippage_pct=Decimal("0.0010"),   # 10bps
    gap_threshold_pct=Decimal("0.03"),    # 3% 阈值
)

# 压测档（20bps，阈值 5%）
price_model_stress = make_price_model(
    gap_slippage_pct=Decimal("0.0020"),
    gap_threshold_pct=Decimal("0.05"),
)

# 注入引擎
from backtest.matching import MatchEngine

engine = MatchEngine(
    fee_model=fee_model,
    price_model=price_model_with_gap,  # 使用缺口滑点模型
)
```

### 5.2 回测对比

```python
# 场景：同一策略跑两档，对比年化收益差异
result_baseline = run_backtest(strategy, price_model=make_price_model())
result_gap10 = run_backtest(strategy, price_model=make_price_model(gap_slippage_pct=Decimal("0.0010")))

print(f"无缺口滑点：年化 {result_baseline.cagr_annual}")
print(f"缺口 10bps：年化 {result_gap10.cagr_annual}")
print(f"拖累：{result_baseline.cagr_annual - result_gap10.cagr_annual}")
```

---

## 6. 与 T204 / FR-BT-6 的关系

| 项 | T204 原有逻辑 | 缺口滑点扩展 |
|---|---|---|
| 成交价锚 | 次一开盘 `bar.open` | 不变 |
| 基础滑点 | 5bps（默认）/ 15bps（压测） | 不变，**叠加**基础 |
| 新增滑点 | 无 | **可选**缺口滑点（`gap_slippage_pct`），检测超阈值跳空时触发 |
| tick 取整 | 0.01 元 `ROUND_HALF_UP` | 不变 |
| 涨跌停限幅 | 可选 `limit_pct` | 不变（在缺口滑点**之后**生效） |
| 向后兼容 | — | `gap_slippage_pct=None` ⇒ 恒等原逻辑 |

**红线不破**：

- tick 红线：成交价恒为 0.01 的整数倍（`test_tick_size_always_respected`）。
- 限幅红线：滑点后成交价不越涨跌停（`test_limit_clamp_after_gap_slippage`）。
- 撮合顺序：规则 2/3 的一字板拒单在价格模型**之前**（封板不可成交语义独立）。

---

## 7. 敏感性与局限

### 7.1 模型假设

1. **缺口 ≥ 阈值 ⇒ 流动性枯竭是二元的**（触发/不触发）。真实情况可能是连续函数（缺口越大、滑点越高）。
2. **跳空方向不影响滑点率**（保守估计）。真实可能：追涨成本 > 杀跌成本（A 股散户行为不对称）。
3. **无个股差异**：大盘蓝筹 vs 小盘股的流动性差异未建模。
4. **v1 不区分板块**：主板 10% 涨跌停 vs 创业板/科创板 20% 涨跌停，跳空 3% 的相对剧烈程度不同。

### 7.2 适用场景

✅ **适用**：

- 中低频日线策略（周调仓、月调仓），跳空日占比 < 10%，累积拖累可量化。
- 压测极端市况（如 2015 股灾、2020 年 3 月熔断）。
- 高分红、低波动策略（对成本敏感）。

⚠ **慎用**：

- 高频 T+0 策略（盘中逐笔成交，跳空只占开盘一刻，缺口滑点模型过于粗糙）。
- 小盘股专打策略（流动性本就稀疏，5bps 基础滑点已低估，需独立校准）。

---

## 8. 后续优化方向（v2 留口）

1. **连续缺口函数**：`gap_slippage = f(gap)` 替代二元阈值，如 `max(0, (gap - 2%) × 5)`（超 2% 后每 1pp 加 5bps）。
2. **个股流动性分层**：按日均成交额分档（如 ≥10 亿 / 1–10 亿 / <1 亿），不同档位不同缺口滑点率。
3. **方向性系数**：追涨 × 1.5、杀跌 × 1.0（反映 A 股散户追涨杀跌的不对称性）。
4. **Tick 数据校准**（若有条件）：统计真实订单簿，拟合缺口 → 滑点的经验公式。

---

## 9. 变更纪律

修改缺口滑点模型须：

1. **修改 `backtest/fees.py::make_price_model`**（唯一实现点）。
2. **在 `tests/test_gap_slippage.py` 补测试**（新参数 / 新逻辑全覆盖）。
3. **更新本文档**：参数表 + 手算锚点 + 敏感度对比。
4. **登记 `docs/t204_price_model_sensitivity.md`**（若改变默认口径或引入与 T204 的交互）。

⛔ 不许在策略层 / 撮合层 / 账本层内联缺口逻辑 —— 全部走 `price_model` 注入点。

---

## 10. 参考文献

- `docs/t204_price_model_sensitivity.md` —— T204 成交模型显式声明与敏感度对比（九宫格）。
- `backtest/matching.py` —— 撮合规则顺序与 `price_model` 注入点（`PriceModelFn`）。
- `tests/test_t204_price_model.py` —— T204 原有逻辑的 18 例验收（基础滑点 / tick / 限幅）。
- `tests/test_gap_slippage.py` —— 缺口滑点 26 例验收（本文档全部锚点均可复现）。
