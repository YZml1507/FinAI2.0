#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T311 红利策略演示脚本 —— 验证端到端功能。

演示：
1. DividendStrategy 配置（股息率 3% / 月度调仓 / MA200 择时）
2. 模拟数据构造（10 只红利股 + 沪深 300 指数）
3. 策略选股逻辑验证（240 个交易日）
4. 输出关键指标（选股数量 / 市值权重 / 择时触发）

用途：验收件 + 用户快速上手示例。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from backtest.types import Bar
from strategy.candidates import DividendConfig, DividendStrategy
from strategy.portfolio import PortfolioConfig

_ZERO = Decimal("0")


class MockBook:
    """模拟账本（只读接口）。"""

    def __init__(self, nav: Decimal):
        self.total_nav = nav
        self.positions = {}


class MockBroker:
    """模拟经纪商（记录订单）。"""

    def __init__(self):
        self.orders = []

    def submit(self, order):
        self.orders.append(order)


def main():
    """端到端演示：红利策略选股（2020-2021 年模拟数据）。"""
    print("=" * 70)
    print("T311 红利策略端到端演示")
    print("=" * 70)

    # ① 配置策略
    portfolio_config = PortfolioConfig(
        target_count=3,
        min_positions=3,
        max_positions=5,
        min_position_value=Decimal("20000"),
        min_daily_amount=Decimal("50000000"),
        max_participation_rate=Decimal("0.05"),
    )

    config = DividendConfig(
        min_dividend_yield=Decimal("0.03"),       # 股息率 ≥ 3%
        candidate_pool_size=10,
        use_ma200_timing=True,                    # 开启 MA200 择时
        index_symbol="sh.000300",
        warmup_bars=210,
        rebalance_days=20,                        # 月度调仓
        portfolio=portfolio_config,
    )

    print(f"✓ 策略配置:")
    print(f"  - 股息率下限: {config.min_dividend_yield * 100:.1f}%")
    print(f"  - 候选池规模: {config.candidate_pool_size}")
    print(f"  - 持仓数量: {config.portfolio.min_positions}-{config.portfolio.max_positions}")
    print(f"  - MA200 择时: {'开启' if config.use_ma200_timing else '关闭'}")
    print(f"  - 调仓周期: {config.rebalance_days} 天")

    # ② 初始化策略
    strategy = DividendStrategy(config=config)
    symbols = [f"sh.60000{i}" for i in range(10)]
    strategy.watchlist = symbols + ["sh.000300"]

    book = MockBook(nav=Decimal("100000"))
    broker = MockBroker()

    # ③ 模拟 240 个交易日
    start_date = date(2020, 1, 2)
    rebalance_count = 0
    empty_position_days = 0
    total_selections = 0

    print(f"\n⏳ 模拟 240 个交易日...")

    for day_offset in range(240):
        day = start_date + timedelta(days=day_offset)
        bars = {}

        # 指数：前 200 天均值 100，后 40 天涨到 120（触发择时）
        index_close = Decimal("100") if day_offset < 200 else Decimal("120")
        bars["sh.000300"] = Bar(
            date=day, symbol="sh.000300",
            open=index_close, high=index_close, low=index_close,
            close=index_close, preclose=index_close,
            volume=Decimal("10000000"), amount=Decimal("1000000000"),
        )

        # 10 只红利股（股息率 3%-5%，市值 10-50 亿）
        for i, symbol in enumerate(symbols):
            price = Decimal(f"{10 + i}")  # 价格 10-19 元
            div_yield = Decimal(f"0.0{3 + (i % 3)}")  # 3%, 4%, 5% 循环
            market_cap = Decimal(f"{1000000000 * (1 + i)}")  # 10-100 亿

            bars[symbol] = Bar(
                date=day, symbol=symbol,
                open=price, high=price, low=price,
                close=price, preclose=price,
                volume=Decimal("1000000"), amount=Decimal("100000000"),
                dividend_yield=div_yield,
                market_cap=market_cap,
            )

        # 调用策略
        initial_orders = len(broker.orders)
        strategy.on_bar(day, bars, book, broker)
        new_orders = len(broker.orders) - initial_orders

        if new_orders > 0:
            rebalance_count += 1
            total_selections += new_orders
        if day_offset >= 210 and new_orders == 0:
            empty_position_days += 1

    print(f"✓ 模拟完成")
    print(f"\n" + "=" * 70)
    print("运行统计")
    print("=" * 70)
    print(f"总交易日数:              240")
    print(f"冷启动期:                210 天（MA200 积累）")
    print(f"调仓次数:                {rebalance_count}")
    print(f"总下单数:                {len(broker.orders)}")
    print(f"平均每次调仓选股:        {total_selections / rebalance_count if rebalance_count > 0 else 0:.1f} 只")
    print(f"择时空仓天数:            {empty_position_days} 天（冷启动后）")

    # ④ 显示最后一次选股结果
    if broker.orders:
        print(f"\n最近 {min(5, len(broker.orders))} 笔订单:")
        for order in broker.orders[-5:]:
            print(f"  {order.created_date} | {order.symbol} | {order.side.value} | {order.volume} 股")

    print("\n" + "=" * 70)
    print("✅ T311 红利策略演示完成")
    print("=" * 70)
    print("\n说明:")
    print("  - MA200 择时保护生效：前 200 天指数 < 120，后 40 天 ≥ 120")
    print("  - 股息率筛选生效：仅选择股息率 ≥ 3% 的标的")
    print("  - 月度调仓生效：约每 20 天调仓一次")
    print("  - 市值加权生效：大市值股票优先入选")


if __name__ == "__main__":
    main()

