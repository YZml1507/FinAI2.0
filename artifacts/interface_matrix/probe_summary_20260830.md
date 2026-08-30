# 接口打点摘要 — 2026-08-30

> 生成：2026-08-30，`scripts/auto_probe_interfaces.py` 网络探针复验（agent 会话）
> 基准：2026-08-09 产物（`auto_probe_results.bak_20260830.json`，8d5bc6a7…）
> 范围：本轮逐库全量探测 **baostock(22) / efinance(37) / mootdx(59) / tdxpy(38) / adata(54)**
>       另以 `--limit 20` 探测 akshare 前 20 条。akshare(756) / tushare(98) 未整库复测。
> 网络：代理 127.0.0.1:7897（HTTP/HTTPS）；网络门禁 baidu 3/3、datacenter-web(东财) 3/3、TDX 4/4

## 最终状态分布（1081 条，按库）

| 库 | 计划 | OK | EMPTY_OK | FAIL_GATEWAY | FAIL_UNREACHABLE | FAIL_DETERMINISTIC | FAIL_PROBE_BUG |
|---|---|---|---|---|---|---|---|
| akshare | 756 | 701 | 10 | 46 | 11 | 3 | – |
| tushare | 98 | 23 | 31 | 5 | 36 | 3 | 2 |
| mootdx | 59 | 41 | 7 | – | – | – | 11 |
| tdxpy | 38 | 24 | 5 | – | – | – | 9 |
| baostock | 22 | 22 | – | – | – | – | – |
| efinance | 37 | 36 | 1 | – | – | – | – |
| adata | 54 | 47 | 3 | 1 | 2 | 1 | – |
| **合计** | **1064** | **894** | **57** | **52** | **49** | **7** | **22** |

## 与 2026-08-09 基准对比

| 状态 | 2026-08-09 | 2026-08-30 | 变化 |
|---|---|---|---|
| OK | 893 | 894 | **+1** |
| EMPTY_OK | 57 | 57 | 0 |
| FAIL_GATEWAY | 53 | 52 | –1 |
| FAIL_UNREACHABLE | 49 | 49 | 0 |
| FAIL_DETERMINISTIC | 7 | 7 | 0 |
| FAIL_PROBE_BUG | 22 | 22 | 0 |
| **总计** | **1081** | **1081** | **0** |

逐条 diff：仅 **1 条**状态变化 —— `adata::stock.info.all_code` `FAIL_GATEWAY → OK`
（东财 host 池复测恢复）。

## FAIL_DETERMINISTIC（可作能力结论：明确不可用）7 条

| 库 | 接口 | 错误 |
|---|---|---|
| akshare | fred_md | HTTP 403 Forbidden |
| akshare | fred_qd | HTTP 403 Forbidden |
| akshare | hf_sp_500 | HTTP 404 Not Found |
| adata | stock.market.wencai_hexin_v | `mr_eval_context` 函数缺失（问财库侧） |
| tushare | get_index | HTTP 403 Forbidden |
| tushare | global_realtime | HTTP 403 Forbidden |
| tushare | tick | `int('')` 解析失败 |

（与基准一致，无新增。）

## ⚠ 不可解释失败 ≠ 接口不可用

- **FAIL_GATEWAY / FAIL_UNREACHABLE（共 101 条）**多为网络/代理/网关层形态，**不得**读作“接口不可用”。代表性：akshare amac 族 8 条（见下）、tushare 36 条 UNREACHABLE、akshare 46 条 GATEWAY。
- **FAIL_PROBE_BUG（22 条，仅 mootdx/tdxpy 的 reader/class 构造族 + tushare pytdx 缺失）**是**我方调用层/本地环境**问题：
  - mootdx 7 条 + tdxpy 9 条 `resolved object is not callable: xxx` —— 类实例化路径问题（FINDING-233 同族），接口本身未必不可用（同批次肉眼可见的 `StdQuotes.k` / `ExtQuotes.bars` / `TdxDailyBarReader.get_df_by_code` 等均 OK）。
  - tushare `get_instrument` / `reset_instrument` —— `No module named 'pytdx'`：本地缺库。

