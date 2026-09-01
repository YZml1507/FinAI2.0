#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T203 费用模型单测（离线，⛔ 无网络 / 无磁盘依赖）—— FR-BT-7 逐项核对表（可执行版）。

核对基线 = research-finai《07 号 A股交易规则与交易成本数据手册》§A①-⑤ / §D / §E2 / §F：

  A. 费率逐项核对（黄金数字，逐项对 07 号费率表与 10 万往返算例）
  B. 完备性与金额口径（六键齐备 / 逐项 ROUND_HALF_UP 到分 / 全 Decimal）
  C. fail-closed（空分段表 / 日期未覆盖 / 非 Decimal / 负价 / 非法数量 / 非法 side）
  D. 滑点模型（5bps 默认 / 15bps 压测 / rate=0 恒等 / 方向符号 / 不取整 / 归因单列）
  E. 撮合接线（make_fee_model 注入 MatchEngine：Trade.fees 真实化 + 规则 7 精确校验）

⛔ 永不静默原则：断言不被 try/except 吞错；任何失败让 pytest 红。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from backtest.constants import FeeItem, OrderSide, OrderType
from backtest.fees import (
    FeeConfig,
    FeeError,
    FeeSchedule,
    apply_slippage,
    compute_fees,
    default_fee_config,
    make_fee_model,
)
from backtest.matching import MatchContext, MatchEngine, MatchResult
from backtest.types import Bar, Order

D = Decimal
D0 = Decimal("0")

# —— 关键日期锚点 ——
_DAY = date(2024, 1, 4)          # 现行费率段（全调整之后）
_OLD = date(2015, 1, 5)          # 回测起点段（全部旧率）
_PRE_STAMP = date(2023, 8, 27)   # 印花税减半前一日（旧率 1‰）
_NEW_STAMP = date(2023, 8, 28)   # 印花税减半当日（含，新率 0.5‰ 生效）
_PRE_TF = date(2022, 4, 28)      # 过户费下调前一日（旧率 0.02‰）
_NEW_TF = date(2022, 4, 29)      # 过户费下调当日（含，新率 0.01‰ 生效）
_EPOCH_PRE = date(1990, 12, 18)  # 早于开市日（未覆盖 ⇒ fail-closed）

SH = "sh.600000"                 # 沪市样本
BJ = "bj.430047"                 # 北交所样本（43 前缀）


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------

def _fees(
    side: OrderSide = OrderSide.BUY,
    d: date = _DAY,
    volume: int = 10000,
    price: str = "10.00",
    symbol: str = SH,
    config: FeeConfig | None = None,
    base_price: Decimal | None = None,
) -> dict[FeeItem, Decimal]:
    return compute_fees(symbol, side, volume, D(price), d, config, base_price=base_price)


def _bar(d: date = _DAY, price: str = "10.00", symbol: str = SH, **flags: bool) -> Bar:
    p = D(price)
    return Bar(
        date=d, symbol=symbol, open=p, high=p, low=p, close=p, preclose=p,
        volume=D("1000000"), amount=D("10000000"), **flags,
    )


def _order(
    side: OrderSide = OrderSide.BUY, volume: int = 10000, symbol: str = SH,
    d: date = _DAY,
) -> Order:
    return Order(
        client_order_id=f"t203-{side.value}-{volume}-{symbol}-{d.isoformat()}",
        symbol=symbol, side=side, order_type=OrderType.MARKET,
        volume=volume, price=None, created_date=d,
    )


def _ctx(
    bar: Bar, order: Order, cash: str = "200000",
    sellable: int = 0, position_volume: int = 0,
) -> MatchContext:
    return MatchContext(
        bar=bar, order=order, sellable=sellable,
        cash_available=D(cash), position_volume=position_volume,
    )


# ======================================================================
# A. 费率逐项核对（07 号 §A①-⑤ / §D）
# ======================================================================

