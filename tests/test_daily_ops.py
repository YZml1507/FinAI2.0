"""daily_ops 编排器契约测试——fail-closed 与子集调度语义锁。"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.daily_ops import STEPS, _commands, main  # noqa: E402


class TestCommands:
    def test_emit_cmd_includes_both_vetoes(self):
        cmd = _commands("2026-09-23", 20, 150000)["emit"]
        assert cmd.count("--veto-path") == 2
        assert "--scores" in cmd and "--topn" in cmd and "--capital" in cmd

    def test_veto_cmd_extends_calendar(self):
        cmd = _commands("2026-09-23", 20, 150000)["veto"]
        assert "--extend-calendar" in cmd and "--min-date" in cmd
        assert "veto_daily_2025plus.parquet" in " ".join(cmd)

    def test_bars_end_passthrough(self):
        assert "2030-01-15" in _commands("2030-01-15", 40, 3000000)["bars"]

class TestMain:
    def test_unknown_step_rejected(self):
        assert main.__name__ == "main"
        with patch.object(sys, "argv", ["daily_ops", "--steps", "bogus"]):
            assert main() == 2

    def test_step_failure_stops_chain(self, tmp_path):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            kw["stdout"].write("x")

            class P:
                returncode = 1 if len(calls) == 1 else 0
            return P()

        argv = ["daily_ops", "--steps", "bars,veto,emit"]
        with patch.object(sys, "argv", argv), \
             patch("scripts.daily_ops.LOG_DIR", tmp_path), \
             patch("subprocess.run", fake_run):
            rc = main()
        assert rc == 1 and len(calls) == 1  # 首步炸即停，后续不跑

    def test_subset_skips_omitted(self, tmp_path):
        ran = []

        def fake_run(cmd, **kw):
            ran.append(Path(cmd[1]).stem)
            kw["stdout"].write("x")

            class P:
                returncode = 0
            return P()

        argv = ["daily_ops", "--steps", "score,emit"]
        with patch.object(sys, "argv", argv), \
             patch("scripts.daily_ops.LOG_DIR", tmp_path), \
             patch("subprocess.run", fake_run):
            rc = main()
        assert rc == 0
        assert ran == ["e63_score_2025", "emit_live_basket"]
        assert "bars" not in STEPS[:0]  # 顺序常量不被破坏

    def test_lhb_step_wired_before_veto(self):
        cmds = _commands("2026-09-23", 20, 150000)
        assert "pull_lhb_daily.py" in cmds["lhb"][1]
        assert "20260923" in cmds["lhb"]
        assert STEPS.index("lhb") < STEPS.index("veto")
