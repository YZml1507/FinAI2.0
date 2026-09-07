#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T302 候选策略 v1 —— 日线中低频（动量 + 流动性过滤 + 时间退出）。

**定位**：首个用于走通「信号 → 组合 → 回测 → 指标」全链的可跑策略基座，
不追求超额收益（那是 T303 参数扫描/T304 压力测试的活）。形态：周线级换仓的
价格动量 + A 股流动性过滤 + 持仓时长上限，配 `strategy/portfolio.py` 的
组合管理器出意图、⛔ 不直接舔撮合。

策略读数（⛔ 交易语义只信 `bar.limit_up/limit_down/exdiv/is_st` 等注入列，
绝不读 parquet 原生价量字段判断买不买——R1/R4 数据卫生原则）：

| 信号（每个交易日对 watchlist 算） | 口径 |
|---|---|
| ``momentum`` | 最近 ``lookback`` 个可用 bar 的区间收益率 ``close / close[-lookback] - 1`` |
| ``liquid`` | 当日 ``bar.amount`` ≥ ``min_daily_amount``（与组合层口径一致） |
| ``exit_after`` | 持仓超过 ``max_holding_days`` ⇒ 无条件退出（时间退出，FR-PM-3 最简分支） |

**出场优先级**：时间退出 > 动量跌出前 N > 持有不动。止损/跟踪止损留 T302 后
续迭代（本版只放「跌出榜单」这一种动量退出，⛔ 不做价格跌幅止损）。

线程模型：`on_bar(date, bars, book, broker)` 被引擎每日回调；内部状态仅是
「每个 symbol 的买入日/持有天数」——状态推进确定性、无随机数（NFR-7 seed
钉扎在 registry：`seed=None` 也通过因无随机源）。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date as _date
from decimal import Decimal
from typing import Any, Mapping, Sequence

from backtest.constants import OrderSide
from backtest.types import Bar, Order, OrderType
from strategy.portfolio import (
    OrderIntent,
    PortfolioConfig,
    diff_to_orders,
    plan_positions,
    select_targets,
)

__all__ = ["MomentumConfig", "MomentumStrategy", "DividendConfig", "DividendStrategy"]


@dataclass(frozen=True)
class MomentumConfig:
    """策略参数（进入 registry `params` 的载体；全部显式、无隐藏默认）。"""

    lookback: int = 20                    # 动量回看窗口（bar 数）
    rebalance_days: int = 5               # 每 N 个交易日重评分一次（中低频）
    max_holding_days: int = 40            # 时间退出上限（交易日）
    warmup_bars: int = 25                 # 冷启动：凑不够这个 bar 数不评分（⛔ 不雪球）
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)

    def __post_init__(self) -> None:
        if not isinstance(self.lookback, int) or self.lookback < 2:
            raise ValueError(f"lookback 须为 ≥2 的 int: {self.lookback!r}")
        if not isinstance(self.rebalance_days, int) or self.rebalance_days < 1:
            raise ValueError(f"rebalance_days 须为 ≥1 的 int: {self.rebalance_days!r}")
        if not isinstance(self.max_holding_days, int) or self.max_holding_days < 1:
            raise ValueError(f"max_holding_days 须为 ≥1 的 int: {self.max_holding_days!r}")
        if self.warmup_bars < self.lookback:
            raise ValueError(
                f"warmup_bars={self.warmup_bars} < lookback={self.lookback}（ Welch 不出来）")


