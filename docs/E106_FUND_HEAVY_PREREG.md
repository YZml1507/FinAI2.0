# E106 基金重仓拥挤度轴 PREREG（冻结于评估前）

## 数据
- 源：`data/cninfo_fund_heavy/{year}.parquet`（cninfo p_sysapi 基金重仓表，2005-2026，181,699 行）
- 字段：SECCODE 股码 / ENDDATE 报告期(季末) / F001N 持有基金数 / F002N 持股数 / F003N 持仓市值(万元)

## PIT
- `asof = ENDDATE + 45 自然日`（基金季报披露约季后 22 个工作日，留安全垫）

## 信号（季度→月频 asof 对齐）
- `fh_cnt` = log1p(F001N) 持基家数（拥挤广度）
- `fh_cnt_chg` = F001N 环比差分（机构进出）
- `fh_mv_chg` = log(F003N_t/F003N_{t-1}) 持仓市值环比

## 假设
基金重仓是拥挤/跟风轴：重仓家数上升→后续跑输（拥挤反转，IC<0）；或动量延续（IC>0）。方向不定，幅度定强弱。

## 门禁
- 月频 Spearman IC ≥0.04 且 |t|≥3 判强晋级
- |t|<2 或 |IC|<0.02 判弱收单
- 安慰剂 ±20 日 p<0.05
- 2025+ 永久 OOS 只观察登记
