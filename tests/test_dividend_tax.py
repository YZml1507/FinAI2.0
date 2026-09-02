#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T309 红利税模块测试 —— FIFO 配对 + 三档税率边界。"""
from datetime import date
from decimal import Decimal

import pytest

from backtest.dividend_tax import (
    TAX_BRACKETS,
    DividendEvent,
    TaxBracket,
    compute_dividend_tax,
)


class TestTaxBrackets:
    """税率档位结构测试。"""

    def test_tax_brackets_definition(self):
        """三档税率定义正确。"""
        assert len(TAX_BRACKETS) == 3
        # 按 min_holding_days 降序排列
        assert TAX_BRACKETS[0].min_holding_days == 365
        assert TAX_BRACKETS[0].tax_rate == Decimal("0.05")
        assert TAX_BRACKETS[1].min_holding_days == 30
        assert TAX_BRACKETS[1].tax_rate == Decimal("0.10")
        assert TAX_BRACKETS[2].min_holding_days == 0
        assert TAX_BRACKETS[2].tax_rate == Decimal("0.20")

    def test_tax_bracket_float_rejected(self):
        """税率为 float 拒绝。"""
        with pytest.raises(TypeError, match="tax_rate 必须是 Decimal"):
            TaxBracket(min_holding_days=30, tax_rate=0.1)  # type: ignore


class TestDividendEvent:
    """分红事件结构测试。"""

    def test_dividend_event_creation(self):
        """正常创建分红事件。"""
        div = DividendEvent(
            ex_date=date(2023, 6, 15),
            symbol="sh.600000",
            dividend_per_share=Decimal("1.5"),
            shares_held=1000,
        )
        assert div.ex_date == date(2023, 6, 15)
        assert div.shares_held == 1000

    def test_dividend_per_share_float_rejected(self):
        """每股分红为 float 拒绝。"""
        with pytest.raises(TypeError, match="dividend_per_share 必须是 Decimal"):
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=1.5,  # type: ignore
                shares_held=1000,
            )

    def test_negative_shares_rejected(self):
        """负持股数拒绝。"""
        with pytest.raises(ValueError, match="持股数不得为负"):
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=-100,
            )


