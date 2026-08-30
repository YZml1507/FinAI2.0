# FinAI2.0 — A股中低频量化交易系统（v1）

> 调研依据：`D:\Projects\research-finai\`（00–16 号文档 + `specs\001-a-stock-longonly-daily-quant\` spec/plan/tasks）
> 数据取数母库：`finai/sources/`（860 接口探测目录）
> 权威指令：`docs\engineering\DATA_LAYER_WORK_ORDER.md`

## 结构

```
FinAI2.0/
  finai/          # 母库：数据源路由 + 凭据 + 限流 + 落盘
  scripts/        # 探测/诊断工具
  docs/           # 工程指令（DATA_LAYER_WORK_ORDER.md 等）
  artifacts/      # 探测产物（interface_matrix/*.json）
  data/           # 数据包（占位）
  backtest/       # 回测引擎（占位）
  strategy/       # 策略（占位）
  accounting/     # 会计/对账（占位）
  reporting/      # 报告（占位）
  ops/            # 运维/调度（占位）
  requirements.txt
```

## 环境

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
# 密钥走环境变量或 .env（不入库）
```

## 权威 spec 位置

- **权威版本**：`D:\Projects\research-finai\specs\001-a-stock-longonly-daily-quant\`（已链接为 git remote `research`）
- **只读快照**：`docs\spec\001-a-stock-longonly-daily-quant\`（防挪走失锚；改动请回 research-finai 修改后重拷）
- 拉取 spec 变更：`git fetch research`

## 红线（详见 docs/engineering/DATA_LAYER_WORK_ORDER.md §3）

1. 复权口径：禁止默认调用，显式传 `adjustment`
2. 停牌脏行：baostock 须过滤 `tradestatus != '1'`
3. 静默截断：TDX 腿校验返回行数覆盖
4. 凭据：统一走 `finai/credentials.py`，不写字符串字面量
