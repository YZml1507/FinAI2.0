#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M6 路径A 影子复算 —— 判据化双向测试（⛔ 离线、零网络、零回测）。

覆盖（每组均由**生产纯函数实测**得出预期，⛔ 不读文档表格）：

* (a) H5 权重离散矩阵（等权 / 3:2:2:1:1 / 10:1:1:1:1）⇒ 实建只数 + 留存现金 + 丢弃原因；
* (b) H5b 价位真空带边界对撞（150/151/199/200/301；`planned == m` 的严格小于语义）；
* (c) 精确式 vs 生产函数**暴力等价**（`[20,300]` × base 网格）+ 已证否闭式的**反例**；
* (d) H5c 死带随 base 退化（全失效实测 + NAV 表占比 + 单调性集合包含）；
* (e) ㊶ `default_positions × target_count` 双口径（含 `(8,8)` ⇒ 空计划）；
* (f) 端点括号（只读签名产物 ⇒ 终值/谷值死带占比区间）。

另有两条纪律测试：生产默认配置基线锚定 + 落盘产物**确定性且无 float**。

运行：``cd D:/Projects/FinAI2.0 && py -3.11 -m pytest tests/test_m6_shadow_recompute.py \
-p no:ddtrace -p no:ddtrace.pytest_bdd -q``
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from scripts import m6_shadow_recompute as m6
from strategy.portfolio import PortfolioConfig, plan_positions, select_targets

D = Decimal
_ZERO = D("0")

# 基线常量（与生产默认一致；下方 TestBaselineAnchoring 会显式校验）
_NAV = D("150000")
_M = D("20000")
_LO, _HI = 20, 300
_N_PRICES = 281


def _bars(n: int, close: str = "10.00") -> dict[str, object]:
    return {f"s{i}": m6.make_bar(close, symbol=f"s{i}") for i in range(n)}


@pytest.fixture(scope="module")
def signed_metrics() -> dict[str, object]:
    """只读签名产物的 ``metrics``（⛔ 不修改该文件）。"""
    doc = json.loads((m6.ROOT / m6.ARTIFACT_REL).read_text(encoding="utf-8"))
    return doc["metrics"]


# ----------------------------------------------------------------------
class TestBaselineAnchoring:
    """生产默认配置 = 本测试全部预期的基线（改了默认值 ⇒ 立刻红，⛔ 不静默漂移）。"""

    def test_production_defaults_are_the_baseline(self) -> None:
        cfg = PortfolioConfig()
        assert cfg.min_position_value == _M
        assert cfg.lot_size == 100
        assert cfg.max_price == D("300.0")
        assert cfg.min_daily_amount == D("50000000")
        assert cfg.max_participation_rate == D("0.05")
        assert cfg.target_count == 5 and cfg.hard_limit == 10


