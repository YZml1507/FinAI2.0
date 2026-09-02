"""
台账自动化调度脚本 — 执行到期提醒、复查提醒、生成报告

用途：Windows任务计划每日18:00执行，检查台账状态并发送告警
"""
import sys
from pathlib import Path
from datetime import datetime, date
import json

# 注入项目路径（兼容独立执行）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ops.expiry_reminder import check_expiry_dates, format_expiry_summary, get_expiry_items_report
from ops.check_reminder import check_review_due, format_check_summary, get_check_items_report


def send_alert_to_feishu(message: str, level: str = "warning"):
    """发送告警到飞书（mock实现，真实环境应调用飞书MCP）

    Args:
        message: 告警内容（Markdown格式）
        level: 告警级别（info/warning/critical）
    """
    print(f"\n{'='*60}")
    print(f"[{level.upper()}] 飞书告警（模拟发送）")
    print(f"{'='*60}")
    print(message)
    print(f"{'='*60}\n")

    # TODO: 集成飞书MCP或Webhook
    # from ops.feishu_alert import send_card
    # send_card(title="台账告警", content=message, level=level)


def generate_ledger_report(today: date | None = None) -> dict:
    """生成台账综合报告

    Args:
        today: 报告日期（测试时可注入）

    Returns:
        结构化报告数据
    """
    if today is None:
        today = date.today()

    expiry_report = get_expiry_items_report(today)
    check_report = get_check_items_report(today)

    return {
        "report_date": today.isoformat(),
        "expiry": expiry_report,
        "review": check_report,
        "summary": {
            "total_critical": expiry_report["critical_count"],
            "total_warning": expiry_report["warning_count"] + check_report["overdue_count"],
            "total_due": check_report["due_count"],
            "total_upcoming": check_report["upcoming_count"],
        }
    }


def save_report(report: dict, output_dir: Path | None = None):
    """保存报告到文件

    Args:
        report: 报告数据
        output_dir: 输出目录（默认 PROJECT_ROOT/runs/ledger_reports）
    """
    if output_dir is None:
        output_dir = PROJECT_ROOT / "runs" / "ledger_reports"

    output_dir.mkdir(parents=True, exist_ok=True)

    # 文件名：YYYYMMDD-ledger-report.json
    filename = f"{report['report_date'].replace('-', '')}-ledger-report.json"
    output_path = output_dir / filename

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f">> 报告已保存：{output_path}")


def run_ledger_tasks():
    """执行台账自动化任务（调度入口）"""
    print(f"\n{'='*60}")
    print(f"台账自动化任务 — {datetime.now().isoformat()}")
    print(f"{'='*60}\n")

    today = date.today()

    # 1. 到期提醒
    print("[1] 检查到期项...")
    expiry_alerts = check_expiry_dates(today)
    if expiry_alerts:
        summary = format_expiry_summary(expiry_alerts)
        # 判断告警级别
        has_critical = any(a.level.value == "critical" for a in expiry_alerts)
        level = "critical" if has_critical else "warning"
        send_alert_to_feishu(summary, level=level)
    else:
        print(">> 无到期项")

    # 2. 复查提醒
    print("\n[2] 检查复查项...")
    check_alerts = check_review_due(today)
    if check_alerts:
        summary = format_check_summary(check_alerts)
        # 判断告警级别
        has_overdue = any(a.urgency.value == "overdue" for a in check_alerts)
        level = "critical" if has_overdue else "warning"
        send_alert_to_feishu(summary, level=level)
    else:
        print(">> 无需复查项")

    # 3. 生成报告
    print("\n[3] 生成报告...")
    report = generate_ledger_report(today)
    save_report(report)

    # 4. 统计摘要
    print("\n" + "="*60)
    print("统计摘要")
    print("="*60)
    summary = report["summary"]
    print(f"紧急项：{summary['total_critical']}")
    print(f"警告项：{summary['total_warning']}")
    print(f"到期项：{summary['total_due']}")
    print(f"即将到期：{summary['total_upcoming']}")
    print("="*60 + "\n")


if __name__ == "__main__":
    try:
        run_ledger_tasks()
    except Exception as e:
        print(f"\n[ERROR] 执行失败：{e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
