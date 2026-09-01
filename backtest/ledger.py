#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T201 §5 双账本 —— Journal（append-only 流水）+ BookView（可重算视图）+ Ledger（组合）。

双账本纪律（SDD-2）：

  ① **Journal 是事实，只增不改**：每条 ``JournalEntry`` 带 ``tx_hash``
     （SHA-256 of canonical repr）作幂等重放键。重复 append **静默忽略并返回
     ``False``** —— 事件重放 / 断线重连补推 / 券商重复回报都不会二次记账。
  ② **BookView 是推导，可丢弃**：现金、持仓、市值、NAV 全部能从 Journal 重放
     重建（``Journal.replay()``），⛔ 不落盘、不作为真相来源。
  ③ **金额全 ``Decimal``，落盘转字符串**：``JournalEntry.to_dict()`` 把 Decimal
     → ``str``、date → ``isoformat()``、枚举 → ``.value``。⛔ 禁止 float 落盘
     （二进制噪声会让同一笔流水在两台机器上算出不同 ``tx_hash``）。

关键口径（写死在此，⛔ 改动需同步 T201_design.md）：

  · **买入**：``cash -= price×volume + Σfees``；``volume += v``；``avg_cost``
    按**成交价**加权重算（不含费用，费用单独在 ``fees`` 里可归因）。
  · **卖出**：``cash += price×volume - Σfees``；``volume -= v``；``avg_cost``
    **不变**（卖出不改变剩余持仓的持有成本）。持仓清零时 ``avg_cost`` 归 0。
  · **除权除息**（FR-BT-4）：``volume' = round_half_up(volume × factor)``、
    ``avg_cost' = avg_cost / factor``、``cash += cash_dividend × old_volume``。
    同时把 ``last_close`` 按同一 factor 折算并重算市值 —— **保证除权前后净值曲线
    无跳变**（T202 必挂用例 #4 的锚点）；当日 settle 读到真实除权后 bar 会再覆盖一次。
  · **结算**：bar 存在 → 刷新 ``last_close`` / ``market_value``；bar 缺失
    （停牌）→ **市值冻结不动**（T202 必挂用例 #3：停牌三日 NAV 水平线）。
    ``NAV = cash + Σ market_value``（``frozen_cash`` 是 cash 中被挂单占用部分的
    标记，⛔ 不重复计入）。
  · **T+1**：``advance_sellable(today)`` 把 ``sellable_date <= today`` 的买入
    批次转入可卖。批次索引由 ``register_trades()`` / ``process_trade()`` 建立。

⛔ long-only（SDD-7）：任何会把持仓打成负数的卖出一律 raise ``LedgerError``，
不静默截断 —— 负持仓意味着上游撮合的 T+1/可卖校验漏了，必须炸出来。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from backtest.constants import FeeItem, OrderSide
from backtest.types import Bar, PortfolioView, Position, Trade

