#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T203 费用模型 —— FR-BT-7（逐项对照 research-finai《07 号 A股交易规则与交易成本数据手册》）。

**唯一**费率登记点：A 股的六个成本科目（``backtest.constants.FeeItem``）在这里
按"带生效日的分段费率"建模，⛔ 不许在撮合 / 账本 / 策略里再写费率字面量。

## 六科目对照表（07 号手册 §C + §E2；FR-BT-7）

| FeeItem | 方向 | 现行费率 | 分段沿革 | 07 号出处 |
|---|---|---|---|---|
| ``COMMISSION`` | 买卖双向 | 0.00025（万 2.5 全佣） | 行业默认档长期稳定 | §C① |
| ``STAMP_TAX`` | **仅卖出** | 0.0005（0.5‰） | 2023-08-28 起 1‰ → 0.5‰（财政部/税务总局 2023 年第 39 号） | §C② |
| ``TRANSFER_FEE`` | 买卖双边 | 0.00001（0.01‰） | 2022-04-29 起 0.02‰ → 0.01‰（中国结算 2022-04-28 通知） | §C③ |
| ``HANDLING_FEE`` | 买卖双边 | 沪深 0.0000341；**北交所 0.000125** | 沪深 2023-08-28 起 0.00487% → 0.00341%；北交所独立路径 0.25‰ → 0.125‰（北证公告〔2023〕54号，同日施行） | §C④ |
| ``MANAGEMENT_FEE`` | 买卖双边 | 0.00002（0.02‰ 证管费） | 发改价格〔2012〕2119号，延续执行至今 | §C⑤ |
| ``SLIPPAGE`` | 买卖双边 | 默认 5bps，压测 15bps | 非规费、无官方值；业界 5–15bps/边 | §E2 |

### ⚠ 三条容易踩的口径坑

1. **北交所不可套用沪深经手费**：现行 0.125‰ ≈ 沪深 0.00341% 的 **3.67 倍**，
   套错低估近 3 倍成本。北交所以代码前缀 ``43 / 83 / 87 / 92`` 识别
   （见 :meth:`FeeConfig.is_bj`）。
2. **回测自 2015-01-01** ⇒ 2015–2022 段过户费必须走**旧率 0.00002**、
   2015–2023 段经手费必须走**旧率 0.0000487**（沪深）/ **0.00025**（北交所）、
   印花税走**旧率 0.001**。⛔ 拿现行费率回刷十年历史 = 系统性低估成本。
3. **证管费（``MANAGEMENT_FEE``）常被券商包进"全佣"**里不单独列账。本模块仍
   **逐项透视**（默认 rate=0.00002 单独计一行）：口径更保守、归因更清楚。若上层
   用的是"含规费全佣"报价，把 ``management_fee_rate`` 与
   ``handling_fee_schedules_*`` 显式置 0 以免重复计费。

### 滑点与"不重复计费"

滑点是**成交价的函数**，不是加在价格之外的一笔费：走 :func:`apply_slippage`
把撮合锚定价推成实际成交价（BUY 更贵、SELL 更贱）。因此
:func:`compute_fees` 默认给 ``SLIPPAGE`` 记 **0** —— 价里已经含了，再记一笔就是
双重扣现金（``MatchEngine`` 把 ``sum(fees.values())`` 直接从现金里扣）。
只在**归因报表**需要把滑点单列时才传 ``base_price=``（滑点前的锚定价），
此时 ``SLIPPAGE`` = ``|成交价 - 锚定价| × volume``，⛔ 该结果不可再进现金流。

### 与撮合的接线（MatchEngine 注入点）

``matching.MatchEngine(fee_model=...)`` 的注入函数由 :func:`make_fee_model` 生成，
签名 ``(order, fill_price, volume, bar) -> dict[FeeItem, Decimal]``。注入后撮合
规则 7 的资金校验从 ``PREFLIGHT_FEE_RATE``（万三拍数垫）切换到**逐项精确费用**
——含 ¥5 最低佣金对小单的放大效应，小资金场景校验显著变严（07 号 §F 结论 3）。

