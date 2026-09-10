# PM-1 根因定位报告：同一 `params_hash` 为何产出 3 种互斥结果

> 审计对象：`D:\Projects\FinAI2.0`（A 股长仓量化回测系统）
> 缺陷编号：**PM-1（复现性/证据链缺陷，致命）**
> 环境：Windows / bash / `py -3.11`（Python 3.11.5）
> 纪律：**本报告为纯诊断产出。未修改任何生产代码（`reporting/`、`scripts/`、`tests/` 全程只读）；仅新建本文件。**
> 证据口径：所有结论均带 `文件:行号` 或命令输出。

---

## 1. 一句话根因

**`params_hash` 只哈希了策略参数字典本身（`params`），而真正决定回测结果的另外两类输入——输入数据内容与候选池/代码状态——在产物里分别只是两个手写常量字符串（`data_version="dividend-stocks-2015-2024"`、`code_version="t312-dividend-v1"`），既不是内容哈希、也不参与 `params_hash`。**

因此：**参数相同 ≠ 输入相同**。4 份产物在 09-03→09-07 期间，数据被多次重写、代码在未提交的工作树里演进，但 `params_hash` 与两个版本串全程纹丝不动，最终产出 3 种互斥指标，而系统对此**无任何拦截**——"同参重跑一致（FR-REP-2）"从未被真正证明。

---

## 2. `params_hash` / `code_version` / `data_version` 三者现状

### 2.1 `params_hash` —— 只哈希 `params`，确认无疑

计算入口 `reporting/registry.py:66-70`：

```python
66  def _params_hash(params: Mapping[str, Any]) -> str:
67      """参数快照的 SHA-256 前 16 hex（canonical 尺与 ``tx_hash`` 同宗，键序无关）。"""
68      canon = _canonicalize(dict(params))
69      text = json.dumps(canon, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
70      return sha256(text.encode("utf-8")).hexdigest()[:16]
```

调用点 `reporting/registry.py:164`（在 `RunRecord` 组装时）：

```python
157      record = RunRecord(
...
161          code_version=self.code_version + ("+dirty" if self.dirty else ""),
162          data_version=self.data_version,
...
164          params_hash=_params_hash(params),
165          params=dict(params),
```

**结论**：`_params_hash` 的输入**只有 `params` 一个实参**（第 68 行 `_canonicalize(dict(params))`）。`code_version`（161 行）与 `data_version`（162 行）**写入记录但不参与哈希**。这是"参数指纹"与"输入指纹"被画等号的直接原因。

**实测复核**（遍历 4 份产物，用 registry 同一把尺重算）：

```
$ py -3.11 -c "... 重算 _params_hash ..."
20260903-135508 stored_ph= f54c298d5168eac5 recomputed= f54c298d5168eac5 match= True
20260903-142212 stored_ph= f54c298d5168eac5 recomputed= f54c298d5168eac5 match= True
20260906-184556 stored_ph= f54c298d5168eac5 recomputed= f54c298d5168eac5 match= True
20260907-150402 stored_ph= f54c298d5168eac5 recomputed= f54c298d5168eac5 match= True
```

4 份 `params_hash` 全为 `f54c298d5168eac5`，与任务给定的硬事实一致。

### 2.2 `code_version` —— 手写标签，**不是**内容哈希

来源在回测脚本内**硬编码**，`scripts/run_dividend_backtest.py:295-299`：

```python
295      registry = ExperimentRegistry(
296          root=registry_root or (_root / "experiments"),
297          code_version="t312-dividend-v1",
298          data_version="dividend-stocks-2015-2024",
299      )
```

对照 registry 模块自己的设计口径 `reporting/registry.py:14`：

```
| ``code_version`` | git 短哈希，**注入式**（⛔ 不许内部 subprocess 取 git —— 离线测试可重现）；dirty 工作树须调用方显式传 ``dirty=True`` 留痕 |
```

**设计意图是 git 短哈希，但运行方传入的是常量 `"t312-dividend-v1"`。** 判定为**手写标签**（非内容哈希）。

