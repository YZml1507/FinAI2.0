# 晋级门禁清单（实验冠军 → 可实盘方案）

> 创建：2026-09-17 ｜ 依据 STRATEGY 二节：实验冠军晋级前必须通过
> 独立留出验证、±20% 参数扰动、成本/成交约束复核、基准对比。
> 当前候选构型：宽度冠军 + GC001 序列计息 + e7 降档出清（=e8b 构型）。
> 本文档在 Q4 评估时逐项打勾，⛔ 任何一项不过 = 不晋级、不许放松口径。

## G-1 独立留出数据验证【L3】

- [x] 留出段复跑 ✅：`pg-holdout-e8b`（2022-2024）CAGR/MDD 见 tracker
      ——vs 全窗零退化且 MDD 更优；阈值自定=退化<40%，实测无退化；
- [ ] 分段年收益表：pgy-* 逐年复跑在跑（10 组），出齐附 tracker。

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

- [ ] ±20% 矩阵（8 有效组）：❌ 悬崖敏感——attack/defense 维全低于
      对照 80%，仅 GC001 平移维平滑（数值见 tracker）；
- [ ] 处置=收窄到平台区：±10% 矩阵 pg2-* 10 组在跑，平台存在则记
      「±10% 平台内有效」条件性通过，不存在则 G-2 判负。

## G-3 成本/成交约束复核【L1】

- [x] 费率×2 ✅：`pg-fee2` CAGR 见 tracker——仍 > 对照，敏感温和；
- [x] 本金端点 ✅：`pg-cap10`（10 万）CAGR≈15万版（见 tracker），非特供；
- [x] 流动性约束 ✅：引擎成交量占比上限已建模（min_position/流动性
      门槛内建于撮合），跳过。

## G-4 基准对比【L1】

- [ ] vs 510880 红利 ETF 全收益、vs 沪深300：分年对照表；
- [ ] beta/alpha 分解更新（PROJECT_ASSESSMENT 口径）。

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