class TestRateTable:
    """费率逐项对表：黄金数字全部手算核对过（见各断言注释）。"""

    def test_commission_default_rate_both_sides(self) -> None:
        # 佣金万 2.5 双向：100,000 × 0.00025 = 25.00（买=卖）
        assert _fees(OrderSide.BUY)[FeeItem.COMMISSION] == D("25.00")
        assert _fees(OrderSide.SELL)[FeeItem.COMMISSION] == D("25.00")

    def test_commission_min5_floor(self) -> None:
        # ¥5/笔最低：小单抬底（07 号 §F 结论 3）
        assert _fees(volume=100)[FeeItem.COMMISSION] == D("5.00")     # 1,000 → raw 0.25
        assert _fees(volume=1600)[FeeItem.COMMISSION] == D("5.00")    # 16,000 → raw 4.00 < 5
        assert _fees(volume=2000)[FeeItem.COMMISSION] == D("5.00")    # 20,000 → raw 5.00 恰压线不垫
        assert _fees(volume=2400)[FeeItem.COMMISSION] == D("6.00")    # 24,000 → raw 6.00

    def test_stamp_tax_sell_only(self) -> None:
        # 印花税 0.5‰ 仅卖出：买方恒 0
        assert _fees(OrderSide.BUY)[FeeItem.STAMP_TAX] == D0
        assert _fees(OrderSide.SELL)[FeeItem.STAMP_TAX] == D("50.00")  # 100,000×0.0005

    def test_stamp_tax_segment_boundary(self) -> None:
        # 分段：2023-08-28 当日（含）起新率生效
        assert _fees(OrderSide.SELL, _PRE_STAMP)[FeeItem.STAMP_TAX] == D("100.00")  # 1‰
        assert _fees(OrderSide.SELL, _NEW_STAMP)[FeeItem.STAMP_TAX] == D("50.00")   # 0.5‰

    def test_transfer_fee_bilateral_and_segment(self) -> None:
        # 过户费双边：2022-04-29 起 0.02‰ → 0.01‰
        assert _fees(OrderSide.BUY, _PRE_TF)[FeeItem.TRANSFER_FEE] == D("2.00")   # ×0.00002
        assert _fees(OrderSide.BUY, _NEW_TF)[FeeItem.TRANSFER_FEE] == D("1.00")   # ×0.00001
        assert _fees(OrderSide.SELL, _NEW_TF)[FeeItem.TRANSFER_FEE] == D("1.00")  # 双边

    def test_handling_fee_cn_segment_bilateral(self) -> None:
        # 沪深经手费双边：2023-08-28 起 0.00487% → 0.00341%
        assert _fees(OrderSide.BUY, _PRE_STAMP)[FeeItem.HANDLING_FEE] == D("4.87")  # ×0.0000487
        assert _fees(OrderSide.BUY, _NEW_STAMP)[FeeItem.HANDLING_FEE] == D("3.41")  # ×0.0000341
        assert _fees(OrderSide.SELL, _DAY)[FeeItem.HANDLING_FEE] == D("3.41")       # 双边

    def test_handling_fee_bj_independent_path(self) -> None:
        # 北交所独立路径（北证公告〔2023〕54号）：0.25‰ → 0.125‰，⛔ 不可套用沪深
        assert _fees(OrderSide.SELL, _DAY, symbol=BJ)[FeeItem.HANDLING_FEE] == D("12.50")
        assert _fees(OrderSide.SELL, _PRE_STAMP, symbol=BJ)[FeeItem.HANDLING_FEE] == D("25.00")
        # 现行北交所 ≈ 沪深 3.67 倍（12.50 / 3.41）
        cn = _fees(OrderSide.SELL, _DAY, symbol=SH)[FeeItem.HANDLING_FEE]
        bj = _fees(OrderSide.SELL, _DAY, symbol=BJ)[FeeItem.HANDLING_FEE]
        assert bj > cn * D("3.6") and bj < cn * D("3.7")

    def test_management_fee_bilateral(self) -> None:
        # 证管费 0.02‰ 双边（发改价格〔2012〕2119号）
        assert _fees(OrderSide.BUY)[FeeItem.MANAGEMENT_FEE] == D("2.00")   # 100,000×0.00002
        assert _fees(OrderSide.SELL)[FeeItem.MANAGEMENT_FEE] == D("2.00")

    def test_rates_at_backtest_start_2015(self) -> None:
        # 回测自 2015-01-01 ⇒ 2015 段必须走旧率（拿现行费率回刷十年 = 系统性低估）
        buy = _fees(OrderSide.BUY, _OLD)
        assert buy[FeeItem.TRANSFER_FEE] == D("2.00")    # 旧率 0.02‰
        assert buy[FeeItem.HANDLING_FEE] == D("4.87")    # 旧率 0.00487%（沪深）
        assert _fees(OrderSide.SELL, _OLD)[FeeItem.STAMP_TAX] == D("100.00")  # 旧率 1‰
        assert _fees(OrderSide.BUY, _OLD, symbol=BJ)[FeeItem.HANDLING_FEE] == D("25.00")  # 旧率 0.25‰

    def test_golden_round_trip_100k(self) -> None:
        """07 号 §A 10 万元往返算例（逐项透视口径 vs 行业『含规费全佣』口径）。"""
        buy = _fees(OrderSide.BUY)
        sell = _fees(OrderSide.SELL)
        # 逐项透视（本模块口径：佣金万2.5 **不含**规费，规费单列）
        assert sum(buy.values(), D0) == D("31.41")    # 25 + 0 + 1 + 3.41 + 2 + 0
        assert sum(sell.values(), D0) == D("81.41")   # 25 + 50 + 1 + 3.41 + 2 + 0
        assert sum(buy.values(), D0) + sum(sell.values(), D0) == D("112.82")
        # 行业『含规费全佣』报价 = 102.00（07 号算例）；差值 = 规费双端 = 6.82 + 4.00
        bundled = D("102.00")
        regulatory = (buy[FeeItem.HANDLING_FEE] + sell[FeeItem.HANDLING_FEE]
                      + buy[FeeItem.MANAGEMENT_FEE] + sell[FeeItem.MANAGEMENT_FEE])
        assert sum(buy.values(), D0) + sum(sell.values(), D0) - bundled == D("10.82")
        assert regulatory == D("10.82")

    def test_is_bj_prefixes(self) -> None:
        cfg = default_fee_config()
        for s in ("bj.430047", "430047", "839946", "870199", "920002", "BJ.430047"):
            assert cfg.is_bj(s), f"{s} 应判北交所"
        # baostock 前缀不可信时以数字码为准：sh. 前缀 + 83 码仍判北交所
        assert cfg.is_bj("sh.830799")
        for s in ("sh.600519", "sz.300750", "600519", "300750", "sz.000001"):
            assert not cfg.is_bj(s), f"{s} 不应判北交所"
        with pytest.raises(FeeError):
            cfg.is_bj("")