**旁证**：`ExperimentRegistry.__init__` 明明提供了 `dirty` 标记（`reporting/registry.py:100`、`:109`、`:161` 的 `+dirty` 后缀），但 `run_dividend_backtest.py` 调用 `ExperimentRegistry(...)` 时**没有传 `dirty`**（默认 `False`），4 份产物 `code_version` 均无 `+dirty` 后缀——即工作树即使脏，留痕机制也不会被触发。

**版本串在代码大幅变化时仍未变**：09-06→09-07 之间代码经历了 `CLAUDE.md:32` 所述"数据层四大硬伤修复"（commit `e64b0a9`，2026-09-07 15:25），但 `code_version` 仍是 `t312-dividend-v1`。

### 2.3 `data_version` —— 手写标签，**不是**哈希

同样来自 `scripts/run_dividend_backtest.py:298` 的常量 `"dividend-stocks-2015-2024"`。

对照 registry 设计口径 `reporting/registry.py:15`：

```
| ``data_version`` | 数据快照标识（如 ``data/daily_bars`` 分区哈希），注入式；⛔ 不许默认空 |
```

**设计意图是"分区哈希"，实传的是"数据集名字"。** 判定为**标签（非哈希）**。

**数据在此期间确实变过**（mtime 实证，见 §3）：`data/dividend_stocks/` 下文件在 09-03 与 09-07 被多轮重写，而 `data_version` 常量从未改变。

### 2.4 三者的关系（一句话总结）

| 字段 | 现状 | 是否内容寻址 | 是否影响 `params_hash` |
|---|---|---|---|
| `params_hash` | SHA-256(params) 前 16 hex | 是（但只覆盖 params） | —— |
| `code_version` | 常量 `"t312-dividend-v1"` | **否** | **否** |
| `data_version` | 常量 `"dividend-stocks-2015-2024"` | **否** | **否** |

> `anti_tamper_signature` 也无法补救：它签名的字段集合是 `run_id / code_version / data_version / params_hash / status / metrics`（`scripts/gates/tamper_guard.py:54-61`）——**签的是记录自身，而不是记录赖以产生的输入**。4 份产物各自内部自洽，签名/验签（`tamper_guard.py:74-99`）全都会 PASS，却对"同参异果"完全无感。

---

## 3. 09-03 两次运行差异的机制判定

### 3.1 时间线（关键证据）

| 时间（2026-09-03, +08:00） | 事件 | 证据 |
|---|---|---|
| 08:10:13–08:10:24 | 一批 `_bars.done` 落盘 | `find data/dividend_stocks -printf`（hour 08 桶 424 个文件） |
| **13:27:02–13:44:18** | **又一批 `_bars.done` 落盘（46 个）**——日线采集仍在收尾 | `data/dividend_stocks/sh.600423/_bars.done`(13:27:02) … `sz.300825/_bars.done`(13:44:18)，共 46 个 |
| **13:55:08** | **运行 A**：`20260903-135508` → **0 笔成交** | `experiments/runs/20260903-135508-*.json` |
| **14:00:02–14:06:07** | **441 个 `exdiv/*.parquet` 落盘（除权 sidecar 生成）** | `data/dividend_stocks/exdiv/sh.600000.parquet`(14:00:02.66) … `sz.301618.parquet`(14:06:06.96) |
| **14:22:12** | **运行 B**：`20260903-142212` → **131 笔往返 / MDD 25.37%** | `experiments/runs/20260903-142212-*.json` |

即：**两跑之间（27 分 4 秒），`data/dividend_stocks/exdiv/` 下 441 个数据文件被新建**，且其前一刻（13:27–13:44）日线采集仍在收尾。这是"输入数据在这一窗口内确实发生了变化"的**直接 mtime 实证**。

### 3.2 窗口内没有任何代码提交

```
$ git log --since=2026-09-03 --until=2026-09-05 --stat
（空）
```

全量提交时间线中，两跑之前最后一个提交与之后第一个提交之间存在**近 5 天空档**：

