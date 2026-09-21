# Gate Generalization Spike → 产品化实证

E 路线第二步 spike 设计与分析见 `docs/E_ROUTE_GATE_GENERALIZATION_SPIKE.md`；
本目录现承载**第三步产品化实证**：玩具账本走正式 `ExternalEvidenceAdapter` 协议 +
`audit_external` 入口。

## 内容

- `toy_ledger.csv` —— 5 行玩具外部回测账本（date/asset/qty/price，qty 正负表买/卖；3 标的跨 5 日）
- `toy_adapter.py` —— `ExternalEvidenceAdapter` 协议的玩具实现（纯 dict 产出，零本仓业务类型依赖；
  经 `market_rules()` 声明玩具市场规则：无整手约束/无高价线/仅佣金科目/T+0/±10%）
- `run_spike.py` —— 走 `audit_adapter()`（22 门外部集：✅7 直接通用 + 🔧15 需适配器）分层报告
- `RESULTS.txt` —— 本次实跑结果

## 复跑

```bash
.venv/bin/python experiments/spikes/gate_generalization/run_spike.py
# 或走 CLI 入口（等价路径）
.venv/bin/python -m scripts.gates.audit_external \
    --adapter experiments.spikes.gate_generalization.toy_adapter:ToyEvidenceAdapter
```

分层口径：🏠 本仓特有 7 门（E-1/E-2/S-2/G-2/G-3/G-DOC-1/G-REF-1）由外部入口**显式排除**——
它们审计的是本仓引擎探针/文档纪律/母库，喂外部证据只会误扫工具包安装目录（spike §4.3）。

INCONCLUSIVE 为有效数据点（缺证据 fail-closed）；D-5/S-5 经 `MarketRules` 参数化后
对玩具市场如实判 PASS（spike 阶段被 A 股字面量拦 FAIL 的两门）。
