# T404 台账保鲜与到期提醒自动化

> 实施日期：2026-09-02
> 来源：14号文档《易变数据保鲜与复查台账》
> 目的：将技术台账复查与到期提醒纳入自动化调度，减少人工遗漏

---

## 一、系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    台账自动化系统                              │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐    │
│  │ 台账登记表    │   │ 到期提醒      │   │ 复查提醒      │    │
│  │ registry.py  │──▶│ expiry.py    │   │ check.py     │    │
│  │              │   │              │   │              │    │
│  │ - 数据源     │   │ - ≤30天警告  │   │ - 月度30天   │    │
│  │ - 依赖库     │   │ - ≤7天紧急   │   │ - 季度90天   │    │
│  │ - 密钥凭据   │   │ - 已过期     │   │ - 超期2周期  │    │
│  │ - 交易规则   │   └──────┬───────┘   └──────┬───────┘    │
│  │ - 交易成本   │          │                  │            │
│  └──────┬───────┘          │                  │            │
│         │                  ▼                  ▼            │
│         │          ┌──────────────────────────────┐        │
│         │          │      调度脚本                 │        │
│         └─────────▶│   run_ledger_tasks.py       │        │
│                    │                              │        │
│                    │ 1. 检查到期项                │        │
│                    │ 2. 检查复查项                │        │
│                    │ 3. 生成报告                  │        │
│                    │ 4. 发送告警（飞书）          │        │
│                    └──────────┬───────────────────┘        │
│                               │                            │
└───────────────────────────────┼────────────────────────────┘
                                ▼
                    ┌──────────────────────┐
                    │ Windows任务计划       │
                    │ 每日18:00自动执行     │
                    │ 首次运行: 2026-09-28  │
                    └──────────────────────┘
```

---

## 二、核心模块

### 2.1 台账登记表（`ops/ledger_registry.py`）

**作用**：唯一定义点，所有技术台账项在此登记。

**数据结构**：
```python
@dataclass
class LedgerItem:
    name: str                  # 名称（如 "baostock API"）
    category: LedgerCategory   # 类别（数据源/依赖库/密钥/配置等）
    last_check_date: date      # 上次复查日期
    check_frequency: CheckFrequency  # 复查频率（monthly/quarterly/event_triggered）
    expiry_date: date | None   # 到期日（可选，仅凭据/证书类）
    notes: str                 # 备注（当前值/时点/来源）
    source_doc: str            # 所在文档路径
```

**初始台账项**（≥20项）：
- **R1-R5 母库缺陷台账**：已清零项作为技术债监控（baostock停牌过滤、TDX截断、东财可达性、复权映射、TDX腿依赖）
- **核心数据源**：baostock API、akshare、新浪/腾讯财经接口
- **关键依赖库**：pyarrow、pandas、Python版本（3.10 EOL=2026-10-31）
- **凭据类**：代理服务（127.0.0.1:7897）
- **交易成本类**：印花税、过户费、经手费、证管费（基于14号台账A）
- **交易规则类**：涨跌停档位（基于14号台账B）

---

### 2.2 到期提醒（`ops/expiry_reminder.py`）

**触发逻辑**：
```python
def check_expiry_dates(today: date) -> list[ExpiryAlert]:
    """扫描所有台账项的 expiry_date"""
    - 距到期 ≤30天 → WARNING 告警
    - 距到期 ≤7天  → CRITICAL 告警
    - 已过期      → CRITICAL 告警（每日重复直至处理）
```

**输出**：
- `ExpiryAlert` 列表（按紧急度排序）
- Markdown格式摘要（`format_expiry_summary`）
- 结构化报告（`get_expiry_items_report`）

**示例告警**：
```markdown
## 🔔 台账到期提醒

**统计**：紧急 1 项 / 警告 2 项

### 🚨 紧急项
- 【紧急】Python版本 将在 7 天后到期！（到期日：2026-10-31，类别：依赖库）

### ⚠️ 警告项
- 【提醒】测试凭据 将在 25 天后到期（到期日：2026-11-01，类别：密钥/凭据）
```

---

### 2.3 复查提醒（`ops/check_reminder.py`）

**触发逻辑**（基于14号SOP）：
```python
def check_review_due(today: date) -> list[CheckAlert]:
    """扫描所有台账项的 last_check_date + check_frequency"""
    - 月度项：距上次 ≥30天  → DUE 提醒
    - 季度项：距上次 ≥90天  → DUE 提醒
    - 超期：≥2个周期未复查  → OVERDUE 告警
    - 即将到期：≤7天        → UPCOMING 提醒
    - 事件触发项：不参与自动检查