class TestHoldingPeriodBoundaries:
    """分档边界测试（5 例）。"""

    def test_holding_29_days_20_percent(self):
        """持股 29 天分红 → 20% 税率。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=1000,
            )
        ]
        buys = [(date(2023, 5, 17), "sh.600000", 1000)]  # 29 天
        tax = compute_dividend_tax(divs, buys, [])
        # 1000 × 1 × 20% = 200
        assert tax == Decimal("200.00")

    def test_holding_30_days_10_percent(self):
        """持股 30 天分红 → 10% 税率（边界）。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=1000,
            )
        ]
        buys = [(date(2023, 5, 16), "sh.600000", 1000)]  # 30 天
        tax = compute_dividend_tax(divs, buys, [])
        # 1000 × 1 × 10% = 100
        assert tax == Decimal("100.00")

    def test_holding_364_days_10_percent(self):
        """持股 364 天分红 → 10% 税率。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=1000,
            )
        ]
        buys = [(date(2022, 6, 16), "sh.600000", 1000)]  # 364 天
        tax = compute_dividend_tax(divs, buys, [])
        # 1000 × 1 × 10% = 100
        assert tax == Decimal("100.00")

    def test_holding_365_days_5_percent(self):
        """持股 365 天分红 → 5% 税率（边界）。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=1000,
            )
        ]
        buys = [(date(2022, 6, 15), "sh.600000", 1000)]  # 365 天
        tax = compute_dividend_tax(divs, buys, [])
        # 1000 × 1 × 5% = 50
        assert tax == Decimal("50.00")

    def test_holding_1000_days_5_percent(self):
        """持股 1000 天分红 → 5% 税率。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=1000,
            )
        ]
        buys = [(date(2020, 9, 20), "sh.600000", 1000)]  # 1000 天
        tax = compute_dividend_tax(divs, buys, [])
        # 1000 × 1 × 5% = 50
        assert tax == Decimal("50.00")


class TestFIFOMatching:
    """FIFO 配对测试（4 例）。"""

    def test_single_buy_single_dividend(self):
        """单次买入 → 除权 → 单档税率。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=1000,
            )
        ]
        buys = [(date(2023, 5, 1), "sh.600000", 1000)]  # 45 天 → 10%
        tax = compute_dividend_tax(divs, buys, [])
        # 1000 × 1 × 10% = 100
        assert tax == Decimal("100.00")

    def test_two_buys_mixed_tax_rates(self):
        """两次买入（不同持股期）→ 除权 → 混合税率。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=1000,
            )
        ]
        buys = [
            (date(2022, 5, 11), "sh.600000", 500),  # 400 天（≥365）→ 5%（早的在前）
            (date(2023, 5, 26), "sh.600000", 500),  # 20 天 → 20%
        ]
        tax = compute_dividend_tax(divs, buys, [])
        # 500×1×5% + 500×1×20% = 25 + 100 = 125
        assert tax == Decimal("125.00")

    def test_buy_partial_sell_then_dividend(self):
        """买入 → 部分卖出 → 除权 → FIFO 扣减后剩余股份分红。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=600,  # 剩余 600 股
            )
        ]
        buys = [
            (date(2023, 1, 1), "sh.600000", 500),  # 165 天 → 10%
            (date(2023, 2, 1), "sh.600000", 500),  # 134 天 → 10%
        ]
        sells = [
            (date(2023, 5, 1), "sh.600000", 400),  # FIFO：先卖第一批 400股，第一批剩余 100股
        ]
        tax = compute_dividend_tax(divs, buys, sells)
        # 剩余：第一批 100 股（165 天 → 10%）+ 第二批 500 股（134 天 → 10%）
        # 100×1×10% + 500×1×10% = 10 + 50 = 60
        assert tax == Decimal("60.00")

    def test_multiple_buys_multiple_dividends(self):
        """多次买入 → 多次除权 → 累计税额。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 3, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("0.5"),
                shares_held=1000,
            ),
            DividendEvent(
                ex_date=date(2023, 9, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("0.8"),
                shares_held=1000,
            ),
        ]
        buys = [
            (date(2023, 1, 1), "sh.600000", 500),  # 第一次分红 73 天 → 10%，第二次分红 257 天 → 10%
            (date(2023, 2, 1), "sh.600000", 500),  # 第一次分红 42 天 → 10%，第二次分红 226 天 → 10%
        ]
        tax = compute_dividend_tax(divs, buys, [])
        # 第一次分红：1000 × 0.5 × 10% = 50
        # 第二次分红：1000 × 0.8 × 10% = 80
        # 总计：130
        assert tax == Decimal("130.00")


class TestEdgeCases:
    """边界 case（3 例）。"""

    def test_empty_position_zero_tax(self):
        """空持仓分红 → 0 税额。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=0,
            )
        ]
        buys: list[tuple] = []
        tax = compute_dividend_tax(divs, buys, [])
        assert tax == Decimal("0")

    def test_same_day_buy_and_dividend(self):
        """除权日当天买入当天分红 → 持股期 0 天（20% 税率）。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=1000,
            )
        ]
        buys = [(date(2023, 6, 15), "sh.600000", 1000)]  # 0 天
        tax = compute_dividend_tax(divs, buys, [])
        # 1000 × 1 × 20% = 200
        assert tax == Decimal("200.00")

    def test_buy_sell_all_then_dividend_zero_tax(self):
        """买入 → 全部卖出 → 除权 → 0 税额（已无持仓）。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=0,
            )
        ]
        buys = [(date(2023, 5, 1), "sh.600000", 1000)]
        sells = [(date(2023, 6, 1), "sh.600000", 1000)]  # 全部卖出
        tax = compute_dividend_tax(divs, buys, sells)
        assert tax == Decimal("0")


