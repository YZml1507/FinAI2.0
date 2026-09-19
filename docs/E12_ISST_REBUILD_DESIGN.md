# E12-isST 重建设计（预登记，数据层语义变更——先登记后动）

> 创建：2026-09-19 ｜ 状态：设计冻结，待 namechange 采集完成 + 本文档评审后实施
> 依据：docs/audit/data_layer_risk_sample_gaps_20260919.md §三/§四（isST 恒 0 ⇒
> ST 触板规则静默失效，暴露 1070 日）；PLAYBOOK 纪律「任何数据层语义变更 ⇒
> 整批同 SHA 重跑 + 更新基线 + 登记决策」。

## 一、缺陷（已实锤）

- `data/dividend_stocks/*/bars` 的 `isST` 列恒 `'0'`（采集器腾讯源无该字段，硬编码 0）；
- `data/cleaner.py::mark_limit_flags` 依 `isST=='1'` 选 5% 档 ⇒ ST 票按 ±10% 判触板；
- 后果：`backtest/matching.py`「涨停买入不可成交/跌停卖出不可成交」对 ST 票
  ±5% 触板日**静默失效**（现池 ST 名称票 5% 档日数 1070 vs 真 ±10% 档 1008，同量级）。

## 二、修复方案

**数据源**：tushare `namechange`（datahubco 代理，已在采 → `data/namechange/namechange.parquet`），
字段 ts_code/name/start_date/end_date/ann_date/change_reason。

**重建规则（PIT，名称生效区间口径）**：

```
isST(date) = 1  iff  ∃ 名称史行: start_date <= date <= (end_date or +∞)
                     and 'ST' in name.upper()
```

- 用 **start_date/end_date（生效区间）** 而非 ann_date：名称生效日才是交易规则
  切换日（5% 档从生效日起算）；ann_date 仅作审计留痕。
- `*ST`/`ST`/`退` 均含 ST 字样 ⇒ 统一命中；摘帽（end_date 非空）后恢复 0。
- 区间空缺（namechange 无行的早期）⇒ isST=0（与现状一致，不制造不存在的标记）。

**写入目标（两个独立产物，按序）**：

1. `data/dividend_stocks/*/bars`（权威目录，487 只）——**语义变更**，回写后
   universe/data hash 变化 ⇒ 受影响实验整批同 SHA 重跑 + 基线更新登记；
2. `experiments/lab/market-breadth-a/daily_bars + delisted_bars`（宽度宇宙，
   5475 只）——供未来宽度口径使用（当前宽度计算不消费 isST，先写备着）。

**实施形态**：`scripts/lab/rebuild_isst_from_namechange.py`（幂等、原子写、
每票 tmp→rename、跑前跑后 hash 留痕、干跑 `--dry-run` 模式先出影响面报告）。

## 三、验收断言（探针先于重跑）

1. 现池 16 只 ST 名称票：重建后 5% 档日数（0.048~0.052）从「未标记」变为
   「已标记 limit_up/down」——1070 暴露日中处于 ST 生效区间内的必须 100% 覆盖；
2. 非 ST 票/非 ST 期间：isST 全 0（与现状逐值一致，防误标扩散）；
3. *ST柳化（sh.600423）抽验：2019-12-20 ~ 2021-05-19 = ST、2026-04-28 起 = *ST，
   区间内 isST=1、区间外 0；
4. mark_limit_flags 在重建后数据上对 ST 生效日按 5% 档判触板（单测口径复核）；
5. 幂等：重跑二次产物 hash 不变。

## 四、影响面与重跑纪律

| 影响 | 处置 |
|---|---|
| universe_hash/data_hash 变化 | 受影响实验整批同 SHA 重跑（G-REPRO-1） |
| e8b/e11-linear 等历史数值 | 大概率微调（ST 票触板日成交被禁）⇒ 更新基线 + 登记「为何改历史」 |
| e11-linear 16 组 | **不重跑旧口径**；若 e11 收单在 isST 重建后，须用新数据同 SHA 重跑整批 |
| 宽度序列 | 不消费 isST ⇒ 无需重算（debiased 版亦同） |

⛔ 顺序约束：namechange 采集完成 → 本设计评审 → 实施重建 → 探针全过 →
再启动任何「依赖 isST 正确性」的实验重跑。