```
b573525 2026-09-02 23:14:34  fix: T312 接续 — 修复 feed/engine/strategy 链路的回测阻塞点
   ← 运行 A / 运行 B 落在此区间（无提交） →
e64b0a9 2026-09-07 15:25:07  fix(t312): 根除数据与引擎硬伤…
```

**含义**：09-03 两跑跑的是**未提交的工作树**，其状态既无 commit 记录、也无 `dirty` 留痕。所以"代码有没有变"在证据层面不可证伪——这本身就是缺陷的一部分。

### 3.3 机制判定：**交易域（候选池）在 13:55 为空/退化**

"0 笔成交"是一个**二值事件**（`final_nav == initial_nav == 150000`、`total_return=0`、`annual_turnover=0`、`round_trips=0`），与"数据不完整导致少成交"的**渐变特征不符**；它指向**交易域整体为空**。

代码自带两条直接证据：

1. **当前版脚本的作者自述**，`scripts/run_dividend_backtest.py:137-141`：

```python
137      拉不到（baostock 故障/离线）⇒ 扫描数据目录全部 symbol（⚠ 幸存者偏差，
138      已在 T312 文档登记；等距抽样 500/5215 本身已带存活截面，影响可控）。
139      ⛔ 不返回 None：watchlist 空集 ⇒ 引擎每天取 0 只票 ⇒ 永无信号（本轮
140      零交易回测的根因）。
141      """
```

> 括号内"**本轮零交易回测的根因**"正是作者对这次 0 成交的自证：**watchlist 空集 ⇒ 每天取 0 只票 ⇒ 永无信号**。

2. **09-03 当时的已提交版本**（`b573525`）在该路径上会**直接产出空池**，`git show b573525:scripts/run_dividend_backtest.py:96-105`：

```python
 96      # ② 股票池（历史存活池）
 97      logger.info("加载历史股票池...")
 98      try:
 99          stock_basic = load_stock_basic()
100          def universe_provider(day: _date) -> list[str]:
101              return compute_alive_universe(stock_basic, day)
102      except Exception as exc:
103          logger.error(f"历史股票池加载失败: {exc}")
104          logger.info("降级：使用数据目录所有 symbol（⚠ 可能含幸存者偏差）")
105          universe_provider = None        # ← 降级为 None ⇒ 空池 ⇒ 0 笔成交
```

而 `load_stock_basic()` 是**在线 baostock 调用**，会抛异常，`data/universe.py:278-283`：

```python
278  def load_stock_basic(timeout: int = DEFAULT_TIMEOUT) -> pd.DataFrame:
...
283      return run_with_timeout(_query_stock_basic_once, timeout)
```

（`_login` 失败会 `raise RuntimeError`，`data/universe.py:244-247`。）

**判定**：09-03 13:55 那次运行，最可能的机制是 **`load_stock_basic()` 这一在线、非内容寻址的外部输入在该时刻取数失败/退化，导致候选池为空（`universe_provider=None`），策略全程无票可买**；到 14:22 重试成功获得满池，于是产生 131 笔往返。与此同时（14:00–14:06）除权 sidecar 也刚被写出——**两类决定结果的输入（候选池快照 + 数据内容）在 27 分钟窗口内都发生了未被哈希记录的变化**。

> 置信度：**候选池为空 = 高**（0 成交是二值事件 + 代码自述 + 历史代码路径三重指向）；**"是 `load_stock_basic` 这一次失败"= 中**（当时工作树未提交，无法逐字节复现；`_bars.done` 13:27–13:44 也提示数据层仍在收尾，不能排除是数据目录 fallback 时尚未就绪）。两者是同一缺陷的两个面：**外部输入未入哈希**。

### 3.4 三个结果的来源（完整链条）

