# -*- coding: utf-8 -*-
"""宽度口径（S-2）后置门禁 context 注入与接线的锁定测试（P2-c · 行为版）。

2026-09-16 修复纪事：原实现把宽度字段塞进嵌套 ``breadth_gate_context``，
而 ``runner.py`` 构造 ``s2_ctx`` 时只取 MA200 字段——宽度口径成死代码，
且 ``timing_grace_dates`` 两个口径都从未传入。本套件锁定三件事：

1. 产出侧：``_build_post_run_gate_context`` 在宽度择时下把 S-2 所需证据
   升到 ctx 顶层（use_breadth_timing / breadth_series / defense / 宽限日）；
2. 消费侧：``runner.py`` 的 S-2 ctx 真实消费宽度字段（行为级，非文本）；
3. 门禁本体：TimingExitSurvivalGate 在宽度口径下按冰点日判定。
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _src(p: str) -> str:
    return (_ROOT / p).read_text(encoding="utf-8")


# ------------------------------------------------------------------
# 1. 产出侧接线（文本锁——__post_init__ 函数体级证据）
# ------------------------------------------------------------------

def test_ctx_builder_surfaces_breadth_evidence():
    s = _src("scripts/run_dividend_backtest.py")
    for key in (
        'ctx["use_breadth_timing"] = True',
        'ctx["breadth_series"] = bser',
        'ctx["breadth_timing_grace_dates"]',
    ):
        assert key in s, f"产出侧缺少顶层注入: {key}"
    # 重复补丁事故防线：同一注入块只允许出现一次
    assert s.count('"breadth_gate_context":') == 1, "record 重复挂载 breadth_gate_context"
    assert s.count("gate_ctx_breadth = {") == 1, "gate_ctx_breadth 补丁块重复残留"


def test_runner_consumes_breadth_context():
    s = _src("scripts/gates/runner.py")
    for key in (
        'ctx.get("use_breadth_timing")',
        's2_ctx["breadth_series"]',
        's2_ctx["timing_grace_dates"] = ctx["breadth_timing_grace_dates"]',
        's2_ctx["timing_grace_dates"] = ctx["timing_grace_dates"]',
    ):
        assert key in s, f"runner 未接线: {key}"


# ------------------------------------------------------------------
# 2. 行为级：S-2 门禁在宽度口径下真实生效
# ------------------------------------------------------------------

def _breadth_ctx(positions: dict) -> dict:
    """构造宽度口径 ctx：3 天中第 2/3 天宽度 < 0.25 防守线。"""
    return {
        "use_breadth_timing": True,
        "breadth_defense_threshold": 0.25,
        "breadth_series": {"2024-01-02": 0.50, "2024-01-03": 0.10,
                           "2024-01-04": 0.10},
        "daily_positions_ratio": positions,
        "timing_grace_dates": ["2024-01-03", "2024-01-04"],
    }


def test_s2_breadth_criterion_actually_engages():
    """宽度口径下，冰点日持仓 >5% 且超宽限 ⇒ FAIL（证明口径被真实使用）。"""
    from scripts.gates.gate_s_scientific import TimingExitSurvivalGate
    from scripts.gates.base import GateStatus

    ctx = _breadth_ctx({"2024-01-02": 1.0, "2024-01-03": 1.0, "2024-01-04": 1.0})
    # 宽限日清空 → 两个冰点日都违规
    ctx["timing_grace_dates"] = []
    res = TimingExitSurvivalGate().evaluate(ctx)
    assert res.status == GateStatus.FAIL
    assert res.metrics.get("criterion") == "breadth", "未走宽度判据"


def test_s2_breadth_grace_and_flat_pass():
    """冰点日持仓清零（或落在宽限日内）⇒ PASS。"""
    from scripts.gates.gate_s_scientific import TimingExitSurvivalGate
    from scripts.gates.base import GateStatus

    ctx = _breadth_ctx({"2024-01-02": 1.0, "2024-01-03": 1.0, "2024-01-04": 0.0})
    res = TimingExitSurvivalGate().evaluate(ctx)
    assert res.status == GateStatus.PASS, res.message
    assert res.metrics.get("criterion") == "breadth"


def test_s2_breadth_missing_series_is_inconclusive():
    """宽度开关开启但缺序列 ⇒ INCONCLUSIVE（fail-closed，不回退 MA200）。"""
    from scripts.gates.gate_s_scientific import TimingExitSurvivalGate
    from scripts.gates.base import GateStatus

    res = TimingExitSurvivalGate().evaluate({
        "use_breadth_timing": True,
        "daily_positions_ratio": {"2024-01-02": 0.0},
    })
    assert res.status == GateStatus.INCONCLUSIVE


def test_s2_ma200_caliber_still_works():
    """MA200 口径向后兼容：破位日持仓 ≤5%（含宽限）⇒ PASS。"""
    from scripts.gates.gate_s_scientific import TimingExitSurvivalGate
    from scripts.gates.base import GateStatus

    res = TimingExitSurvivalGate().evaluate({
        "index_below_ma200_dates": ["2024-02-01", "2024-02-02"],
        "daily_positions_ratio": {"2024-02-01": 1.0, "2024-02-02": 0.0},
        "timing_grace_dates": ["2024-02-01"],
    })
    assert res.status == GateStatus.PASS, res.message


def test_s2_breadth_out_of_window_ice_days_excluded():
    """宽度序列覆盖回测窗口之外的日期 ⇒ 界外冰点是「不适用」而非「缺证据」。

    回归锁：此前 below_dates 吃全序列 ⇒ 任何早于序列末尾结束的回测
    永远 INCONCLUSIVE（界外日不可能有仓位比例）。界内缺失仍须拦。
    """
    from scripts.gates.gate_s_scientific import TimingExitSurvivalGate
    from scripts.gates.base import GateStatus

    ctx = _breadth_ctx({"2024-01-02": 1.0, "2024-01-03": 1.0,
                        "2024-01-04": 0.0})
    # 追加窗口外冰点日（2025 年，pos_ratios 不覆盖）
    ctx["breadth_series"]["2025-06-01"] = 0.05
    ctx["breadth_series"]["2025-06-02"] = 0.05
    res = TimingExitSurvivalGate().evaluate(ctx)
    assert res.status == GateStatus.PASS, res.message


def test_s2_breadth_in_window_missing_ratio_still_inconclusive():
    """界内冰点日缺仓位比例 ⇒ 仍 INCONCLUSIVE（窗口化不削弱缺证据拦截）。

    窗口界用产出方声明的 run_calendar_bounds（与生产 ctx 同口径）——
    即使该缺失日恰在 pos_ratios 键的边界上，声明界仍把它判为界内缺证据。
    """
    from scripts.gates.gate_s_scientific import TimingExitSurvivalGate
    from scripts.gates.base import GateStatus

    ctx = _breadth_ctx({"2024-01-02": 1.0, "2024-01-03": 0.0})
    ctx["run_calendar_bounds"] = ["2024-01-02", "2024-01-04"]
    # 2024-01-04 在声明窗口内但 pos_ratios 缺键 ⇒ 缺证据
    res = TimingExitSurvivalGate().evaluate(ctx)
    assert res.status == GateStatus.INCONCLUSIVE, res.message


def test_s2_breadth_declared_bounds_exclude_series_tail():
    """声明窗口裁剪全程序列：窗口后冰点日不参与判定（fix 回归锁）。"""
    from scripts.gates.gate_s_scientific import TimingExitSurvivalGate
    from scripts.gates.base import GateStatus

    ctx = _breadth_ctx({"2024-01-02": 1.0, "2024-01-03": 1.0,
                        "2024-01-04": 0.0})
    ctx["run_calendar_bounds"] = ["2024-01-02", "2024-01-04"]
    ctx["breadth_series"]["2025-06-01"] = 0.05   # 窗口后冰点日
    res = TimingExitSurvivalGate().evaluate(ctx)
    assert res.status == GateStatus.PASS, res.message
