#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对枚举出的数据接口做**四态实测打点**，产出可查的接口矩阵。

背景（用户 2026-08-05 要求，理由已核实成立）：
  不是只调通当前需要的几类，而是把接口都打点一遍并留档；
  以后需要某类数据直接查表，避免后期遇到盈利瓶颈时被"数据不足"困住，
  也避免成本计算漏项。本项目已证伪 8 条盈利路径，台账明写
  「再加价格派生因子已走不通，需要结构性不同的东西」——找新信息轴的前提是知道有哪些轴。

⭐ 四态契约（FINDING-175 定契约 / FINDING-179 修正网关态）：
  OK                  拿到数据（行数 > 0）——**永远可信**，行数与字段无法伪造
  EMPTY_OK            调用成功但 0 行（可能真无数据，也可能静默失败）——**不可作为"无数据"结论**
  FAIL_DETERMINISTIC  端点/库给出明确语义报错（无权限、参数错、需 token）——可信
  FAIL_GATEWAY        502/503/504 或空 body（代理网关失败，请求可能没到端点）——**不可解释**
  FAIL_UNREACHABLE    连接层失败 / 超时 / SSLError ——**不可解释**

⛔ 硬约束（全部来自本轮实测教训，违反即重蹈）：
  1. 不得把 FAIL_UNREACHABLE 记为"接口不可用"（FINDING-175）
  2. 静默 0 行必须单独成态，不得当 OK（FINDING-178 mootdx 默认服务器返回 0 行不抛异常）
  3. 每次调用必须有**外部超时**——baostock 实测能挂死且 socket.setdefaulttimeout 约束不住它
     （FINDING-181），故用子进程隔离
  4. 解析类异常（JSONDecodeError 等）必须记原始异常文本，它常是 502 空体伪装的
     （FINDING-179：akshare 与 efinance 报的 JSONDecodeError 逐字相同，真因是代理 502）
  5. 打点前必须先跑网络基线，否则失败结果不可解释

用法:
  python scripts/probe_data_interfaces.py --batch B1          # 只打 B1 批
  python scripts/probe_data_interfaces.py --batch B1 --dry-run # 只列将测什么
  python scripts/probe_data_interfaces.py --list-batches
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import queue as _queue
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

OUT = Path("artifacts/interface_matrix")

# ---------------------------------------------------------------- 四态判定

STATE_OK = "OK"
STATE_EMPTY = "EMPTY_OK"
STATE_DET = "FAIL_DETERMINISTIC"
STATE_GATEWAY = "FAIL_GATEWAY"
STATE_UNREACH = "FAIL_UNREACHABLE"

# 明确语义的报错关键词 -> FAIL_DETERMINISTIC（端点真的回答了我们）
DETERMINISTIC_MARKERS = (
    "api not purchased", "没有权限", "无权限", "权限不足", "积分不足",
    "token", "抱歉", "不存在", "invalid", "参数", "param",
    "每分钟最多访问", "访问频率", "rate limit", "超过", "限制",
    "not found", "404", "403", "401",
)
# 网关/代理失败 -> FAIL_GATEWAY（请求可能根本没到端点）
GATEWAY_MARKERS = ("502", "503", "504", "bad gateway", "gateway time")
# 连接层 -> FAIL_UNREACHABLE
UNREACH_MARKERS = (
    "connectionerror", "connectionaborted", "remotedisconnected", "timeout",
    "timed out", "sslerror", "ssleoferror", "max retries", "connection closed",
    "connection reset", "nameresolution", "failed to perform",
)


def classify(exc_name: str, exc_text: str) -> str:
    """把异常归入四态之一。顺序敏感：网关 > 连接层 > 语义。"""
    blob = f"{exc_name} {exc_text}".lower()
    for m in GATEWAY_MARKERS:
        if m in blob:
            return STATE_GATEWAY
    for m in UNREACH_MARKERS:
        if m in blob:
            return STATE_UNREACH
    for m in DETERMINISTIC_MARKERS:
        if m in blob:
            return STATE_DET
    # JSONDecodeError 等解析错：常是 502 空体伪装（FINDING-179），不可解释
    if "jsondecode" in blob or "expecting value" in blob:
        return STATE_GATEWAY
    return STATE_UNREACH  # 默认最保守：不可解释


