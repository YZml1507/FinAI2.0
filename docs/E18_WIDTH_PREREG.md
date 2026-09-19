# E18 持仓宽度扩展实验 — 预登记（实验前冻结）

> 创建：2026-09-20 01:5x（夜班窗口）｜ 状态：**已冻结，允许开跑**
> 一句话：e17-pos8 弱正（+1.05pp、MDD −2.23pp）+ D1 归因（dv 头名
> 集中无优势、候选池等权 fwd20 反超 +0.30pp）⇒ 沿「持仓宽度」轴
> 继续外推，探明宽度-收益曲线的平台/拐点；同时补 floor-only 对照臂
> 分离「单票下限地板」混杂效应。

## 一、动机与证据链

1. **e17-pos8（同批预登记臂）**：CAGR 9.63%（Δ=+1.05pp vs isst-e8b），
   MDD 15.17%（−2.23pp）——弱正证据带。
2. **e17-pos3**：4.02%（Δ=−4.57pp）——宽度轴单调性已现
   （pos3 < pos5 < pos8）。
3. **D1 归因（/tmp/d1_probe/）**：top5-dv 持仓期 fwd20 对 dv≥3% 候选池
   等权 −0.30pp（t=−0.74）、top5 哑变量不显著——「dv 取头名」相对
   候选池边际为负/零 ⇒ 向候选池宽持方向外推有先验支撑。
4. **混杂因子**：pos>8 在 15 万本金下撞上 ¥2万 单票下限
   （150k/8≈18.75k 已贴边；pos10→15k 必触发尾部剔除）——
   宽持仓实验与 `min_position_value` 耦合，须设 floor-only 对照臂分离。

## 二、实验设计（对照 isst-e8b，只动宽度/地板/权重）

固定 e8b 定稿参数：宽度择时 defense 0.25 / attack 0.35 / mid_cap 0 /
ice 1d / demote_liquidate / GC001 现金层 / 本金 15 万 /
rebalance_days=20 / candidate_pool_size=50。

| 臂 | 变更 | 目的 |
|---|---|---|
| e18-pos10 | default_positions=10, portfolio_target_count=10, max_positions=10, portfolio_max_positions=10 | 纯宽度+2（硬顶 10 内），地板 2 万不动（尾部或剔除→实际持仓或 <10，如实观察） |
| e18-floor10 | portfolio_min_position_value=10000（持仓数=5 不动） | floor 单变量对照：分离地板效应（mc 加权尾部 <2万 仓位在地板 1 万下被放行） |
| e18-pos15f10 | positions=15 + tc=15 + maxpos=15(DividendConfig) + portfolio_maxpos=15 + portfolio_hard_limit=15 + floor=10000 | 复合臂：宽度突破地板须降板——声明为复合变更 |
| e18-pos20f10 | 同上但 positions=20 | 宽度轴远端 |
| e18-pos20f10eq | pos20f10 + weight_mode=equal | 宽度×等权交互（D1 提示等权捕获暴露更纯） |

## 三、判据（预冻结，不许事后放宽）

- ΔCAGR ≥ +1.5pp：显著有效；+0.4pp < ΔCAGR < +1.5pp：弱正登记；
  |Δ| ≤ 0.4pp：噪声；ΔCAGR < −0.4pp：判负。
- MDD 劣化 >+3pp 记异常；换手 >8 记异常。
- 显著正臂 ⇒ ±20% 邻域扰动复核（positions ±20% 取整）+ 分年一致性
  检查后才谈晋级；⛔ 任何臂结果都不回溯改判据。

## 四、口径与已知边界（登记不装作没风险）

- 复合臂（pos15f10/pos20f10/pos20f10eq）同时动 positions+floor
  （+weight_mode），非单变量——为物理耦合所迫，floor10 对照臂
  负责分离地板效应；解读时按「宽度组合包」而非单参数结论处理。
- e17 两臂跑在红利税修复（dbd1b1b）之前代码上；e18 全部跑修复后
  代码——除权日当日 fill 计税差异量级 ≤百元级 ≪±0.4pp 噪声地板，
  登记 immaterial；边界臂复跑确认。
- pos≥10 在 15 万本金下单票均仓 <2万：即使降地板至 1 万，佣金
  ¥5 最低在 5k 单上的相对成本升至 ~0.1% 单边——宽度收益须覆盖
  小额佣金摩擦，这正是容量问题的另一面，如实观察。
