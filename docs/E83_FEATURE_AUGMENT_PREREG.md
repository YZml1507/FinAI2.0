# E83 —— 特征边际贡献扫描（e63 XGBoost 特征增强）预登记

## 0. 背景与动机

生产 BASE（27 特征，`e63_score_sweep_local.BASE`）已含价量/事件/分析师/
融资融券系；矩阵（e63_Xlab4 / e63_Xlab_2025）内尚有 7 列采集齐了但从未进
模型的特征：fund_cov、fund_cov_chg（基金覆盖机构拥挤度）、
s1_eps_rev90、s2_np_rev90、s3_fy_slope、s4_pe_chg（一致预期明细族）、
fwd_ep（预期 EP）。

单信号口径下这些族有判弱史（e61 基金覆盖残差 t=2.2；e82 C1 复合篮
−10.45% 判负）——**但复合/单信号失败不等于特征边际贡献为零**：
XGBoost 的非线性交互可能吸收其正交信息。本实验测的是
"把它们加进 BASE 后 IC 边际变化"，不是再测单信号。

## 1. 变体臂（同管线同参数，仅 BASE 增列）

| 臂 | 特征集 |
|---|---|
| v0 基线 | BASE（27 列，现状复跑） |
| v1 | BASE + fund_cov + fund_cov_chg |
| v2 | BASE + s1_eps_rev90 + s2_np_rev90 + s3_fy_slope + s4_pe_chg |
| v3 | BASE + fwd_ep |
| v4 | BASE + 上述全部 7 列 |
| v5 | BASE + ann_cnt60（公告密度：60 自然日公告条数，notice_meta，PIT-safe） |
| v6 | BASE + gdhs_qoq（股东户数增减比例最新值，gdhs 公告日锚定） |
| v7 | BASE + ann_cnt60 + gdhs_qoq + 上述全部 7 列 |

同一打分管线（e63_score_2025 同构：月末 sig、24 月滚动训练窗、
EMBARGO_TD=20、PARAMS 固定），复用 X_TRAIN/X_SCORE 矩阵零新采集；
v5/v6/v7 额外 join e83/aux_features.parquet（ann_cnt60/gdhs_qoq 均为
公告日 PIT 锚定，无未来函数）。
**修订记录**：v5-v7 于 v0-v4 结果未出前加入（预登记仍处冻结期），
理由——notice_meta 351MB/gdhs 55 期两源本就落盘未启用，边际成本低。

## 2. 判据

- **判强（晋级门槛）**：训练期 walk-forward IC 均值 ≥ 基线+0.010
  且 t 不降超过 0.5，且 2025 OOS IC 不低于基线−0.01；
  过线者进入引擎层验证（run_score_basket_backtest gated），
  引擎 ΔCAGR ≥ +1pp 且 ΔMDD ≤ +2pp 才改生产参数。
- **判弱**：所有臂 IC 增量 <+0.005 或 t 显著恶化——登记关闭，
  确认 BASE 27 特征已饱和，该轴封存。
- 2025 OOS 段依旧 observe-only，不作晋升判据（纪律不变）。

## 3. 防自欺

- 特征在矩阵内已齐 → 无新采集偏差；缺失值沿用全链 z-score 后 fillna(0)。
- 评分器只看 IC/t 统计，不看引擎收益（先 IC 门再引擎门，两道独立）。
- 判弱即关，不追加窗口/参数变体挽尊。
