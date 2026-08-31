#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T110 数据层验收：三源抽样比对 + 停牌命中 100% + 幂等哈希一致（G2 / FR-DATA-1）。

本模块是数据层的**验收裁判**（G2 门禁），不是生产者/清洗者 —— 它站在
``collector``/``cleaner`` 之上，用三把尺子量已入库的数据是否达标：

  ① ``validate`` —— **三源抽样比对**（FR-DATA-1）：
     抽 N 只，对每只跑 baostock（主源）vs 新浪 vs 腾讯交叉比对，逐字段算
     **最大相对偏差**，超阈值（``check_tol``=0.2%，spec FR-DATA-1 红线）记
     ``exceed``；同时校验主源行内无缺值（"行内无缺值"验收）。2015 年之前的行
     不参与偏差比较（"跨源分歧≤2015 年后 <0.2pp 阈值"）。

  ② ``check_suspension_hit`` —— **停牌命中 100%**（FR-DATA-2 / R1）：
     对每只标的，用已知停牌日（外部真值）对照清洗后输出：**已知停牌日必须
     已被过滤（即不在输出帧里）**。任一已知停牌日残留即算漏检（missed）；
     108% 命中 = 漏检为空。走 ``cleaner.enforce_tradestatus``（⛔ 只过滤不填充，
     R1）体现过滤动作。

  ③ ``check_idempotency`` —— **幂等哈希一致**（FR-DATA-6）：
     同 (symbol, 区间) 重跑两次，分区文件 ``hash_file()`` SHA-256 必须一致。

不做的事（⛔ 边界）：本模块**不采集、不落盘、不清洗**，只读取/验证；采集校验腿
失败的 warning 不在此展开（那是 collector 的职责）。默认取数器仍走
``collector._DEFAULT_CHECK_FETCHERS``（akshare 懒导入）与 ``baostock_source.fetch``，
本模块**依赖注入**后即可全离线跑单测（同 collector 的测试纪律）。

离线可测性：``fetch_fn``/``check_fetchers``/``root`` 全部可注入，单测全程不打网、
不真睡、不写进生产目录（root 用 tmp_path）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd

from data import cleaner as cln
from data import collector as col
from finai.sources import baostock_source
from finai.sources.adjustment_mode import AdjustmentMode
from finai.sources.base import OK

logger = col.logger

__all__ = [
    "AcceptanceError", "PrimaryFetchError",
    "FieldDeviation", "NullFieldIssue", "ThreeSourceReport",
    "SuspensionHit", "SuspensionReport", "IdempotencyReport",
    "ThreeSourceValidator",
]


# =====================================================================
# 常量（对齐 collector / spec，⛔ 不另起炉灶）
# =====================================================================

#: 三源比对容差（无量纲，0.002 = 0.2%）。spec FR-DATA-1 红线：跨源分歧 ≤0.2pp。
#: ⛔ 复用 collector.CHECK_TOL，同源同阈值，本模块不重定义。
DEFAULT_CHECK_TOL = col.CHECK_TOL

#: 2015 年之前的行不参与偏差比较（spec FR-DATA-1「2015 年后 <0.2pp 阈值」）。
DEFAULT_MIN_COMPARABLE_DATE = "2015-01-01"

#: 三源比对字段 → 各校验源可校字段（复用 collector 的登记表，⛔ 不手写）。
#:   新浪可校 OHLC/volume/amount；腾讯 amount 实为成交量 ⇒ **只校 OHLC**。
SOURCE_FIELDS: dict[str, tuple[str, ...]] = {
    col.SOURCE_SINA: tuple(col._SINA_COLS.keys()),
    col.SOURCE_TENCENT: tuple(col._TENCENT_COLS.keys()),
}

#: baostock（主源）可校字段：六项全量。
PRIMARY_FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "amount")

#: 抽样默认只数（默认 10，T110 任务）。
DEFAULT_SAMPLE_SIZE = 10


# =====================================================================
# 异常族（⛔ 绝不静默）
# =====================================================================

class AcceptanceError(ValueError):
    """T110 验收域的错误基类。"""


class PrimaryFetchError(AcceptanceError):
    """主源取数非 OK/空帧 → 无法验收（⛔ 不静默当"无数据"）。

    同 collector ``_fetch_with_retry`` 的 FAIL 态语义：EMPTY_OK（合法空行）也是
    **不为 OK**，此时无法给出比对结论，必须显式 raise 让调用方决定。
    """


