# 数据字典 v1 — A 股日线量化系统

> **版本**: 1.0.0
> **日期**: 2026-08-31
> **依据**: constitution v1.0.0、spec v1.0.0（FR-DATA-1~5）、REVALIDATE.md（R1-R5）、12 号 §9-A 实测
> **权威位置**: `research-finai\specs\001-a-stock-longonly-daily-quant\data_dictionary_v1.md`
> **本仓只读快照**: `FinAI2.0\docs\spec\001-a-stock-longonly-daily-quant\data_dictionary_v1.md`
>
> 本字典登记每个字段的**语义、复权口径、可得日期、pubDate 对齐规则**。所有新数据源/新字段接入前，必须先在此登记"历史上哪一天可得"（constitution 原则 I）。

---

## 1. 日线行情字段

### 1.1 主源：baostock `query_history_k_data_plus`

**实测基准**（12 号 §9-A-4，2026-08-29 复现确认）：

| 字段 | 类型 | 语义 | 复权口径 | 可得起始 | 备注 |
|---|---|---|---|---|---|
| `date` | str (YYYY-mm-dd) | 交易日 | 无关 | 1990‑12‑19 | 索引列 |
| `open` | float64 | 开盘价（元） | 随 `adjustflag` | 同左 | ⛔ 含停牌日脏行（前收平推），须 `tradestatus` 过滤 |
| `high` | float64 | 最高价（元） | 随 `adjustflag` | 同左 | 同上 |
| `low` | float64 | 最低价（元） | 随 `adjustflag` | 同左 | 同上 |
| `close` | float64 | 收盘价（元） | 随 `adjustflag` | 同左 | 同上 |
| `preclose` | float64 | 昨收（元） | 随 `adjustflag` | 同左 | 复权后 ≠ 前日 close（股改对价/除权） |
| `volume` | float64 | 成交量（股） | 无关 | 同左 | 停牌日=0 |
| `amount` | float64 | 成交额（元） | 无关 | 同左 | 停牌日=0 |
| `turn` | float64 | 换手率（%） | 无关 | 2010‑01‑01 前缺失 | 部分早期股无数据 |
| `pctChg` | float64 | 涨跌幅（%） | 随 `adjustflag` | 同左 | 即 `(close - preclose) / preclose` |
| `tradestatus` | str | `'1'`=正常交易、`'0'`=停牌 | 无关 | 同左 | **R1 强制过滤列**；缺失则无法过滤脏行 |
| `isST` | str | `'1'`=ST/\*ST、`'0'`=正常 | 无关 | 2010‑01‑01 前缺失 | |
| `adjustflag` | str | 返回数据的复权档 | 见下文 | 同 `date` | baostock 入参，非输出字段 |
| `code` | str | 股票代码（如 `sh.600000`） | 无关 | 同左 | baostock 格式 |
| `time` | str | 复用时间戳 | 无关 | 同左 | 固定值，可忽略 |

**复权档 `adjustflag` 枚举**（R4 映射表，`finai/sources/adjustment_mode.py`）：

| 值 | 语义 | 映射枚举 |
|---|---|---|
| `'1'` | 不复权（RAW） | `AdjustmentMode.RAW` |
| `'2'` | 前复权（QFQ） | `AdjustmentMode.QFQ` |
| `'3'`（默认） | 后复权（HFQ） | `AdjustmentMode.HFQ` |
| `''`（空串） | 同 `'3'`（后复权） | `AdjustmentMode.HFQ` |

⛔ **baostock 默认 `adjustflag='3'`=后复权（最贵陷阱）**。12 号 §9-A 实测：无参调用返回后复权价，且 baostock 之 hfq 精度 10 位小数。**调用侧必须显式传参，禁止默认**（constitution 原则 II + R4）。

**R1 停牌脏行过滤规则**（FR-DATA-2）：
```
filtered = df[df.tradestatus == "1"].copy()
suspended_rows = len(df) - len(filtered)
meta["suspended_rows"] = suspended_rows   # 记入血缘
```
停牌日 baostock 返回 OHLC=前收平推、volume=0、tradestatus=`'0'`，100% 命中（12 号 §9-A ②）。**不报错不告警，必须显式过滤**。