# ---------------------------------------------------------------- 待打点清单
# 每项: (批次, 库, 显示名, 数据种类, 可调用表达式, kwargs)
# ⚠ 表达式在子进程里 eval，必须自包含。不用 lambda 是为了能序列化。

PROBES: list[tuple[str, str, str, str, str, dict]] = [
    # ---- B1: 直接对应 6 条 staging 缺陷 与 FINDING-113/-133 ----
    ("B1", "mootdx", "mootdx.bars 5min(pin)", "分钟/日内",
     "__import__('mootdx.quotes',fromlist=['Quotes']).Quotes.factory("
     "market='std', server=('60.12.136.250',7709)).bars",
     {"symbol": "000001", "frequency": 0, "offset": 48}),
    ("B1", "mootdx", "mootdx.bars daily(pin)", "行情-日频",
     "__import__('mootdx.quotes',fromlist=['Quotes']).Quotes.factory("
     "market='std', server=('60.12.136.250',7709)).bars",
     {"symbol": "000001", "frequency": 9, "offset": 20}),
    ("B1", "baostock", "bs 5min 2026", "分钟/日内", "BS_KLINE",
     {"code": "sz.000001", "fields": "date,time,open,high,low,close,volume,amount",
      "start_date": "2026-07-24", "end_date": "2026-07-24", "frequency": "5"}),
    ("B1", "baostock", "bs 5min 2015(深度)", "分钟/日内", "BS_KLINE",
     {"code": "sz.000001", "fields": "date,time,close",
      "start_date": "2015-06-15", "end_date": "2015-06-15", "frequency": "5"}),
    ("B1", "baostock", "bs daily tradestatus+isST 2010", "停牌/退市/ST", "BS_KLINE",
     {"code": "sz.000001", "fields": "date,close,tradestatus,isST",
      "start_date": "2010-01-04", "end_date": "2010-01-15", "frequency": "d"}),
    ("B1", "baostock", "bs 复权因子 2010-2015", "复权因子", "BS_ADJ",
     {"code": "sz.000001", "start_date": "2010-01-01", "end_date": "2015-12-31"}),
    ("B1", "baostock", "bs 分红 税前/税后", "分红送转", "BS_DIV",
     {"code": "sz.000001", "year": "2014", "yearType": "report"}),
    ("B1", "baostock", "bs 历史宇宙(生存者偏差)", "交易日历/宇宙", "BS_ALLSTOCK",
     {"day": "2010-01-04"}),
    ("B1", "akshare", "ak 停牌 tfp 20180418", "停牌/退市/ST",
     "AK.stock_tfp_em", {"date": "20180418"}),
    ("B1", "akshare", "ak 涨停池 20260804", "涨跌停",
     "AK.stock_zt_pool_em", {"date": "20260804"}),
    # FINDING-184: 我最初写 stock_dt_pool_em —— 该函数不存在。实测正确名是 stock_zt_pool_dtgc_em
    ("B1", "akshare", "ak 跌停池 20260804", "涨跌停",
     "AK.stock_zt_pool_dtgc_em", {"date": "20260804"}),
    ("B1", "akshare", "ak 复权因子 hfq", "复权因子",
     "AK.stock_zh_a_daily", {"symbol": "sz000001", "adjust": "hfq-factor"}),
    ("B1", "akshare", "ak 分红送配详情", "分红送转",
     "AK.stock_fhps_detail_em", {"symbol": "000001"}),
    ("B1", "akshare", "ak 深市更名史", "停牌/退市/ST",
     "AK.stock_info_sz_change_name", {"symbol": "全称变更"}),
    ("B1", "akshare", "ak 深市退市", "停牌/退市/ST",
     "AK.stock_info_sz_delist", {"symbol": "终止上市公司"}),
    ("B1", "akshare", "ak 沪市退市", "停牌/退市/ST",
     "AK.stock_info_sh_delist", {}),
    ("B1", "akshare", "ak 5min em(已知 push2his 失败)", "分钟/日内",
     "AK.stock_zh_a_hist_min_em",
     {"symbol": "000001", "period": "5", "start_date": "2026-07-24 09:30:00",
      "end_date": "2026-07-24 15:00:00", "adjust": ""}),
    ("B1", "efinance", "ef 历史资金流", "资金流",
     "EF.stock.get_history_bill", {"stock_code": "000001"}),
    ("B1", "efinance", "ef 日线(走 push2his)", "行情-日频",
     "EF.stock.get_quote_history",
     {"stock_codes": "000001", "beg": "20260701", "end": "20260804"}),
    ("B1", "adata", "adata 5min", "分钟/日内", "ADATA_MARKET",
     {"stock_code": "000001", "start_date": "2026-07-24", "k_type": 5}),
    ("B1", "adata", "adata 分红", "分红送转", "ADATA_DIV",
     {"stock_code": "000001"}),

    # ---- B2: 回测地基 — 交易日历宇宙 / 行情日频 / 指数 / 财务三表 / 财务指标 / 业绩预告快报 ----
    ("B2", "baostock", "bs 交易日历 2010-2020", "交易日历/宇宙", "BS_CALENDAR",
     {"start_date": "2010-01-01", "end_date": "2010-03-31"}),
    ("B2", "baostock", "bs 股票列表(当日)", "交易日历/宇宙", "BS_ALLSTOCK",
     {"day": "2026-07-01"}),
    ("B2", "baostock", "bs 日线 qfq 2010", "行情-日频", "BS_KLINE",
     {"code": "sz.000001", "fields": "date,open,high,low,close,preclose,volume,amount,turn,isST",
      "start_date": "2010-01-04", "end_date": "2010-03-31", "frequency": "d",
      "adjustflag": "2"}),
    ("B2", "baostock", "bs 日线 raw 2026", "行情-日频", "BS_KLINE",
     {"code": "sz.000001", "fields": "date,open,high,low,close,preclose,volume,amount",
      "start_date": "2026-07-01", "end_date": "2026-07-31", "frequency": "d",
      "adjustflag": "3"}),
    ("B2", "baostock", "bs 指数日线 000001.SH", "指数", "BS_KLINE",
     {"code": "sh.000001", "fields": "date,close,pctChg,volume",
      "start_date": "2026-01-01", "end_date": "2026-07-31", "frequency": "d",
      "adjustflag": "3"}),
    ("B2", "baostock", "bs 利润表 2020", "财务报表", "BS_PROFIT",
     {"code": "sh.600519", "year": "2020", "quarter": "4"}),
    ("B2", "baostock", "bs 资产负债表 2020", "财务报表", "BS_BALANCE",
     {"code": "sh.600519", "year": "2020", "quarter": "4"}),
    ("B2", "baostock", "bs 现金流量表 2020", "财务报表", "BS_CASHFLOW",
     {"code": "sh.600519", "year": "2020", "quarter": "4"}),
    ("B2", "baostock", "bs 财务指标 2020", "财务指标", "BS_FINI",
     {"code": "sh.600519", "year": "2020", "quarter": "4"}),
    ("B2", "akshare", "ak A股日线(东财) 000001", "行情-日频",
     "AK.stock_zh_a_hist",
     {"symbol": "000001", "period": "daily", "start_date": "20260701",
      "end_date": "20260804", "adjust": ""}),
    ("B2", "akshare", "ak 全A日线(前复权) 000001", "行情-日频",
     "AK.stock_zh_a_hist",
     {"symbol": "000001", "period": "daily", "start_date": "20100104",
      "end_date": "20100131", "adjust": "qfq"}),
    ("B2", "akshare", "ak 交易日历", "交易日历/宇宙",
     "AK.tool_trade_date_hist_sina", {}),
    ("B2", "akshare", "ak A股代码宇宙", "交易日历/宇宙",
     "AK.stock_info_a_code_name", {}),
    ("B2", "akshare", "ak 业绩预告(EM)", "业绩预告/快报",
     "AK.stock_profit_forecast_em",
     {"symbol": "全部"}),
    ("B2", "akshare", "ak 上市公司利润表", "财务报表",
     "AK.stock_financial_report_sina",
     {"stock": "600519", "symbol": "利润表"}),
    ("B2", "akshare", "ak 指数成分权重 沪深300", "指数",
     "AK.index_stock_cons_weight_csindex",
     {"symbol": "000300"}),
    ("B2", "akshare", "ak 指数日线行情 000300", "指数",
     "AK.stock_zh_index_daily",
     {"symbol": "sh000300"}),
    ("B2", "tushare", "ts 交易日历", "交易日历/宇宙",
     "TS_TRADE_CAL",
     {"exchange": "SSE", "start_date": "20100101", "end_date": "20100331"}),
    ("B2", "tushare", "ts 股票基本信息", "交易日历/宇宙",
     "TS_STOCK_BASIC",
     {"list_status": "L"}),
    ("B2", "tushare", "ts 日线行情 000001.SZ", "行情-日频",
     "TS_DAILY",
     {"ts_code": "000001.SZ", "start_date": "20260701", "end_date": "20260804"}),
    ("B2", "tushare", "ts 利润表 600519.SH", "财务报表",
     "TS_INCOME",
     {"ts_code": "600519.SH", "period": "20231231"}),
    ("B2", "tushare", "ts 资产负债表 600519.SH", "财务报表",
     "TS_BALANCESHEET",
     {"ts_code": "600519.SH", "period": "20231231"}),
    ("B2", "tushare", "ts 业绩快报 2023", "业绩预告/快报",
     "TS_EXPRESS",
     {"start_date": "20240101", "end_date": "20240401"}),

    # ---- B3: 候选新信息轴 — 股东/股本 / 资金流 / 龙虎榜 / 融资融券 / 板块概念行业 ----
    ("B3", "akshare", "ak 龙虎榜 20260804", "龙虎榜",
     "AK.stock_lhb_detail_daily_sina",
     {"date": "20260804"}),
    ("B3", "akshare", "ak 龙虎榜机构买方", "龙虎榜",
     "AK.stock_lhb_hyyyb_em",
     {"start_date": "20260801", "end_date": "20260804"}),
    ("B3", "akshare", "ak 融资融券余额 20260804", "融资融券",
     "AK.stock_margin_detail_szse",
     {"date": "20260804"}),
    ("B3", "akshare", "ak 融资融券汇总", "融资融券",
     "AK.stock_margin_account_info", {}),
    ("B3", "akshare", "ak 大股东持股人数 最近季", "股东/股本",
     "AK.stock_hold_num_cninfo",
     {"date": "20260630"}),
    ("B3", "akshare", "ak 十大流通股东 000001", "股东/股本",
     "AK.stock_gdfx_free_top_10_em",
     {"symbol": "000001", "date": "20231231"}),
    ("B3", "akshare", "ak 个股资金流(近3月)", "资金流",
     "AK.stock_individual_fund_flow",
     {"stock": "000001", "market": "sz"}),
    ("B3", "akshare", "ak 板块资金流(行业)", "资金流",
     "AK.stock_sector_fund_flow_rank",
     {"indicator": "今日", "sector_type": "行业资金流"}),
    ("B3", "akshare", "ak 概念板块成分", "板块/概念/行业",
     "AK.stock_board_concept_cons_em",
     {"symbol": "芯片"}),
    ("B3", "akshare", "ak 行业分类", "板块/概念/行业",
     "AK.stock_board_industry_name_em", {}),
    ("B3", "akshare", "ak 指数成分 上证50", "指数",
     "AK.index_stock_cons",
     {"symbol": "000016"}),
    ("B3", "efinance", "ef 资金流历史 000001", "资金流",
     "EF.stock.get_history_bill",
     {"stock_code": "000001"}),
    ("B3", "tushare", "ts 融资融券 000001.SZ 2026", "融资融券",
     "TS_MARGIN_DETAIL",
     {"ts_code": "000001.SZ", "start_date": "20260701", "end_date": "20260804"}),
    ("B3", "tushare", "ts 龙虎榜 20260804", "龙虎榜",
     "TS_LHB",
     {"trade_date": "20260804"}),
    ("B3", "tushare", "ts 股东人数", "股东/股本",
     "TS_STKHOLDER_NUM",
     {"ts_code": "000001.SZ", "startdate": "20230101", "enddate": "20261231"}),
    ("B3", "tushare", "ts 行业分类(申万)", "板块/概念/行业",
     "TS_INDEX_CLASSIFY",
     {"level": "L1", "src": "SW2021"}),
    ("B3", "baostock", "bs 行业分类信息", "板块/概念/行业", "BS_INDUSTRY",
     {"code": "sh.600519"}),

    # ---- B4: 宏观 / 基金ETF / 债券可转债（代表性抽样，不全量）----
    ("B4", "akshare", "ak 宏观PMI", "宏观",
     "AK.macro_china_pmi_yearly", {}),
    ("B4", "akshare", "ak 宏观SHIBOR", "宏观",
     "AK.macro_china_shibor_all", {}),
    ("B4", "akshare", "ak 宏观CPI", "宏观",
     "AK.macro_china_cpi_yearly", {}),
    ("B4", "akshare", "ak 宏观GDP", "宏观",
     "AK.macro_china_gdp_yearly", {}),
    ("B4", "akshare", "ak 基金日线 510050", "基金/ETF",
     "AK.fund_etf_hist_em",
     {"symbol": "510050", "period": "daily",
      "start_date": "20260701", "end_date": "20260804"}),
    ("B4", "akshare", "ak 基金代码列表", "基金/ETF",
     "AK.fund_name_em", {}),
    ("B4", "akshare", "ak ETF日线成交", "基金/ETF",
     "AK.fund_etf_fund_daily_em", {}),
    ("B4", "akshare", "ak 可转债列表", "债券/可转债",
     "AK.bond_cb_jsl", {}),
    ("B4", "akshare", "ak 可转债日线 110059", "债券/可转债",
     "AK.bond_zh_hs_cov_daily", {"symbol": "sh110059"}),
    ("B4", "baostock", "bs 宏观利率 lpr", "宏观", "BS_LPR",
     {"start_date": "2020-01-01", "end_date": "2026-07-31"}),
]

