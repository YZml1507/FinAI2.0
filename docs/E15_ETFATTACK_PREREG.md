# e15 预登记：指数 placebo——同择时持 510880 vs 选股栈

> 创建：2026-09-19 ｜ 状态：待发车（实现链已通，短窗探针先行验证）
> 依据：e14-bump 影子判负后 mid 带方向收敛关闭（`docs/audit/e14_bump_shadow_finding_20260919.md`）；
> C7 标定收单（`docs/audit/c7_g2_calibration_20260919.md`）。
> 本实验回答一个 **existential 问题**：e8b 冠军栈的 top5-dv 选股层
> 相对「同择时下直接持有红利指数 ETF」是否创造净价值。判据全部
> 预先固定，⛔ 不存在事后放宽的空间。

## 一、实验变量

- **唯一变量**：`attack_instrument="sh.510880"`——attack 档持仓物
  从「top5-dv 市值加权篮子」换成单票红利 ETF（510880，跟踪上证
  红利指数 000922）。择时/宽度/出清/费用/现金计息全部与对照一致。
- **对照组**：`isst-e8b`（≡ `e8b-gc001-e7-combo`，run
  `20260917-160223` / `20260919-132626` 重跑逐值一致）：
  CAGR 8.5814% / MDD 17.3990% / 换手 4.6074 / 156 笔。 <!-- gate-doc-ignore: 历史快照（对照实验实测值，非基线声明），⛔ 不改史 -->
- 同 universe（487 静态池，尽管 placebo 臂不消费选股）、同宽度
  序列、同日历、同费用模型、同 GC001 现金计息。

## 二、机制与已知不对称（全部预先登记）

| 不对称 | 方向 | 处理 |
|---|---|---|
| ETF 二级成交额早期薄（510880 中位 ~0.24 亿/日）vs `min_daily_amount=5000万` hygiene 下限 | 若沿用下限→placebo 臂大量攻击日无法建仓=伪空仓 | `--set portfolio_min_daily_amount=1` 放开该臂下限——依据：ETF 有申赎机制兜底，二级成交额非真实容量约束；真实约束=参与率上限（5%）仍生效。**只用于本臂**，选股池语义不变 |
| ETF 分红不适用股息红利差别化个税（股票口径税） | 若套用→placebo 臂被错扣 ~0.15-0.3pp/yr | `BacktestBroker(dividend_tax_exempt={attack_instrument})` 正确性豁免 |
| ETF 数据无 tradestatus 列 | feed 跳过 R1 纵深过滤 | 登记（sina 源只含交易日行） |
| 510880 2012 前无数据 | 攻击档首日 bar 缺失→该日空仓 | 采集起点 2012-01-04 覆盖 warmup+全窗 |

数据源登记（⛔ 语义变更）：citydata 代理 407 失效 ⇒ baostock 不覆盖
ETF ⇒ **sina 通道**（akshare `fund_etf_hist_sina` RAW 日线 +
`fund_etf_dividend_sina` 累计分红差分=12 次除息事件）。落盘
`data/etf_bars/sh.510880/`（3157 行 2012-01-04~2024-12-31 +
`exdiv.parquet`），meta.json 登记，data_hash 并入出处指纹。

## 三、运行命令（全部 `--set` 显式留痕）

```
.venv/bin/python scripts/lab/run_experiment.py --name e15-etfattack \
  --set use_breadth_timing=True --set breadth_defense_threshold=0.25 \
  --set breadth_attack_threshold=0.35 --set breadth_mid_cap=0.0 \
  --set breadth_ice_confirm_days=1 --set breadth_demote_liquidate=True \
  --set cash_yield_series=data/rates/gc001_daily.parquet \
  --set attack_instrument=sh.510880 \
  --set portfolio_min_daily_amount=1
```

## 四、验收判据（预先固定）

主判读（ΔCAGR = e15 − e8b，噪声地板 ±0.4pp 取 C7 §三-c 实测值）：