class MomentumStrategy:
    """日线动量候选策略（鸭子类型，引擎只要 ``on_bar`` + ``watchlist``）。"""

    def __init__(
        self,
        config: MomentumConfig | None = None,
        universe_provider: Any | None = None,
    ) -> None:
        """
        Args:
            config: 策略参数包。
            universe_provider: ``(date) -> Iterable[str]`` 型回调，回测里返回**当日**
                可交易池（防幸存者偏差）。``None`` = 由调用方手动维护 ``watchlist``
                （向后兼容，但仍推荐注入——T108 要求历史成分回放）。
        """
        self.config = config or MomentumConfig()
        self.universe_provider = universe_provider
        self.watchlist: list[str] = []                     # 引擎在此读范围
        self._bars_seen: dict[str, int] = {}             # symbol → 已见 bar 数（含停牌日缺席）
        self._closes: dict[str, deque[Decimal]] = {}     # symbol → 最近 window 收盘
        self._entry_date: dict[str, _date] = {}          # symbol → 实际建仓日
        self._step = 0                                    # 已走过交易日数
        self._pending_ids: dict[str, int] = {}           # symbol → 已下单 client 计数（幂等）

    # ------------------------------------------------------------------
    # 引擎契约
    # ------------------------------------------------------------------

    def on_bar(self, day: _date, bars: Mapping[str, Bar], book: Any, broker: Any) -> None:
        cfg = self.config
        self._step += 1

        # ⓪ 当日股票池：无 provider 时保持手工 watchlist（向后兼容）
        if self.universe_provider is not None:
            self.watchlist = list(self.universe_provider(day))

        # ① 消化当日 bar：补收盘价历史
        for symbol in self.watchlist:
            bar = bars.get(symbol)
            self._bars_seen[symbol] = self._bars_seen.get(symbol, 0) + (1 if bar else 0)
            if bar is not None:
                self._closes.setdefault(symbol, deque(maxlen=cfg.lookback + 5))
                self._closes[symbol].append(bar.close)

        # ② 非调仓日不动手（中低频）
        if self._step % cfg.rebalance_days != 0:
            return

        # ③ 清仓超期持仓（时间退出，不看信号）
        held_symbols = list(book.positions.keys()) if hasattr(book, "positions") else []
        current = {s: int(book.positions[s].volume) for s in held_symbols}

        # ④ 评分（只在 ≥warmup 的标的上）
        scores: dict[str, Decimal] = {}
        for symbol in self.watchlist:
            if self._bars_seen.get(symbol, 0) < cfg.warmup_bars:
                continue
            bar = bars.get(symbol)
            if bar is None:
                continue                                  # 停牌不评分
            if getattr(bar, "limit_up", False) or getattr(bar, "limit_down", False):
                continue                                  # 涨跌停不参与（不抢板）
            closes = list(self._closes.get(symbol, ()))
            if len(closes) < cfg.lookback:
                continue
            momentum = closes[-1] / closes[-cfg.lookback] - 1
            scores[symbol] = momentum

        # ⑤ 组合计划（择时空仓也合法）
        targets = select_targets(scores, cfg.portfolio)
        total_nav = book.total_nav if hasattr(book, "total_nav") else getattr(book, "nav", _ZERO_)
        plan, _plan_dropped = plan_positions(targets, total_nav, bars, cfg.portfolio)

        # 时间退出独立的 path：持仓超期 ⇒ 目标计划里**强行剔除**
        max_hold = cfg.max_holding_days
        held_to_exit = [
            s for s in list(plan)
            if s in self._entry_date and (day - self._entry_date[s]).days >= max_hold
        ]
        for s in held_to_exit:
            plan.pop(s, None)

        # ⑥ 出意图
        report = diff_to_orders(current, plan, bars, cfg.portfolio)
        self._submit(broker, report.intents, day)

        # ⑦ 挂机清理：不再出现（不再持有也不在清单）的标的，忘掉它的 entry 记录
        live = set(plan) | {s for s in current if s in plan}
        for dead in [s for s in self._entry_date if s not in live]:
            # 已清仓且不在计划里 → 记录可清
            if dead not in current:
                self._entry_date.pop(dead, None)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _submit(self, broker: Any, intents: Sequence[OrderIntent], day: _date) -> None:
        for intent in intents:
            count = self._pending_ids.get(intent.symbol, 0) + 1
            self._pending_ids[intent.symbol] = count
            oid = f"t302-{intent.symbol}-{intent.side.value}-{day.isoformat()}-{count}"
            broker.submit(Order(
                client_order_id=oid, symbol=intent.symbol, side=intent.side,
                order_type=OrderType.MARKET, volume=intent.volume, price=None,
                created_date=day))
            # 建仓成功记录买入日（撮合失败不影响——entry 按 intent 记录，
            # 到时间退出的判读容错：即使没审到成交也在 plan 层重评）
            if intent.side is OrderSide.BUY:
                self._entry_date.setdefault(intent.symbol, day)


_ZERO_ = Decimal("0")


# ==============================================================================
# T311 红利策略（低 beta 股息率排序 + 市值加权 + MA200 择时保护）
# ==============================================================================


