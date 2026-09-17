# -*- coding: utf-8 -*-
"""宪法守卫：防止防护文档被后继窗口/模型削弱或删除。

背景（2026-09-17 E4 翻案教训）：项目防跑偏机制依赖 PLAYBOOK/STRATEGY
两份文档，但它们此前没有任何硬约束保护——一个糊涂的窗口可以删掉
「不许做」条款让项目失去护栏。本测试把防护文档的关键结构变成
测试断言：文档消失、关键章节被删、禁止清单缩水 → 测试红 →
按「全绿才 commit」铁律无法提交。

规则：本文件中的阈值只许上调（防护只增不减），下调须用户明示。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = ROOT / "docs" / "STRATEGY.md"
PLAYBOOK = ROOT / "docs" / "ALPHA3_PLAYBOOK.md"
TRACKER = ROOT / "docs" / "TASK_TRACKER.md"

# 禁止清单基线行数（PLAYBOOK「已证负/已封顶」表，2026-09-17 快照=7）。
# 只允许增加，不允许减少。
_FORBIDDEN_FLOOR = 7


def _read(p: Path) -> str:
    assert p.exists(), f"防护文档缺失：{p.relative_to(ROOT)}"
    return p.read_text(encoding="utf-8")


class TestConstitutionDocs:
    """三份治理文档必须存在且关键章节在位。"""

    def test_strategy_exists_with_key_sections(self):
        text = _read(STRATEGY)
        for kw in ("瓶颈归因优先", "终局裁决", "临时裁决",
                   "数据引入原则", "收缩纪律"):
            assert kw in text, f"STRATEGY.md 缺少关键章节关键词：{kw}"

    def test_playbook_exists_with_key_sections(self):
        text = _read(PLAYBOOK)
        for kw in ("已裁决证据台账", "操作铁律", "标准实验流程",
                   "性能受限模式"):
            assert kw in text, f"ALPHA3_PLAYBOOK.md 缺少关键章节关键词：{kw}"

    def test_tracker_exists(self):
        _read(TRACKER)

    def test_strategy_reading_order_first(self):
        """STRATEGY 必须声明自己是最先读取的文档（防被降级）。"""
        text = _read(STRATEGY)
        assert "先于一切文档" in text or "最高层" in text


class TestForbiddenListMonotonic:
    """已证负清单只增不减——防「删掉禁止条款重开已判负方向」。"""

    def test_forbidden_table_not_shrinking(self):
        text = _read(PLAYBOOK)
        m = re.search(r"已证负 / 已封顶（不许再做）\n\n((?:\|.*\n)+)", text)
        assert m, "PLAYBOOK 已证负表格结构丢失"
        rows = [l for l in m.group(1).strip().splitlines()
                if l.startswith("|") and "---" not in l
                and "方向" not in l.split("|")[1]]
        assert len(rows) >= _FORBIDDEN_FLOOR, (
            f"已证负清单 {len(rows)} 行 < 基线 {_FORBIDDEN_FLOOR} 行——"
            "禁止条款被删减，违反宪法守卫")

    def test_verdict_grading_present(self):
        """裁决分级制度必须在位（E4 教训：实现存疑不许升级为路线判负）。"""
        text = _read(STRATEGY)
        assert "该实现下证负" in text and "不许" in text
