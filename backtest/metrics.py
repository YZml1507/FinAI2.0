#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T205 绩效与风控指标 —— FR-REP-1（收益/波动/回撤/夏普/换手/费用/胜率/月度热力图）。

口径声明（**显式，改口径必须改本文档 + 测试同步红**）：

| 指标 | 口径 | 说明 |
|---|---|---|
| ``total_return`` | ``final_nav / initial_nav − 1`` | 务必是**扣费后**净值（引擎 NAV 已扣费） |
| ``cagr`` | ``(1 + total_return) ** (365.25 / 日历年天数) − 1`` | 日历年天数 = 首末自然日差，⛔ 不是交易日数 |
| ``annual_volatility`` | 日收益率序列 std(ddof=1) × √252 | 交易日年化；⛔ 日收益 < 2 个则返回 None |
| ``max_drawdown`` | ``max((running_peak − nav) / running_peak)`` | 正数（0.25 = 25%）；附 peak/trough/recovery 三日期 |
| ``sharpe_ratio`` | ``(mean(r) − rf/252) / std(r, ddof=1) × √252`` | ``risk_free_annual`` **必须显式传入**（spec 未钉值）；std=0 ⇒ None（fail-soft，不炸） |
| ``annual_turnover`` | ``(Σ买入额 + Σ卖出额) / 2 ÷ 平均NAV ÷ 年数`` | 单边年化口径（07 号 §F 公式） |
| 费用汇总 | 逐 ``FeeItem`` 求和（六键齐备，缺记 0） | 与账本 ``sum(fees.values())`` 对账口径一致 |
| ``win_rate`` | **FIFO round-trip 配对**：同 symbol 按时间序买队列配卖出，盈利对数/总对数；无完整往返 ⇒ None | 显式声明（spec 未钉口径） |
| 月度矩阵 | ``{(year, month): 月收益率}``（月首 NAV→月末 NAV） | v1 产数据矩阵，渲染归 reporting（T403） |

红线：

* ⛔ 纯函数、零 IO、零 pandas（``nav_curve`` 已是 ``dict[str, Decimal]``）；
* ⛔ 金额统计内部用 float 做幂/开方，**输出一律 ``Decimal``（6 位小数 ROUND_HALF_UP）** ——
  统计指标不是对账口径，但浮点噪声不许漂进报告；
* ``nav_curve`` 为空 / 日期键非法 ⇒ ``MetricsError``（fail-closed，不静默产 0）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping, Sequence

from backtest.constants import FeeItem, OrderSide
from backtest.types import Trade

__all__ = [
    "MetricsError",
    "PerformanceReport",
    "compute_metrics",
]

_TRADING_DAYS = 252            # A 股年化交易日数（行业惯例）
_QUANT_6 = Decimal("0.000001")  # 指标输出统一 6 位小数
_ZERO = Decimal("0")


class MetricsError(RuntimeError):
    """指标计算的契约违约（空净值曲线 / 脏日期键 / 非法配置）。"""


@dataclass(frozen=True)
class PerformanceReport:
    """一次回测的绩效快照（纯数据，⛔ 无方法）。"""

    # —— 形态 ——
    start: _date
    end: _date
    calendar_days: int            # 首末自然日差（CAGR 的年化分母）
    trading_days: int             # nav_curve 条目数（= 交易日历长度）
    # —— 收益 ——
    initial_nav: Decimal
    final_nav: Decimal
    total_return: Decimal
    cagr: Decimal
    # —— 风险 ——
    annual_volatility: Decimal | None     # 日收益 <2 ⇒ None
    max_drawdown: Decimal
    max_dd_peak: _date | None
    max_dd_trough: _date | None
    max_dd_recovery: _date | None         # trough 后首个 nav≥peak 的日；未恢复 ⇒ None
    # —— 风险调整 ——
    sharpe_ratio: Decimal | None          # std=0 ⇒ None
    calmar_ratio: Decimal | None          # CAGR / MDD（MDD=0 ⇒ None）；03 号建议项
    risk_free_annual: Decimal             # 回显口径（显式声明，spec 未钉值）
    # —— 活动与成本 ——
    annual_turnover: Decimal | None       # 平均 NAV=0 ⇒ None
    win_rate: Decimal | None              # 无完整往返 ⇒ None
    round_trips: int                      # FIFO 配对成功的往返数
    fees_total: dict[FeeItem, Decimal]    # 六键齐备
    fees_sum: Decimal
    # —— 月度热力图（数据矩阵，渲染归 reporting） ——
    monthly_returns: dict[tuple[int, int], Decimal] = field(default_factory=dict)


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------

def _q6(value: float | Decimal) -> Decimal:
    """float → Decimal（str 中转断二进制尾巴）→ 6 位 ROUND_HALF_UP。"""
    return Decimal(str(value)).quantize(_QUANT_6, rounding=ROUND_HALF_UP)


