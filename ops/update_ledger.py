"""
台账更新接口 — 人工复查后更新台账项

⚠️ 警告：本模块提供动态更新接口，但台账登记表的唯一真相源是 ledger_registry.py。
         生产环境应将更新结果回写到 ledger_registry.py 源码，而非维护运行时状态。
"""
from datetime import date
from typing import Optional

from ops.ledger_registry import LEDGER_ITEMS, LedgerItem, LedgerCategory, CheckFrequency


class LedgerUpdateError(Exception):
    """台账更新错误"""
    pass


def update_check_date(item_name: str, check_date: date) -> LedgerItem:
    """更新台账项的复查日期（人工复查后调用）

    Args:
        item_name: 台账项名称
        check_date: 新的复查日期

    Returns:
        更新后的台账项

    Raises:
        LedgerUpdateError: 台账项不存在
    """
    for item in LEDGER_ITEMS:
        if item.name == item_name:
            item.last_check_date = check_date
            return item

    raise LedgerUpdateError(f"台账项不存在：{item_name}")


def update_expiry_date(item_name: str, expiry_date: Optional[date]) -> LedgerItem:
    """更新台账项的到期日

    Args:
        item_name: 台账项名称
        expiry_date: 新的到期日（None表示无到期日）

    Returns:
        更新后的台账项

    Raises:
        LedgerUpdateError: 台账项不存在
    """
    for item in LEDGER_ITEMS:
        if item.name == item_name:
            item.expiry_date = expiry_date
            return item

    raise LedgerUpdateError(f"台账项不存在：{item_name}")


def update_notes(item_name: str, notes: str) -> LedgerItem:
    """更新台账项的备注

    Args:
        item_name: 台账项名称
        notes: 新的备注内容

    Returns:
        更新后的台账项

    Raises:
        LedgerUpdateError: 台账项不存在
    """
    for item in LEDGER_ITEMS:
        if item.name == item_name:
            item.notes = notes
            return item

    raise LedgerUpdateError(f"台账项不存在：{item_name}")


def add_ledger_item(item: LedgerItem) -> None:
    """新增台账项

    Args:
        item: 新的台账项

    Raises:
        LedgerUpdateError: 台账项已存在
    """
    for existing in LEDGER_ITEMS:
        if existing.name == item.name:
            raise LedgerUpdateError(f"台账项已存在：{item.name}")

    LEDGER_ITEMS.append(item)


def remove_ledger_item(item_name: str) -> LedgerItem:
    """移除台账项

    Args:
        item_name: 台账项名称

    Returns:
        被移除的台账项

    Raises:
        LedgerUpdateError: 台账项不存在
    """
    for i, item in enumerate(LEDGER_ITEMS):
        if item.name == item_name:
            return LEDGER_ITEMS.pop(i)

    raise LedgerUpdateError(f"台账项不存在：{item_name}")


def generate_ledger_source_code() -> str:
    """生成台账登记表的源码（用于持久化更新）

    Returns:
        可直接写入 ledger_registry.py 的 LEDGER_ITEMS 定义
    """
    lines = ["LEDGER_ITEMS = ["]

    for item in LEDGER_ITEMS:
        lines.append("    LedgerItem(")
        lines.append(f'        name="{item.name}",')
        lines.append(f"        category=LedgerCategory.{item.category.name},")
        lines.append(f"        last_check_date=date({item.last_check_date.year}, "
                     f"{item.last_check_date.month}, {item.last_check_date.day}),")
        lines.append(f"        check_frequency=CheckFrequency.{item.check_frequency.name},")

        if item.expiry_date is None:
            lines.append("        expiry_date=None,")
        else:
            lines.append(f"        expiry_date=date({item.expiry_date.year}, "
                        f"{item.expiry_date.month}, {item.expiry_date.day}),")

        lines.append(f'        notes="{item.notes}",')
        lines.append(f'        source_doc="{item.source_doc}"')
        lines.append("    ),")

    lines.append("]")
    return "\n".join(lines)


def export_ledger_csv() -> str:
    """导出台账表为CSV格式（用于人工审阅）

    Returns:
        CSV格式的台账表
    """
    lines = ["名称,类别,上次复查日期,复查频率,到期日,备注,所在文档"]

    for item in LEDGER_ITEMS:
        expiry = item.expiry_date.isoformat() if item.expiry_date else ""
        lines.append(
            f'"{item.name}",'
            f'"{item.category.value}",'
            f"{item.last_check_date.isoformat()},"
            f"{item.check_frequency.value},"
            f"{expiry},"
            f'"{item.notes}",'
            f'"{item.source_doc}"'
        )

    return "\n".join(lines)
