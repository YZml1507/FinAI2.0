# REVALIDATE — FinAI2.0 已知缺陷登账（R1–R5）

> 生成时间：2026-08-29
> 状态：**只登账，不修复**（修复是下一阶段任务）
> 来源：MIGRATION_LIST.md、旧仓实测记录、本仓库 `import` 冒烟结果
> 前置读取：**`docs/engineering/DATA_LAYER_WORK_ORDER.md`**（旧版 DATA_LAYER_DESIGN_PRINCIPLES.md 已于 2026-08-29 被用户判定删除：未调研时期 AI 代笔）

---

## R1 · baostock 日线缺 `tradestatus` 过滤 → 停牌脏行入库

| 字段 | 内容 |
|---|---|
| **现象** | baostock 日线返回的 `tradestatus` 列被丢弃（`baostock_source.py:126` 显式排除），停牌日（`tradestatus='0'`）的行带着当日收盘价入库 |
| **证据** | `baostock_source.py:126` `_COLS.drop(columns=['tradestatus'])`；旧仓实测曾记录 2025-07-18 停牌股 `close=7.77`（前收平推值）被当作真实行情入库 |
| **修复要求** | 1) `_COLS` 保留 `tradestatus` 列；2) 落盘前过滤 `df[df.tradestatus == '1']`；3) 被过滤的行数写入 `FetchResult.meta['suspended_rows']` 供血缘审计；4) ⛔ 不得用前收平推填补停牌缺口 |
| **验收判据** | 对一只 2025 年有停牌记录的标的（如 300xxx），拉取后停牌日**不在**结果集内，`meta['suspended_rows'] >= 1`，且 parquet 落盘列含 `tradestatus` |
| **优先级** | P1（数据正确性） |

---

## R2 · TDX 腿静默截断缺陷

| 字段 | 内容 |
|---|---|
| **现象** | `tdx_source.py` 的 5min 拉取对单次响应超过 TDX 协议返回上限（约 800 根/次）的段**静默截断**——返回 DataFrame 行数 < 请求行数时无 WARNING、无 FAIL，上层读成"完整数据" |
| **证据** | `tdx_source.py:87-95`：`raw = client.get_security_bars(...)` 后未校验 `len(raw)` 对请求区间的覆盖率；旧仓实测记录 5min 单标的 3 年段只返回 11,520 根（应为 13,824 根，截断 17%） |
| **修复要求** | 1) 每次 `get_security_bars` 后断言 `len(raw) >= expected_min_bars`（expected 由 `date_range` × 48 根/日计算，见 `__init__.py` SOURCE_REGISTRY note）；2) 截断时返回 `FAIL_DETERMINISTIC` 并附 `meta['truncated_bars']`；3) ⛔ 不得静默降级为"部分数据"返回 |
| **验收判据** | 对 5min 3 年段（同一标的）两次调用：一次修复前（预期行数少）一次修复后（应 FAIL 或行数完整）；FAIL 时 `FetchResult.status == FAIL_DETERMINISTIC` |
| **优先级** | P1（数据完整性，静默截断比报错更危险） |

---

## R3 · 东财 push2his 接口本机不可达复验

| 字段 | 内容 |
|---|---|
| **现象** | 旧仓 `auto_probe_results.json` 记录 `push2his.eastmoney.com` 域名探测结果为 `FAIL_UNREACHABLE`（连接超时），但该结果可能为本机网络环境（防火墙/代理）所致，非源端真实不可用 |
| **证据** | `auto_probe_results.json` 中 `push2his.eastmoney.com` 的 `probe_result` 字段为 `FAIL_UNREACHABLE`，`elapsed_ms > 30000` |
| **修复要求** | 1) 运行 `python scripts/auto_probe_interfaces.py --domain push2his.eastmoney.com` 复测；2) 若仍超时，检查本机代理设置后重测；3) 若确认源端可用，更新 `DOMAIN_MIN_INTERVAL` 表并清除该域名的 `FAIL_UNREACHABLE` 标记；4) ⛔ 不得在未复测的情况下直接采信旧 JSON 的探测结果 |
| **验收判据** | 复测命令输出有明确结果（`OK` 或新的 `FAIL_*`），并更新 `artifacts/interface_matrix/auto_probe_results.json` 对应条目 |
| **优先级** | P2（影响 efinance/东财路径可用性判定） |

