# quarantine_062849 —— 错误域产物隔离

run 20260923-062849（top40@10M）实为其启动者误用
`scores_label150.parquet`（82 期截断旧文件，已改名 `_STALE82`）
的产物；launch 后即被 kill 意图终止，但 record_run 已落盘。

与域正确的 20260923-064906（89 期 label150x）**params_hash/
repro_fingerprint 全同**（scores 文件原不进指纹要素——已由
run_score_basket_backtest 的 `scores_sha256` 入参修复），
metrics 不同 ⇒ 若在 runs/ 内将永久触发 G-REPRO-1 撞对 FAIL。

故按"错误的输入产物"隔离至此：留档但移出 canonical runs/ 域与
index.jsonl。域修正后的有效产物 = 20260923-064906。
