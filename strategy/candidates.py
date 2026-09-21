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
    cash_yield_annual: Decimal = Decimal("0")         # 空仓现金年化收益（e6 防御资产近似：货基/逆回购 ~0.02；0=不计息）
    breadth_demote_liquidate: bool = False            # e7 降档即出清：宽度由 attack 跌入 <attack 当日向 mid_cap 收敛（⛔ 默认关——须开关隔离，否则无条件生效污染消融实验）
    breadth_weight_mode: str = "hard"                 # C1 连续权重映射（R9/R10 §B3.1）：'hard'=现行阶跃（默认，基线可比）；'linear'=[defense,attack) 内 mid_cap→1.0 线性裁剪（ice 保留硬阈值）
    attack_instrument: str = ""                       # e15 指数 placebo：非空时 attack 档满仓该单票（如 'sh.510880'），选股层整体旁路（⛔ 默认空——须显式开启，否则无条件生效污染消融实验）
    low_vol_keep_pct: Optional[Decimal] = None        # D2 低波翼：非 None 时 dv 合格候选先按 trailing-250d 波动率升序保留前 pct（0,1]，再做股息率排序/top5/市值加权（⛔ 默认 None=不启用）
    dv_skip_top: int = 0                              # e16 剔尾：dv 降序排序后先跳过前 N 名（实证 top15 尾部逆向选择带），再取候选池（⛔ 默认 0=不跳过）
    max_dividend_yield: Optional[Decimal] = None      # e16 扰动臂替代机制：股息率上限——dv>cap 的极端高息票剔除（⛔ 默认 None=不设上限）
    weight_mode: str = "market_cap"                 # e17 权重形态：'market_cap'=自由流通市值加权（默认，基线可比）；'equal'=等权；'dividend_yield'=股息率加权（⛔ 默认 market_cap——须显式开启，否则无条件生效污染消融实验）
    cash_yield_series: str = ""                       # e6b GC001 日度利率 parquet 路径（date,rate_annual%）；与 cash_yield_annual 互斥
    use_crowding_breaker: bool = False                # e19 D7 拥挤度熔断（默认关，显式开启）
    crowding_series: Optional[Mapping[str, Decimal]] = None  # iso date→roll3y 拥挤分位；缺失日=中性不熔断（roll3y 暖机期结构盲区）
    crowding_threshold: Decimal = Decimal("0.85")     # 熔断触发分位（预登记冻结 0.85）
    crowding_cap: Decimal = Decimal("0.5")            # 触发时目标仓位乘数（减半，差额留现金计 GC001 息）
    pead_entry_mode: str = "rebalance"                # 'event'=公告日事件驱动建仓（需 reserve）；'rebalance'=调仓日并入候选源（软叠加，零闲置现金）
    # e36 C1 合成信号上游叠加（默认 None=逐位复现锚点）：
    # overlay_series: iso date → {symbol: z} 的预计算合成 z 表；
    # overlay_mode: 'tilt'=权重乘 max(0.10,1+λ·clip(z,±2))；'filter'=剔除 z<0 候选
    composite_overlay: Optional[Mapping[str, Mapping[str, Decimal]]] = None
    overlay_mode: str = "tilt"                        # 'tilt' | 'filter'
    overlay_lambda: Decimal = Decimal("0.30")         # e36 冻结 λ=0.30
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
        if self.breadth_weight_mode not in ("hard", "linear"):
            raise ValueError(f"breadth_weight_mode 须为 'hard'/'linear'（fail-closed）: "
                             f"{self.breadth_weight_mode!r}")
        if self.attack_instrument and not self.attack_instrument.startswith(("sh.", "sz.")):
            raise ValueError(f"attack_instrument 须为 'sh./sz.' 前缀代码或空（fail-closed）: "
                             f"{self.attack_instrument!r}")
        if self.low_vol_keep_pct is not None:
            if not isinstance(self.low_vol_keep_pct, Decimal):
                raise TypeError(f"low_vol_keep_pct 须为 Decimal（⛔ 禁 float）: "
                                f"{type(self.low_vol_keep_pct).__name__}")
            if not (_ZERO_ < self.low_vol_keep_pct <= Decimal("1")):
                raise ValueError(f"low_vol_keep_pct 须在 (0, 1] 范围内: "
                                 f"{self.low_vol_keep_pct}")
        if not isinstance(self.dv_skip_top, int) or isinstance(self.dv_skip_top, bool):
            raise TypeError(f"dv_skip_top 须为 int: {self.dv_skip_top!r}")
        if self.dv_skip_top < 0:
            raise ValueError(f"dv_skip_top 须 >= 0: {self.dv_skip_top}")
        if self.max_dividend_yield is not None:
            if not isinstance(self.max_dividend_yield, Decimal):
                raise TypeError(f"max_dividend_yield 须为 Decimal（⛔ 禁 float）: "
                                f"{type(self.max_dividend_yield).__name__}")
            if self.max_dividend_yield <= self.min_dividend_yield:
                raise ValueError(f"max_dividend_yield={self.max_dividend_yield} 须严格大于 "
                                 f"min_dividend_yield={self.min_dividend_yield}"
                                 f"（否则候选恒空——fail-closed）")
        if self.overlay_mode not in ("tilt", "filter"):
            raise ValueError(f"overlay_mode 须为 'tilt'/'filter'，实际={self.overlay_mode}")
        if not isinstance(self.overlay_lambda, Decimal):
            raise TypeError(f"overlay_lambda 须为 Decimal（⛔ 禁 float）: "
                            f"{type(self.overlay_lambda).__name__}")
        if self.weight_mode not in ("market_cap", "equal", "dividend_yield"):
            raise ValueError(f"weight_mode 须为 'market_cap'/'equal'/'dividend_yield'"
                             f"（fail-closed）: {self.weight_mode!r}")
        if self.breadth_mid_cap > Decimal("0.8"):
            raise ValueError(f"breadth_mid_cap 警戒档仓位上限不应超 0.8: {self.breadth_mid_cap}")
        if self.cash_yield_series and Decimal(self.cash_yield_annual) != 0:
            raise ValueError(
                "cash_yield_annual 与 cash_yield_series 互斥（⛔ 双利率源歧义）")
        if not isinstance(self.breadth_ice_confirm_days, int) or isinstance(self.breadth_ice_confirm_days, bool):
            raise TypeError(f"breadth_ice_confirm_days 须为 int: "
                            f"{type(self.breadth_ice_confirm_days).__name__}")
        if self.breadth_ice_confirm_days < 1:
            raise ValueError(f"breadth_ice_confirm_days 须 >= 1: {self.breadth_ice_confirm_days}")
        if self.use_breadth_timing and self.use_ma200_timing:
            raise ValueError("use_breadth_timing 与 use_ma200_timing 互斥，\u26d4 同时开启会产生矛盾择时信号")
        if self.use_breadth_timing and self.breadth_series is None:
            raise ValueError("启用宽度择时时 breadth_series 不得为 None（⛔ Fail-Closed：无证据≠通过）")
        # e19 D7 拥挤度熔断校验（fail-closed）：宽度择时叠加层，
        # 序列缺失不得放行；阈值/仓位乘数须在 (0,1]。
        if self.use_crowding_breaker:
            if not self.use_breadth_timing:
                raise ValueError("use_crowding_breaker 为宽度择时叠加层——"
                                 "请先开启 use_breadth_timing（fail-closed）")
            if self.crowding_series is None:
                raise ValueError("启用拥挤度熔断时 crowding_series 不得为 None"
                                 "（⛔ Fail-Closed：无证据≠通过）")
            for name in ("crowding_threshold", "crowding_cap"):
                v = getattr(self, name)
                if not isinstance(v, Decimal):
                    raise TypeError(f"{name} 须为 Decimal（⛔ 禁 float）: {type(v).__name__}")
                if v <= _ZERO_ or v > Decimal("1"):
                    raise ValueError(f"{name} 须在 (0, 1] 范围内: {v}")
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
        self._crowd_today: Decimal | None = None         # e19：当日拥挤分位（None=暖机盲区/无数据=中性）
        self._crowd_break_count = 0                      # e19：熔断触发计数（附属判据验证用）
        self._breadth_ice_streak = 0                     # 连续处于冰点线下天数
        self._breadth_ice = False                        # True = 已确认冰点、全额避险中
        self._breadth_today: Decimal | None = None       # 当日宽度值（Decimal 纪律）
        # L-2 证据链（只读捕获，不改变任何决策）：每次产仓计划非空的调仓
        # 记录 {date, target_weights(计划市值)}；装配侧取最近一次做权重保真对账。
        self._evidence_rebalances: list[dict[str, Any]] = []
        self._evidence_last_rebalance: dict[str, Any] | None = None
        # D2 低波翼：逐票日收益滚动缓冲（close/preclose−1，250 日窗）——
        # 策略内自算波动率，PIT 正确、零外部数据依赖
        self._ret_buffer: dict[str, deque] = {}

    # ------------------------------------------------------------------
    # 引擎契约
    # ------------------------------------------------------------------

    @staticmethod
    def _breadth_cap(cfg: "DividendConfig", b: Decimal) -> Decimal:
        """宽度 → 目标仓位上限（C1 连续权重映射，R9/R10 §B3.1）。

        'hard'（默认）：与旧 in_mid_zone/mid_cap 语义**逐值等价**——
          b>=attack 满仓；b<attack（含未确认冰点日）一律 mid_cap；
        'linear'：b>=attack 满仓；b<defense 零仓（ice 保留硬阈值）；
          [defense, attack) 内由 mid_cap 线性升至 1.0——把台阶改造成斜坡，
          端点复用既有参数（新增自由度 0），针对 G-2「台阶+棱边」病根。
        """
        one = Decimal("1")
        if b >= cfg.breadth_attack_threshold:
            return one
        if cfg.breadth_weight_mode == "hard":
            return cfg.breadth_mid_cap
        if b < cfg.breadth_defense_threshold:
            return _ZERO_
        span = cfg.breadth_attack_threshold - cfg.breadth_defense_threshold
        frac = (b - cfg.breadth_defense_threshold) / span
        return cfg.breadth_mid_cap + (one - cfg.breadth_mid_cap) * frac

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

        # ⓪- D2：更新逐票日收益缓冲（须在冷启动早退之前——warmup 期也要累积；
        #   仅在低波翼启用时维护——默认构型零开销）
        if cfg.low_vol_keep_pct is not None:
            for _sym, _bar in bars.items():
                if _bar.preclose is not None and _bar.preclose > _ZERO_:
                    self._ret_buffer.setdefault(_sym, deque(maxlen=250)).append(
                        float(_bar.close / _bar.preclose) - 1.0)

        # ⓪ 当日股票池
        if self.universe_provider is not None:
            self.watchlist = list(self.universe_provider(day))

        # ⭐ T312：择时开启 ⇒ 指数恒入选股域（否则 feed 不加载指数 bar，MA200
        #   择时永远拿不到数据）。去重靠引擎 ``_symbols_for`` 的 set 语义。
        if cfg.use_ma200_timing and cfg.index_symbol not in self.watchlist:
            self.watchlist.append(cfg.index_symbol)
        # e15 指数 placebo：攻击资产恒入选股域（feed 需要其 bar 才能计划/成交）
        if cfg.attack_instrument and cfg.attack_instrument not in self.watchlist:
            self.watchlist.append(cfg.attack_instrument)

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
        prev_b = self._breadth_today      # 昨日宽度（降档出清判跨界用）
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

        # ③.6 e19 D7 拥挤度序列查表（每日；缺失日=中性不熔断——roll3y
        #     暖机段为结构盲区，影子探针已声明，非 fail-closed 项）
        self._crowd_today = None
        if cfg.use_crowding_breaker and cfg.crowding_series is not None:
            self._crowd_today = cfg.crowding_series.get(day.isoformat())

        # ③.8 排雷 overlay（每日、先于调仓节拍——事件驱动 T+1 清/减半）
        if cfg.use_landmine_overlay and self._layers is not None:
            self._apply_landmine(day, bars, book, broker)

        # ③.9 PEAD 进攻档持仓管理（每日：到期退出；event 模式另加新事件建仓）
        if cfg.use_pead and self._layers is not None:
            self._apply_pead(day, bars, book, broker)

        # ④ 非调仓日：保持现持仓——但「降档即出清」豁免调仓节拍：
        #    mid_cap=0 语义是警戒区零仓，但此前只在调仓日生效——跌入警戒
        #    区后的非调仓日持仓继续挨跌（归因实证：2020 mid 档 34 天 @31%
        #    仓位贡献 -4.7%）。规则：宽度由 ≥attack 跌入 <attack 当日立即
        #    走正常调仓流程（in_mid_zone 路径会按计划收敛到 mid_cap 上限）。
        #    ⛔ 降档只出清不重置调仓时钟：_last_rebalance_bar 仅在常规
        #    节拍日更新，防出清事件挤占/推迟后续正常调仓。
        demote_due = (
            cfg.breadth_demote_liquidate
            and cfg.use_breadth_timing and not self._breadth_ice
            and self._breadth_today is not None
            and self._breadth_today < cfg.breadth_attack_threshold
            and prev_b is not None and prev_b >= cfg.breadth_attack_threshold)
        scheduled = (self._bar_count - self._last_rebalance_bar
                     >= cfg.rebalance_days)
        if not scheduled and not demote_due:
            return

        # ⑤ 调仓日：标记 + 选股（启用择时时，能走到这里即未触发确认破位）
        if scheduled:
            self._last_rebalance_bar = self._bar_count
        if cfg.attack_instrument:
            # e15 指数 placebo：attack 档满仓单票——选股层整体旁路；
            # bar 缺失（停牌/无数据）⇒ 空目标计划 ⇒ fail-closed 空仓
            signals = [Signal(symbol=cfg.attack_instrument, score=Decimal("1"),
                              reason="attack_instrument placebo")]
        else:
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
        # C1 连续权重映射（R9/R10 §B3.1）：cap=宽度的连续函数；
        # 'hard' 模式与旧 in_mid_zone/mid_cap 语义逐值等价（基线可比）。
        breadth_cap = (self._breadth_cap(cfg, self._breadth_today)
                       if (cfg.use_breadth_timing and self._breadth_today is not None)
                       else None)
        if breadth_cap is not None and breadth_cap == _ZERO_:
            plan, _plan_dropped = {}, ()          # 目标零仓：diff 将出清全部持仓
        else:
            if breadth_cap is not None:
                total_nav = total_nav * breadth_cap
            # e19 D7 拥挤度熔断：调仓计划日 crowd_pct > threshold ⇒ 目标
            # 仓位乘 crowding_cap（减半，差额留现金）。e8b 构型
            # breadth_mid_cap=0 ⇒ 熔断实质只在 attack 档生效；非调仓日
            # 不主动清（拥挤段以周-月计持续，节拍抽样捕获）。
            if (cfg.use_crowding_breaker and self._crowd_today is not None
                    and self._crowd_today > cfg.crowding_threshold):
                total_nav = total_nav * cfg.crowding_cap
                self._crowd_break_count += 1
            # PEAD reserve（仅 event 模式）：披露密集月（1/4/7/8/10，R5 实测
            # 公告聚簇）或有在册持仓时预留现金池；非聚簇月不预留（避免进攻档
            # 长期现金拖累）。rebalance 模式恒为 0——PEAD 走正常目标位资金。
            reserve = _ZERO_
            if (cfg.use_pead and cfg.pead_entry_mode == "event"
                    and (day.month in (1, 4, 7, 8, 10) or self._pead_holds)):
                reserve = cfg.pead_reserve_pct
            plan, _plan_dropped = plan_positions(
                targets, total_nav * (Decimal("1") - reserve), bars,
                cfg.portfolio, weights=self._overlay_tilt(cfg, scores, day))
            # PEAD 在册持仓 sticky：reserve 池按槽位等权给目标市值（防被 diff 卖掉）
            if reserve > _ZERO_ and self._pead_holds:
                each = total_nav * reserve / Decimal(cfg.pead_max_slots)
                for s in self._pead_holds:
                    if bars.get(s) is not None:
                        plan[s] = each

        # L-2 证据链（只读）：非空计划才记（清仓计划 target_weights={} 不构成
        # 权重保真样本——等权分支会把它误判成"无分配证据"）。
        if plan:
            rec = {"date": day.isoformat(),
                   "target_weights": {s: str(v) for s, v in plan.items()}}
            self._evidence_rebalances.append(rec)
            self._evidence_last_rebalance = rec

        # ⑦ 出意图
        held_symbols = list(book.positions.keys()) if hasattr(book, "positions") else []
        current = {s: int(book.positions[s].volume) for s in held_symbols}
        report = diff_to_orders(current, plan, bars, cfg.portfolio)
        self._submit(broker, report.intents, day)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _trailing_vol(self, symbol: str) -> float | None:
        """trailing-250d 日收益波动率（样本 std）；缓冲 <200 日 → None
        （fail-closed：无足够 vol 史的票不允许过 D2 筛）。"""
        buf = self._ret_buffer.get(symbol)
        if buf is None or len(buf) < 200:
            return None
        n = len(buf)
        m = sum(buf) / n
        return (sum((x - m) ** 2 for x in buf) / (n - 1)) ** 0.5

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
                # e16 扰动臂：极端高息上限剔除（dv>cap=困境高息尾部）
                if (cfg.max_dividend_yield is not None
                        and bar.dividend_yield > cfg.max_dividend_yield):
                    continue
                # ① 准入端质量否决（连续分红/ROE-TTM/分红现金流/伪高股息）
                if (cfg.use_quality_veto and self._layers is not None
                        and self._layers.veto_reason(symbol, day)):
                    continue
                # ② 排雷冷却窗禁买（事件窗口语义：pub≤day≤cooldown_until）
                if (cfg.use_landmine_overlay and self._layers is not None
                        and self._layers.landmine_block(symbol, day)):
                    continue
                candidates.append((symbol, bar.dividend_yield, bar.market_cap))

        # D2 低波翼：dv 合格候选先按 trailing-250d 波动率升序截断
        # （无 vol 史=缓冲<200 日的票 fail-closed 排除——无法验证低波不买）
        if cfg.low_vol_keep_pct is not None:
            vol_map = {sym: self._trailing_vol(sym)
                       for sym, _dv, _mc in candidates}
            vol_ok = [c for c in candidates if vol_map[c[0]] is not None]
            vol_ok.sort(key=lambda c: vol_map[c[0]])
            keep_n = max(1, int(len(vol_ok) * cfg.low_vol_keep_pct))
            candidates = vol_ok[:keep_n]

        # 按股息率降序排序
        candidates.sort(key=lambda x: x[1], reverse=True)
        # e16 剔尾：跳过 dv 前 N 名（实证逆向选择带）——候选不足时
        # 切片自然缩短/清空 → 空仓（fail-closed 语义）
        if cfg.dv_skip_top > 0:
            candidates = candidates[cfg.dv_skip_top:]
        # e36 filter 臂：剔除合成 z<0 候选（无 z 记缺省的票不受影响）；
        # 过滤后候选 < min_positions 时按原序补回被删票到下限（防池坍缩）
        if (cfg.composite_overlay is not None and cfg.overlay_mode == "filter"
                and day is not None):
            zmap = cfg.composite_overlay.get(day.isoformat()) or {}
            kept = [c for c in candidates
                    if zmap.get(c[0], Decimal("0")) >= _ZERO_]
            if len(kept) < cfg.min_positions:
                dropped = [c for c in candidates
                           if zmap.get(c[0], Decimal("0")) < _ZERO_]
                kept = kept + dropped[:cfg.min_positions - len(kept)]
            candidates = kept
        top_candidates = candidates[:cfg.candidate_pool_size]

        # 加权（归一化）：e17 权重形态——'market_cap'=自由流通市值占比（默认）/
        # 'equal'=等权 / 'dividend_yield'=股息率加权。score 仅作相对权重，
        # 组合层在 targets 内再归一化，故等权给 1、dv 加权给 dv 原值即可。
        total_market_cap = sum(c[2] for c in top_candidates)
        if cfg.weight_mode == "market_cap" and total_market_cap == _ZERO_:
            return []  # 无有效候选，空仓

        signals = []
        for symbol, div_yield, market_cap in top_candidates[:cfg.default_positions]:
            if cfg.weight_mode == "equal":
                weight = Decimal("1")
            elif cfg.weight_mode == "dividend_yield":
                weight = div_yield
            else:  # "market_cap"（默认，基线可比）
                weight = market_cap / total_market_cap
            signals.append(Signal(
                symbol=symbol,
                score=weight,  # 权重作为 score
                reason=f"股息率 {div_yield * 100:.2f}% / 权重[{cfg.weight_mode}] {weight * 100:.2f}%"
            ))

        return signals

    def _overlay_tilt(self, cfg: DividendConfig, scores: dict,
                      day: _date) -> dict:
        """e36 tilt：对 plan_positions 的 weights 施加 z 乘数（不影响 select_targets 排序）。"""
        if (cfg.composite_overlay is None or cfg.overlay_mode != "tilt"
                or not scores):
            return scores
        zmap = cfg.composite_overlay.get(day.isoformat()) or {}
        if not zmap:
            return scores
        out = dict(scores)
        for s in out:
            z = zmap.get(s)
            if z is not None:
                zc = min(max(z, Decimal("-2")), Decimal("2"))
                out[s] = out[s] * max(Decimal("0.10"),
                                      Decimal("1") + cfg.overlay_lambda * zc)
        return out

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