---

## R4 · 复权口径映射表未建 → 跨源合并 126 倍跳变风险

| 字段 | 内容 |
|---|---|
| **现象** | 不同库对"复权"参数的默认值方向相反：`akshare adjust=''`（不复权）vs `efinance fqt=1`（前复权）；两源按默认参数调用后按 `trade_date` 合并，`close` 列可出现 126 倍跳变（`07-21 → 10.84`，`07-22 → 1371.53`） |
| **证据** | `akshare.stock_zh_a_hist(..., adjust: str = '')` vs `efinance.stock.get_quote_history(..., fqt: int = 1)`（实测签名）；`mootdx.reader.StdReader.daily` 无 adjust 参数（始终不复权） |
| **修复要求** | 1) 建立统一 `AdjustmentMode` 枚举：`RAW / QFQ / HFQ`；2) 建立映射表：`akshare: ''→RAW, 'qfq'→QFQ, 'hfq'→HFQ`；`efinance: 0→RAW, 1→QFQ, 2→HFQ`；`mootdx/tdxpy: 无参数→RAW`；3) ⛔ **禁止默认调用**——所有日线接口调用必须显式传 `adjustment` 参数；4) 落盘 parquet 列含 `adjust_mode` 字段（不只在元数据库）；5) 跨源合并前先比口径，同日同码 `close` 不等时先查 `adjust_mode` 差异，不得先怀疑数据错 |
| **验收判据** | 1) 映射表文件存在且覆盖 akshare/efinance/mootdx/tdxpy 四库；2) 对同一标的同一日，两个不同源拉取的 `close` 列在 `adjust_mode` 相同时误差 < 0.5%；3) 代码内 grep 不到无参调用 `stock_zh_a_hist(`（不带 `adjust=`）或 `get_quote_history(`（不带 `fqt=`） |
| **优先级** | P1（⭐ 最贵的一条：不做则所有跨源日线合并结果不可信） |

---

## R5 · TDX/5min 腿不可调用：`finai.data_catalog` 缺失（已知，不立即修复）

| 字段 | 内容 |
|---|---|
| **现象** | `import finai.sources.capability_router` **冒烟通过**（exit 0，2026-08-29 实测）；但 `tdx_source.py` 的 TDX/5min 腿在**调用时**（非 import 时）会失败——`tdx_source.py:60/:67` 懒导入 `finai.tdx_minute5`，而 `tdx_minute5.py:23` 模块级 `import finai.data_catalog`，该模块未搬入新仓 |
| **证据** | MIGRATION_LIST.md ①-a 表：`finai/tdx_minute5.py:23` 模块级 `import finai.data_catalog`；`tdx_source.py:60/:67` 懒导入 `tdx_minute5`；冒烟结果：import 层面无错，因懒导入推迟到调用时才触发 |
| **修复要求** | 二选一：**A** 将 `data_catalog.py`（旧仓 1,284 行，纯 stdlib）最小裁剪后搬入，剥离其 catalog.sqlite 根假设；**B** 新仓第一版砍掉 TDX/5min 腿（`tdx_source` 的 tdx 腿、`stk_mins`、`auction` 不用），把 `tdx_minute5` 留在旧仓 |
| **验收判据** | 对 A：`tdx_source.connect()` 调用成功且 5min 拉取返回非空 DataFrame；对 B：`capability_router` 中 `lib='tdx'` 的候选被标记为不可用并有明确注释说明原因 |
| **优先级** | P1（影响 TDX 整条腿，但 import 层面不阻塞其他模块） |
| **冒烟实测记录** | `python -c "import finai.sources.capability_router"` → exit 0（2026-08-29，Windows / Python 3.11） |

---

## 登账说明

| 条目 | 来源 |
|---|---|
| R1 | 旧仓 `baostock_source.py:126` 实测（停牌脏行入库） |
| R2 | 旧仓 `tdx_source.py:87-95` 实测（5min 静默截断） |
| R3 | `artifacts/interface_matrix/auto_probe_results.json` 环境存疑记录 |
| R4 | MIGRATION_LIST.md §3（四条硬判据，⭐ 最贵） |
| R5 | 本骨架 C 阶段 import 冒烟结果（待填） |