```

**输出**：
- `CheckAlert` 列表（按紧急度排序）
- Markdown格式摘要（`format_check_summary`）
- 结构化报告（`get_check_items_report`）

**示例告警**：
```markdown
## 📋 台账复查提醒

**统计**：超期 0 项 / 到期 3 项 / 即将到期 1 项

### ⏰ 到期应复查
- 【到期】baostock API 应复查（上次：2026-08-02，已过 31 天，频率：monthly）
- 【到期】akshare 应复查（上次：2026-07-28，已过 36 天，频率：monthly）
```

---

### 2.4 台账更新接口（`ops/update_ledger.py`）

**人工复查后的回写接口**：
```python
# 更新复查日期
update_check_date(item_name: str, check_date: date) -> LedgerItem

# 更新到期日
update_expiry_date(item_name: str, expiry_date: date | None) -> LedgerItem

# 更新备注
update_notes(item_name: str, notes: str) -> LedgerItem

# 新增/移除台账项
add_ledger_item(item: LedgerItem) -> None
remove_ledger_item(item_name: str) -> LedgerItem

# 导出工具
export_ledger_csv() -> str  # CSV格式导出
generate_ledger_source_code() -> str  # 生成Python源码（持久化）
```

⚠️ **警告**：`update_ledger.py` 提供动态更新接口，但台账的**唯一真相源**是 `ledger_registry.py` 源码。生产环境应将更新结果回写到 `ledger_registry.py`，而非维护运行时状态。

---

### 2.5 调度脚本（`scripts/run_ledger_tasks.py`）

**执行流程**：
```python
def run_ledger_tasks():
    """调度入口（Windows任务计划每日18:00执行）"""
    1. 检查到期项 → 发送告警（有则发送）
    2. 检查复查项 → 发送告警（有则发送）
    3. 生成报告  → 保存到 runs/ledger_reports/YYYYMMDD-ledger-report.json
    4. 统计摘要  → 控制台输出
```

**告警集成**：
- 当前：`send_alert_to_feishu(message, level)` 为mock实现（控制台输出）
- 生产：应调用飞书MCP或Webhook：
  ```python
  from ops.feishu_alert import send_card
  send_card(title="台账告警", content=message, level=level)
  ```

---

## 三、使用指南

### 3.1 手动执行

**命令行**：
```bash
# 激活虚拟环境
.venv\Scripts\activate

# 执行调度脚本
python scripts/run_ledger_tasks.py
```

**输出示例**：
```
============================================================
台账自动化任务 — 2026-09-02T18:00:00
============================================================

📌 检查到期项...
✅ 无到期项

📌 检查复查项...
============================================================
[WARNING] 飞书告警（模拟发送）
============================================================
## 📋 台账复查提醒

**统计**：超期 0 项 / 到期 3 项 / 即将到期 0 项

### ⏰ 到期应复查
- 【到期】baostock API 应复查（上次：2026-08-02，已过 31 天，频率：monthly）
...
============================================================

📌 生成报告...
✅ 报告已保存：D:\Projects\FinAI2.0\runs\ledger_reports\20260902-ledger-report.json

