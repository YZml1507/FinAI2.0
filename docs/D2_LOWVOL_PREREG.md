# D2 预登记：池内低波翼——波动率升序截断叠 e8b 栈

> 创建：2026-09-19 ｜ 状态：待发车（可行性测算已过——筛子有牙）
> 依据：`docs/DEEP_RESEARCH_DIVIDEND_20260919.md` §七 D2（VOL 中国最强
> 单因子证据链）+ `docs/DEEP_ANALYSIS_GAP_20260919.md`（MDD 是结构性
> 痛点）+ e15 placebo 收单（选股层=alpha 主载体 ⇒ 在选股层内部改）。
> 判据全部预先固定，⛔ 不存在事后放宽的空间。

## 一、实验变量

- **唯一变量**：`low_vol_keep_pct=0.5`——攻击档选股在 dv≥3% 合格
  候选内，先按 trailing-250d 日收益波动率升序保留前 50%，再按
  股息率排序取 top5、市值加权。其余全部同 e8b 冠军构型。
- **对照组**：`isst-e8b`（run `20260919-132626` / `20260917-160223`
  逐值一致）：CAGR 8.5814% / MDD 17.3990% / 换手 4.6074 / 156 笔。 <!-- gate-doc-ignore: 历史快照（对照实验实测值，非基线声明），⛔ 不改史 -->
- 同 universe、同宽度序列、同日历、同费用模型、同 GC001。

## 二、机制假设与可行性（先行测算，本仓数据面实测）

假设：高股息率候选中存在「困境高息」尾部（股息率高=价格深跌=
高波动），截掉候选内波动率上半区可降组合波动与 MDD，且不显著
损失收益（VOL 因子在中国为最强单因子之一，见深研 §二）。

可行性测算（`data/dividend_stocks` 491 分区 trailing-250d vol，
99 个 attack 档月度快照，top5-dv 票的池内波动率分位）：

- top5-dv 票平均 vol 分位 **0.537**（p10=0.40 / p50=0.55 / p90=0.66）
- top5 中高于池内中位 vol 的票数均值 **2.75/5**
- 判定：筛子有牙（截 50% 平均换掉 ~2-3/5 票）但非极端——
  预期改变持仓构成、检验 MDD 假说，非 no-op。
- 数据面：pctChg 在 `dividend_stocks` 分区全覆盖；vol 由策略内
  250 日滚动缓冲自算（close/preclose−1），PIT 正确、零外部依赖；
  缓冲 <200 日的票 fail-closed 排除（无 vol 史=无法验证低波）。

## 三、运行命令

```
.venv/bin/python scripts/lab/run_experiment.py --name d2-lowvol50 \
  --set use_breadth_timing=True --set breadth_defense_threshold=0.25 \
  --set breadth_attack_threshold=0.35 --set breadth_mid_cap=0.0 \
  --set breadth_ice_confirm_days=1 --set breadth_demote_liquidate=True \
  --set cash_yield_series=data/rates/gc001_daily.parquet \
  --set low_vol_keep_pct=0.5
```

## 四、验收判据（预先固定）

主判读（本实验为 **MDD 侧改进**，收益侧设不劣化红线）：

| 结果 | 裁决 |
|---|---|
| ΔMDD ≤ −2.0pp 且 ΔCAGR ≥ −1.5pp | **有效**：低波翼显著降回撤且不实质损收益 ⇒ 晋级候选构型，进扰动/PBO 复核 |
| ΔMDD ∈ (−2.0pp, 0] 且 ΔCAGR ≥ −1.5pp | 弱证据：方向对但量级不足 ⇒ 登记，不追加调参 |
| ΔMDD > 0 或 ΔCAGR < −1.5pp | **判负**：低波翼未降回撤或代价过大 ⇒ 关闭该线索 |

附属判据：

- **换手**：预期 ≥e8b（候选池缩窄→成分更替增多）；若 >8 须登记；
- **选股集中度**：候选数减半后 top5 是否更集中于超大盘
  （低波↔大市值相关性）——抽查持仓市值分位变化；
- **噪声地板**：|ΔCAGR|<0.4pp 视为不可分辨（C7 标定值）。

## 五、纪律备注

- `low_vol_keep_pct` 默认 `None`（不启用）⇒ 默认构型零影响；
  本实验仅经 `--set` 显式开启；
- 本实验是 e8b 栈上的**单变量增量**——选股层是已证 alpha 主载体
  （e15 +5.76pp），在此之上测构造约束的边际贡献；
- 结果写 `experiments/lab/d2-lowvol50/`，登记 leaderboard +
  TASK_TRACKER，按 §四模板裁决。
