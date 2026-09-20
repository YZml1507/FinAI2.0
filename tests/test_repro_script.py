"""reproduce_final_delivery.compare() 单元测试（假数据，不跑真实回测）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.repro.reproduce_final_delivery import ARMS, compare


class TestCompare:
    def test_exact_match_passes(self):
        actual = {"cagr": "0.085814", "round_trips": 156}
        assert compare({"cagr": "0.085814", "round_trips": 156}, actual) == []

    def test_value_mismatch_fails(self):
        actual = {"cagr": "0.085815", "round_trips": 156}
        diffs = compare({"cagr": "0.085814"}, actual)
        assert len(diffs) == 1 and "cagr" in diffs[0]

    def test_missing_key_fails(self):
        diffs = compare({"fees_sum": "22328.60"}, {})
        assert diffs and "缺失" in diffs[0]

    def test_decimal_zero_tolerance(self):
        # 数值等值（尾零差异）视为相等；任何截断误差则失败（容差 0）
        assert compare({"max_drawdown": "0.1947481109982804239272227391"},
                       {"max_drawdown": "0.19474811099828042392722273910"}) == []
        assert compare({"max_drawdown": "0.1947481109982804239272227391"},
                       {"max_drawdown": "0.1947481109982804"}) != []

    def test_expectations_match_leaderboard_records(self):
        # 期望全集与权威记录同键（防写错字段名导致恒 PASS）
        import json
        lb = Path(__file__).resolve().parents[1] / "experiments" / "lab" / "leaderboard.jsonl"
        if not lb.exists():
            return  # 无数据仓环境跳过
        recs = {}
        for line in lb.read_text().splitlines():
            d = json.loads(line)
            recs[d["experiment"]] = d
        pairs = {"anchor": "isst-e8b-fix688-v2", "cold": "e20-oos-2025",
                 "warm": "e20-oos-warm"}
        for arm, rec_name in pairs.items():
            rec = recs[rec_name]
            for k, want in ARMS[arm]["expect"].items():
                assert str(rec[k]) == str(want), f"{arm}.{k} 期望漂移"
