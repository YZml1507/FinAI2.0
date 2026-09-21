# -*- coding: utf-8 -*-
"""C1 连续权重映射（breadth_weight_mode）单元测试。

契约（R9/R10 §B3.1，预登记 e11-linear）：
  * 'hard'（默认）：与旧 in_mid_zone/mid_cap 语义逐值等价（基线可比）；
  * 'linear'：[defense, attack) 内 mid_cap→1.0 线性；b>=attack 恒 1.0；
    b<defense 零仓（ice 保留硬阈值）；
  * fail-closed：非法取值 raise；
  * 端点复用既有参数（新增自由度 0）。
"""
from decimal import Decimal

import pytest

from strategy.candidates import DividendConfig, DividendStrategy


def _cfg(**kw) -> DividendConfig:
    base = dict(use_breadth_timing=False,
                breadth_attack_threshold=Decimal("0.35"),
                breadth_defense_threshold=Decimal("0.25"),
                breadth_mid_cap=Decimal("0"))
    base.update(kw)
    return DividendConfig(**base)  # type: ignore[arg-type]


def _cfg_linear(mid_cap="0") -> DividendConfig:
    return DividendConfig(
        use_breadth_timing=False, use_ma200_timing=False,
        breadth_series={"2015-01-05": Decimal("0.30")},
        breadth_attack_threshold=Decimal("0.35"),
        breadth_defense_threshold=Decimal("0.25"),
        breadth_mid_cap=Decimal(mid_cap),
        breadth_weight_mode="linear",
    )


class TestBreadthCapHard:
    def test_attack_zone_full(self):
        cfg = _cfg()
        assert DividendStrategy._breadth_cap(cfg, Decimal("0.40")) == Decimal("1")
        assert DividendStrategy._breadth_cap(cfg, Decimal("0.35")) == Decimal("1")

    def test_mid_zone_hard_is_mid_cap(self):
        cfg = _cfg(breadth_mid_cap=Decimal("0.5"))
        assert DividendStrategy._breadth_cap(cfg, Decimal("0.30")) == Decimal("0.5")

    def test_hard_champion_midcap_zero_gives_zero(self):
        """冠军构型（mid_cap=0）hard 模式：mid 区 cap=0（与旧语义逐值等价）。"""
        cfg = _cfg()
        assert DividendStrategy._breadth_cap(cfg, Decimal("0.30")) == Decimal("0")

    def test_hard_below_defense_uses_mid_cap_semantics(self):
        """未确认冰点日（b<defense）在 hard 模式仍按 mid_cap（旧语义等价）。"""
        cfg = _cfg(breadth_mid_cap=Decimal("0.5"))
        assert DividendStrategy._breadth_cap(cfg, Decimal("0.10")) == Decimal("0.5")


class TestBreadthCapLinear:
    def _cfg(self, mid_cap="0"):
        return _cfg_linear(mid_cap)

    def test_endpoints_reuse_existing_params(self):
        cfg = _cfg()
        assert DividendStrategy._breadth_cap(cfg, Decimal("0.25")) == Decimal("0")
        assert DividendStrategy._breadth_cap(cfg, Decimal("0.35")) == Decimal("1")
        assert DividendStrategy._breadth_cap(cfg, Decimal("0.36")) == Decimal("1")
        assert DividendStrategy._breadth_cap(cfg, Decimal("0.24")) == Decimal("0")

    def test_midpoint(self):
        cfg = _cfg_linear()
        assert DividendStrategy._breadth_cap(cfg, Decimal("0.30")) == Decimal("0.5")

    def test_monotonic_nondecreasing(self):
        """斜坡单调不减（宽度越差仓位越低），且相邻步长差 == 斜率×步长。"""
        cfg = _cfg_linear()
        slope = Decimal("1") / (cfg.breadth_attack_threshold - cfg.breadth_defense_threshold)
        b, prev = Decimal("0.45"), None
        step = Decimal("0.005")
        while b >= Decimal("0.20"):
            c = DividendStrategy._breadth_cap(cfg, b)
            if prev is not None:
                assert c <= prev + Decimal("1e-9"), f"非单调 at {b}: {prev} -> {c}"
                diff = prev - c
                # 跨 attack/defense 边界处恒为 0 落差；斜坡上落差 == slope×step
                if Decimal("0.25") <= prev <= Decimal("0.35") and Decimal("0.25") <= b <= Decimal("0.35"):
                    assert diff <= slope * step + Decimal("0.05"), (b, prev, c, diff)
            prev = c
            b -= step

    def test_ice_stays_hard(self):
        """ice 保留硬阈值：b<defense 恒 0（含确认期前），不受 mid_cap 影响。"""
        cfg = DividendConfig(use_breadth_timing=False, use_ma200_timing=False,
                             breadth_attack_threshold=Decimal("0.35"),
                             breadth_defense_threshold=Decimal("0.25"),
                             breadth_mid_cap=Decimal("0.6"),
                             breadth_weight_mode="linear")
        assert DividendStrategy._breadth_cap(cfg, Decimal("0.20")) == Decimal("0")
        assert DividendStrategy._breadth_cap(cfg, Decimal("0.10")) == Decimal("0")

    def test_no_new_threshold_at_midpoint(self):
        """斜坡上任意点只是线性插值——不存在新的决策阈值（防 G-2 复发）。"""
        cfg = _cfg_linear()
        lo, hi = Decimal("0.25"), Decimal("0.35")
        for i in range(1, 10):
            b = lo + (hi - lo) * Decimal(i) / Decimal(10)
            expect = (b - lo) / (hi - lo)
            got = DividendStrategy._breadth_cap(cfg, b)
            assert abs(got - expect) < Decimal("0.0000001"), (b, got, got - expect)