# ----------------------------------------------------------------------
class TestH5WeightDispersion:
    """(a) 权重离散 ⇒ 低权重票被 min_position_value 丢弃且不重新分配。"""

    def test_equal_weight_builds_all_five(self) -> None:
        syms = [f"s{i}" for i in range(5)]
        plan, dropped = plan_positions(syms, _NAV, _bars(5), PortfolioConfig(), weights=None)
        assert len(plan) == 5 and dropped == ()
        assert sum(plan.values()) == _NAV
        assert _NAV - sum(plan.values()) == _ZERO          # 留存现金 = 0

    @pytest.mark.parametrize(
        "weights, n_built, retained, n_dropped",
        [
            ({"s0": D("3"), "s1": D("2"), "s2": D("2"), "s3": D("1"), "s4": D("1")},
             3, D("34000"), 2),
            ({"s0": D("10"), "s1": D("1"), "s2": D("1"), "s3": D("1"), "s4": D("1")},
             1, D("43000"), 4),
        ],
    )
    def test_weighted_dispersion_drops_low_weight_tickets(
        self, weights: dict[str, Decimal], n_built: int, retained: Decimal, n_dropped: int,
    ) -> None:
        syms = [f"s{i}" for i in range(5)]
        plan, dropped = plan_positions(syms, _NAV, _bars(5), PortfolioConfig(), weights=weights)
        assert len(plan) == n_built
        assert len(dropped) == n_dropped
        assert _NAV - sum(plan.values()) == retained
        assert {r for _, r in dropped} == {"低于单票下限"}   # 全部因 H5（非流动性/非高价）
        # 丢弃额度**不**被重新分配（合计 < NAV）——即断崖式丢弃
        assert sum(plan.values()) < _NAV

    def test_script_matrix_matches_predecessor_table(self) -> None:
        """脚本产出的 H5 矩阵与前任表（5只/0；3只/34,000；1只/43,000）逐项对照。"""
        rows = {r["scenario"]: r for r in m6.recompute_h5()["rows"]}
        assert rows["等权 1:1:1:1:1"]["n_built"] == 5
        assert rows["等权 1:1:1:1:1"]["retained_cash"] == "0.00"
        assert rows["加权 3:2:2:1:1"]["n_built"] == 3
        assert rows["加权 3:2:2:1:1"]["retained_cash"] == "34000.00"
        assert rows["加权 10:1:1:1:1"]["n_built"] == 1
        assert rows["加权 10:1:1:1:1"]["retained_cash"] == "43000.00"


# ----------------------------------------------------------------------
class TestH5bDeadBandBoundary:
    """(b) 价位真空带的边界（逐点断言，全部由生产函数实测）。"""

    @pytest.mark.parametrize(
        "base, price, built, reason",
        [
            (D("30000"), D("150"), True, ""),
            (D("30000"), D("151"), False, "低于单票下限"),
            (D("30000"), D("199"), False, "低于单票下限"),
            (D("30000"), D("200"), True, ""),      # planned == m ⇒ 通过（严格 <）
            (D("30000"), D("301"), False, "超过价格上限"),  # ⛔ 原因不同，勿与 H5b 混
        ],
    )
    def test_boundary(self, base: Decimal, price: Decimal, built: bool, reason: str) -> None:
        dead, got_reason = m6.production_dead(price, base)
        assert (not dead) is built
        assert got_reason == reason

    def test_price_301_is_not_the_same_failure_mode(self) -> None:
        """301 走的是 F10c（max_price），与 H5b 的整手化跌破下限**不同因**。"""
        _, r_301 = m6.production_dead(D("301"), D("30000"))
        _, r_199 = m6.production_dead(D("199"), D("30000"))
        assert r_301 == "超过价格上限" and r_199 == "低于单票下限" and r_301 != r_199

    @pytest.mark.parametrize("base", [D("21684"), D("21684.228")])
    def test_planned_exactly_min_value_is_built(self, base: Decimal) -> None:
        """`planned < m` 是**严格小于**：planned 恰 = 20000 时建成。"""
        dead, _ = m6.production_dead(D("20"), base)
        lots = int(base / (D(100) * D("20")))
        assert D(100) * D("20") * D(lots) == _M
        assert dead is False

    def test_base_30000_dead_band_is_exactly_151_to_199(self) -> None:
        dead = m6.dead_prices_exact(D("30000"))
        assert dead == list(range(151, 200))
        assert len(dead) == 49 and D(len(dead)) / D(_N_PRICES) == D("49") / D("281")


