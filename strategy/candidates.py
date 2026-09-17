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
from typing import Any, Mapping, Optional, Sequence

from backtest.constants import OrderSide
from backtest.types import Bar, Order, OrderType
from strategy.portfolio import (
    OrderIntent,
    PortfolioConfig,
    diff_to_orders,
    plan_positions,
    select_targets,
)
from strategy.signal_layers import SignalLayers

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
# PEAD 在册对账宽限（bar）：登记后 T+1 成交需 1 日，另留 2 日拒单/停牌缓冲；
# 超过此期限仍无实际持仓的在册项判定为幽灵/僵尸并注销（见 _apply_pead）。
_PEAD_GHOST_GRACE_BARS = 3


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
    timing_breach_buffer: Decimal = Decimal("0.01")     # MA200 破位缓冲带（1%）：收盘 < MA200×(1-1%) 才算有效破位
    timing_breach_confirm_days: int = 2                 # 连续 N 日有效破位才确认清仓（抗震荡市假破位）
    timing_rebuild_confirm_days: int = 1                # 已避险后站回 MA200 当日即解除（不对称：破位 2 日确认防假破，重建 1 日抓 V 型反转起点，证据见 2024-09-26 踏空诊断）
    # —— 市场宽度择时（方案 D，19 号报告 §2.1 三档状态机）——
    use_breadth_timing: bool = False                    # 宽度择时开关（默认关，显式开启）
    # 宽度序列（date_str -> Decimal 宽度值），由回测脚本注入，⛔ 不走 config 默认值
    breadth_series: Optional[Mapping[str, Decimal]] = None
    breadth_attack_threshold: Decimal = Decimal("0.40")   # 进攻档：>40% 满仓出击
    breadth_defense_threshold: Decimal = Decimal("0.20")  # 冰点档：<20% 全额避险
    breadth_mid_cap: Decimal = Decimal("0.50")            # 警戒档（20%~40%）仓位上限 50%
    breadth_ice_confirm_days: int = 2                   # 跌破冰点连续 N 日才清仓（抗假破位）
    # —— Alpha 三层（修池子/排雷/PEAD，2026-09-17 立项；默认关，开启须注入 SignalLayers）——
    use_quality_veto: bool = False                    # ① 准入端质量否决（连续分红/ROE-TTM/分红现金流/伪高股息）
    use_landmine_overlay: bool = False                # ② 持仓内排雷一票否决（T+1 清/减半）
    landmine_cooldown_full: int = 120                 # 全清事件禁买冷却（交易日）
    landmine_cooldown_half: int = 60                  # 减半事件禁买冷却（交易日）
    use_pead: bool = False                            # ③ PEAD 进攻档候选源（扣非 SUE 分位≥80%+DEMAX）
    pead_max_slots: int = 2                           # PEAD 同时持仓上限
    pead_hold_days: int = 30                          # PEAD 持有上限（交易日，20-40 窗口内）
    pead_reserve_pct: Decimal = Decimal("0.40")       # event 模式：进攻档为 PEAD 预留资金比例（2 槽×~20%净值≈常规单票量级，低于单票下限会永远买不进）
    pead_entry_mode: str = "rebalance"                # 'event'=公告日事件驱动建仓（需 reserve）；'rebalance'=调仓日并入候选源（软叠加，零闲置现金）
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
        # 择时确认期 / 缓冲带
        if not isinstance(self.timing_breach_buffer, Decimal):
            raise TypeError(f"timing_breach_buffer 须为 Decimal（⛔ 禁 float）: "
                            f"{type(self.timing_breach_buffer).__name__}")
        if self.timing_breach_buffer < _ZERO_ or self.timing_breach_buffer > Decimal("0.10"):
            raise ValueError(f"timing_breach_buffer 须在 [0, 0.10] 范围内: "
                             f"{self.timing_breach_buffer}")
        for name in ("timing_breach_confirm_days", "timing_rebuild_confirm_days"):
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool):
                raise TypeError(f"{name} 须为 int: {v!r}")
            if v < 1:
                raise ValueError(f"{name} 须 >= 1: {v}")

        # 市场宽度三档参数校验（fail-closed）
        for name in ("breadth_attack_threshold", "breadth_defense_threshold",
                     "breadth_mid_cap"):
            v = getattr(self, name)
            if not isinstance(v, Decimal):
                raise TypeError(f"{name} 须为 Decimal（⛔ 禁 float）: "
                                f"{type(v).__name__}")
            if v < _ZERO_ or v > Decimal("1"):
                raise ValueError(f"{name} 须在 [0, 1] 范围内: {v}")
        if self.breadth_defense_threshold >= self.breadth_attack_threshold:
            raise ValueError(f"breadth_defense_threshold={self.breadth_defense_threshold} "
                             f"须严格小于 breadth_attack_threshold={self.breadth_attack_threshold}")
        if self.breadth_mid_cap > Decimal("0.8"):
            raise ValueError(f"breadth_mid_cap 警戒档仓位上限不应超 0.8: {self.breadth_mid_cap}")
        if not isinstance(self.breadth_ice_confirm_days, int) or isinstance(self.breadth_ice_confirm_days, bool):
            raise TypeError(f"breadth_ice_confirm_days 须为 int: "
                            f"{type(self.breadth_ice_confirm_days).__name__}")
        if self.breadth_ice_confirm_days < 1:
            raise ValueError(f"breadth_ice_confirm_days 须 >= 1: {self.breadth_ice_confirm_days}")
        if self.use_breadth_timing and self.use_ma200_timing:
            raise ValueError("use_breadth_timing 与 use_ma200_timing 互斥，\u26d4 同时开启会产生矛盾择时信号")
        if self.use_breadth_timing and self.breadth_series is None:
            raise ValueError("启用宽度择时时 breadth_series 不得为 None（⛔ Fail-Closed：无证据≠通过）")
        # Alpha 三层参数校验（fail-closed）
        for name in ("landmine_cooldown_full", "landmine_cooldown_half",
                     "pead_max_slots", "pead_hold_days"):
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool):
                raise TypeError(f"{name} 须为 int: {v!r}")
            if v < 1:
                raise ValueError(f"{name} 须 >= 1: {v}")
        if self.pead_max_slots > self.max_positions:
            raise ValueError(f"pead_max_slots={self.pead_max_slots} 不应超过 "
                             f"max_positions={self.max_positions}")
        if self.pead_hold_days > 60:
            raise ValueError(f"pead_hold_days={self.pead_hold_days} 超出 20-60 "
                             f"交易日漂移窗口上限（R5 裁决口径）")
        if not isinstance(self.pead_reserve_pct, Decimal):
            raise TypeError(f"pead_reserve_pct 须为 Decimal（⛔ 禁 float）: "
                            f"{type(self.pead_reserve_pct).__name__}")
        if self.pead_reserve_pct < _ZERO_ or self.pead_reserve_pct > Decimal("0.5"):
            raise ValueError(f"pead_reserve_pct 须在 [0, 0.5] 范围内: "
                             f"{self.pead_reserve_pct}")
        if self.use_pead and not self.use_breadth_timing:
            raise ValueError("use_pead 仅在宽度择时进攻档下合法（R5：PEAD 只在进攻档"
                             "做候选源）——请先开启 use_breadth_timing")
        if self.pead_entry_mode not in ("event", "rebalance"):
            raise ValueError(f"pead_entry_mode 须为 'event'|'rebalance': "
                             f"{self.pead_entry_mode!r}")


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
        signal_layers: SignalLayers | None = None,
    ) -> None:
        """
        Args:
            config: 红利策略参数包。
            universe_provider: ``(date) -> Iterable[str]`` 型回调，回测里返回**当日**
                可交易池（防幸存者偏差）。``None`` = 由调用方手动维护 ``watchlist``。
            signal_layers: Alpha 三层查询对象（修池子/排雷/PEAD 的只读数据）。
                任一 ``use_*`` 开关打开而本参数为 ``None`` ⇒ **fail-closed raise**
                （⛔ 不许静默降级为“无约束”）。
        """
        self.config = config or DividendConfig()
        cfg = self.config
        need_layers = (cfg.use_quality_veto or cfg.use_landmine_overlay
                       or cfg.use_pead)
        if need_layers and signal_layers is None:
            raise ValueError(
                "use_quality_veto/use_landmine_overlay/use_pead 已开启但 "
                "signal_layers=None —— ⛔ Fail-Closed：先跑 "
                "scripts/build_signal_layers.py 并由 runner 注入")
        self._layers = signal_layers
        self.universe_provider = universe_provider
        self.watchlist: list[str] = []
        # Alpha 三层运行态（全部确定性推进，无随机源）
        self._lm_cursor: dict[str, int] = {}        # symbol → landmine 事件游标
        self._lm_pending: dict[str, list] = {}      # symbol → 已见未了事件队列
        self._pead_holds: dict[str, int] = {}       # symbol → 建仓 bar_count
        self._pead_acted: set = set()               # 已建仓 event_id 幂等集
        self._bar_count = 0
        self._last_rebalance_bar = -1
        self._ma200_buffer: deque[Decimal] = deque(maxlen=200)
        self._pending_ids: dict[str, int] = {}           # symbol → 已下单计数（幂等）
        # MA200 择时状态机：缓冲带 + 双向确认期（抗震荡市假破位反复止损）
        self._breach_streak = 0                          # 连续有效破位（收在缓冲带之下）天数
        self._timing_avoid = False                       # True = 已确认破位、处于避险状态
        self._rebuild_streak = 0                         # 避险中连续站回 MA200 天数
        # 市场宽度择时状态机（方案 D，19 号报告 §2.1）
        self._breadth_ice_streak = 0                     # 连续处于冰点线下天数
        self._breadth_ice = False                        # True = 已确认冰点、全额避险中
        self._breadth_today: Decimal | None = None       # 当日宽度值（Decimal 纪律）

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

        # ③ 每日 MA200 择时（缓冲带 + 双向确认期；信号日 T 下单、T+1 成交）
        #    有效破位：收盘 < MA200 × (1 − 缓冲带)；连续 N 日有效破位才确认清仓。
        #    避险期间：连续 M 日收盘 ≥ MA200 才解除避险、允许调仓重建（对称确认）。
        #    缓冲带内（MA200×(1−buf) ≤ 收盘 < MA200）：维持现状，不清仓不重建。
        if cfg.use_ma200_timing:
            if len(self._ma200_buffer) < 200:
                # MA200 未凑够 → 不交易（冷启动延长期）
                return
            ma200 = sum(self._ma200_buffer) / len(self._ma200_buffer)
            breach_line = ma200 * (Decimal("1") - cfg.timing_breach_buffer)

            if self._timing_avoid:
                # 避险状态：等待站回 MA200 的对称确认
                if index_bar.close >= ma200:
                    self._rebuild_streak += 1
                    if self._rebuild_streak >= cfg.timing_rebuild_confirm_days:
                        self._timing_avoid = False
                        self._rebuild_streak = 0
                        self._breach_streak = 0
                else:
                    self._rebuild_streak = 0
                # 无论是否解除避险，当日均不重建（解除后待下一调仓节拍）
                if self._timing_avoid:
                    return
                return  # 解除避险当日也不立即建仓，等下一调仓节拍

            # 正常持仓状态
            if index_bar.close < breach_line:
                self._breach_streak += 1
                if self._breach_streak >= cfg.timing_breach_confirm_days:
                    # 确认破位 → 空目标计划 ⇒ 现持仓全部清仓（plan 外持仓 SELL 全清）
                    self._timing_avoid = True
                    self._breach_streak = 0
                    self._rebuild_streak = 0
                    held_symbols = list(book.positions.keys()) if hasattr(book, "positions") else []
                    current = {s: int(book.positions[s].volume) for s in held_symbols}
                    report = diff_to_orders(current, {}, bars, cfg.portfolio)
                    self._submit(broker, report.intents, day)
                    return
                # 确认期内：不清仓、不调仓（等待确认）
                return
            elif index_bar.close >= ma200:
                # 站回 MA200 之上：破位序列中断，重置计数
                self._breach_streak = 0
            # else：缓冲带内（breach_line ≤ close < ma200）→ 保留破位计数，维持现状

        # ③.5 市场宽度择时（方案 D）：冰点确认清仓 + 警戒仓位管控
        self._breadth_today = None
        if cfg.use_breadth_timing:
            b = cfg.breadth_series.get(day.isoformat()) if cfg.breadth_series else None
            if b is None:
                raise ValueError(
                    f"宽度序列缺失 {day.isoformat()}（⛔ Fail-Closed：交易期无宽度数据不得放行）")
            self._breadth_today = b
            if self._breadth_ice:
                if b >= cfg.breadth_defense_threshold:
                    self._breadth_ice = False
                    self._breadth_ice_streak = 0
                return  # 解除当日也不建仓，等下一调仓节拍
            if b < cfg.breadth_defense_threshold:
                self._breadth_ice_streak += 1
                if self._breadth_ice_streak >= cfg.breadth_ice_confirm_days:
                    self._breadth_ice = True
                    self._breadth_ice_streak = 0
                    held_symbols = list(book.positions.keys()) if hasattr(book, "positions") else []
                    current = {s: int(book.positions[s].volume) for s in held_symbols}
                    report = diff_to_orders(current, {}, bars, cfg.portfolio)
                    self._submit(broker, report.intents, day)
                    return
                return  # 确认期内不清仓不调仓
            else:
                self._breadth_ice_streak = 0

        # ③.8 排雷 overlay（每日、先于调仓节拍——事件驱动 T+1 清/减半）
        if cfg.use_landmine_overlay and self._layers is not None:
            self._apply_landmine(day, bars, book, broker)

        # ③.9 PEAD 进攻档持仓管理（每日：到期退出；event 模式另加新事件建仓）
        if cfg.use_pead and self._layers is not None:
            self._apply_pead(day, bars, book, broker)

        # ④ 非调仓日：保持现持仓
        if self._bar_count - self._last_rebalance_bar < cfg.rebalance_days:
            return

        # ⑤ 调仓日：标记 + 选股（启用择时时，能走到这里即未触发确认破位）
        self._last_rebalance_bar = self._bar_count
        signals = self._select_stocks(bars, cfg, day)

        # ⑥ 组合计划（复用 portfolio.py 三段链，传入市值权重）
        scores = {s.symbol: s.score for s in signals}
        # 方案 D 警戒区（defense ≤ 宽度 < attack）：仓位上限 breadth_mid_cap（默认 50%）。
        # ⛔ 上限语义而非资金缩放：mid_cap 是『目标仓位占净值比例上限』，调仓日据此
        #   生成目标计划并由 diff 出清超出部分；mid_cap=0 表示警戒区目标零仓（合法
        #   配置，区别于『拿 0 资金做计划』——后者会触发 plan_positions 的 total_nav>0
        #   守卫而崩溃）。
        in_mid_zone = (cfg.use_breadth_timing and self._breadth_today is not None
                       and self._breadth_today < cfg.breadth_attack_threshold)
        # PEAD rebalance 模式（软叠加）：进攻档调仓日把在册持仓+当日 active
        # 事件并入候选源——不占闲置现金、不中途追高；持有到期由 _apply_pead
        # 日频卖出。冻结票（当日无 bar）不注入，防 plan 缺数据除名后被 diff 追杀。
        pead_new: dict[str, tuple] = {}  # symbol → event_id（本轮新并入）
        if (cfg.use_pead and cfg.pead_entry_mode == "rebalance"
                and self._layers is not None and not in_mid_zone):
            top_score = max(scores.values()) if scores else Decimal("1")
            picks: list[str] = [s for s in self._pead_holds
                                if bars.get(s) is not None]
            for ev in sorted(self._layers.pead_active(day),
                             key=lambda e: -e.pct_rank):
                if len(picks) >= cfg.pead_max_slots:
                    break
                s = ev.symbol
                if (s in picks or s in self._pead_holds
                        or ev.event_id in self._pead_acted):
                    continue
                if (cfg.use_landmine_overlay
                        and self._layers.landmine_block(s, day)):
                    continue
                if (cfg.use_quality_veto
                        and self._layers.veto_reason(s, day)):
                    continue
                if bars.get(s) is None:
                    continue
                picks.append(s)
                pead_new[s] = ev.event_id
            for s in picks:
                scores[s] = top_score   # 与红利头部同权，等权入计划
        targets = select_targets(scores, cfg.portfolio)
        if cfg.use_pead and cfg.pead_entry_mode == "event":
            # event 模式在册持仓由 reserve 池供资 ⇒ 从红利目标剔除防双重计价
            pead_syms = set(self._pead_holds)
            targets = [t for t in targets if t not in pead_syms]
        # 并入成功的新 PEAD 目标登记为在册（起算持有期）
        for s, eid in pead_new.items():
            if s in targets:
                self._pead_holds[s] = self._bar_count
                self._pead_acted.add(eid)
        total_nav = book.total_nav if hasattr(book, "total_nav") else getattr(book, "nav", _ZERO_)
        if in_mid_zone and cfg.breadth_mid_cap == _ZERO_:
            plan, _plan_dropped = {}, ()          # 警戒区目标零仓：diff 将出清全部持仓
        else:
            if in_mid_zone:
                total_nav = total_nav * cfg.breadth_mid_cap
            # PEAD reserve（仅 event 模式）：披露密集月（1/4/7/8/10，R5 实测
            # 公告聚簇）或有在册持仓时预留现金池；非聚簇月不预留（避免进攻档
            # 长期现金拖累）。rebalance 模式恒为 0——PEAD 走正常目标位资金。
            reserve = _ZERO_
            if (cfg.use_pead and cfg.pead_entry_mode == "event"
                    and (day.month in (1, 4, 7, 8, 10) or self._pead_holds)):
                reserve = cfg.pead_reserve_pct
            plan, _plan_dropped = plan_positions(
                targets, total_nav * (Decimal("1") - reserve), bars,
                cfg.portfolio, weights=scores)
            # PEAD 在册持仓 sticky：reserve 池按槽位等权给目标市值（防被 diff 卖掉）
            if reserve > _ZERO_ and self._pead_holds:
                each = total_nav * reserve / Decimal(cfg.pead_max_slots)
                for s in self._pead_holds:
                    if bars.get(s) is not None:
                        plan[s] = each

        # ⑦ 出意图
        held_symbols = list(book.positions.keys()) if hasattr(book, "positions") else []
        current = {s: int(book.positions[s].volume) for s in held_symbols}
        report = diff_to_orders(current, plan, bars, cfg.portfolio)
        self._submit(broker, report.intents, day)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _select_stocks(self, bars: Mapping[str, Bar], cfg: DividendConfig,
                       day: _date | None = None) -> list[Signal]:
        """选股：股息率筛选 + 排序 + 市值加权（+ 准入质量否决 + 排雷冷却）。

        ``day`` 在 ``use_quality_veto`` 开启时必传（质量否决是日频 PIT 表）。
        """
        if cfg.use_quality_veto and day is None:
            raise ValueError("use_quality_veto 开启时 _select_stocks 必须传 day"
                             "（质量否决是日频 PIT 表，⛔ 不许缺省）")
        candidates = []
        for symbol, bar in bars.items():
            if symbol == cfg.index_symbol:
                continue  # 跳过指数自身

            # 检查必需字段（fail-closed）
            if bar.dividend_yield is None or bar.market_cap is None:
                continue  # 数据不全，跳过

            if bar.dividend_yield >= cfg.min_dividend_yield:
                # ① 准入端质量否决（连续分红/ROE-TTM/分红现金流/伪高股息）
                if (cfg.use_quality_veto and self._layers is not None
                        and self._layers.veto_reason(symbol, day)):
                    continue
                # ② 排雷冷却窗禁买（事件窗口语义：pub≤day≤cooldown_until）
                if (cfg.use_landmine_overlay and self._layers is not None
                        and self._layers.landmine_block(symbol, day)):
                    continue
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

    # ------------------------------------------------------------------
    # Alpha 三层：排雷 overlay + PEAD 事件驱动（每日调用，先于调仓节拍）
    # ------------------------------------------------------------------

    def _apply_landmine(self, day: _date, bars: Mapping[str, Bar],
                        book: Any, broker: Any) -> None:
        """排雷 overlay：持仓内一票否决（硬约束，T+1 开盘执行）。

        口径：事件 ``pub_date <= day`` 即“新可见”（公告日当晚披露 ⇒ 次日开盘
        成交，与撮合 FR-BT-6 次一开盘天然对齐）；**同日同票只执行最强一个
        动作**（L2+L3+L4 连击只减半一次，防级联减到 1/8）；exit_full 在
        停牌日留 pending 次日重试（跌停卖不出由撮合拒单，次日位置仍在 ⇒
        再试）；exit_half 只消费一次。block_only 不动仓（仅禁买窗口生效）。
        冷却窗口由 ``SignalLayers.landmine_block`` 按事件窗语义判定
        （pub_date+cooldown 自然日），与本处动作解耦——卖出后游标停住也
        不会因买回而重触发同一事件（事件还在窗口期内继续禁买）。
        """
        if not hasattr(book, "positions"):
            return
        for symbol in list(book.positions.keys()):
            vol_now = int(book.positions[symbol].volume)
            if vol_now <= 0:
                # 空仓即清陈旧 pending：防「卖出→买回→旧事件对新仓位重触发」空转
                self._lm_pending.pop(symbol, None)
                continue
            pending = self._lm_pending.setdefault(symbol, [])
            cur = self._lm_cursor.get(symbol, 0)
            events = self._layers.landmine_list(symbol)
            while cur < len(events) and events[cur].pub_date <= day:
                ev = events[cur]
                # 只接收仍处冷却窗口内的事件——过期事件不重触发（窗口外买回
                # 的仓位不应被旧事件追杀；窗口语义见 landmine_block）
                if ev.cooldown_until is None or day <= ev.cooldown_until:
                    pending.append(ev)
                cur += 1
            self._lm_cursor[symbol] = cur
            if not pending:
                continue
            # 同日只执行最强动作：任一 exit_full → 全清；否则任一 exit_half → 减半一次
            full = any(e.action == "exit_full" for e in pending)
            half = any(e.action == "exit_half" for e in pending)
            bar = bars.get(symbol)
            still: list = []
            if bar is None:
                still = [e for e in pending if e.action != "block_only"]
                still += [e for e in pending if e.action == "block_only"]
                # 停牌：全部留队明日重试
            elif full:
                self._submit(broker, [OrderIntent(
                    symbol, OrderSide.SELL, vol_now)], day)
                # exit_full 硬约束重试到出清；exit_half/block_only 已消费
                still = [e for e in pending if e.action == "exit_full"]
            elif half:
                sell_vol = vol_now // 200 * 100
                if sell_vol <= 0:
                    sell_vol = vol_now           # 不足两手 → 全清更保守
                self._submit(broker, [OrderIntent(
                    symbol, OrderSide.SELL, sell_vol)], day)
            self._lm_pending[symbol] = still

    def _apply_pead(self, day: _date, bars: Mapping[str, Bar],
                    book: Any, broker: Any) -> None:
        """PEAD 进攻档事件驱动持仓（到期退出 + 新事件建仓）。

        口径（R5 §4.2 S4 复合）：只在宽度进攻档建仓；信号源是离线构建的
        eligible PEAD 事件（扣非 SUE 分位≥80% + DEMAX 条件化 + 未触板）；
        持有 ``pead_hold_days`` 交易日到期退出（20-40 漂移窗口内）；
        建仓资金来自进攻档 reserve 池（``pead_reserve_pct``，调仓日预留）。
        """
        cfg = self.config
        # ① 到期退出 + 在册对账（每日，不等调仓节拍）
        #    ⛔ 对账修复（E4 审计实锤）：_pead_holds 只在 targets 阶段登记，
        #    与 book.positions 无同步——产生两类幽灵：
        #    a) 纯幽灵：进 targets 后被 plan_positions 丢弃，从未发出 BUY
        #       （探针实证 sz.000014 在册 34 bar 零持仓零意图）；
        #    b) 僵尸：被排雷/冰点/警戒等外部路径出清后登记簿不注销，继续
        #       占槽位并被 scores 注入买回（实证 300443 被排雷卖出后由
        #       PEAD 注入重新买回——排雷与 PEAD 互搏，E4 回撤恶化真因）。
        #    规则：在册 ≥3 bar（覆盖 T+1 成交 + 拒单/停牌缓冲）仍无实际
        #    持仓 → 注销。event_id 保留在 _pead_acted，防同事件反复纠缠。
        for s, entry_bc in list(self._pead_holds.items()):
            vol = int(book.positions[s].volume) if (
                hasattr(book, "positions") and s in book.positions) else 0
            age = self._bar_count - entry_bc
            if age >= cfg.pead_hold_days:
                if vol > 0 and bars.get(s) is not None:
                    self._submit(broker, [OrderIntent(
                        s, OrderSide.SELL, vol)], day)
                self._pead_holds.pop(s, None)
            elif vol <= 0 and age >= _PEAD_GHOST_GRACE_BARS:
                self._pead_holds.pop(s, None)

        # ② 非进攻档不建仓（冰点/警戒 PEAD 关闭——宽度择时最高优先级）；
        #    rebalance 模式下建仓只在调仓日发生（软叠加，不占闲置现金）
        attack = (self._breadth_today is not None
                  and self._breadth_today >= cfg.breadth_attack_threshold)
        if (not attack or cfg.pead_entry_mode != "event"
                or len(self._pead_holds) >= cfg.pead_max_slots):
            return

        nav = book.total_nav if hasattr(book, "total_nav") else getattr(
            book, "nav", _ZERO_)
        cash = getattr(book, "cash", _ZERO_)
        active = sorted(self._layers.pead_active(day),
                        key=lambda e: -e.pct_rank)
        for ev in active:
            if len(self._pead_holds) >= cfg.pead_max_slots:
                break
            s = ev.symbol
            if (s in self._pead_holds
                    or (hasattr(book, "positions") and s in book.positions
                        and int(book.positions[s].volume) > 0)
                    or ev.event_id in self._pead_acted):
                continue
            if (cfg.use_landmine_overlay
                    and self._layers.landmine_block(s, day)):
                continue
            if (cfg.use_quality_veto
                    and self._layers.veto_reason(s, day)):
                continue
            bar = bars.get(s)
            if bar is None or bar.limit_up or bar.limit_down:
                continue
            if bar.amount < cfg.portfolio.min_daily_amount:
                continue
            if (cfg.portfolio.max_price is not None
                    and bar.close > cfg.portfolio.max_price):
                continue
            # 槽位资金 = 净值 × reserve ÷ 总槽位数；参与率与单票下限沿用组合层口径
            value = nav * cfg.pead_reserve_pct / Decimal(cfg.pead_max_slots)
            value = min(value, bar.amount * cfg.portfolio.max_participation_rate)
            if cash < value * Decimal("1.01"):   # 预留费用缓冲
                continue
            shares = int(value / bar.close) // 100 * 100
            if bar.close * shares < cfg.portfolio.min_position_value:
                continue
            self._submit(broker, [OrderIntent(s, OrderSide.BUY, shares)], day)
            self._pead_holds[s] = self._bar_count
            self._pead_acted.add(ev.event_id)

