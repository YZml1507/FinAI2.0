#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T201 §6 feed 单测（离线，⛔ 无任何网络调用）。

两条取数路径都覆盖：``preloaded``（内存帧）与真实 parquet（``tmp_path`` 落盘，
经 ``data.collector.write_daily_bars_partitioned`` 写出，与生产布局逐字一致）。

覆盖锚点：

  ① 单日单 symbol 读回 —— Bar 每个字段精确（``Decimal`` 无 float 尾差、``date``
     是 ``datetime.date``、``is_st`` / ``adjust_mode`` 透传）。
  ② **停牌 = 键缺席**（契约 §6-1）：当日无行 ⇒ symbol 不在返回 dict；
     ⛔ 不是 None 值。
  ③ ``limit_up`` / ``limit_down`` 由 ``data.cleaner.mark_limit_flags`` 注入正确
     （60 开头主板 ±10%）。
  ④ ``isST='1'`` ⇒ ``Bar.is_st=True`` 且走 ST 档 ±5%（+6% 主板不涨停、ST 涨停）。
  ⑤ ``exdiv`` 预注入 ⇒ ``Bar.exdiv=True``（只在事件日那天）。
  ⑥ 跨年分区拼接（2024 + 2025 两个文件）。
  ⑦ ``get_trading_dates``：未注入日历 → raise；注入 mock → 期望列表。

⛔ 永不静默：断言不被 try/except 吞错。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from backtest.feed import (
    CalendarNotInjectedError,
    DataFeed,
    FeedError,
    ParquetDailyFeed,
)
from backtest.types import Bar
from data.cleaner import LimitFlagsConfig
from data.collector import write_daily_bars_partitioned

D = Decimal

_SYM = "sh.600000"
_D1 = date(2024, 3, 1)
_D2 = date(2024, 3, 4)
_D3 = date(2024, 3, 5)

#: T105 落盘列（``data/collector.py::BAOSTOCK_DAILY_FIELDS`` + 血缘列）。
#: ⛔ 落盘列里**没有** limit_up/limit_down/exdiv —— 那三列由 feed 调 cleaner 补。
_COLS = (
    "date", "open", "high", "low", "close", "preclose",
    "volume", "amount", "turn", "pctChg",
    "tradestatus", "isST", "code", "adjust_mode", "source",
)


def _row(
    d: date,
    *,
    close: float = 10.5,
    preclose: float = 10.0,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1_000_000.0,
    amount: float = 10_500_000.0,
    is_st: str = "0",
    code: str = _SYM,
    tradestatus: str = "1",
    adjust_mode: str = "hfq",
) -> dict:
    """造一行落盘形状的 bars 记录（数值为 float64，模拟真实 parquet）。"""
    return {
        "date": d,
        "open": close if open_ is None else open_,
        "high": close if high is None else high,
        "low": close if low is None else low,
        "close": close,
        "preclose": preclose,
        "volume": volume,
        "amount": amount,
        "turn": 1.23,
        "pctChg": (close - preclose) / preclose * 100.0,
        "tradestatus": tradestatus,
        "isST": is_st,
        "code": code,
        "adjust_mode": adjust_mode,
        "source": "baostock",
    }


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(_COLS))


def _feed(rows: list[dict], **kwargs) -> ParquetDailyFeed:
    """构造走 ``preloaded`` 的 feed（不碰磁盘、不打网）。"""
    return ParquetDailyFeed(preloaded={_SYM: _frame(rows)}, **kwargs)


# ----------------------------------------------------------------------
# ① 单日单 symbol 读回：字段精确
# ----------------------------------------------------------------------

def test_single_day_bar_fields_exact():
    feed = _feed([_row(_D1, close=10.5, preclose=10.0, open_=10.1,
                       high=10.8, low=9.9, volume=1234500.0,
                       amount=12876543.21)])
    bars = feed.get_bars([_SYM], _D1)

    assert set(bars) == {_SYM}
    bar = bars[_SYM]
    assert isinstance(bar, Bar)
    # date 必须是 datetime.date（⛔ 不是 Timestamp / 字符串）
    assert bar.date == _D1
    assert type(bar.date) is date
    assert bar.symbol == _SYM
    # 数值全 Decimal 且**精确**（Decimal(str(v)) 中转，⛔ 无 float 尾差）
    for field_name in ("open", "high", "low", "close", "preclose",
                       "volume", "amount"):
        assert isinstance(getattr(bar, field_name), Decimal), field_name
    assert bar.open == D("10.1")
    assert bar.high == D("10.8")
    assert bar.low == D("9.9")
    assert bar.close == D("10.5")
    assert bar.preclose == D("10.0")
    assert bar.volume == D("1234500")
    assert bar.amount == D("12876543.21")
    assert bar.adjust_mode == "hfq"
    # 派生列：+5% 不触板（主板 ±10%）
    assert bar.limit_up is False
    assert bar.limit_down is False
    assert bar.exdiv is False
    assert bar.is_st is False
    # bool 必须是 Python bool（⛔ 不是 numpy.bool_）
    for flag in ("limit_up", "limit_down", "exdiv", "is_st"):
        assert type(getattr(bar, flag)) is bool, flag