# ----------------------------------------------------------------------
class TestCriticalLineBoundary:
    """临界线 `base == m` 与 `max_price` 恰界的**存活集**（生产函数实测）。

    ⛔ 只断言"死价位数"是不够的——同计数可对应不同死集。此处钉死**具体存活价位**。
    """

    def test_base_equal_min_value_survival_set(self) -> None:
        """`base = m = 20000` ⇒ 275 死 / **恰 6 个存活**：{20, 25, 40, 50, 100, 200}。

        （存活 ⟺ planned == 20000；即 `p` 使 `100·p·floor(20000/(100p)) == 20000`。）
        """
        base = _M                                   # base 恰等于 min_position_value
        survived = [p for p in range(_LO, _HI + 1)
                    if not m6.production_dead(D(p), base)[0]]
        assert survived == [20, 25, 40, 50, 100, 200]
        assert set(survived) == {20, 25, 40, 50, 100, 200}
        assert len(survived) == 6
        # 死价位 275 个，与 NAV=100000 档一致
        assert _N_PRICES - len(survived) == 275
        # 存活者 planned 恰 == m（边界语义）
        for p in survived:
            lots = int(base / (D(100) * D(p)))
            assert D(100) * D(p) * D(lots) == _M

    def test_price_at_max_price_boundary_is_built(self) -> None:
        """`p = 300` = `max_price` 恰界 ⇒ **建成**（`close > max_price` 为假，不触发 F10c）。"""
        cfg_max = PortfolioConfig().max_price
        assert cfg_max == D("300.0")
        assert D("300") <= cfg_max                        # 恰界不触发高价过滤
        dead, reason = m6.production_dead(D("300"), D("30000"))
        assert dead is False and reason == ""
        lots = int(D("30000") / (D(100) * D("300")))
        assert D(100) * D("300") * D(lots) == D("30000")  # planned = base，满额
        # 恰界之内建成、恰界之外异因丢弃（对照）
        assert m6.production_dead(D("299"), D("30000"))[0] is False
        assert m6.production_dead(D("301"), D("30000"))[1] == "超过价格上限"


# ----------------------------------------------------------------------
class TestExactVsProductionEquivalence:
    """(c) 独立判别式 == 生产函数（暴力）；闭式 (base/200, base/100] 已被证否。"""

    def test_exact_discriminant_equals_production_on_grid(self) -> None:
        bases = [D(b) for b in range(18000, 40001, 500)]
        mismatches: list[tuple[str, int]] = []
        for base in bases:
            for p_int in range(_LO, _HI + 1):
                p = D(p_int)
                prod_dead, _ = m6.production_dead(p, base)
                if prod_dead != m6.exact_discriminant_dead(p, base):
                    mismatches.append((str(base), p_int))
        assert mismatches == [], f"精确式与生产函数不一致: {mismatches[:5]}"

    def test_closed_form_is_falsified_with_counterexample(self) -> None:
        """闭式 `(base/200, base/100]` 与生产实测**判定相反** ⇒ 从测试层面钉死已证否。"""
        base, p = D("30000"), D("200")
        assert m6.closed_form_dead(p, base) is True          # 闭式误判为死
        assert m6.production_dead(p, base)[0] is False        # 生产实测为活
        # 另一方向的反例：精确式判死、闭式判活（base=26000 的 k≥2 碎段）
        base2, p2 = D("26000"), D("66")
        assert m6.exact_discriminant_dead(p2, base2) is True
        assert m6.closed_form_dead(p2, base2) is False
        # 网格上闭式与生产实测存在大量不一致（故必须用精确式）
        n_closed_mismatch = 0
        for b in (D("30000"), D("26000"), D("22000")):
            for p_int in range(_LO, _HI + 1):
                pp = D(p_int)
                if m6.closed_form_dead(pp, b) != m6.production_dead(pp, b)[0]:
                    n_closed_mismatch += 1
        assert n_closed_mismatch > 0