| 产物 | run_id | 结果 | 与该次"输入状态"的对应 |
|---|---|---|---|
| A | `20260903-135508` | 0 成交 | 候选池空/退化（13:55，`load_stock_basic` 失败或数据未就绪） |
| B | `20260903-142212` | −14.24% / MDD 25.37% / 131 往返 | 候选池恢复 + 除权 sidecar(14:00–14:06) 就绪 |
| C | `20260906-184556` | 与 B **逐字节相同**（仅 run_id/timestamp 不同） | 09-03 14:22 → 09-06 18:45 之间，代码与数据**实质未变** |
| D | `20260907-150402` | −27.72% / MDD 43.08% / 78 往返 | 09-07 14:39–14:41 数据被整体重写（"四大硬伤修复"）+ 工作树代码演进 |

B 与 C 逐字段一致（`diff` 实测：仅 `run_id`、`timestamp` 两处不同），这**反向印证**了"结果只随输入变"——B、C 之间输入没变所以结果没变；A/D 之间输入变了所以结果变了。而 `params_hash` 对这一切**无差别**。

---

## 4. 为什么现有"同参重跑一致"测试没抓到

测试确有其身：`tests/test_t206_registry.py:224-253`

```python
224  class TestSameParamRerunConsistency:
225      """FR-REP-2 核心验收：同参重跑两条登记，除 run_id/timestamp 外逐字段一致。"""
226
227      def test_rerun_identical_except_run_id_and_time(self, tmp_path) -> None:
228          params = {"strategy": "toy", "cash": D("120000"), "seed_note": "hd50"}
229          report_a = _run_once()
230          report_b = _run_once()                       # 第二遍：独立重建全栈
...
233          reg = ExperimentRegistry(
234              tmp_path / "exp", code_version="abc1234",
235              data_version="sha256:fixture-frame-v1",
236              clock=lambda: next(clocks)())
237
238          rid_a = reg.record_run(params, report_a, seed=7)
239          rid_b = reg.record_run(params, report_b, seed=7)
...
246          for key in ("params_hash", "params", "metrics",
247                      "code_version", "data_version", "seed", "status"):
248              assert a[key] == b[key], f"字段 {key} 不一致：{a[key]!r} vs {b[key]!r}"
```

**它为什么必然漏掉 PM-1——四条硬伤：**

1. **输入被硬编码为内存常量**。`_run_once()`（`:197-221`）用的是模块级写死的合成行情 `_rows()`（`:164-177`，5 个交易日、单标的 `sh.600777`）。**两次运行读的是同一块内存**，数据内容在物理上**不可能**在两次之间变化——测试把"数据会变"这个 Risk 在构造层面就消灭了。真实系统里变化的正是它（§3.1 的 exdiv 441 文件）。

2. **代码来自同一次加载**。两次 `_run_once()` 处于同一进程、同一代码修订内。它验证的是"**同一修订内**重跑一致"，而 PM-1 恰恰发生在**跨修订/跨数据快照**之间。

3. **`code_version` / `data_version` 是测试自己传的常量**（`:234-235`：`"abc1234"`、`"sha256:fixture-frame-v1"`），且测试只断言"两次相等"——它**从不校验这两个字段与真实代码/数据内容是否相符**。即：测试把 provenance 当作**输入接受下来**，而不当作**待验证的断言**。

4. **它只做"同一时刻的横切相等"，不做"跨快照的纵切分组"**。真正能抓 PM-1 的断言形态是：**对一批产物按"完整出处键"分组，要求同组内 `metrics` 必须一致**（见 §6）。现有测试没有这个跨产物的分组逻辑。

> 一句话：该测试是"同进程 + 同内存 fixture → 同输出"的**自证式（tautology）**，它对真实风险（磁盘数据/代码修订漂移）零覆盖。

---

## 5. 缺失的输入清单

