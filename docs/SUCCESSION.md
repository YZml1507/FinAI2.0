# 继任执行手册（弱模型接管专用）

> 创建：2026-09-17 ｜ 定位：**模型性能降级后的作战地图**。
> 阅读顺序不变：STRATEGY → ALPHA3_PLAYBOOK → TASK_TRACKER → 本文件。
> 本文件把「剩下要做的事」拆成带难度分级的执行包——L1 机械执行、
> L2 需要判断、L3 禁止弱模型独自做（须向用户请示）。写不出「针对哪个
> 瓶颈、预期改变什么」的动作 = 不做（STRATEGY 七.2）。

## 〇、能力分级定义

| 级 | 含义 | 自主权限 |
|---|---|---|
| **L1** | 命令已写好、裁决模板已备好，只需执行+填表 | 可独立做 |
| **L2** | 需要读懂实验结果/日志做判断，或写 <50 行探针脚本 | 可做，但裁决只记「临时」 |
| **L3** | 改 strategy/backtest 逻辑、新数据接入、新方向立项 | ⛔ 须先向用户请示，写明理由 |

## 一、当前队列（按序执行，不许跳号开新方向）

### Q1【L1】三组对照实验收单

⏳ 状态：`b-repro` ✅（与冠军逐值一致，基线重锚完成）、`e6-v3` ✅（**计息有效，+1.45pp/MDD -2.1pp**，终局登记见 TASK_TRACKER）、`e7-demote` 重发中。

等待 `experiments/lab/{e7-demote,e6b-gc001}/experiment.json`。

```
cd /home/ubuntu/FinAI2.0
python3 -c "import json; d=json.load(open('experiments/lab/<NAME>/experiment.json')); \
  print(d['cagr'], d['max_drawdown'], d['win_rate'])"
```

**先核指纹再比较**（⛔ 铁律）：三组的 `universe_hash` 必须 =
`ac50e9da4fdf2942`（2595 只缓存截面）；三组 `data_hash` 必须相同。
满足后与 `b-repro`（而非旧冠军数值）比较。

| 结果 | 裁决 |
|---|---|
| b-repro ≈ 冠军台账值（±0.2pp，实测值见 PLAYBOOK 台账，此处不抄数） | 数据漂移无影响，旧冠军继续当基线 |
| b-repro 偏离明显 | 以 b-repro 为新对照基线，在 tracker 登记「重锚」 |
| e6-v3 > b-repro +0.3pp | 计息有效（预期 +0.48pp）→ 登记台账，晋级候选 |
| e7-demote > b-repro +0.3pp 且 MDD 不恶化 | 有效 → 走 Q2 探针+扰动；否则按 PREREG 第五节模板裁决 |

登记格式照抄 tracker 既有 bullet；实测数字只在 TASK_TRACKER 登记
（该文件在豁免白名单内），本文件及其他新文档一律不抄实测值——
写「见 PLAYBOOK 台账」即可（详见坑 4）。

### Q2【L2】e7 行为探针（仅当 e7-demote 有效）

按 `docs/E7_DEMOTE_PREREG.md` 第四节 4 条断言写回放探针（参照
`experiments/lab/e4-risk-audit/probe_replay.py` 模式）。探针过 →
临时裁决升终局；不过 → 记「该实现下证负」，⛔ 不许升级路线判负。

### Q3【L1】e6b-gc001（代码已就绪，纯执行）⏳ 已在跑

前置已满足：e6-v3 抬升成立。e6b 已于 14:21 发车（命令同下，已在后台）。
注意：GC001 ffill 阈值已放宽至 16 自然日（春节断档实证）。
探针 `experiments/lab/e7-demote-audit/probe_replay.py` 已实现并在跑——
e7 结果出来后直接读 `probe_summary.json` 的 violations 字段。

```
# （如须重跑）
.venv/bin/python scripts/lab/run_experiment.py --name e6b-gc001 \
  --set use_breadth_timing=True --set breadth_defense_threshold=0.25 \
  --set breadth_attack_threshold=0.35 --set breadth_mid_cap=0.0 \
  --set breadth_ice_confirm_days=1 \
  --set cash_yield_series=data/rates/gc001_daily.parquet
```

预期：CAGR 略低于 e6-v3（GC001 均值 2.53% 但后期下行）。
裁决：vs b-repro +0.4pp 以上 → 登记有效。设计细节见
`docs/GC001_CASH_YIELD_DESIGN.md`。

### Q4【L3】组合晋级评估（e6+e7 同开）

`--set cash_yield_annual=0.02 --set breadth_demote_liquidate=True`
（若两者各自有效）。⛔ 晋级前必须过六维门禁（独立留出/±20% 扰动/
成本复核/基准对比）——扰动矩阵设计与分段验证属 L3。

### Q5【L3】扩池/换底层资产（触发条件才启动）

触发：e6/e7 落地后 CAGR 仍不达标（对照 STRATEGY 五节）。
方向候选（STRATEGY 六节）：a) 池扩容/多池分仓 b) 波动率目标仓位
c) 分批退出。⛔ 不许在 487 红利池内挖因子、不许重开已封顶路线。

## 二、高频坑速查（每条都是用废数据换的）

1. **对比先核 universe_hash**——不同截面（2595 vs 986 事故）结果作废；
2. **新行为必须有显式开关**——e6-v2 教训：demote 无开关污染了并行实验；
3. **broker 不走 ledger.settle**——账本侧改动要同时在
   `BacktestBroker.settle` 挂接（③.5 模式）；
4. **新文档不许抄实测数字**——触发 G-DOC-1 漂移守卫；写「见 PLAYBOOK
   台账」或加 `gate-doc-ignore` 行内豁免（理由须含「历史快照」）；
5. **改完测试数变了要同步** `scripts/gates/constants.py` 的
   `TEST_BASELINE_PASSED`（= 实际收集数，不许 >=）；
6. **实验产物不入 git**（experiments/lab 已 ignore），但 dirty 文件会进
   code_hash——实验在跑时别改 strategy/backtest/scripts；
7. **货币 ETF K 线无收益信息**、akshare 511880「7日年化」列是假的、
   东财净值有 1→100 面值切换（详见 GC001_CASH_YIELD_DESIGN 第五节）。

## 三、弱模型纪律（复述 PLAYBOOK 五节）

1. 只做本文件队列里的下一步；
2. 每步：改代码 → 全绿 → commit → 跑实验 → 登记 → commit；
3. 不确定选保守分支；台账冲突先写理由再动手；
4. ⛔ 唯一须停手请示：删数据/仓库级破坏操作 + 一切 L3。
