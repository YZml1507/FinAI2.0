# E99 东财研报原子明细全史因子族 — 预登记

> 2026-09-24 冻结。数据源：`reportapi.eastmoney.com/report/list`
> （每股研报全史，免 key）。字段：infoCode/publishDate/orgSName/author/
> emRatingName/lastEmRatingName/indvAimPriceL-T/predictThisYearEps/
> predictNextYearEps/predictNextTwoYearEps/title/code。
> 落盘 data/em_reports/{prefix3}.parquet：**144,718 条研报 × 4,296 股，
> 2017-01→2026-09，EPS/目标价字段填充率 100%/年**。
> 本族为 E86 信号族的**全史版**——E86 滚动 6 月快照通道继续日采；
> E99 用 9.75 年历史直接上月频正式门。

## 一、信号族（全部 PIT by publishDate，月末 T 截面）

| 信号 | 构造 | 假设方向 |
|---|---|---|
| rev_breadth90 | T 前 90 日内全部研报：每篇 EPS1(当年) − 同股同年此前 90 日一致均值的方向，(#up−#down)/#reports | 上修宽度>0 → Fwd 正 |
| rev_net90 | 同口径方向净值均值（±1 算术平均） | 正 |
| rating_chg90 | 90d 内评级上调数−下调数（emRatingName vs lastEmRatingName） | 事件族，正 |
| dispersion | 最新-每机构(120d 窗) EPS1 截面 std/\|mean\| | 分歧大 → Fwd 负 |
| eps_slope | 最新-每机构 (EPS3−EPS1)/\|EPS1\| 均值（期限斜率） | 正 |
| n_orgs | 120d 窗覆盖机构数 | 正 |
| cov_chg | n_orgs(T) − n_orgs(T−120d) | 正 |
| tp_gap | 最新-每机构目标价中位 / close(T) − 1 | 空间大 → Fwd 正 |
| rating_score | 最新-每机构评级序数均值（买入2/增持1/持有0.5/中性0/减持-1/卖出-2/回避-2） | 正 |
| report_cnt30 | 30d 研报篇数（关注流） | 正 |

## 二、判定门（沿用体系）

- 月频 IC ≥0.04 且 t≥3 → 判强；t<2 或 IC<0.02 → 判弱；
- 事件族（rev_*、rating_chg90）必跑 ±20 日安慰剂 p<0.05；
- 覆盖局限登记：无研报覆盖的股天然无信号（~4.3K/5.3K 史上有覆盖，
  截面内实际覆盖更低），覆盖偏差属结构局限非缺陷；
- 晋级路径：判强者 → e83 式边际扫描（ΔIC_train ≥ 冠军集 +0.008）
  → 引擎验证（ΔCAGR ≥+1pp 且 ΔMDD ≤+2pp）；
- fwd=20 交易日收益（close→close+20），与全体系一致。

## 三、限制登记

- 2017 年前无研报数据（窗口起点 2017-01）→ 有效评估窗 2017-04 起；
- 退市股覆盖不全（东财研报集以在市股为主）→ 轻微存活偏差，登记；
- emRatingName 空串 10,587 行（无评级行）按缺失处理。