# 需要特殊调用序列的探针（不能用单个表达式表达）
SPECIAL = {"BS_KLINE", "BS_ADJ", "BS_DIV", "BS_ALLSTOCK", "ADATA_MARKET", "ADATA_DIV",
           "BS_CALENDAR", "BS_PROFIT", "BS_BALANCE", "BS_CASHFLOW", "BS_FINI", "BS_INDUSTRY",
           "BS_LPR",
           "TS_TRADE_CAL", "TS_STOCK_BASIC", "TS_DAILY", "TS_INCOME", "TS_BALANCESHEET",
           "TS_EXPRESS", "TS_MARGIN_DETAIL", "TS_LHB", "TS_STKHOLDER_NUM", "TS_INDEX_CLASSIFY"}


# ---------------------------------------------------------------- 子进程执行体

def _describe(obj) -> dict:
    """把返回值压成可 JSON 化的证据：行数 + 字段 + 首行。不返回大对象。"""
    import pandas as pd  # noqa: PLC0415
    if obj is None:
        return {"rows": 0, "cols": None, "note": "returned None"}
    if isinstance(obj, pd.DataFrame):
        n = len(obj)
        return {
            "rows": n,
            "cols": [str(c) for c in obj.columns][:40],
            "first_row": {str(k): str(v)[:60] for k, v in obj.iloc[0].items()} if n else None,
            "dtypes": {str(c): str(t) for c, t in list(obj.dtypes.items())[:12]},
        }
    if isinstance(obj, (list, tuple)):
        return {"rows": len(obj), "cols": None, "first_row": str(obj[0])[:200] if obj else None}
    return {"rows": 1, "cols": None, "first_row": str(obj)[:200]}


