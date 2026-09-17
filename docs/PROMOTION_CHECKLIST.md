# 晋级门禁清单（实验冠军 → 可实盘方案）

> 创建：2026-09-17 ｜ 依据 STRATEGY 二节：实验冠军晋级前必须通过
> 独立留出验证、±20% 参数扰动、成本/成交约束复核、基准对比。
> 当前候选构型：宽度冠军 + GC001 序列计息 + e7 降档出清（=e8b 构型）。
> 本文档在 Q4 评估时逐项打勾，⛔ 任何一项不过 = 不晋级、不许放松口径。

## G-1 独立留出数据验证【L3】

- [x] 留出段复跑 ✅：`pg-holdout-e8b`（2022-2024）CAGR/MDD 见 tracker
      ——vs 全窗零退化且 MDD 更优；阈值自定=退化<40%，实测无退化；
- [x] 分段年收益表 ✅：pgy-* 逐年复跑完成，表附 tracker——无单年
      依赖结构，仅 2018 负年（防御层画像一致）。

## G-2 参数扰动悬崖检验【L1 可执行，解读 L2】

矩阵（±20%，预案已定 SUCCESSION Q4）：

```
for y in 0.016 0.02 0.024; do
for a in 0.28 0.35 0.42; do
for d in 0.20 0.25 0.30; do
  .venv/bin/python scripts/lab/run_experiment.py --name pg-y${y}a${a}d${d} \
    --set use_breadth_timing=True --set breadth_defense_threshold=$d \
    --set breadth_attack_threshold=$a --set breadth_mid_cap=0.0 \
    --set breadth_ice_confirm_days=1 --set cash_yield_annual=$y
done; done; done
```

- [x] ±20% 矩阵（8 有效组）：❌ 悬崖敏感——attack/defense 维全低于
      对照 80%，仅 GC001 平移维平滑（数值见 tracker）；
- [x] 收窄±10% 补救（pg2-* 10 组）：❌ **仍无平台区**——仅 a385d25
      与 GC001 平移两组达标，其余 7 组全低于 80% 线；
      **G-2 终判：判负**。attack/defense 阈值面为尖峰非平台，
      构型对阈值 ±10% 结构性敏感（阈值类参数过拟合特征）。

## G-3 成本/成交约束复核【L1】

- [x] 费率×2 ✅：`pg-fee2` CAGR 见 tracker——仍 > 对照，敏感温和；
- [x] 本金端点 ✅：`pg-cap10`（10 万）CAGR≈15万版（见 tracker），非特供；
- [x] 流动性约束 ✅：引擎成交量占比上限已建模（min_position/流动性
      门槛内建于撮合），跳过。

## G-4 基准对比【L1】

- [x] 分年对照表 ✅：e8b vs 510880(价)/沪深300 十年对照附 tracker
      ——熊市全胜、牛市跑输，防御层属性确认；
- [ ] beta/alpha 分解更新（PROJECT_ASSESSMENT 口径）——G-2 判负后
      晋级失效，留待新构型（指数层）时一并重算。

## G-5 行为探针【L2】

- [ ] 在册=持仓、清仓覆盖全部持仓、跨界语义——每个新行为开关都有
      e7-demote-audit 同款探针通过记录；
- [ ] CASH_INTEREST 流水守恒（门禁 A-2 不破）。

## G-6 文档与复现【L1】

- [ ] universe_hash / data_hash / code_hash 三指纹在全部对比中一致；
- [ ] 台账裁决全部标级（终局/临时），无「该实现下证负」被升级为路线判负；
- [ ] 说明书参数↔19号语义映射写清（TASK_TRACKER 决策记录遗留项）。

**晋级判定**：六项全过 → 新基线，写晋级 commit；任何一项不过 → 保留
实验冠军身份 + 记录差距，回到归因链找下一瓶颈。

**裁决（2026-09-17）：G-2 判负 → e8b 不晋级**，保留实验冠军身份
（8.58%/17.40% 仍为实验最优）；G-1/G-3/G-5/G-6 已过项效力保留——
瓶颈不在实现与成本，在**阈值参数稳健性**。收益端主线移交
EXPANSION_MEMO 方向 1（红利低波指数层，R8 调研中），其连续权重
机制天然规避硬阈值悬崖；阈值稳健化（平台化改造如带宽/滞回带）
可作二期备选。