__all__ = [
    "JournalType",
    "JournalEntry",
    "LedgerError",
    "DuplicateEntryError",
    "compute_tx_hash",
    "canonical_repr",
    "Journal",
    "BookView",
    "Ledger",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")
#: avg_cost 保留位数（除权对折 / 加权均价会产生长尾小数，统一收敛避免无界膨胀）
_COST_QUANT = Decimal("0.00000001")


class JournalType(str, Enum):
    """流水条目类型（T201 §5）。"""

    TRADE = "TRADE"                  # 成交（含随成交发生的费用明细）
    FEE = "FEE"                      # 独立费用（管理费等，不随成交）
    DIVIDEND = "DIVIDEND"            # 现金分红入账
    EXDIV_ADJUST = "EXDIV_ADJUST"    # 除权调整（送股 / 转增 / 拆股）
    CASH_IN = "CASH_IN"              # 出入金（含期初本金）
    SETTLE = "SETTLE"                # 日终结算快照


class LedgerError(ValueError):
    """账本一致性被破坏（超卖、非法 factor、负现金语义等）。"""


class DuplicateEntryError(LedgerError):
    """tx_hash 重复。

    注意：``Journal.append`` **不**抛这个异常（幂等重放要静默返回 ``False``）；
    仅供需要严格模式的调用方通过 ``append_strict`` 使用。
    """


# ----------------------------------------------------------------------
# canonical 表示 + tx_hash
# ----------------------------------------------------------------------

def _canonicalize(value: Any) -> Any:
    """递归把值转成 JSON 可序列化的 canonical 形态。

    ``Decimal`` → ``str``（⛔ 不转 float）；``date`` → ``isoformat()``；
    ``Enum`` → ``.value``；``dict`` → 键 canonical 化后按键名排序；
    ``list/tuple`` → 逐元素 canonical（保序，顺序是语义的一部分）。
    """
    if isinstance(value, Decimal):
        # 归一化去掉尾随零，保证 Decimal("10") 与 Decimal("10.00") 同 hash
        normalized = value.normalize()
        text = format(normalized, "f")
        return text
    if isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return _canonicalize(value.value)
    if isinstance(value, _date):
        return value.isoformat()
    if isinstance(value, Mapping):
        items = {}
        for key, val in value.items():
            canon_key = _canonicalize(key)
            if not isinstance(canon_key, str):
                canon_key = str(canon_key)
            items[canon_key] = _canonicalize(val)
        return {k: items[k] for k in sorted(items)}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    if value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):        # 防御：float 入账是错的，显式炸
        raise LedgerError(
            f"流水字段禁止 float（精度不可复现），请改用 Decimal：{value!r}"
        )
    return str(value)


