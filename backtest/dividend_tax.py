#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T309 红利税模块 —— 股息红利差别化个人所得税（三档持股期 FIFO 配对计算）。

## 政策背景

财政部/国家税务总局/证监会《关于实施上市公司股息红利差别化个人所得税政策有关问题的通知》
（财税〔2012〕85号，2013年1月1日起执行；财税〔2015〕101号延续）：

  - **持股期 > 1年**：暂免征收个人所得税（税率 5%，实际减按 25% 计入应纳税所得，等效免征）
  - **1月 ≤ 持股期 ≤ 1年**：税负 10%（减按 50% 计入应纳税所得额）
  - **持股期 < 1月**：税负 20%（全额计入应纳税所得额，按 20% 税率征收）

## 计算口径（FIFO 配对）

1. **除权日快照**：每个除权日（ex_date），当前持有的股票按买入日期 FIFO 队列追溯；
2. **持股期**：``(ex_date - buy_date).days``（日历天数，含除权日当天）；
3. **分档匹配**：持股期 ≥365天 → 5%；≥30天 → 10%；<30天 → 20%；
4. **逐档计税**：红利税 = Σ(每档持有股数 × 每股分红 × 该档税率)；
5. **卖出扣减**：卖出时 FIFO 队列先出先扣（最早买入批次先消耗）。

## 边界处理（fail-closed）

- dividends 必须按 ex_date 升序排列（assert 检查）；
- buy/sell trades 必须按 date 升序排列（assert 检查）；
- 除权日持股数不得为负（assert 检查）；
- 税率必须是 Decimal（拒绝 float）；
- 金额取整：逐项 ROUND_HALF_UP 到分，最后汇总。

## v1 简化项（显式登记）

本模块实现**三档税率计算**，但 v1 回测引擎**未主动调用**（T207 §3 已评估：
月频调仓场景红利税影响 ≈0.4%/年上限，属二阶项）。启用路径：

  ① 高分红策略（股息率 > 3% 标的占比 > 1/3）必须先接入本模块；
  ② 集成到 ledger：在卖出流水里按 FIFO 配对追溯分红历史，补 DIVIDEND_TAX 条目。

## 参考

- 07 号《A股交易规则与交易成本数据手册》§C：红利税三档税率；
- T207 门禁 G3 验收报告 §3：红利税简化项量化评估（极端上限 0.4%/年）。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "DividendEvent",
    "TaxBracket",
    "TAX_BRACKETS",
    "compute_dividend_tax",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")
#: 金额取整单位：分（与 fees.py::MONEY_QUANT 一致）
MONEY_QUANT = Decimal("0.01")


@dataclass
class DividendEvent:
    """股息事件（除权日分红）。

    Attributes:
        ex_date: 除权日（分红登记日）
        symbol: 股票代码（baostock 风格，如 "sh.600000"）
        dividend_per_share: 每股分红（元，税前）
        shares_held: 持有股数（除权日快照）
    """

    ex_date: _date
    symbol: str
    dividend_per_share: Decimal
    shares_held: int

    def __post_init__(self):
        if not isinstance(self.dividend_per_share, Decimal):
            raise TypeError(
                f"dividend_per_share 必须是 Decimal，实际 {type(self.dividend_per_share)}"
            )
        if self.shares_held < 0:
            raise ValueError(f"持股数不得为负：{self.shares_held}")


@dataclass
class TaxBracket:
    """税率档位（持股期 → 税率映射）。

    Attributes:
        min_holding_days: 最小持股天数（含）
        tax_rate: 税率（Decimal，如 Decimal("0.20") 表示 20%）
    """

    min_holding_days: int
    tax_rate: Decimal

    def __post_init__(self):
        if not isinstance(self.tax_rate, Decimal):
            raise TypeError(f"tax_rate 必须是 Decimal，实际 {type(self.tax_rate)}")


#: 三档税率（财税〔2012〕85号，2013-01-01 起执行）
#: 按 min_holding_days **降序**排列（匹配时从高到低）
TAX_BRACKETS = [
    TaxBracket(min_holding_days=365, tax_rate=Decimal("0.05")),  # ≥1年: 5%
    TaxBracket(min_holding_days=30, tax_rate=Decimal("0.10")),   # 1月-1年: 10%
    TaxBracket(min_holding_days=0, tax_rate=Decimal("0.20")),    # <1月: 20%
]


@dataclass
class _Lot:
    """FIFO 批次（内部用）。"""

    buy_date: _date
    shares: int


