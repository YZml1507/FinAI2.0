# MASTER_LIBRARY_INDEX — FinAI2.0 数据接口集合

> 生成时间：2026-08-29
> 来源：`D:\Projects\_archive\FinAI_20260829\library\`
> 前置读取：**`docs/engineering/DATA_LAYER_WORK_ORDER.md`**（旧版 DATA_LAYER_DESIGN_PRINCIPLES.md 已删：未调研时期 AI 代笔；母库工程约束见该文 §3）

---

## 1. 母库边界

| 维度 | 数值 | 来源 |
|---|---|---|
| 源模块数 | 18 文件（含 `__init__.py`） | `ls finai/sources/*.py | wc` |
| 模块总行数 | 7,403 行 | `wc -l finai/sources/*.py` |
| 实测可用接口 | 860 条 | 旧仓实测（见 MIGRATION_LIST.md） |
| 实测依赖库 | 7 个 | akshare / adata / baostock / efinance / mootdx / tdxpy / citydata-proxy |
| 凭据需求 | 仅 `CITYDATA_TS_TOKEN` + `CITYDATA_PROXY` 必需 | citydata 路径独占 |

## 2. 文件清单与行数

### 2.1 `finai/sources/`（18 文件，7,403 行）

| 模块 | 行数 | 职责 | P1–P6 归属 |
|---|---:|---|---|
| `__init__.py` | 116 | 包入口 + SOURCE_REGISTRY 枚举 | P1（全量登记）、P6（发现） |
| `capability_router.py` | 1,575 | 按数据种类取数 + 自动换源 | **P2 核心**（多源容错） |
| `segmented_pull.py` | 1,146 | 可分段并行拉取 | **P6 核心**（维护汇总表 + 按需拉取） || `base.py` | 1,062 | 接口探测与解析、FetchResult / classify_exception | P4（血缘标注）、P6（可用判定） |
| `catalog_source.py` | 434 | 按接口名直调（实测 672 条可直调） | P1（全部登记） |
| `citydata_source.py` | 229 | citydata 代理实现（替换失效 tushare 直连） | P1（顶替失效路径）、P4（凭据 + proxy 必填） |
| `lhb_source.py` | 195 | 龙虎榜 | P2（多源备选） |
| `money_flow_source.py` | 220 | 资金流 | P2 |
| `announcement_source.py` | 207 | 公告 | P2 |
| `block_trade_source.py` | 163 | 大宗交易 | P2 |
| `margin_source.py` | 156 | 融资融券 | P2 |
| `sentiment_source.py` | 153 | 舆情 | P2 |
| `baostock_source.py` | 160 | baostock 日线 | P2 + P5（落盘 parquet） |
| `tdx_source.py` | 136 | TDX 行情 | P2 |
| `tdx_ext_source.py` | 151 | TDX 扩展（港股/期货/宏观） | P2 |
| `tdx_daily_bar_adapter.py` | 201 | TDX 日线适配器 | P5 |
| `ths_limit_up_adapter.py` | 209 | 同花顺连板适配器 | P5 |
| `overseas_registry.py` | 114 | 非 A 股市场登记（不取数） | P1（登记即完备） |

### 2.2 `finai/` 平铺依赖（4 文件）

| 文件 | 行数 | 职责 | 被引用模块 |
|---|---:|---|---|
| `credentials.py` | 329 | 凭据读取（`get_credential`） | `citydata_source.py` |
| `security_ids.py` | 338 | 代码→交易所归属映射，拒绝猜 | `capability_router.py:35` |
| `date_utils.py` | 42 | 混合日期格式解析入口（`normalize_date_column` 定义在此，上层消费） | 上层消费（不引用本文件，留待后续接入） |
| `em_rate_limiter.py` | 133 | 东财限流器 `em_get/em_post`（归档版残留，新仓尚无调用方） | 尚未被 sources 调用，留档待后续接入 efinance 路径 |

### 2.3 `scripts/`（2 文件，3,133 行）

| 文件 | 职责 | 被谁导入 |
|---|---|---|
| `auto_probe_interfaces.py` | 接口探测 + 限流阈值表（`DOMAIN_MIN_INTERVAL` 等） | `segmented_pull.py:380`（`_probe_tables()` 注入 `sys.path` 调用） |
| `probe_data_interfaces.py` | 探测辅助脚本 | 独立运行 |

### 2.4 `artifacts/interface_matrix/`（2 JSON）

| 文件 | 字节 | 被谁读取 | 风险 |
|---|---:|---|---|
| `auto_probe_results.json` | 1,557,610 | `catalog_source.py:58-59` | ⛔ 缺文件 → `catalog_source` 直接挂 |
| `interfaces_raw.json` | 405,826 | `overseas_registry.py:32-33` | ⛔ 缺文件 → `overseas_registry` 直接挂 |

## 3. P1–P6 归属摘要

| 原则 | 核心落实模块 | 说明 |
|---|---|---|
| **P1** 全量接口入库 | `catalog_source.py`, `overseas_registry.py` | 目录即登记，不依赖供应商 |
| **P2** 多源容错 | `capability_router.py` | `lib` 字段驱动候选轮换，失败即换源 |
| **P3** 交叉验证 | `base.py` 的 `classify_exception` / `CONCLUSIVE` | 多源对账判据 |
| **P4** 单源默认正确+血缘 | `base.py`（`make_result` 写入血缘字段） | 落盘 parquet 时带 `adjust_mode` |
| **P5** 按用途存储 | `tdx_daily_bar_adapter.py`, `baostock_source.py`, `ths_limit_up_adapter.py` | parquet 列含复权口径 |
| **P6** 维护汇总表+按需消费 | `segmented_pull.py`, `catalog_source.py` | catalog 目录 + `callable_here` 字段驱动发现 |

## 4. 硬性布局约束（来自 `segmented_pull.py:360-380`）

- `finai` 必须在仓库根下两层：`FinAI2.0/finai/...`
- `scripts/` 必须是 `FinAI2.0/scripts/`（兄弟目录）
- 否则 `_probe_tables()` 的 `sys.path` 注入会指向错误路径 → 限流阈值静默过期

## 5. 已知缺陷登账

见同目录 **`REVALIDATE.md`**（R1–R5，待修复）。

## 6. 本文件与旧仓的关系

- 母库归档于 `D:\Projects\_archive\FinAI_20260829\library\`
- 本文件是对母库边界的**实测登记**，**不是复制**。
- 任何关于"接口数 / 行数 / 依赖"的争议，以本表实测数据为准。
