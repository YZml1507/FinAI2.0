# REVALIDATE — FinAI2.0 已知缺陷登账（R1–R5）

> 生成时间：2026-08-29
> 状态：**R1✅ R3✅ R4✅ 已修复/关闭**；**R2 随 R5 方案 B 挂起**（离线校验已落地）；**R5✅ 已按方案 B 处置（砍腿）**
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

## R2 · TDX 腿静默截断缺陷 —— 状态：随 R5 方案 B 关闭/挂起

| 字段 | 内容 |
|---|---|
| **现象** | `tdx_source.py` 的 5min 拉取对单次响应超过 TDX 协议返回上限（约 800 根/次）的段**静默截断**——返回 DataFrame 行数 < 请求行数时无 WARNING、无 FAIL，上层读成"完整数据" |
| **处置** | 随 R5 方案 B 关闭/挂起。R2 的修复对象是 `fetch_bars` 的 5min/分钟腿；该腿 `connect()`/`_market_id()` 依赖未搬入的 `finai.tdx_minute5`/`finai.data_catalog`（= R5 根因），且 v1 纯日线（spec §2.2）。R5 砍腿后 `connect()` 恒 `ModuleNotFoundError`，分钟路径永不执行 ⇒ 失去修复对象。⚠ 与 `capability_router` 的 `tdx::daily_bar`（走 `tdx_daily_bar_adapter`，category 9 日线）是**不同腿**，不在本条 |
| **已落地（离线部分）** | 纯函数 `_assert_coverage(frames, *, requested, got)`（`tdx_source.py:77`）：`got < requested` 抛 `BarTruncationError`（`ValueError` 子类，携带 `truncated_bars = requested - got`），绝不静默返回部分数据（`base.py:22` 硬约束②）。已钉在 `fetch_bars` 分页尾部（`tdx_source.py:161`）作占位正确性，供 R5 方案 A 恢复腿时接回。离线单测 `tests/test_r2_truncation.py`（4 用例全绿） |
| **恢复腿时须补（已知未尽）** | ① 截断须显式归 `FAIL_DETERMINISTIC` 并写 `meta['truncated_bars']`——当前 `classify_exception` 对 `BarTruncationError` 文本归 `FAIL_UNREACHABLE`，需在 `except` 分支特判该异常直取 `FAIL_DETERMINISTIC` + meta；② `expected_min_bars` 按交易日历 × 48 根/日的判决层走 `segmented_pull.expected_sessions`（与本纯断言分工：本断言喂的是服务端实得计数） |
| **验收判据** | 离线已验：`_assert_coverage` 4 用例 + 全套 15 passed。真实链路的"两次调用 5min 3 年段 ⇒ FAIL_DETERMINISTIC"验收随腿一并挂起，待 R5 方案 A 恢复腿后复测 |
| **优先级** | 原为 P1 → **挂起**（随 R5）。若恢复腿则升回 P1 |

---

## R3 · 东财 push2his 接口本机不可达复验 —— ✅ 已复验关闭（2026-08-30）

| 字段 | 内容 |
|---|---|
| **现象（原始）** | 旧仓 `auto_probe_results.json` 曾记 `push2his.eastmoney.com` 为 `FAIL_UNREACHABLE`（连接超时），疑为本机网络/代理环境噪音而非源端不可用 |
| **复验结论** | **证伪旧记录，关闭**。当前仓 `artifacts/interface_matrix/auto_probe_results.json`（probed_at 2026-08-30T01:13:56Z）中 `push2his`/`push2` 命中 0，不存在独立 push2his `FAIL_UNREACHABLE` 项；efinance 37 接口 = 36 OK + 1 EMPTY_OK，0 FAIL。push2his 的三个直接客户端实测成功返回非空：`efinance.stock/common.get_quote_history`（OK, rows=8453，走 push2his `kline/get`）、`efinance.common.get_history_bill`（OK, rows=120，走 push2his `fflow/daykline/get`）。另：东财可用面 network_gate `datacenter-web 3/3`。直连 `https://push2his.eastmoney.com/api/qt/stock/kline/get` 实测 HTTP 200（境内站走代理会被断连，直连正常）。 |
| **证据** | 当前仓 probe JSON：efinance OK 36/37、push2his 0 命中、eastmoney `rate_limit_domain` 下 FAIL_UNREACHABLE 0 个；`efinance\common\getter.py:150 / :344 / :584`（push2his 三端点）；`network_gate` 东财可用面 3/3；直连 push2his 200（2026-08-30） |
| **判定依据** | contract：`FAIL_UNREACHABLE` 为「连接层失败——⛔ 不可解释，不得记为接口不可用」。旧仓该记录即属此类环境噪音；新仓接口级 OK（行数>0，不可伪造）+ 直连 200 双重确认源端可用 |
| **后续动作（转出 R3，另列）** | ① 5 个 akshare `*_em` 接口（`fund_money_fund_info_em`/`stock_gdfx_holding_teamwork_em`/`stock_ggcg_em`/`stock_gpzy_pledge_ratio_detail_em`/`stock_hold_management_detail_em`）为 `FAIL_UNREACHABLE` 且 host 归属 `endpoint_family=eastmoney / rate_limit_domain=None` 不一致 → 另列项复测；② efinance 未入 `scripts/probe_host_attribution.py` 的 eastmoney 家族，限流键对 efinance 不生效 → 治理侧补归属。**二者均不影响 push2his 可达判定** |
| **状态** | ✅ **已复验关闭**（2026-08-30，复核：r3-push2his 独立复核） |

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