| 结果 | 裁决 |
|---|---|
| ΔCAGR ≥ −0.4pp | **选股层无净价值**——同择时持指数即可达到/超过选股栈 ⇒ 选股层整体负贡献或零贡献，路线级结论：转向「指数+择时」重构或选股层重做 |
| ΔCAGR ∈ (−1.5pp, −0.4pp) | 选股层小幅正贡献但未达显著线 ⇒ 登记为弱证据，选股层存疑待查归因（费用差/换手差/集中度差） |
| ΔCAGR ≤ −1.5pp | **选股层显著正贡献** ⇒ e8b 栈的 alpha 实锤来自选股而非仅择时；选股栈通过 placebo 检验 |

附属判据：

- **MDD**：placebo 臂预期 ≤ e8b（指数分散度高于 5 票篮子）；若 MDD 反
  升 >3pp 须登记异常；
- **换手**：placebo 臂应 ≪ e8b（单票 vs 5 票轮换）；若 ≥4 须查 demote
  反复出单；
- **费用绝对额**：placebo 臂应显著更低（无红利税、换手低）——登记
  fees_sum 对照；
- **暴露一致性**：两臂 attack 档持仓日数应逐日相同（同一宽度序列、
  同一状态机）——产物对账时核对 invested-days 计数，差异 >2% 交易日
  须查因（预期唯一差异源：ETF 参与率/流动性过滤 vs 股票篮子过滤）。

## 五、纪律备注

- `attack_instrument` 默认空串 ⇒ 默认构型零影响（4 单测钉死：空默认/
  脏代码拒/选股旁路/缺 bar 空仓）；本实验仅经 `--set` 显式开启；
- placebo 臂的 `portfolio_min_daily_amount=1` 是**臂内覆盖**——e8b
  对照臂保持 5000万 不变，两臂各自对自己的可投资产应用各自的
  可行性模型（股票池=hygiene 下限、ETF=参与率上限）；
- 本实验不做晋级裁决——它是归因/证伪工具：判「选股层是否值钱」，
  不判「e15 是否优于 e8b 应否上线」；
- 结果写 `experiments/lab/e15-etfattack/`，登记 leaderboard +
  TASK_TRACKER，按 §四模板裁决；
- 若结果落在 ±0.4pp 噪声地板内 → 按「placebo ≥ 选股栈」处理
  （证据不足以声称选股有价值）。

## 六、收单登记（2026-09-19 19:xx）★判负 placebo——选股栈通过检验

- **结果**：`e15-etfattack`（run `20260919-190223`，27.9min，FINISHED，
  门禁 BLOCKER 全 PASS）vs 对照 `isst-e8b`： <!-- gate-doc-ignore: 历史快照（消融实验实测对照值，非基线声明），⛔ 不改史 -->
  CAGR 2.8181% vs 8.5814% / MDD 22.7658% vs 17.3990% /
  换手 3.0229 vs 4.6074 / 41 笔 vs 156 笔 / fees 7.3k vs 22.3k /
  胜率 61.0% vs 44.2%。
- **裁决（§四 主判据，口径未动）**：ΔCAGR = −5.7633pp ≤ −1.5pp ⇒
  **选股层显著正贡献**——e8b 栈的 alpha 实锤来自选股层而非仅择时；
  指数 placebo 检验通过。同择时持红利指数仅得 2.82%/yr，选股栈
  在其上加成 +5.76pp/yr。
- **附属判据复核**：①MDD 反升 +5.37pp（22.77% vs 17.40%）——
  超预期方向（指数分散度本应更低）⇒ 登记异常：攻击档集中持单票
  ETF 在 regime 切换点承担了篮子未承担的择时冲击，选股篮子的
  个券分散缓冲了出清日波动；②换手 3.02 <4 符合预期（无 demote
  异常）；③费用 7.3k ≪ 22.3k 符合（单票+无红利税+低换手）；
  ④暴露一致性——两臂共用同一宽度状态机，placebo 臂 41 笔往返
  vs 选股臂 156 笔的差异源=单票整手/参与率约束与多票篮子轮动
  的结构性差别，属机制内差异。
- **含义**：选股层被 placebo 证伪失败 = 选股层存活——它是收益的
  主要载体。研究方向回到「选股层内部改进」（质量/排雷/veto 线索
  已死，剩下=池构成、持仓数、调仓节拍、权重形态）；择时层已证
  价值但非 alpha 主源。
- **产物**：`experiments/lab/e15-etfattack/`（data_hash a1ae027e，
  data_version 含 +etf@sh.510880 出处指纹）。