def _run_special(kind: str, kw: dict):
    """baostock / adata 的多步调用序列。"""
    if kind.startswith("BS_"):
        import baostock as bs  # noqa: PLC0415
        bs.login()
        try:
            if kind == "BS_KLINE":
                rs = bs.query_history_k_data_plus(
                    kw["code"], kw["fields"], start_date=kw["start_date"],
                    end_date=kw["end_date"], frequency=kw["frequency"],
                    adjustflag=kw.get("adjustflag", "3"))
            elif kind == "BS_ADJ":
                rs = bs.query_adjust_factor(code=kw["code"], start_date=kw["start_date"],
                                            end_date=kw["end_date"])
            elif kind == "BS_DIV":
                rs = bs.query_dividend_data(code=kw["code"], year=kw["year"],
                                            yearType=kw["yearType"])
            elif kind == "BS_ALLSTOCK":
                rs = bs.query_all_stock(day=kw["day"])
            elif kind == "BS_CALENDAR":
                rs = bs.query_trade_dates(start_date=kw["start_date"], end_date=kw["end_date"])
            elif kind == "BS_PROFIT":
                rs = bs.query_profit_data(code=kw["code"], year=kw["year"], quarter=kw["quarter"])
            elif kind == "BS_BALANCE":
                rs = bs.query_balance_data(code=kw["code"], year=kw["year"], quarter=kw["quarter"])
            elif kind == "BS_CASHFLOW":
                rs = bs.query_cash_flow_data(code=kw["code"], year=kw["year"], quarter=kw["quarter"])
            elif kind == "BS_FINI":
                rs = bs.query_growth_data(code=kw["code"], year=kw["year"], quarter=kw["quarter"])
            elif kind == "BS_INDUSTRY":
                rs = bs.query_stock_industry(code=kw["code"])
            else:
                raise ValueError(kind)
            if rs.error_code != "0":
                raise RuntimeError(f"baostock error_code={rs.error_code} msg={rs.error_msg}")
            # ⭐ FINDING-185: `rs.next()` **只在消费行数据时推进游标**。
            # 不调 get_row_data() 就永远返回 True —— 是死循环而非阻塞
            # （实测: no_consume 跑到 9000 次仍不停，真实只有 1976 行；
            #  consume 则 1976 行 / 0% 重复 / 6.6s 自然终止）。
            # ⛔ 故必须逐行消费。上限只作保险，且**触发即报错**，
            #    不得静默截断 —— 我第一版写成静默上限，让死循环吐出 20000 行垃圾。
            # ⛔ 另: rs.get_data() 实测挂死，不可用。
            rows = []
            LIMIT = 60000
            while rs.next():
                rows.append(rs.get_row_data())
                if len(rows) > LIMIT:
                    raise RuntimeError(
                        f"baostock 游标未终止: 已取 {len(rows)} 行仍未结束 —— "
                        "疑似未消费行数据导致的死循环，不得当作真实数据")
            import pandas as pd  # noqa: PLC0415
            return pd.DataFrame(rows, columns=rs.fields) if rows else pd.DataFrame(columns=rs.fields)
        finally:
            try:
                bs.logout()
            except Exception:  # noqa: BLE001, S110
                pass
    if kind == "ADATA_MARKET":
        import adata  # noqa: PLC0415
        return adata.stock.market.get_market(**kw)
    if kind == "ADATA_DIV":
        import adata  # noqa: PLC0415
        return adata.stock.market.get_dividend(**kw)
    # B2: baostock financial statements
    if kind in ("BS_CALENDAR", "BS_PROFIT", "BS_BALANCE", "BS_CASHFLOW", "BS_FINI", "BS_INDUSTRY"):
        import baostock as bs  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
        bs.login()
        try:
            if kind == "BS_CALENDAR":
                rs = bs.query_trade_dates(start_date=kw["start_date"], end_date=kw["end_date"])
            elif kind == "BS_PROFIT":
                rs = bs.query_profit_data(code=kw["code"], year=kw["year"], quarter=kw["quarter"])
            elif kind == "BS_BALANCE":
                rs = bs.query_balance_data(code=kw["code"], year=kw["year"], quarter=kw["quarter"])
            elif kind == "BS_CASHFLOW":
                rs = bs.query_cash_flow_data(code=kw["code"], year=kw["year"], quarter=kw["quarter"])
            elif kind == "BS_FINI":
                rs = bs.query_growth_data(code=kw["code"], year=kw["year"], quarter=kw["quarter"])
            elif kind == "BS_INDUSTRY":
                rs = bs.query_stock_industry(code=kw["code"])
            elif kind == "BS_LPR":
                rs = bs.query_lpr_data(start_date=kw["start_date"], end_date=kw["end_date"])
            else:
                raise ValueError(kind)
            if rs.error_code != "0":
                raise RuntimeError(f"baostock error_code={rs.error_code} msg={rs.error_msg}")
            rows = []
            LIMIT = 60000
            while rs.next():
                rows.append(rs.get_row_data())
                if len(rows) > LIMIT:
                    raise RuntimeError(f"baostock 游标未终止: 已取 {len(rows)} 行")
            return pd.DataFrame(rows, columns=rs.fields) if rows else pd.DataFrame(columns=rs.fields)
        finally:
            try:
                bs.logout()
            except Exception:  # noqa: BLE001
                pass
    # B2/B3: tushare API calls via finai.tushare_primary (project proxy endpoint)
    if kind.startswith("TS_"):
        import sys as _sys  # noqa: PLC0415
        _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
        from finai.tushare_primary import pro_api_compatible, primary_is_configured  # noqa: PLC0415
        if not primary_is_configured():
            raise RuntimeError("INDEVS_TUSHARE_KEY not set — FAIL_DETERMINISTIC: needs proxy key")
        pro = pro_api_compatible()
        import pandas as pd  # noqa: PLC0415
        import time  # noqa: PLC0415
        time.sleep(0.5)  # rate limit: proxy允许 2 IP / 同时段
        try:
            if kind == "TS_TRADE_CAL":
                return pro.trade_cal(**kw)
            elif kind == "TS_STOCK_BASIC":
                return pro.stock_basic(**kw)
            elif kind == "TS_DAILY":
                return pro.daily(**kw)
            elif kind == "TS_INCOME":
                return pro.income(**kw)
            elif kind == "TS_BALANCESHEET":
                return pro.balancesheet(**kw)
            elif kind == "TS_EXPRESS":
                return pro.express(**kw)
            elif kind == "TS_MARGIN_DETAIL":
                return pro.margin_detail(**kw)
            elif kind == "TS_LHB":
                return pro.top_list(**kw)
            elif kind == "TS_STKHOLDER_NUM":
                return pro.stk_holdernumber(**kw)
            elif kind == "TS_INDEX_CLASSIFY":
                return pro.index_classify(**kw)
            else:
                raise ValueError(kind)
        except Exception as e:
            err = str(e).lower()
            if ("权限" in err or "积分不足" in err or "not purchased" in err
                    or "active_ip_limit" in err or "ip_limit" in err
                    or "403" in err or "401" in err or "invalid" in err):
                raise RuntimeError(f"TS_DETERMINISTIC: {e}")
            raise
    raise ValueError(kind)