# ----------------------------------------------------------------------
class TestH5cDeadBandDegradation:
    """(d) H5c：死带随 base 单调变宽 ⇒ NAV 退化放大丢弃率。"""

    @pytest.mark.parametrize("base", [D("19999"), D("18000")])
    def test_below_min_value_all_prices_dropped(self, base: Decimal) -> None:
        """base < m ⇒ 对 p∈[20,300] 全体，生产函数实测全部丢弃且原因为「低于单票下限」。"""
        for p_int in range(_LO, _HI + 1):
            dead, reason = m6.production_dead(D(p_int), base)
            assert dead is True, f"base={base} p={p_int} 竟未丢弃"
            assert reason == "低于单票下限"

    @pytest.mark.parametrize(
        "nav, base, n_dead",
        [("150000", "30000", 49), ("130000", "26000", 123), ("110000", "22000", 217),
         ("100000", "20000", 275), ("90000", "18000", 281)],
    )
    def test_nav_table_dead_counts(self, nav: str, base: str, n_dead: int) -> None:
        dead, n, _ = m6.dead_share_exact(D(base))
        assert n == n_dead
        assert len(dead) == n_dead
        assert D(n_dead) / D(_N_PRICES) == D(sum(1 for p in dead)) / D(_N_PRICES)

    def test_dead_set_is_monotone_non_decreasing_as_base_falls(self) -> None:
        bases = [D("30000"), D("26000"), D("22000"), D("20000"), D("18000")]
        dead_sets = [set(m6.dead_prices_exact(b)) for b in bases]
        for hi, lo in zip(dead_sets, dead_sets[1:]):        # base 下降 ⇒ 死集只增（包含）
            assert lo >= hi, "单调性被破坏：base 下降后死集反而收缩"

    def test_critical_line_equalities(self) -> None:
        assert m6.recompute_h5c()["critical_line"]["equal_N8_always_dead"] is True
        assert _NAV / D(8) == D("18750") < _M


# ----------------------------------------------------------------------
class TestDoubleCount61:
    """(e) ㊶ `default_positions × target_count` 的隐性双口径。"""

    def _combo(self, dp: int, tc: int) -> dict[str, object]:
        signals = {f"s{i}": D("1") for i in range(dp)}
        cfg = PortfolioConfig(target_count=tc)
        targets = select_targets(signals, cfg)               # N = min(dp, tc)
        bars = {s: m6.make_bar("10.00", symbol=s) for s in targets}
        plan, dropped = plan_positions(targets, _NAV, bars, cfg)
        return {"N": len(targets), "base": _NAV / D(len(targets)) if targets else _ZERO,
                "plan": plan, "dropped": dropped}

    def test_dp5_tc5_n5(self) -> None:
        r = self._combo(5, 5)
        assert r["N"] == 5 and r["base"] == D("30000") and len(r["plan"]) == 5

    def test_raising_only_target_count_is_ineffective(self) -> None:
        """(5,8) ⇒ len(signals)≤5 ⇒ N=5 不变（抬 target_count 单改无效）。"""
        r = self._combo(5, 8)
        assert r["N"] == 5 and r["base"] == D("30000") and len(r["plan"]) == 5

    def test_raising_only_default_positions_is_ineffective(self) -> None:
        r = self._combo(8, 5)
        assert r["N"] == 5 and r["base"] == D("30000") and len(r["plan"]) == 5

    def test_dp8_tc8_is_permanently_dead(self) -> None:
        """(8,8) ⇒ N=8 ⇒ base=18750 < 20000 ⇒ plan_positions 返回**空计划**。"""
        r = self._combo(8, 8)
        assert r["N"] == 8 and r["base"] == D("18750") < _M
        assert r["plan"] == {}
        assert len(r["dropped"]) == 8
        assert {reason for _, reason in r["dropped"]} == {"低于单票下限"}

    def test_dp3_tc5_gives_wide_base_no_dead(self) -> None:
        r = self._combo(3, 5)
        assert r["N"] == 3 and r["base"] == D("50000") and len(r["plan"]) == 3


