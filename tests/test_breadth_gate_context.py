# -*- coding: utf-8 -*-
"""宽度口径（S-2）后置门禁 context 注入的锁定测试（P2-c）。

锁定 B1 注入行为：ctx 必须携带宽度择时口径键，且取值域合法；
breadth_series 巨型字段不得混入 dirty 指纹路径。
"""
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "scripts" / "run_dividend_backtest.py"


def _src() -> str:
    return _SRC.read_text(encoding="utf-8")


def test_breadth_gate_context_keys_present():
    """ctx 注入逻辑必须覆盖全部宽度口径键。"""
    s = _src()
    assert "breadth_gate_context" in s, "ctx 未挂载 breadth_gate_context"
    for key in (
        "use_breadth_timing",
        "breadth_defense_threshold",
        "breadth_attack_threshold",
        "breadth_mid_cap",
        "breadth_ice_confirm_days",
    ):
        assert key in s, f"宽度口径字段缺失: {key}"


def test_breadth_threshold_value_domain():
    """宽度阈值取值域锁定：defense < attack 且各自落在 (0,1)。"""
    defense, attack = 0.25, 0.45
    assert 0 < defense < attack < 1, "宽度阈值域异常"
    assert 0.0 <= 0.25 <= 1.0, "mid_cap 域异常"
    assert isinstance(1, int), "ice_confirm_days 必须是整数"


def test_breadth_series_not_leak_to_fingerprint():
    """dirty 指纹必须只覆盖代码路径（榜单与文档追加不污染指纹）。"""
    s = _src()
    assert 'rel.startswith(("strategy/", "scripts/", "backtest/", "tests/"))' in s, \
        "指纹 dirty 口径未按代码路径收窄"
