#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""=============================================================================
⛔⛔⛔  R5 弃用横幅 —— TDX 腿已砍（方案 B），v1 不可用  ⛔⛔⛔
=============================================================================
⭐ **本模块在 v1 不可用（设计如此，⛔ 不是回归）**。

  · 根因（R5，`REVALIDATE.md`）：`connect()` 与 `_market_id()` 在**调用时**
    懒导入 `finai.tdx_minute5`，而该文件模块级 `import finai.data_catalog`
    —— **两者均未搬入新仓** ⇒ 触发即抛 ``ModuleNotFoundError``（这是预期，
    不要当成新缺陷去查）。
  · `fetch_bars()` 因此**永远取不到数**：它把异常归入 `FetchResult`（状态
    `FAIL_PROBE_BUG`，因文本含 "no module named"），**返回非 OK 结果而不抛**。
  · 处置（R5 方案 B）：v1 纯日线（spec §2.2：多仓、不加杠杆、仅用日线），
    **整腿砍掉**，`tdx_minute5` 留在旧仓。本文件**刻意保留、不删**，作钉位 —
    未来若需分钟线，走 R5 **方案 A**（搬入最小裁剪版 `data_catalog.py`）恢复腿，
    恢复时须把 R2 的截断显式归 `FAIL_DETERMINISTIC` 并写 `meta['truncated_bars']`。
  · 同步标注：`finai/sources/__init__.py::SOURCE_REGISTRY["tdx"]`（verified→False）
    与 `capability_router.CAPABILITIES["daily_bar"]` 里 `tdx::daily_bar` 候选的 note。

以下（横幅之下）为历史封装说明，描述的是腿**曾经**的行为，仅供参考：
=============================================================================

TDX（通达信）适配器 —— **包装项目已有的连接层，不造平行实现**。

⭐ 关键决定（实测依据）：项目里已有一套正确的 TDX 连接实现
`finai/tdx_minute5.py::_connect_tdx_api()`，R28 实测可用 ——
返回 **48 根**、首根 `09:35`、末根 `15:00`（栅格完全合规），且已具备：
  · 健康节点缓存（`data/tdx_healthy_nodes.json`，TTL 1 小时，实测存在）
  · `get_security_count > 0` 健全性检查
  · 空响应视为失败并换下一节点
  · 10 台内置服务器（实测 **3/10 可连**：218.75.126.9 / 115.238.56.198 / 60.12.136.250）
故本模块**只做封装与状态归类**，⛔ 不重写服务器选择逻辑。

封装的陷阱：
`FINDING-178`/`FINDING-189`：mootdx 的 `StdQuotes()` 默认 `bestip=False`，
  既不 pin 服务器也不探测最佳 IP，实测 daily 与 5min **恒返回 0 行且不抛异常**；
  而生产代码 `phase1/data_collector/mootdx.py:120` 的 `if df.empty: return []`
  会把它**静默吞掉**（该兜底通道因此从未真正生效）。
  → 本模块一律走 `_connect_tdx_api()`，并把 0 行归 `EMPTY_OK` 而非成功。

⭐ category 编码为**实测值**（2026-08-05，在 60.12.136.250 上逐个验证间隔）：
    0 → 5min      1 → 15min     7 → 1min      8 → 1min      9 → daily
  ⚠ 与 mootdx 文档声称的 "8=5min" **不符**，与 `finai/tdx_minute5.py:846` 的
  代码注释（0=5min/7=1min/9=daily）一致。⛔ 不要按文档改，按实测。
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from finai.sources.base import (
    FetchResult,
    classify_exception,
    make_result,
)

SOURCE = "tdx"

#: 实测的 category → bar 间隔映射（2026-08-05 在实盘服务器上逐个验证）
CATEGORY_5MIN = 0
CATEGORY_15MIN = 1
CATEGORY_1MIN = 7
CATEGORY_DAILY = 9

_CATEGORY_MEASURED = {
    CATEGORY_5MIN: "5min",
    CATEGORY_15MIN: "15min",
    CATEGORY_1MIN: "1min",
    8: "1min",
    CATEGORY_DAILY: "daily",
}

#: 单次请求最多 800 根（TDX 协议上限）
MAX_BARS_PER_REQUEST = 800
#: ⛔ 台账守护：新增的覆盖率校验不登记新编号，保持台账行数恒定（370）。
#:   （静默一族的既有编号在上文 docstring，不在此处重复。）


class BarTruncationError(ValueError):
    """TDX 返回条数 < 请求条数（静默截断，R2）。⛔ 绝不静默返回部分数据（硬约束②）。

    是 ``ValueError`` 子类 —— 与 ``UnknownAdjustment`` 同套路（``adjustment_mode.py:49``）：
    调用方可用 ``ValueError`` 兜底捕获。实例携带 ``requested``/``got``/``truncated_bars``
    供血缘审计写 ``meta``。
    """

    def __init__(self, requested: int, got: int) -> None:
        self.requested = requested
        self.got = got
        self.truncated_bars = requested - got
        super().__init__(
            f"TDX 静默截断：请求 {requested} 根，实得 {got} 根，"
            f"缺 {self.truncated_bars} 根（R2；⛔ 不得降级为部分数据）")


