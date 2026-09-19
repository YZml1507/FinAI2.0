# e13a 预登记：暴露等价对照臂（hard + mid_cap=0.5）

> 创建：2026-09-19 ｜ 状态：待发车（测试基线 1092 全绿 @ 9a7b359）
> 依据：`docs/audit/e11_linear_interim_finding_20260919.md` §三/§四——
> e11-linear 主实验的「唯一变量」在语义上混淆了**仓位形态**（阶跃 vs
> 斜坡）与**仓位水平**（mid 带 0% vs 49.6%）两个因素；本实验补齐
> 暴露等价对照臂，干净隔离「斜坡 vs 平台」。判据全部预先固定，
> ⛔ 不存在事后放宽的空间。

## 一、实验变量

- 唯一变量：`breadth_mid_cap = 0.50`（e8b 冠军构型为 0.0）；
  `breadth_weight_mode` 显式 `hard`（与默认一致，留痕）。
- 对照组：
  - `e11-linear`——**主对照**：CAGR 6.32% / MDD 24.40% / 换手 6.45， <!-- gate-doc-ignore: 历史快照（消融实验实测值，非基线声明），⛔ 不改史 -->
    暴露等价（70.74% vs 70.78%，Δ仅 0.04pp）⇒ 隔离形态因素；
  - `e8b-gc001-e7-combo`——次对照：CAGR 8.58% / MDD 17.40% / 换手 4.607， <!-- gate-doc-ignore: 历史快照（对照实验实测值，非基线声明），⛔ 不改史 -->
    形态相同（皆 hard）⇒ 隔离暴露因素。
- 同 universe 截面（stock_basic_cache）、同宽度序列
  （market-breadth-a/breadth20_daily.parquet）、同 SHA。

## 二、机制与归因链（因素分解）

e8b → e11-linear 的 CAGR 总差 −2.26pp 是两个因素之和：

```
exposure_effect = e8b − e13a   （同形态 hard，暴露 63.98%→70.78%）
shape_effect    = e13a − e11   （同暴露 ~70.7%，平台 vs 斜坡）
总差            = exposure_effect + shape_effect ≈ −2.26pp（恒等式约束）
```

R11 已实证收益对宽度呈 W 形（30-35% dead zone 10 年中 7 年负），
linear 的单调斜坡把仓位重心压在坏带上；hard+mid_cap=0.5 在
[defense, attack) 全带给恒定 50% 仓 ⇒ 两臂暴露相等但坏带内
**仓位分布形态不同**，shape_effect 度量这一差别。

## 三、运行命令（全部 `--set` 显式留痕）

```
.venv/bin/python scripts/lab/run_experiment.py --name e13a-midcap50 \
  --set use_breadth_timing=True --set breadth_defense_threshold=0.25 \
  --set breadth_attack_threshold=0.35 --set breadth_mid_cap=0.5 \
  --set breadth_ice_confirm_days=1 --set breadth_demote_liquidate=True \
  --set breadth_weight_mode=hard \
  --set cash_yield_series=data/rates/gc001_daily.parquet
```

## 四、验收判据（预先固定）

主判读（CAGR 三档，阈值为§五暴露分解的自然刻度）：

| 结果 | 裁决 |
|---|---|
| e13a ∈ [5.82%, 6.82%]（≈ e11 ± 0.5pp） | 损失几乎全部来自暴露 ⇒ **斜坡形态本身无害**；G-2 修复方向=「暴露中性的连续映射」（如 linear 端点下移/中点配平），路线存活 |
| e13a ≥ 8.08%（≈ e8b − 0.5pp） | 暴露增加几乎无损、损失全在形态 ⇒ **斜坡形态本身有害**；连续单调映射路线按「该实现下判负」记台账，非单调权重若再做必须预登记+PBO/WFA |
| e13a ∈ (6.82%, 8.08%) | 两因素各有贡献 ⇒ 按 §二 恒等式分解并登记各自 pp |

附属判据：

- **换手**：预期显著低于 e11-linear 的 6.45（mid 带恒定 cap ⇒ 无逐日
  目标漂移），落在 e8b 4.61 附近；若 >6.5 须登记异常并查 demote 出单；
- **MDD**：预期介于 17.40%~24.40% 之间；越界须登记；
- **恒等式校验**：exposure_effect + shape_effect 应 ≈ −2.26pp
  （容差 ±0.3pp，吸收非线性交互），超出容差 ⇒ 登记「存在交互项」
  不自圆其说。

## 五、暴露等价依据（沿用中间发现的实测口径，不重复计算）

真实宽度序列 2846 日日度目标仓均值（未跑回测纯计算）：
hard+mid_cap=0.5 ⇒ **70.78%**；linear+mid_cap=0 ⇒ **70.74%**； <!-- gate-doc-ignore: 历史快照（用户口径验证实测值，非基线声明），⛔ 不改史 -->
Δ=0.04pp ≪ 待分解效应量（2.26pp），暴露等价成立。

## 六、纪律备注

- hard 恒为默认值不动摇；本实验 `breadth_mid_cap=0.5` 仅经 `--set`
  显式开启，⛔ 不改默认构型；
- 本实验不回答「e13a 是否优于 e8b」的晋级问题（mid 带加仓方向与
  「警戒区零仓」既有裁决相反，预期 CAGR 走低）；它只为归因服务；
- 结果写 `experiments/lab/e13a-midcap50/`，登记 leaderboard +
  TASK_TRACKER，按 §四模板裁决。
