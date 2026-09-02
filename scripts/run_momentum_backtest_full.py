#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全周期动量回测（2015-01-01 至 2024-12-31）—— 用户请求的完整跑数脚本。

从 Parquet 读真实数据 → 跑 MomentumStrategy（lookback=20, rebalance=5, warmup=25,
max_hold=40）→ 输出核心指标摘要（CAGR / 夏普 / MDD / 年换手 / 胜率 / 总收益）。

依赖 T105–T110 数据层 + T201–T207 回测引擎 + T301–T302 策略层已入库。
"""
from __future__ import annotations

import sys
from datetime import date as _date
from decimal import Decimal
from pathlib import Path

# 注入仓根（与 segmented_pull.py:380 同套路）
_repo_root = Path(__file__).parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from backtest.broker import BacktestBroker
from backtest.engine import BacktestEngine
from backtest.feed import ParquetDailyFeed
from backtest.fees import make_fee_model
from backtest.matching import MatchEngine
from backtest.metrics import compute_metrics
from data.universe import alive_universe, load_stock_basic
from strategy.candidates import MomentumConfig, MomentumStrategy
from strategy.portfolio import PortfolioConfig


def main() -> None:
    print("=" * 70)
    print("全周期动量回测（2015-01-01 至 2024-12-31）")
    print("=" * 70)

    # ---------------------------------------------------------------- 参数
    START = "2015-01-01"
    END = "2024-12-31"
    INITIAL_CASH = Decimal("150000")  # 15 万初始资金
    RISK_FREE_ANNUAL = Decimal("0.03")  # 3% 无风险利率

    # 策略参数（用户请求的配置）
    mom_config = MomentumConfig(
        lookback=20,
        rebalance_days=5,
        warmup_bars=25,
        max_holding_days=40,
        portfolio=PortfolioConfig(
            target_count=5,
            min_position_value=Decimal("20000"),
            min_daily_amount=Decimal("50000000"),
            max_participation_rate=Decimal("0.05"),
        ),
    )

    print(f"\n策略参数:")
    print(f"  lookback={mom_config.lookback}, rebalance={mom_config.rebalance_days}")
    print(f"  warmup={mom_config.warmup_bars}, max_hold={mom_config.max_holding_days}")
    print(f"  持仓数={mom_config.portfolio.target_count}, 单票≥{mom_config.portfolio.min_position_value}")
    print(f"\n回测区间: {START} 至 {END}")
    print(f"初始资金: {INITIAL_CASH:,} RMB")

    # ---------------------------------------------------------------- 数据源
    data_root = _repo_root / "data" / "daily_bars"
    if not data_root.exists():
        print(f"\n❌ 数据目录不存在: {data_root}")
        print("   请先运行 T109 增量更新采集数据。")
        sys.exit(1)

    print(f"\n加载数据源: {data_root}")

    # 需要交易日历注入（T201 §6：不静默打网）
    def trade_calendar_fn(start: _date, end: _date) -> list[_date]:
        """从 baostock 取交易日历（生产路径应预加载，这里演示直接取）。"""
        import baostock as bs
        try:
            bs.login()
            rs = bs.query_trade_dates(start_date=start.isoformat(), end_date=end.isoformat())
            dates = []
            while rs.error_code == '0' and rs.next():
                row = rs.get_row_data()
                if row[1] == '1':  # is_trading_day == '1'
                    dates.append(_date.fromisoformat(row[0]))
            bs.logout()
            return dates
        except Exception as e:
            print(f"⚠ 取交易日历失败: {e}，使用自然日 fallback")
            # fallback: 自然日（次优）
            from datetime import timedelta
            result = []
            current = start
            while current <= end:
                result.append(current)
                current += timedelta(days=1)
            return result

    feed = ParquetDailyFeed(root=data_root, trade_calendar=trade_calendar_fn)

    # ---------------------------------------------------------------- 股票池
    # T108 alive_universe: 避免幸存者偏差
    print("\n加载股票基本信息（baostock stock_basic）...")
    try:
        stock_basic = load_stock_basic()
        print(f"  共 {len(stock_basic)} 条股票记录")
    except Exception as e:
        print(f"❌ 加载 stock_basic 失败: {e}")
        print("   fallback: 使用 feed 已有的全部标的")
        stock_basic = None

    def universe_provider(day: _date) -> list[str]:
        """每日可交易池（防幸存者偏差）。"""
        if stock_basic is not None:
            return alive_universe(stock_basic, day)
        # fallback: 从 data 目录扫描可用标的（次优）
        return [p.name for p in data_root.iterdir() if p.is_dir() and not p.name.startswith('.')]

    # ---------------------------------------------------------------- 回测引擎
    print("\n构建回测引擎...")

    from backtest.ledger import Ledger
    from datetime import date as _date

    start_date = _date.fromisoformat(START)
    ledger = Ledger(initial_cash=INITIAL_CASH, date=start_date)
    matcher = MatchEngine(fee_model=make_fee_model())
    broker = BacktestBroker(matcher=matcher, ledger=ledger, feed=feed)

    engine = BacktestEngine(broker=broker, feed=feed)

    # ---------------------------------------------------------------- 策略
    strategy = MomentumStrategy(config=mom_config, universe_provider=universe_provider)

    # ---------------------------------------------------------------- 运行
    print("\n开始回测...")
    print("  （根据数据量，可能需要数分钟，请耐心等待）\n")

    try:
        result = engine.run(strategy, start=START, end=END)
    except Exception as e:
        print(f"\n❌ 回测失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print(f"✅ 回测完成！")
    print(f"   交易日数: {len(result.trading_dates)}")
    print(f"   订单数: {len(result.orders)}")
    print(f"   成交数: {len(result.trades)}")

    # ---------------------------------------------------------------- 指标
    print("\n计算绩效指标...")
    report = compute_metrics(result, risk_free_annual=RISK_FREE_ANNUAL)

    # ---------------------------------------------------------------- 输出
    print("\n" + "=" * 70)
    print("核心指标摘要")
    print("=" * 70)
    print(f"区间: {report.start} 至 {report.end}")
    print(f"交易日数: {report.trading_days} 天 / 日历日数: {report.calendar_days} 天")
    print(f"\n初始净值: {report.initial_nav:,.2f}")
    print(f"最终净值: {report.final_nav:,.2f}")
    print(f"总收益率: {float(report.total_return) * 100:.2f}%")
    print(f"\n年化收益率 (CAGR): {float(report.cagr) * 100:.2f}%")
    print(f"夏普比率: {float(report.sharpe_ratio) if report.sharpe_ratio else 'N/A'}")
    print(f"最大回撤: {float(report.max_drawdown) * 100:.2f}%")
    print(f"  回撤峰值日: {report.max_dd_peak}")
    print(f"  回撤谷底日: {report.max_dd_trough}")
    print(f"  回撤恢复日: {report.max_dd_recovery or '未恢复'}")
    print(f"\nCalmar 比率: {float(report.calmar_ratio) if report.calmar_ratio else 'N/A'}")
    print(f"年化波动率: {float(report.annual_volatility) * 100 if report.annual_volatility else 0:.2f}%")
    print(f"单边年化换手率: {float(report.annual_turnover) * 100 if report.annual_turnover else 0:.2f}%")
    print(f"\n往返交易数: {report.round_trips}")
    print(f"FIFO 胜率: {float(report.win_rate) * 100 if report.win_rate is not None else 0:.2f}%")

    # 费用明细
    if report.fees_sum:
        print(f"\n总费用: {float(sum(report.fees_sum.values())):.2f} 元")
        for k, v in sorted(report.fees_sum.items()):
            print(f"  {k}: {float(v):.2f} 元")

    print("\n" + "=" * 70)
    print("月度收益矩阵")
    print("=" * 70)
    if report.monthly_returns:
        # 找出所有年份和月份
        years = sorted({y for y, _ in report.monthly_returns.keys()})
        months = range(1, 13)

        # 表头
        header = "年份  " + "  ".join(f"{m:>6}" for m in months) + "   年度"
        print(header)
        print("-" * len(header))

        for year in years:
            row_values = []
            year_total = Decimal("0")
            year_months = 0
            for m in months:
                ret = report.monthly_returns.get((year, m))
                if ret is not None:
                    row_values.append(f"{float(ret)*100:>6.2f}")
                    year_total += ret
                    year_months += 1
                else:
                    row_values.append("    --")

            year_avg = float(year_total / year_months) * 100 if year_months > 0 else 0.0
            print(f"{year}  " + "  ".join(row_values) + f"  {year_avg:>6.2f}")

    print("\n✅ 全部完成！\n")


if __name__ == "__main__":
    main()