@dataclass(frozen=True)
class DividendConfig:
    """红利策略配置（FR-PM-1 红利分支 + 择时退出）。

    v1 口径声明：
    * 选股：股息率 ≥ ``min_dividend_yield`` → 按股息率降序取前 ``candidate_pool_size`` 只
    * 加权：自由流通市值加权（市值越大权重越高，归一化后传组合层）
    * 择时：指数（默认沪深 300）收盘价 < MA200 → 全部空仓退出（FR-PM-2 择时分支）
    * 调仓：每 ``rebalance_days`` 个交易日（默认 20 = 月度）
    * 持仓时长：无时间退出（只在调仓日被动调整，对比 MomentumStrategy 的 max_hold）
    """

    min_dividend_yield: Decimal = Decimal("0.03")       # 股息率下限（3%）
    candidate_pool_size: int = 50                       # 股息率排序后候选池规模
    min_positions: int = 5
    max_positions: int = 8
    default_positions: int = 5
    use_ma200_timing: bool = True                       # MA200 择时开关
    index_symbol: str = "sh.000300"                     # 沪深 300 作为市场基准
    rebalance_days: int = 20                            # 调仓频率（交易日）
    warmup_bars: int = 210                              # 冷启动期（≥200 + 缓冲）
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)

    def __post_init__(self) -> None:
        """参数校验（fail-closed）。"""
        # min_dividend_yield 须为 Decimal [0, 0.20]
        if not isinstance(self.min_dividend_yield, Decimal):
            raise TypeError(f"min_dividend_yield 须为 Decimal（⛔ 禁 float）: "
                            f"{type(self.min_dividend_yield).__name__}")
        if self.min_dividend_yield < _ZERO_ or self.min_dividend_yield > Decimal("0.20"):
            raise ValueError(f"min_dividend_yield 须在 [0, 0.20] 范围内: "
                             f"{self.min_dividend_yield}")

        # 整数字段校验
        for name in ("candidate_pool_size", "min_positions", "max_positions",
                     "default_positions", "rebalance_days", "warmup_bars"):
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool):
                raise TypeError(f"{name} 须为 int: {v!r}")

        # 持仓数范围
        if not (self.min_positions <= self.default_positions <= self.max_positions):
            raise ValueError(
                f"default_positions={self.default_positions} 须在 "
                f"[{self.min_positions}, {self.max_positions}] 内")

        # 候选池须 >= 最大持仓数
        if self.candidate_pool_size < self.max_positions:
            raise ValueError(
                f"candidate_pool_size={self.candidate_pool_size} 须 >= "
                f"max_positions={self.max_positions}")

        # 调仓频率 / 冷启动期
        if self.rebalance_days < 1:
            raise ValueError(f"rebalance_days 须 >= 1: {self.rebalance_days}")
        if self.warmup_bars < 200:
            raise ValueError(f"warmup_bars 须 >= 200（MA200 最小需求）: {self.warmup_bars}")


@dataclass(frozen=True)
class Signal:
    """策略信号（symbol + score + 原因）。"""
    symbol: str
    score: Decimal
    reason: str = ""


