# e17 预登记：残余非筛选自由度盘点矩阵（持仓数/调仓节拍/权重形态）

> 创建：2026-09-20 ｜ 状态：已冻结待发车
> 依据：`docs/EXPANSION_MEMO.md` §三 裁决表——筛选类改造四次独立判负
> （e1-veto −1.02pp / C3 +0.66pp+MDD7.8 / D2 双轴全劣 / e16 −3.07pp），
> 「剩余自由度仅剩非筛选型（持仓数/调仓节拍/权重形态）」。
> 本矩阵是**盘点性质**（inventory），不是寻优——判据全部预先固定，
> ⛔ 不存在事后放宽的空间。

## 一、实验变量（每臂单一行为变量，对照锚 `isst-e8b`）

对照锚：`isst-e8b`（run `20260919-132626`）：CAGR 8.5814% / MDD 17.3990% /
换手 4.6074 / 156 笔 / fees 22.3k。 <!-- gate-doc-ignore: 历史快照（对照实验实测值，非基线声明），⛔ 不改史 -->
同 universe（487 静态池）、同宽度序列、同日历、同费用模型、同 GC001 现金
计息、同本金 15 万。

| 臂 | --set 覆盖 | 测的自由度 |
|---|---|---|
| e17-pos3 | `default_positions=3 min_positions=3` | 持仓数下限方向（5→3，集中度上升） |
| e17-pos8 | `default_positions=8 portfolio_target_count=8` | 持仓数上限方向（5→8，分散度上升；组合层截断数须同步覆盖，否则被 target_count=5 截回） |
| e17-rebal10 | `rebalance_days=10` | 调仓节拍加密（20→10，半月度） |
| e17-rebal40 | `rebalance_days=40` | 调仓节拍放宽（20→40，双月度） |
| e17-eqw | `weight_mode=equal` | 权重形态（市值加权→等权） |
| e17-dvw | `weight_mode=dividend_yield` | 权重形态（市值加权→股息率加权） |

**已识别伪自由度（不立项，登记理由）**：
- `candidate_pool_size`：仅作市值权重分母（top50 的 Σmc），组合层归一化
  后约去 ⇒ 对持仓/权重无实际影响，改动≈no-op；
- `warmup_bars`：冷启动窗，非收益机制；
- `min_dividend_yield`：筛选类参数（池内筛选已四次判负），非本矩阵范围。

## 二、实现（e17 新增机制）

- `weight_mode` 字段（`"market_cap"` 默认 / `"equal"` / `"dividend_yield"`，
  fail-closed 校验）：score=相对权重，组合层归一化；默认零影响；
- `--set` caster 新增：`weight_mode`、`min_positions`、`max_positions`、
  `portfolio_target_count`（嵌套 PortfolioConfig，同 portfolio_min_daily_amount
  机制）；
- 5 单测（默认市值加权逐值等价/非法拒/等权 score=1/dv 加权/Σmc=0 不阻塞
  等权）。基线 1114→1119。

## 三、运行命令（骨架）

```
COMMON="--set use_breadth_timing=True --set breadth_defense_threshold=0.25 \
  --set breadth_attack_threshold=0.35 --set breadth_mid_cap=0.0 \
  --set breadth_ice_confirm_days=1 --set breadth_demote_liquidate=True \
  --set cash_yield_series=data/rates/gc001_daily.parquet"
run_experiment.py --name e17-<arm> $COMMON --set <var>=<val>
```

## 四、验收判据（预先固定，逐臂判读）

ΔCAGR = 臂 − isst-e8b；噪声地板 ±0.4pp（C7 标定）：

| 结果 | 裁决 |
|---|---|
| ΔCAGR ≥ +1.5pp | **显著有效候选** ⇒ ⛔ 不直接晋级；须 ±20% 扰动矩阵 + 分年一致性复核后才可谈晋级（防单点侥幸） |
| ΔCAGR ∈ (+0.4pp, +1.5pp) | 弱证据登记，不动基线 |
| ΔCAGR ∈ [−0.4pp, +0.4pp] | 噪声带 ⇒ 不可分辨，登记为不显著 |
| ΔCAGR < −0.4pp | **判负**：该自由度方向有损 |