| 输入类别 | 是否已入 `params_hash` | 是否已入产物 | 影响 | 建议处置 |
|---|---|---|---|---|
| 策略参数字典 `params` | ✅ 是 | ✅ | ——（唯一被覆盖项） | 保留 |
| **数据文件内容**（`data/dividend_stocks/**`，含分区/除权 sidecar） | ❌ 否（`data_version` 仅标签） | 仅标签 | 直接决定成交/收益/MDD；本案例 441 个文件在窗口内新建 | **纳入**：数据清单哈希（路径+文件 SHA-256 聚合） |
| **代码内容**（git commit + 工作树 dirty） | ❌ 否（`code_version` 仅常量） | 仅常量 | 09-07 四大硬伤修复后结果剧变，`code_version` 未变 | **纳入**：`git rev-parse HEAD` + `git status --porcelain` 非空则 `+dirty` |
| **候选池时点快照**（`load_stock_basic` / `alive_universe` 结果） | ❌ 否 | ❌ 否 | 本案例 0 成交的**首要嫌疑**（在线快照，非内容寻址） | **纳入**：把当日候选码表 + as_of 落盘并哈希；在线源须冻结成文件 |
| **交易日历版本**（实际消费的 `cal_days`） | ❌ 否 | ❌ 否 | 影响 `trading_days`/年化口径；日历来自指数分区（也在变） | **纳入**：对实际使用的日期序列哈希（或被数据内容哈希覆盖） |
| **暖机窗口 / 起始可用数据长度** | ⚠️ 部分：`warmup_bars=210` 已在 `params` | ⚠️ 仅 `warmup_bars` | 每个 symbol 的**实际可用历史起点**决定首个可交易日，未入哈希 | **纳入**：`data_hash` 覆盖内容即可推得；如需精确，额外记录 per-symbol 起点哈希 |
| 随机种子 `seed` | ⚠️ 未入 `params_hash`，但已入 `run_id` | ✅ | 本次 `seed=None`（策略无随机数），风险低 | 保持；建议一并入"完整出处键"以防未来引入 RNG |
| 初始资金 / 无风险利率 / 费用与价格模型 | ❌ 否（在 `run_dividend_backtest_2015_2024` 形参内，未进 `params`） | ❌ 否 | 会改变结果；当前靠调用约定固定 | **纳入**：并入 `params` 或出处键 |

> 关键点：**上表除第 1 行外，其余全部未真正入哈希。** 这正是"同一 `params_hash` 三种结果"的充要解释。

---

## 6. 修复规格（工程师可直接实现）

> 目标：让 **"同参数 + 同输入 ⇒ 同结果"** 从"未证明"变为"被门禁强制"。
> 原则：**输入 → 内容寻址**；**分组 → 一致性断言**；**历史产物 → 不重写、显式标 legacy**。

### 6.1 改哪个文件 / 哪个函数 / 加什么字段

#### (A) 新增 `reporting/provenance.py`（纯函数，零 IO 依赖注入友好）

```python
# reporting/provenance.py  —— 出处四要素的内容寻址实现（纯函数，可离线单测）
from hashlib import sha256
from pathlib import Path
import json

def hash_path_manifest(root: Path, *, patterns: tuple[str, ...] = ("**/*",)) -> str:
    """数据快照指纹：对 root 下所有匹配文件，按 (相对路径, 文件内容 SHA-256) 排序聚合。
    ⛔ 相对路径必须入哈希：symbol 集合本身变化也必须改变指纹。"""
    items: list[tuple[str, str]] = []
    for pat in patterns:
        for p in sorted(Path(root).glob(pat)):
            if p.is_file():
                items.append((str(p.relative_to(root)).replace("\\", "/"),
                              sha256(p.read_bytes()).hexdigest()))
    items.sort()
    blob = "\n".join(f"{rel}:{h}" for rel, h in items)
    return sha256(blob.encode("utf-8")).hexdigest()[:16]

def hash_sequence(values, *, label: str) -> str:
    """对序列（交易日历日期表 / 候选池码表）做顺序敏感的确定性哈希。"""
    blob = label + "|" + "|".join(str(v) for v in values)
    return sha256(blob.encode("utf-8")).hexdigest()[:16]

def repro_fingerprint(*, params_hash: str, code_hash: str, data_hash: str,
                      calendar_hash: str, universe_hash: str, seed: int | None) -> str:
    """完整出处键：任何输入变动 ⇒ 键变动。"""
    parts = dict(params_hash=params_hash, code_hash=code_hash, data_hash=data_hash,
                 calendar_hash=calendar_hash, universe_hash=universe_hash,
                 seed=("noseed" if seed is None else str(seed)))
    text = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return sha256(text.encode("utf-8")).hexdigest()[:32]
```

