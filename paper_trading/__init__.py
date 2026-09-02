#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T401 模拟盘执行器（Paper Trading）。

本包实现模拟盘执行器，复用 Phase 2 回测引擎的全部抽象（SDD-1 四环境同构）。
唯一差异：数据源（回测=历史 Parquet；模拟盘=实时/盘后采集）。

模块结构：
  - broker.py       —— PaperBroker（继承 Broker 接口，复用撮合/账本）
  - runner.py       —— PaperTradingRunner（日终任务编排）
  - config.py       —— PaperTradingConfig（初始资金/策略参数/路径）
  - state.py        —— 状态持久化（持仓/资金/订单队列）

SDD-1 同构原则（spec SDD-1）：
  - 撮合规则（matching.py）：完全复用，8 条规则一致
  - 账本（ledger.py）：完全复用，双账本+幂等键
  - 状态机（order_fsm.py）：完全复用，七态迁移表
  - T+1 约束：完全一致，今日信号→明日开盘成交

v1 简化路径（盘后采集+次日回放，与回测同构）：
  - 不做实时行情接入（避免引入 WebSocket/消息队列复杂度）
  - 日终触发：盘后采集→策略信号→订单提交→次日开盘撮合
  - 状态持久化：JSON 存储（持仓/资金/订单队列），幂等重跑保护

T401 交付物：
  - config.py / state.py / broker.py / runner.py
  - tests/test_t401_paper_trading.py（≥10 单测）
  - docs/t401_paper_trading_design.md
"""
from __future__ import annotations

__all__ = []
