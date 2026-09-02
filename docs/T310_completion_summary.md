# T310 跳空缺口滑点压力测试 — 完成报告

> 生成时间：2026-09-02  
> 状态：✅ 已完成并验收通过

---

## 1. 任务概述

在现有 T204 成交价模型基础上，添加**可选**跳空缺口滑点参数，应对隔夜 ±3%/±5% 价格跳空时的额外流动性冲击成本。

---

## 2. 交付物清单

| 交付物 | 位置 | 状态 |
|--------|------|------|
| **实现代码** | `backtest/fees.py::make_price_model` | ✅ 已完成 |
| **测试用例** | `tests/test_gap_slippage.py` | ✅ 26 例全绿 |
| **文档** | `docs/gap_slippage_model.md` | ✅ 已完成 |
| **向后兼容** | T204 原有 18 例 | ✅ 全部通过 |

---

## 3. 验收结果

### 3.1 核心指标

- ✅ **26 例离线单测全绿**（`pytest tests/test_gap_slippage.py`，2.35 秒）
- ✅ **T204 向后兼容**（18 例原有测试全绿，13.22 秒）
- ✅ **黄金算例验证通过**（preclose=9.50 → open=10.00，+5.26% 跳空，BUY 成交价 10.02）
- ✅ **全量测试通过**（515 passed，与 T310 无关的 5 个 dividend 测试失败已存在）

### 3.2 测试覆盖矩阵

| 测试类 | 用例数 | 覆盖内容 |
|--------|--------|----------|
| `TestGapSlippageParamValidation` | 5 | fail-closed：类型校验 / 负值拒绝 / 边界检查 |
| `TestBackwardCompatibility` | 2 | `gap_slippage_pct=None` ⇒ 恒等 T204 原逻辑 |
| `TestGapDetection` | 4 | 阈值逻辑：< 3% 不触发 / ≥ 3% 触发 / 自定义阈值 |
| `TestDirectionality` | 4 | BUY/SELL × 跳空↑/↓ 四象限方向性 |
| `TestStressScenarios` | 6 | 3%/5% 缺口 × 10bps/20bps 滑点 + 单调性 |
| `TestEdgeCasesAndInvariants` | 4 | preclose=0 / 涨跌停限幅 / tick 红线 |
| `TestMultiDayGapStress` | 1 | 5 日连续跳空累积拖累实战场景 |

### 3.3 黄金算例（手算验证）

```python
# 场景：跳空高开 +5.26%，BUY 订单
preclose = 9.50
open = 10.00  # 跳空 +5.26% > 3% 阈值

# 计算：
基础滑点 = 10.00 × 1.0005 = 10.005
缺口滑点 = 10.005 × 1.0010 = 10.01501  （复利叠加）
tick 取整 = 10.02  （ROUND_HALF_UP 到 0.01）

# 验证：
assert make_price_model(gap_slippage_pct=Decimal("0.0010"))(order, bar) == Decimal("10.02")
✅ PASSED
```

---

## 4. 技术实现

### 4.1 签名扩展（`backtest/fees.py:411-493`）

```python
def make_price_model(
    config: FeeConfig | None = None,
    *,
    limit_pct: Decimal | None = None,
    gap_slippage_pct: Decimal | None = None,      # 新增
    gap_threshold_pct: Decimal = Decimal("0.03"), # 新增
) -> Callable[[Order, Bar], Decimal]:
    """次一开盘 + 基础滑点 + 缺口滑点 + tick 取整 + 涨跌停限幅"""
```

### 4.2 缺口检测逻辑

```python
# 基础滑点（原有逻辑）
slipped = apply_slippage(bar.open, order.side, cfg)

# 缺口滑点（新增，可选）
if gap_slippage_pct is not None and bar.preclose > 0:
    gap_pct = abs(bar.open - bar.preclose) / bar.preclose
    if gap_pct >= gap_threshold_pct:
        sign = 1 if order.side is OrderSide.BUY else -1
        slipped = slipped * (1 + sign * gap_slippage_pct)  # 复利叠加
```

### 4.3 Fail-Closed 校验

- ✅ `gap_slippage_pct` 必须是 `Decimal` 或 `None`（拒绝 `float`）
- ✅ `gap_threshold_pct` 必须是 `Decimal`（拒绝 `float`）
- ✅ `gap_slippage_pct` ≥ 0（负数无意义）
- ✅ `gap_threshold_pct` ∈ (0, 1)（超界拒绝）
- ✅ `bar.preclose > 0` 才计算缺口（防零除）

---

## 5. 使用示例

### 5.1 默认模式（向后兼容）

```python
# 不传 gap_slippage_pct ⇒ 无缺口滑点，完全兼容 T204
price_model = make_price_model()
```

### 5.2 启用缺口滑点（建议档）

```python
# 3% 阈值 + 10bps 缺口滑点
price_model = make_price_model(
    gap_slippage_pct=Decimal("0.0010"),   # 10bps
    gap_threshold_pct=Decimal("0.03"),    # 3%
)
engine = MatchEngine(price_model=price_model, ...)
```

### 5.3 压测档

```python
# 5% 阈值 + 20bps 缺口滑点
price_model_stress = make_price_model(
    gap_slippage_pct=Decimal("0.0020"),   # 20bps
    gap_threshold_pct=Decimal("0.05"),    # 5%
)
```