def _parse_nav_curve(nav_curve: Mapping[str, Decimal]) -> list[tuple[_date, Decimal]]:
    """:returns: 按日期升序的 ``(date, nav)`` 列表；空/脏键 ⇒ ``MetricsError``。"""
    if not nav_curve:
        raise MetricsError("nav_curve 为空 —— 没有可统计的回测区间")
    pairs: list[tuple[_date, Decimal]] = []
    for key, nav in nav_curve.items():
        try:
            d = _date.fromisoformat(str(key))
        except (TypeError, ValueError) as exc:
            raise MetricsError(f"nav_curve 键 {key!r} 不是 ISO 日期") from exc
        if not isinstance(nav, Decimal):
            raise MetricsError(f"nav_curve[{key}] 非 Decimal（⛔ 禁 float 净值）: {nav!r}")
        if nav <= 0:
            raise MetricsError(f"nav_curve[{key}] = {nav} ≤ 0（净值必须为正，爆仓请显式建模）")
        pairs.append((d, nav))
    pairs.sort(key=lambda kv: kv[0])
    return pairs


def _daily_returns(nav: list[Decimal]) -> list[float]:
    """日对数？不——日**算术**收益率序列（年化×√252 的口径基准）。"""
    out: list[float] = []
    for prev, cur in zip(nav, nav[1:]):
        out.append(float(cur / prev) - 1.0)
    return out


def _max_drawdown(series: list[tuple[_date, Decimal]]):
    """最大回撤 + peak/trough/recovery 三日期（recovery 可为 None）。"""
    peak_date, peak_nav = series[0]
    max_dd = Decimal("0")
    dd_peak = dd_trough = None
    for d, nav in series:
        if nav > peak_nav:
            peak_date, peak_nav = d, nav
        dd = (peak_nav - nav) / peak_nav
        if dd > max_dd:
            max_dd, dd_peak, dd_trough = dd, peak_date, d
    recovery = None
    if dd_trough is not None and dd_peak is not None:
        # recovery = trough 之后首个 nav ≥ 峰值（DD 峰值日的 nav）的日期
        peak_level = next(nav for d, nav in series if d == dd_peak)
        for d, nav in series:
            if d > dd_trough and nav >= peak_level:
                recovery = d
                break
    return max_dd, dd_peak, dd_trough, recovery


def _fees_total(trades: Sequence[Trade]) -> dict[FeeItem, Decimal]:
    """逐科目汇总（六键齐备，⛔ 与账本对账口径一致：各项已按分取整，直接求和）。"""
    total = {item: _ZERO for item in FeeItem}
    for trade in trades:
        for item, amount in trade.fees.items():
            total[item] = total.get(item, _ZERO) + amount
    return total


def _win_rate_fifo(trades: Sequence[Trade]) -> tuple[Decimal | None, int]:
    """FIFO round-trip 配对胜率（显式声明口径，spec 未钉）。

    同 symbol 内部按时间序排队；每个 SELL 从队首配 BUY，盈利 = SELL 总额 >
    对应 BUY 总额（含各自费用，费用摊进成本端）。部分配对按数量切分。
    """
    queues: dict[str, list[list]] = {}   # symbol → [ [remaining_volume, unit_cost_with_fee], ... ]
    wins = losses = 0
    for t in sorted(trades, key=lambda x: (x.date, x.trade_id)):
        fee_total = sum(t.fees.values(), _ZERO)
        gross = t.price * Decimal(t.volume)
        unit = (gross + fee_total) / Decimal(t.volume) if t.side is OrderSide.BUY \
            else (gross - fee_total) / Decimal(t.volume)
        q = queues.setdefault(t.symbol, [])
        if t.side is OrderSide.BUY:
            q.append([t.volume, unit])
            continue
        # SELL：FIFO 配对
        remain = t.volume
        while remain > 0 and q:
            head_v, head_cost = q[0]
            take = min(remain, head_v)
            if unit * take > head_cost * take:
                wins += 1
            else:
                losses += 1
            head_v -= take
            remain -= take
            if head_v == 0:
                q.pop(0)
    if wins + losses == 0:
        return None, 0
    rate = Decimal(wins) / Decimal(wins + losses)
    return rate.quantize(_QUANT_6, rounding=ROUND_HALF_UP), wins + losses


def _monthly_returns(series: list[tuple[_date, Decimal]]) -> dict[tuple[int, int], Decimal]:
    """月收益率 = 当月末 NAV / 上月末 NAV − 1；首月 = 月末/首月首日 NAV − 1。"""
    buckets: list[tuple[int, int, _date, Decimal, Decimal]] = []
    for d, nav in series:
        if buckets and buckets[-1][0] == d.year and buckets[-1][1] == d.month:
            buckets[-1] = (d.year, d.month, buckets[-1][2], buckets[-1][3], nav)
        else:
            buckets.append((d.year, d.month, d, nav, nav))
    out: dict[tuple[int, int], Decimal] = {}
    prev_end_nav: Decimal | None = None
    for year, month, _first_day, first_nav, last_nav in buckets:
        base = prev_end_nav if prev_end_nav is not None else first_nav
        if base > 0:
            out[(year, month)] = _q6(float(last_nav / base) - 1.0)
        prev_end_nav = last_nav
    return out


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------

