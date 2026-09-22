# Gate Generalization Spike

E 路线第二步实证。完整设计与分析见 `docs/E_ROUTE_GATE_GENERALIZATION_SPIKE.md`。

## 内容

- `toy_ledger.csv` —— 5 行玩具外部回测账本（date/asset/qty/price，qty 正负表买/卖；3 标的跨 5 日）
- `toy_adapter.py` —— `ExternalEvidenceAdapter` 协议草案的最小实现（纯 dict 产出，零本仓业务类型依赖）
- `run_spike.py` —— 对全部 29 门逐一 `evaluate(ctx)`，如实记录 status
- `RESULTS.txt` —— 本次实跑结果（PASS 16 / FAIL 2 / INCONCLUSIVE 11 / ERROR 0）

## 复跑

```bash
.venv/bin/python experiments/spikes/gate_generalization/run_spike.py
```

玩具市场规则：佣金每笔固定 5 元、无印花税科目、T+0、±10% 合成涨跌停界。
 FAIL 与 INCONCLUSIVE 均为有效数据点（市场规则硬编码 / 缺键 fail-closed），见报告 §4–§6。