# ======================================================================
# B. 完备性与金额口径
# ======================================================================

class TestCompletenessAndRounding:
    def test_all_six_keys_present_both_sides(self) -> None:
        # Trade.fees 契约：六科目齐备（不适用记 0 不缺键）
        for side in (OrderSide.BUY, OrderSide.SELL):
            assert set(_fees(side).keys()) == set(FeeItem)

    def test_every_value_quantized_to_fen(self) -> None:
        for fees in (_fees(OrderSide.BUY), _fees(OrderSide.SELL, volume=500, price="1.00")):
            for item, v in fees.items():
                assert isinstance(v, Decimal), f"{item} 非 Decimal"
                assert v == v.quantize(D("0.01")), f"{item}={v} 未取整到分"

    def test_rounding_half_up_per_item(self) -> None:
        # 逐项 ROUND_HALF_UP（先合计再取整会与对账单差分）：500 股 @1.00 = 500 元
        fees = _fees(OrderSide.SELL, volume=500, price="1.00")
        assert fees[FeeItem.TRANSFER_FEE] == D("0.01")    # raw 0.005 → 半入 0.01
        assert fees[FeeItem.HANDLING_FEE] == D("0.02")    # raw 0.01705 → 0.02
        assert fees[FeeItem.MANAGEMENT_FEE] == D("0.01")  # raw 0.01000 → 0.01
        assert fees[FeeItem.STAMP_TAX] == D("0.25")       # raw 0.25000 → 0.25
        assert fees[FeeItem.COMMISSION] == D("5.00")      # raw 0.125 → 0.13 → 抬底 5.00

    def test_override_default_config(self) -> None:
        cfg = default_fee_config(commission_rate=D("0.0001"))
        assert _fees(OrderSide.BUY, config=cfg)[FeeItem.COMMISSION] == D("10.00")


