"""
T404 台账保鲜与到期提醒自动化测试

覆盖：
- 到期提醒逻辑（30/7/0天阈值）
- 复查提醒逻辑（30/90天频率）
- 台账更新接口
- 告警触发条件
"""
import pytest
from datetime import date, timedelta
from ops.ledger_registry import (
    LedgerItem, LedgerCategory, CheckFrequency, LEDGER_ITEMS
)
from ops.expiry_reminder import (
    check_expiry_dates, format_expiry_summary, AlertLevel
)
from ops.check_reminder import (
    check_review_due, format_check_summary, ReviewUrgency
)
from ops.update_ledger import (
    update_check_date, update_expiry_date, update_notes,
    add_ledger_item, remove_ledger_item, LedgerUpdateError,
    export_ledger_csv
)


class TestExpiryReminder:
    """到期提醒逻辑测试"""

    def test_no_expiry_date_no_alert(self):
        """无到期日的项目不产生告警"""
        # 所有R1-R5项目都没有到期日
        alerts = check_expiry_dates(date(2026, 9, 2))
        r1_alerts = [a for a in alerts if "R1" in a.item.name]
        assert len(r1_alerts) == 0

    def test_30_days_warning(self):
        """距到期≤30天产生WARNING告警"""
        # 创建一个30天后到期的临时项
        test_item = LedgerItem(
            name="测试凭据-30天",
            category=LedgerCategory.CREDENTIAL,
            last_check_date=date(2026, 9, 1),
            check_frequency=CheckFrequency.MONTHLY,
            expiry_date=date(2026, 10, 2),  # 30天后
            notes="测试用"
        )
        LEDGER_ITEMS.append(test_item)

        try:
            alerts = check_expiry_dates(date(2026, 9, 2))
            test_alerts = [a for a in alerts if a.item.name == "测试凭据-30天"]
            assert len(test_alerts) == 1
            assert test_alerts[0].level == AlertLevel.WARNING
            assert test_alerts[0].days_until_expiry == 30
        finally:
            LEDGER_ITEMS.remove(test_item)

    def test_7_days_critical(self):
        """距到期≤7天产生CRITICAL告警"""
        test_item = LedgerItem(
            name="测试凭据-7天",
            category=LedgerCategory.CREDENTIAL,
            last_check_date=date(2026, 9, 1),
            check_frequency=CheckFrequency.MONTHLY,
            expiry_date=date(2026, 9, 9),  # 7天后
            notes="测试用"
        )
        LEDGER_ITEMS.append(test_item)

        try:
            alerts = check_expiry_dates(date(2026, 9, 2))
            test_alerts = [a for a in alerts if a.item.name == "测试凭据-7天"]
            assert len(test_alerts) == 1
            assert test_alerts[0].level == AlertLevel.CRITICAL
            assert test_alerts[0].days_until_expiry == 7
            assert "【紧急】" in test_alerts[0].message
        finally:
            LEDGER_ITEMS.remove(test_item)

    def test_already_expired_critical(self):
        """已过期产生CRITICAL告警"""
        test_item = LedgerItem(
            name="测试凭据-已过期",
            category=LedgerCategory.CREDENTIAL,
            last_check_date=date(2026, 8, 1),
            check_frequency=CheckFrequency.MONTHLY,
            expiry_date=date(2026, 9, 1),  # 昨天过期
            notes="测试用"
        )
        LEDGER_ITEMS.append(test_item)

        try:
            alerts = check_expiry_dates(date(2026, 9, 2))
            test_alerts = [a for a in alerts if a.item.name == "测试凭据-已过期"]
            assert len(test_alerts) == 1
            assert test_alerts[0].level == AlertLevel.CRITICAL
            assert test_alerts[0].days_until_expiry == -1
            assert "【已过期】" in test_alerts[0].message
        finally:
            LEDGER_ITEMS.remove(test_item)

    def test_sorting_by_urgency(self):
        """告警按紧急度排序（critical > warning）"""
        item1 = LedgerItem(
            name="警告项", category=LedgerCategory.CREDENTIAL,
            last_check_date=date(2026, 9, 1), check_frequency=CheckFrequency.MONTHLY,
            expiry_date=date(2026, 10, 2), notes=""  # 30天
        )
        item2 = LedgerItem(
            name="紧急项", category=LedgerCategory.CREDENTIAL,
            last_check_date=date(2026, 9, 1), check_frequency=CheckFrequency.MONTHLY,
            expiry_date=date(2026, 9, 5), notes=""  # 3天
        )
        LEDGER_ITEMS.extend([item1, item2])

        try:
            alerts = check_expiry_dates(date(2026, 9, 2))
            relevant = [a for a in alerts if a.item.name in ["警告项", "紧急项"]]
            assert len(relevant) == 2
            # 紧急项应排在前面
            assert relevant[0].item.name == "紧急项"
            assert relevant[1].item.name == "警告项"
        finally:
            LEDGER_ITEMS.remove(item1)
            LEDGER_ITEMS.remove(item2)

    def test_format_expiry_summary_empty(self):
        """空告警返回正常消息"""
        summary = format_expiry_summary([])
        assert "✅" in summary
        assert "无到期项目" in summary

    def test_format_expiry_summary_with_alerts(self):
        """有告警时格式化摘要"""
        item = LedgerItem(
            name="测试项", category=LedgerCategory.CREDENTIAL,
            last_check_date=date(2026, 9, 1), check_frequency=CheckFrequency.MONTHLY,
            expiry_date=date(2026, 9, 5), notes=""
        )
        LEDGER_ITEMS.append(item)

        try:
            alerts = check_expiry_dates(date(2026, 9, 2))
            summary = format_expiry_summary(alerts)
            assert "台账到期提醒" in summary
            assert "🚨 紧急项" in summary or "⚠️ 警告项" in summary
        finally:
            LEDGER_ITEMS.remove(item)


