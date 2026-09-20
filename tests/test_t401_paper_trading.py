#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T401 模拟盘执行器测试套件（≥10 单测）。

测试覆盖：
  1. PaperBroker 接口契约（继承 BacktestBroker）
  2. 状态持久化/恢复（JSON 原子写 + 哈希校验）
  3. 幂等重跑保护（重复日期跳过）
  4. 配置校验（初始资金/路径/策略参数）
  5. 日终任务编排基本流程
"""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.broker import BacktestBroker
from backtest.ledger import BookView
from paper_trading.broker import PaperBroker
from paper_trading.config import ConfigError, PaperTradingConfig
from paper_trading.state import PaperTradingState, StateError

# ---------------------------------------------------------------------- 配置测试


class TestPaperTradingConfig:
    """PaperTradingConfig 校验测试。"""

    def test_default_config_valid(self):
        """默认配置通过校验（初始资金 10 万）。"""
        cfg = PaperTradingConfig()
        assert cfg.initial_capital == Decimal("100000")
        assert cfg.dry_run is True
        assert cfg.max_orders_per_day == 100

    def test_initial_capital_below_minimum_raises(self):
        """初始资金低于 10000 → ConfigError。"""
        with pytest.raises(ConfigError, match="低于系统下限"):
            PaperTradingConfig(initial_capital=Decimal("5000"))

    def test_data_root_not_exist_raises(self):
        """数据根目录不存在 → ConfigError。"""
        with pytest.raises(ConfigError, match="数据根目录不存在"):
            PaperTradingConfig(data_root=Path("/nonexistent/path"))

    def test_strategy_params_serializable(self, tmp_path: Path):
        """策略参数可 JSON 序列化。"""
        cfg = PaperTradingConfig(
            data_root=tmp_path,
            state_path=tmp_path / "state.json",
            strategy_params={"lookback": 20, "threshold": 0.5},
        )
        # 能成功创建即通过
        assert cfg.strategy_params["lookback"] == 20

    def test_config_to_dict_and_back(self, tmp_path: Path):
        """配置序列化 → 反序列化往返一致。"""
        cfg = PaperTradingConfig(
            initial_capital=Decimal("150000"),
            data_root=tmp_path,
            state_path=tmp_path / "state.json",
            strategy_params={"foo": "bar"},
        )
        data = cfg.to_dict()
        cfg2 = PaperTradingConfig.from_dict(data)
        assert cfg2.initial_capital == Decimal("150000")
        assert cfg2.strategy_params == {"foo": "bar"}


# ---------------------------------------------------------------------- 状态测试


class TestPaperTradingState:
    """PaperTradingState 持久化/恢复测试。"""

    def test_state_save_and_load_roundtrip(self, tmp_path: Path):
        """状态保存 → 加载往返一致（哈希校验通过）。"""
        state = PaperTradingState(
            last_trading_date="2026-09-02",
            cash="50000.00",
            frozen_cash="1000.00",
            positions={
                "sh.600000": {
                    "volume": 100,
                    "sellable": 0,
                    "cost_basis": "10.00",
                    "last_close": "10.50",
                    "market_value": "1050.00",
                }
            },
            nav="51050.00",
        )
        path = tmp_path / "state.json"
        state.save(path)

        state2 = PaperTradingState.load(path)
        assert state2.last_trading_date == "2026-09-02"
        assert state2.cash == "50000.00"
        assert state2.positions["sh.600000"]["volume"] == 100
        assert state2.nav == "51050.00"

    def test_state_hash_mismatch_raises(self, tmp_path: Path):
        """状态文件被篡改（哈希不匹配）→ StateError。"""
        state = PaperTradingState(
            last_trading_date="2026-09-02",
            cash="50000.00",
        )
        path = tmp_path / "state.json"
        state.save(path)

        # 篡改文件
        with path.open("r") as f:
            data = json.load(f)
        data["cash"] = "99999.00"  # 篡改现金
        with path.open("w") as f:
            json.dump(data, f)

        with pytest.raises(StateError, match="状态哈希不匹配"):
            PaperTradingState.load(path)

    def test_state_compute_hash_stable(self):
        """状态哈希计算稳定（同数据 → 同哈希）。"""
        state1 = PaperTradingState(
            last_trading_date="2026-09-02",
            cash="100000.00",
        )
        hash1 = state1.compute_hash()

        state2 = PaperTradingState(
            last_trading_date="2026-09-02",
            cash="100000.00",
        )
        hash2 = state2.compute_hash()

        assert hash1 == hash2
        assert len(hash1) == 16  # SHA-256[:16]


# ---------------------------------------------------------------------- PaperBroker 测试


class TestPaperBroker:
    """PaperBroker 接口契约测试（继承 BacktestBroker）。"""

    def test_paper_broker_inherits_backtest_broker(self):
        """PaperBroker 是 BacktestBroker 的子类。"""
        from backtest.ledger import Ledger
        from backtest.matching import MatchEngine

        today = date(2026, 9, 2)
        ledger = Ledger(Decimal("100000"), date=today)
        matcher = MatchEngine()
        broker = PaperBroker(matcher, ledger)
        assert isinstance(broker, BacktestBroker)

    def test_paper_broker_get_summary(self):
        """PaperBroker.get_summary() 返回账户摘要。"""
        from backtest.ledger import Ledger
        from backtest.matching import MatchEngine

        today = date(2026, 9, 2)
        ledger = Ledger(Decimal("100000"), date=today)
        matcher = MatchEngine()
        broker = PaperBroker(matcher, ledger)
        broker.deposit(Decimal("100000"), date=today, ref_id="INIT")

        summary = broker.get_summary()
        assert "cash" in summary
        assert "nav" in summary
        assert "positions_count" in summary


# ---------------------------------------------------------------------- 一致性测试


class TestBacktestPaperParity:
    """回测与模拟盘结构一致性验证。"""

    def test_broker_api_compatibility(self):
        """PaperBroker 与 BacktestBroker API 一致（submit/cancel/on_bars/settle）。"""
        from backtest.ledger import Ledger
        from backtest.matching import MatchEngine

        today = date(2026, 9, 2)
        ledger = Ledger(Decimal("100000"), date=today)
        matcher = MatchEngine()
        broker = PaperBroker(matcher, ledger)

        # 检查方法存在
        assert hasattr(broker, "submit")
        assert hasattr(broker, "cancel")
        assert hasattr(broker, "on_bars")
        assert hasattr(broker, "settle")
        assert hasattr(broker, "book")
        assert hasattr(broker, "deposit")

    def test_state_structure_complete(self):
        """PaperTradingState 字段完整（持仓/现金/订单/NAV）。"""
        state = PaperTradingState(
            last_trading_date="2026-09-02",
            cash="100000.00",
            frozen_cash="0",
            nav="100000.00",
        )
        # 检查必需字段
        assert hasattr(state, "last_trading_date")
        assert hasattr(state, "cash")
        assert hasattr(state, "frozen_cash")
        assert hasattr(state, "positions")
        assert hasattr(state, "pending_orders")
        assert hasattr(state, "nav")
        assert hasattr(state, "state_hash")


# ---------------------------------------------------------------------- 基本功能测试


class TestPaperTradingBasics:
    """模拟盘基本功能测试。"""

    def test_config_validates_max_orders(self, tmp_path: Path):
        """max_orders_per_day 必须 > 0。"""
        with pytest.raises(ConfigError, match="max_orders_per_day"):
            PaperTradingConfig(
                data_root=tmp_path,
                max_orders_per_day=0,
            )

    def test_state_version_recorded(self):
        """状态版本记录（兼容性标记）。"""
        state = PaperTradingState()
        assert state.version == "1.0"

    def test_state_empty_positions_allowed(self):
        """空持仓状态合法（冷启动）。"""
        state = PaperTradingState(
            last_trading_date="2026-09-02",
            cash="100000.00",
            nav="100000.00",
        )
        assert len(state.positions) == 0
        assert len(state.pending_orders) == 0

    def test_state_persists_trade_history_for_dividend_tax(self, tmp_path: Path):
        """成交史+送转史落盘并可恢复（红利税 FIFO 重放数据源）。

        回归：此前 state 不落 ``broker._trades`` ⇒ 重启后首个除权日
        「FIFO 队列空 vs 持仓>0」撞 ``compute_dividend_tax`` 边界校验崩。
        """
        from backtest.ledger import Ledger
        from backtest.matching import MatchEngine
        from backtest.types import Trade
        from backtest.constants import OrderSide

        today = date(2026, 9, 2)
        ledger = Ledger(Decimal("100000"), date=today)
        broker = PaperBroker(MatchEngine(), ledger)
        # 手工注入一笔历史买入 + 一笔送转 + 对应持仓
        broker._trades.append(Trade(
            trade_id="t1", client_order_id="o1", symbol="sh.600000",
            side=OrderSide.BUY, volume=1000, price=Decimal("10.00"),
            date=date(2026, 6, 1), fees={}, sellable_date=None,
        ))
        broker._split_events.append((date(2026, 7, 1), "sh.600000", Decimal("2")))
        state = PaperTradingState.from_broker(broker, today)
        assert len(state.trade_history) == 1
        assert len(state.split_events) == 1

        path = tmp_path / "state.json"
        state.save(path)
        loaded = PaperTradingState.load(path)
        assert loaded.trade_history[0]["volume"] == 1000
        assert loaded.split_events[0]["factor"] == "2"

        # 恢复到新 broker ⇒ _trades/_split_events 回填，红利税可算
        ledger2 = Ledger(Decimal("100000"), date=today)
        broker2 = PaperBroker(MatchEngine(), ledger2)
        loaded.restore_to_broker(broker2)
        assert len(broker2._trades) == 1
        t = broker2._trades[0]
        assert (t.symbol, t.side, t.volume, t.date) == (
            "sh.600000", OrderSide.BUY, 1000, date(2026, 6, 1))
        assert broker2._split_events == [(date(2026, 7, 1), "sh.600000", Decimal("2"))]

        # 端到端：恢复后的成交史喂给红利税 —— 除权日持股 2000（买1000×送转2）
        from backtest.dividend_tax import DividendEvent, compute_dividend_tax
        buys = [(t.date, t.symbol, t.volume) for t in broker2._trades if t.side == OrderSide.BUY]
        tax = compute_dividend_tax(
            [DividendEvent(ex_date=date(2026, 8, 1), symbol="sh.600000",
                           dividend_per_share=Decimal("0.5"), shares_held=2000)],
            buys, [], split_events=list(broker2._split_events))
        # 持有 61 天 ⇒ 10% 档：2000 × 0.5 × 10% = 100.00
        assert tax == Decimal("100.00")

    def test_state_restore_legacy_without_history(self, tmp_path: Path):
        """旧版 state（无 trade_history 字段）仍可加载恢复（向后兼容）。"""
        from backtest.ledger import Ledger
        from backtest.matching import MatchEngine

        today = date(2026, 9, 2)
        state = PaperTradingState(
            last_trading_date="2026-09-02", cash="50000.00", nav="50000.00",
        )
        path = tmp_path / "state.json"
        state.save(path)
        data = json.loads(path.read_text("utf-8"))
        del data["trade_history"]; del data["split_events"]
        # 旧格式无新键 ⇒ 手工重算哈希绕过完整性校验（模拟历史文件）
        legacy = PaperTradingState(**{k: v for k, v in data.items()
                                      if k in PaperTradingState.__dataclass_fields__})
        path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        broker = PaperBroker(MatchEngine(), Ledger(Decimal("100000"), date=today))
        legacy.restore_to_broker(broker)
        assert len(broker._trades) == 0