### 1.2 校验源：新浪 `akshare::stock_zh_a_hist`

| 字段 | 类型 | 语义 | 复权口径 | 可得起始 | 备注 |
|---|---|---|---|---|---|
| `date` | str (YYYY-mm-dd) | 交易日 | 无关 | 同主源 | 索引列 |
| `open/high/low/close` | float64 | 价格（元） | 随 `adjust` | 同左 | 仅 2 位小数（baostock 10 位） |
| `volume` | int64 | 成交量（股） | 无关 | 同左 | |
| `amount` | float64 | 成交额（元） | 无关 | 同左 | |
| `振幅` | float64 | 振幅（%） | 无 | 同左 | 非必需字段 |
| `涨跌幅` | float64 | 涨跌幅（%） | 随 `adjust` | 同左 | |
| `涨跌额` | float64 | 涨跌额（元） | 随 `adjust` | 同左 | |
| `换手率` | float64 | 换手率（%） | 无 | 2010- | |

**差异点**：无 `preclose`、无 `tradestatus`（停牌日直接跳过该行，不返回脏行）、无 `isST`。精度 2 位 vs baostock 10 位。

**复权档 `adjust` 枚举**（akshare 参数名 `adjust`）：

| 值 | 语义 | 映射枚举 |
|---|---|---|
| `''`（默认） | 不复权 | `AdjustmentMode.RAW` |
| `'qfq'` | 前复权 | `AdjustmentMode.QFQ` |
| `'hfq'` | 后复权 | `AdjustmentMode.HFQ` |

**跨源对账规则**（FR-DATA-1）：
- 先对齐行数与日期（新浪停牌日直接跳过，行数可能少）
- 2015 年后跨源收益差 <0.2pp（12 号 §9-A ⑧）
- 分歧来源：2005-2006 股改对价（新浪计入、baostock 不计入，×1.32~1.35）
- ⛔ **禁止跨源混用复权因子**（FR-DATA-3）：对账前各自列明 `adjust_mode`

### 1.3 校验源：腾讯 `akshare::stock_zh_a_hist_tx`

| 字段 | 类型 | 语义 | 可得起始 | 备注 |
|---|---|---|---|---|
| `date` | str | 交易日 | 同左 | 索引列 |
| `open/high/low/close` | float64 | 价格（元） | 同左 | ⛔ 仅 6 列，最薄 |
| `volume` | int64 | 成交量（股） | 同左 | |
| `amount` | float64 | **此列名为"成交额"实为成交量** | 同左 | ⚠ 探针实测：`amount` 与 `volume` 中位比值 1.0（取整抖动 ≤3.3e-4） |

⛔ 独自一个 schema `ohlcv_daily_tx`，不与主源 schema `ohlcv_daily` 自动互换。无成交额、无换手率、无 `tradestatus`。停牌日直接跳过行。

### 1.4 其他源（排除/降级）

| 源 | 状态 | 原因 |
|---|---|---|
| **东财 push2his**（akshare/efinance 包） | ⛔ 本机不可达（FR-DATA-8） | 12 号附录 A.5 探测 3 次全 `ProxyError: RemoteDisconnected`；换机后复测通过才可纳为第四源 |
| **mootdx/tdxpy**（通达信 TCP） | ⛔ 已砍（R5 方案 B） | 分钟线时代产物；v1 仅日线，`data_catalog` 未搬入；`connect()` 触发 `ModuleNotFoundError`；未来需分钟线时按 R5 方案 A 恢复 |
| **citydata（tushare 代理）** | 可选补充 | 需 `TUSHARE_TOKEN`，凭据存于 `_archive` 未激活；手势不同（`ts_code`/`trade_date`），不与主源 schema 自动互换 |

---

## 2. 财务数据字段

### 2.1 baostock 季频财务表（6 张季度比值表）

**实测基准**（12 号 §9-A ⑤，2026-08-29 复现确认）：

