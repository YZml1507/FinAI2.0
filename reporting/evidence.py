#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T317 运行证据链装配（Run Evidence Assembly）—— G-Gate 26 门共堵根因修复。

根因：``registry.record_run`` 只落 metrics/params/出处，门禁审计需要的
orders / trades / journal / 逐日现金流 / 税档 / 权重保真证据从未序列化进产物，
--scheduled 模式下 14 门 RUN_EVIDENCE 一律 INCONCLUSIVE → 每日 CI 阻断。

本模块把 ``BacktestResult`` 的运行期对象转成**可审计、可复算**的 JSON 证据块，
经 ``record_run(evidence=...)`` 附加在产物签名域**之外**（签名只绑
run_id/code_version/data_version/params_hash/status/metrics，
见 ``tamper_guard.compute_run_signature`` —— evidence 可增改不改签名校验，
但任何篡改都会反映在指纹分组与指标比对门上）。

证据键与门禁消费契约（``scripts/gates/gate_*.py`` 逐门核对）：

| 证据键 | 消费门 | 语义 |
|---|---|---|
| ``orders`` | D-5 | 委托 {side, volume, price(已实现均价), order_type, created_date, status, fills} |
| ``trades`` | E-3/A-1/A-4/S-5 | 成交 {price, side, date, volume, fees{科目:str}, total_fee, limit_up/limit_down(重算板价)} |
| ``ledger_entries`` | L-1 | 流水 ``JournalEntry.to_dict()`` 全量 |
| ``daily_cash_flows`` | A-2 | 逐日现金流（成分 vs SETTLE 锚定的**真实**守恒） |
| ``roundtrip_total_fee`` | A-3 | 黄金算例：10 万元往返六科目实算 |
| ``orders_adv_ratio`` | S-3 | 单笔委托名义额 / 标的 ADV20（缺口日顺延） |
| ``target_weights`` / ``actual_values`` | L-2 | 末次非空调仓计划市值 vs 成交后实际持仓市值 |
| ``penalty_tax_amount`` / ``total_dividend_received`` / ``tax_by_bracket`` | S-4 | 红利税 <30d 惩罚档、分红总额、分档合计 |
| ``trades_count`` | S-5 | 成交笔数（费用非零归因辅助） |