#### (B) `reporting/registry.py`：`RunRecord` 加字段 + 构造函数加注入参数 + `record_run` 计算 `repro_fingerprint`

```python
# reporting/registry.py —— RunRecord 新增（保持 params_hash 语义不变，另加内容哈希）
@dataclass(frozen=True)
class RunRecord:
    run_id: str
    status: str
    timestamp: str
    code_version: str
    data_version: str
    seed: int | None
    params_hash: str
    params: dict
    metrics: dict
    error: str | None = None
    # --- 新增（向后兼容：默认 None）---
    schema_version: int = 2
    code_hash: str | None = None        # git HEAD(+dirty) 内容指纹
    data_hash: str | None = None        # data/ 清单内容指纹
    calendar_hash: str | None = None    # 实际消费的交易日历指纹
    universe_hash: str | None = None    # 候选池快照指纹
    repro_fingerprint: str | None = None   # 上述 + params_hash + seed 的聚合键
```

```python
# reporting/registry.py::ExperimentRegistry.__init__ —— 新增可选注入参数（dirty 已有）
def __init__(self, root, *, code_version, data_version,
             code_hash=None, data_hash=None, calendar_hash=None,
             universe_hash=None,            # ← 新增
             clock=None, dirty=False):
    ...
    self._code_hash = str(code_hash) if code_hash else None
    self._data_hash = str(data_hash) if data_hash else None
    self._calendar_hash = str(calendar_hash) if calendar_hash else None
    self._universe_hash = str(universe_hash) if universe_hash else None
```

```python
# reporting/registry.py::record_run —— 在组装 RunRecord 前计算聚合键
from reporting.provenance import repro_fingerprint

ph = _params_hash(params)
if self._code_hash and self._data_hash:          # 四要素齐备才生成指纹（fail-open→显式降级）
    rf = repro_fingerprint(params_hash=ph, code_hash=self._code_hash,
                           data_hash=self._data_hash,
                           calendar_hash=self._calendar_hash or "na",
                           universe_hash=self._universe_hash or "na",
                           seed=seed)
else:
    rf = None      # 缺内容哈希 ⇒ 显式标为不可复现，⛔ 不静默用常量兜底
record = RunRecord(..., params_hash=ph, repro_fingerprint=rf,
                   code_hash=self._code_hash, data_hash=self._data_hash,
                   calendar_hash=self._calendar_hash, universe_hash=self._universe_hash,
                   schema_version=2)
```

> 落盘 payload（`:169-178`）需把上述新字段一并写入 JSON。

#### (C) `scripts/run_dividend_backtest.py`：让注入的版本串变成真内容哈希

```python
# scripts/run_dividend_backtest.py —— 替换 :295-299 的常量注入
from reporting.provenance import hash_path_manifest, hash_sequence
import subprocess

def _git_code_hash() -> str:
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True, cwd=_root).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"],
                           capture_output=True, text=True, cwd=_root).stdout.strip() != ""
    return head + ("+dirty" if dirty else "")

registry = ExperimentRegistry(
    root=registry_root or (_root / "experiments"),
    code_version="t312-dividend-v1",                      # 保留人类可读标签（仅供参考）
    data_version="dividend-stocks-2015-2024",
    code_hash=_git_code_hash(),                           # ★ 新增：内容寻址
    data_hash=hash_path_manifest(data_path),              # ★ 新增：数据快照内容哈希
    calendar_hash=hash_sequence(cal_days, label="cal"),   # ★ 新增：实际交易日历
    universe_hash=hash_sequence(sorted(universe_codes),   # ★ 新增：候选池时点快照
                                label="universe"),
)
```