| 表 | baostock 接口 | 行数（探针 OK） | 关键列 |
|---|---|---|---|
| 利润表 | `query_profit_data` | 1 行（单股样例） | `pubDate`,`statDate`,`roe`,`netProfitMargin`,`grossProfitMargin`… |
| 资产负债表 | `query_balance_data` | 1 行 | `pubDate`,`statDate`,`currentRatio`,`quickRatio`… |
| 现金流量表 | `query_cash_flow_data` | 1 行 | `pubDate`,`statDate`… |
| 成长能力 | `query_growth_data` | 1 行 | `pubDate`,`statDate`… |
| 杜邦指标 | `query_dupont_data` | 1 行 | `pubDate`,`statDate`… |
| 经营能力 | `query_operation_data` | 1 行 | `pubDate`,`statDate`… |

**所有 baostock 财务表共有的 PIT 字段**：

| 字段 | 语义 | PIT 规则 |
|---|---|---|
| `pubDate` | **公告日**（YYYY-mm-dd） | PIT 对齐主键（FR-DATA-4）——信号时点只能用到 `pubDate ≤ 信号日` 的财报 |
| `statDate` | 报告期（YYYY-mm-dd） | 仅用于标识报告期，⛔ 不作为 PIT 对齐依据 |
| `performanceExpPubDate` | 业绩快报公告日（快报表） | 替代 `pubDate` 用于快报 |
| `profitForcastExpPubDate` | 业绩预告公告日（预告表） | 替代 `pubDate` 用于预告 |

**PIT 对齐规则**（FR-DATA-4，constitution 原则 I）：

```
# 信号时点 t 可用的最新财报：
available = df[df.pubDate <= t].sort_values("pubDate").drop_duplicates("code", keep="last")
```

12 号 §9-A 实测降级方案保守性验证：成立率 100%，中位滞后 3 天，最大 46 天。

⛔ **禁止按报告期对齐**（`statDate` 是未来函数：年报可在次年 4 月才公告，但 `statDate` 写前一年 12-31）。

### 2.2 baostock 业绩快报与预告

| 接口 | 探针状态 | 行数 | PIT 字段 |
|---|---|---|---|
| `query_performance_express_report` | OK | 4 行 | `performanceExpPubDate` |
| `stock_profit_forecast_em`（东财） | OK | 5 行 | `profitForcastExpPubDate` 或等价 |

### 2.3 东财财务表（akshare 桥，可选补充）

| 表 | 接口 | 探针 | PIT 字段 |
|---|---|---|---|
| 利润表 | `stock_profit_sheet_by_quarterly_em` | 未探 | `ann_date`（公告日期） |
| 现金流量表 | `stock_cash_flow_sheet_by_quarterly_em` | OK（93 行） | `ann_date` |
| 资产负债表 | `stock_balance_sheet_by_quarterly_em` | 未探 | `ann_date` |
| 业绩快报 | `stock_yjkb_em` | 未探 | `ann_date` |
| 业绩预告 | `stock_yjyg_em` | 未探 | `ann_date` |

**已知 gap**：新浪财务指标（`stock_finance_sina` 系列）**无公告日字段**，不可用于 PIT 对齐。

**⚠ 东财 `ann_date` 风险**：`ann_date` 可能被后续更正公告覆盖（同报告期多次公告），`announcement_source.py`（FINDING-127）规定业务生效日为 `ann_date + 1`（公告日次日才可用）。

### 2.4 财务数据可用性总表

| 数据类别 | 主源 | 可得起始 | PIT 可用 | 备注 |
|---|---|---|---|---|
| 季度利润表 | baostock | 2007‑Q1 | ✅（`pubDate`） | 季频财报含 pubDate 已实测确认 |
| 季度资产负债表 | baostock | 2007‑Q1 | ✅（`pubDate`） | |
| 季度现金流量表 | baostock | 2007‑Q1 | ✅（`pubDate`） | |
| 成长能力 | baostock | 2007‑Q1 | ✅（`pubDate`） | |
| 杜邦指标 | baostock | 2007‑Q1 | ✅（`pubDate`） | |
| 经营能力 | baostock | 2007‑Q1 | ✅（`pubDate`） | |
| 业绩快报 | baostock | 2010‑ | ✅（`performanceExpPubDate`） | |
| 业绩预告 | baostock/东财 | 2010‑ | ✅（`profitForcastExpPubDate`） | 东财预告更多 |
| 东财利润表 | akshare 桥 | 2007‑ | ⚠（`ann_date`+1） | 本机东财不可达 |
| 新浪财务指标 | akshare 桥 | 2007‑ | ❌（无公告日） | 仅作参考，不用于 PIT |