## R5 · TDX/5min 腿不可调用：`finai.data_catalog` 缺失 —— ✅ 已按方案 B 处置（砍腿，2026-08-30）

| 字段 | 内容 |
|---|---|
| **现象** | `import finai.sources.capability_router` **冒烟通过**（exit 0）；但 `tdx_source.py` 的 TDX 腿在**调用时**（非 import 时）会失败——`tdx_source.py:93/:100` 懒导入 `finai.tdx_minute5`，而 `tdx_minute5.py:23` 模块级 `import finai.data_catalog`，两文件均未搬入新仓，触发即 `ModuleNotFoundError` |
| **处置裁决：方案 B（砍腿）** | ① 项目 v1 纯日线（spec §2.2 多仓/不加杠杆/仅日线，回测自 2015-01-01），分钟线腿本就用不上；② 方案 A 需搬入最小裁剪版 `data_catalog.py`（旧仓约 1,284 行纯 stdlib + 剥离 catalog.sqlite 根假设），重且 v1 用不上，是纯负债；③ 砍腿零回归——唯一活消费方 `capability_router.get()` 的 `tdx::daily_bar` 旁路经 `tdx_daily_bar_adapter.fetch()` 仍走 `tdx_source.fetch_bars()`，它把异常归入 `FetchResult`（`state=FAIL_PROBE_BUG`，`.ok=False`）返回非 OK 而**不抛**，故 `get()` 静默安全降级到下一候选 |
| **已落地标注（三处，腿留作钉位，文件未删）** | ① `tdx_source.py:3-22` 模块 docstring 顶部追加 R5 显著弃用横幅（v1 不可用、根因、`fetch_bars` 返回非 OK、方案 A 恢复路径、R2 接回要求）；② `capability_router.py:1094` `daily_bar` 的 `tdx::daily_bar` 候选 note 前置 R5 不可用标注（:1107-1112）；③ `__init__.py:37-47` `SOURCE_REGISTRY[tdx]`：`verified True→False`，note 改 R5 不可用标注（保留历史实测 48 根/日栅格合规作存档） |
| **透明披露** | 被砍的 `tdx::daily_bar` 是一条 **daily_bar 降级域**（category 9 日线、`ohlcv_daily` schema），非纯 5min 腿。砍掉它使 `daily_bar` 可用候选从 5 减到 4（剩 akshare/efinance/citydata/baostock）。仍正确——该腿本就因缺依赖恒失败、从未真返回数据，砍掉只是如实登记既有不可用，未减少任何实际能用的冗余 |
| **验收判据（方案 B）** | ✅ `capability_router` 中 `tdx::daily_bar` 候选已被标记为不可用并有明确注释说明原因。离线单测 `tests/test_r5_tdx_disabled.py` 4 用例全绿：import 成功 / `connect()` 抛 `ModuleNotFoundError` / `fetch_bars("000001")` 不抛且 `state==FAIL_PROBE_BUG` / router 候选含 R5 标注。全套 19 passed |
| **恢复路径（未来若需分钟线）** | 走方案 A：搬入裁剪版 `data_catalog.py` + 恢复 `finai.tdx_minute5`，撤销三处 R5 标注，`SOURCE_REGISTRY[tdx].verified` 改回 True；并接回 R2 的 `_assert_coverage`（`tdx_source.py:77` 已钉位）+ 补 `FAIL_DETERMINISTIC`/`meta['truncated_bars']` 特判 |
| **状态** | ✅ **已按方案 B 处置**（2026-08-30，实施：r5-tdx-limb） |

---

## 登账说明

| 条目 | 来源 |
|---|---|
| R1 | 旧仓 `baostock_source.py:126` 实测（停牌脏行入库） |
| R2 | 旧仓 `tdx_source.py:87-95` 实测（5min 静默截断） |
| R3 | `artifacts/interface_matrix/auto_probe_results.json` 环境存疑记录 |
| R4 | MIGRATION_LIST.md §3（四条硬判据，⭐ 最贵） |
| R5 | 本骨架 C 阶段 import 冒烟结果（待填） |