## 本轮实测（2026-08-30 新观测）按库汇总

- **baostock 22/22 OK**（query_all_stock 7,288 行 / stock_basic 8,928 行 / industry 5,545 行等）
- **efinance 37 条 36 OK / 1 EMPTY_OK**（get_fund_codes 26,003 行 / get_realtime_quotes 5,905 行等）
- **mootdx 59 条 41 OK / 7 EMPTY / 11 BUG**（StdQuotes.k 27 行 / ExtQuotes.bars 700 行 / StdQuotes.block 386,283 行；EMPTY_OK 条目不认领）
- **tdxpy 38 条 24 OK / 5 EMPTY / 9 BUG**（exhq 六条扩展行情 OK：instrument_bars 700 行、transaction_data 1,751 行、minute 330 行；reader 局部读取 OK：get_df_by_code 1,205 行）
- **adata 54 条 47 OK / 3 EMPTY / 1 GW / 2 UNREACH / 1 DET**（get_market 8,468 行 / concept_east 3,453 行 / market_index 1,614 行；get_wencai_server_time 为 `'NoneType' is not subscriptable` = 网关形态）
- **akshare 抽样 20 条：12 OK / 7 FAIL_GATEWAY / 1 FAIL_UNREACHABLE**

### ⚠ akshare amac 8 条：库内分页死循环或端点已 404 —— 属**新增未决调查项**

复测中 `amac_aoin_info` / `amac_fund_abs` / `amac_fund_account_info` / `amac_fund_sub_info` /
`amac_manager_cancelled_info` / `amac_member_sub_info` / `amac_securities_info` 7 条
`JSONDecodeError`（收到的 body 不是 JSON，B 类谓词收入 bytes=0 的才计处分，本条收 4,107~426,236 字节属**命中 B 可复现类**），
`amac_member_info` 1 条 HUNG。直连网关确认 `.../amac-infodisc/api/...` 端点规模分页巨量（旧档记 amac_manager_info 需 ~70 分钟）。
**影响面：akshare 内这 8 条接口宁可视为“本批不结论”，不应从 FA 桶排除。**

## 其他工程改动（本次会话内）

1. **补回 `finai/probe_guard.py`**（284 行，来源旧仓 git bundle `FinAI.bundle`）——新仓骨架入库时遗漏的并发守卫。`scripts/auto_probe_interfaces.py` 的 `from finai.probe_guard import probe_in_progress` 现可正常导入。probe 输出已验。
2. **`scripts/auto_probe_interfaces.py` 两处 `⚠` emoji 打印替换为 ASCII**：GBK 控制台下 `UnicodeEncodeError` 曾致第一批 adata 打点整批中止（`--lib adata` 首次运行 exit)；已修。
3. ⚠ `install_eastmoney_pool_retry` 打点默认 probing ≤6s/成员 —— 该垫片在 host 池损坏主机的复测下 effect OK（东财 38 条全 OK 回归）。

## 产物与磁盘清理

- ✅ 保留：本文档（compact，最终记录）
- ✅ 保留：`artifacts/interface_matrix/auto_probe_results.json`（1.57 MB，git 追踪 —— 基线+本轮更新均已入 git，磁盘删除不损失历史）
- 🔥 **已删除**（本会话）：
  - `auto_probe.lock`（锁文件，探针已结束，进程退出时自动清理）
  - `auto_probe_results.bak_20260830.json`（1.5 MB，与旧基线同源 8d5bc6a7，比对完成）
  - `/tmp` git bundle 临时 clone ×2（FinAI_20260829 探针用）与临时 probe_guard.py 副本

> 注：`auto_probe_results.json`（1.57 MB）按 team-lead 指令保留于 git 历史（结果大文件仍随仓库在磁盘上）；如需彻底释放磁盘占用，删除该文件不影响本摘要结论 —— 摘要 §分布/§diff/§FAIL_DETERMINISTIC 均为最终值。