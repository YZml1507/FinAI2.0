# 产物隔离说明（QUARANTINE）

> 隔离时间：2026-09-14 19:30（Asia/Shanghai）
> 操作性质：**取证保全（保留证据，非删除作废）**——依据 17 号报告 G-Gate 治理防伪
> 『Git SHA + Hash + Timestamp 三位一体』与 gate_repro.py Fail-Closed 设计。

---

## 一、隔离背景

G-REPRO-1（复现一致性门禁，BLOCKER）在真实仓库检出 1 组产物违反
『同参同输入必得同结果』：

| 字段 | 值 |
|---|---|
| 出处指纹 repro_fingerprint | `d92e38c0058e8cb458e680fdaf72e064` |
| params_hash | `f54c298d5168eac5`（全同） |
| code_hash | `d89fd84+dirty`（三份产物全同） |

同指纹分组内 3 份产物 metrics 实质分歧：

| run_id | round_trips | max_drawdown | cagr |
|---|---|---|---|
| 20260913-193032 | 78 | 0.4308 | -0.031979 |
| 20260913-200834 | 78 | 0.4308 | -0.031979 |
| 20260914-123029 | **99** | **0.4975** | **-0.023778** |

## 二、根因

两次运行之间修改了 `strategy/candidates.py`、`scripts/run_dividend_backtest.py`、
`tests/test_dividend_strategy.py`（git status 显示 M 未提交），但 `code_hash` 仅记录到
`d89fd84+dirty` 粒度，**无法区分 dirty 工作区的具体内容差异**，导致『伪同指纹
异结果』——指纹声明同源，实际代码输入不同。

## 三、处置决定

- 这 3 份产物因 dirty 出处不可分辨，**不满足三位一体的内容寻址出处要求**，
  不具备作为复现一致性比对与晋升依据的资格。
- **隔离而不删除**：保留原始文件用于根因取证与后续追责研究；
  同时从 `experiments/runs/index.jsonl` 移除对应索引记录，保持权威目录自洽。
- 隔离后权威目录剩余带指纹产物：`20260914-165623`（fp 7423c7f7）、
  `20260914-182726`（fp 2383621d，**当前权威产物**），各自指纹组内唯一，无同组冲突。

## 四、关联问题（另行立项）

长效治理：`code_hash` 需纳入 dirty 工作区文件内容哈希（方案 A），从源头杜绝
伪同指纹；在未修复前，**禁止在 dirty 工作区运行产生权威产物**。

---

隔离产物清单：
- `20260913-193032-t312-dividend-v1-noseed.json`
- `20260913-200834-t312-dividend-v1-noseed.json`
- `20260914-123029-t312-dividend-v1-noseed.json`