# ======================================================================
# C. fail-closed
# ======================================================================

class TestFailClosed:
    def test_bare_feeconfig_raises(self) -> None:
        # 裸 FeeConfig() 分段表为空：⛔ 不静默按 0 收费
        with pytest.raises(FeeError):
            _fees(OrderSide.BUY, config=FeeConfig())     # 过户费空表 → raise
        with pytest.raises(FeeError):
            _fees(OrderSide.SELL, config=FeeConfig())    # 印花税空表 → raise

    def test_date_before_earliest_raises(self) -> None:
        with pytest.raises(FeeError):
            _fees(OrderSide.BUY, _EPOCH_PRE)

    def test_rate_at_empty_and_uncovered(self) -> None:
        cfg = default_fee_config()
        with pytest.raises(FeeError):
            cfg.rate_at((), _DAY)                        # 空表
        with pytest.raises(FeeError):
            cfg.rate_at(cfg.stamp_tax_schedules, _EPOCH_PRE)  # 未覆盖

    def test_bad_amount_inputs(self) -> None:
        with pytest.raises(FeeError):
            _fees(volume=0)
        with pytest.raises(FeeError):
            _fees(volume=-100)
        with pytest.raises(FeeError):
            compute_fees(SH, OrderSide.BUY, 100, D("-1.00"), _DAY)   # 负价
        with pytest.raises(FeeError):
            compute_fees(SH, OrderSide.BUY, 100, 10.0, _DAY)         # float 价

    def test_bad_side_and_base_price(self) -> None:
        with pytest.raises(FeeError):
            compute_fees(SH, "BUY", 100, D("10.00"), _DAY)           # 非 OrderSide
        with pytest.raises(FeeError):
            _fees(base_price=10.0)                                   # float base_price

    def test_config_fields_reject_float_and_negative(self) -> None:
        with pytest.raises(FeeError):
            FeeConfig(commission_rate=0.00025)                       # float
        with pytest.raises(FeeError):
            FeeConfig(min_commission=D("-5"))                        # 负值
        with pytest.raises(FeeError):
            FeeSchedule(_DAY, 0.001)                                 # float 费率
        with pytest.raises(FeeError):
            FeeSchedule(_DAY, D("-0.001"))                           # 负费率
        with pytest.raises(FeeError):
            FeeSchedule("2020-01-01", D("0.001"))                    # 非 date


# ======================================================================
# D. 滑点模型（07 号 §E2：5–15bps/边，压测取保守值）
# ======================================================================

class TestSlippage:
    def test_direction_sign(self) -> None:
        # BUY 更贵 / SELL 更贱（默认 5bps = 0.0005）
        assert apply_slippage(D("10.00"), OrderSide.BUY) == D("10.005")
        assert apply_slippage(D("10.00"), OrderSide.SELL) == D("9.995")

    def test_zero_rate_identity(self) -> None:
        cfg = default_fee_config(slippage_rate=D("0"))
        assert apply_slippage(D("10.00"), OrderSide.BUY, cfg) == D("10.00")

    def test_stress_15bps(self) -> None:
        cfg = default_fee_config(slippage_rate=D("0.0015"))
        assert apply_slippage(D("10.00"), OrderSide.BUY, cfg) == D("10.015")

    def test_result_not_quantized(self) -> None:
        # 滑点后价格是净值复算链中间量：⛔ 不做分位取整（×volume 后噪声会放大成对账差）
        out = apply_slippage(D("10.00"), OrderSide.BUY)
        assert out.as_tuple().exponent < -2

    def test_apply_slippage_fail_closed(self) -> None:
        with pytest.raises(FeeError):
            apply_slippage(10.0, OrderSide.BUY)          # float
        with pytest.raises(FeeError):
            apply_slippage(D("-1"), OrderSide.BUY)       # 负价
        with pytest.raises(FeeError):
            apply_slippage(D("10.00"), "BUY")            # 非 OrderSide

    def test_slippage_column_default_zero(self) -> None:
        # 不传 base_price ⇒ 价里已含滑点 ⇒ SLIPPAGE 记 0（⛔ 不重复扣现金）
        assert _fees(OrderSide.BUY)[FeeItem.SLIPPAGE] == D0

    def test_slippage_attribution_column(self) -> None:
        # 归因报表单列：SLIPPAGE = |成交价 − 锚定价| × volume（⛔ 不可再进现金流）
        fees = _fees(OrderSide.BUY, price="10.005", base_price=D("10.00"))
        assert fees[FeeItem.SLIPPAGE] == D("50.00")      # 0.005 × 10,000


