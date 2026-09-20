# e19 预登记：D7 拥挤度熔断（利差路 roll3y 分位）

> 创建：2026-09-20 ｜ 状态：已冻结待发车
> 依据：`docs/EXPANSION_MEMO.md` 方向链 + D7 影子探针
> （/tmp/d7_probe/{d7_shadow.py,d7_result.json,CONCLUSION.md}）——
> 利差拥挤度路**双闸通过**（预测力 + 假想熔断改善回撤），成交占比路否决。
> 本实验把影子层信号落到真实引擎（demote/ice/warmup/市值加权全语义），
> 判据全部预先固定，⛔ 不存在事后放宽空间。

## 一、信号定义（冻结，与影子探针同构）

- 池股息率面板：487 静态池 `data/dividend_stocks/{sym}/{year}.parquet`
  的 `dividend_yield` 列（PIT 滚动口径，既有数据面）。
- **利差（口径 a1，冻结主口径）**：`spread[T] = mean(dv[T] | dv[T]≥3%) − yield10[T]`
  （百分点；mean 只取当日 dv≥3% 成员——与策略候选池同语义）。
- **拥挤度**：`crowd[T] = −spread[T]`（利差越窄=越拥挤）。
- **roll3y 相对分位（冻结）**：`crowd_pct[T] = crowd 在 [T−756, T] 窗内的
  百分位`，min_periods=504；序列约 2017-01 起覆盖（2015-2016 裸奔段
  结构盲区，影子探针已声明）。
- **PIT 约定（与 breadth20_daily 同构）**：`crowd_pct[T]` 仅用 ≤T 收盘
  数据计算，T 日决策、T+1 开盘成交 ⇒ 无前视。
- 序列离线构建：`scripts/lab/build_crowding_series.py` →
  `data/macro/crowding_roll3y_daily.parquet`（date, crowd_pct），
  指纹与构建参数入清单。

## 二、引擎接入（单一行为变量：攻击日拥挤熔断）

- 新增配置（默认关，fail-closed）：
  `use_crowding_breaker: bool = False`；
  `crowding_series: Mapping[str, Decimal]`（iso date → pct，缺失日=中性不熔断）；
  `crowding_threshold: Decimal = 0.85`；`crowding_cap: Decimal = 0.5`。
- 语义：调仓计划日（含 demote 触发的非节拍调仓）在计算 `breadth_cap`
  之后，若当日 `crowd_pct > threshold` ⇒ `total_nav ×= crowding_cap`。
  e8b 构型下 `breadth_mid_cap=0` ⇒ 熔断只在 attack 档实际生效
  （mid/ice 档本来就零仓），与影子探针「attack 日熔断」语义一致。
- **不接事件驱动减仓**：非调仓日不主动清（拥挤段以周-月计持续，
  20d 节拍抽样捕获大部分效应；影子层 78/1525 attack 日触发，实际
  引擎内触发点=落入拥挤段的调仓日，预期 ~5-10 次）。差额留现金，
  照常计 GC001 息。
- 择时互斥纪律沿用：`use_crowding_breaker` 必须与 `use_breadth_timing`
  同开（熔断是宽度择时的叠加层，无宽度序列时启用 → fail-closed 拒配）。

## 三、实验矩阵（4 臂，对照锚 `isst-e8b-fix688` 修复后锚点）

| 臂 | 覆盖 | 测点 |
|---|---|---|
| e19-crowd | `use_crowding_breaker=True` + `_crowd_file=data/macro/crowding_roll3y_daily.parquet` | 主臂（a1 口径、thr=0.85、cap=0.5） |
| e19-crowd-t80 | 同上 + `crowding_threshold=0.80` | 阈值左邻域（平台性） |
| e19-crowd-t90 | 同上 + `crowding_threshold=0.90` | 阈值右邻域（平台性） |
| e19-crowd-a2 | 同上 + `_crowd_file=…crowding_roll3y_daily_a2.parquet` | 口径稳健性（全池中位 dv） |