def test_decimal_no_float_tail():
    """0.1 这类二进制不可表示的值必须精确落 Decimal（尾差防线）。"""
    feed = _feed([_row(_D1, close=0.1, preclose=0.1, open_=0.1,
                       high=0.1, low=0.1)])
    bar = feed.get_bars([_SYM], _D1)[_SYM]
    assert bar.close == D("0.1")
    assert str(bar.close) == "0.1"
    assert bar.close != D(0.1)          # ⛔ Decimal(float) 的反面教材


def test_current_date_tracks_last_get_bars():
    feed = _feed([_row(_D1), _row(_D2)])
    with pytest.raises(FeedError):
        feed.current_date()             # ⛔ 未取过 bar 不静默返回今天
    feed.get_bars([_SYM], _D2)
    assert feed.current_date() == _D2


def test_protocol_conformance():
    """``ParquetDailyFeed`` 满足 ``DataFeed`` 协议（方法齐备）。"""
    assert isinstance(_feed([_row(_D1)]), DataFeed)


# ----------------------------------------------------------------------
# ② 停牌 = 键缺席
# ----------------------------------------------------------------------

def test_suspended_day_symbol_absent_not_none():
    """该 symbol 当日**没有行** = 停牌 ⇒ 不在返回 dict（⛔ 不是 None 值）。"""
    feed = _feed([_row(_D1), _row(_D3)])        # 缺 _D2
    bars = feed.get_bars([_SYM], _D2)
    assert bars == {}
    assert _SYM not in bars                    # ⛔ 不许 bars[_SYM] is None
    # 前后两天都在
    assert _SYM in feed.get_bars([_SYM], _D1)
    assert _SYM in feed.get_bars([_SYM], _D3)


def test_unknown_symbol_absent():
    """没有分区/没有预加载的 symbol 同样缺席（未上市 / 已退市），⛔ 不 raise。"""
    feed = _feed([_row(_D1)])
    bars = feed.get_bars([_SYM, "sz.000001"], _D1)
    assert set(bars) == {_SYM}


def test_tradestatus_zero_rows_filtered_as_suspended():
    """R1 纵深防御：``tradestatus != '1'`` 的平推脏行被滤 ⇒ 当日缺席 + 计数。"""
    feed = _feed([
        _row(_D1),
        _row(_D2, close=10.5, preclose=10.5, tradestatus="0"),
    ])
    assert feed.get_bars([_SYM], _D2) == {}
    assert feed.suspended_rows == 1


# ----------------------------------------------------------------------
# ③ 涨跌停注入
# ----------------------------------------------------------------------

def test_limit_up_injected_main_board():
    """60 开头主板 +10%（11.0 / 10.0）⇒ ``limit_up=True``。"""
    bar = _feed([_row(_D1, close=11.0, preclose=10.0)]).get_bars([_SYM], _D1)[_SYM]
    assert bar.limit_up is True
    assert bar.limit_down is False


def test_limit_down_injected_main_board():
    """60 开头主板 -10%（9.0 / 10.0）⇒ ``limit_down=True``。"""
    bar = _feed([_row(_D1, close=9.0, preclose=10.0)]).get_bars([_SYM], _D1)[_SYM]
    assert bar.limit_down is True
    assert bar.limit_up is False


def test_no_limit_flags_when_within_band():
    bar = _feed([_row(_D1, close=10.9, preclose=10.0)]).get_bars([_SYM], _D1)[_SYM]
    assert bar.limit_up is False
    assert bar.limit_down is False


def test_limit_config_override_honored():
    """``LimitFlagsConfig`` 覆盖（FR-EXT-6）经 feed 生效：主板档改 5% ⇒ +6% 触板。"""
    rows = [_row(_D1, close=10.6, preclose=10.0)]
    assert _feed(rows).get_bars([_SYM], _D1)[_SYM].limit_up is False
    tight = _feed(rows, limit_config=LimitFlagsConfig(main_pct=5.0))
    assert tight.get_bars([_SYM], _D1)[_SYM].limit_up is True


