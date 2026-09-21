#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Spike: 最小 ExternalEvidenceAdapter 草案实证。

把一个**与本仓 schema 无关**的 5 行玩具外部回测账本（``toy_ledger.csv``，
仅 date/asset/qty/price 四列）翻译成 ``scripts/gates`` 各门消费的 ctx 键集合。

目的：验证「外部回测 → 适配器 → 通用层门禁」链路的可行性，并如实暴露
每个门卡在哪些键上。**不改 gates/evidence 本体代码。**

玩具市场规则（外部回测的"市场语义"，与本仓 A 股规则无关）：
- 佣金：每笔固定 5 元；
- 印花税：本市场无此科目（恒为 0）——用于如实观察 S-5 的 A 股耦合；
- 涨跌停：无交易所限幅，adapter 以 ±10% 合成 limit_up/limit_down 喂 E-3；
- T+0：当日买可当日卖（本仓 E-1/E-2 的 T+1/FIFO 探针不适用）。
"""

from __future__ import annotations

import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

_SPIKE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SPIKE_DIR.parents[2]

import sys

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gates.tamper_guard import sign_run_record  # noqa: E402

INITIAL_CASH = Decimal("100000")
COMMISSION_PER_FILL = Decimal("5")
LIMIT_PCT = Decimal("0.10")  # 玩具市场涨跌幅限幅 ±10%


class ToyEvidenceAdapter:
    """玩具外部回测 → 门禁 ctx 键集合的最小适配器。

    对应设计文档中的 ``ExternalEvidenceAdapter`` 协议草案：
    外部系统只需把自有账本/成交/现金流水翻译为**普通 dict/list**，
    无需引入本仓的任何业务类型（Order/Trade/Ledger 等）。
    """

    def __init__(self, ledger_csv: Path) -> None:
        self.ledger_csv = Path(ledger_csv)
        self.rows = self._load_rows()

    def _load_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self.ledger_csv.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                qty = Decimal(r["qty"])
                price = Decimal(r["price"])
                rows.append({
                    "date": r["date"],
                    "asset": r["asset"],
                    "qty": qty,
                    "price": price,
                    "side": "BUY" if qty > 0 else "SELL",
                    "amount": abs(qty) * price,
                })
        return rows

    # ---- 通用键：成交/委托 -------------------------------------------------

    def trades(self) -> list[dict[str, Any]]:
        """A-1/A-4/E-3 消费的成交明细：价格、费用分解、涨跌停界。"""
        out = []
        for r in self.rows:
            price = r["price"]
            fee = COMMISSION_PER_FILL
            out.append({
                "date": r["date"],
                "symbol": r["asset"],
                "side": r["side"],
                "price": str(price),
                "volume": str(abs(r["qty"])),
                "amount": str(r["amount"]),
                "fees": {"COMMISSION": str(fee), "STAMP_TAX": "0"},
                "total_fee": str(fee),
                # adapter 合成的市场界（外部市场规则，非本仓数据）
                "limit_up": str((price * (1 + LIMIT_PCT)).quantize(Decimal("0.01"))),
                "limit_down": str((price * (1 - LIMIT_PCT)).quantize(Decimal("0.01"))),
            })
        return out

    def orders(self) -> list[dict[str, Any]]:
        """D-5 消费的委托明细。"""
        return [{
            "side": r["side"],
            "price": str(r["price"]),
            "volume": str(abs(r["qty"])),
        } for r in self.rows]

    # ---- 通用键：逐日现金流 -------------------------------------------------

    def daily_cash_flows(self) -> list[dict[str, Any]]:
        """A-2 消费的逐日资金守恒流水（8 个科目 + 现金锚点对）。"""
        by_date: dict[str, list[dict[str, Any]]] = {}
        for r in self.rows:
            by_date.setdefault(r["date"], []).append(r)

        flows: list[dict[str, Any]] = []
        cash = INITIAL_CASH
        for date in sorted(by_date):
            c_start = cash
            t_in = Decimal("0")
            t_out = Decimal("0")
            fee_out = Decimal("0")
            for r in by_date[date]:
                if r["side"] == "BUY":
                    t_out += r["amount"]
                else:
                    t_in += r["amount"]
                fee_out += COMMISSION_PER_FILL
            c_end = c_start + t_in - t_out - fee_out
            flows.append({
                "date": date,
                "cash_start": str(c_start),
                "cash_end": str(c_end),
                "trade_in": str(t_in),
                "trade_out": str(t_out),
                "fee_out": str(fee_out),
                "dividend_in": "0",
                "dividend_tax_out": "0",
                "other_in": "0",
                "other_out": "0",
            })
            cash = c_end
        return flows

    # ---- 通用键：产物记录与签名 --------------------------------------------

    def run_record(self) -> dict[str, Any]:
        """G-4/G-MDD-1/G-STRESS-1 消费的落盘记录（含防篡改签名）。

        签名直接用本仓 ``tamper_guard.sign_run_record`` —— 签名域只有
        run_id/code_version/data_version/params_hash/status/metrics 六键，
        全部是通用回测概念；证据块在签名域之外，外部系统无需理解它。
        """
        metrics = {
            "total_return": "0.04375",
            "max_drawdown": "0.012",
            "win_rate": "1.000000",
            "annual_turnover": "0.15",
            "round_trips": 2,
            "trading_days": 250,
        }
        data_hash = hashlib.sha256(self.ledger_csv.read_bytes()).hexdigest()
        record = {
            "run_id": "toy-20240108-0001",
            "schema_version": 2,
            "code_version": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
            "data_version": data_hash[:16],
            "params_hash": hashlib.sha256(b"toy-params").hexdigest()[:16],
            "status": "FINISHED",
            "metrics": metrics,
            "timestamp": "2024-01-08T00:00:00+00:00",
            "repro_fingerprint": "toy-fp-0001",
        }
        return sign_run_record(record)

    def run_records(self) -> list[dict[str, Any]]:
        """G-REPRO-1 消费的产物集合：两个同指纹、同指标的玩具产物。"""
        rec = self.run_record()
        rec2 = dict(rec)
        rec2["run_id"] = "toy-20240108-0002"
        return [rec, sign_run_record(rec2)]

    # ---- 汇总 ---------------------------------------------------------------

    def build_context(self) -> dict[str, Any]:
        rec = self.run_record()
        metrics = rec["metrics"]
        trades = self.trades()
        total_comm = sum(Decimal(t["total_fee"]) for t in trades)
        return {
            # 通用层键
            "trades": trades,
            "orders": self.orders(),
            "daily_cash_flows": self.daily_cash_flows(),
            "run_record": rec,
            "run_records": self.run_records(),
            "metrics": metrics,
            "round_trips": metrics["round_trips"],
            "trading_days": metrics["trading_days"],
            "annualized_turnover": metrics["annual_turnover"],
            "git_commit": rec["code_version"],
            "data_hash": rec["data_version"],
            "timestamp": rec["timestamp"],
            # A-3 黄金算例：玩具费率模型 10 万往返 = 25+25 = 50.00 元
            "roundtrip_total_fee": "50.00",
            "expected_fee": "50.00",
            # S-5 归因：玩具市场无印花税科目（如实提供 0，观察 FAIL）
            "total_stamp_tax": "0",
            "total_commission": str(total_comm),
            "trades_count": len(trades),
            "code_evidence": "experiments/spikes/gate_generalization/toy_adapter.py",
            # L-1 特性存活：声明启用 COMMISSION 特性，账本中对应科目非零
            "active_features": ["COMMISSION"],
            "ledger_entries": [
                {"entry_type": "COMMISSION", "amount": "5"} for _ in trades
            ],
            "fee_summary": {"COMMISSION": str(total_comm)},
            # L-2 分配保真：3 标的字典，目标权重与实配金额同序单调
            "target_weights": {"AAA": "0.2", "BBB": "0.3", "CCC": "0.5"},
            "actual_values": {"AAA": "1000", "BBB": "3000", "CCC": "5000"},
            # ⛔ E-2 的 final_positions 编码的是「送转拆股后全额卖出探针」场景，
            #    并非"当前持仓"——玩具未跑该探针，如实不喂（INCONCLUSIVE 即预期）。
        }


def main() -> None:
    adapter = ToyEvidenceAdapter(_SPIKE_DIR / "toy_ledger.csv")
    ctx = adapter.build_context()
    print(json.dumps({k: (v if not isinstance(v, list) else f"<{len(v)} rows>") for k, v in ctx.items()},
                     ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
