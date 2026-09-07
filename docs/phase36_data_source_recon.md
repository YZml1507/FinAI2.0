# Phase 3.6 数据源勘测报告 —— CITYDATA 与 HITHINK 双主力实测

> 日期：2026-09-07。为 Phase 3.6 多策略研发（agy 下发 Task A/B/C 三采集任务）做的数据源能力核实。
> 结论先行：**Task A（ETF 日线）与 Task B（PIT 财务特征）由 CITYDATA 独家满足；HITHINK 只覆盖 A 股 K 线，不覆盖基金/ETF，也不提供带公告日的批量财务指标**，故本阶段三任务不采用 HITHINK。

## 1. 数据源定位

| 源 | 基址 | 认证 | 能力 |
|---|---|---|---|
| CITYDATA | `tushare.citydata.club` | `CITYDATA_TS_TOKEN` + `CITYDATA_PROXY`（商家指定代理，⛔ 不可更换） | tushare 镜像，114 接口 / 18 数据族 |
| HITHINK | `fuyao.aicubes.cn` | `HITHINK_FINANCE_API_KEY`（X-api-key 头） | 同花顺，A 股 K 线 + 复权 + 财报指标 |

## 2. Task A（6 只核心 ETF 日线）—— 逐源实测

| 源 | 结论 | 证据 |
|---|---|---|
| **CITYDATA `fund_daily`** | ✅ 权威，全量满足 | 6 只 ETF 2015→2024 深度实测：510300=2431 行、510500=2429、159915=2430、511010=2431、513100=2430、512890=1443（2019-01-18 上市，前段合法缺失）。字段 `trade_date/open/high/low/close/vol/amount/pre_close`，`vol` 单位**手**、`amount` 单位**千元**（已用腾讯 RAW 交叉验证 close 一致：2024-12-30=4.086、12-31=4.022） |
| **CITYDATA `fund_adj`** | ✅ 复权因子全量满足 | 与 `fund_daily` 同日期口径的 `adj_factor`，全 10 年深度；513100 因子 5.0019→1.0、510300 因子 1.0364→1.2078 等，与真实除权事件吻合 |
| **CITYDATA `fund_basic`** | ✅ 上市日期 | 6 只 `list_date` 全取到，用于自动界定起点（⛔ 不硬编码） |
| **HITHINK `search`** | ⚠ 能检索到 ETF（返回 `fund-etf`/`fund-otc` 两类） | `search q=510300` → 2 行，含 `asset_type=fund-etf` |
| **HITHINK `kline`** | ❌ **不覆盖基金/ETF** | `510300.SH`/`510300` 均报 `code=1002 Unknown thscode`；而 `600519.SH` 股票正常返回。已探 `/api/fund/*`、`/api/etf/*`、`/api/a-share/fund/*` 三路均 404 → **HITHINK 结构上没有 ETF 日线端点** |
| 腾讯 `ifzq.gtimg.cn` | ✅ 备选交叉验证 | RAW 日线 close 与 CITYDATA 逐日一致（已抽 2024-12 两日核对） |

**结论**：Task A 用 **CITYDATA `fund_daily` + `fund_adj` + `fund_basic`**，腾讯 RAW 作交叉验证。

## 3. Task B（487 只高股息标的 PIT 财务特征）—— 逐源实测

目标字段：`pub_date / stat_date / roe / net_profit_yoy / debt_to_assets / cash_flow_per_share`。

| 源 | 结论 | 证据 |
|---|---|---|
| **CITYDATA `fina_indicator`** | ✅ 权威，字段**逐一对上** | 600519 实测 66 行，`ann_date` 20150421→20241026；字段全命中：`roe`（加权 ROE）、`netprofit_yoy`（归母净利润同比）、`debt_to_assets`（资产负债率）、`ocfps`（每股经营现金流）。null 率：roe 0/66、netprofit_yoy 2/66、debt_to_assets 0/66、ocfps 2/66 |
| CITYDATA `fina_indicator` PIT 键 | ✅ `ann_date` 即真实公告日 | 与 agy「PIT 硬红线：只认 pub_date，永不 stat_date」对齐 —— `ann_date`=公告日（对齐主键），`end_date`=报告期（仅元数据） |
| **HITHINK `financials/indicators`** | ❌ **无公告日 + 按报告期键控** | 端点 `/api/a-share/financials/indicators`，入参 `report={yyyy}-{q}`（如 `2024-4`），**只接受单一报告期**，返回 `abilities`（growth/profitability/solvency/operation/cash-flow 五组指标）。两个致命缺陷：① 顶层键仅 `thscode/report/abilities`，**无 ann_date/pub_date 公告日** → 无法做 PIT 对齐；② 10 年 × 4 季 = 40 次调用/股 × 487 股 = 19480 次调用，不可行。且 `index_id`（如 `index_deduct_weighted_avg_roe`）与 tushare 字段名不对齐，需额外映射 |
| HITHINK 批量财报端点 | ❌ 不存在 | `/api/a-share/financials/list`、`/api/a-share/financial-reports` 均 404 |
| baostock 财务表 | ⚠ 有但 IP 封锁 | `data/financial_pit.py` 已登记 `profit/balance/cash_flow/growth/dupont/operation` 六表，但 baostock 服务器对当前出口 IP 封锁（T312 已弃用） |

**结论**：Task B 用 **CITYDATA `fina_indicator`**，`ann_date` 作 pub_date 对齐主键。

## 4. Task C（10 年期国债收益率）—— 逐源实测

| 源 | 结论 | 证据 |
|---|---|---|
| **akshare `bond_zh_us_rate`** | ✅ 权威 | 东财中国国债收益率，列 `中国国债收益率10年`，实测 2015-01-02→2026-09-07 全量；2015-2024 裁剪后 2667 行，null 164（周末/节假日自然缺失） |
| CITYDATA / HITHINK | ❌ 均无国债收益率端点 | 两源均以 A 股/基金行情与财报为主，无宏观利率数据族 |

**结论**：Task C 用 **akshare `bond_zh_us_rate`**。

## 5. 单位口径（反伪审计关键）

| 字段 | 源原始 | 落盘 | 依据 |
|---|---|---|---|
| ETF `volume` | `fund_daily.vol` = **手** | **股**（×100） | tushare fund_daily 约定，已用 510300 2024-12-31 实证：vol 18436200 手 = 1.84B 股 |
| ETF `amount` | `fund_daily.amount` = **千元** | **元**（×1000） | 同上：amount 7479850 千元 = 7.48B 元，与 vol×VWAP≈4.0 吻合 |
| 财务 `roe` 等 | 百分比数值 | 原样（% 语义，不乘 100） | tushare fina_indicator 约定（roe=20.2461 即 20.25%） |

## 6. 采集产物与反伪审计

三脚本已落地 `scripts/`：`collect_etf_data.py`（Task A）、`collect_financial_pit.py`（Task B）、`collect_treasury_yield.py`（Task C）。

反伪约束落地：
- **无静态前向填充**：停牌/未上市/缺失 = 行缺失或 NaN，绝不 `ffill`。
- **PIT 硬红线**：Task B 对齐主键只认 `pub_date`（=`ann_date`），`stat_date`（=`end_date`）仅元数据列；`pub_date` 缺失行直接丢弃（宁缺勿错）。
- **幂等 + SHA-256**：复用 `data/collector._atomic_write_parquet` / `_canonicalize` / `hash_file`；Task A 二跑 SHA-256 逐字节一致（已实测 5 只 ETF 2015 分区哈希 MATCH）。
- **meta.json**：每任务产出总行数 / null 率 / 每 symbol 起止日期 / SHA-256。
