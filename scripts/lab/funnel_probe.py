#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A-1 建不满仓诊断 —— 漏斗观测探针（⛔ 只加计数，不改任何策略行为）。

原理：继承 ``DividendStrategy``，利用其 ``self._select_stocks`` 多态钩子
收集选股漏斗计数；``on_bar`` 前后快照择时状态机，推导每日归因。

每日记录（daily_log）：日期 / 持仓数 / MA200 区域 / 避险状态前后值 /
是否调仓到期 / 是否实际调仓。

调仓日记录（funnel_log）七关计数：
    F0 股票池供给（bars 内非指数标的数）
    F2 数据残缺（dividend_yield / market_cap 缺失）
    F3 股息率低于下限
    F4 候选池溢出（排序后超出 candidate_pool_size）
    F5 名额截取（候选池内超出 default_positions）
    F6 组合层淘汰（停牌/超价/流动性/单票下限，由 plan_positions 包装器注入）
    signals_out 实际产出信号数

F1（择时拦截）不在选股内——由 daily_log 的『调仓到期但未执行』推导。

实例注册表 ``instances``：回测函数内部实例化策略后，驱动脚本经此取回探针。
"""
from __future__ import annotations

from datetime import date as _date
from typing import Any, ClassVar, Mapping

from strategy.candidates import DividendConfig, DividendStrategy, Signal

_ZERO = None  # 占位，避免误 import Decimal


class DividendFunnelProbe(DividendStrategy):
    """红利策略漏斗探针（与父类行为逐字节一致，仅追加观测计数）。"""

    #: 驱动脚本取回最新实例用（回测函数内部实例化、外部拿不到引用）。
    instances: ClassVar[list["DividendFunnelProbe"]] = []

    def __init__(self, config: DividendConfig, *, universe_provider: Any | None = None):
        super().__init__(config, universe_provider=universe_provider)
        self.daily_log: list[dict[str, Any]] = []
        self.funnel_log: list[dict[str, Any]] = []
        self._probe_day: _date | None = None
        self._pending_funnel: dict[str, Any] | None = None
        self._f6_dropped: list[tuple[str, str]] = []
        DividendFunnelProbe.instances.append(self)

    # ------------------------------------------------------------------
    # 引擎契约（观测包装）
    # ------------------------------------------------------------------

    def on_bar(self, day: _date, bars: Mapping[str, Any], book: Any, broker: Any) -> None:
        cfg = self.config
        bc_after = self._bar_count + 1                     # 父类进入即 +1
        pre_avoid = self._timing_avoid
        pre_last_rb = self._last_rebalance_bar
        warmup_done = bc_after >= cfg.warmup_bars
        due = warmup_done and (bc_after - pre_last_rb) >= cfg.rebalance_days

        self._probe_day = day
        self._f6_dropped = []
        self._pending_funnel = None
        super().on_bar(day, bars, book, broker)

        executed = warmup_done and (self._last_rebalance_bar == self._bar_count)

        # MA200 区域（与策略同口径：缓冲带之下 / 缓冲带 / 均线下方 / 均线上方）
        zone = "na"
        if cfg.use_ma200_timing and len(self._ma200_buffer) >= 200:
            idx_bar = bars.get(cfg.index_symbol)
            if idx_bar is not None:
                ma200 = sum(self._ma200_buffer) / len(self._ma200_buffer)
                breach_line = ma200 * (1 - cfg.timing_breach_buffer)
                c = idx_bar.close
                if c < breach_line:
                    zone = "below_breach"
                elif c < ma200:
                    zone = "buffer"
                else:
                    zone = "above"

        # 调仓日漏斗行落盘（_select_stocks 已暂存计数；此处补 F6 后归档）
        if self._pending_funnel is not None:
            row = self._pending_funnel
            reason_hist: dict[str, int] = {}
            for _sym, reason in self._f6_dropped:
                reason_hist[reason] = reason_hist.get(reason, 0) + 1
            row["f6_portfolio_dropped"] = len(self._f6_dropped)
            row["f6_reasons"] = reason_hist
            self.funnel_log.append(row)
            self._pending_funnel = None

        # 有效持仓口径：清仓后 positions 条目残留（volume=0），须按 volume>0 过滤
        n_pos = sum(
            1 for p in (getattr(book, "positions", {}) or {}).values()
            if getattr(p, "volume", 0) > 0
        )
        self.daily_log.append({
            "date": day.isoformat(),
            "warmup_done": warmup_done,
            "zone": zone,
            "avoid_pre": pre_avoid,
            "avoid_post": self._timing_avoid,
            "breach_cleared_today": (not pre_avoid) and self._timing_avoid,
            "rebuild_cleared_today": pre_avoid and (not self._timing_avoid),
            "due": due,
            "executed": executed,
            "positions": n_pos,
        })

    # ------------------------------------------------------------------
    # 选股钩子（观测包装：先计数，再原样调父类）
    # ------------------------------------------------------------------

    def _select_stocks(self, bars: Mapping[str, Any], cfg: DividendConfig) -> list[Signal]:
        universe = [s for s in bars if s != cfg.index_symbol]
        f0 = len(universe)
        data_ok = []
        f2 = 0
        for symbol in universe:
            bar = bars[symbol]
            if bar.dividend_yield is None or bar.market_cap is None:
                f2 += 1
            else:
                data_ok.append((symbol, bar))
        above = [(s, b) for s, b in data_ok if b.dividend_yield >= cfg.min_dividend_yield]
        f3 = len(data_ok) - len(above)
        ranked = sorted(((s, b.dividend_yield, b.market_cap) for s, b in above),
                        key=lambda x: x[1], reverse=True)
        f4 = max(0, len(ranked) - cfg.candidate_pool_size)
        pool = ranked[: cfg.candidate_pool_size]
        f5 = max(0, len(pool) - cfg.default_positions)

        signals = super()._select_stocks(bars, cfg)          # ⛔ 行为不变，产出原样

        self._pending_funnel = {
            "date": self._probe_day.isoformat() if self._probe_day else None,
            "f0_universe": f0,
            "f2_data_missing": f2,
            "f3_below_yield": f3,
            "f4_pool_overflow": f4,
            "f5_over_positions": f5,
            "signals_out": len(signals),
        }
        return signals
