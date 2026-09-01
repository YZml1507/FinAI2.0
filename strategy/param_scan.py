#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T303 param robustness neighborhood scan (FR: spec sec 6.1, no-cliff check).

Goal: for each scalar config field in MomentumConfig, perturb it by ±20% (one at a
time, others held at base), run the same backtest, compare metrics. A cliff is flagged
when ANY of these fires:

| cliff signal     | criterion                                      |
|------------------|------------------------------------------------|
| CAGR flips sign  | base CAGR > 0 and perturbed CAGR < 0           |
| MDD doubles      | perturbed max_drawdown >= base*2 and > 0.05    |
| no trades        | perturbed run produces zero trades             |

PASS = no perturbed point triggers a cliff.

This module does NOT run backtests itself — it expands the grid, calls the supplied
runner, judges cliffs, and formats a report. Intended for offline experiments and
T305 review bundles. All public APIs are pure / deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from backtest.metrics import compute_metrics
from strategy.candidates import MomentumConfig

__all__ = [
    "ParamScanError",
    "ParamScanPoint",
    "ParamScanReport",
    "ParamScan",
]


class ParamScanError(RuntimeError):
    """Param-scan contract violation (base is already cliff / bad inputs)."""


@dataclass(frozen=True)
class ParamScanPoint:
    """Snapshot of one grid point."""

    param: str                     # perturbed param name, 'base' for the origin
    direction: str                 # 'base' / 'minus' / 'plus'
    value: Any                     # actual value used
    cagr: Decimal
    max_drawdown: Decimal
    sharpe: Decimal | None
    turnover: Decimal | None
    trades: int
    nav_end: Decimal
    is_cliff: bool
    cliff_reason: str | None


@dataclass(frozen=True)
class ParamScanReport:
    """Scan report with cliff verdicts (input to T305 review)."""

    base: ParamScanPoint
    points: tuple[ParamScanPoint, ...]
    pct: Decimal
    generated_at: str
    findings: tuple[str, ...]

    def summary_md(self) -> str:
        """Compact Markdown table for docs/t303_param_sensitivity.md."""
        head = (
            "| param | dir | value | CAGR | MDD | Sharpe | Turnover | trades | cliff |\n"
            "|---|---|---|---|---|---|---|---|---|"
        )
        rows = [self._row(self.base)] + [self._row(p) for p in self.points]
        verdict = (
            "PASS: no cliff detected"
            if not self.findings
            else "CLIFF: " + "; ".join(self.findings)
        )
        return "\n".join([head, *rows, "", f"**verdict**: {verdict}"])

    @staticmethod
    def _row(p: ParamScanPoint) -> str:
        sharpe = p.sharpe if p.sharpe is not None else "n/a"
        turnover = p.turnover if p.turnover is not None else "n/a"
        cliff = f"YES ({p.cliff_reason})" if p.is_cliff else "-"
        return (
            f"| {p.param} | {p.direction} | {p.value} | {p.cagr} | "
            f"{p.max_drawdown} | {sharpe} | {turnover} | {p.trades} | {cliff} |"
        )


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ParamScan:
    """Single-parameter ±20% neighborhood scanner (stateless, reusable)."""

    _SCANNABLE: tuple[str, ...] = (
        "lookback", "rebalance_days", "max_holding_days", "warmup_bars",
    )

    def __init__(self, pct: Decimal = Decimal("0.2")) -> None:
        if not isinstance(pct, Decimal) or not (Decimal("0") < pct <= Decimal("0.5")):
            raise ParamScanError(f"pct must be Decimal in (0, 0.5]: {pct!r}")
        self.pct = pct
        self._base_cagr: Decimal | None = None
        self._base_mdd: Decimal | None = None

    def neighbors(self, base: MomentumConfig) -> dict[str, MomentumConfig]:
        """Expand each scannable int field into minus/plus variants."""
        out: dict[str, MomentumConfig] = {}
        for name in self._SCANNABLE:
            base_val = getattr(base, name)
            if not isinstance(base_val, int):
                continue
            pct_val = float(self.pct) * 100
            for delta, value in (
                (f"-{pct_val:.0f}%", max(1, int(round(base_val * (1 - float(self.pct)))))),
                (f"+{pct_val:.0f}%", int(round(base_val * (1 + float(self.pct))))),
            ):
                out[f"{name}@{delta}"] = self._with(base, **{name: value})
        return out

    @staticmethod
    def _with(base: MomentumConfig, **overrides: Any) -> MomentumConfig:
        return MomentumConfig(
            lookback=overrides.get("lookback", base.lookback),
            rebalance_days=overrides.get("rebalance_days", base.rebalance_days),
            max_holding_days=overrides.get("max_holding_days", base.max_holding_days),
            warmup_bars=overrides.get("warmup_bars", base.warmup_bars),
            portfolio=base.portfolio,
        )

    def run(
        self,
        base: MomentumConfig,
        runner_fn: Callable[[MomentumConfig], Any],
    ) -> ParamScanReport:
        """Full scan: measure base + all neighbors, evaluate cliffs."""
        self._base_cagr = None
        self._base_mdd = None
        base_pt = self._measure("base", "base", "base", base, runner_fn)
        if base_pt.is_cliff:
            raise ParamScanError(
                f"base is itself a cliff ({base_pt.cliff_reason}); fix the baseline first")

        points = [
            self._measure(
                label.split("@")[0], label.split("@")[1],
                getattr(cfg, label.split("@")[0]), cfg, runner_fn,
            )
            for label, cfg in self.neighbors(base).items()
        ]
        return ParamScanReport(
            base=base_pt,
            points=tuple(points),
            pct=self.pct,
            generated_at=_now_iso(),
            findings=tuple(
                f"{p.param}@{p.direction}: {p.cliff_reason}"
                for p in points if p.is_cliff
            ),
        )

    def _measure(
        self, param: str, direction: str, value: Any,
        cfg: MomentumConfig,
        runner_fn: Callable[[MomentumConfig], Any],
    ) -> ParamScanPoint:
        result = runner_fn(cfg)
        report = compute_metrics(result, risk_free_annual=Decimal("0.02"))

        is_cliff = False
        reason: str | None = None
        if len(getattr(result, "trades", ())) == 0:
            is_cliff, reason = True, "zero trades (param kills all signals)"
        elif param != "base":
            if (
                self._base_cagr is not None and self._base_cagr > 0
                and report.cagr < 0
            ):
                is_cliff, reason = True, "CAGR flips negative"
            elif (
                self._base_mdd is not None
                and self._base_mdd > 0
                and report.max_drawdown >= self._base_mdd * 2
                and report.max_drawdown > Decimal("0.05")
            ):
                is_cliff, reason = True, "MDD doubles vs base"

        pt = ParamScanPoint(
            param=param, direction=direction, value=value,
            cagr=report.cagr, max_drawdown=report.max_drawdown,
            sharpe=report.sharpe_ratio, turnover=report.annual_turnover,
            trades=len(result.trades), nav_end=report.final_nav,
            is_cliff=is_cliff, cliff_reason=reason,
        )
        if param == "base":
            self._base_cagr = report.cagr
            self._base_mdd = report.max_drawdown
        return pt
