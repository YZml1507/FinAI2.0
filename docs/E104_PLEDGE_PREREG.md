# E104 股权质押轴 预注册（冻结于评估前）

## 数据
`data/cninfo_pledge/`（cninfo webapi sysapi/p_sysapi1019，全 A 股公告质押/解押记录）。
字段：SECCODE / DECLAREDATE（公告日=PIT 锚点）/ F001V 出质人 / F003V 质权人 /
F006N 质押股数(万股) / F012N 解押股数(万股) / F008V 事由文本。
PIT：asof = DECLAREDATE + 1 交易日（公告日收盘后才可知，保守错后一天）。

## 信号（月度截面）
- `plg_net3m`：近 90 日 Σ(F006N − F012N)（净质押股数，万股），log1p 变换
- `plg_cnt3m`：近 90 日质押笔数（F006N 非空记录数）
- `plg_rel3m`：近 90 日解押笔数（F012N 非空记录数）

## 假设
质押潮 = 控股股东资金链压力信号 → 高净质押个股未来 20 日收益更低
（distress 定价不足假说；反向结论亦可登记，以方向稳定为准）。

## 判定门（沿用族标准）
- 月频 Spearman IC：|IC| ≥ 0.04 且 |t| ≥ 3 → 判强（晋级候选）
- |IC| < 0.02 或 |t| < 2 → 判弱
- 安慰剂：asof ±20 交易日错位重算，p ≥ 0.05 方可计真
- 区间：2016-01 ~ 2024-12（2025+ 永久 OOS 只观察登记）

## 验收产物
`experiments/lab/e104/{eval.json, sig_pledge_monthly.parquet}`
