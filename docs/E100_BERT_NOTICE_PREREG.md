# E100 公告正文 FinBERT 情感因子族 — 预登记

> 2026-09-24 冻结（打分结果落地前）。语料 = e62 合并去重正文
> （145,431 art_code，前 1500 字截断，Release `notice-body-shards`）。
> 模型 = `yiyanghkust/finbert-tone-chinese`（bert-base-chinese 金融情感
> 三分类：Neutral/Positive/Negative），Kaggle GPU 批量推断，
> 输出 notice_bert_scores.parquet（art_code × P(neu/pos/neg)）。
> 评估框架与门禁**完全沿用 e62 预登记**（`docs/E62_BODY_NLP_PREREG.md`）：
> 本族是 pos_den 词表法的模型升级，作为 e62 族补充信号列，不构成新轴。

## 一、信号（月末 T 截面，滚动 60 日窗聚合，与 e62 同构）

| 信号 | 构造 | 假设 |
|---|---|---|
| bert_pos_den | 当月各公告 P(pos) 均值 | 与 pos_den 同假设（正密度 → 负向，报喜藏忧） |
| bert_neg_den | 当月各公告 P(neg) 均值 | 负密度 → 负向（直接利空） |
| bert_posneg | P(pos)−P(neg) 均值 | 净情绪 → 正 |
| bert_conf | 当月 P(neu) 低值比例（中性低=情绪强烈） | 极端情绪 → 负向 |
| bert_pos_cnt | P(pos)>0.7 公告篇数 | 报喜数量 → 负向 |

## 二、判定门（同 e62）

- 月频 IC ≥0.04 且 t≥3 → 判强；t<2 或 IC<0.02 → 判弱；
- 安慰剂 ±20 日 p<0.05；
- 覆盖门：≥80%（与 e62 合并统计——BERT 分数与正文语料同覆盖）；
- 与 pos_den 作对照：bert_* 若显著优于词表版则替换入候选，
  等效则记「模型升级无增益」；
- 2025+ 永久 OOS 只观察登记。

## 三、覆盖口径说明

BERT 分数仅覆盖**已采到正文**的公告（当前 20.5%+，与 e62 同爬坡）；
正式判定仍待覆盖率 ≥80% 后随 e62 一并结算。当前为预评估性质，
分数先落地、IC 先看方向，判强判弱均只登记。
