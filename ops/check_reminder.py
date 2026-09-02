"""
复查提醒逻辑 — 扫描台账中的复查频率，提醒过期项

复查判据（基于14号SOP）：
- 月度项：距上次复查 ≥30天 → 提醒复查
- 季度项：距上次复查 ≥90天 → 提醒复查
- 事件触发项：不参与自动提醒
"""
from datetime import date, timedelta
from dataclasses import dataclass
from enum import Enum

from ops.ledger_registry import LEDGER_ITEMS, LedgerItem, CheckFrequency


class ReviewUrgency(str, Enum):
    """复查紧急度"""
    OVERDUE = "overdue"        # 已超期（≥2个周期）
    DUE = "due"                # 到期应复查
    UPCOMING = "upcoming"      # 即将到期（≤7天）


@dataclass
class CheckAlert:
    """复查提醒"""
    item: LedgerItem
    urgency: ReviewUrgency
    days_since_last_check: int
    next_check_due: date
    message: str


def _get_check_interval_days(frequency: CheckFrequency) -> int:
    """获取复查间隔天数"""
    if frequency == CheckFrequency.MONTHLY:
        return 30
    elif frequency == CheckFrequency.QUARTERLY:
        return 90
    else:  # EVENT_TRIGGERED
        return 0  # 不参与周期检查


def check_review_due(today: date | None = None) -> list[CheckAlert]:
    """检查需要复查的台账项

    Args:
        today: 当前日期（测试时可注入，默认使用今天）

    Returns:
        复查提醒列表，按紧急程度排序
    """
    if today is None:
        today = date.today()

    alerts: list[CheckAlert] = []

    for item in LEDGER_ITEMS:
        if item.check_frequency == CheckFrequency.EVENT_TRIGGERED:
            continue  # 事件触发项不参与周期检查

        interval_days = _get_check_interval_days(item.check_frequency)
        if interval_days == 0:
            continue

        days_since = (today - item.last_check_date).days
        next_due = item.last_check_date + timedelta(days=interval_days)
        days_until_due = (next_due - today).days

        # 已超期（≥2个周期）
        if days_since >= interval_days * 2:
            alerts.append(CheckAlert(
                item=item,
                urgency=ReviewUrgency.OVERDUE,
                days_since_last_check=days_since,
                next_check_due=next_due,
                message=f"【超期】{item.name} 已超期 {days_since - interval_days} 天未复查！"
                        f"（上次：{item.last_check_date}，频率：{item.check_frequency.value}）"
            ))
        # 到期应复查
        elif days_since >= interval_days:
            alerts.append(CheckAlert(
                item=item,
                urgency=ReviewUrgency.DUE,
                days_since_last_check=days_since,
                next_check_due=next_due,
                message=f"【到期】{item.name} 应复查"
                        f"（上次：{item.last_check_date}，已过 {days_since} 天，频率：{item.check_frequency.value}）"
            ))
        # 即将到期（≤7天）
        elif days_until_due <= 7 and days_until_due >= 0:
            alerts.append(CheckAlert(
                item=item,
                urgency=ReviewUrgency.UPCOMING,
                days_since_last_check=days_since,
                next_check_due=next_due,
                message=f"【即将到期】{item.name} 将在 {days_until_due} 天后需要复查"
                        f"（上次：{item.last_check_date}，频率：{item.check_frequency.value}）"
            ))

    # 按紧急程度排序（overdue > due > upcoming）
    urgency_order = {ReviewUrgency.OVERDUE: 0, ReviewUrgency.DUE: 1, ReviewUrgency.UPCOMING: 2}
    alerts.sort(key=lambda a: (urgency_order[a.urgency], -a.days_since_last_check))

    return alerts


def format_check_summary(alerts: list[CheckAlert]) -> str:
    """格式化复查提醒摘要（用于飞书卡片）

    Returns:
        Markdown格式的摘要文本
    """
    if not alerts:
        return "✅ 无需复查项目，一切正常"

    overdue_count = sum(1 for a in alerts if a.urgency == ReviewUrgency.OVERDUE)
    due_count = sum(1 for a in alerts if a.urgency == ReviewUrgency.DUE)
    upcoming_count = sum(1 for a in alerts if a.urgency == ReviewUrgency.UPCOMING)

    summary = f"## 📋 台账复查提醒\n\n"
    summary += f"**统计**：超期 {overdue_count} 项 / 到期 {due_count} 项 / 即将到期 {upcoming_count} 项\n\n"

    if overdue_count > 0:
        summary += "### 🚨 超期未复查\n"
        for alert in alerts:
            if alert.urgency == ReviewUrgency.OVERDUE:
                summary += f"- {alert.message}\n"
        summary += "\n"

    if due_count > 0:
        summary += "### ⏰ 到期应复查\n"
        for alert in alerts:
            if alert.urgency == ReviewUrgency.DUE:
                summary += f"- {alert.message}\n"
        summary += "\n"

    if upcoming_count > 0:
        summary += "### 📅 即将到期\n"
        for alert in alerts:
            if alert.urgency == ReviewUrgency.UPCOMING:
                summary += f"- {alert.message}\n"

    return summary.strip()


def get_check_items_report(today: date | None = None) -> dict:
    """生成复查项报告（结构化数据）

    Returns:
        {
            "check_date": "2026-09-02",
            "overdue_count": 0,
            "due_count": 3,
            "upcoming_count": 1,
            "alerts": [...]
        }
    """
    if today is None:
        today = date.today()

    alerts = check_review_due(today)

    return {
        "check_date": today.isoformat(),
        "overdue_count": sum(1 for a in alerts if a.urgency == ReviewUrgency.OVERDUE),
        "due_count": sum(1 for a in alerts if a.urgency == ReviewUrgency.DUE),
        "upcoming_count": sum(1 for a in alerts if a.urgency == ReviewUrgency.UPCOMING),
        "alerts": [
            {
                "name": a.item.name,
                "category": a.item.category.value,
                "urgency": a.urgency.value,
                "days_since_last_check": a.days_since_last_check,
                "next_check_due": a.next_check_due.isoformat(),
                "last_check_date": a.item.last_check_date.isoformat(),
                "frequency": a.item.check_frequency.value,
                "message": a.message,
            }
            for a in alerts
        ],
    }