def canonical_repr(entry_dict: Mapping[str, Any]) -> str:
    """entry 的 canonical JSON 字符串（键排序、Decimal→str、date→isoformat）。

    ``tx_hash`` 键若存在会被剔除（hash 不能包含自身）。
    """
    payload = {k: v for k, v in entry_dict.items() if k != "tx_hash"}
    canon = _canonicalize(payload)
    return json.dumps(canon, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_tx_hash(entry_dict: Mapping[str, Any]) -> str:
    """对 entry 的 canonical 表示做 SHA-256，返回 64 位小写 hex。

    同数据 → 同 hash（跨进程 / 跨机器稳定：不依赖 dict 插入顺序、不依赖 float）。
    """
    return hashlib.sha256(canonical_repr(entry_dict).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# JournalEntry
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class JournalEntry:
    """一条 append-only 流水（不可变）。

    ``amount``：现金变动，**正 = 流入，负 = 流出**（非现金条目如 EXDIV_ADJUST
    的股数调整部分为 0）。
    """

    tx_hash: str
    date: _date
    entry_type: JournalType
    symbol: str = ""
    side: OrderSide | None = None
    volume: int = 0
    price: Decimal = _ZERO
    amount: Decimal = _ZERO
    fees: dict[FeeItem, Decimal] = field(default_factory=dict)
    ref_id: str = ""
    meta: dict = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        date: _date,
        entry_type: JournalType,
        symbol: str = "",
        side: OrderSide | None = None,
        volume: int = 0,
        price: Decimal = _ZERO,
        amount: Decimal = _ZERO,
        fees: Mapping[FeeItem, Decimal] | None = None,
        ref_id: str = "",
        meta: Mapping[str, Any] | None = None,
    ) -> "JournalEntry":
        """构造 entry 并自动算好 ``tx_hash``（唯一推荐入口）。"""
        payload: dict[str, Any] = {
            "date": date,
            "entry_type": entry_type,
            "symbol": symbol,
            "side": side,
            "volume": volume,
            "price": price,
            "amount": amount,
            "fees": dict(fees or {}),
            "ref_id": ref_id,
            "meta": dict(meta or {}),
        }
        return cls(tx_hash=compute_tx_hash(payload), **payload)

    def to_dict(self) -> dict[str, Any]:
        """落盘形态：Decimal → 字符串、date → isoformat、枚举 → value。"""
        return {
            "tx_hash": self.tx_hash,
            "date": self.date.isoformat(),
            "entry_type": self.entry_type.value,
            "symbol": self.symbol,
            "side": self.side.value if self.side is not None else None,
            "volume": int(self.volume),
            "price": str(self.price),
            "amount": str(self.amount),
            "fees": {k.value: str(v) for k, v in sorted(
                self.fees.items(), key=lambda kv: kv[0].value)},
            "ref_id": self.ref_id,
            "meta": _canonicalize(self.meta),
        }


# ----------------------------------------------------------------------
# Journal
# ----------------------------------------------------------------------

class Journal:
    """append-only 流水簿。``tx_hash`` 唯一，重复 append 静默忽略。"""

    def __init__(self) -> None:
        self._entries: list[JournalEntry] = []
        self._hashes: set[str] = set()

    # —— 只读访问 ——
    @property
    def entries(self) -> Sequence[JournalEntry]:
        """只读快照（返回浅拷贝 list，调用方改它不影响账本）。"""
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(list(self._entries))

    def contains(self, tx_hash: str) -> bool:
        """该 tx_hash 是否已记账。"""
        return tx_hash in self._hashes

    # —— 写入 ——
    def append(self, entry: JournalEntry) -> bool:
        """追加一条流水。

        Returns:
            ``True`` = 新记账；``False`` = tx_hash 已存在（幂等重放，静默忽略）。
        """
        if entry.tx_hash in self._hashes:
            return False
        self._entries.append(entry)
        self._hashes.add(entry.tx_hash)
        return True

    def append_strict(self, entry: JournalEntry) -> None:
        """严格模式追加：重复直接 raise ``DuplicateEntryError``。"""
        if not self.append(entry):
            raise DuplicateEntryError(f"tx_hash 已存在: {entry.tx_hash}")

    def filter_by_type(self, entry_type: JournalType) -> list[JournalEntry]:
        """按类型筛选（只读辅助）。"""
        return [e for e in self._entries if e.entry_type is entry_type]

    def replay(self, *, initial_date: _date | None = None) -> "BookView":
        """从头重放构建 BookView（验证性：与在线视图逐字段比对可查账本漂移）。

        只重放**可推导**的条目：CASH_IN / TRADE / DIVIDEND / FEE / EXDIV_ADJUST。
        SETTLE 只携带快照，不参与推导（市值须由 bars 重新刷新）。
        """
        first_date = initial_date or (
            self._entries[0].date if self._entries else _date(1970, 1, 1)
        )
        book = BookView(cash=_ZERO, date=first_date)
        for entry in self._entries:
            book.date = entry.date
            if entry.entry_type in (
                JournalType.CASH_IN,
                JournalType.DIVIDEND,
                JournalType.FEE,
            ):
                book.cash += entry.amount
            elif entry.entry_type is JournalType.TRADE:
                if entry.side is None:
                    raise LedgerError(f"TRADE 流水缺 side: {entry.tx_hash}")
                book.apply_trade_effect(
                    symbol=entry.symbol,
                    side=entry.side,
                    volume=entry.volume,
                    price=entry.price,
                    total_fees=sum(entry.fees.values(), _ZERO),
                )
            elif entry.entry_type is JournalType.EXDIV_ADJUST:
                factor = Decimal(str(entry.meta.get("factor", "1")))
                dividend = Decimal(str(entry.meta.get("cash_dividend", "0")))
                book.process_exdiv(entry.symbol, factor, dividend)
        book.recompute_nav()
        return book


# ----------------------------------------------------------------------
# BookView
# ----------------------------------------------------------------------

class BookView:
    """推导视图：现金 / 持仓 / 市值 / NAV。⛔ 不落盘。"""

    def __init__(self, cash: Decimal = _ZERO, *, date: _date) -> None:
        self.cash: Decimal = Decimal(cash)
        self.frozen_cash: Decimal = _ZERO
        self.positions: dict[str, Position] = {}
        self.nav: Decimal = Decimal(cash)
        self.date: _date = date
        #: T+1 待解禁批次：symbol → [[sellable_date, volume], ...]
        self._pending_sellable: dict[str, list[list[Any]]] = {}

    # —— 内部工具 ——
    def _position(self, symbol: str) -> Position:
        pos = self.positions.get(symbol)
        if pos is None:
            pos = Position(symbol=symbol)
            self.positions[symbol] = pos
        return pos

    def total_market_value(self) -> Decimal:
        """Σ market_value。"""
        return sum((p.market_value for p in self.positions.values()), _ZERO)

    @property
    def total_nav(self) -> Decimal:
        """``cash + Σ market_value``（实时计算，不依赖上次 settle）。"""
        return self.cash + self.total_market_value()

    def recompute_nav(self) -> Decimal:
        """刷新并返回 ``self.nav``。"""
        self.nav = self.total_nav
        return self.nav

    # —— 成交 ——
    def apply_trade_effect(
        self,
        *,
        symbol: str,
        side: OrderSide,
        volume: int,
        price: Decimal,
        total_fees: Decimal,
    ) -> None:
        """成交对现金/持仓的影响（Journal.replay 与在线路径共用同一实现）。"""
        if volume <= 0:
            raise LedgerError(f"成交数量须为正: {volume}")
        gross = price * Decimal(volume)
        pos = self._position(symbol)
        if side is OrderSide.BUY:
            self.cash -= gross + total_fees
            old_volume = pos.volume
            new_volume = old_volume + volume
            # 加权均价（按成交价，费用不计入持有成本）
            pos.avg_cost = (
                (pos.avg_cost * Decimal(old_volume) + gross) / Decimal(new_volume)
            ).quantize(_COST_QUANT, rounding=ROUND_HALF_UP)
            pos.volume = new_volume
        else:
            if volume > pos.volume:
                raise LedgerError(
                    f"超卖（long-only 不允许负持仓）: {symbol} 卖 {volume} > 持仓 {pos.volume}"
                )
            self.cash += gross - total_fees
            pos.volume -= volume
            pos.sellable = max(0, min(pos.sellable - volume, pos.volume))
            if pos.volume == 0:
                pos.avg_cost = _ZERO
                pos.market_value = _ZERO
                self._pending_sellable.pop(symbol, None)
        if pos.volume > 0 and pos.last_close > 0:
            pos.market_value = pos.last_close * Decimal(pos.volume)

    def process_trade(self, trade: Trade) -> None:
        """吃一笔成交：更新现金 / 持仓 / 成本 / T+1 待解禁批次。"""
        self.apply_trade_effect(
            symbol=trade.symbol,
            side=trade.side,
            volume=trade.volume,
            price=trade.price,
            total_fees=sum(trade.fees.values(), _ZERO),
        )
        if trade.side is OrderSide.BUY:
            self.register_trade(trade)
        self.recompute_nav()

    # —— T+1 ——
    def register_trade(self, trade: Trade) -> None:
        """登记一笔买入成交的 T+1 解禁批次（``sellable_date`` 为 None 时视为已可卖）。"""
        if trade.side is not OrderSide.BUY:
            return
        if trade.sellable_date is None:
            pos = self._position(trade.symbol)
            pos.sellable = min(pos.sellable + trade.volume, pos.volume)
            return
        self._pending_sellable.setdefault(trade.symbol, []).append(
            [trade.sellable_date, int(trade.volume)]
        )

    def register_trades(self, trades: Iterable[Trade]) -> None:
        """批量登记（供从历史 trades 重建可卖索引）。"""
        for trade in trades:
            self.register_trade(trade)

    def advance_sellable(self, today: _date) -> dict[str, int]:
        """把 ``sellable_date <= today`` 的批次转入可卖。

        Returns:
            symbol → 本次新解禁数量（只含实际有解禁的标的）。
        """
        unlocked: dict[str, int] = {}
        for symbol in list(self._pending_sellable):
            batches = self._pending_sellable[symbol]
            remaining: list[list[Any]] = []
            gained = 0
            for sellable_date, volume in batches:
                if sellable_date <= today:
                    gained += int(volume)
                else:
                    remaining.append([sellable_date, volume])
            if gained:
                pos = self._position(symbol)
                pos.sellable = min(pos.sellable + gained, pos.volume)
                unlocked[symbol] = gained
            if remaining:
                self._pending_sellable[symbol] = remaining
            else:
                self._pending_sellable.pop(symbol, None)
        return unlocked

    # —— 除权除息 ——
    def process_exdiv(
        self,
        symbol: str,
        factor: Decimal,
        cash_dividend: Decimal = _ZERO,
    ) -> None:
        """除权除息调整（FR-BT-4）。

        ``volume' = round_half_up(volume × factor)``；``avg_cost' = avg_cost / factor``；
        ``cash += cash_dividend × old_volume``；``last_close`` 同步按 factor 折算 →
        除权前后市值连续（净值曲线无跳变）。

        Args:
            factor: 送股/拆股因子（10 送 10 → ``Decimal("2")``）。须 > 0。
            cash_dividend: **每股**税前现金分红。
        """
        factor = Decimal(factor)
        cash_dividend = Decimal(cash_dividend)
        if factor <= 0:
            raise LedgerError(f"除权 factor 须为正: {factor}")
        pos = self.positions.get(symbol)
        if pos is None or pos.volume == 0:
            # 无持仓 → 只可能有现金分红意义，但按股数计为 0，直接返回
            return
        old_volume = pos.volume
        if cash_dividend != 0:
            self.cash += cash_dividend * Decimal(old_volume)
        if factor != _ONE:
            new_volume = int(
                (Decimal(old_volume) * factor).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            pos.volume = new_volume
            pos.avg_cost = (pos.avg_cost / factor).quantize(
                _COST_QUANT, rounding=ROUND_HALF_UP
            )
            pos.sellable = int(
                (Decimal(pos.sellable) * factor).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            pos.sellable = min(pos.sellable, pos.volume)
            if pos.last_close > 0:
                pos.last_close = pos.last_close / factor
                pos.market_value = pos.last_close * Decimal(pos.volume)
            # 待解禁批次同步按因子放大
            batches = self._pending_sellable.get(symbol)
            if batches:
                for batch in batches:
                    batch[1] = int(
                        (Decimal(batch[1]) * factor).quantize(
                            Decimal("1"), rounding=ROUND_HALF_UP
                        )
                    )
        self.recompute_nav()

    # —— 日终结算 ——
    def settle(self, date: _date, bars: Mapping[str, Bar]) -> set[str]:
        """日终结算：刷新市值 + NAV。

        bar 存在 → ``last_close = bar.close``、``market_value = volume × close``；
        bar 缺失（停牌）→ **冻结不动**（沿用上一日 last_close 与 market_value）。

        Returns:
            本次被刷新（bar 存在）的 symbol 集合。
        """
        self.date = date
        refreshed: set[str] = set()
        for symbol, pos in self.positions.items():
            bar = bars.get(symbol)
            if bar is None:
                continue        # 停牌：市值冻结
            pos.last_close = bar.close
            pos.market_value = bar.close * Decimal(pos.volume)
            refreshed.add(symbol)
        self.recompute_nav()
        return refreshed

    # —— 快照 ——
    @property
    def view(self) -> PortfolioView:
        """深拷贝快照（调用方改快照不会污染账本）。"""
        return PortfolioView(
            cash=self.cash,
            frozen_cash=self.frozen_cash,
            positions={
                s: Position(
                    symbol=p.symbol,
                    volume=p.volume,
                    sellable=p.sellable,
                    avg_cost=p.avg_cost,
                    last_close=p.last_close,
                    market_value=p.market_value,
                )
                for s, p in self.positions.items()
            },
            nav=self.nav,
            date=self.date,
        )


# ----------------------------------------------------------------------
# Ledger = Journal + BookView
# ----------------------------------------------------------------------

class Ledger:
    """双账本组合体：写流水（事实）+ 更新视图（推导），一次调用两边同步。

    四环境同构（SDD-1）：回测 / 模拟盘 / 实盘都用这一个 Ledger，差异只在谁产生
    ``Trade``。
    """

    def __init__(
        self,
        initial_cash: Decimal = _ZERO,
        *,
        date: _date,
        journal: Journal | None = None,
    ) -> None:
        self.journal = journal or Journal()
        self.book = BookView(cash=_ZERO, date=date)
        self._start_date = date
        if Decimal(initial_cash) != 0:
            self.deposit(Decimal(initial_cash), date=date, ref_id="INITIAL_CAPITAL")

    # —— 现金 ——
    def deposit(
        self,
        amount: Decimal,
        *,
        date: _date | None = None,
        ref_id: str = "",
    ) -> bool:
        """出入金（正 = 入金，负 = 出金）。写 CASH_IN 流水。"""
        amount = Decimal(amount)
        entry = JournalEntry.create(
            date=date or self.book.date,
            entry_type=JournalType.CASH_IN,
            amount=amount,
            ref_id=ref_id,
        )
        if not self.journal.append(entry):
            return False
        self.book.cash += amount
        self.book.recompute_nav()
        return True

    # —— 成交 ——
    def process_trade(self, trade: Trade) -> None:
        """记一笔成交：TRADE 流水 + BookView 更新。

        幂等：同 ``trade_id`` / 同内容重复调用只记一次（tx_hash 命中即跳过，
        视图也不会被二次更新）。
        """
        total_fees = sum(trade.fees.values(), _ZERO)
        gross = trade.price * Decimal(trade.volume)
        amount = -(gross + total_fees) if trade.side is OrderSide.BUY else gross - total_fees
        entry = JournalEntry.create(
            date=trade.date,
            entry_type=JournalType.TRADE,
            symbol=trade.symbol,
            side=trade.side,
            volume=int(trade.volume),
            price=trade.price,
            amount=amount,
            fees=trade.fees,
            ref_id=trade.trade_id,
            meta={
                "client_order_id": trade.client_order_id,
                "sellable_date": trade.sellable_date,
            },
        )
        if not self.journal.append(entry):
            return          # 幂等重放：已记账，视图不再动
        self.book.process_trade(trade)

    # —— 除权除息 ——
    def process_exdiv(
        self,
        symbol: str,
        factor: Decimal,
        cash_dividend: Decimal = _ZERO,
        *,
        date: _date | None = None,
        ref_id: str = "",
    ) -> None:
        """除权除息：写 EXDIV_ADJUST 流水（含现金分红金额）+ BookView 调整。"""
        factor = Decimal(factor)
        cash_dividend = Decimal(cash_dividend)
        pos = self.book.positions.get(symbol)
        old_volume = pos.volume if pos else 0
        if old_volume == 0:
            return
        dividend_cash = cash_dividend * Decimal(old_volume)
        entry = JournalEntry.create(
            date=date or self.book.date,
            entry_type=JournalType.EXDIV_ADJUST,
            symbol=symbol,
            volume=old_volume,
            amount=dividend_cash,
            ref_id=ref_id,
            meta={
                "factor": factor,
                "cash_dividend": cash_dividend,
                "old_volume": old_volume,
            },
        )
        if not self.journal.append(entry):
            return
        self.book.process_exdiv(symbol, factor, cash_dividend)

    # —— 日终 ——
    def settle(self, date: _date, bars: Mapping[str, Bar]) -> set[str]:
        """日终结算 + 写 SETTLE 快照流水。返回被刷新的 symbol 集合。"""
        refreshed = self.book.settle(date, bars)
        entry = JournalEntry.create(
            date=date,
            entry_type=JournalType.SETTLE,
            amount=_ZERO,
            ref_id=f"SETTLE:{date.isoformat()}",
            meta={
                "nav": self.book.nav,
                "cash": self.book.cash,
                "market_value": self.book.total_market_value(),
                "refreshed": sorted(refreshed),
            },
        )
        self.journal.append(entry)
        return refreshed

    def advance_sellable(self, today: _date) -> dict[str, int]:
        """T+1 推进（见 ``BookView.advance_sellable``）。"""
        return self.book.advance_sellable(today)

    def register_trades(self, trades: Iterable[Trade]) -> None:
        """从历史成交重建 T+1 待解禁索引。"""
        self.book.register_trades(trades)

    # —— 只读 ——
    @property
    def entries(self) -> Sequence[JournalEntry]:
        """流水只读快照。"""
        return self.journal.entries

    @property
    def cash(self) -> Decimal:
        return self.book.cash

    @property
    def positions(self) -> dict[str, Position]:
        return self.book.positions

    @property
    def nav(self) -> Decimal:
        return self.book.nav

    @property
    def view(self) -> PortfolioView:
        """组合快照（PortfolioView）。"""
        return self.book.view