class DividendStrategy:
    """红利策略：股息率排序 + 市值加权 + MA200 择时。

    选股逻辑：
    1. 筛选股息率 >= min_dividend_yield 的股票
    2. 按股息率降序排序，取前 candidate_pool_size 只
    3. 按自由流通市值加权分配（市值越大权重越高）
    4. MA200 择时：指数收盘价 < MA200 → 空仓退出

    调仓频率：月度（rebalance_days=20）
    持仓时长：无时间退出（只在调仓日被动调整）
    """

    def __init__(
        self,
        config: DividendConfig | None = None,
        universe_provider: Any | None = None,
    ) -> None:
        """
        Args:
            config: 红利策略参数包。
            universe_provider: ``(date) -> Iterable[str]`` 型回调，回测里返回**当日**
                可交易池（防幸存者偏差）。``None`` = 由调用方手动维护 ``watchlist``。
        """
        self.config = config or DividendConfig()
        self.universe_provider = universe_provider
        self.watchlist: list[str] = []
        self._bar_count = 0
        self._last_rebalance_bar = -1
        self._ma200_buffer: deque[Decimal] = deque(maxlen=200)
        self._pending_ids: dict[str, int] = {}           # symbol → 已下单计数（幂等）

    # ------------------------------------------------------------------
    # 引擎契约
    # ------------------------------------------------------------------

    def on_bar(self, day: _date, bars: Mapping[str, Bar], book: Any, broker: Any) -> None:
        """每日回调（引擎契约）。

        Args:
            day: 当日日期
            bars: {symbol: Bar}（含 dividend_yield / market_cap 字段）
            book: 账本视图（读 positions / nav）
            broker: 下单接口（.submit(Order)）
        """
        cfg = self.config
        self._bar_count += 1

        # ⓪ 当日股票池
        if self.universe_provider is not None:
            self.watchlist = list(self.universe_provider(day))

        # ⭐ T312：择时开启 ⇒ 指数恒入选股域（否则 feed 不加载指数 bar，MA200
        #   择时永远拿不到数据）。去重靠引擎 ``_symbols_for`` 的 set 语义。
        if cfg.use_ma200_timing and cfg.index_symbol not in self.watchlist:
            self.watchlist.append(cfg.index_symbol)

        # ① 冷启动期：只收集 MA200 数据，不交易
        if self._bar_count < cfg.warmup_bars:
            # 冷启动期间收集指数数据（如果使用择时）
            if cfg.use_ma200_timing:
                index_bar = bars.get(cfg.index_symbol)
                if index_bar is not None:
                    self._ma200_buffer.append(index_bar.close)
            return

        # ② 每日更新 MA200 缓存（交易期必须有指数数据）
        if cfg.use_ma200_timing:
            index_bar = bars.get(cfg.index_symbol)
            if index_bar is None:
                raise ValueError(f"指数 {cfg.index_symbol} 数据缺失（MA200 择时必需）")
            self._ma200_buffer.append(index_bar.close)

        # ③ 非调仓日：保持现持仓
        if self._bar_count - self._last_rebalance_bar < cfg.rebalance_days:
            return

        # ④ 调仓日标记
        self._last_rebalance_bar = self._bar_count

        # ⑤ MA200 择时检查
        signals: list[Signal] = []
        if cfg.use_ma200_timing:
            if len(self._ma200_buffer) >= 200:
                ma200 = sum(self._ma200_buffer) / len(self._ma200_buffer)

                # 指数 < MA200 → 空仓（不产信号 → 组合层全部清仓）
                if index_bar.close < ma200:
                    signals = []
                else:
                    # 指数 >= MA200 → 正常选股
                    signals = self._select_stocks(bars, cfg)
            else:
                # MA200 未凑够 → 不交易（冷启动延长期）
                return
        else:
            # 不使用择时 → 直接选股
            signals = self._select_stocks(bars, cfg)

        # ⑥ 组合计划（复用 portfolio.py 三段链，传入市值权重）
        scores = {s.symbol: s.score for s in signals}
        targets = select_targets(scores, cfg.portfolio)
        total_nav = book.total_nav if hasattr(book, "total_nav") else getattr(book, "nav", _ZERO_)
        plan, _plan_dropped = plan_positions(
            targets, total_nav, bars, cfg.portfolio, weights=scores)

        # ⑦ 出意图
        held_symbols = list(book.positions.keys()) if hasattr(book, "positions") else []
        current = {s: int(book.positions[s].volume) for s in held_symbols}
        report = diff_to_orders(current, plan, bars, cfg.portfolio)
        self._submit(broker, report.intents, day)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _select_stocks(self, bars: Mapping[str, Bar], cfg: DividendConfig) -> list[Signal]:
        """选股：股息率筛选 + 排序 + 市值加权。"""
        candidates = []
        for symbol, bar in bars.items():
            if symbol == cfg.index_symbol:
                continue  # 跳过指数自身

            # 检查必需字段（fail-closed）
            if bar.dividend_yield is None or bar.market_cap is None:
                continue  # 数据不全，跳过

            if bar.dividend_yield >= cfg.min_dividend_yield:
                candidates.append((symbol, bar.dividend_yield, bar.market_cap))

        # 按股息率降序排序，取前 N 只
        candidates.sort(key=lambda x: x[1], reverse=True)
        top_candidates = candidates[:cfg.candidate_pool_size]

        # 市值加权（归一化）
        total_market_cap = sum(c[2] for c in top_candidates)
        if total_market_cap == _ZERO_:
            return []  # 无有效候选，空仓

        signals = []
        for symbol, div_yield, market_cap in top_candidates[:cfg.default_positions]:
            weight = market_cap / total_market_cap
            signals.append(Signal(
                symbol=symbol,
                score=weight,  # 市值权重作为 score
                reason=f"股息率 {div_yield * 100:.2f}% / 市值权重 {weight * 100:.2f}%"
            ))

        return signals

    def _submit(self, broker: Any, intents: Sequence[OrderIntent], day: _date) -> None:
        """提交订单意图（复用 MomentumStrategy 的模式）。"""
        for intent in intents:
            count = self._pending_ids.get(intent.symbol, 0) + 1
            self._pending_ids[intent.symbol] = count
            oid = f"t311-{intent.symbol}-{intent.side.value}-{day.isoformat()}-{count}"
            from backtest.types import Order, OrderType
            broker.submit(Order(
                client_order_id=oid, symbol=intent.symbol, side=intent.side,
                order_type=OrderType.MARKET, volume=intent.volume, price=None,
                created_date=day))