def compute_dividend_tax(
    dividends: list[DividendEvent],
    buy_trades: list[tuple[_date, str, int]],  # (trade_date, symbol, shares)
    sell_trades: list[tuple[_date, str, int]],  # (trade_date, symbol, shares)
    split_events: list[tuple[_date, str, Decimal]] | None = None,  # (date, symbol, factor)
) -> Decimal:
    """FIFO 配对计算红利税总额。

    Args:
        dividends: 分红事件列表（必须按 ex_date 升序排列）
        buy_trades: 买入交易列表（必须按 date 升序排列）
        sell_trades: 卖出交易列表（必须按 date 升序排列）
        split_events: 送转股拆股事件列表 [(date, symbol, factor)]（可选）

    Returns:
        红利税总额（Decimal，精确到分）
    """
    # ① 边界校验：dividends 升序
    if dividends:
        for i in range(1, len(dividends)):
            if dividends[i].ex_date < dividends[i - 1].ex_date:
                raise ValueError(
                    f"dividends 必须按 ex_date 升序排列：{dividends[i-1].ex_date} > {dividends[i].ex_date}"
                )

    # ② 边界校验：buy_trades 升序
    if buy_trades:
        for i in range(1, len(buy_trades)):
            if buy_trades[i][0] < buy_trades[i - 1][0]:
                raise ValueError(
                    f"buy_trades 必须按 date 升序排列：{buy_trades[i-1][0]} > {buy_trades[i][0]}"
                )

    # ③ 边界校验：sell_trades 升序
    if sell_trades:
        for i in range(1, len(sell_trades)):
            if sell_trades[i][0] < sell_trades[i - 1][0]:
                raise ValueError(
                    f"sell_trades 必须按 date 升序排列：{sell_trades[i-1][0]} > {sell_trades[i][0]}"
                )

    # ④ 建立 FIFO 队列（按 symbol 分组）
    holdings: dict[str, deque[_Lot]] = {}  # symbol → FIFO 批次队列

    # ⑤ 合并 buy/sell/dividend/split 事件流，按日期升序处理
    events: list[tuple[_date, str, str, tuple]] = []  # (date, event_type, symbol, payload)

    for date, symbol, shares in buy_trades:
        events.append((date, "BUY", symbol, (shares,)))

    for date, symbol, shares in sell_trades:
        events.append((date, "SELL", symbol, (shares,)))

    for div in dividends:
        events.append((div.ex_date, "DIV", div.symbol, (div.dividend_per_share, div.shares_held)))

    for date, symbol, factor in (split_events or []):
        events.append((date, "SPLIT", symbol, (factor,)))

    # 同日期执行序：BUY < DIV < SPLIT < SELL
    # 保证 DIV 按除权当日开盘持仓（pre-split）计税，随后 SPLIT 扩充批次股数供日后 SELL 抵扣
    order_priority = {"BUY": 0, "DIV": 1, "SPLIT": 2, "SELL": 3}
    events.sort(key=lambda x: (x[0], order_priority.get(x[1], 99)))

    total_tax = _ZERO

    # ⑥ 逐事件处理
    for event_date, event_type, symbol, payload in events:
        if symbol not in holdings:
            holdings[symbol] = deque()

        fifo_queue = holdings[symbol]

        if event_type == "BUY":
            shares = payload[0]
            fifo_queue.append(_Lot(buy_date=event_date, shares=shares))

        elif event_type == "SPLIT":
            factor = payload[0]
            if factor > _ZERO and factor != _ONE:
                for lot in fifo_queue:
                    lot.shares = int(Decimal(str(lot.shares)) * factor)

        elif event_type == "SELL":
            shares_to_sell = payload[0]
            remaining = shares_to_sell

            while remaining > 0 and fifo_queue:
                lot = fifo_queue[0]
                if lot.shares <= remaining:
                    # 整批卖出
                    remaining -= lot.shares
                    fifo_queue.popleft()
                else:
                    # 部分卖出
                    lot.shares -= remaining
                    remaining = 0

            if remaining > 0:
                raise ValueError(
                    f"{event_date} {symbol} 卖出 {shares_to_sell} 股，但 FIFO 队列只剩 {shares_to_sell - remaining} 股"
                )

        elif event_type == "DIV":
            dividend_per_share, shares_held = payload

            # 边界校验：除权日持股数 = FIFO 队列总和
            total_in_queue = sum(lot.shares for lot in fifo_queue)
            if total_in_queue != shares_held:
                raise ValueError(
                    f"{event_date} {symbol} 除权日持股数 {shares_held}，但 FIFO 队列持股 {total_in_queue}"
                )

            # FIFO 配对计算红利税
            for lot in fifo_queue:
                holding_days = (event_date - lot.buy_date).days

                # 匹配税率档位（从高到低）
                tax_rate = TAX_BRACKETS[-1].tax_rate  # 默认最低档（20%）
                for bracket in TAX_BRACKETS:
                    if holding_days >= bracket.min_holding_days:
                        tax_rate = bracket.tax_rate
                        break

                lot_tax_raw = lot.shares * dividend_per_share * tax_rate
                lot_tax = lot_tax_raw.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
                total_tax += lot_tax

    return total_tax
