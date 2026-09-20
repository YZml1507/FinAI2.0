# -*- coding: utf-8 -*-
"""scripts/lab/run_experiment.py 单测——登记路径纯函数（_compact_override /
_parse_overrides）。登记行不可膨胀、caster fail-closed 语义须钉死。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.lab import run_experiment as m


class TestCompactOverride:
    """大型序列映射折叠为指纹摘要，标量照常 str()。"""

    def test_large_series_folds_to_digest(self):
        series = {f"2020-01-{i:02d}": Decimal("0.5") for i in range(1, 29)}
        out = m._compact_override(series)
        assert out.startswith("<series:28 entries sha256=")
        assert "2020-01" not in out  # 不展开原始内容

    def test_digest_deterministic(self):
        a = {"x": Decimal("1"), "y": Decimal("2")}
        b = {"y": Decimal("2"), "x": Decimal("1")}  # 乱序同内容
        assert m._compact_override(a) == m._compact_override(b)

    def test_digest_sensitive_to_content(self):
        a = {"x": Decimal("1")}
        b = {"x": Decimal("2")}
        assert m._compact_override(a) != m._compact_override(b)

    def test_scalars_passthrough(self):
        assert m._compact_override(Decimal("0.5")) == "0.5"
        assert m._compact_override(15) == "15"
        assert m._compact_override("x") == "x"
        assert m._compact_override(True) == "True"


class TestParseOverrides:
    """--set 解析：caster 生效、未知键透传（由下游 dataclass 校验）。"""

    def test_known_keys_cast(self):
        ov = m._parse_overrides(["dv_skip_top=15", "low_vol_keep_pct=0.5"])
        assert ov["dv_skip_top"] == 15 and isinstance(ov["dv_skip_top"], int)
        assert ov["low_vol_keep_pct"] == Decimal("0.5")

    def test_unknown_key_fail_closed(self):
        # 未登记键 fail-closed：SystemExit（防 --set 笔误静默落地）
        with pytest.raises(SystemExit, match="不支持的实验参数"):
            m._parse_overrides(["nonexistent_knob=1"])

    def test_malformed_pair_raises(self):
        with pytest.raises(SystemExit, match="参数格式错误"):
            m._parse_overrides(["no_equals_sign"])