# =====================================================================
# 验收报告结构
# =====================================================================

@dataclass(frozen=True)
class FieldDeviation:
    """单 (symbol, 源, 字段) 的最大相对偏差。``exceed`` = 超阈值（红线）。"""

    symbol: str
    source: str          # "sina" | "tencent"
    field: str           # open/high/low/close/volume/amount
    max_rel_dev: float   # 无量纲最大相对偏差（0.002 = 0.2%）
    tol: float

    @property
    def exceed(self) -> bool:
        return self.max_rel_dev > self.tol


@dataclass(frozen=True)
class NullFieldIssue:
    """主源行内缺值（"行内无缺值"验收）。``symbol``/``field``/``null_count``。"""

    symbol: str
    field: str
    null_count: int


@dataclass
class ThreeSourceReport:
    """``validate`` 的聚合报告。``ok`` = 无超阈偏差、无取数失败、行内无缺值。"""

    total_symbols: int = 0
    sampled_symbols: int = 0
    deviations: list[FieldDeviation] = field(default_factory=list)
    null_issues: list[NullFieldIssue] = field(default_factory=list)
    fetch_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """三源比对全部达标（⛔ 任一超阈偏差/取数失败/缺值即 False）。"""
        return (
            not any(d.exceed for d in self.deviations)
            and not self.fetch_errors
            and not self.null_issues
        )

    def failures(self) -> list[str]:
        """把不达标项转成可读清单（供上报/日志）。"""
        out: list[str] = []
        for d in self.deviations:
            if d.exceed:
                out.append(
                    f"{d.symbol} [{d.source}] {d.field} 最大相对偏差 "
                    f"{d.max_rel_dev:.4%} > 阈值 {d.tol:.2%}")
        for n in self.null_issues:
            out.append(f"{n.symbol} 行内 {n.field} 缺 {n.null_count} 个值")
        out.extend(self.fetch_errors)
        return out


@dataclass(frozen=True)
class SuspensionHit:
    """单 (symbol, 停牌日) 的命中结果。``filtered`` = 该停牌日已被滤掉。"""

    symbol: str
    date: str
    filtered: bool


@dataclass
class SuspensionReport:
    """``check_suspension_hit`` 的聚合报告。``ok`` = 漏检为空（100% 命中）。

    ⚠ ``fetch_errors`` 记录主源取数失败的 symbol（⛔ 不静默跳过——比对 100%
    命中只有**取到数据**才有意义；有取数失败时 ``ok`` 恒 False）。
    """

    total_known: int = 0
    hits: list[SuspensionHit] = field(default_factory=list)
    fetch_errors: list[str] = field(default_factory=list)

    @property
    def missed(self) -> list[SuspensionHit]:
        """未被过滤的已知停牌日（= 漏检，意味着停牌命中 <100%）。"""
        return [h for h in self.hits if not h.filtered]

    @property
    def hit_rate(self) -> float:
        """已过滤已知停牌日占比（1.0 = 100% 命中）。"""
        if self.total_known <= 0:
            return 1.0
        return (self.total_known - len(self.missed)) / self.total_known

    @property
    def ok(self) -> bool:
        """100% 命中 = 无任何已知停牌日残留**且**无主源取数失败。"""
        return not self.missed and not self.fetch_errors


@dataclass(frozen=True)
class IdempotencyReport:
    """``check_idempotency`` 的聚合报告。``identical`` = 两次哈希一致。"""

    symbol: str
    start_date: str
    end_date: str
    first_hash: str
    second_hash: str
    path: Path

    @property
    def identical(self) -> bool:
        return self.first_hash == self.second_hash


# =====================================================================
# 三源验收裁判
# =====================================================================