# ======================================================================
# E. 撮合接线（MatchEngine 注入点）
# ======================================================================

class TestMatchEngineWiring:
    def test_fee_model_fills_trade_with_real_fees(self) -> None:
        engine = MatchEngine(fee_model=make_fee_model())
        bar, order = _bar(), _order()
        result, trade, reason = engine.match(_ctx(bar, order))
        assert result is MatchResult.FILLED and not reason
        assert trade is not None
        # Trade.fees == 逐项精确六科目（与裸调 compute_fees 一致）
        assert trade.fees == _fees(OrderSide.BUY)
        assert trade.price == D("10.00")                 # 成交价 = 次一开盘（T204 域）
        assert sum(trade.fees.values(), D0) == D("31.41")

    def test_no_model_keeps_four_zero_items(self) -> None:
        # 既有默认行为不退化：无模型 ⇒ 四必填科目置 0
        engine = MatchEngine()
        result, trade, _ = engine.match(_ctx(_bar(), _order()))
        assert result is MatchResult.FILLED
        assert trade is not None
        assert trade.fees == {
            FeeItem.COMMISSION: D0, FeeItem.STAMP_TAX: D0,
            FeeItem.TRANSFER_FEE: D0, FeeItem.HANDLING_FEE: D0,
        }

    def test_rule7_exact_fees_replace_preflight_pad_large(self) -> None:
        # 10 万买单：万三垫需 100,030.00；精确费用需 100,031.41
        bar, order = _bar(), _order()
        cash_border = D("100030.50")
        # 无模型（万三垫）⇒ 放行
        no_model = MatchEngine()
        r0, _, _ = no_model.match(_ctx(bar, order, cash=str(cash_border)))
        assert r0 is MatchResult.FILLED
        # 有模型（逐项精确）⇒ 资金不足拒
        with_model = MatchEngine(fee_model=make_fee_model())
        r1, t1, reason = with_model.match(_ctx(bar, order, cash=str(cash_border)))
        assert r1 is MatchResult.REJECTED and t1 is None and reason == "资金不足"
        # 现金恰满精确需款 ⇒ 放行（边界 = 放行）
        r2, t2, _ = with_model.match(_ctx(bar, order, cash="100031.41"))
        assert r2 is MatchResult.FILLED and t2 is not None

    def test_rule7_min_commission_dominates_small_order(self) -> None:
        # 1 万买单：万三垫 10,003.00；精确（¥5 最低佣金主导）需 10,005.64
        bar, order = _bar(), _order(volume=1000)
        cash = "10004.00"
        assert MatchEngine().match(_ctx(bar, order, cash))[0] is MatchResult.FILLED
        r, t, reason = MatchEngine(fee_model=make_fee_model()).match(
            _ctx(bar, order, cash))
        assert r is MatchResult.REJECTED and t is None and reason == "资金不足"

    def test_sell_side_no_cash_check(self) -> None:
        # 卖出不查资金（费用从回款里扣）
        engine = MatchEngine(fee_model=make_fee_model())
        order = _order(OrderSide.SELL)
        result, trade, _ = engine.match(
            _ctx(_bar(), order, cash="0", sellable=10000, position_volume=10000))
        assert result is MatchResult.FILLED
        assert trade is not None
        assert trade.fees == _fees(OrderSide.SELL)
