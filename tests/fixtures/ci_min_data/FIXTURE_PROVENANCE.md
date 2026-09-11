# CI 最小数据 fixture（tests/fixtures/ci_min_data）

## 它是什么

`data/**` 被 `.gitignore` 排除 ⇒ GitHub Actions 上 `data/dividend_stocks/` **完全不存在**
（本地 488 只标的 / CI 0 个）⇒ D-1~D-4 判 INCONCLUSIVE、
G-1 的 `data_hash` 取不到 ⇒ CI 永久红。**红的是"没数据"，不是"数据有问题"**。

本 fixture 让 CI 有**真东西可判**：⛔ 不是给门禁加豁免，⛔ 没有放宽任何阈值/判据。

## 抽样来源与规模

| 项 | 值 |
|---|---|
| 来源 | `data/dividend_stocks`（真实采集，RAW 不复权） |
| 源采集时间 | 2026-09-07T14:41:30.163266 |
| 抽样规则 | 2024 年 >= 130 个交易日且切片末行 market_cap > 0 的标的，按代码字典序取前 30 只（⛔ 不做质量筛选） |
| 年份 | 2024 |
| 标的数 | 30 |
| 每标的交易日 | 130（真实行原样拷贝，未改数值） |
| 列结构 | 与真实 parquet **逐列一致**（17 列，含 `market_cap`/`dividend_yield`/`tradestatus`） |
| 除权 sidecar | `exdiv/<symbol>.parquet` 原样拷贝 |

## ⛔ 合成成分（唯一非真实部分，必须可见）

真实数据 **2015-2024 全部 488 只标的 / 3754 个 symbol-year 的 `tradestatus` 恒为 `'1'`**
—— 实测**没有任何停牌日**可抽样。若 fixture 不含停牌日，D-4 会判 **SKIP（不适用）**，
等于 CI 上这条门禁没在判。故对字典序前 3 只标的（含 D-1/D-4 实际抽样的第一只）
各注入 **3 个合成停牌日**，按停牌语义构造：

```
tradestatus='0'、volume=0、amount=0、turn=0、pctChg=0、OHLC=preclose
```

（其后一交易日的 `preclose` 同步修正，保持价格链自洽。）

注入标的与日期：见 `fixture_manifest.json::synthetic_suspension_days`。

## ⛔ 边界（不许含糊）

CI 上数据类门禁校验的是**这份抽样小样** —— **不等于**校验全量真实数据质量。
全量校验仍须在本地 `data/` 上跑同一条命令：

```bash
py -3.11 -m scripts.gates.gate_master_audit --ci
```

本地有真实数据时，门禁**优先取真实数据**，本 fixture 不参与（行为完全不变）。

## 重新生成

```bash
py -3.11 scripts/build_ci_fixture_data.py
py -3.11 scripts/build_ci_fixture_data.py --check
```
