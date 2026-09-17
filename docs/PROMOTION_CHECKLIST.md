# 晋级门禁清单（实验冠军 → 可实盘方案）

> 创建：2026-09-17 ｜ 依据 STRATEGY 二节：实验冠军晋级前必须通过
> 独立留出验证、±20% 参数扰动、成本/成交约束复核、基准对比。
> 当前候选构型：宽度冠军 + e6 现金计息 +（待定）e7 降档出清。
> 本文档在 Q4 评估时逐项打勾，⛔ 任何一项不过 = 不晋级、不许放松口径。

## G-1 独立留出数据验证【L3】

- [ ] 全窗口拟合期外留出段复跑（建议：2015-2021 拟合 / 2022-2024 留出，
      或反向），CAGR/MDD 退化 ≤ 阈值（自定并写理由，建议 CAGR 退化 <40%）；
- [ ] 分段年收益表：不许出现「全靠某一年」结构（列逐年收益附在 tracker）。

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

- [ ] 27 组（或裁剪后 12 组）全部 ≥ 对照的 80%（CAGR），无悬崖；
- [ ] 悬崖敏感参数登记 + 处置（弃参或收窄到平台区）。

## G-3 成本/成交约束复核【L1】

- [ ] 换手×费率敏感性：佣金/印花税 ×2 复跑，CAGR 仍 > 对照；
- [ ] 最小成交额/最小头寸门槛在 10 万与 15 万本金两端点复跑
      （`initial_capital` 覆盖，确认不是 15 万特供）；
- [ ] 流动性约束：单票成交 ≤ 当日成交额 ×10%（引擎内已建模则跳过）。

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