# ----------------------------------------------------------------------
class TestEndpointBracket:
    """(f) 只读签名产物 ⇒ 端点括号（终值点 / 谷值点死带占比区间）。"""

    def test_final_and_initial_nav_from_signed_artifact(self, signed_metrics: dict) -> None:
        assert signed_metrics["final_nav"] == "108421.14"
        assert signed_metrics["initial_nav"] == "150000"

    def test_final_point_dead_share_recomputed(self, signed_metrics: dict) -> None:
        final_nav = D(str(signed_metrics["final_nav"]))
        base_final = final_nav / D(5)
        assert base_final == D("21684.228")
        _dead, n_dead, share = m6.dead_share_exact(base_final)
        assert n_dead == 228
        assert share == D("228") / D("281")

    def test_trough_dead_share_interval_bracket(self, signed_metrics: dict) -> None:
        """峰值 ≥ initial_nav、谷值 ≤ final_nav ⇒ share(谷值) ∈ [share(final), 100%]。"""
        initial = D(str(signed_metrics["initial_nav"]))
        final = D(str(signed_metrics["final_nav"]))
        mdd = D(str(signed_metrics["max_drawdown"]))

        base_final = final / D(5)
        trough_lo = (D("1") - mdd) * initial          # peak 取最小允许值 initial_nav
        assert trough_lo <= final                     # 与"谷值 ≤ 终值"一致
        base_trough_lo = trough_lo / D(5)
        assert base_trough_lo < base_final            # 谷值 base ≤ 终值 base

        dead_final = set(m6.dead_prices_exact(base_final))
        dead_trough = set(m6.dead_prices_exact(base_trough_lo))
        # 单调性 ⇒ 谷值死带 ⊇ 终值死带 ⇒ share ∈ [share(final), 100%]
        assert dead_trough >= dead_final
        share_final = D(len(dead_final)) / D(_N_PRICES)
        share_trough = D(len(dead_trough)) / D(_N_PRICES)
        assert share_final <= share_trough <= D("1")


# ----------------------------------------------------------------------
class TestOutputDiscipline:
    """落盘纪律：内容确定性（重跑逐字节相同）+ 无 float。"""

    def test_build_result_is_deterministic_and_float_free(self) -> None:
        r1 = m6.build_result()
        r2 = m6.build_result()
        s1 = json.dumps(r1, sort_keys=True, ensure_ascii=False, indent=2)
        s2 = json.dumps(r2, sort_keys=True, ensure_ascii=False, indent=2)
        assert s1 == s2                                   # 无时间戳 / 无随机量
        assert not _contains_float(r1)                     # 金额全 Decimal→str

    def test_anchor_block_present(self) -> None:
        anchor = m6.build_result()["source_anchor"]
        assert anchor["source_run_id"] == "20260907-150402-t312-dividend-v1-noseed"
        assert anchor["anti_tamper_signature"] == (
            "055cb1d6b2bedbb88aa73d107396982b49f829a6d57236d4d16b921f4c2982d5")
        assert "metrics.final_nav" in anchor["artifact_fields_read"]
        assert any("plan_positions" in f for f in anchor["production_functions"])

    def test_on_disk_artifact_matches_build_result(self) -> None:
        """**落盘漂移门禁**：盘上产物必须逐字节 == 重算结果（人手改 JSON ⇒ 立刻红）。

        ⚠️ 与 `test_build_result_is_deterministic_and_float_free` 的区别：那条只比对
        **内存**两次 `build_result()`，**从不读盘** ⇒ 手改 JSON 仍会全绿。本条补上该缺口。
        读取用 bytes→utf-8 解码（比 `read_text` 严格：连 CRLF 改写也能检出）。
        """
        path = m6.ROOT / m6.OUTPUT_REL
        assert path.exists(), "落盘产物不存在；先跑 scripts/m6_shadow_recompute.py"
        on_disk = path.read_bytes().decode("utf-8")
        expected = json.dumps(
            m6.build_result(), sort_keys=True, ensure_ascii=False, indent=2,
        ) + "\n"
        assert on_disk == expected


def _contains_float(obj: object) -> bool:
    """递归断言结构内无 float（Decimal 已全部 str 化）。"""
    if isinstance(obj, float):
        return True
    if isinstance(obj, dict):
        return any(_contains_float(k) or _contains_float(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return any(_contains_float(v) for v in obj)
    return False