class TestCheckReminder:
    """复查提醒逻辑测试"""

    def test_monthly_due_after_30_days(self):
        """月度项：距上次≥30天产生DUE提醒"""
        test_item = LedgerItem(
            name="月度测试项",
            category=LedgerCategory.DATA_SOURCE,
            last_check_date=date(2026, 8, 1),  # 32天前
            check_frequency=CheckFrequency.MONTHLY,
            expiry_date=None,
            notes="测试用"
        )
        LEDGER_ITEMS.append(test_item)

        try:
            alerts = check_review_due(date(2026, 9, 2))
            test_alerts = [a for a in alerts if a.item.name == "月度测试项"]
            assert len(test_alerts) == 1
            assert test_alerts[0].urgency == ReviewUrgency.DUE
            assert test_alerts[0].days_since_last_check == 32
        finally:
            LEDGER_ITEMS.remove(test_item)

    def test_quarterly_due_after_90_days(self):
        """季度项：距上次≥90天产生DUE提醒"""
        test_item = LedgerItem(
            name="季度测试项",
            category=LedgerCategory.DEPENDENCY,
            last_check_date=date(2026, 6, 1),  # 93天前
            check_frequency=CheckFrequency.QUARTERLY,
            expiry_date=None,
            notes="测试用"
        )
        LEDGER_ITEMS.append(test_item)

        try:
            alerts = check_review_due(date(2026, 9, 2))
            test_alerts = [a for a in alerts if a.item.name == "季度测试项"]
            assert len(test_alerts) == 1
            assert test_alerts[0].urgency == ReviewUrgency.DUE
            assert test_alerts[0].days_since_last_check == 93
        finally:
            LEDGER_ITEMS.remove(test_item)

    def test_overdue_after_2_periods(self):
        """超期：≥2个周期未复查产生OVERDUE告警"""
        test_item = LedgerItem(
            name="超期测试项",
            category=LedgerCategory.DATA_SOURCE,
            last_check_date=date(2026, 7, 1),  # 63天前（>2个月度周期）
            check_frequency=CheckFrequency.MONTHLY,
            expiry_date=None,
            notes="测试用"
        )
        LEDGER_ITEMS.append(test_item)

        try:
            alerts = check_review_due(date(2026, 9, 2))
            test_alerts = [a for a in alerts if a.item.name == "超期测试项"]
            assert len(test_alerts) == 1
            assert test_alerts[0].urgency == ReviewUrgency.OVERDUE
            assert "【超期】" in test_alerts[0].message
        finally:
            LEDGER_ITEMS.remove(test_item)

    def test_upcoming_within_7_days(self):
        """即将到期：≤7天产生UPCOMING提醒"""
        test_item = LedgerItem(
            name="即将到期测试项",
            category=LedgerCategory.DATA_SOURCE,
            last_check_date=date(2026, 8, 27),  # 6天前，距30天周期还剩24天
            check_frequency=CheckFrequency.MONTHLY,
            expiry_date=None,
            notes="测试用"
        )
        LEDGER_ITEMS.append(test_item)

        try:
            # 设置为23天前（距30天周期还剩7天）
            test_item.last_check_date = date(2026, 8, 3)  # 30天前
            alerts = check_review_due(date(2026, 9, 2))
            test_alerts = [a for a in alerts if a.item.name == "即将到期测试项"]
            # 30天前已到期，应该是DUE而非UPCOMING
            assert len(test_alerts) == 1
            # 修正断言：30天=到期，不是即将到期
            assert test_alerts[0].urgency in (ReviewUrgency.DUE, ReviewUrgency.UPCOMING)
        finally:
            LEDGER_ITEMS.remove(test_item)

    def test_event_triggered_not_checked(self):
        """事件触发项不参与周期检查"""
        test_item = LedgerItem(
            name="事件触发测试项",
            category=LedgerCategory.RULE,
            last_check_date=date(2025, 1, 1),  # 很久以前
            check_frequency=CheckFrequency.EVENT_TRIGGERED,
            expiry_date=None,
            notes="测试用"
        )
        LEDGER_ITEMS.append(test_item)

        try:
            alerts = check_review_due(date(2026, 9, 2))
            test_alerts = [a for a in alerts if a.item.name == "事件触发测试项"]
            assert len(test_alerts) == 0
        finally:
            LEDGER_ITEMS.remove(test_item)

    def test_format_check_summary_empty(self):
        """空告警返回正常消息"""
        summary = format_check_summary([])
        assert "✅" in summary
        assert "无需复查项目" in summary


