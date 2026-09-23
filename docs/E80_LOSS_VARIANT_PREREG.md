# E80 损失函数变体臂预登记（classifier vs regressor）

> 预登记冻结：2026-09-23。**`--run` 前先定口径与判据**。

## 1. 假设

label150 冠军链是 XGBRegressor on 截面 rank-pct 标签。替代目标：
XGBClassifier 预测「是否进入 fwd150 截面 top20%」——二分类损失对
排序信息利用方式不同（概率输出），可能产生不同分数分布的篮子。

## 2. 臂（特征/调仓/域/窗口/资金全同基线）

- L-reg（对照=label150 regressor scores_label150x → run 052313，
  不重跑）
- L-cls：ytr=(rank_pct≥0.8)，XGBClassifier 同参数 d6n600，
  predict_proba[:,1] 为分数 → scores_label150_cls.parquet →
  top40 gated run 同窗。

判定：CAGR/MDD/换手 对基线。判强=CAGR ≥ +3pp；否则判弱/负登记不追。

## 3. 门禁

同 gated 管线；训练 walk-forward/embargo 与 e63 口径逐位一致
（复用 run_label 结构，只换 label 构造与模型类）。