def compute_metrics(
    result,
    *,
    risk_free_annual: Decimal | None = None,
) -> PerformanceReport:
    """从 ``BacktestResult`` 算绩效快照（FR-REP-1 全指标）。

    Args:
        result: ``backtest.engine.BacktestResult``（鸭子类型：需 ``nav_curve`` /
            ``trades`` / ``final_nav`` 三字段，⛔ 不 import engine 避免环）。
        risk_free_annual: 无风险年化利率（如 ``Decimal("0.02")`` = 2%）。
            ⛔ **必须显式传**（spec 未钉值，默认 None ⇒ raise）——声明 R_f 是
            报告的口径义务，不许藏默认值。

    Returns:
        :class:`PerformanceReport`。

    Raises:
        MetricsError: ``risk_free_annual`` 缺失 / nav_curve 空或脏 /
            ``final_nav`` 与净值曲线末值矛盾并超出分位噪声。
    """
    if risk_free_annual is None:
        raise MetricsError("risk_free_annual 必须显式传入（spec 未钉值，声明是义务）")
    if not isinstance(risk_free_annual, Decimal):
        raise MetricsError(f"risk_free_annual 须为 Decimal（⛔ 禁 float）: {risk_free_annual!r}")

    series = _parse_nav_curve(result.nav_curve)
    dates = [d for d, _ in series]
    navs = [n for _, n in series]
    start, end = dates[0], dates[-1]
    calendar_days = (end - start).days
    trading_days = len(navs)
    initial_nav = navs[0]
    final_nav = navs[-1]
    if result.final_nav is not None and result.final_nav != final_nav:
        raise MetricsError(
            f"final_nav={result.final_nav} 与 nav_curve 末值 {final_nav} 矛盾（对账失败）")

    # —— 收益 ——
    total_return = final_nav / initial_nav - 1
    if calendar_days <= 0:
        cagr = _ZERO                      # 单日回测无年化意义，记 0 而不炸
    else:
        cagr = _q6((float(final_nav / initial_nav)) ** (365.25 / calendar_days) - 1.0)

    # —— 风险 ——
    daily = _daily_returns(navs)
    if len(daily) >= 2:
        mean = sum(daily) / len(daily)
        var = sum((x - mean) ** 2 for x in daily) / (len(daily) - 1)
        std = var ** 0.5
        annual_volatility = _q6(std * (_TRADING_DAYS ** 0.5)) if std > 0 else None
    else:
        std = None
        annual_volatility = None
    max_dd, dd_peak, dd_trough, dd_recovery = _max_drawdown(series)

    # —— 夏普 ——
    if std and std > 0:
        rf_daily = float(risk_free_annual) / _TRADING_DAYS
        sharpe = _q6((mean - rf_daily) / std * (_TRADING_DAYS ** 0.5))
    else:
        sharpe = None

    # —— 活动 ——
    buy_amt = sum((t.price * Decimal(t.volume) for t in result.trades
                   if t.side is OrderSide.BUY), _ZERO)
    sell_amt = sum((t.price * Decimal(t.volume) for t in result.trades
                    if t.side is OrderSide.SELL), _ZERO)
    avg_nav = sum(navs, _ZERO) / Decimal(len(navs))
    years = Decimal(calendar_days) / Decimal("365.25") if calendar_days > 0 else _ZERO
    annual_turnover = (
        _q6(float(((buy_amt + sell_amt) / 2) / avg_nav / years))
        if avg_nav > 0 and years > 0 else None
    )

    # —— 胜率 & 费用 ——
    win_rate, round_trips = _win_rate_fifo(result.trades)
    fees_total = _fees_total(result.trades)
    fees_sum = sum(fees_total.values(), _ZERO)

    return PerformanceReport(
        start=start, end=end,
        calendar_days=calendar_days, trading_days=trading_days,
        initial_nav=initial_nav, final_nav=final_nav,
        total_return=total_return, cagr=cagr,
        annual_volatility=annual_volatility,
        max_drawdown=max_dd, max_dd_peak=dd_peak, max_dd_trough=dd_trough,
        max_dd_recovery=dd_recovery,
        sharpe_ratio=sharpe, risk_free_annual=risk_free_annual,
        calmar_ratio=(
            _q6(float(cagr) / float(max_dd)) if max_dd > 0 else None
        ),
        annual_turnover=annual_turnover,
        win_rate=win_rate, round_trips=round_trips,
        fees_total=fees_total, fees_sum=fees_sum,
        monthly_returns=_monthly_returns(series),
    )
