# T402 回测-模拟偏差容忍带量化

> 生成：2026-09-02 ｜ 例行命令：`py -3.11 -m pytest tests/test_t402_deviation.py -p no:ddtrace -q`

## 1. 核心目标

定义并实现**回测与模拟盘偏差的监控机制**，量化容忍带，超出即告警。

回测使用历史数据 + 模型估算（滑点/冲击成本），模拟盘使用实时/盘后数据 + 真实撮合。两者**必然存在偏差**，但偏差过大说明回测假设失效，需要调整回测模型或终止模拟盘。

## 2. 偏差指标定义

| 指标 | 口径 | 说明 |
|---|---|---|
| ``nav_deviation`` | ``abs(nav_paper − nav_backtest) / nav_backtest`` | NAV 日偏差（百分比，绝对值） |
| ``return_deviation`` | ``abs(return_paper − return_backtest)`` | 累计收益偏差（差值，非比例，单位百分点 pp） |
| ``turnover_deviation`` | ``abs(turnover_paper − turnover_backtest)`` | 换手偏差（差值，单位百分点 pp） |
| ``price_deviation`` | ``abs(price_paper − price_backtest) / price_backtest`` | 单笔成交价偏差（百分比） |
| ``slippage_deviation`` | ``slippage_realized − slippage_model`` | 单笔滑点偏差（已实现 − 模型估算，可正可负） |

## 3. 容忍带设定依据

容忍带设定来自 **T204 成交模型敏感度分析**（`docs/t204_price_model_sensitivity.md`）+ **T304 跨区间压力报告**（`docs/t304_stress_report.md`）：

| 指标 | 容忍带 | 依据 |
|---|---|---|
| **NAV 日偏差** | **≤0.5%** | 单档滑点（5bps → 15bps）在 T204 场景下终值影响 ≈0.1%~0.16%；累计偏差+撮合微差允许 **5×单档空间**（保守设定） |
| **月度收益偏差** | **≤2%** | 15bps 滑点档在 T204 场景下终值影响 −0.16%；按 **12× 月度累计**留 2% 空间（保守设定） |
| **月换手偏差** | **≤10pp** | T304 动量策略换手 ≈300%~1300%（熊市 vs 股灾）；偏离 **10 个百分点**（注意：非比例，是绝对差）视为可疑 |
| **单笔成交价偏差** | **≤1%** | 1%=100bps，**远大于滑点（5bps）**，足够吸收 tick 0.01 取整+盘口深度微差 |
| **单笔滑点偏差** | **≤0.5%** | 模型滑点 5bps；实际滑点上下波动 **±50bps（10×模型）**为正常流动性微差 |

### 3.1 保守原则

容忍带设定遵循 **"宁可响亮失败，不可静默继续"**（constitution 原则 X）：

- 单档滑点影响已知（T204 实测），容忍带取 **5×~10×单档**留足误差空间；
- 换手偏差取 **绝对差**（10pp），不取比例（避免小数放大误判）；
- 超出容忍带 ⇒ **立即告警**（fail-closed），不做"连续超出 N 天才告警"的宽松策略。

## 4. 监控频率

| 频率 | 对齐指标 | 说明 |
|---|---|---|
| **日频** | NAV 偏差 | 每个交易日对齐（回测与模拟盘同日净值比对） |
| **周频** | 累计收益偏差 | 每周五或最后一个交易日对齐 |
| **月频** | 换手偏差 | 每月末交易日对齐（换手需累计一定时间才有统计意义） |

成交价与滑点偏差按 **逐笔或批次均值** 监控（任一笔超出即告警）。

## 5. 实现架构

```
paper_trading/
├── deviation.py       —— 偏差指标计算（纯函数，compute_deviation_metrics）
├── tolerance.py       —— 容忍带配置（DEFAULT_TOLERANCE_BANDS 唯一登记点）
└── monitor.py         —— 偏差监控器（DeviationMonitor.check 判定超出 + 根因提示）
```

**职责分离**：
- `deviation.py` 只管计算（⛔ 纯函数、零 IO、零 pandas）；
- `tolerance.py` 只管配置（⛔ 不写魔法数字，全部显式登记）；
- `monitor.py` 只管判定（⛔ 不推送告警、不记文件，返回 DeviationReport 让调用方决定下游动作）。

## 6. 输出格式

### 6.1 DeviationMetrics（偏差指标）

```python
@dataclass(frozen=True)
class DeviationMetrics:
    date: _date
    nav_backtest: Decimal
    nav_paper: Decimal
    nav_deviation: Decimal
    return_backtest: Decimal
    return_paper: Decimal
    return_deviation: Decimal
    turnover_backtest: Decimal | None
    turnover_paper: Decimal | None
    turnover_deviation: Decimal | None
    price_deviations: list[Decimal]
    slippage_deviations: list[Decimal]
```

### 6.2 DeviationReport（监控报告）

