"""
台账登记表 — 技术资产保鲜与到期提醒

来源：14号文档《易变数据保鲜与复查台账》
用途：定义技术台账项（数据源/依赖库/密钥/配置），供自动化调度检查
"""
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional


class LedgerCategory(str, Enum):
    """台账类别"""
    DATA_SOURCE = "数据源"
    DEPENDENCY = "依赖库"
    CREDENTIAL = "密钥/凭据"
    CONFIG = "配置"
    RULE = "交易规则"
    COST = "交易成本"


class CheckFrequency(str, Enum):
    """复查频率"""
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    EVENT_TRIGGERED = "event_triggered"


@dataclass
class LedgerItem:
    """台账项定义

    Attributes:
        name: 项目名称（如 "baostock API"）
        category: 类别
        last_check_date: 上次复查日期
        check_frequency: 复查频率
        expiry_date: 到期日（可选，仅凭据/证书类）
        notes: 备注（当前值/时点/来源）
        source_doc: 所在文档路径（相对 research-finai 或本仓）
    """
    name: str
    category: LedgerCategory
    last_check_date: date
    check_frequency: CheckFrequency
    expiry_date: Optional[date]
    notes: str
    source_doc: str = ""


# ============================================================
# 台账登记表（唯一定义点，基于 REVALIDATE.md 与 14号文档）
# ============================================================