公共参数与 e8b 全同：breadth 0.35/0.25/mid_cap=0/ice=1/demote、
GC001 现金计息、本金 15 万、同 universe、同日历。

## 四、验收判据（预先固定）

Δ 相对 `isst-e8b-fix688`；噪声地板 ±0.4pp（C7 标定）。
本机制是**防御性熔断**，主指标=MDD，收益指标为不恶化约束：

| 结果 | 裁决 |
|---|---|
| ΔMDD ≤ −1.0pp **且** ΔCAGR ≥ −0.4pp | **熔断有效** ⇒ 叠加阈值平台（t80/t90 同向）与口径稳健（a2 同向）通过后才谈晋级；仍须 ±20% 扰动矩阵（对 threshold/cap）复核 |
| ΔMDD ≤ −1.0pp 但 ΔCAGR < −0.4pp | 防御有效但代价过高 ⇒ 弱证据登记，不晋级 |
| ΔCAGR ≥ +0.4pp 且 ΔMDD 不劣化 | 视作有效候选（收益端意外正贡献），同走扰动复核 |
| 主臂 ΔMDD ∈ (−1.0pp, +0.4pp] 且 ΔCAGR 在噪声带 | 不可分辨 ⇒ 登记不显著，机制关闭 |
| ΔMDD > +0.4pp 或 ΔCAGR < −0.4pp | **判负**：熔断在该构型下净有害 |

附属判据（每臂）：熔断触发次数须 >0（=0 为配置失效需查）、
换手增幅 ≤ +1.0（熔断只减不加，换手应基本持平或微升）、
费用增幅 ≤ +20%。

## 五、口径限制声明（预登记固定披露）

1. roll3y 需 ~2 年暖机：2015-2016 股灾段无覆盖，全窗 MDD 不期望改善
   （影子层全窗 MDD 不变即此因）——判据落在全窗指标上属保守口径。
2. 重叠窗 t 值虚高（影子层已声明）；本实验以端到端 ΔCAGR/ΔMDD 为准，
   不以 fwd-return t 值作晋级依据。
3. 影子层用池等权≠引擎 top5 市值加权——影子 CAGR 量级不可比，方向
   可信、量级偏保守（抱团瓦解优先打击头部票）。
4. 阈值 0.80~0.90 间影子 CAGR 摆动 ±1pp（非单调）——故阈值扰动臂
   直接进矩阵，不允许事后挑最优阈值。
5. 688 单位修复与熔断信号无关（信号用 dividend_yield 列，非
   volume/amount）；但锚点已换为修复后数据，全矩阵同锚可比。

## 六、收单登记（2026-09-20 回填）★D7 拥挤度熔断判负/不可分辨→关闭

**运行环境**：新机（Ubuntu 8 核/31G，Python 3.11.15，pytest 1127 绿）。
`data/` 由 Release `data-20260920` 恢复（MD5 校验通过）；
`experiments/lab/market-breadth-a/breadth20_daily.parquet` 不在 Release 内， <!-- gate-doc-ignore: 历史快照（运行期产物路径，本机已重建、不入库），⛔ 不改史 -->
**由 leaderboard.jsonl `isst-e8b` 记录 `overrides.breadth_series` 逐值重建**
（98 条 e1~e18 记录该字段字节一致；`_load_breadth_series` 回读与原字典
逐键逐值相等；PROVENANCE.md 登记 sha256）——非重新计算，零口径漂移。
`scripts/repair_688_unit_fix.py --dry-run` 残留 0 行。

**锚点复测**：`isst-e8b-fix688-v2` = CAGR 8.5814% / MDD 17.3990% / <!-- gate-doc-ignore: 历史快照（e19 锚点复测实测值，非权威基线声明） -->
trips 156 / fees 22328.60 / 换手 4.6074——与原锚及 fix688 记录**逐值 Δ=0**， <!-- gate-doc-ignore: 历史快照（e19 锚点复测实测值，非权威基线声明） -->
本节 Δ 同时相对新机锚与历史锚成立。