class TestErrorHandling:
    """错误处理测试（3 例）。"""

    def test_dividends_out_of_order(self):
        """dividends 乱序 → raise ValueError。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 9, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=1000,
            ),
            DividendEvent(
                ex_date=date(2023, 3, 15),  # 乱序
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=1000,
            ),
        ]
        buys = [(date(2023, 1, 1), "sh.600000", 1000)]
        with pytest.raises(ValueError, match="dividends 必须按 ex_date 升序排列"):
            compute_dividend_tax(divs, buys, [])

    def test_holding_mismatch(self):
        """除权日持股数为负（队列不匹配）→ raise ValueError。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=1500,  # 声明持有 1500 股
            )
        ]
        buys = [(date(2023, 5, 1), "sh.600000", 1000)]  # 实际只买了 1000 股
        with pytest.raises(ValueError, match="除权日持股数.*但 FIFO 队列持股"):
            compute_dividend_tax(divs, buys, [])

    def test_buy_trades_out_of_order(self):
        """buy_trades 乱序 → raise ValueError。"""
        divs: list[DividendEvent] = []
        buys = [
            (date(2023, 6, 1), "sh.600000", 500),
            (date(2023, 5, 1), "sh.600000", 500),  # 乱序
        ]
        with pytest.raises(ValueError, match="buy_trades 必须按 date 升序排列"):
            compute_dividend_tax(divs, buys, [])


class TestGoldenExamples:
    """黄金算例（验收标准）。"""

    def test_golden_1000_shares_20_days_1_yuan(self):
        """验收标准 2：1000 股持股 20 天分红 1 元/股 → 税额 200 元。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=1000,
            )
        ]
        buys = [(date(2023, 5, 26), "sh.600000", 1000)]  # 20 天
        tax = compute_dividend_tax(divs, buys, [])
        assert tax == Decimal("200.00")

    def test_golden_mixed_rates_500_plus_500(self):
        """验收标准 3：500 股持 400 天（≥365天，5%）+ 500 股持 20 天（20%），分红 1 元/股 → 税额 125 元。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("1"),
                shares_held=1000,
            )
        ]
        buys = [
            (date(2022, 5, 11), "sh.600000", 500),  # 400 天（≥365）→ 5%（早的在前）
            (date(2023, 5, 26), "sh.600000", 500),  # 20 天 → 20%
        ]
        tax = compute_dividend_tax(divs, buys, [])
        # 500×1×5% + 500×1×20% = 25 + 100 = 125
        assert tax == Decimal("125.00")


class TestRoundingPrecision:
    """金额取整精度测试。"""

    def test_rounding_to_cent(self):
        """逐项 ROUND_HALF_UP 到分。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("0.333"),  # 每股 0.333 元
                shares_held=100,
            )
        ]
        buys = [(date(2023, 5, 1), "sh.600000", 100)]  # 45 天 → 10%
        tax = compute_dividend_tax(divs, buys, [])
        # 100 × 0.333 × 10% = 3.33，ROUND_HALF_UP → 3.33
        assert tax == Decimal("3.33")

    def test_multiple_lots_rounding(self):
        """多批次逐项取整后汇总。"""
        divs = [
            DividendEvent(
                ex_date=date(2023, 6, 15),
                symbol="sh.600000",
                dividend_per_share=Decimal("0.777"),
                shares_held=300,
            )
        ]
        buys = [
            (date(2023, 3, 1), "sh.600000", 100),  # 106 天 → 10%（早的在前）
            (date(2023, 4, 1), "sh.600000", 100),  # 75 天 → 10%
            (date(2023, 5, 1), "sh.600000", 100),  # 45 天 → 10%
        ]
        tax = compute_dividend_tax(divs, buys, [])
        # 每批：100 × 0.777 × 10% = 7.77，ROUND_HALF_UP → 7.77
        # 总计：7.77 × 3 = 23.31
        assert tax == Decimal("23.31")
