#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R5（TDX/5min 腿已砍，方案 B）单测 —— 全部**离线**，⛔ 无任何网络/TDX 调用。

对应 `REVALIDATE.md R5`：`tdx_source.py` 的 TDX 腿在**调用时**（非 import 时）
失败 —— `connect()`/`_market_id()` 懒导入 `finai.tdx_minute5`，后者模块级
`import finai.data_catalog`，而**两者均未搬入新仓**（v1 纯日线，spec §2.2，
故按方案 B 整腿砍掉，`tdx_minute5` 留在旧仓）。

四条钉位（全部确定性、可离线复现）：
  ① `import finai.sources.tdx_source` **成功**（懒导入把崩点推迟到调用时，
     import 层面不阻塞其他模块 —— 这正是 R5"import 冒烟通过"的成因）；
  ② `connect()` / `_market_id()` 抛 ``ModuleNotFoundError``（缺依赖，预期，
     ⛔ 不是回归）；
  ③ `fetch_bars()` **不抛**（它把异常归入 `FetchResult`），返回
     `state == FAIL_PROBE_BUG`（`classify_exception` 对 "no module named" 的归类，
     即"与数据源能力无关"）且 `.ok is False`；
  ④ `capability_router.CAPABILITIES["daily_bar"]` 里 `tdx::daily_bar` 候选的
     `note` 带 R5 不可用标注（R5 验收判据："lib='tdx' 的候选被标记为不可用
     并有明确注释说明原因"）。

⛔⛔ 永不静默原则：断言不被 try/except 吞错。本模块不打网、不连 TDX。
"""
from __future__ import annotations

import pytest

from finai.sources import capability_router, tdx_source
from finai.sources.base import FAIL_PROBE_BUG

#: R5 不可用标注的关键词（与三处源码标注逐字一致）。
R5_MARKER = "R5：TDX 腿已砍"


def test_import_tdx_source_succeeds() -> None:
    """① import 层面不阻塞：懒导入把崩点推迟到调用时。"""
    import finai.sources.tdx_source  # noqa: F401  # 导入成功即通过


def test_connect_and_market_raise_module_not_found() -> None:
    """② connect()/‌_market_id() 缺依赖即抛 ModuleNotFoundError（预期）。"""
    with pytest.raises(ModuleNotFoundError):
        tdx_source.connect()
    with pytest.raises(ModuleNotFoundError):
        tdx_source._market_id("000001")


def test_fetch_bars_returns_fail_probe_bug_instead_of_raising() -> None:
    """③ fetch_bars() 不抛：把异常归入 FetchResult，归 FAIL_PROBE_BUG 且非 OK。"""
    res = tdx_source.fetch_bars("000001")
    assert res.state == FAIL_PROBE_BUG, (
        f"缺依赖应归 FAIL_PROBE_BUG（与数据源能力无关），实得 {res.state!r}"
    )
    assert not res.ok  # ⛔ 0 行/失败不得被读成成功


def test_router_tdx_daily_bar_candidate_marked_unavailable() -> None:
    """④ capability_router 里 tdx::daily_bar 候选的 note 带 R5 不可用标注。"""
    cands = capability_router.CAPABILITIES["daily_bar"]
    tdx = [c for c in cands if c.key.startswith("tdx::")]
    assert tdx, "daily_bar 能力下应存在 tdx:: 前缀的候选"
    assert any(R5_MARKER in (c.note or "") for c in tdx), (
        "tdx::daily_bar 候选的 note 缺少 R5 不可用标注"
    )
