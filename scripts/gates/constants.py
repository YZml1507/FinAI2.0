#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁体系**单一事实源常量**（Single Source of Truth）。

全仓任何"单测基线 passed 数"只能引用本模块的 :data:`TEST_BASELINE_PASSED`，
⛔ 不得在别处再写字面量（否则人肉同步治不住漂移——见 `T405` 同文件 24/28 并存）。

* 门禁**数量**不在此固化：它由 ``GateMasterAudit.get_standard_gates()`` **动态取值**，
  新增/下线门禁即自动生效，避免"写死 29"二次漂移。
"""

from __future__ import annotations

__all__ = ["TEST_BASELINE_PASSED"]

#: 历史核准的单测最低通过基线（任何时候不得低于此数值）。
#: 引用方：``scripts/hooks/pre_push.py``（防倒退），``scripts/gates/gate_consistency.py``（G-DOC-1 文档一致性）。
TEST_BASELINE_PASSED = 780