---

## 3. 股票池与指数成分

### 3.1 baostock 股票池

| 接口 | 探针状态 | 行数 | 可得起始 | 备注 |
|---|---|---|---|---|
| `query_stock_basic` | ✅ OK | 8,878 行 | 全量 | `ipoDate`/`outDate`/`type`/`status` 列；可回放历史存活状态 |
| `query_hs300_stocks` | ✅ OK | 300 行 | **仅当前快照** | 有 `date` 参数但**无历史回放保证**（探针仅测当前） |
| `query_zz500_stocks` | ✅ OK | 500 行 | **仅当前快照** | 同上 |
| `query_sz50_stocks` | ✅ OK | 50 行 | **仅当前快照** | 同上 |
| CSI1000 成分 | ❌ 无对应接口 | — | 不可回放 | baostock 不提供；需其他源 |

**历史回放规则**（FR-DATA-5，constitution 原则 I）：
```
# 方式 A（bs 股票池）：通过 query_stock_basic 的 ipoDate/outDate 推算
# 方式 B（指数成分）：仅当前快照 → 用下一版数据字典登记"历史成分"源
```

**已知 gap**：HS300/SZ50/ZZ500 的历史成分回放未实测通过。`finai/sources` 下**无**对应 wrapper。CSI1000 无回放能力。这部分待 T108 实现时补充。

### 3.2 退市/停牌/新股规则

| 场景 | 规则 |
|---|---|
| 退市股 | `query_stock_basic` 中 `outDate` 非空且≤当前日期；退市日前的数据照常可用 |
| 停牌股 | 行情含 `tradestatus='0'` 行（过滤后不入库），但历史数据仍保留 |
| 新股 | `ipoDate` 为上市日；上市首日无涨跌停限制（回测中需特殊处理） |

---

## 4. 复权语义总表

### 4.1 三态枚举（AdjustmentMode）

`finai/sources/adjustment_mode.py`（R4 实现，2026-08-30）：

| 枚举值 | 人话标签 | 语义 |
|---|---|---|
| `AdjustmentMode.RAW` | 不复权 | 原始成交价，无任何复权修正 |
| `AdjustmentMode.QFQ` | 前复权 | 以最新价为基准前向调整历史价格（含未来函数！） |
| `AdjustmentMode.HFQ` | 后复权 | 以首日为基准向后调整价格（无未来函数，但价格不可直接交易） |

⛔ **不设 `UNKNOWN`/`AUTO` 枚举值**：不知道口径=不能入库，宁可缺不可错（R4 §1）。

### 4.2 各库复权参数映射

| 库 | 原生参数名 | RAW 值 | QFQ 值 | HFQ 值 | 默认值 | 默认口径 |
|---|---|---|---|---|---|---|
| baostock | `adjustflag` | `'1'` | `'2'` | `'3'`/`''` | `'3'` | **HFQ（后复权，最贵陷阱）** |
| akshare（新浪） | `adjust` | `''` | `'qfq'` | `'hfq'` | `''` | RAW（不复权） |
| efinance | `fqt` | `0` | `1` | `2` | `1` | **QFQ（前复权）** |
| mootdx | 无参数 | — | — | — | — | 仅 RAW（无复权参数） |
| tdxpy | 无参数 | — | — | — | — | 仅 RAW（无复权参数） |
| adata | `adjust_type` | `0` | `1` | `2` | `1` | **QFQ（前复权）** |

### 4.3 数据存储规则（FR-DATA-3）

1. **存储优先 baostock hfq**（10 位小数精度，新浪仅 2 位）
2. **落盘列含 `adjust_mode` 标签**：每列/每批数据标注其口径
3. **前复权仅用于展示**：历史信号特征禁止直接用"当前基准"前复权价（constitution 原则 I）
4. **历史信号用后复权或以信号时点重构复权价**
5. **跨源对账前先对齐行数与日期**，然后各自列明 `adjust_mode`
6. **跨源禁止混用复权因子**（2005-2006 股改对价分歧源）

