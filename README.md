# FinAI2.0 — A股中低频量化交易系统（v1）

> 调研依据：`D:\Projects\research-finai\`（00–17 号报告 + `specs\001-a-stock-longonly-daily-quant\` spec/plan/tasks）  
> 数据取数母库：`finai/sources/`（860 接口探测目录，守护红线 `FINDING-` 恒为 370 行）  
> 权威工程指令：`docs\engineering\DATA_LAYER_WORK_ORDER.md`  
> 文档全景导航：[`docs/README.md`](docs/README.md)  
> 当前测试基线：**760 passed（2026-09-10 实测，0 failed, 0 errors, 100% PASS）**；历史阶段快照 629 / 681 / 699 / 717 / 725 见 `docs/delivery/` 归档  
> 核心物理约束：**实际资金 10~15 万元、纯多头（Long-Only）、无两融对冲手段、持仓 3~8 只、5 元佣金地板**

## 系统工程结构

```text
FinAI2.0/
  finai/          # 母库：数据源路由 + 凭据 + 限流 + 落盘（FINDING- 恒等于 370 行）
  data/           # 数据层：日线采集/清洗/停牌过滤/PIT 财务对齐/股票池回放/增量更新
  backtest/       # 回测引擎：事件驱动九模块/订单状态机/双账本/分段费率/红利税/拆股扩充
  strategy/       # 策略层：组合管理(3-8只/2万下限)/红利策略/MA200择时/高价股排除/参数扫描
  accounting/     # 【占位包】⛔ 无实现：__init__.py 为 0 字节；双账本真实落地在 backtest/ledger.py，
                  #   日终对账实现在 paper_trading/reconciliation.py::reconcile_account
  reporting/      # 报告层：绩效指标计算(纯函数)/实验 Registry 原子落盘
  paper_trading/  # 模拟盘：模拟执行器/偏差容忍带量化/日终任务/台账保鲜调度
  ops/            # 运维层（台账提醒 4 模块）：check_reminder / expiry_reminder / ledger_registry / update_ledger
                  #   ⛔ 无飞书告警与心跳监控实现（飞书仅 data/collector.py 的 stub，只记日志不真发）
  scripts/        # 自动化工具：防伪审计(audit_evidence_integrity)/数据采集/回测运行
  docs/           # 技术文档库（详见 docs/README.md 导航）
    delivery/     # 阶段交付与就绪清单归档
  tests/          # 自动化离线单测套件（760 单测全绿，2026-09-10 实测）
  experiments/    # 正式全周期回测实验落盘产物（JSON + 索引）
  data/daily_bars/  # ⚠️ 仅 1 只标的（sh.600000/2024.parquet）——动量全周期回测数据不足，
                  #   相关动量结论无机读产物，见 docs/audit/void_documents.md
  requirements.txt
```

## 环境与运行

```bash
# 运行环境
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 运行全量离线单测（760 例全绿基线，2026-09-10 实测）
py -3.11 -m pytest tests/ -p no:ddtrace -p no:ddtrace.pytest_bdd

# 运行自动化防伪与数据真值审计工具（5/5 PASS）
python scripts/audit_evidence_integrity.py
```

## 权威源与防伪门禁位置

- **计划与验收权威**：`D:\Projects\research-finai\specs\001-a-stock-longonly-daily-quant\`（已链接为 git remote `research`）
- **研发防伪与六维门禁**：`D:\Projects\research-finai\17_中低频量化研发防伪与工程质量门禁体系深度调研报告.md`（D-L-E-A-S-G 体系）
- **只读快照**：`docs\spec\001-a-stock-longonly-daily-quant\`（防挪走失锚；改动请回 research-finai 修改后重拷）
- 拉取 spec 变更：`git fetch research`

## 核心红线（详见 docs/engineering/DATA_LAYER_WORK_ORDER.md §3）

1. **复权口径**：禁止默认调用，必须显式传 `adjustment`，RAW 真实日线严禁混入后复权；
2. **停牌脏行**：baostock 须过滤 `tradestatus != '1'`，停牌日成交量恒为 0；
3. **静默截断**：TDX 腿校验返回行数覆盖，禁止静默截断；
4. **母库行数**：`finai/sources/` 下 `FINDING-` 行数严格锁定 **370 行**；
5. **死代码与参数生效**：开启特性必须在账本产生有效流水，组合打分必须真实决定资金权重；
6. **滑点与规费防伪**：滑点通过推移成交价体现，禁止现金二次扣款；历史回测必须按法定分段费率扣税，严禁穿越历史。