def test_missing_preclose_column_flags_false():
    """契约 §6 防御分支：帧缺 ``preclose`` 列 ⇒ 双 False（触板不可判定）。"""
    frame = _frame([_row(_D1, close=11.0, preclose=10.0)]).drop(columns=["preclose"])
    bar = ParquetDailyFeed(preloaded={_SYM: frame}).get_bars([_SYM], _D1)[_SYM]
    assert bar.limit_up is False
    assert bar.limit_down is False
    assert bar.preclose == D("0")       # 缺失 → 0，⛔ 不是 Decimal("NaN")


# ----------------------------------------------------------------------
# ④ ST：is_st 透传 + 5% 档
# ----------------------------------------------------------------------

def test_st_flag_and_five_pct_band():
    """``isST='1'`` ⇒ ``is_st=True``；+6% 在 ST 档（5%）**触板**、主板档不触板。"""
    st_bar = _feed(
        [_row(_D1, close=10.6, preclose=10.0, is_st="1")]
    ).get_bars([_SYM], _D1)[_SYM]
    assert st_bar.is_st is True
    assert st_bar.limit_up is True              # 走 st_pct=5%

    normal_bar = _feed(
        [_row(_D1, close=10.6, preclose=10.0, is_st="0")]
    ).get_bars([_SYM], _D1)[_SYM]
    assert normal_bar.is_st is False
    assert normal_bar.limit_up is False         # 走 main_pct=10%


def test_st_limit_down_five_pct():
    bar = _feed(
        [_row(_D1, close=9.5, preclose=10.0, is_st="1")]
    ).get_bars([_SYM], _D1)[_SYM]
    assert bar.is_st is True
    assert bar.limit_down is True


# ----------------------------------------------------------------------
# ⑤ 除权预注入
# ----------------------------------------------------------------------

def test_exdiv_preinjected_marks_only_event_day():
    events = pd.DataFrame(
        [{"date": _D2.isoformat(), "exdiv": True,
          "adjust_factor": True, "dividend": False}],
        columns=["date", "exdiv", "adjust_factor", "dividend"],
    )
    feed = ParquetDailyFeed(
        preloaded={_SYM: _frame([_row(_D1), _row(_D2), _row(_D3)])},
        exdiv_events={_SYM: events},
    )
    assert feed.get_bars([_SYM], _D1)[_SYM].exdiv is False
    assert feed.get_bars([_SYM], _D2)[_SYM].exdiv is True
    assert feed.get_bars([_SYM], _D3)[_SYM].exdiv is False


def test_exdiv_fetch_fn_injected_stays_offline():
    """给了 ``exdiv_fetch_fn`` 才取数，且取数器可注入 ⇒ 离线可测（⛔ 不碰 baostock）。"""
    from finai.sources.base import OK

    class _Res:
        state = OK

        def __init__(self, frame):
            self.frame = frame
            self.detail = ""

    calls: list[str] = []

    def _fake_fetch(kind, **kwargs):
        calls.append(kind)
        if kind == "adjust_factor":
            return _Res(pd.DataFrame({"dividOperateDate": [_D2.isoformat()]}))
        return _Res(pd.DataFrame({"dividOperateDate": []}))

    feed = ParquetDailyFeed(
        preloaded={_SYM: _frame([_row(_D1), _row(_D2), _row(_D3)])},
        exdiv_fetch_fn=_fake_fetch,
    )
    assert feed.get_bars([_SYM], _D2)[_SYM].exdiv is True
    assert feed.get_bars([_SYM], _D1)[_SYM].exdiv is False
    assert "adjust_factor" in calls
    before = len(calls)
    feed.clear_cache()
    feed.get_bars([_SYM], _D3)          # 事件已缓存到 exdiv_events ⇒ 不再取数
    assert len(calls) == before


def test_exdiv_absent_defaults_false_without_network(caplog):
    """未注入事件、未给取数器 ⇒ 全 False + warning（⛔ 绝不在热路径打网）。"""
    with caplog.at_level("WARNING"):
        bar = _feed([_row(_D1)]).get_bars([_SYM], _D1)[_SYM]
    assert bar.exdiv is False
    assert any("除权" in rec.getMessage() for rec in caplog.records)


# ----------------------------------------------------------------------
# ⑥ 跨年分区拼接（真实 parquet）
# ----------------------------------------------------------------------

def _write_parts(root: Path, rows_by_year: dict[int, list[dict]]) -> None:
    """用生产落盘函数写分区，保证测试读的是真实布局/真实 dtype。"""
    for rows in rows_by_year.values():
        write_daily_bars_partitioned(_frame(rows), symbol=_SYM, root=root)


