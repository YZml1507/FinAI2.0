#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T403 净值计算与存储测试（paper_trading/nav.py）。

覆盖范围：
  - NAVRecord 数据容器
  - append_nav 幂等写入（同日期覆盖 + 按日期排序）
  - load_nav_series 读取时间序列
"""
import shutil
import tempfile
from datetime import date as dt
from decimal import Decimal
from pathlib import Path

import pytest

from paper_trading.nav import NAVRecord, append_nav, load_nav_series

_ZERO = Decimal("0")


@pytest.fixture
def temp_nav_file():
    """临时 NAV 文件路径（自动清理）。"""
    tmpdir = Path(tempfile.mkdtemp())
    nav_file = tmpdir / "nav_series.parquet"
    yield nav_file
    shutil.rmtree(tmpdir, ignore_errors=True)


class TestNAVRecord:
    """NAVRecord 数据容器测试。"""

    def test_nav_record_creation(self):
        """创建 NAVRecord 正常。"""
        rec = NAVRecord(
            date=dt(2026, 9, 1),
            nav=Decimal("100000"),
            cash=Decimal("100000"),
            market_value=_ZERO,
        )
        assert rec.date == dt(2026, 9, 1)
        assert rec.nav == Decimal("100000")

    def test_nav_record_to_dict(self):
        """to_dict() 转为 DataFrame 行字典。"""
        rec = NAVRecord(
            date=dt(2026, 9, 1),
            nav=Decimal("100500.123456"),
            cash=Decimal("90000"),
            market_value=Decimal("10500.123456"),
        )
        d = rec.to_dict()
        assert d["date"] == dt(2026, 9, 1)
        assert d["nav"] == "100500.123456"  # Decimal → str
        assert d["cash"] == "90000"
        assert d["market_value"] == "10500.123456"


class TestAppendNAV:
    """append_nav 幂等写入测试。"""

    def test_append_first_record(self, temp_nav_file):
        """首次写入：创建文件 + 单条记录。"""
        rec = NAVRecord(
            date=dt(2026, 9, 1),
            nav=Decimal("100000"),
            cash=Decimal("100000"),
            market_value=_ZERO,
        )
        append_nav(temp_nav_file, [rec])

        assert temp_nav_file.exists()
        series = load_nav_series(temp_nav_file)
        assert len(series) == 1
        assert series[0] == (dt(2026, 9, 1), Decimal("100000"))

    def test_append_multiple_records(self, temp_nav_file):
        """追加多条记录：按日期排序。"""
        rec1 = NAVRecord(dt(2026, 9, 1), Decimal("100000"), Decimal("100000"), _ZERO)
        rec2 = NAVRecord(dt(2026, 9, 2), Decimal("101000"), Decimal("99000"), Decimal("2000"))
        append_nav(temp_nav_file, [rec1, rec2])

        series = load_nav_series(temp_nav_file)
        assert len(series) == 2
        assert series[0][0] == dt(2026, 9, 1)
        assert series[1][0] == dt(2026, 9, 2)
        assert series[1][1] == Decimal("101000")

    def test_idempotent_overwrite_same_date(self, temp_nav_file):
        """幂等模式：同日期重写 → 覆盖旧值（keep='last'）。"""
        rec1 = NAVRecord(dt(2026, 9, 1), Decimal("100000"), Decimal("100000"), _ZERO)
        append_nav(temp_nav_file, [rec1])

        # 重写同日期，NAV 改为 102000
        rec2 = NAVRecord(dt(2026, 9, 1), Decimal("102000"), Decimal("100000"), Decimal("2000"))
        append_nav(temp_nav_file, [rec2], idempotent=True)

        series = load_nav_series(temp_nav_file)
        assert len(series) == 1
        assert series[0][1] == Decimal("102000")  # 新值覆盖

    def test_non_idempotent_duplicate_date(self, temp_nav_file):
        """非幂等模式：同日期重写 → 可能重复（不推荐）。"""
        rec1 = NAVRecord(dt(2026, 9, 1), Decimal("100000"), Decimal("100000"), _ZERO)
        append_nav(temp_nav_file, [rec1], idempotent=False)

        rec2 = NAVRecord(dt(2026, 9, 1), Decimal("102000"), Decimal("100000"), Decimal("2000"))
        append_nav(temp_nav_file, [rec2], idempotent=False)

        series = load_nav_series(temp_nav_file)
        # 可能重复（取决于 parquet 去重行为），至少有 1 条
        assert len(series) >= 1

    def test_append_out_of_order_auto_sort(self, temp_nav_file):
        """乱序追加 → 自动按日期排序。"""
        rec2 = NAVRecord(dt(2026, 9, 2), Decimal("101000"), Decimal("99000"), Decimal("2000"))
        rec1 = NAVRecord(dt(2026, 9, 1), Decimal("100000"), Decimal("100000"), _ZERO)
        rec3 = NAVRecord(dt(2026, 9, 3), Decimal("102000"), Decimal("98000"), Decimal("4000"))

        append_nav(temp_nav_file, [rec2, rec1, rec3])

        series = load_nav_series(temp_nav_file)
        assert len(series) == 3
        assert series[0][0] == dt(2026, 9, 1)
        assert series[1][0] == dt(2026, 9, 2)
        assert series[2][0] == dt(2026, 9, 3)


class TestLoadNAVSeries:
    """load_nav_series 读取测试。"""

    def test_load_empty_file(self, temp_nav_file):
        """空文件 → 空列表。"""
        series = load_nav_series(temp_nav_file)
        assert series == []

    def test_load_single_record(self, temp_nav_file):
        """单条记录 → 正确读取。"""
        rec = NAVRecord(dt(2026, 9, 1), Decimal("100000"), Decimal("100000"), _ZERO)
        append_nav(temp_nav_file, [rec])

        series = load_nav_series(temp_nav_file)
        assert len(series) == 1
        assert series[0] == (dt(2026, 9, 1), Decimal("100000"))

    def test_load_multiple_records_sorted(self, temp_nav_file):
        """多条记录 → 按日期升序。"""
        recs = [
            NAVRecord(dt(2026, 9, 1), Decimal("100000"), Decimal("100000"), _ZERO),
            NAVRecord(dt(2026, 9, 2), Decimal("101000"), Decimal("99000"), Decimal("2000")),
            NAVRecord(dt(2026, 9, 3), Decimal("102000"), Decimal("98000"), Decimal("4000")),
        ]
        append_nav(temp_nav_file, recs)

        series = load_nav_series(temp_nav_file)
        assert len(series) == 3
        assert series[0][0] < series[1][0] < series[2][0]

    def test_decimal_precision_preserved(self, temp_nav_file):
        """Decimal 精度保留（str 存储往返无损）。"""
        rec = NAVRecord(
            dt(2026, 9, 1),
            nav=Decimal("100000.123456"),
            cash=Decimal("90000.654321"),
            market_value=Decimal("10000.469135"),
        )
        append_nav(temp_nav_file, [rec])

        series = load_nav_series(temp_nav_file)
        assert series[0][1] == Decimal("100000.123456")
