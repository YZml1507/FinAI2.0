#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L0 数据源适配层。

⛔ 此后取数脚本**不得**直接 `import baostock / mootdx / akshare / tdxpy`，
必须经本层，否则 `DATA_PLATFORM_REBUILD_PLAN_20260806.md` §3.1 列的 6 类陷阱
会在每个新脚本里重新出现（R28 中我每一类都踩过至少一次）。
"""
from __future__ import annotations

from finai.sources.base import (
    CONCLUSIVE,
    EMPTY_OK,
    FAIL_DETERMINISTIC,
    FAIL_GATEWAY,
    FAIL_PROBE_BUG,
    FAIL_UNREACHABLE,
    OK,
    FetchResult,
    SourceFetchError,
    classify_exception,
    force_ipv4,
    harden_requests_session,
    make_result,
)

#: 可选数据源登记表 —— 「等需要拉时可以作为一个选项被选取」的那份清单。
#:
#: ⛔ **登记 ≠ 可入库**。`verified` 为 False 的源只证了"取得到、有行数字段"，
#:   **没有**与已有源交叉对账，**没有**做 PIT 检查。
#:   按 `FINDING-232`（330 只 BJ 标的被误映射 `.SH`）的教训，入库前必须双源对账。
#:   CLAUDE.md：**A green gate does not mean the data is correct.**
#:
#: ⚠ `covers` 写的是**实测覆盖**，`not_covered` 写的是**实测不覆盖**（负面结论同样重要
#:   —— 商家宣称支持历史分钟线，实测 5min 完全缺失，见 `FINDING-243`）。
SOURCE_REGISTRY: dict[str, dict] = {
    "tdx": {
        "module": "finai.sources.tdx_source",
        "covers": ["A股 5min/15min/1min/daily（沪深北）"],
        "not_covered": ["A股之外的市场"],
        "needs_credential": False,
        # ⛔ R5：TDX 腿已砍（v1 仅日线，`finai.tdx_minute5`/`data_catalog` 未搬入）。
        #   调用即 ModuleNotFoundError —— 这是预期，不是回归。
        "verified": False,
        "note": "⛔ R5：TDX 腿已砍（v1 仅日线，data_catalog 未搬入）；"
                "如未来需要分钟线再行恢复（R5 方案 A）。"
                "（历史实测：48 根/日栅格合规；首根 09:35、末根 15:00）",
    },
    "tdx_ext": {
        "module": "finai.sources.tdx_ext_source",
        "covers": ["港股 5min（实测回溯至 2015-11-30）", "期货/期权 5min（受合约生命周期限制）",
                   "开放式基金/货币基金 daily", "宏观指标 99 条", "香港指数", "逐笔成交"],
        "not_covered": ["A股（那是 `tdx` 的职责，走 hq 协议）",
                        "美股/中概股（`get_markets` 报了 market=74/40，但目录样本里未出现 ⇒ 未证实也未否证）"],
        "needs_credential": False,
        "verified": False,  # ⛔ 只验了可取，未验正确性
        "note": "FINDING-246/247/250/251；活主机 47.112.95.207:7720",
    },
    "citydata": {
        "module": "finai.sources.citydata_source",
        "covers": ["114 个 tushare 接口 / 18 个数据族（日线·复权因子·筹码分布·"
                   "资金流·财务·指数·基金·期货·宏观等）"],
        "not_covered": ["5min（实测完全缺失）", "1min（仅约半年，回测深度不足）"],
        "needs_credential": True,   # CITYDATA_TS_TOKEN + CITYDATA_PROXY
        "verified": False,
        "note": "⛔ 代理不得更换（商家明示：换了账号会被封）；无直连兜底。FINDING-243",
    },
    "baostock": {
        "module": "finai.sources.baostock_source",
        "covers": ["A股 daily（复权）"],
        "not_covered": [],
        "needs_credential": False,
        "verified": True,
        "note": "⚠ 会挂起主进程，必须子进程隔离（FINDING-181）",
    },
    "catalog": {
        "module": "finai.sources.catalog_source",
        "covers": ["**按接口名直调**（实测 2026-08-09: 672 条）—— 宏观 158 / 基金ETF 79 / "
                   "指数 68 / 股东股本 44 / 快照 36 / 新闻研报 26 / QVIX 18 / 舆情 5 等。"
                   "⛔ 数字随打点推进会变，按 `callable_here` 字段过滤而非引用此数"],
        "not_covered": [
            "58 条**按设计**不可直调：baostock 22（须子进程隔离）+ "
            "mootdx 20 + tdxpy 16（须会话初始化）⇒ 走各自专用适配器",
            "5 条 off_plan 记录（产物明令不得计入分子）⇒ 需显式 in_plan=False 才可见",
            "港股/美股/期货/期权（按设计未实测，见 overseas_registry）",
        ],
        "needs_credential": False,
        "verified": True,   # 参数来自实测产物，非现编
        "note": "⭐ 补的是真缺口：此前只在文档表格里，**无代码能按名调用**。"
                "默认复用打点器实测成功的入参（FINDING-252）。"
                "⛔ 口径：`usable()` 与门禁 CHECK 10 **同口径**（实测 730）；其中可直调 672。"
                "此前登记表写「734 条直调」是两处夸大之和（FINDING-257/260）",
    },
    "overseas": {
        "module": "finai.sources.overseas_registry",
        "covers": ["⛔ **不取数，只登记**：港股 53 / 美股 67 / 期货 104 / 期权 33 / 加密外汇 37"],
        "not_covered": ["韩股 —— 实测枚举清单里**一条都没有**，扩展需引入新数据源"],
        "needs_credential": False,
        "verified": False,  # ⛔ 按设计未实测
        "note": "用户 2026-08-08：其他股市等后续扩展，接口先留着并说明。"
                "⚠ 唯一例外：港股/期货/期权已有 tdx_ext 这条**已实测**的独立通路",
    },
}

__all__ = [
    "CONCLUSIVE",
    "EMPTY_OK",
    "FAIL_DETERMINISTIC",
    "FAIL_GATEWAY",
    "FAIL_PROBE_BUG",
    "FAIL_UNREACHABLE",
    "OK",
    "SOURCE_REGISTRY",
    "FetchResult",
    "SourceFetchError",
    "classify_exception",
    "force_ipv4",
    "harden_requests_session",
    "make_result",
]
