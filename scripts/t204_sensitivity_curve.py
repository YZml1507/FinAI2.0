#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T204 敏感度对比曲线（FR-BT-6「对比曲线」验收物）—— 零依赖 SVG 直出。

读 tests/test_t204_price_model.py 的 `_run` + 三套价格口径工厂，把 3×3 九宫格
期末 NAV 画成三条走势线；产物 `docs/t204_sensitivity_curve.svg`。

⛔ 一次性文档工具脚本，不进库不修版：重跑即等价重画（数据与 TestSensitivityReportFixture
同源，改了引擎/费用口径跑这个脚本就能出新图）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

# 复用 T204 测试的脚本化策略与数据工厂（同一条数据源，⛔ 不重抄）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))               # backtest/* 包
sys.path.insert(0, str(ROOT / "tests"))     # test_t204_price_model 模块
from test_t204_price_model import (  # noqa: E402
    _close_model,
    _open_model,
    _vwap_proxy_model,
    _BuyHoldThenSell,
    _COLS,
    _DAYS,
    _SY,
    _rows,
)

from backtest.broker import BacktestBroker  # noqa: E402
from backtest.engine import BacktestEngine  # noqa: E402
from backtest.feed import ParquetDailyFeed  # noqa: E402
from backtest.fees import default_fee_config, make_fee_model  # noqa: E402
from backtest.ledger import Ledger  # noqa: E402
from backtest.matching import MatchEngine  # noqa: E402

D = Decimal

_SLIP_TIERS = ("0", "0.0005", "0.0015")
_SLIP_LABEL = {"0": "0 bps", "0.0005": "5 bps", "0.0015": "15 bps"}
_MODELS = (("open", _open_model), ("close", _close_model), ("vwap", _vwap_proxy_model))
_COLORS = {"open": "#2563eb", "close": "#dc2626", "vwap": "#16a34a"}

# NAV 曲线序列（每个交易日）：日期列表 × 三个口径净值
def _series(price_model_factory, slip: str) -> list[Decimal]:
    cfg = default_fee_config(slippage_rate=D(slip))
    frame = pd.DataFrame(_rows(), columns=_COLS)
    feed = ParquetDailyFeed(
        preloaded={_SY: frame},
        trade_calendar=lambda s, e: [d for d in _DAYS if s <= d <= e],
    )
    ledger = Ledger(D("120000"), date=_DAYS[0])
    matcher = MatchEngine(
        fee_model=make_fee_model(cfg), price_model=price_model_factory(cfg))
    broker = BacktestBroker(matcher, ledger, feed)
    result = BacktestEngine(broker, feed).run(
        _BuyHoldThenSell(), _DAYS[0], _DAYS[-1])
    return [result.nav_curve[d.isoformat()] for d in _DAYS]


def _fmt(x: Decimal) -> str:
    return f"{float(x):.2f}"


def _svg(series_map: dict[tuple[str, str], list[Decimal]]) -> str:
    W, H = 860, 420
    ml, mr, mt, mb = 70, 16, 58, 42           # margins
    plot_w, plot_h = W - ml - mr, H - mt - mb
    all_vals = [v for series in series_map.values() for v in series]
    lo = min(all_vals) * D("0.9999")
    hi = max(all_vals) * D("1.0001")
    span = hi - lo

    def xy(i: int, v: Decimal) -> tuple[float, float]:
        x = ml + plot_w * i / (len(_DAYS) - 1)
        y = mt + plot_h * float((hi - v) / span)
        return x, y

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'font-family="Consolas,monospace" font-size="12">',
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
        f'<text x="{ml}" y="26" font-size="14" font-weight="bold" fill="#0f172a">'
        'T204 敏感度对比 · 同一策略 3 价格口径 × 3 滑点档（合成 5 日上行，初始 120,000）</text>',
        f'<text x="{ml}" y="44" font-size="11" fill="#64748b">'
        '线=口径，虚实=滑点档位（实 0 / 短划 5bps / 点划 15bps）｜数据= tests/test_t204_price_model.py 实测</text>',
    ]
    # 网格 + y 轴标签
    for i in range(5):
        gy = mt + plot_h * i / 4
        val = hi - span * D(i) / D(4)
        parts.append(f'<line x1="{ml}" y1="{gy:.1f}" x2="{W - mr}" y2="{gy:.1f}" '
                     f'stroke="#e2e8f0" stroke-width="1"/>')
        parts.append(f'<text x="{ml - 6}" y="{gy + 4:.1f}" text-anchor="end" '
                     f'fill="#94a3b8" font-size="10">{_fmt(val)}</text>')
    # x 轴日期
    for i, d in enumerate(_DAYS):
        x, _ = xy(i, lo)
        parts.append(f'<text x="{x:.1f}" y="{H - mb + 16}" text-anchor="middle" '
                     f'fill="#94a3b8" font-size="10">{d.month}-{d.day}</text>')
    # 九条线
    dash = {"0": "", "0.0005": 'stroke-dasharray="6,3"',
            "0.0015": 'stroke-dasharray="2,2"'}
    for (name, slip), vals in series_map.items():
        pts = " ".join(f"{xy(i, v)[0]:.1f},{xy(i, v)[1]:.1f}"
                       for i, v in enumerate(vals))
        parts.append(
            f'<polyline points="{pts}" fill="none" stroke="{_COLORS[name]}" '
            f'stroke-width="1.8" {dash[slip]}/>')
    # 图例
    lx = ml + 8
    ly = mt + 16
    for name, _ in _MODELS:
        parts.append(
            f'<rect x="{lx}" y="{ly - 10}" width="10" height="3" fill="{_COLORS[name]}"/>'
            f'<text x="{lx + 14}" y="{ly - 5}" font-size="11" fill="#334155">{name}</text>')
        lx += 64
    for j, slip in enumerate(_SLIP_TIERS):
        parts.append(
            f'<line x1="{lx}" y1="{ly - 8}" x2="{lx + 20}" y2="{ly - 8}" '
            f'stroke="#64748b" stroke-width="1.8" {dash[slip]}/>'
            f'<text x="{lx + 24}" y="{ly - 5}" font-size="11" fill="#334155">'
            f'{_SLIP_LABEL[slip]}</text>')
        lx += 78
    # 脚注
    parts.append(
        f'<text x="{ml}" y="{H - 8}" font-size="10" fill="#94a3b8">'
        '滑点单调向下；口径间差 &lt; 0.3 元（本温和上行场景）。仅文档展示用，非交易依据。</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    series: dict[tuple[str, str], list[Decimal]] = {}
    for name, factory in _MODELS:
        for slip in _SLIP_TIERS:
            series[(name, slip)] = _series(factory, slip)
    out = ROOT / "docs" / "t204_sensitivity_curve.svg"
    out.write_text(_svg(series), encoding="utf-8")
    print(f"written: {out}")


if __name__ == "__main__":
    main()