---

## 5. 血缘与元数据字段

每批入库数据应携带以下元数据（记入 `meta` 字典或血缘列）：

| 字段 | 语义 | 来源 | 必须 |
|---|---|---|---|
| `adjust_mode` | 复权口径枚举 | `AdjustmentMode` | ✅ |
| `source` | 数据源标识（`baostock`/`sina`/`tencent`） | 调用方 | ✅ |
| `suspended_rows` | 本批过滤的停牌脏行数 | R1 过滤步骤 | ✅ |
| `truncated_bars` | 静默截断行数（R2，当前挂起） | 覆盖率校验 | ⚠ 待 R2 恢复 |
| `fetch_time` | 采集时间戳 | 调用方 | 推荐 |
| `symbol` | 股票代码（统一格式） | 调用方 | ✅ |
| `start_date`/`end_date` | 请求区间 | 调用方 | 推荐 |
| `rows_requested` vs `rows_returned` | 覆盖率校验 | 调用方 | ⚠ 待 R2 恢复 |

---

## 6. 采集限速与幂等（FR-DATA-7 / FR-DATA-6）

| 规则 | 内容 |
|---|---|
| 串行 | 单源单线程采集，不并发 |
| 间隔 | 相邻请求间隔 ≥0.5s |
| 重试 | 指数退避重试（退避基数为 1s，最大 3 次） |
| 熔断 | 连续失败 3 次 → 熔断当前源，切换备胎；触发告警（FR-OPS-3，飞书） |
| 幂等 | 按日期分区覆盖写入（重复运行同一区间不产生脏数据） |
| 禁止重试风暴 | 熔断后不再重试该源，直到人工介入或下一调度周期 |

---

## 7. 排除项（spec §2.2 明确不做的）

| 数据类别 | 排除原因 |
|---|---|
| 融资融券（两融余额） | v1 无杠杆（constitution 原则 V）；北交所 50 万门槛不可达 |
| 资金流（主力/散户） | 属于 spec §2.2 排除的"信息轴"；非日线信号所需 |
| 龙虎榜 | 同上 |
| 分钟线/小时线 | v1 纯日线（constitution 附加约束）；baostock 分钟线仅 2020 年起 |
| 美股/韩股数据 | v1 仅 A 股；架构留口但不实现（FR-EXT，YAGNI） |
| 机器学习/深度学习 | 初期策略不涉及（constitution 原则 X，YAGNI） |

---

## 8. 验收判据（G2 / T110）

| # | 判据 | 对应 |
|---|---|---|
| 1 | 三源抽样比对：baostock vs 新浪 vs 腾讯，跨源分歧 <0.2pp（2015 后） | FR-DATA-1 |
| 2 | 停牌命中 100%：抽样 10 停牌日，OHLC≠前值填充满仓，成交量=0 标记 | FR-DATA-2 |
| 3 | 复权口径列在每行数据中可追溯（`adjust_mode` 列或血缘） | FR-DATA-3 |
| 4 | PIT 验证：信号时点用到的最新财报公告日 ≤ 信号日 | FR-DATA-4 |
| 5 | 历史成分回放：2015 年成分与当年全量对照，无前瞻成分 | FR-DATA-5 |
| 6 | 同一区间重跑两次哈希一致（幂等性） | FR-DATA-6 |
| 7 | 注入错误源：重试次数上限、熔断触发后停止并告警 | FR-DATA-7 |

---

## 9. 修订日志

| 编号 | 日期 | 内容 | 依据 |
|---|---|---|---|
| DD-1 | 2026-08-31 | 初版（v1.0.0）：日线行情字段、财务 PIT 字段、股票池、复权语义、血缘元数据、排除项、验收判据 | FR-DATA-1~5 要求 + 6 路并行调研（6 子 agent 探针交叉验证）+ 本仓代码实测（baostock_source.py / capability_router.py / adjustment_mode.py / announcement_source.py）+ 探针矩阵 auto_probe_results.json |