⛔ 纪律：本模块**只读**运行结果，绝不回写引擎状态；所有 Decimal→str、
date→iso、枚举→value；``float`` 仅在证据值层出现（不入签名域——
``_canonicalize`` 拒 float 的纪律不适用于 evidence，但 evidence 的
float 一律来自 ``Decimal.__float__`` 的显式降精度并标注 ``_approx``）。
"""
from __future__ import annotations

from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping, Sequence

from backtest.constants import FeeItem, OrderSide
from backtest.fees import TICK_SIZE, compute_fees
from data.cleaner import _is_st, board_limit_pct

__all__ = [
    "to_json_safe",
    "serialize_orders",
    "serialize_trades",
    "serialize_journal",
    "daily_cash_flows",
    "golden_roundtrip_fee",
    "orders_adv_ratio",
    "allocation_evidence",
    "dividend_tax_evidence",
    "build_run_evidence",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")


def to_json_safe(v: Any) -> Any:
    """证据域 JSON 序列化尺：Decimal→str、date/datetime→iso、Enum→value、
    Mapping→键 str 化递归、Sequence→list、float/int/str/bool/None 原样保留。

    与 ``ledger._canonicalize`` 的分野：签名域 canonical 尺**拒绝 float**
    （防精度污染出处哈希）；证据域允许 float（审计数值，门侧用
    ``float()/Decimal()`` 按需降阶），但只接受原生 float——
    其他类型（set/bytes/自定义对象）一律 raise（⛔ 不静默降级为 repr）。
    """
    if hasattr(v, "value") and hasattr(v, "name") and not isinstance(v, type):
        # Enum（含 str-Enum：先枚举判定，否则落进 isinstance(str) 原样返回）
        import enum
        if isinstance(v, enum.Enum):
            return v.value
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, Mapping):
        return {str(k): to_json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [to_json_safe(x) for x in v]
    if hasattr(v, "isoformat"):          # date / datetime
        return v.isoformat()
    raise TypeError(f"evidence 不支持类型 {type(v).__name__}（⛔ 不静默降级）")


def _tick_round(p: Decimal) -> Decimal:
    return p.quantize(TICK_SIZE, rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------ 帧索引

def _build_frame_index(frames: Mapping[str, Any] | None) -> dict[str, dict[str, tuple]]:
    """``tables{symbol: DataFrame}`` → ``{sym: {date_iso: (preclose, open, amount, close, is_st)}}``。

    供板价重算 / ADV / 持仓估值共用。``None`` ⇒ 空索引（调用侧自行降级处理）。
    """
    idx: dict[str, dict[str, tuple]] = {}
    if not frames:
        return idx
    for sym, df in frames.items():
        cols = df.columns
        need = ("date", "preclose", "open", "amount", "close", "isST")
        if any(c not in cols for c in need):
            continue
        per: dict[str, tuple] = {}
        for d, pc, op, amt, cl, st in zip(
                df["date"], df["preclose"], df["open"],
                df["amount"], df["close"], df["isST"]):
            try:
                is_st = _is_st(st)
            except Exception:
                is_st = False            # isST 非法值 ⇒ 保守按非 ST（板幅更宽、检查更松 ⛔ 仅限证据富化，不放宽撮合）
            per[str(d)[:10]] = (
                Decimal(str(pc)) if pc is not None else _ZERO,
                Decimal(str(op)) if op is not None else _ZERO,
                Decimal(str(amt)) if amt is not None else _ZERO,
                Decimal(str(cl)) if cl is not None else _ZERO,
                is_st,
            )
        idx[sym] = per
    return idx


def _limit_prices(symbol: str, preclose: Decimal, is_st: bool) -> tuple[Decimal, Decimal]:
    """按板块规则重算涨跌停**价**（与 ``fees.make_price_model(limit_pct=...)``
    同一公式：``tick_round(preclose × (1 ± pct))``；ST 档 5%）。"""
    try:
        pct = Decimal("5.0") if is_st else Decimal(str(board_limit_pct(symbol)))
    except Exception:
        return _ZERO, _ZERO
    if preclose <= _ZERO:
        return _ZERO, _ZERO
    up = _tick_round(preclose * (_ONE + pct / _HUNDRED))
    down = _tick_round(preclose * (_ONE - pct / _HUNDRED))
    return up, down


# ------------------------------------------------------------------ 序列化

def serialize_orders(orders: Sequence[Any]) -> list[dict[str, Any]]:
    """``Order`` → 门禁可读 dict。

    ``price`` 口径声明：MARKET 委托无申报价，取**已实现均价**（``avg_fill_price``，
    未成交 ⇒ 缺键，D-5 价检自然跳过但数量检仍生效）；LIMIT 委托用申报价。
    """
    out: list[dict[str, Any]] = []
    for o in orders:
        d: dict[str, Any] = {
            "client_order_id": getattr(o, "client_order_id", ""),
            "symbol": getattr(o, "symbol", ""),
            "side": getattr(getattr(o, "side", None), "value", None)
            or str(getattr(o, "side", "")),
            "order_type": getattr(getattr(o, "order_type", None), "value", None)
            or str(getattr(o, "order_type", "")),
            "volume": int(getattr(o, "volume", 0)),
            "status": getattr(getattr(o, "status", None), "value", None)
            or str(getattr(o, "status", "")),
            "created_date": str(getattr(o, "created_date", ""))[:10],
            "filled_volume": int(getattr(o, "filled_volume", 0)),
        }
        declared = getattr(o, "price", None)
        filled = getattr(o, "avg_fill_price", None)
        if isinstance(declared, Decimal) and declared > _ZERO:
            d["price"] = str(declared)
        elif isinstance(filled, Decimal) and filled > _ZERO:
            d["price"] = str(filled)
        if isinstance(filled, Decimal):
            d["avg_fill_price"] = str(filled)
        out.append(d)
    return out


def serialize_trades(
    trades: Sequence[Any],
    frames: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """``Trade`` → 门禁可读 dict；``limit_up``/``limit_down`` 按成交日 preclose
    + 板块档重算为**价格**（E-3 契约：板价为元，非 flag）。缺帧 ⇒ 板价 "0"
    （门侧按不可判定跳过，⛔ 不伪造）。``fees`` 六科目齐备（含 ``total_fee`` 合计键）。
    """
    idx = _build_frame_index(frames)
    out: list[dict[str, Any]] = []
    for t in trades:
        sym = getattr(t, "symbol", "")
        d_iso = str(getattr(t, "date", ""))[:10]
        bar = idx.get(sym, {}).get(d_iso)
        if bar is not None:
            up, down = _limit_prices(sym, bar[0], bar[4])
        else:
            up, down = _ZERO, _ZERO
        fees_raw = getattr(t, "fees", {}) or {}
        fees = {k.value if hasattr(k, "value") else str(k): str(v)
                for k, v in fees_raw.items()}
        total_fee = sum(fees_raw.values(), _ZERO) if fees_raw else _ZERO
        out.append({
            "trade_id": getattr(t, "trade_id", ""),
            "client_order_id": getattr(t, "client_order_id", ""),
            "symbol": sym,
            "side": getattr(getattr(t, "side", None), "value", None)
            or str(getattr(t, "side", "")),
            "date": d_iso,
            "volume": int(getattr(t, "volume", 0)),
            "price": str(getattr(t, "price", _ZERO)),
            "amount": str(Decimal(str(getattr(t, "price", _ZERO)))
                          * int(getattr(t, "volume", 0))),
            "fees": fees,
            "total_fee": str(total_fee),
            "limit_up": str(up),
            "limit_down": str(down),
            "sellable_date": str(getattr(t, "sellable_date", ""))[:10],
        })
    return out


def serialize_journal(entries: Sequence[Any]) -> list[dict[str, Any]]:
    """``JournalEntry`` → ``to_dict()``（tx_hash/日期/类型/金额/分科目费用/meta 全量）。"""
    out: list[dict[str, Any]] = []
    for e in entries:
        to_dict = getattr(e, "to_dict", None)
        out.append(to_dict() if callable(to_dict) else dict(e))
    return out


# ------------------------------------------------------------------ 现金流

def daily_cash_flows(entries: Sequence[Any]) -> list[dict[str, Any]]:
    """逐日现金流表（A-2 真实守恒口径）。

    与 ``runner._build_daily_cash_flows`` 的关键差异：这里 ``cash_end`` 取
    **SETTLE 锚定值**（meta.cash = 日终账本现金实测），非公式自证——
    成分与锚定值不符即为真·账本泄漏，不是恒等式。

    成分口径（与 entry.amount 现金流语义逐项核对）：
      * TRADE:    trade_out/trade_in = 毛额（vol×price，不含费）；
                    fee_out = fees 各项合计（amount 已含费，毛费拆分不重复计）
      * DIVIDEND_TAX: dividend_tax_out = |amount|（fees 字段是其明细，⛔ 不入 fee_out，否则双倍计）
      * EXDIV_ADJUST / DIVIDEND: dividend_in = amount（派现现金；负值除权调整罕见，按 sign 分流）
      * CASH_IN / CASH_INTEREST: other_in = amount（期初本金、计息）
      * FEE 及其他类型: 按 amount 符号分流 other_in/other_out
      * SETTLE: 跳过成分，仅取锚定

    Returns:
        ``(flows, unanchored_days)``——``flows`` 为逐日 dict（含 ``anchored``
        真假标记）；``unanchored_days`` 为缺 SETTLE 锚定的日期清单
        （正常回测每日必有 SETTLE，非空 = 异常信号）。
    """
    by_day: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for e in entries:
        d = e.to_dict() if hasattr(e, "to_dict") else dict(e)
        day = str(d.get("date", ""))[:10]
        if day not in by_day:
            by_day[day] = {
                "trade_in": _ZERO, "trade_out": _ZERO, "fee_out": _ZERO,
                "dividend_in": _ZERO, "dividend_tax_out": _ZERO,
                "other_in": _ZERO, "other_out": _ZERO,
                "anchor_cash": None,
            }
            order.append(day)
        g = by_day[day]
        et = str(d.get("entry_type", ""))
        try:
            amt = Decimal(str(d.get("amount", "0")))
        except Exception:
            amt = _ZERO
        try:
            vol = int(d.get("volume") or 0)
        except Exception:
            vol = 0
        try:
            price = Decimal(str(d.get("price", "0")))
        except Exception:
            price = _ZERO
        fees = d.get("fees") or {}
        meta = d.get("meta") or {}

        if et == "TRADE":
            gross = price * Decimal(vol)
            if str(d.get("side", "")) == "BUY":
                g["trade_out"] += gross
            else:
                g["trade_in"] += gross
            for fv in fees.values():
                try:
                    g["fee_out"] += Decimal(str(fv))
                except Exception:
                    pass
        elif et == "DIVIDEND_TAX":
            # ⛔ 该类型的 fees{DIVIDEND_TAX} 是 amount 的明细，成分只计一次。
            g["dividend_tax_out"] += abs(amt)
        elif et in ("DIVIDEND", "EXDIV_ADJUST"):
            if amt >= _ZERO:
                g["dividend_in"] += amt
            else:
                g["other_out"] += abs(amt)
        elif et == "SETTLE":
            try:
                g["anchor_cash"] = Decimal(str(meta.get("cash", "0")))
            except Exception:
                pass
        else:
            if amt >= _ZERO:
                g["other_in"] += amt
            else:
                g["other_out"] += abs(amt)

    flows: list[dict[str, Any]] = []
    running = _ZERO
    unanchored: list[str] = []
    for day in sorted(order):
        g = by_day[day]
        c_start = running
        if g["anchor_cash"] is not None:
            c_end = g["anchor_cash"]
        else:
            unanchored.append(day)
            c_end = (c_start + g["trade_in"] - g["trade_out"] - g["fee_out"]
                     + g["dividend_in"] - g["dividend_tax_out"]
                     + g["other_in"] - g["other_out"])
        flows.append({
            "date": day,
            "cash_start": str(c_start),
            "cash_end": str(c_end),
            "trade_in": str(g["trade_in"]),
            "trade_out": str(g["trade_out"]),
            "fee_out": str(g["fee_out"]),
            "dividend_in": str(g["dividend_in"]),
            "dividend_tax_out": str(g["dividend_tax_out"]),
            "other_in": str(g["other_in"]),
            "other_out": str(g["other_out"]),
            "anchored": g["anchor_cash"] is not None,
        })
        running = c_end
    return flows, unanchored


# ------------------------------------------------------------------ A-3 / S-3 / L-2 / S-4

def golden_roundtrip_fee(
    *,
    symbol: str = "sh.600000",
    volume: int = 10_000,
    price: Decimal = Decimal("10.00"),
    trade_date: _date = _date(2024, 1, 2),
) -> Decimal:
    """A-3 黄金算例：10 万元往返（BUY+SELL 各 10 万）六科目总费实算。

    工程基线 ``112.82``（逐项含规费口径）/ 行业含规费全佣 ``102.00``
    （``gate_a_accounting`` 容差 0.05）。默认交易日 2024-01-02（印花税减半后）。
    """
    buy = compute_fees(symbol, OrderSide.BUY, volume, price, trade_date)
    sell = compute_fees(symbol, OrderSide.SELL, volume, price, trade_date)
    return sum(buy.values(), _ZERO) + sum(sell.values(), _ZERO)


def orders_adv_ratio(
    orders: Sequence[Any],
    frames: Mapping[str, Any] | None,
    *,
    lookback: int = 20,
) -> dict[str, Any]:
    """单笔委托名义额 / 标的当日可观测成交额（S-3 ADV 契约）。

    分子口径：委托 ``volume × 价格``（已实现均价优先，无成交则取该标的
    ``created_date`` 之后首个 bar 开盘价——委托最早可成交口径）。
    分母口径：该标的截至参考日（含）的**最近 lookback 个 bar 成交额均值**
    （ADV20；停牌缺席日天然不计，无 20 日历史时按可得日均）。

    返回 ``{"ratios": [...], "skipped": n}``——``ratios`` 供门侧逐笔阈值判定。
    """
    if not frames:
        return {"ratios": [], "skipped": len(list(orders))}
    # 每标的升序 (date, open, amount)
    series: dict[str, list[tuple[str, Decimal, Decimal]]] = {}
    for sym, df in frames.items():
        if df is None or not {"date", "open", "amount"} <= set(df.columns):
            continue
        rows = sorted(
            (str(d)[:10],
             Decimal(str(op)) if op is not None else _ZERO,
             Decimal(str(a)) if a is not None else _ZERO)
            for d, op, a in zip(df["date"], df["open"], df["amount"]))
        series[sym] = rows

    ratios: list[float] = []
    skipped = 0
    for o in orders:
        sym = getattr(o, "symbol", "")
        created = str(getattr(o, "created_date", ""))[:10]
        vol = int(getattr(o, "volume", 0))
        rows = series.get(sym)
        if not rows or vol <= 0:
            skipped += 1
            continue
        # 参考日 = created_date 之后该标的的首个 bar（T+1 撮合口径）
        ref_i = None
        for i, (d, _op, _a) in enumerate(rows):
            if d > created:
                ref_i = i
                break
        if ref_i is None:
            skipped += 1
            continue
        price = getattr(o, "avg_fill_price", None)
        if not (isinstance(price, Decimal) and price > _ZERO):
            price = rows[ref_i][1]              # 开盘价作预期成交口径
        if price <= _ZERO:
            skipped += 1
            continue
        notional = price * Decimal(vol)
        win = rows[max(0, ref_i - lookback + 1): ref_i + 1]
        adv = sum((r[2] for r in win), _ZERO) / Decimal(len(win))
        if adv <= _ZERO:
            skipped += 1
            continue
        ratios.append(float(notional / adv))
    return {"ratios": ratios, "skipped": skipped}


def allocation_evidence(
    result: Any,
    frames: Mapping[str, Any] | None,
    rebalances: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """L-2 权重保真证据：末次**有成交落地**的非空调仓。

    ``target_weights`` = 该次调仓的计划市值表（元，``plan_positions`` 直出）；
    ``actual_values`` = 该次委托成交后**持仓市值**（累计净股数 × 成交日收盘，
    重建自 ``result.trades``——覆盖"原计划 A 加仓 2k 而非 20k"的偏增量口径）。

    从 ``_evidence_rebalances`` 列表倒序取首个产生 BUY 成交的调仓（清仓/
    全拒单样本不参与保真度判定——那些是择时事件而非分配证据）。
    """
    empty = {"rebalance_date": None, "target_weights": {}, "actual_values": {}}
    if not rebalances:
        return empty
    trading_dates = sorted(getattr(result, "trading_dates", []) or [])
    trades = sorted(getattr(result, "trades", []) or [],
                    key=lambda t: t.date)
    idx = _build_frame_index(frames)

    def _close_asof(sym: str, day_iso: str) -> Decimal | None:
        per = idx.get(sym)
        if not per:
            return None
        # 取 ≤day 的最近收盘（停牌日顺延前值——持仓估值口径）
        cands = [d for d in per if d <= day_iso]
        if not cands:
            return None
        return per[max(cands)][3]

    # 两趟选择：先找「目标持仓数 ≥3」的末次有成交调仓（L-2 秩相关需 ≥3 共同
    # 标的），找不到再退「任意有成交调仓」（如实降级——证据形状不迁就门禁）。
    recs = list(rebalances)
    for min_targets in (3, 1):
        out = _pick_rebalance(recs, min_targets, trading_dates, trades,
                              result, idx, _close_asof)
        if out is not None:
            return out
    return empty


def _pick_rebalance(recs, min_targets, trading_dates, trades, result, idx,
                    _close_asof):
    for rec in reversed(recs):
        reb_iso = str(rec.get("date", ""))[:10]
        tw = {str(k): str(v) for k, v in (rec.get("target_weights") or {}).items()}
        if len(tw) < min_targets:
            continue
        try:
            reb_day = _date.fromisoformat(reb_iso)
        except Exception:
            continue
        fill_day = next((d for d in trading_dates if d > reb_day), None)
        if fill_day is None:
            continue                        # 末调仓后无成交日 ⇒ 换更早样本
        # 成交截至 fill_day 的累计净持仓
        pos: dict[str, int] = {}
        for t in trades:
            if t.date > fill_day:
                break
            v = int(getattr(t, "volume", 0))
            sgn = 1 if getattr(getattr(t, "side", None), "value", None) == "BUY" \
                or str(getattr(t, "side", "")) == "BUY" else -1
            pos[t.symbol] = pos.get(t.symbol, 0) + sgn * v
        # 该次调仓委托有真实落地才作保真样本（created_date=调仓日 且部分/全部成交）
        filled_orders = [
            o for o in getattr(result, "orders", []) or []
            if getattr(o, "created_date", None) == reb_day
            and int(getattr(o, "filled_volume", 0) or 0) > 0
            and getattr(o, "symbol", "") in tw
        ]
        if not filled_orders:
            continue
        av: dict[str, float] = {}
        fill_iso = fill_day.isoformat()
        for sym in tw:
            close = _close_asof(sym, fill_iso)
            if close is None:
                continue
            av[sym] = float(Decimal(pos.get(sym, 0)) * close)
        if not av:
            continue
        return {
            "rebalance_date": reb_iso,
            "valuation_date": fill_iso,
            "target_weights": tw,
            "actual_values": av,
        }
    return None


def dividend_tax_evidence(entries: Sequence[Any]) -> dict[str, Any]:
    """S-4 证据：``penalty_tax_amount``（20% 惩罚档合计）/
    ``total_dividend_received``（分红现金总额）/ ``tax_by_bracket`` 分档。"""
    penalty = _ZERO
    total_div = _ZERO
    by_rate: dict[str, Decimal] = {}
    for e in entries:
        d = e.to_dict() if hasattr(e, "to_dict") else dict(e)
        et = str(d.get("entry_type", ""))
        if et == "DIVIDEND_TAX":
            meta = d.get("meta") or {}
            for r, v in (meta.get("tax_by_bracket") or {}).items():
                try:
                    by_rate[str(r)] = by_rate.get(str(r), _ZERO) + Decimal(str(v))
                except Exception:
                    pass
            try:
                penalty += Decimal(str((meta.get("tax_by_bracket") or {}).get("0.20", "0")))
            except Exception:
                pass
        elif et in ("DIVIDEND", "EXDIV_ADJUST"):
            try:
                amt = Decimal(str(d.get("amount", "0")))
            except Exception:
                amt = _ZERO
            if amt > _ZERO:
                total_div += amt
    return {
        "penalty_tax_amount": str(penalty),
        "total_dividend_received": str(total_div),
        "tax_by_bracket": {r: str(v) for r, v in sorted(by_rate.items())},
    }


# ------------------------------------------------------------------ 总装配

def build_run_evidence(
    result: Any,
    *,
    tables: Mapping[str, Any] | None = None,
    rebalance_history: Sequence[Mapping[str, Any]] | None = None,
    extras: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把一次回测的运行产物装配成门禁证据块（``record_run(evidence=...)`` 入参）。

    Args:
        result: ``BacktestResult``（orders/trades/journal_entries/trading_dates）。
        tables: 运行期日线帧 ``{symbol: DataFrame}``（板价重算/ADV/估值共用）。
        rebalance_history: 策略捕获的 ``_evidence_rebalances``（L-2 样本池）。
        extras: 调用侧附加键（如压测窗统计 ``round_trips``/``trading_days``、
            滑点压测收益 ``stress_return``、S-2 宽度择时键、``executed_calls``、
            ``active_features``、``fee_summary`` 等）——后置合并、同键覆盖。
    """
    entries = list(getattr(result, "journal_entries", []) or [])
    orders = list(getattr(result, "orders", []) or [])
    trades = list(getattr(result, "trades", []) or [])

    flows, unanchored = daily_cash_flows(entries)
    alloc = allocation_evidence(result, tables, rebalance_history)
    tax_ev = dividend_tax_evidence(entries)
    adv = orders_adv_ratio(orders, tables)

    evidence: dict[str, Any] = {
        "evidence_version": 1,
        "orders": serialize_orders(orders),
        "trades": serialize_trades(trades, tables),
        "ledger_entries": serialize_journal(entries),
        "daily_cash_flows": flows,
        "daily_cash_flows_unanchored_days": unanchored,
        "roundtrip_total_fee": str(golden_roundtrip_fee()),
        "orders_adv_ratio": adv["ratios"],
        "orders_adv_ratio_skipped": adv["skipped"],
        "target_weights": alloc["target_weights"],
        "actual_values": alloc["actual_values"],
        "rebalance_evidence_date": alloc.get("rebalance_date"),
        "rebalance_valuation_date": alloc.get("valuation_date"),
        "penalty_tax_amount": tax_ev["penalty_tax_amount"],
        "total_dividend_received": tax_ev["total_dividend_received"],
        "tax_by_bracket": tax_ev["tax_by_bracket"],
        "trades_count": len(trades),
    }
    if extras:
        evidence.update(dict(extras))
    return to_json_safe(evidence)