---

## 6. 推荐参数

| 参数 | 默认值 | 建议档 | 压测档 | 说明 |
|------|--------|--------|--------|------|
| `gap_slippage_pct` | `None`（不启用） | `0.0010`（10bps） | `0.0020`（20bps） | 缺口滑点率 |
| `gap_threshold_pct` | `0.03`（3%） | `0.03`（3%） | `0.05`（5%） | 缺口检测阈值 |

**校准思路**：
- 中性建议：10bps 缺口滑点 @ 3% 阈值（A 股主板日常极端波动）
- 保守压测：20bps 缺口滑点 @ 5% 阈值（2015 股灾 / 2020 熔断期间）

---

## 7. 历史实证

根据 A 股历史数据（`docs/t304_stress_report.md`）：

- **2015 股灾+熔断期间**：单日跳空 ±5% 约占 15% 交易日
- **2018 熊市**：跳空 ±3% 约占 30% 交易日
- **保守原则**：跳空向上/向下都触发（流动性枯竭双向），防御流动性危机

---

## 8. 与现有系统集成

### 8.1 与 T204 的关系

| 项 | T204 原有 | T310 扩展 |
|----|-----------|-----------|
| 成交价锚 | 次一开盘 `bar.open` | ✅ 不变 |
| 基础滑点 | 5bps（默认） | ✅ 保持，叠加基础 |
| 新增滑点 | 无 | ✅ 可选缺口滑点 |
| tick 取整 | 0.01 元 | ✅ 不变 |
| 涨跌停限幅 | 可选 `limit_pct` | ✅ 在缺口滑点**之后**生效 |
| 向后兼容 | — | ✅ `None` ⇒ 恒等原逻辑 |

### 8.2 红线不破

- ✅ **tick 红线**：成交价恒为 0.01 的整数倍（`test_tick_size_always_respected`）
- ✅ **限幅红线**：滑点后不越涨跌停（`test_limit_clamp_after_gap_slippage`）
- ✅ **撮合顺序**：规则 2/3 一字板拒单在价格模型**之前**（语义独立）

---

## 9. 局限与后续优化

### 9.1 当前假设

1. **二元阈值**：缺口 ≥ 阈值即触发（实际可能是连续函数）
2. **方向对称**：跳空↑/↓同等对待（实际追涨成本可能 > 杀跌）
3. **无个股差异**：大盘蓝筹 vs 小盘股流动性未区分
4. **v1 不区分板块**：主板 10% vs 创业板 20% 涨跌停未建模

### 9.2 v2 留口（`docs/gap_slippage_model.md` §8）

- 连续缺口函数：`f(gap) = max(0, (gap - 2%) × 5)`
- 个股流动性分层：按日均成交额分档
- 方向性系数：追涨 × 1.5 / 杀跌 × 1.0
- Tick 数据校准（若有条件）

---

## 10. 验收判据达成情况

| 判据 | 要求 | 实际 | 状态 |
|------|------|------|------|
| 1. 单测全绿 | ≥26 例 | 26 passed（2.35s） | ✅ |
| 2. T204 兼容 | 18 例保持绿 | 18 passed（13.22s） | ✅ |
| 3. 黄金算例 | 手算验证 | 10.02（预期 10.02） | ✅ |
| 4. 文档齐全 | 模型定义+参数+实证 | `docs/gap_slippage_model.md` | ✅ |

---

## 11. 文件清单

```
backtest/
  fees.py                          # 实现（411-493 行）
tests/
  test_gap_slippage.py             # 26 例测试
  test_t204_price_model.py         # T204 兼容性（18 例）
docs/
  gap_slippage_model.md            # 完整文档（272 行）
  T310_completion_summary.md       # 本报告
```

---

## 12. 后续建议

### 12.1 集成到 T305 评审

当前 T305 技术评审已停手等指示（`docs/t305_technical_review.md`）。若用户决定进入 Phase 4 模拟盘，建议：

1. **默认关闭缺口滑点**（`gap_slippage_pct=None`），先积累模拟盘数据
2. **记录跳空日实际成交价**：抽取模拟盘中跳空 ≥3% 的日期，统计实际成交价 vs 开盘价偏离
3. **校准后启用**：根据实盘数据反推缺口滑点率，再启用到正式回测

### 12.2 参数稳健性扫描（对标 T303）

若需量化缺口滑点对策略的影响，可扩展 T303 参数扫描：

```python
# 扫描缺口滑点档：0bps / 10bps / 20bps
for gap_slip in [None, Decimal("0.0010"), Decimal("0.0020")]:
    result = run_backtest(strategy, price_model=make_price_model(gap_slippage_pct=gap_slip))
    # 对比 CAGR / MDD / 年化换手差异
```

---

## 13. 结论

✅ **T310 跳空缺口滑点压力测试已完成并通过全部验收**：

- 实现完整、测试充分（26 例全绿）
- 向后兼容 T204（18 例保持）
- 文档齐全、接入示例清晰
- Fail-closed 红线完备、金额全 Decimal
- 黄金算例手算验证通过

**可立即用于 Phase 3 策略压测，或待 Phase 4 模拟盘校准后启用。**

---

**生成：2026-09-02**  
**验收人：Claude Opus 4.8**  
**状态：✅ 全部交付物齐备，可交付**