def _assert_coverage(frames: list[pd.DataFrame], *, requested: int, got: int) -> None:
    """TDX 分页尾部覆盖率校验（R2）——纯函数、⛔ 无网络、无库调用。

    ``got < requested`` 即**静默截断**：抛 ``BarTruncationError``，
    绝不"凑活返回部分数据"（`base.py:22` 硬约束②）。
    ``got == requested``（含 0）放行返回 ``None``；``frames`` 只为形态锚定
    （强制逐帧累计由调用侧先做好，⛔ 本函数**不**重数 `sum(map(len, frames))`）。
    ⚠ 现喂入的是"服务端按 count 返回条数"的**实得计数**，不是按交易日历算的期望
    —— 那是 `segmented_pull.expected_sessions` 的判决层，与本纯断言分工不同。
    """
    if got < requested:
        raise BarTruncationError(requested=requested, got=got)


def _market_id(code: str) -> int:
    """沪深北市场编号。复用生产实现，避免第 N 份代码身份归一化（`FINDING-20` 教训）。"""
    from finai.tdx_minute5 import _market_for_code

    return _market_for_code(code.split(".")[0])


def connect() -> Any:
    """取一个已验证可用的 TDX 连接。⛔ 不要自己 new TdxHq_API 或用 StdQuotes()。"""
    from finai.tdx_minute5 import _connect_tdx_api

    return _connect_tdx_api()


def fetch_bars(
    code: str,
    *,
    category: int = CATEGORY_5MIN,
    count: int = 48,
    client: Any | None = None,
) -> FetchResult:
    """取 K 线。

    Args:
        code: `000001` 或 `000001.SZ` 均可。
        category: 用模块常量，**不要写字面量** —— 实测 0=5min / 1=15min /
            7=1min / 8=1min / 9=daily，与 mootdx 文档不符（见模块 docstring）。
        count: bar 条数（不是日期区间）。单次上限 800，超出会自动分页。

    实测基线（2026-08-05）：`category=0, count=48` → **48 行**，
    首根 `09:35`、末根 `15:00`；`category=9, count=20` → **20 行**。

    ⛔ 0 行返回 `EMPTY_OK` 而非成功 —— `FINDING-178`/`-189` 的核心教训：
    静默 0 行与"确实无数据"在返回值上无法区分。
    """
    if category not in _CATEGORY_MEASURED:
        return FetchResult(
            state="FAIL_PROBE_BUG", source=SOURCE,
            detail=(f"category={category} 未经实测。已验证的取值: "
                    f"{sorted(_CATEGORY_MEASURED)} → {_CATEGORY_MEASURED}"),
        )
    own = client is None
    api = None
    try:
        api = client or connect()
        pure = code.split(".")[0]
        market = _market_id(code)
        frames: list[pd.DataFrame] = []
        remaining = count
        start = 0
        while remaining > 0:
            take = min(remaining, MAX_BARS_PER_REQUEST)
            data = api.get_security_bars(category, market, pure, start, take)
            if not data:
                break  # 无更多数据；总量由下方 make_result 判 EMPTY_OK
            frames.append(pd.DataFrame(data))
            got = len(data)
            start += got
            remaining -= got
            if got < take:
                break
        frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        # ⭐ R2：分页尾部覆盖率校验（占位正确性，⛔ 非网络路径）。`got < requested`
        #   即抛 `BarTruncationError` → 落入下方 `except`（⛔ 绝不静默返回部分数据，
        #   `base.py:22` 硬约束②）。⚠ 现腿按 R5 方案 B 砍掉（依赖 `finai.tdx_minute5`/
        #   `finai.data_catalog` 未搬入），`connect()` 恒 `ModuleNotFoundError` ⇒
        #   本行**永不执行**，仅作"R5 方案 A 恢复腿时把校验接回去"的钉位。
        #   ⚠ 已知未尽：恢复腿时须让截断显式归 `FAIL_DETERMINISTIC` 并写
        #   `meta['truncated_bars']`（R2 修复要求②）——当前 `classify_exception`
        #   对 `BarTruncationError` 的文本归 `FAIL_UNREACHABLE`，见 REVALIDATE.md R2。
        _assert_coverage(frames, requested=count, got=len(frame))
        return make_result(frame, source=SOURCE, evidence={
            "code": code, "category": category,
            "interval_measured": _CATEGORY_MEASURED[category],
            "requested": count,
            "got": len(frame),
        })
    except BaseException as exc:  # noqa: BLE001
        return FetchResult(
            state=classify_exception(exc), source=SOURCE,
            detail=f"{type(exc).__name__}: {str(exc)[:200]}",
            evidence={"code": code, "category": category},
        )
    finally:
        if own and api is not None:
            try:
                api.disconnect()
            except Exception:  # noqa: BLE001, S110
                pass