附属判据（每臂）：

- **换手**：rebal10 预期上升（加密节拍）、rebal40 预期下降；若 >8 登记异常；
- **MDD**：pos3（集中度↑）若 ΔMDD>+3pp 登记异常；pos8/eqw 若 ΔMDD>+3pp
  同登记；
- **持仓行为**：pos3 臂需确认实际持仓数=3（探针检查 average holdings）。

## 五、元裁决（矩阵级，预先固定）

- 若全臂 |ΔCAGR|<1.5pp（无显著正臂）⇒ **e8b 局部最优性坐实**——
  选股层非筛选自由度盘点关闭，残余研究面仅剩 D3（弱先验筛选）/
  D1（诊断）/资金分层容量件；
- 若有显著正臂 ⇒ 该方向进扰动复核（±20%），通过才谈基线变更；
  ⛔ 不许跨臂组合最优值直接造新构型（交互未测，组合属新实验须再登记）。

## 六、收单登记（2026-09-20 03:4x 全臂收齐）

| 臂 | CAGR | ΔCAGR | MDD | ΔMDD | 换手 | trips | 裁决 |
|---|---|---|---|---|---|---|---|
| pos3 | 4.02% | −4.57pp | 29.81% | +12.41pp | 4.51 | 134 | 判负（集中度方向有害+MDD异常） | <!-- gate-doc-ignore: 历史快照（消融实验实测值，非基线声明），⛔ 不改史 -->
| pos8 | 9.63% | +1.05pp | 15.17% | −2.23pp | 4.83 | 167 | **弱正**（落 +0.4~+1.5 带） | <!-- gate-doc-ignore: 历史快照（消融实验实测值，非基线声明），⛔ 不改史 -->
| rebal10 | 6.43% | −2.15pp | 22.50% | +5.10pp | 6.73 | 282 | 判负（换手反噬+MDD异常） | <!-- gate-doc-ignore: 历史快照（消融实验实测值，非基线声明），⛔ 不改史 -->
| rebal40 | 4.62% | −3.96pp | 13.40% | −4.00pp | 2.86 | 69 | 判负（节拍拉长丢收益） | <!-- gate-doc-ignore: 历史快照（消融实验实测值，非基线声明），⛔ 不改史 -->
| eqw | 4.16% | −4.42pp | 14.76% | −2.64pp | 3.86 | 248 | 判负（top5 内等权摊薄到大票外） | <!-- gate-doc-ignore: 历史快照（消融实验实测值，非基线声明），⛔ 不改史 -->
| dvw | 3.26% | −5.32pp | 17.51% | +0.12pp | 3.69 | 241 | 判负（dv 加权=加倍逆向选择尾部） | <!-- gate-doc-ignore: 历史快照（消融实验实测值，非基线声明），⛔ 不改史 -->

## 七、矩阵级裁决

- **节拍轴**：10d/40d 双负 ⇒ 20d 局部最优确认。
- **权重轴**：市值加权 ≫ 等权 ≫ dv 加权（8.58 / 4.16 / 3.26）——
  mc 加权恰是「大票=更稳分红主体」的隐含质量倾斜，等权把它摊薄
  到尾部即亏；与 D1「dv 排序无残差、暴露才值钱」一致。
- **持仓数轴**：pos3 判负、**pos8 +1.05pp 弱正**——但 e18-pos10
  （其 +25% 邻域，e18 矩阵内已测）Δ=−2.85pp ⇒ **孤峰非平台**，
  按预登记 §五「±20% 扰动复核」口径**未通过** ⇒ pos8 不晋级、
  不回溯。三轴合计 5 判负 + 1 个非稳健弱正 ⇒ **e8b 局部最优性
  坐实**，非筛选自由度盘点关闭（宽度轴外延由 e18 补枪确认）。
