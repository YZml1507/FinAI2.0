#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T312 红利策略 2015-2024 全周期回测脚本。

技术规格：
  - 策略：DividendStrategy（股息率 ≥3% + 市值加权 + MA200 择时）
  - 参数：候选池 50 只 / 持仓 5 只 / 月度调仓（20 天）/ 沪深 300 MA200 择时
  - 初始资金：15 万 RMB
  - 费用模型：默认六科目（佣金万 2.5 + 印花税 + 过户费 + 经手费 + 证管费 + 滑点 5bps）
  - 价格模型：次一开盘 + 5bps 滑点（红利股波动小，不启用缺口滑点）
  - 回测区间：2015-01-05 至 2024-12-31
  - 输出：PerformanceReport + 实验 registry

命令行：
  python scripts/run_dividend_backtest.py [--data-path data/dividend_stocks]
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date as _date
from decimal import Decimal
from pathlib import Path

# 确保项目根目录在 sys.path
_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from backtest.engine import BacktestEngine
from backtest.feed import ParquetDailyFeed
from backtest.metrics import compute_metrics
from reporting.registry import ExperimentRegistry
from strategy.candidates import DividendConfig, DividendStrategy
from strategy.portfolio import PortfolioConfig
from data.universe import load_stock_basic, compute_alive_universe

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ===================================================================
# 红利策略回测主函数
# ===================================================================

def run_dividend_backtest_2015_2024(
    data_path: Path,
    initial_capital: Decimal = Decimal("150000"),
    risk_free_annual: Decimal = Decimal("0.025"),
) -> dict:
    """红利策略 2015-2024 全周期回测。

    Args:
        data_path: 红利股数据路径（data/dividend_stocks/）
        initial_capital: 初始资金（默认 15 万）
        risk_free_annual: 年化无风险利率（默认 2.5%）

    Returns:
        回测结果字典（含 PerformanceReport + 实验 registry 信息）
    """
    logger.info("=" * 60)
    logger.info("T312 红利策略 2015-2024 全周期回测")
    logger.info("=" * 60)

    # ① 策略配置
    portfolio_config = PortfolioConfig(
        min_positions=3,
        max_positions=8,
        default_positions=5,
        hard_limit=10,
        min_position_value=Decimal("20000"),
        min_daily_amount=Decimal("50000000"),  # 5000 万流动性下限
        participation_rate=Decimal("0.05"),    # 5% 参与率
    )

    strategy_config = DividendConfig(
        min_dividend_yield=Decimal("0.03"),     # 股息率 ≥3%
        candidate_pool_size=50,                 # 候选池 50 只
        default_positions=5,                    # 持仓 5 只
        use_ma200_timing=True,                  # MA200 择时
        index_symbol="sh.000300",               # 沪深 300
        rebalance_days=20,                      # 月度调仓
        warmup_bars=210,                        # 冷启动期
        portfolio=portfolio_config,
    )

    # ② 股票池（历史存活池）
    logger.info("加载历史股票池...")
    try:
        stock_basic = load_stock_basic()
        def universe_provider(day: _date) -> list[str]:
            return compute_alive_universe(stock_basic, day)
    except Exception as exc:
        logger.error(f"历史股票池加载失败: {exc}")
        logger.info("降级：使用数据目录所有 symbol（⚠ 可能含幸存者偏差）")
        universe_provider = None

    # ③ 策略实例
    strategy = DividendStrategy(
        config=strategy_config,
        universe_provider=universe_provider,
    )

    # ④ 数据源
    logger.info(f"加载红利股数据: {data_path}")
    feed = ParquetDailyFeed(root=data_path)

    # ⑤ 引擎
    engine = BacktestEngine(
        initial_capital=initial_capital,
        start_date=_date(2015, 1, 5),
        end_date=_date(2024, 12, 31),
        feed=feed,
        strategy=strategy,
        # fee_model: 默认六科目（T203）
        # price_model: 默认次一开盘 + 5bps 滑点（T204）
    )

    # ⑥ 运行回测
    logger.info("开始回测...")
    result = engine.run()

    # ⑦ 计算指标
    logger.info("计算绩效指标...")
    report = compute_metrics(result, risk_free_annual=risk_free_annual)

    # ⑧ 注册实验
    logger.info("注册实验记录...")
    registry = ExperimentRegistry(
        root=Path("experiments"),
        code_version="t312-dividend-v1",
        data_version="dividend-stocks-2015-2024",
    )
    run_id = registry.record_run(
        params=strategy_config,
        report=report,
        seed=None,  # 红利策略无随机数
        status="FINISHED",
    )

    logger.info(f"实验记录已保存: {run_id}")

    # ⑨ 打印摘要
    print("\n" + "=" * 60)
    print("红利策略 2015-2024 回测结果")
    print("=" * 60)
    print(f"初始资金:     {initial_capital:>12,.2f} 元")
    print(f"最终净值:     {report.final_nav:>12,.2f} 元")
    print(f"总收益率:     {report.total_return:>12.2%}")
    print(f"年化收益率:   {report.cagr:>12.2%}")
    print(f"年化波动率:   {report.annual_volatility or 0:>12.2%}")
    print(f"最大回撤:     {report.max_drawdown:>12.2%}")
    print(f"夏普比率:     {report.sharpe_ratio or 0:>12.2f}")
    print(f"Calmar 比率:  {report.calmar_ratio or 0:>12.2f}")
    print(f"年化换手率:   {report.annual_turnover or 0:>12.2%}")
    print(f"胜率:         {report.win_rate or 0:>12.2%}")
    print(f"往返次数:     {report.round_trips:>12,}")
    print(f"总费用:       {report.fees_sum:>12,.2f} 元")
    print("-" * 60)
    print("费用明细:")
    for item, amount in report.fees_total.items():
        print(f"  {item.value:12s}: {amount:>12,.2f} 元")
    print("=" * 60)

    return {
        "run_id": run_id,
        "report": report,
        "config": strategy_config,
    }


# ===================================================================
# CLI 入口
# ===================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="T312 红利策略回测")
    parser.add_argument(
        "--data-path",
        type=Path,
        default=Path("data/dividend_stocks"),
        help="红利股数据目录（默认 data/dividend_stocks）",
    )
    parser.add_argument(
        "--initial-capital",
        type=float,
        default=150000.0,
        help="初始资金（元，默认 15 万）",
    )
    parser.add_argument(
        "--risk-free",
        type=float,
        default=0.025,
        help="年化无风险利率（默认 2.5%）",
    )

    args = parser.parse_args()

    if not args.data_path.exists():
        logger.error(f"数据目录不存在: {args.data_path}")
        logger.info("请先运行 scripts/collect_dividend_stocks.py 采集数据")
        return 1

    try:
        result = run_dividend_backtest_2015_2024(
            data_path=args.data_path,
            initial_capital=Decimal(str(args.initial_capital)),
            risk_free_annual=Decimal(str(args.risk_free)),
        )
        logger.info(f"✅ 回测完成: {result['run_id']}")
        return 0
    except Exception as exc:
        logger.exception(f"❌ 回测失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