def _worker(expr: str, kw: dict, q) -> None:
    """在子进程里执行一次探测。子进程隔离是硬要求：baostock 实测能挂死主进程。"""
    import socket
    socket.setdefaulttimeout(25)
    try:
        # 关代理：本轮实测开代理严格更差（弄坏 3 个端点、修好 0 个）
        import requests
        _init = requests.sessions.Session.__init__

        def patched(self, *a, **k):
            _init(self, *a, **k)
            self.trust_env = False
        requests.sessions.Session.__init__ = patched
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        if expr in SPECIAL:
            obj = _run_special(expr, kw)
        else:
            ns: dict = {}
            if expr.startswith("AK."):
                import akshare
                ns["AK"] = akshare
            elif expr.startswith("EF."):
                import efinance
                ns["EF"] = efinance
            fn = eval(expr, ns)  # noqa: S307 — 表达式是本文件内的常量，非外部输入
            obj = fn(**kw)
        q.put(("RESULT", _describe(obj)))
    except BaseException as exc:  # noqa: BLE001
        q.put(("EXC", type(exc).__name__, str(exc)[:400],
               traceback.format_exc()[-500:]))


# ---------------------------------------------------------------- 主流程

def probe_one(expr: str, kw: dict, timeout: int = 70) -> dict:
    """跑一个探针，带外部超时（FINDING-181: baostock 能挂死且 socket 超时约束不住）。"""
    q: mp.Queue = mp.Queue()
    p = mp.Process(target=_worker, args=(expr, kw, q), daemon=True)
    p.start()
    try:
        kind, *rest = q.get(timeout=timeout)
    except (_queue.Empty, EOFError):
        p.terminate()
        p.join(5)
        return {"state": STATE_UNREACH, "rows": None,
                "error": f"HUNG_NO_RESULT_IN_{timeout}s",
                "note": "子进程超时被杀 —— 不可解释为接口不可用"}
    finally:
        if p.is_alive():
            p.terminate()
            p.join(5)

    if kind == "RESULT":
        d = rest[0]
        rows = d.get("rows") or 0
        return {"state": STATE_OK if rows > 0 else STATE_EMPTY, **d}
    exc_name, exc_text, tb = rest
    return {"state": classify(exc_name, exc_text), "rows": None,
            "error": f"{exc_name}: {exc_text[:200]}", "traceback_tail": tb[-200:]}


