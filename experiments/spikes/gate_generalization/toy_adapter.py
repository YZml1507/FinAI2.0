#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ExternalEvidenceAdapter 玩具实现（产品化升级版）。

把一个**与本仓 schema 无关**的 5 行玩具外部回测账本（``toy_ledger.csv``，
仅 date/asset/qty/price 四列）翻译成 ``scripts/gates`` 各门消费的 ctx 键集合——
实现 ``scripts.gates.adapter.ExternalEvidenceAdapter`` 协议（鸭子类型，
不继承、不导入本仓业务类型）。

玩具市场规则（外部回测的"市场语义"，与本仓 A 股规则无关）——经
``market_rules()`` 显式声明为 ``MarketRules`` 参数对象：

- 佣金：每笔固定 5 元；
- 印花税：本市场无此科目 ⇒ ``required_fee_subjects`` 只含佣金
  （spike 阶段因此踩 S-5 FAIL；参数化后如实 PASS）；
- 涨跌停：无交易所限幅，adapter 以 ±10% 合成 limit_up/limit_down 喂 E-3；
- 整手：本市场无 100 股整手约束 ⇒ ``board_lot_size=1``
  （spike 阶段玩具 150 股委托被 A 股整手拦 FAIL；参数化后如实 PASS）；
- 高价股线：无 ⇒ ``high_price_limit=None``；
- T+0：当日买可当日卖 ⇒ ``t_plus_1=False``（本仓 E-1/E-2 的 T+1/FIFO 探针不适用）。
"""

from __future__ import annotations

import csv
import hashlib
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

_SPIKE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SPIKE_DIR.parents[2]

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.gates.market_rules import MarketRules  # noqa: E402
from scripts.gates.tamper_guard import sign_run_record  # noqa: E402

INITIAL_CASH = Decimal("100000")
COMMISSION_PER_FILL = Decimal("5")
LIMIT_PCT = Decimal("0.10")  # 玩具市场涨跌幅限幅 ±10%

#: 玩具市场规则声明：无整手约束、无高价线、必非零科目仅佣金、T+0、±10%。
TOY_MARKET_RULES = MarketRules(
    market_id="TOY_MKT",
    board_lot_size=1,
    high_price_limit=None,
    price_limit_pct=0.10,
    t_plus_1=False,
    required_fee_subjects=(("total_commission", "佣金"),),
)


class ToyEvidenceAdapter:
    """玩具外部回测 → 门禁 ctx 键集合的 ExternalEvidenceAdapter 实现。

    所有方法返回普通 dict/list/str——协议鸭子类型满足，无需注册/继承。
    """

    def __init__(self, ledger_csv: Path | str | None = None) -> None:
        self.ledger_csv = Path(ledger_csv) if ledger_csv else _SPIKE_DIR / "toy_ledger.csv"
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

    # ---- 市场规则 ----------------------------------------------------------

    def market_rules(self) -> MarketRules:
        return TOY_MARKET_RULES

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

    # ---- 通用键：账本/特性/分配 ---------------------------------------------

    def ledger_entries(self) -> list[dict[str, Any]]:
        """L-1：声明启用 COMMISSION 特性 ⇒ 账本中对应科目逐笔流水。"""
        return [{"entry_type": "COMMISSION", "amount": "5"} for _ in self.rows]

    def active_features(self) -> list[str]:
        return ["COMMISSION"]

    def fee_summary(self) -> dict[str, Any]:
        return {"COMMISSION": str(COMMISSION_PER_FILL * len(self.rows))}

    def allocation(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """L-2：3 标的字典，目标权重与实配金额同序单调。"""
        return (
            {"AAA": "0.2", "BBB": "0.3", "CCC": "0.5"},
            {"AAA": "1000", "BBB": "3000", "CCC": "5000"},
        )

    # ---- 通用键：产物记录与签名 --------------------------------------------

    def run_record(self) -> dict[str, Any]:
        """单条产物记录（含防篡改签名）。

        签名用本仓 ``tamper_guard.sign_run_record``——签名域只有
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

    # ---- 通用键：出处三件套 ---------------------------------------------------

    def provenance(self) -> dict[str, Any]:
        rec = self.run_record()
        return {
            "git_commit": rec["code_version"],
            "data_hash": rec["data_version"],
            "timestamp": rec["timestamp"],
        }

    # ---- 通用键：费率探针 -----------------------------------------------------

    def roundtrip_fee_probe(self) -> dict[str, Any]:
        """A-3 黄金算例：玩具费率模型 10 万往返 = 25+25 = 50.00 元（逃生门基准）。"""
        return {"roundtrip_total_fee": "50.00", "expected_fee": "50.00"}

    # ---- 行情段 ----------------------------------------------------------------

    def market_bars(self) -> dict[str, Any]:
        """玩具账本不含行情序列 ⇒ 如实返回空段（D-1~D-4 将判 INCONCLUSIVE）。"""
        return {}

    # ---- 逃生口 --------------------------------------------------------------

    def extra_context(self) -> dict[str, Any]:
        """S-5 归因证据链出处；⛔ E-2 的 final_positions 编码的是「送转拆股后
        全额卖出探针」场景，玩具未跑该探针，如实不喂（INCONCLUSIVE 即预期）。"""
        return {"code_evidence": "experiments/spikes/gate_generalization/toy_adapter.py"}


def main() -> None:
    import json

    from scripts.gates.adapter import build_external_context

    adapter = ToyEvidenceAdapter()
    ctx = build_external_context(adapter)
    print(json.dumps(
        {k: (v if not isinstance(v, (list, dict)) else f"<{type(v).__name__} len={len(v)}>")
         for k, v in ctx.items()},
        ensure_ascii=False, indent=2, default=str,
    ))


if __name__ == "__main__":
    main()