> 候选池 `universe_codes` 需在跑之前显式落盘（如 `experiments/runs/<run_id>.universe.json`），否则"在线快照"依旧不可审计（对应 §5 第 3 行）。

### 6.2 新增回归测试的断言形式

**(T1) registry 单测（`tests/test_t206_registry.py` 增补）—— 输入变更 ⇒ 指纹必须变：**

```python
def test_data_hash_change_breaks_repro_fingerprint(tmp_path):
    data = tmp_path / "data"; data.mkdir(); (data / "a.parquet").write_bytes(b"v1")
    ph = "f54c298d5168eac5"
    fp1 = repro_fingerprint(params_hash=ph, code_hash="c1",
                            data_hash=hash_path_manifest(data),
                            calendar_hash="cal", universe_hash="u", seed=None)
    (data / "a.parquet").write_bytes(b"v2")          # 输入内容变了
    fp2 = repro_fingerprint(params_hash=ph, code_hash="c1",
                            data_hash=hash_path_manifest(data),
                            calendar_hash="cal", universe_hash="u", seed=None)
    assert fp1 != fp2, "数据内容变化必须改变 repro_fingerprint（否则复现性不成立）"
```

**(T2) 核心回归断言 —— 同出处键的多份产物，`metrics` 必须一致（否则 FAIL）：**

```python
# tests/test_repro_consistency.py —— 跨产物纵切分组断言
def test_same_repro_key_must_have_identical_metrics():
    """同一 repro_fingerprint 的多份产物，其 metrics 必须逐字段一致，否则 FAIL。"""
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in sorted((ROOT / "experiments" / "runs").glob("2026*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        key = rec.get("repro_fingerprint") or f"LEGACY::{rec['params_hash']}"
        groups[key].append(rec)
    violations = []
    for key, recs in groups.items():
        if len(recs) < 2:
            continue
        base = recs[0]["metrics"]
        for r in recs[1:]:
            if r["metrics"] != base:
                violations.append(
                    f"出处键 {key[:12]} 下 {recs[0]['run_id']} 与 {r['run_id']} "
                    f"metrics 不一致 —— 违反'同参必得同结果'")
    assert not violations, "\n".join(violations)
```

> 对**当前仓库**跑 T2：4 份 legacy 产物 `params_hash` 同为 `f54c298d5168eac5`，会被归入同一 `LEGACY::f54c298d5168eac5` 组，**metrics 三缺一异 → 断言红**。这正是期望行为（把 PM-1 钉死在门禁里）。

**(T3) 门禁 `G-REPRO-1`（新增，写入 `scripts/gates/`）**：断言"同键产物指标一致，否则 FAIL"（形态同 T2），与既有 `AntiTamperSignatureGate`（`tests/test_gate_p3_hardening.py:136-154`）并列纳入 `GateMasterAudit`（现 24 道，`test_gate_p3_hardening.py:299` 会随之更新为 25）。

### 6.3 向后兼容处理（**不修改** 4 份历史产物）

| 项 | 建议 | 理由 |
|---|---|---|
| 4 份历史产物 | **保持原样，绝不重写** | 重写会（a）使 `20260907-150402` 的 `anti_tamper_signature` 失效/需重签（等于事后改史）；（b）抹掉 PM-1 的第一手证据 |
| 缺失字段的判定 | 新增 `schema_version`（新产物 = `2`；缺省 = `1`/legacy） | 显式区分，避免"字段缺失"被当作"检查通过" |
| 门禁对 legacy 的语义 | legacy 产物归组时用 `LEGACY::<params_hash>` 前缀，**报告为 `LEGACY_UNVERIFIED`（WARN/FAIL 而非静默 PASS）** | 对应本任务同批的"门禁 SKIP 语义修正"——⛔ 不得把"无法验证"当成"验证通过" |
| legacy 归档 | 另建只读清单（如 `docs/audit/legacy_runs.md`）登记 4 份 run_id + 已知分歧，**文件本身不动** | 留住审计线索，不污染事实源 |
| `anti_tamper_signature` | 沿用；但新产物签名字段集合应扩入 `repro_fingerprint`（`tamper_guard.py:54-61` 增一行） | 让"出处键"一并受签名保护 |