### v1 未含科目（显式登记，非遗漏）

股息红利**差别化个税**（07 号 §C 三档：>1 年免 / 1 月–1 年 10% / ≤1 月 20%）不在
FR-BT-7 范围内，v1 不建模（除权派现全额入现金）。登记为 T207 成本逐项核对期的
**已知简化项**；启用高分红策略前须先补此科目。

### 金额口径

一律 ``Decimal``（⛔ 禁 float，⛔ 禁 ``Decimal(0.001)`` 这种二进制噪声写法）。
每个科目**各自**按分（``0.01``）``ROUND_HALF_UP`` 取整后入账 —— 与券商逐项计费
对账口径一致；先合计再取整会与对账单差分。佣金的 ¥5 最低收费在取整**之后**比较。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Iterable, Sequence

from backtest.constants import FeeItem, OrderSide
from backtest.types import Bar, Order

__all__ = [
    "FeeError",
    "FeeSchedule",
    "FeeConfig",
    "BJ_CODE_PREFIXES",
    "default_fee_config",
    "compute_fees",
    "apply_slippage",
    "make_fee_model",
    "make_price_model",
    "TICK_SIZE",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")
#: 金额取整单位：分。A 股费用逐项计到分。
MONEY_QUANT = Decimal("0.01")

#: A 股价格最小变动单位（tick_size = 0.01 元，13 号 §7.46：成交价必须是合法 tick 的倍数）。
TICK_SIZE = Decimal("0.01")

#: 北交所代码前缀（07 号 §D）。``43``=新三板精选层平移、``83/87``=北交所新股、``92``=北交所存量。
BJ_CODE_PREFIXES: tuple[str, ...] = ("43", "83", "87", "92")

#: A 股开市日 —— 分段表的"起点"生效日，用于表达"某日期前的默认费率"。
MARKET_EPOCH = _date(1990, 12, 19)


class FeeError(ValueError):
    """费率查询 / 费用计算的契约违约（未覆盖的日期、非法数量、负价等）。"""


@dataclass(frozen=True)
class FeeSchedule:
    """单条带生效日的费率（``effective_from`` 当日**即**适用新率）。

    Attributes:
        effective_from: 生效日（含当日）。
        value: 费率（成交金额的比例，``Decimal``）。
    """

    effective_from: _date
    value: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.effective_from, _date):
            raise FeeError(f"effective_from 须为 date: {self.effective_from!r}")
        if not isinstance(self.value, Decimal):
            raise FeeError(f"费率须为 Decimal（⛔ 禁 float）: {self.value!r}")
        if self.value < _ZERO:
            raise FeeError(f"费率不可为负: {self.value}")


def _schedules(pairs: Iterable[tuple[str, str]]) -> tuple[FeeSchedule, ...]:
    """``[("2023-08-28", "0.0005")]`` → ``(FeeSchedule(...),)``（内部装配助手）。"""
    return tuple(
        FeeSchedule(_date.fromisoformat(day), Decimal(value)) for day, value in pairs
    )


