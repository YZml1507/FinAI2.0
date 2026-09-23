# E82 预登记 —— e33 C1 合成信号作为 top40 篮分数源（scorer-source 轴）

> e33 C1 等权 z 合成（S5 户数/M5 融券/F6 ROE波动/I3 行业相对收益，
> 月频五分位 t=4.46 判强=项目最强影子信号）**从未进入引擎口径**：
> 其 Q5 篮 ~730 股不适配散户持仓，而当时无全 A 多头基座。e63/top40
> 晋级后前置成立——本臂测 C1 信号源直通真引擎的收益转化力。

## 1. 假设

若 C1 在 top40 引擎篮上保住可观收益 → 非 XGB 信号族可独立驱动基座，
且为「异源信号合成/集成」打开第二轴（与 label150 不相关证据独立）。

## 2. 臂设计

| 臂 | 分数源 | 构造 | 其余口径 |
|---|---|---|---|
| S-base | scores_label150x | 现行基线 | top40 eq reb60 amt5M min5k no-timing ¥3M |
| S-c1 | C1 月频 z 合成 | e33 同构（四族 z 均值，月末 sig_date） | 同基线逐位 |

## 3. 判据

- **判强**：S-c1 CAGR ≥ S-base −3pp 且 MDD ≤ S-base +5pp——异源信号
  能独立撑起基座收益 → 登记为第二分数源候选（后续合成臂再立预登记）
- **判弱**：CAGR < 20% 或 MDD > 0.35 → 引擎转化失败，登记关闭
- 附加诊断：C1-top40 与 label150-top40 持仓重合率（异源正交性证据）

## 4. 实现与 PIT

- `scripts/lab/e82_c1_scores.py`：复用 e33 信号链（e31.build_signals +
  S5/M5/F6/I3 + 截面 z 等权）逐月末产出 comp →
  `experiments/lab/e82/scores_c1.parquet` (sig_date, ts_code, score)。
- 信号可见性：e33 构件全部 PIT（pubDate/报告期锚）+ sig_date=月末 T，
  策略 T+1 生效——与分数篮既有口径一致。
- 覆盖期：受 margin_detail(2849 日) 与 gdhs 覆盖约束，预计
  2017-07→2024-11 与 label150x 同域可比。
- gated run：`--registry-root experiments` 29 门。

## 5. 流程门

本文件 commit 后再 --run；登记 tracker + RESULT 简表。