def test_cross_year_partitions_concat(tmp_path):
    """2024 + 2025 各一行，跨年区间内两日都命中（契约 §6-5）。"""
    d2024 = date(2024, 12, 31)
    d2025 = date(2025, 1, 2)
    root = tmp_path / "daily_bars"
    _write_parts(root, {
        2024: [_row(d2024, close=20.0, preclose=19.5)],
        2025: [_row(d2025, close=21.0, preclose=20.0)],
    })
    assert (root / _SYM / "2024.parquet").exists()
    assert (root / _SYM / "2025.parquet").exists()

    feed = ParquetDailyFeed(root=root)
    bar_a = feed.get_bars([_SYM], d2024)[_SYM]
    bar_b = feed.get_bars([_SYM], d2025)[_SYM]
    assert bar_a.date == d2024 and bar_a.close == D("20.0")
    assert bar_b.date == d2025 and bar_b.close == D("21.0")
    # 中间的元旦无行 ⇒ 缺席
    assert feed.get_bars([_SYM], date(2025, 1, 1)) == {}


def test_parquet_partition_cached_once(tmp_path, monkeypatch):
    """按 (symbol, year) 缓存：多日复用只读盘一次（契约 §6-4）。"""
    root = tmp_path / "daily_bars"
    _write_parts(root, {2024: [_row(_D1), _row(_D2), _row(_D3)]})

    calls: list[Path] = []
    real_read = pd.read_parquet

    def _counting_read(path, *args, **kwargs):
        calls.append(Path(path))
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", _counting_read)
    feed = ParquetDailyFeed(root=root)
    for d in (_D1, _D2, _D3, _D1):
        assert _SYM in feed.get_bars([_SYM], d)
    assert len(calls) == 1

    feed.clear_cache()
    feed.get_bars([_SYM], _D1)
    assert len(calls) == 2


def test_parquet_roundtrip_types(tmp_path):
    """落盘→读回：``date`` 列是 ``datetime.date``，Bar 字段口径不变。"""
    root = tmp_path / "daily_bars"
    _write_parts(root, {2024: [_row(_D1, close=10.53, preclose=10.0,
                                   is_st="1", adjust_mode="qfq")]})
    stored = pd.read_parquet(root / _SYM / "2024.parquet", engine="pyarrow")
    assert type(stored["date"].iloc[0]) is date

    bar = ParquetDailyFeed(root=root).get_bars([_SYM], _D1)[_SYM]
    assert bar.close == D("10.53")
    assert bar.is_st is True
    assert bar.adjust_mode == "qfq"


# ----------------------------------------------------------------------
# ⑦ get_trading_dates
# ----------------------------------------------------------------------

def test_get_trading_dates_raises_without_calendar():
    """⛔ 未注入日历不静默打网（契约 §6）。"""
    feed = _feed([_row(_D1)])
    with pytest.raises(CalendarNotInjectedError):
        feed.get_trading_dates(_D1, _D3)


def test_get_trading_dates_delegates_to_injected_calendar():
    seen: list[tuple[date, date]] = []

    def _calendar(start: date, end: date) -> list[date]:
        seen.append((start, end))
        # 故意乱序 + 越界 + 重复：feed 须去重、裁边界、升序
        return [_D3, _D1, _D2, _D1, date(2024, 2, 28), date(2024, 3, 6)]

    feed = _feed([_row(_D1)], trade_calendar=_calendar)
    assert feed.get_trading_dates(_D1, _D3) == [_D1, _D2, _D3]
    assert seen == [(_D1, _D3)]


def test_get_trading_dates_accepts_string_dates_from_calendar():
    feed = _feed(
        [_row(_D1)],
        trade_calendar=lambda s, e: ["2024-03-01", "2024-03-04", "2024-03-05"],
    )
    assert feed.get_trading_dates(_D1, _D3) == [_D1, _D2, _D3]


def test_get_trading_dates_rejects_inverted_range():
    feed = _feed([_row(_D1)], trade_calendar=lambda s, e: [])
    with pytest.raises(FeedError):
        feed.get_trading_dates(_D3, _D1)


# ----------------------------------------------------------------------
# 多 symbol 混合场景（引擎实际调用形态）
# ----------------------------------------------------------------------

def test_multi_symbol_mixed_suspension_and_limits():
    other = "sz.300750"          # 创业板 ±20%
    feed = ParquetDailyFeed(preloaded={
        _SYM: _frame([_row(_D1, close=11.0, preclose=10.0)]),          # 主板涨停
        other: _frame([_row(_D1, close=11.0, preclose=10.0, code=other)]),
    })
    bars = feed.get_bars([_SYM, other, "sh.600001"], _D1)
    assert set(bars) == {_SYM, other}          # 第三个缺席
    assert bars[_SYM].limit_up is True         # 10% 档触板
    assert bars[other].limit_up is False       # 20% 档未触板
    assert bars[other].symbol == other