| 臂 | CAGR | MDD | trips | fees | 换手/年 | ΔCAGR | ΔMDD | Δfees | Δ换手 | 裁决 |
|---|---|---|---|---|---|---|---|---|---|---|
| e19-crowd (t0.85/A1) | 7.8857% | 17.4565% | 153 | 20801.88 | 4.4561 | −0.696pp | +0.058pp | −6.8% | −0.151 | **判负**（CAGR 降幅 >0.4pp，MDD 未改善） | <!-- gate-doc-ignore: 历史快照（e19 实验臂实测值，非权威基线声明） -->
| e19-crowd-t80 | 7.9269% | 16.8668% | 152 | 20009.00 | 4.3106 | −0.655pp | −0.532pp | −10.4% | −0.297 | **判负**（MDD 改善 <1.0pp 且 CAGR 降幅 >0.4pp） | <!-- gate-doc-ignore: 历史快照（e19 实验臂实测值，非权威基线声明） -->
| e19-crowd-t90 | 8.5907% | 17.4850% | 155 | 22098.67 | 4.5253 | +0.009pp | +0.086pp | −1.0% | −0.082 | **不可分辨**（噪声带内，关闭） | <!-- gate-doc-ignore: 历史快照（e19 实验臂实测值，非权威基线声明） -->
| e19-crowd-a2 (t0.85/A2) | 8.5987% | 17.4818% | 156 | 21939.65 | 4.4764 | +0.017pp | +0.083pp | −1.7% | −0.131 | **不可分辨**（噪声带内，关闭） | <!-- gate-doc-ignore: 历史快照（e19 实验臂实测值，非权威基线声明） -->

**副条件**：换手/费用四臂均下降（满足 ≤+1.0 / ≤+20%）；触发次数引擎
未持久化 `_crowd_break_count`（口径限制），以「四臂 fees/trips 均偏离锚点」
作为触发>0 的间接证据，并登记 crowd_pct>阈值的交易日上界：A1 >0.80 188d /
>0.85 126d / >0.90 68d，A2 >0.85 132d（1928 个有效日；2017-01-24 前为
滚动窗热身盲区）。

**结论**：
1. 影子探针的「假想熔断 post-MDD 22.2%→18.3~19.6%」**未在实测回测中兑现**：
   实测 MDD 在 t0.85/t0.90/A2 三臂反而 +0.06~0.09pp，t0.80 仅 −0.53pp。 <!-- gate-doc-ignore: 历史快照（e19 实验臂实测值，非权威基线声明） -->
   推断（未做逐段分解，标注为解释性假设）：熔断只在攻击态调仓日按
   cap=0.5 压缩目标 NAV，锚点的最大回撤段大概率不与高拥挤分位重叠；
   被削掉的主要是拥挤期内仍在延续的上涨暴露。
2. 高拥挤分位 ⇒ 池 fwd60 弱势（影子层 t≈−2.03）在**个股执行层/择时叠加下
   不可转化为组合级防御**——与 GAP 文档「任何粒度择时叠加均负贡献」同向。
3. 阈值方向单调：阈值越低（越常触发）代价越大（t0.80/t0.85 −0.66~−0.70pp），
   阈值越高退化为无操作（t0.90/A2 ≈0），**不存在正效应平台**，不做 ±20% 扰动。
4. **D7 方向关闭。`use_crowding_breaker` 保持默认 False，代码保留为负结果
   可复现载体，不晋级。** 锚点 isst-e8b（8.58%/17.40%）维持最终基线。 <!-- gate-doc-ignore: 历史快照（锚点实测值，非权威基线声明） -->

产物：`experiments/lab/{isst-e8b-fix688-v2,e19-crowd,e19-crowd-t80,e19-crowd-t90,e19-crowd-a2}/`
+ leaderboard.jsonl 追加 5 行。
