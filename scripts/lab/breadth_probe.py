#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""方案 D 市场宽度择时归因诊断 —— 宽度观测探针（⛔ 只加观测，不改任何策略行为）。

原理：继承 ``DividendStrategy``，在 ``on_bar`` 前后快照宽度状态机与组合状态，
推导每日归因；行为与父类逐字节一致。

每日记录（daily_log）：
    date            日期
    warmup_done     冷启动是否完成
    breadth         当日宽度值（缺失时为 null）
    zone            档位: attack(>进攻线) / mid(警戒区) / ice_pending(冰点确认期) / ice(已确认冰点)
    ice_streak_pre  进入当日前的连续冰点天数
    ice_pre         进入当日前是否处于已确认冰点
    ice_post        当日结束后是否处于已确认冰点
    positions       有效持仓数（volume>0）
    nav             当日总市值（book.total_nav，缺省回退 nav）
    cash            当日现金（若可得）
    pos_value_est   估算持仓市值 = nav - cash（现金不可得时为 null）
    due/executed    调仓节拍

实例注册表 ``instances``：回测函数内部实例化策略后，驱动脚本经此取回探针。
"""
from __future__ import annotations

from datetime import date as _date
from typing import Any, ClassVar, Mapping

from strategy.candidates import DividendConfig, DividendStrategy


class DividendBreadthProbe(DividendStrategy):
    """红利策略宽度归因探针（与父类行为逐字节一致，仅追加观测记录）。"""

    #: 驱动脚本取回最新实例用（回测函数内部实例化、外部拿不到引用）。
    instances: ClassVar[list["DividendBreadthProbe"]] = []

    def __init__(self, config: DividendConfig, *, universe_provider: Any | None = None):
        super().__init__(config, universe_provider=universe_provider)
        self.daily_log: list[dict[str, Any]] = []
        DividendBreadthProbe.instances.append(self)

    # ------------------------------------------------------------------
    # 引擎契约（观测包装）
    # ------------------------------------------------------------------

    def on_bar(self, day: _date, bars: Mapping[str, Any], book: Any, broker: Any) -> None:
        cfg = self.config
        bc_after = self._bar_count + 1
        warmup_done = bc_after >= cfg.warmup_bars
        pre_last_rb = self._last_rebalance_bar
        due = warmup_done and (bc_after - pre_last_rb) >= cfg.rebalance_days

        pre_ice = self._breadth_ice
        pre_streak = self._breadth_ice_streak

        super().on_bar(day, bars, book, broker)

        executed = warmup_done and (self._last_rebalance_bar == self._bar_count)

        b = self._breadth_today
        if b is None:
            zone = "na"
        elif self._breadth_ice:
            zone = "ice"
        elif b < cfg.breadth_defense_threshold:
            zone = "ice_pending"
        elif b < cfg.breadth_attack_threshold:
            zone = "mid"
        else:
            zone = "attack"

        n_pos = sum(
            1 for p in (getattr(book, "positions", {}) or {}).values()
            if getattr(p, "volume", 0) > 0
        )
        nav_attr = getattr(book, "total_nav", None)
        nav = None
        try:
            nav = nav_attr() if callable(nav_attr) else nav_attr
        except Exception:
            nav = None
        if nav is None:
            nav = getattr(book, "nav", None)
        cash = getattr(book, "cash", None)
        pos_value = None
        try:
            if nav is not None and cash is not None:
                pos_value = float(nav) - float(cash)
        except (TypeError, ValueError):
            pos_value = None

        self.daily_log.append({
            "date": day.isoformat(),
            "warmup_done": warmup_done,
            "breadth": float(b) if b is not None else None,
            "zone": zone,
            "ice_streak_pre": pre_streak,
            "ice_pre": pre_ice,
            "ice_post": self._breadth_ice,
            "positions": n_pos,
            "nav": float(nav) if nav is not None else None,
            "cash": float(cash) if cash is not None else None,
            "pos_value_est": pos_value,
            "due": due,
            "executed": executed,
        })
