"""
到期提醒逻辑 — 扫描台账中的到期日，触发告警

告警阈值：
- 距到期 ≤30天 → 普通提醒
- 距到期 ≤7天 → 紧急告警
- 已过期 → 每日告警直至处理
"""
from datetime import date, timedelta
from dataclasses import dataclass
from enum import Enum

from ops.ledger_registry import LEDGER_ITEMS, LedgerItem


class AlertLevel(str, Enum):
    """告警级别"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class ExpiryAlert:
    """到期告警"""
    item: LedgerItem
    level: AlertLevel
    days_until_expiry: int
    message: str


def check_expiry_dates(today: date | None = None) -> list[ExpiryAlert]:
    """检查所有台账项的到期日，返回需要告警的项

    Args:
        today: 当前日期（测试时可注入，默认使用今天）

    Returns:
        告警列表，按紧急程度排序（critical > warning > info）
    """
    if today is None:
        today = date.today()

    alerts: list[ExpiryAlert] = []

    for item in LEDGER_ITEMS:
        if item.expiry_date is None:
            continue  # 无到期日，跳过

        days_left = (item.expiry_date - today).days

        if days_left < 0:
            # 已过期
            alerts.append(ExpiryAlert(
                item=item,
                level=AlertLevel.CRITICAL,
                days_until_expiry=days_left,
                message=f"【已过期】{item.name} 已过期 {-days_left} 天！"
                        f"（到期日：{item.expiry_date}，类别：{item.category.value}）"
            ))
        elif days_left <= 7:
            # 紧急：≤7天
            alerts.append(ExpiryAlert(
                item=item,
                level=AlertLevel.CRITICAL,
                days_until_expiry=days_left,
                message=f"【紧急】{item.name} 将在 {days_left} 天后到期！"
                        f"（到期日：{item.expiry_date}，类别：{item.category.value}）"
            ))
        elif days_left <= 30:
            # 警告：≤30天
            alerts.append(ExpiryAlert(
                item=item,
                level=AlertLevel.WARNING,
                days_until_expiry=days_left,
                message=f"【提醒】{item.name} 将在 {days_left} 天后到期"
                        f"（到期日：{item.expiry_date}，类别：{item.category.value}）"
            ))

    # 按紧急程度排序（critical > warning > info）
    level_order = {AlertLevel.CRITICAL: 0, AlertLevel.WARNING: 1, AlertLevel.INFO: 2}
    alerts.sort(key=lambda a: (level_order[a.level], a.days_until_expiry))

    return alerts


def format_expiry_summary(alerts: list[ExpiryAlert]) -> str:
    """格式化到期告警摘要（用于飞书卡片）

    Returns:
        Markdown格式的摘要文本
    """
    if not alerts:
        return "✅ 无到期项目，一切正常"

    critical_count = sum(1 for a in alerts if a.level == AlertLevel.CRITICAL)
    warning_count = sum(1 for a in alerts if a.level == AlertLevel.WARNING)

    summary = f"## 🔔 台账到期提醒\n\n"
    summary += f"**统计**：紧急 {critical_count} 项 / 警告 {warning_count} 项\n\n"

    if critical_count > 0:
        summary += "### 🚨 紧急项\n"
        for alert in alerts:
            if alert.level == AlertLevel.CRITICAL:
                summary += f"- {alert.message}\n"
        summary += "\n"

    if warning_count > 0:
        summary += "### ⚠️ 警告项\n"
        for alert in alerts:
            if alert.level == AlertLevel.WARNING:
                summary += f"- {alert.message}\n"

    return summary.strip()


def get_expiry_items_report(today: date | None = None) -> dict:
    """生成到期项报告（结构化数据）

    Returns:
        {
            "check_date": "2026-09-02",
            "critical_count": 1,
            "warning_count": 2,
            "alerts": [...]
        }
    """
    if today is None:
        today = date.today()

    alerts = check_expiry_dates(today)

    return {
        "check_date": today.isoformat(),
        "critical_count": sum(1 for a in alerts if a.level == AlertLevel.CRITICAL),
        "warning_count": sum(1 for a in alerts if a.level == AlertLevel.WARNING),
        "alerts": [
            {
                "name": a.item.name,
                "category": a.item.category.value,
                "level": a.level.value,
                "days_until_expiry": a.days_until_expiry,
                "expiry_date": a.item.expiry_date.isoformat(),
                "message": a.message,
            }
            for a in alerts
        ],
    }