def network_gate() -> dict:
    """打点前的网络基线。⛔ 不通过则挂起 —— 否则失败结果不可解释（FINDING-175）。"""
    import requests
    checks = {
        "baidu(境内基准)": "http://www.baidu.com",
        "datacenter-web(东财可用面)": "https://datacenter-web.eastmoney.com/api/data/v1/get",
    }
    res = {}
    for name, url in checks.items():
        ok = 0
        for _ in range(3):
            s = requests.Session()
            s.trust_env = False
            try:
                r = s.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                ok += (r.status_code < 500)
            except Exception:  # noqa: BLE001
                pass
        res[name] = f"{ok}/3"
    tcp_ok = 0
    import socket
    for host in ("60.12.136.250", "180.153.18.170"):
        try:
            s = socket.create_connection((host, 7709), timeout=8)
            s.close()
            tcp_ok += 1
        except Exception:  # noqa: BLE001
            pass
    res["TDX可连主机"] = f"{tcp_ok}/2"
    res["_pass"] = all(v.startswith(("2/", "3/")) for k, v in res.items() if not k.startswith("_"))
    return res


def main() -> int:
    # ⛔ LESSONS §25.7 / FINDING-205: 不得在打点扫描运行期间对同一批端点做对照测量。
    #    实测代价：我一边跑 1,029 个探针，一边测 macro_china_cpi_yearly，
    #    两次(35s/150s)都失败，而单独跑时 49 秒成功 —— 我差点记成"接口不可用"。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from finai.probe_guard import assert_no_concurrent_probe
    assert_no_concurrent_probe("probe_data_interfaces.py")

    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default="B1")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list-batches", action="store_true")
    ap.add_argument("--timeout", type=int, default=70)
    ap.add_argument("--force", action="store_true", help="网络门禁未过也继续（结果会标记）")
    args = ap.parse_args()

    batches = sorted({p[0] for p in PROBES})
    if args.list_batches:
        for b in batches:
            print(f"  {b}: {sum(1 for p in PROBES if p[0] == b)} 个探针")
        return 0

    sel = [p for p in PROBES if p[0] == args.batch]
    if not sel:
        print(f"批次 {args.batch} 无探针。可用: {batches}")
        return 1

    if args.dry_run:
        print(f"=== DRY RUN: 批次 {args.batch} 共 {len(sel)} 个探针 ===")
        for _b, lib, name, cat, expr, kw in sel:
            print(f"  {lib:9s} {name:34s} [{cat}]  {expr[:40]} {list(kw)[:4]}")
        return 0

    print("=== 网络门禁（打点前必跑）===")
    gate = network_gate()
    for k, v in gate.items():
        if not k.startswith("_"):
            print(f"  {k:28s} {v}")
    if not gate["_pass"]:
        print("\n⛔ 网络门禁未通过 —— 此状态下失败结果不可解释。")
        if not args.force:
            print("   已挂起。确认网络后重跑，或加 --force（结果会标 network_degraded）。")
            return 2
        print("   --force 指定，继续但标记 network_degraded。")

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"\n=== 打点 批次 {args.batch}（{len(sel)} 个，每个外部超时 {args.timeout}s）===\n")
    print(f"{'库':9s} {'接口':34s} {'状态':20s} 行数    证据")
    print("-" * 108)
    records = []
    for _b, lib, name, cat, expr, kw in sel:
        r = probe_one(expr, kw, timeout=args.timeout)
        rows = r.get("rows")
        cols = r.get("cols") or []
        ev = ""
        if r["state"] == STATE_OK:
            ev = f"cols={cols[:5]}"
        elif r.get("error"):
            ev = r["error"][:52]
        print(f"{lib:9s} {name:34s} {r['state']:20s} {str(rows):7s} {ev}")
        records.append({"batch": _b, "lib": lib, "name": name, "category": cat,
                        "expr": expr, "kwargs": {k: str(v)[:40] for k, v in kw.items()},
                        **r})

    payload = {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "batch": args.batch,
        "network_gate": gate,
        "network_degraded": not gate["_pass"],
        "contract": {
            "OK": "行数>0，永远可信",
            "EMPTY_OK": "调用成功但 0 行 —— 不可作为『无数据』结论",
            "FAIL_DETERMINISTIC": "端点给出明确语义报错，可信",
            "FAIL_GATEWAY": "502/503/504 或解析失败 —— 不可解释",
            "FAIL_UNREACHABLE": "连接层失败/超时/挂死 —— 不可解释",
        },
        "results": records,
    }
    path = OUT / f"interfaces_probed_{args.batch}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    from collections import Counter
    cnt = Counter(r["state"] for r in records)
    print("\n=== 汇总 ===")
    for st in (STATE_OK, STATE_EMPTY, STATE_DET, STATE_GATEWAY, STATE_UNREACH):
        if cnt.get(st):
            print(f"  {st:20s} {cnt[st]}")
    print(f"\n产物: {path}")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