============================================================
📊 统计摘要
============================================================
紧急项：0
警告项：3
到期项：3
即将到期：0
============================================================
```

---

### 3.2 Windows任务计划集成

**步骤**：

1. **打开任务计划程序**：Win+R → `taskschd.msc`

2. **创建基本任务**：
   - 名称：`FinAI_LedgerTasks`
   - 描述：台账保鲜与到期提醒自动化
   - 触发器：每天 18:00

3. **操作配置**：
   - 操作：启动程序
   - 程序/脚本：`D:\Projects\FinAI2.0\.venv\Scripts\python.exe`
   - 添加参数：`scripts\run_ledger_tasks.py`
   - 起始于：`D:\Projects\FinAI2.0`

4. **条件与设置**：
   - ☑ 只有计算机使用交流电源时才启动
   - ☑ 如果任务失败，每隔 10 分钟重启任务
   - 停止任务运行于：1 小时

5. **首次运行**：2026-09-28（月度首轮复查）

---

### 3.3 人工复查流程（对接14号SOP）

**当收到复查告警时**：

1. **打开台账**：`ops/ledger_registry.py` 找到对应项
2. **执行检查**：按 `source_doc` 中的检查方式执行（URL检查/API测试/版本查询）
3. **判定结果**：
   - 无变更 → 更新复查日期
   - 有变更 → 修改来源文档 + 修订日志 + 回填台账
4. **更新台账**：
   ```python
   from ops.update_ledger import update_check_date, update_notes
   from datetime import date
   
   # 更新复查日期
   update_check_date("baostock API", date(2026, 9, 2))
   
   # 如有变更，更新备注
   update_notes("baostock API", "已验证login正常，交易日历2026-09-02确认")
   
   # 持久化（可选）：回写到源码
   from ops.update_ledger import generate_ledger_source_code
   code = generate_ledger_source_code()
   # 手工复制到 ledger_registry.py 的 LEDGER_ITEMS 定义处
   ```

5. **登记记录**：在 14号文档 §七 复查记录表追加一行

---

## 四、测试覆盖

**测试文件**：`tests/test_t404_ledger_automation.py`（≥28单测）

**覆盖场景**：

### 4.1 到期提醒测试（7个）
- ✅ 无到期日不产生告警
- ✅ 距到期≤30天产生WARNING
- ✅ 距到期≤7天产生CRITICAL
- ✅ 已过期产生CRITICAL
- ✅ 按紧急度排序（critical > warning）
- ✅ 空告警格式化
- ✅ 有告警格式化摘要

### 4.2 复查提醒测试（6个）
- ✅ 月度项≥30天产生DUE
- ✅ 季度项≥90天产生DUE
- ✅ 超期≥2周期产生OVERDUE
- ✅ 即将到期≤7天产生UPCOMING
- ✅ 事件触发项不参与检查
- ✅ 空告警格式化

### 4.3 台账更新测试（9个）
- ✅ 更新复查日期成功
- ✅ 更新不存在的项报错
- ✅ 更新到期日成功
- ✅ 更新备注成功
- ✅ 新增台账项成功
- ✅ 新增重复项报错
- ✅ 移除台账项成功
- ✅ 移除不存在的项报错
- ✅ 导出CSV格式

### 4.4 集成测试（2个）
- ✅ 完整流程：创建项 → 检查到期+复查 → 更新日期 → 再次检查无告警
- ✅ 真实场景：Python 3.10 EOL 2026-10-31 应产生告警

**执行**：
```bash
py -3.11 -m pytest tests/test_t404_ledger_automation.py -p no:ddtrace -v
```

**预期结果**：28 passed

---

## 五、报告输出

### 5.1 JSON报告（自动生成）

**路径**：`runs/ledger_reports/YYYYMMDD-ledger-report.json`

**结构**：
```json
{
  "report_date": "2026-09-02",
  "expiry": {
    "check_date": "2026-09-02",
    "critical_count": 0,
    "warning_count": 0,
    "alerts": []
  },
  "review": {
    "check_date": "2026-09-02",
    "overdue_count": 0,
    "due_count": 3,
    "upcoming_count": 0,
    "alerts": [
      {
        "name": "baostock API",
        "category": "数据源",
        "urgency": "due",
        "days_since_last_check": 31,
        "next_check_due": "2026-09-01",
        "last_check_date": "2026-08-02",
        "frequency": "monthly",
        "message": "【到期】baostock API 应复查..."
      }
    ]
  },
  "summary": {
    "total_critical": 0,
    "total_warning": 3,
    "total_due": 3,
    "total_upcoming": 0
  }
}
```

### 5.2 CSV导出（人工审阅）

```python
from ops.update_ledger import export_ledger_csv

csv = export_ledger_csv()
with open("ledger_export.csv", "w", encoding="utf-8") as f:
    f.write(csv)