---

## 7. 存疑与未证实项（诚实标注）

1. **09-03 两跑的确切工作树快照不可复原。** 窗口内无提交（`git log` 空），且两跑记录的 `params.portfolio.max_participation_rate` 字段名与 `b573525` 提交的 `participation_rate` **不一致**（`b573525:scripts/run_dividend_backtest.py` 用 `participation_rate`，产物 JSON 用 `max_participation_rate`）——**表明当时工作树已被改动、超出 `b573525`**。故"0 成交 = 候选池为空"证据充分，但**无法证明它就是 `load_stock_basic` 那一次失败**（也可能是数据目录 fallback 未就绪）。**未证实。**

2. **27 分钟内数据"变"的确凿证据是 exdiv（441 文件，14:00–14:06）**；但**这些 exdiv 文件在 09-03 当时是否被消费、是否足以造成 0→131 的跃变，未被证实**——因为当时代码是否加载 exdiv sidecar 无法复现（`b573525` 版 feed **不使用** exdiv，而参数形态又指向更新的工作树）。**未证实。**

3. **`data/dividend_stocks` 的 symbol 集合在 09-03 两跑之间是否变化，无法证实**——目录 mtime（`..`=09-03 08:55）与个别 `_bars.done`（13:27–13:44）提示采集仍在收尾，但缺少当时的目录清单快照。**未证实。**

4. **`_bars.done`（13:27–13:44）究竟属于"补采/重采"还是"首采收尾"，未证实**（`data/collect_retry.log`、`collect_v2.log` 无对应 13:xx 时间戳；`collect_full*.log` 截止 08:09）。

5. **附带发现（非本任务范围，未展开）**：`docs/T312_FINAL_SUMMARY.md` 正文表格陈述的正式产物指标（`round_trips=102`、`commission=1363.34`、`stamp=1840.40`、`slippage=1490.77`，见该文档 `:129/:135/:141`）与磁盘上的 `20260907-150402` 实际 JSON（`round_trips=78`、`commission=1556.18`、`stamp=2651.94`、`slippage=0`）**对不上**。
   > 注：本报告撰写期间（2026-09-10 15:16）该文档已被**另一名队友**追加了顶部"失实数字与真值"作废/冻结声明（现 `docs/T312_FINAL_SUMMARY.md:3-21`），其对照表与本条结论一致（佣金/印花税/滑点/往返/NAV 逐项列出真值）。**该差异已由团队他处登记**，本报告仅作交叉印证，不重复展开。

6. **未触碰 `finai/sources/`**（370 行 `FINDING-` 守卫红线），仅读取 `scripts/gates/tamper_guard.py:193-218` 的守卫实现逻辑。

---

## 附：本报告用到的关键命令（可复现）

```bash
# 1. params_hash 只覆盖 params（重算）
py -3.11 -c "import json;from pathlib import Path;from backtest.ledger import _canonicalize;..."
# 2. 09-03 窗口无提交
git log --since=2026-09-03 --until=2026-09-05 --stat
# 3. 数据在窗口内变动（exdiv 441 文件 14:00–14:06）
find data/dividend_stocks/exdiv -name '*.parquet' -printf '%TY-%Tm-%Td %TH:%TM:%TS %p\n' | sort
# 4. 日线采集收尾（_bars.done 13:27–13:44）
find data/dividend_stocks -type f -newermt '2026-09-03 13:00' ! -newermt '2026-09-03 15:00' -printf '%TH:%TM:%TS %p\n' | sort
# 5. B/C 两跑逐字节一致（仅 run_id/timestamp 别）
diff experiments/runs/20260903-142212-*.json experiments/runs/20260906-184556-*.json
# 6. 09-03 当时脚本的"空池"路径
git show b573525:scripts/run_dividend_backtest.py | nl -ba | sed -n '96,105p'
```