```python
@dataclass(frozen=True)
class DeviationReport:
    date: _date
    metrics: DeviationMetrics
    # 超出标记（5 项）
    nav_exceeded: bool
    return_exceeded: bool
    turnover_exceeded: bool
    price_exceeded: bool
    slippage_exceeded: bool
    # 根因提示（规则匹配，⛔ 不做 AI 推理）
    root_cause_hints: list[str]

    @property
    def any_exceeded(self) -> bool:
        """是否有任何指标超出容忍带。"""
```

## 7. 根因提示逻辑

监控器在检测到超出时，按以下规则生成根因提示（⛔ **规则匹配，不做 AI 推理**）：

| 超出模式 | 提示内容 |
|---|---|
| NAV 单项超出 | `"NAV 偏差 X% 超出容忍带 0.5%（…依据）"` |
| 收益单项超出 | `"累计收益偏差 X% 超出容忍带 2%（…依据）"` |
| 成交价单项超出 | `"成交价偏差最大 X% 超出容忍带 1%（…依据）"` |
| **NAV + 换手同时超出** | `"⚠️ NAV 与换手同时超出 → 可疑：成交执行路径可能与回测假设偏离（实盘冲击成本 > 模型估算 或 拒单率 > 预期）"` |
| **成交价 + 滑点同时超出** | `"⚠️ 成交价与滑点同时超出 → 可疑：市场流动性可能低于回测假设（盘口深度不足 或 波动率异常）"` |

联合诊断（多指标同时超出）提供 **系统性问题的快速诊断路径**，帮助人工决策是否调整回测模型或终止模拟盘。

## 8. 告警集成（飞书）

监控器本身 **⛔ 不做 IO**（不推送告警、不记文件），只返回 `DeviationReport`。告警推送由 **调用方**（日终任务 / 实时监控脚本）负责：

```python
report = monitor.check(metrics)
if report.any_exceeded:
    # 调用飞书告警（hermes_orchestrator MCP）
    send_feishu_alert(
        title=f"⚠️ 模拟盘偏差超出容忍带 {report.date}",
        content="\n".join(report.root_cause_hints),
    )
```

飞书告警通道配置见 `D:\Projects\research-finai\.specify\memory\alert_channel.md`（T001 决策备忘）。

## 9. 测试覆盖

单元测试 `tests/test_t402_deviation.py` 覆盖：

1. ✅ 偏差计算公式验证（5 项指标逐一对照文档口径）
2. ✅ 边界与 fail-closed（NAV≤0 / 成交价≤0 / 换手 None 必须 raise 或 fail-soft）
3. ✅ 容忍带判定逻辑（临界值测试 + 任一笔超出即告警）
4. ✅ 联合诊断（NAV+换手 / 成交价+滑点 同时超出 → 系统性提示）
5. ✅ 自定义容忍带注入（覆盖默认配置）
6. ✅ 输出格式验证（DeviationReport 字段完整性 + any_exceeded 正确性）

**共 17 个单测**（≥8 要求），全部纯函数黑盒测试。

## 10. 容忍带变更纪律

⛔ 修改容忍带必须遵守以下流程：

1. **在本文档登记变更依据**（引用实测数据 / 压测报告 / 敏感度分析）；
2. **修改 `paper_trading/tolerance.py` 的 `DEFAULT_TOLERANCE_BANDS`**（唯一登记点）；
3. **同步修改测试用例**（`test_t402_deviation.py` 的临界值测试）；
4. **全量测试重跑**（保证新容忍带不破坏既有判定逻辑）。

⛔ 不许在代码里写魔法数字（如 `if dev > 0.005:`），必须引用 `tolerance.py` 配置。

## 11. 可选扩展（优先级低）

v1 实现聚焦 **偏差量化 + 超出判定 + 告警**，以下功能属可选扩展（Phase 4 后期或 Phase 5 按需实施）：

- **可视化**：NAV 对比曲线（回测 vs 模拟）/ 偏差热力图（按时间 × 指标）
- **统计诊断**：偏差分布（均值/标准差/95%分位）+ 趋势检测（连续偏离方向）
- **自适应容忍带**：根据策略换手率 / 市场波动率动态调整容忍带（需回测验证）

## 12. 验收清单

- [x] `paper_trading/deviation.py` 偏差计算函数实现（5 项指标）
- [x] `paper_trading/tolerance.py` 容忍带配置（DEFAULT_TOLERANCE_BANDS 唯一登记点）
- [x] `paper_trading/monitor.py` 监控器实现（判定 + 根因提示 + 联合诊断）
- [x] 至少 8 个单测全绿（实际 17 个）
- [x] 文档 `docs/t402_deviation_tolerance.md` 说明容忍带设定依据与使用

## 修订日志

| 日期 | 内容 |
|---|---|
| 2026-09-02 | 初版：偏差指标定义 + 容忍带设定（基于 T204/T304 实测数据）+ 监控器实现 + 17 单测全绿 |