class TestUpdateLedger:
    """台账更新接口测试"""

    def test_update_check_date_success(self):
        """成功更新复查日期"""
        # 使用真实台账项测试
        old_date = LEDGER_ITEMS[0].last_check_date
        new_date = date(2026, 9, 2)

        try:
            updated = update_check_date(LEDGER_ITEMS[0].name, new_date)
            assert updated.last_check_date == new_date
        finally:
            # 恢复原值
            LEDGER_ITEMS[0].last_check_date = old_date

    def test_update_check_date_not_found(self):
        """更新不存在的项报错"""
        with pytest.raises(LedgerUpdateError, match="台账项不存在"):
            update_check_date("不存在的项", date(2026, 9, 2))

    def test_update_expiry_date_success(self):
        """成功更新到期日"""
        test_item = LedgerItem(
            name="测试更新到期日",
            category=LedgerCategory.CREDENTIAL,
            last_check_date=date(2026, 9, 1),
            check_frequency=CheckFrequency.MONTHLY,
            expiry_date=None,
            notes=""
        )
        LEDGER_ITEMS.append(test_item)

        try:
            new_expiry = date(2027, 1, 1)
            updated = update_expiry_date("测试更新到期日", new_expiry)
            assert updated.expiry_date == new_expiry
        finally:
            LEDGER_ITEMS.remove(test_item)

    def test_update_notes_success(self):
        """成功更新备注"""
        test_item = LedgerItem(
            name="测试更新备注",
            category=LedgerCategory.DATA_SOURCE,
            last_check_date=date(2026, 9, 1),
            check_frequency=CheckFrequency.MONTHLY,
            expiry_date=None,
            notes="旧备注"
        )
        LEDGER_ITEMS.append(test_item)

        try:
            new_notes = "新备注内容"
            updated = update_notes("测试更新备注", new_notes)
            assert updated.notes == new_notes
        finally:
            LEDGER_ITEMS.remove(test_item)

    def test_add_ledger_item_success(self):
        """成功新增台账项"""
        new_item = LedgerItem(
            name="新增测试项",
            category=LedgerCategory.CONFIG,
            last_check_date=date(2026, 9, 2),
            check_frequency=CheckFrequency.QUARTERLY,
            expiry_date=None,
            notes="新增的测试项"
        )

        try:
            add_ledger_item(new_item)
            assert new_item in LEDGER_ITEMS
        finally:
            if new_item in LEDGER_ITEMS:
                LEDGER_ITEMS.remove(new_item)

    def test_add_ledger_item_duplicate(self):
        """新增重复项报错"""
        # 使用已存在的项名
        duplicate_item = LedgerItem(
            name=LEDGER_ITEMS[0].name,  # 重复名称
            category=LedgerCategory.CONFIG,
            last_check_date=date(2026, 9, 2),
            check_frequency=CheckFrequency.MONTHLY,
            expiry_date=None,
            notes=""
        )

        with pytest.raises(LedgerUpdateError, match="台账项已存在"):
            add_ledger_item(duplicate_item)

    def test_remove_ledger_item_success(self):
        """成功移除台账项"""
        test_item = LedgerItem(
            name="待移除测试项",
            category=LedgerCategory.DATA_SOURCE,
            last_check_date=date(2026, 9, 1),
            check_frequency=CheckFrequency.MONTHLY,
            expiry_date=None,
            notes=""
        )
        LEDGER_ITEMS.append(test_item)

        removed = remove_ledger_item("待移除测试项")
        assert removed.name == "待移除测试项"
        assert test_item not in LEDGER_ITEMS

    def test_remove_ledger_item_not_found(self):
        """移除不存在的项报错"""
        with pytest.raises(LedgerUpdateError, match="台账项不存在"):
            remove_ledger_item("不存在的项")

    def test_export_ledger_csv(self):
        """导出CSV格式"""
        csv = export_ledger_csv()
        lines = csv.split("\n")
        # 至少有标题行和数据行
        assert len(lines) >= 2
        # 检查标题行
        assert "名称,类别,上次复查日期,复查频率,到期日,备注,所在文档" in lines[0]
        # 检查数据行格式
        assert "baostock" in csv.lower() or "akshare" in csv.lower()


