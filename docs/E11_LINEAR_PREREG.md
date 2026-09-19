# e11-linear 预登记：C1 连续权重映射（linear）主实验 + ±20% 扰动矩阵

> 创建：2026-09-19 ｜ 状态：待发车（基线 1092 全绿 @ beaf247 之后）
> 依据 PLAYBOOK 铁律 8 + Hermes R9/R10 §B3.1/§C-C1 + C2 判负结论
> （尖峰归因于阶跃映射本身，连续映射是正确主攻方向）。
> 防 E4 式错误：先审实现、先预登记判据、再出指标；判据全部预先固定，
> ⛔ 不存在事后放宽标准的空间。

## 一、实验变量（一次只动一个）

- 唯一变量：`breadth_weight_mode = linear`（其余全部沿用 e8b 冠军构型）。
- 对照基准：`e8b-gc001-e7-combo`（CAGR 8.58% / MDD 17.40% / 换手 4.607， <!-- gate-doc-ignore: 历史快照（消融实验实测对照值，非基线声明），⛔ 不改史 -->
  run_id 20260917-160223-t312-dividend-v1-noseed）。
- 同 universe 截面（stock_basic_cache）、同宽度序列
  （market-breadth-a/breadth20_daily.parquet）、同 SHA 出码后整批跑。

## 二、机制与归因链（修接口，不修 alpha）

G-2 判负的病在**接口**：仓位 = 宽度的阶跃函数 ⇒ 参数曲面是台阶+棱边，
±10% 扰动无平台区。linear 把 mid 区 [defense, attack) 由 mid_cap→1.0
线性裁剪，端点复用既有参数（新增自由度 0），ice 保留硬阈值。
⛔ 预期修 G-2 判据形态，**不预期提高 CAGR**（修接口不修信号 alpha）。

## 三、运行矩阵（全部 `--set` 显式留痕）

主实验：

```
.venv/bin/python scripts/lab/run_experiment.py --name e11-linear \
  --set use_breadth_timing=True --set breadth_defense_threshold=0.25 \
  --set breadth_attack_threshold=0.35 --set breadth_mid_cap=0.0 \
  --set breadth_ice_confirm_days=1 --set breadth_demote_liquidate=True \
  --set breadth_weight_mode=linear \
  --set cash_yield_series=data/rates/gc001_daily.parquet
```

±20% 扰动矩阵（Hermes 指定 15 格，d∈{0.20,0.225,0.25,0.275,0.30} ×
a∈{0.28,0.31,0.35,0.385,0.42} 且 a>d，⛔ 此矩阵**不选最优**，只为
G-2a~c 判据提供曲面）：命名 `e11-linear-pg-a<A>d<D>`，其余 --set 与主实验同。

## 四、验收判据（预先固定，C7 采用 Hermes G-2a~g 三件套）

- **G-2a 平台宽度**（Hermes 建议值，本仓首用标定）：15 格内
  CAGR ≥ (2/3)×max(CAGR) 的格点占比 ≥ 50%；
- **G-2b 退化单调性**：沿 b_lo（a 固定）与 b_hi（d 固定）两轴，CAGR
  相对 (0.25,0.35) 基点退化基本单调、无非单调跳回尖点；
- **G-2c Lipschitz 斜率**：L = max‖ΔCAGR‖/‖Δθ‖，要求 L×10% ≤ 6.3pp
  （噪声地板建议值）；
- **G-2d PBO/CSCV、G-2e DSR**：本轮不作硬判据，登记为后续跟进项
  （需跨变体 IS/OOS 切分与全历史 N 申报，工程上随 C7 完整版落地）；
- **G-2f WFA**：沿用既有 pgy-* 分年 + pg-holdout 口径，主实验后补跑
  e11-linear 留出段（2022-2024）复核零退化；
- **G-2g 状态计数/换手**：annual_turnover ≤ 8（5 日节拍硬约束，e8b
  实测 4.607；linear 分数仓预期放大换手，影子前瞻见 §五）；
- **CAGR 目标不变**：主实验 CAGR 相对 e8b（8.58%） <!-- gate-doc-ignore: 历史快照（对照实验实测值，非基线声明），⛔ 不改史 --> 退化 < 1.0pp 记为
  「接口修复成立」，退化 1.0~1.5pp 记「临界」，>1.5pp 记「代价过大」
  （阈值为本窗口自定判据，非国际标准，登记备查）。

## 五、影子前瞻（发车前已跑，产物 experiments/lab/e11-linear/_shadow/）

基于真实宽度序列（2846 日）× 5 日节拍 + demote 语义（不改仓、不跑回测）：

- mid 带 [0.25,0.35) 天数 388（13.6%），各年均有分布（25~47 天/年）；
- 影子到仓日 540 天，其中 linear 取分数仓 173 天（32.0%）；
- linear vs hard 到仓 |Δcap| 均值 0.182、p90 0.768 —— 接口改造在
  mid 带生效面足够大；
- linear 相邻调仓日 cap 漂移合计 99.4（hard 等价单位 181）——
  换手形态从「0/1 大跳」变「频繁小步」，总量同量级，预期
  annual_turnover 落在 4.6~8 区间（须实测复核）；
- 日度 |Δb| p99 = 0.31 ⇒ 单日 |Δcap| 恒 ≤ |Δb|/0.10（探针断言 3 的上界
  经验成立：|Δb|≤0.10 时 |Δcap|≤1.0，非 0.25——Hermes 原断言对
  完整 jump 口径偏松，以本影子为准修正预期）。

## 六、行为探针（出指标前必过，E4 先例）

`scripts/lab/e11_linear_probe.py`（复刻策略语义回放断言）：

1. w(b) 在 [0.25,0.35] 严格单调递增、值域恰为 [0,1]；
2. b<0.25 ⇒ 0、b≥0.35 ⇒ 1（与 hard 两端完全一致，向后兼容断言）；
3. 任意单日 |Δcap| ≤ |Δb|/span（span=attack−defense）；
4. demote 跨界日仍出单（86 个跨界日在 linear 下目标仓=linear(b)，
   非 0 时不再全清——这是 linear 的预期行为变更，探针断言**出单存在**
   而非清仓语义）；
5. ice 确认路径逐日与 hard 一致（确认/解除日期集合全同，共 124 次）；
6. 到仓日集合与 hard 完全一致（节拍时钟不受 weight_mode 影响）。

## 七、裁决模板（结果出来后填，⛔ 不许改写阈值）

| G-2a/b/c + CAGR 退化 | 裁决 |
|---|---|
| 三判据全过 且 退化<1.0pp | G-2 修复成立 → 补 G-2f 留出段复核 → 提交六维门禁重评 |
| 三判据过 但 退化 1.0~1.5pp | 临界：平台化收益代价可议，登记后由 C8 集成方案对照 |
| 任一判据不过 | 该实现下判负（⛔ 不升级为路线判负），记台账，转 C4（SMA5 联用）/C8 |
| CAGR 退化 >1.5pp | 不论平台多宽，代价过大，回退 hard 为默认（hard 默认本来就保留） |

## 八、纪律备注

- hard 恒为默认值，基线可比性不破；linear 仅经 `--set` 显式开启。
- 任何数据层语义变更 ⇒ 整批同 SHA 重跑 + 更新基线 + 登记决策。
- 本预登记数值判据（50%/6.3pp/1.0pp/turnover≤8）为**本仓标定值**，
  非国际标准，后续修订须走 C7 文档化并留痕。