@dataclass(frozen=True)
class FeeConfig:
    """费率配置（不可变；分段表按生效日升序或乱序皆可，查询时自行排序）。

    ⛔ 直接 ``FeeConfig()`` 得到的是**空分段表**（除佣金外查不到费率会抛
    :class:`FeeError`）—— 这是刻意的 fail-closed：默认值必须经
    :func:`default_fee_config` 显式装配，避免"忘了配分段表却静默按 0 收费"。
    """

    commission_rate: Decimal = Decimal("0.00025")       # 万 2.5 全佣，双向
    min_commission: Decimal = Decimal("5")              # ¥5/笔，不足按 5 元收
    stamp_tax_schedules: tuple[FeeSchedule, ...] = ()   # 仅卖出侧
    transfer_fee_schedules: tuple[FeeSchedule, ...] = ()
    handling_fee_schedules_cn: tuple[FeeSchedule, ...] = ()  # 沪深
    handling_fee_schedules_bj: tuple[FeeSchedule, ...] = ()  # 北交所
    management_fee_rate: Decimal = Decimal("0.00002")   # 证管费 0.002%，双向
    slippage_rate: Decimal = Decimal("0.0005")          # 5 bps/边（压测用 0.0015）
    stamp_tax_sell_only: bool = True                    # 印花税单边语义（⛔ 勿改）

    def __post_init__(self) -> None:
        for name in (
            "commission_rate",
            "min_commission",
            "management_fee_rate",
            "slippage_rate",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise FeeError(f"{name} 须为 Decimal（⛔ 禁 float）: {value!r}")
            if value < _ZERO:
                raise FeeError(f"{name} 不可为负: {value}")

    # ------------------------------------------------------------------ 查询

    def rate_at(
        self, schedules: Sequence[FeeSchedule], on_date: _date
    ) -> Decimal:
        """分段查询：返回 ``effective_from <= on_date`` 中**最晚**一条的 ``value``。

        Args:
            schedules: 分段表（顺序无关）。
            on_date: 查询日（成交日）。

        Returns:
            该日适用费率。

        Raises:
            FeeError: 分段表为空，或该日早于全部生效日（⛔ 不静默返回 0）。
        """
        if not isinstance(on_date, _date):
            raise FeeError(f"查询日须为 date: {on_date!r}")
        if not schedules:
            raise FeeError("分段费率表为空 —— 请用 default_fee_config() 装配")
        hit: FeeSchedule | None = None
        for sched in sorted(schedules, key=lambda s: s.effective_from):
            if sched.effective_from <= on_date:
                hit = sched
            else:
                break
        if hit is None:
            earliest = min(s.effective_from for s in schedules)
            raise FeeError(f"{on_date} 早于最早生效日 {earliest}，费率未覆盖")
        return hit.value

    def is_bj(self, symbol: str) -> bool:
        """是否北交所标的（代码前缀 43 / 83 / 87 / 92，或 ``bj.`` 交易所前缀）。

        兼容三种写法：``"bj.430047"`` / ``"sh.430047"``（baostock 风格前缀不可信时
        以数字码为准）/ 裸码 ``"430047"``。
        """
        if not symbol:
            raise FeeError("symbol 不可为空")
        text = str(symbol).strip().lower()
        prefix, _, rest = text.partition(".")
        if rest:
            if prefix == "bj":
                return True
            code = rest
        else:
            code = text
        return code.startswith(BJ_CODE_PREFIXES)


def default_fee_config(**overrides: object) -> FeeConfig:
    """装配 07 号手册全部默认分段的 :class:`FeeConfig`（本模块**唯一**费率登记点）。

    Args:
        **overrides: 覆盖任意 :class:`FeeConfig` 字段，例如
            ``default_fee_config(slippage_rate=Decimal("0.0015"))`` 做 15bps 压测。

    Returns:
        已装配默认分段表的配置。
    """
    defaults: dict[str, object] = {
        # 印花税：2023-08-28 起减半（1‰ → 0.5‰）
        "stamp_tax_schedules": _schedules(
            [("1990-12-19", "0.001"), ("2023-08-28", "0.0005")]
        ),
        # 过户费：2022-04-29 起下调 50%（0.02‰ → 0.01‰）
        "transfer_fee_schedules": _schedules(
            [("1990-12-19", "0.00002"), ("2022-04-29", "0.00001")]
        ),
        # 沪深经手费：2023-08-28 起降 30%（0.00487% → 0.00341%）
        "handling_fee_schedules_cn": _schedules(
            [("1990-12-19", "0.0000487"), ("2023-08-28", "0.0000341")]
        ),
        # 北交所经手费：独立路径，2023-08-28 起 0.25‰ → 0.125‰
        "handling_fee_schedules_bj": _schedules(
            [("1990-12-19", "0.00025"), ("2023-08-28", "0.000125")]
        ),
    }
    defaults.update(overrides)
    return FeeConfig(**defaults)  # type: ignore[arg-type]


def _quantize(amount: Decimal) -> Decimal:
    """按分 ``ROUND_HALF_UP`` 取整（逐项入账口径）。"""
    return amount.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _check_amount_inputs(volume: int, price: Decimal) -> Decimal:
    if not isinstance(price, Decimal):
        raise FeeError(f"价格须为 Decimal（⛔ 禁 float）: {price!r}")
    if price < _ZERO:
        raise FeeError(f"价格不可为负: {price}")
    if int(volume) <= 0:
        raise FeeError(f"成交数量须为正: {volume}")
    return price * Decimal(int(volume))


def compute_fees(
    symbol: str,
    side: OrderSide,
    volume: int,
    price: Decimal,
    trade_date: _date,
    config: FeeConfig | None = None,
    *,
    base_price: Decimal | None = None,
) -> dict[FeeItem, Decimal]:
    """算一笔成交的**全部六项**费用（FR-BT-7）。

    Args:
        symbol: 标的代码（用于沪深 / 北交所经手费分流，见 :meth:`FeeConfig.is_bj`）。
        side: 买卖方向（印花税仅 ``SELL`` 收）。
        volume: 成交股数（正整数）。
        price: **实际成交价**（若已过 :func:`apply_slippage`，滑点已含在这里）。
        trade_date: 成交日（分段费率的查询键）。
        config: 费率配置；``None`` ⇒ :func:`default_fee_config`。
        base_price: 可选的**滑点前**锚定价。仅在归因报表里需要把滑点单列时传，
            此时 ``SLIPPAGE`` = ``|price - base_price| × volume``；
            ⛔ 该值已含在成交价里，不可再叠加进现金流。

    Returns:
        六个 :class:`~backtest.constants.FeeItem` **全部齐备**的 dict（不适用的科目
        记 ``Decimal("0")`` 而非缺键）—— 下游 ``Trade.fees`` 的必填键契约与
        ``sum(fees.values())`` 的总费用口径都依赖这份完备性。

    Raises:
        FeeError: 数量非正 / 价格非 Decimal 或为负 / 该成交日无费率覆盖。
    """
    cfg = default_fee_config() if config is None else config
    if not isinstance(side, OrderSide):
        raise FeeError(f"side 须为 OrderSide: {side!r}")
    turnover = _check_amount_inputs(volume, price)

    # ① 佣金：双向，¥5/笔最低（取整后再比最低）
    commission = _quantize(turnover * cfg.commission_rate)
    if commission < cfg.min_commission:
        commission = _quantize(cfg.min_commission)

    # ② 印花税：仅卖出方
    if side is OrderSide.SELL or not cfg.stamp_tax_sell_only:
        stamp_tax = _quantize(
            turnover * cfg.rate_at(cfg.stamp_tax_schedules, trade_date)
        )
    else:
        stamp_tax = _ZERO

    # ③ 过户费：双边
    transfer_fee = _quantize(
        turnover * cfg.rate_at(cfg.transfer_fee_schedules, trade_date)
    )

    # ④ 经手费：双边，沪深 / 北交所分流
    handling_schedules = (
        cfg.handling_fee_schedules_bj
        if cfg.is_bj(symbol)
        else cfg.handling_fee_schedules_cn
    )
    handling_fee = _quantize(
        turnover * cfg.rate_at(handling_schedules, trade_date)
    )

    # ⑤ 证管费：双边（常被包进全佣，此处仍逐项透视）
    management_fee = _quantize(turnover * cfg.management_fee_rate)

    # ⑥ 滑点：默认已含在成交价里 ⇒ 记 0（见模块 docstring "不重复计费"）
    if base_price is None:
        slippage = _ZERO
    else:
        if not isinstance(base_price, Decimal):
            raise FeeError(f"base_price 须为 Decimal（⛔ 禁 float）: {base_price!r}")
        slippage = _quantize(abs(price - base_price) * Decimal(int(volume)))

    return {
        FeeItem.COMMISSION: commission,
        FeeItem.STAMP_TAX: stamp_tax,
        FeeItem.TRANSFER_FEE: transfer_fee,
        FeeItem.HANDLING_FEE: handling_fee,
        FeeItem.MANAGEMENT_FEE: management_fee,
        FeeItem.SLIPPAGE: slippage,
    }


def apply_slippage(
    price: Decimal, side: OrderSide, config: FeeConfig | None = None
) -> Decimal:
    """把撮合锚定价推成含滑点的实际成交价（FR-BT-7 / NFR-3：滑点非零）。

    ``slippage_price = price × (1 + sign × slippage_rate)``，
    ``sign`` = ``+1``（BUY，买得更贵）/ ``-1``（SELL，卖得更贱）。

    ⛔ 结果**不做**分位取整：滑点后价格是净值复算链上的中间量，提前吸到 0.01
    会在 ``× volume`` 后放大成可见的对账差；需要展示时由调用方自行取整。

    Args:
        price: 锚定价（默认口径为次一开盘 ``bar.open``，T201 §7 规则 8）。
        side: 买卖方向。
        config: 费率配置；``None`` ⇒ :func:`default_fee_config`（5 bps）。

    Returns:
        含滑点的成交价。``slippage_rate == 0`` 时**恒等**返回原价。

    Raises:
        FeeError: 价格非 Decimal 或为负 / ``side`` 非 :class:`OrderSide`。
    """
    cfg = default_fee_config() if config is None else config
    if not isinstance(price, Decimal):
        raise FeeError(f"价格须为 Decimal（⛔ 禁 float）: {price!r}")
    if price < _ZERO:
        raise FeeError(f"价格不可为负: {price}")
    if not isinstance(side, OrderSide):
        raise FeeError(f"side 须为 OrderSide: {side!r}")
    sign = _ONE if side is OrderSide.BUY else -_ONE
    return price * (_ONE + sign * cfg.slippage_rate)


def make_fee_model(
    config: FeeConfig | None = None,
) -> Callable[[Order, Decimal, int, Bar], dict[FeeItem, Decimal]]:
    """把本模块封装成 ``matching.FeeModelFn``，注入 ``MatchEngine(fee_model=...)``。

    返回闭包按**实际成交价** ``fill_price`` 与成交日 ``bar.date`` 逐项计算六科目。
    滑点**不在这里重复加**：成交价若已经 :func:`apply_slippage` 推过，滑点已含在
    价里（见模块 docstring『滑点与"不重复计费"』；推价是 T204 成交价模型的活）。

    Args:
        config: 费率配置；``None`` ⇒ :func:`default_fee_config`（07 号全默认分段）。

    Returns:
        ``(order, fill_price, volume, bar) -> {FeeItem: Decimal}`` 纯函数闭包
        （同一配置内捕获，幂等可重放）。
    """
    cfg = default_fee_config() if config is None else config

    def _fee_model(
        order: Order, fill_price: Decimal, volume: int, bar: Bar,
    ) -> dict[FeeItem, Decimal]:
        return compute_fees(
            symbol=order.symbol,
            side=order.side,
            volume=volume,
            price=fill_price,
            trade_date=bar.date,
            config=cfg,
        )

    return _fee_model


def make_price_model(
    config: FeeConfig | None = None,
    *,
    limit_pct: Decimal | None = None,
    gap_slippage_pct: Decimal | None = None,
    gap_threshold_pct: Decimal = Decimal("0.03"),
) -> Callable[[Order, Bar], Decimal]:
    """T204 成交价模型 = 次一开盘 + 滑点 + 缺口滑点 + tick 取整 + 可选涨跌停限幅（FR-BT-6 / FR-BT-7 / 13 号红线）。

    步骤：``anchor = bar.open``（次一开盘 = 默认口径）→ :func:`apply_slippage`
    按方向推价（BUY 更贵 / SELL 更贱）→ **可选缺口滑点**（当 ``|open - preclose| / preclose``
    超过 ``gap_threshold_pct`` 时，额外叠加 ``gap_slippage_pct``）→ 按 ``TICK_SIZE``（0.01 元）
    ROUND_HALF_UP 取整（13 号：成交价必须是合法 tick 的倍数）→ ``limit_pct`` 非空时限幅到
    ``[preclose×(1-pct), preclose×(1+pct)]``（同样 tick 取整；13 号：滑点后
    成交价不得越涨跌停价）。

    ⛔ 限幅是**价格层面**的最后闸，与撮合规则 2/3 的一字板拒单语义独立：
    规则 2/3 处理「封板不可成交」，本层处理「非封板时滑点不越界」。

    **缺口滑点（Gap Slippage）**：隔夜跳空 ±3% / ±5% 时，真实成交价会因流动性枯竭、
    挂单稀疏而额外偏离开盘价。``gap_slippage_pct`` 非空时，当检测到缺口超阈值（默认 3%），
    额外施加该滑点（**叠加**基础滑点 ``config.slippage_rate``）。方向：BUY 时跳空↑加剧不利
    （更贵）、跳空↓有利但也加滑点保守估计；SELL 反之。

    Args:
        config: 费率配置（基础滑点率来源）；``None`` ⇒ :func:`default_fee_config`（5bps）。
        limit_pct: 可选涨跌停幅度（如 ``Decimal("0.1")``）。``None`` ⇒ 只推价+取整，
            不做限幅（调用方/数据层未提供幅度时的安全默认）。
        gap_slippage_pct: 可选缺口滑点率（如 ``Decimal("0.0010")`` = 10bps）。``None`` ⇒
            不启用缺口滑点（默认，向后兼容）。非空时在检测到缺口超阈值时额外叠加。
        gap_threshold_pct: 缺口检测阈值（默认 3% = ``Decimal("0.03")``）。当
            ``abs(open - preclose) / preclose >= gap_threshold_pct`` 时触发缺口滑点。

    Returns:
        ``(order, bar) -> Decimal`` 纯函数闭包（``matching.PriceModelFn`` 形状）。

    Raises:
        FeeError: 参数类型非 Decimal / 幅度超界。
    """
    cfg = default_fee_config() if config is None else config
    if limit_pct is not None:
        if not isinstance(limit_pct, Decimal):
            raise FeeError(f"limit_pct 须为 Decimal（⛔ 禁 float）: {limit_pct!r}")
        if limit_pct <= _ZERO or limit_pct >= _ONE:
            raise FeeError(f"limit_pct 须在 (0, 1) 内: {limit_pct}")
    if gap_slippage_pct is not None:
        if not isinstance(gap_slippage_pct, Decimal):
            raise FeeError(f"gap_slippage_pct 须为 Decimal（⛔ 禁 float）: {gap_slippage_pct!r}")
        if gap_slippage_pct < _ZERO:
            raise FeeError(f"gap_slippage_pct 不可为负: {gap_slippage_pct}")
    if not isinstance(gap_threshold_pct, Decimal):
        raise FeeError(f"gap_threshold_pct 须为 Decimal（⛔ 禁 float）: {gap_threshold_pct!r}")
    if gap_threshold_pct <= _ZERO or gap_threshold_pct >= _ONE:
        raise FeeError(f"gap_threshold_pct 须在 (0, 1) 内: {gap_threshold_pct}")

    def _tick_round(p: Decimal) -> Decimal:
        return p.quantize(TICK_SIZE, rounding=ROUND_HALF_UP)

    def _price_model(order: Order, bar: Bar) -> Decimal:
        # 基础滑点（原有逻辑）
        slipped = apply_slippage(bar.open, order.side, cfg)

        # 缺口滑点（新增，可选）
        if gap_slippage_pct is not None and bar.preclose > _ZERO:
            gap_pct = abs(bar.open - bar.preclose) / bar.preclose
            if gap_pct >= gap_threshold_pct:
                # 检测到超阈值缺口 ⇒ 额外叠加缺口滑点
                sign = _ONE if order.side is OrderSide.BUY else -_ONE
                slipped = slipped * (_ONE + sign * gap_slippage_pct)

        price = _tick_round(slipped)
        if limit_pct is None:
            return price
        # 涨跌停限幅（13 号：滑点后成交价不越界）
        limit_up = _tick_round(bar.preclose * (_ONE + limit_pct))
        limit_down = _tick_round(bar.preclose * (_ONE - limit_pct))
        if order.side is OrderSide.BUY and price > limit_up:
            return limit_up
        if order.side is OrderSide.SELL and price < limit_down:
            return limit_down
        return price

    return _price_model