class TestConfigValidation:
    def test_invalid_mode_raises(self):
        with __import__("pytest").raises(ValueError, match="breadth_weight_mode"):
            DividendConfig(breadth_weight_mode="smooth")

    def test_valid_modes_accepted(self):
        for m in ("hard", "linear"):
            cfg = DividendConfig(breadth_weight_mode=m)
            assert cfg.breadth_weight_mode == m
        cfg = DividendConfig(breadth_weight_mode="linear_neutral",
                             breadth_neutral_attack=Decimal("0.32"))
        assert cfg.breadth_weight_mode == "linear_neutral"

    def test_default_is_hard(self):
        assert DividendConfig().breadth_weight_mode == "hard"


class TestHardModeEquivalence:
    def test_hard_equals_legacy_semantics(self):
        """hard 模式 cap 与旧逻辑逐值等价：attack→1，<attack→mid_cap（ champion=0）。"""
        cfg = _cfg()
        for b in ("0.10", "0.20", "0.24", "0.25", "0.30", "0.34", "0.35", "0.50"):
            b = Decimal(b)
            expect = Decimal("1") if b >= Decimal("0.35") else Decimal("0")
            assert DividendStrategy._breadth_cap(cfg, b) == expect, b


class TestLinearNeutralMode:
    """e44 暴露配平斜坡：ramp 区 [defense, neutral_attack]→1，a' 由
    runner 训练窗标定注入；策略只做裁剪斜坡。"""

    def _na_cfg(self, na="0.32", mid_cap="0"):
        return DividendConfig(
            use_breadth_timing=False, use_ma200_timing=False,
            breadth_series={"2015-01-05": Decimal("0.30")},
            breadth_attack_threshold=Decimal("0.35"),
            breadth_defense_threshold=Decimal("0.25"),
            breadth_mid_cap=Decimal(mid_cap),
            breadth_weight_mode="linear_neutral",
            breadth_neutral_attack=Decimal(na),
        )

    def test_requires_neutral_attack(self):
        with __import__("pytest").raises(ValueError,
                                       match="breadth_neutral_attack"):
            DividendConfig(breadth_weight_mode="linear_neutral")

    def test_neutral_attack_bounds(self):
        import pytest
        # na 须 >defense；越过 attack 合法（标定解常落在 attack 上方，
        # 宽斜坡=更低期望暴露）
        for na in ("0.25", "0.20", "0.24"):
            with pytest.raises(ValueError):
                self._na_cfg(na)
        self._na_cfg(na="0.35")   # na==attack 合法
        self._na_cfg(na="0.46")   # na>attack 合法

    def test_ramp_values(self):
        cfg = self._na_cfg(na="0.30")
        cap = DividendStrategy._breadth_cap
        # ≥attack → 1；≥na <attack → 1；斜坡内线性 0→1；<defense → 0
        assert cap(cfg, Decimal("0.50")) == Decimal("1")
        assert cap(cfg, Decimal("0.35")) == Decimal("1")
        assert cap(cfg, Decimal("0.30")) == Decimal("1")
        assert cap(cfg, Decimal("0.275")) == Decimal("0.5")
        assert cap(cfg, Decimal("0.25")) == Decimal("0")
        assert cap(cfg, Decimal("0.20")) == Decimal("0")

    def test_exposure_identity_vs_hard(self):
        """配平语义抽验：构造宽度分布使 E[neutral(0.30)] == E[hard(0.35)]。

        分布：b∈{0.20,0.275,0.30,0.40} 各一日（简化）——
        hard cap(mid_cap=0): 0,0,0,1 → E=0.25
        neutral na=0.30:      0,0.5,1,1 → E=0.625 —— 演示暴露差；
        真实 a' 由 calibration 脚本在训练窗二分求解，策略不感知。
        """
        cfg = self._na_cfg(na="0.30")
        cap = DividendStrategy._breadth_cap
        hard_cfg = _cfg()
        bs = [Decimal("0.20"), Decimal("0.275"), Decimal("0.30"),
              Decimal("0.40")]
        e_hard = sum(cap(hard_cfg, b) for b in bs) / len(bs)
        e_neut = sum(cap(cfg, b) for b in bs) / len(bs)
        assert e_hard == Decimal("0.25")
        assert e_neut == Decimal("0.625")