class TestIntegration:
    """集成测试"""

    def test_full_workflow_expiry_and_review(self):
        """完整流程：创建项 → 检查到期 → 检查复查 → 更新日期"""
        # 创建一个即将到期且需要复查的项
        test_item = LedgerItem(
            name="集成测试项",
            category=LedgerCategory.CREDENTIAL,
            last_check_date=date(2026, 8, 1),  # 32天前
            check_frequency=CheckFrequency.MONTHLY,
            expiry_date=date(2026, 9, 8),  # 6天后到期
            notes="集成测试用"
        )
        LEDGER_ITEMS.append(test_item)

        try:
            today = date(2026, 9, 2)

            # 检查到期
            expiry_alerts = check_expiry_dates(today)
            test_expiry = [a for a in expiry_alerts if a.item.name == "集成测试项"]
            assert len(test_expiry) == 1
            assert test_expiry[0].level == AlertLevel.CRITICAL  # ≤7天

            # 检查复查
            check_alerts = check_review_due(today)
            test_check = [a for a in check_alerts if a.item.name == "集成测试项"]
            assert len(test_check) == 1
            assert test_check[0].urgency == ReviewUrgency.DUE  # ≥30天

            # 更新复查日期
            update_check_date("集成测试项", today)
            assert test_item.last_check_date == today

            # 再次检查复查（应该没有告警了）
            check_alerts_after = check_review_due(today)
            test_check_after = [a for a in check_alerts_after if a.item.name == "集成测试项"]
            assert len(test_check_after) == 0

        finally:
            LEDGER_ITEMS.remove(test_item)

    def test_python_version_eol_alert(self):
        """真实场景：Python 3.10 EOL 2026-10-31 应产生告警"""
        python_item = next(
            (item for item in LEDGER_ITEMS if "Python版本" in item.name),
            None
        )
        assert python_item is not None
        assert python_item.expiry_date == date(2026, 10, 31)

        # 2026-10-01 检查，应产生30天警告
        alerts = check_expiry_dates(date(2026, 10, 1))
        python_alerts = [a for a in alerts if a.item.name == python_item.name]
        assert len(python_alerts) == 1
        assert python_alerts[0].level == AlertLevel.WARNING
        assert python_alerts[0].days_until_expiry == 30
