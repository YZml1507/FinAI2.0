# experiments/legacy/ — 遗留产物处置说明

> 处置时间：2026-09-21（UTC）· 任务 T317（Scheduled Full Gate Audit 修复）

## 处置对象

| run_id | timestamp | 处置原因 |
|---|---|---|
| 20260903-135508-t312-dividend-v1-noseed | 2026-09-03 13:55 | 无 `repro_fingerprint`（产物诞生于出处指纹机制落地前） |
| 20260903-142212-t312-dividend-v1-noseed | 2026-09-03 14:22 | 同上 |
| 20260906-184556-t312-dividend-v1-noseed | 2026-09-06 18:45 | 同上 |
| 20260907-150402-t312-dividend-v1-noseed | 2026-09-07 15:04 | 同上 |

## 处置方式：**取证保全迁移**（git mv，非删除）

- 这四份产物缺 `repro_fingerprint`——指纹由 `code_hash + data_hash +
  calendar_hash + universe_hash + params_hash + seed` 构成，依赖**运行时刻**
  的仓库状态与数据清单哈希，**事后不可补算**（补算 = 伪造出处，红线）。
- 留在 `experiments/runs/` 会让 G-REPRO-1（同指纹组复现一致性）将其判为
  不可复现孤立产物 → `--scheduled` 审计每日 FAIL。
- 迁至 `experiments/legacy/` = 保留历史取证价值（git 历史不丢），退出权威
  登记面。`experiments/runs/index.jsonl` 中对应索引行**保留不改**——索引是
  追加式登记账，删行 = 篡改历史。

## 替代证据（T317 新跑）

verified 组由 `scripts/produce_gate_evidence_run.py` 在统一干净代码树上
双跑形成（baseline×2，复刻锚点 20260915-235155 全部参数）：
同 `repro_fingerprint` + 同 `metrics` ⇒ G-REPRO-1 PASS。

## 参考

- `experiments/quarantine/QUARANTINE.md` —— 同口径处置先例（d92e38c0 组）。
- `docs/GATE_AUDIT_REPAIR_HANDOFF.md` —— 本处置的实施依据（第 9 步）。