LEDGER_ITEMS = [
    # ---- R1-R5 母库缺陷台账（已清零，作为技术债监控项） ----
    LedgerItem(
        name="R1-baostock停牌过滤",
        category=LedgerCategory.DATA_SOURCE,
        last_check_date=date(2026, 8, 30),
        check_frequency=CheckFrequency.MONTHLY,
        expiry_date=None,
        notes="已修复：tradestatus=='1' 过滤 + meta['suspended_rows'] 血缘；验收已通过",
        source_doc="REVALIDATE.md R1"
    ),
    LedgerItem(
        name="R2-TDX截断校验",
        category=LedgerCategory.DATA_SOURCE,
        last_check_date=date(2026, 8, 30),
        check_frequency=CheckFrequency.QUARTERLY,
        expiry_date=None,
        notes="随R5挂起：_assert_coverage已落地，TDX腿已砍除，v1纯日线不涉及",
        source_doc="REVALIDATE.md R2"
    ),
    LedgerItem(
        name="R3-东财push2his可达性",
        category=LedgerCategory.DATA_SOURCE,
        last_check_date=date(2026, 8, 30),
        check_frequency=CheckFrequency.MONTHLY,
        expiry_date=None,
        notes="已关闭：本机网络噪音，源端可用已实测确认",
        source_doc="REVALIDATE.md R3"
    ),
    LedgerItem(
        name="R4-复权口径映射",
        category=LedgerCategory.DATA_SOURCE,
        last_check_date=date(2026, 8, 30),
        check_frequency=CheckFrequency.MONTHLY,
        expiry_date=None,
        notes="已修复：AdjustmentMode枚举 + to_kwargs映射 + 禁止默认调用",
        source_doc="REVALIDATE.md R4 + finai/sources/adjustment_mode.py"
    ),
    LedgerItem(
        name="R5-TDX腿依赖缺失",
        category=LedgerCategory.DATA_SOURCE,
        last_check_date=date(2026, 8, 30),
        check_frequency=CheckFrequency.QUARTERLY,
        expiry_date=None,
        notes="已按方案B处置：腿砍除，v1纯日线不需要分钟线",
        source_doc="REVALIDATE.md R5"
    ),

    # ---- 核心数据源 ----
    LedgerItem(
        name="baostock API",
        category=LedgerCategory.DATA_SOURCE,
        last_check_date=date(2026, 9, 2),
        check_frequency=CheckFrequency.MONTHLY,
        expiry_date=None,
        notes="日线主源，login正常，交易日历确认通过（12号附录A.4）",
        source_doc="data/collector.py"
    ),
    LedgerItem(
        name="akshare",
        category=LedgerCategory.DATA_SOURCE,
        last_check_date=date(2026, 8, 28),
        check_frequency=CheckFrequency.MONTHLY,
        expiry_date=None,
        notes="v1.18.94，新浪/腾讯校验通过，东财8月中旬故障已修复（12号§9-A.2）",
        source_doc="12_免费行情数据源实测对比与选型指南.md + data/acceptance.py"
    ),
    LedgerItem(
        name="新浪财经接口",
        category=LedgerCategory.DATA_SOURCE,
        last_check_date=date(2026, 8, 28),
        check_frequency=CheckFrequency.MONTHLY,
        expiry_date=None,
        notes="三源验收已通过，阈值0.2pp，作为akshare上游校验源",
        source_doc="data/acceptance.py ThreeSourceValidator"
    ),
    LedgerItem(
        name="腾讯财经接口",
        category=LedgerCategory.DATA_SOURCE,
        last_check_date=date(2026, 8, 28),
        check_frequency=CheckFrequency.MONTHLY,
        expiry_date=None,
        notes="三源验收已通过，与新浪互为备份",
        source_doc="data/acceptance.py ThreeSourceValidator"
    ),

    # ---- 关键依赖库 ----
    LedgerItem(
        name="pyarrow",
        category=LedgerCategory.DEPENDENCY,
        last_check_date=date(2026, 9, 2),
        check_frequency=CheckFrequency.QUARTERLY,
        expiry_date=None,
        notes="Parquet引擎，已安装并验证（T105落盘格式）",
        source_doc="requirements.txt + data/collector.py"
    ),
    LedgerItem(
        name="pandas",
        category=LedgerCategory.DEPENDENCY,
        last_check_date=date(2026, 9, 2),
        check_frequency=CheckFrequency.QUARTERLY,
        expiry_date=None,
        notes="核心数据处理库，全链路依赖",
        source_doc="requirements.txt"
    ),
    LedgerItem(
        name="Python版本",
        category=LedgerCategory.DEPENDENCY,
        last_check_date=date(2026, 8, 28),
        check_frequency=CheckFrequency.QUARTERLY,
        expiry_date=date(2026, 10, 31),  # 3.10 EOL
        notes="当前使用3.11.5；3.10 EOL 2026-10-31（距今65天）",
        source_doc="12号附录A + 14号台账D10"
    ),

    # ---- 凭据类（示例，真实凭据不入库） ----
    LedgerItem(
        name="代理服务",
        category=LedgerCategory.CREDENTIAL,
        last_check_date=date(2026, 9, 2),
        check_frequency=CheckFrequency.MONTHLY,
        expiry_date=None,
        notes="127.0.0.1:7897，已验证连通（T101环境清单A.3）",
        source_doc="CLAUDE.md §3 硬约束"
    ),

    # ---- 交易成本类（14号台账A） ----
    LedgerItem(
        name="印花税",
        category=LedgerCategory.COST,
        last_check_date=date(2026, 8, 28),
        check_frequency=CheckFrequency.MONTHLY,
        expiry_date=None,
        notes="0.5‰仅卖出（2023-08-28起），已整3年无调整",
        source_doc="14号台账A1 + 07_A股交易规则与交易成本数据手册.md"
    ),
    LedgerItem(
        name="过户费",
        category=LedgerCategory.COST,
        last_check_date=date(2026, 8, 28),
        check_frequency=CheckFrequency.MONTHLY,
        expiry_date=None,
        notes="0.01‰双边（2022-04-29起），无最低收费条款",
        source_doc="14号台账A2 + backtest/fees.py"
    ),
    LedgerItem(
        name="经手费",
        category=LedgerCategory.COST,
        last_check_date=date(2026, 8, 28),
        check_frequency=CheckFrequency.QUARTERLY,
        expiry_date=None,
        notes="A股0.00341%双边；北交所0.125‰双边（约3.67倍）",
        source_doc="14号台账A3/T6 + backtest/fees.py"
    ),
    LedgerItem(
        name="证管费",
        category=LedgerCategory.COST,
        last_check_date=date(2026, 8, 28),
        check_frequency=CheckFrequency.QUARTERLY,
        expiry_date=None,
        notes="0.02‰双边（发改价格〔2012〕2119号），基金债券免收",
        source_doc="14号台账A4/T1 + backtest/fees.py"
    ),

    # ---- 交易规则类（14号台账B） ----
    LedgerItem(
        name="涨跌停档位",
        category=LedgerCategory.RULE,
        last_check_date=date(2026, 8, 28),
        check_frequency=CheckFrequency.QUARTERLY,
        expiry_date=None,
        notes="主板10%/双创20%/北交所30%；主板ST 10%（2026-07-06起）",
        source_doc="14号台账B1 + backtest/matching.py"
    ),
]


def get_items_by_frequency(frequency: CheckFrequency) -> list[LedgerItem]:
    """按频率筛选台账项"""
    return [item for item in LEDGER_ITEMS if item.check_frequency == frequency]


def get_items_by_category(category: LedgerCategory) -> list[LedgerItem]:
    """按类别筛选台账项"""
    return [item for item in LEDGER_ITEMS if item.category == category]


def get_item_by_name(name: str) -> Optional[LedgerItem]:
    """按名称查找台账项"""
    for item in LEDGER_ITEMS:
        if item.name == name:
            return item
    return None
