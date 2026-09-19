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

## 六、收单登记（待回填）