class ThreeSourceValidator:
    """T110 三源验收裁判（G2 门禁）。

    ⭐ 只读不写（除 ``check_idempotency`` 的幂等重跑是**刻意**重写同分区，那是
    FR-DATA-6 的验收对象）。默认取数器复用 collector 的登记表与 baostock 主源
    （akshare 懒导入），全部可注入 ⇒ 离线单测。

    依赖注入（离线可测，同 collector 纪律）：``fetch_fn`` 取主源（默认
    ``baostock_source.fetch``）、``check_fetchers`` 取校验腿（默认 collector 登记表）、
    ``root`` 为落盘根目录（幂等重跑写这里，测试用 tmp_path）。
    """

    def __init__(
        self,
        *,
        root: Path | str = Path("data/daily_bars"),
        adjust_mode: AdjustmentMode = AdjustmentMode.HFQ,
        check_tol: float = DEFAULT_CHECK_TOL,
        min_comparable_date: str = DEFAULT_MIN_COMPARABLE_DATE,
        fetch_fn: Callable[..., Any] | None = None,
        check_fetchers: Mapping[str, Callable[..., Any]] | None = None,
    ) -> None:
        self.root = Path(root)
        self.adjust_mode = adjust_mode
        self.check_tol = float(check_tol)
        self.min_comparable_date = pd.Timestamp(min_comparable_date)
        self._fetch_fn = fetch_fn if fetch_fn is not None else baostock_source.fetch
        self._check_fetchers = (
            dict(col._DEFAULT_CHECK_FETCHERS) if check_fetchers is None
            else dict(check_fetchers))

    # ------------------------------------------------------------------
    # 主源取数（复用 collector 参数构造，⛔ 不手写复权字面量）
    # ------------------------------------------------------------------

    def _fetch_primary_bars(
        self, symbol: str, start_date: str, end_date: str,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        """取主源 bars（已由 fetch 内 R1 停牌过滤），返回 (规整 bars 帧, meta)。

        ⛔ 非 OK/空帧 → raise ``PrimaryFetchError``（EMPTY_OK 也是"不为 OK"，
        此时无法验收，必须显式上报而非静默跳过）。
        """
        adjustflag = col._resolve_adjustflag(self.adjust_mode)  # ⛔ 显式，非默认
        params = {
            "code": symbol,
            "fields": ",".join(col.BAOSTOCK_DAILY_FIELDS),
            "start_date": start_date,
            "end_date": end_date,
            "frequency": "d",
            "adjustflag": adjustflag,
        }
        res = self._fetch_fn("kline", **params)
        if res.state != OK or res.frame is None:
            detail = getattr(res, "detail", "") or res.state
            raise PrimaryFetchError(
                f"主源取数 {symbol} 非 OK（state={res.state}）—— ⛔ 不静默当无数据: {detail}")
        meta = dict(getattr(res, "meta", {}) or {})
        bars = col._build_bars(res.frame, mode=self.adjust_mode)
        return bars, meta

    # ------------------------------------------------------------------
    # ① 三源抽样比对（FR-DATA-1）
    # ------------------------------------------------------------------

    def _sample_symbols(
        self, symbols: Iterable[str], sample_size: int | None,
    ) -> list[str]:
        """确定性抽样：取前 ``sample_size`` 只（⛔ 不用随机数 ⇒ 可复现）。

        ``sample_size`` 为 None/<=0 表示全量（不抽样）；默认 ``DEFAULT_SAMPLE_SIZE``。
        """
        seq = list(symbols)
        if not sample_size or sample_size <= 0:
            return seq
        return list(islice(seq, sample_size))

    def _max_rel_dev(
        self, base: pd.Series, cand: pd.Series,
    ) -> float | None:
        """单字段最大相对偏差（仅 ``base != 0`` 且双方非空的行）。

        与 ``collector._cross_check_one`` 同口径：``|cand - base| / |base|`` 取最大。
        """
        base_n = pd.to_numeric(base, errors="coerce")
        cand_n = pd.to_numeric(cand, errors="coerce")
        valid = base_n.notna() & cand_n.notna() & (base_n != 0)
        if not valid.any():
            return None
        return float(((cand_n[valid] - base_n[valid]).abs()
                      / base_n[valid].abs()).max())

    def _compare_source(
        self, symbol: str, source: str, check_df: pd.DataFrame,
        primary: pd.DataFrame,
    ) -> list[FieldDeviation]:
        """单校验源对账：按日期内连接，逐字段算最大相对偏差（⛔ 2015 前不算）。

        对齐复用 ``collector._CHECK_FIELDS``/``_CHECK_DATE_COL``，⛔ 不另造映射表。
        停牌跳行/区间差异行数不一致是自然结果，不计入字段偏差（同 collector）。

        ⚠ 键列名碰撞：腾讯校验腿列名与主源**完全同名**（``open``/``close``…），
        ``merge(suffixes=("", "_c"))`` 只给**撞名的列**加 ``_c`` 后缀，且此时
        ``merged[src_col]`` 读到的是**主源列**——若直接复用 collector 的读法，
        会让腾讯腿永远和主源比自身（假 0 偏差）。故本函数在 merge 前把校验腿的
        测量列统一加 ``ck_`` 前缀，候选列名变成 ``ck_{src_col}``，彻底避开碰撞。
        """
        if check_df is None or check_df.empty:
            return []
        colmap = col._CHECK_FIELDS[source]
        date_col = col._CHECK_DATE_COL[source]
        chk = check_df.copy()
        chk["date"] = pd.to_datetime(chk[date_col]).dt.date
        # ⚠ 键列名碰撞处理：校验腿测量列统一加 ``ck_`` 前缀，避免与主源同名列被
        #   ``suffixes`` 重命名后 ``merged[src_col]`` 读到主源列（假 0 偏差）。
        rename = {c: f"ck_{c}" for c in chk.columns if c != "date"}
        chk = chk.rename(columns=rename)
        merged = primary.merge(chk, on="date", how="inner")
        # ⛔ 2015 年之前的行不参与偏差比较（spec FR-DATA-1「2015 年后 <0.2pp」）
        merged = merged[pd.to_datetime(merged["date"]) >= self.min_comparable_date]
        if merged.empty:
            return []
        out: list[FieldDeviation] = []
        for field_name, src_col in colmap.items():
            cand_col = f"ck_{src_col}"
            if field_name not in merged.columns or cand_col not in merged.columns:
                continue
            dev = self._max_rel_dev(merged[field_name], merged[cand_col])
            if dev is None:
                continue
            out.append(FieldDeviation(
                symbol=symbol, source=source, field=field_name,
                max_rel_dev=dev, tol=self.check_tol))
        return out

    def _primary_null_issues(self, symbol: str, bars: pd.DataFrame) -> list[NullFieldIssue]:
        """主源行内缺值检查（FR-DATA-1「行内无缺值」）。"""
        issues: list[NullFieldIssue] = []
        for fld in PRIMARY_FIELDS:
            if fld not in bars.columns:
                continue
            n = int(bars[fld].isna().sum())
            if n:
                issues.append(NullFieldIssue(symbol=symbol, field=fld, null_count=n))
        return issues

    def validate(
        self,
        symbols: Iterable[str],
        start_date: str,
        end_date: str,
        sample_size: int | None = DEFAULT_SAMPLE_SIZE,
    ) -> ThreeSourceReport:
        """三源抽样比对：抽 N 只，主源 vs 新浪 vs 腾讯逐字段比相对偏差。

        返回 ``ThreeSourceReport``（``ok`` = 全部达标）；任一校验源取数失败只记
        ``fetch_errors``（不 raise，⛔ 校验腿失败不阻断验收——与 collector 同精神，
        但会显式记账到报告里以便人工复核）。
        """
        symbols_list = self._sample_symbols(symbols, sample_size)
        report = ThreeSourceReport(
            total_symbols=len(list(symbols)), sampled_symbols=len(symbols_list))
        for symbol in symbols_list:
            try:
                bars, _meta = self._fetch_primary_bars(symbol, start_date, end_date)
            except PrimaryFetchError as exc:
                report.fetch_errors.append(str(exc))
                continue
            if bars.empty:
                # 空帧（全区间停牌/无数据）：主源 EMPTY_OK —— 无法比对，记账不 raise。
                report.fetch_errors.append(
                    f"{symbol} 主源 bars 为空（疑似全区间停牌/无数据），跳过比对")
                continue
            report.null_issues.extend(self._primary_null_issues(symbol, bars))
            for source, fetcher in self._check_fetchers.items():
                try:
                    check_df = fetcher(symbol, start_date, end_date, self.adjust_mode)
                except Exception as exc:  # noqa: BLE001 - 校验腿失败不阻断验收
                    report.fetch_errors.append(
                        f"{symbol} [{source}] 校验腿取数失败（仅记账）: "
                        f"{type(exc).__name__}")
                    continue
                report.deviations.extend(
                    self._compare_source(symbol, source, check_df, bars))
        return report

    # ------------------------------------------------------------------
    # ② 停牌命中 100%（FR-DATA-2 / R1）
    # ------------------------------------------------------------------

    def check_suspension_hit(
        self,
        symbols: Iterable[str],
        start_date: str,
        end_date: str,
        known_suspensions: Mapping[str, Sequence[str]],
    ) -> SuspensionReport:
        """已知停牌日必须已被过滤（⛔ 残留即漏检，100% 命中 = 漏检为空）。

        ``known_suspensions``：``{symbol: [停牌日 'YYYY-MM-DD', ...]}`` 为**外部真值**
        （如 12 号 §9-A 实测）。对每只：主源取原始帧 → ``cleaner.enforce_tradestatus``
        过滤（只滤不填，R1）→ 逐已知停牌日核对是否残留于输出帧。

        漏检判定（⛔ 只认「停牌日残留在过滤后输出里」这一种）：
          * 残留 = 该日行 ``tradestatus`` 被错误标 '1'（或未被过滤）→ **missed**；
          * 真值日期不在主源帧里（主源口径也认为该日无交易）→ 不算漏检，
            记 WARNING 供人工核对（真值与主源口径不一致须人看，不是机器判）。
        """
        report = SuspensionReport()
        for symbol in symbols:
            known = known_suspensions.get(symbol)
            if known is None:
                continue
            try:
                bars, _meta = self._fetch_primary_bars(symbol, start_date, end_date)
            except PrimaryFetchError as exc:
                report.fetch_errors.append(str(exc))
                continue
            # ⛔ 只过滤不填充（R1）：缺 tradestatus 列会 raise（fail-closed），
            # 该 symbol 的停牌验收无结论 → 记入 fetch_errors（不静默跳过）。
            try:
                clean, _n_susp = cln.enforce_tradestatus(bars.copy())
            except AcceptanceError:
                raise
            except ValueError as exc:
                report.fetch_errors.append(
                    f"{symbol} 停牌过滤失败（{type(exc).__name__}）: {exc}")
                continue
            clean_dates = set(pd.to_datetime(clean["date"]).dt.strftime("%Y-%m-%d"))
            raw_dates = set(pd.to_datetime(bars["date"]).dt.strftime("%Y-%m-%d"))
            for d in known:
                d = str(d).strip()
                # 主源帧里该停牌日是否真的出现（真值是否被主源撞上）。
                present_in_raw = d in raw_dates
                filtered = d not in clean_dates
                report.total_known += 1
                report.hits.append(SuspensionHit(
                    symbol=symbol, date=d, filtered=filtered))
                if not filtered and not present_in_raw:
                    # 真值日期根本不在主源帧里（主源口径也认为无交易）→ 记账提醒。
                    logger.warning(
                        "已知停牌日 %s (symbol=%s) 未出现在主源帧 —— 真值与主源口径"
                        "不一致，请人工核对该日是否确为停牌。", d, symbol)
        return report

    # ------------------------------------------------------------------
    # ③ 幂等哈希一致（FR-DATA-6）
    # ------------------------------------------------------------------

    def check_idempotency(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> IdempotencyReport:
        """同 (symbol, 区间) 重跑两次，分区文件 ``hash_file()`` SHA-256 一致。

        ⛔ 幂等的根是 ``write_daily_bars_partitioned`` 的「已有分区 + 新数据按日期
        去重覆盖写」；两次运行结果逐字节一致。本方法只做"跑两次 → 比哈希"，
        采集逻辑委托给 ``collector.DailyCollector``（复用，⛔ 不重造轮子）。
        """
        collector = col.DailyCollector(
            root=self.root, adjust_mode=self.adjust_mode,
            fetch_fn=self._fetch_fn, check_fetchers=self._check_fetchers)
        r1 = collector.collect_symbol(symbol, start_date, end_date, sample=False)
        if not r1.ok:
            raise PrimaryFetchError(
                f"幂等重跑第 1 次失败（state={r1.state}）—— 无法验收: {r1.warnings}")
        path = r1.paths[0]
        first_hash = col.hash_file(path)
        r2 = collector.collect_symbol(symbol, start_date, end_date, sample=False)
        if not r2.ok:
            raise PrimaryFetchError(
                f"幂等重跑第 2 次失败（state={r2.state}）—— 无法验收: {r2.warnings}")
        second_hash = col.hash_file(path)
        return IdempotencyReport(
            symbol=symbol, start_date=start_date, end_date=end_date,
            first_hash=first_hash, second_hash=second_hash, path=path)