```

---

## 六、运维要点

### 6.1 台账维护纪律

1. **唯一真相源**：`ops/ledger_registry.py` 的 `LEDGER_ITEMS` 列表
2. **新增项目**：技术债/新依赖/新凭据必须同步登记
3. **定期回写**：人工更新后应回写源码（非运行时状态）
4. **文档联动**：台账变更时同步修改来源文档并登记修订日志

### 6.2 告警处理优先级

| 级别 | 类型 | SLA |
|------|------|-----|
| 🚨 CRITICAL | 已过期 / 超期≥2周期 | 24小时内处理 |
| ⚠️ WARNING | 距到期≤30天 / 到期应复查 | 7天内处理 |
| 📅 INFO | 即将到期≤7天 | 提前知悉，按计划处理 |

### 6.3 失败处理

**调度脚本失败**：
- 检查日志：Windows事件查看器 → 任务计划程序历史记录
- 手动重试：`python scripts/run_ledger_tasks.py`
- 重启任务：任务计划程序 → 右键 → 运行

**数据源检查失败**：
- 参考14号SOP §八 异常处理矩阵
- 登记"待验证"+原因（不编造结论）
- 升级为事件触发项，不强行改判

---

## 七、扩展方向

### 7.1 飞书告警集成（待实现）

**当前状态**：mock实现（控制台输出）

**集成路径**：
1. 配置飞书Webhook或MCP连接
2. 在 `ops/` 下新增告警模块（**⛔ 尚未实现**；当前 `ops/` 仅有台账提醒四模块：`ops/check_reminder.py`、`ops/expiry_reminder.py`、`ops/ledger_registry.py`、`ops/update_ledger.py`）：
   ```python
   def send_card(title: str, content: str, level: str):
       """发送交互式卡片到飞书群"""
       # 调用飞书MCP或Webhook
   ```
3. 替换 `run_ledger_tasks.py` 中的 `send_alert_to_feishu` 实现

### 7.2 仪表盘（可选）

- 可视化台账状态（到期倒计时、复查进度）
- 历史报告趋势分析
- 交互式复查操作（Web界面）

### 7.3 智能提醒（可选）

- 根据历史变更频率动态调整复查周期
- 预测高风险到期项（基于依赖关系）
- 自动生成复查脚本（基于检查方式）

---

## 八、交付清单

| 项目 | 路径 | 状态 |
|------|------|------|
| 台账登记表 | `ops/ledger_registry.py` | ✅ 完成（≥20项） |
| 到期提醒逻辑 | `ops/expiry_reminder.py` | ✅ 完成 |
| 复查提醒逻辑 | `ops/check_reminder.py` | ✅ 完成 |
| 台账更新接口 | `ops/update_ledger.py` | ✅ 完成 |
| 调度脚本 | `scripts/run_ledger_tasks.py` | ✅ 完成 |
| 单元测试 | `tests/test_t404_ledger_automation.py` | ✅ 完成（≥28例） |
| 文档说明 | `docs/t404_ledger_automation.md` | ✅ 本文档 |
| 飞书告警集成 | ⛔ 无对应模块（`ops/` 下仅有台账提醒四模块） | ⏳ 待实现（当前为 mock，仅 `data/collector.py` 的 stub） |

---

## 九、参考文献

- `D:\Projects\research-finai\14_易变数据保鲜与复查台账.md` — 14号调研文档（SOP与台账定义）
- `REVALIDATE.md` — R1-R5母库缺陷台账
- `CLAUDE.md` — 新窗口启动指令与硬约束
- 12号文档附录A — 环境清单验证

---

## 附录 A：快速启动检查表

**首次配置**（一次性）：
- [ ] 确认Python 3.11+环境激活
- [ ] 安装依赖（已含在 `requirements.txt`）
- [ ] 手动执行一次：`python scripts/run_ledger_tasks.py`
- [ ] 确认报告生成：`runs/ledger_reports/YYYYMMDD-ledger-report.json`
- [ ] 配置Windows任务计划（每日18:00）
- [ ] 首次运行日期：2026-09-28

**日常运维**（收到告警时）：
- [ ] 查看飞书告警或JSON报告
- [ ] 按14号SOP §一 执行复查
- [ ] 更新台账项（`update_check_date` / `update_notes`）
- [ ] 回写源码（持久化，可选）
- [ ] 在14号文档 §七 登记复查记录

**月度检查**（每月28日）：
- [ ] 复查所有月度项（A1/A2/A6/D11等）
- [ ] 更新台账复查日期
- [ ] 确认任务计划正常执行

**季度检查**（每季末月28日）：
- [ ] 复查所有季度项（A3-A5/B1-B5/C1-C2/D1-D10等）
- [ ] 检查台账结构有效性（URL失效、检查方式变化）
- [ ] 更新台账维护纪律

---

*本文档最后更新：2026-09-02*
