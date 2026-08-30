#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自动打点器 —— 从枚举清单派生探针，覆盖全部 A 股相关接口（FINDING-194）。

为什么必须"自动派生"而不是手写探针（FINDING-194 实测）：
  手写的 B1..B4 共 71 个探针，覆盖率 **71/1,555 = 4.6%**，且清单是我按
  "当前想到的数据种类"列的 —— 这正是用户反复否定的做法：
  **按当前已知需求裁剪探索范围**。用户的三条理由均已核实成立：
    1) 盈利瓶颈的出路不在已知数据里（台账 8 条证伪路径 + median_n_eff=1.9456）
    2) 成本算漏已发生三次，每次根因都是"不知道有这项 / 不知道能取到"
    3) 打点一次以后查表，不重复试错

⭐ 五态契约直接复用 `finai/sources/base.py`，不另造一套：
  OK / EMPTY_OK / FAIL_DETERMINISTIC / FAIL_GATEWAY / FAIL_UNREACHABLE / FAIL_PROBE_BUG

⛔ 硬约束：
  1. `FAIL_*` 一律不得记为"接口不可用"（FINDING-175）；
     只有 `OK` 与 `FAIL_DETERMINISTIC` 可作能力结论
  2. 静默 0 行必须成 `EMPTY_OK`，不得当成功、也不得当"无数据"（FINDING-178）
  3. 每个探针必须有**外部超时** —— baostock 实测能挂死且
     `socket.setdefaulttimeout` 约束不住它（FINDING-181），故用子进程隔离
  4. `AttributeError`/`TypeError` 等调用方错误必须归 `FAIL_PROBE_BUG`，
     不得伪装成"接口不可用"（FINDING-184：我曾把自己写错的函数名记成接口不存在）
  5. 打点前必须过网络门禁，否则失败结果不可解释
  6. 产物必须**增量落盘** —— 近千个探针跑到一半崩掉不能全丢

⚠ 关于"取数脚本必须走 `finai/sources/`"这条红线的**显式豁免**：
  本脚本是**诊断工具**，需按反射调用近千个任意接口，结构上不可能都经 L0 adapter。
  折中：**契约层复用 L0**（`classify_exception` + 五态），调用层用反射。
  ⛔ 本豁免**只适用于打点**；任何**生产取数**仍必须走 `finai/sources/`。

用法：
  python scripts/auto_probe_interfaces.py --dry-run
  python scripts/auto_probe_interfaces.py --limit 40
  python scripts/auto_probe_interfaces.py --lib akshare --offset 0 --limit 200
  python scripts/auto_probe_interfaces.py --all
"""
from __future__ import annotations

import argparse
import collections
import json
import multiprocessing as mp
import os
import queue as _queue
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ⭐ `FINDING-408`：限流的**主机归属表**（生成物，来源为函数源码实测）。
# ⛔ 不放在本文件里手写 —— 手抄副本必然与已安装包漂移。
# ⛔ 也不 try/except 成"没有就算了"：表缺失就意味着主机级预算不存在，
#    那正是 `FINDING-408` 的缺陷本体，必须在 import 时就炸。
try:
    from scripts.probe_host_attribution import (HOST_ATTRIBUTION,
                                                attributed_domain)
except ImportError:      # 以裸文件方式加载本模块时（无包上下文）
    from probe_host_attribution import HOST_ATTRIBUTION, attributed_domain

# ⛔ 并发守卫（LESSONS §25.7 / FINDING-205 / FINDING-204）模块级可覆盖的 lockfile 路径。
#    惰性导入：子进程（spawn 重 import）也不触发 `finai` 包的必需性。
try:
    from finai.probe_guard import probe_in_progress
except (ImportError, SyntaxError, RuntimeError):
    probe_in_progress = None   # 本机无守卫模块 → main() 里降级跳过（不会误拦）

OUT = PROJECT_ROOT / "artifacts" / "interface_matrix"
RAW = OUT / "interfaces_raw.json"
RESULT = OUT / "auto_probe_results.json"

#: ⛔ `FINDING-204`：打点器每 25 个探针**整份重写**产物，故任何在打点期间
#: 修改产物的操作都会被静默覆盖（我的 12 条重判就是这样丢的）。
#: 本 lockfile 让外部脚本能**检测存在性**而不是猜时序 ——
#: 把竞态问题变成状态问题。含 PID 以便识别陈旧锁。
LOCK = OUT / "auto_probe.lock"

#: 本批不实测的类别。B5（港美股/期货/期权/加密）按主方案"仅登记存在性"；
#: 管道/非数据接口与非金融替代数据不是接口能力（FINDING-197 已拆桶）。
EXCLUDE_CATEGORIES = frozenset({
    "管道/非数据接口", "其他-非金融替代数据",
    "港股", "美股", "加密/外汇", "期货", "期权",
})

#: 参数合成表。⭐ 键来自**实测**的必填参数名频次统计，不是我猜的：
#:   code 65 / market 21 / date 12 / start 9 / year 8 / quarter 8 /
#:   stock_code 7 / fund_code 7 / symbol 3 / trade_date 3 / exchange 3 ...
#: 值取项目已验证可用的真实标的与日期（20260804 是 staging 数据实测存在的交易日）。
ARG_SYNTHESIS: dict[str, object] = {
    # 证券代码：各库格式不同，故按参数名分别给
    "code": "000001",
    "symbol": "000001",
    "stock_code": "000001",
    "stock": "000001",
    "ts_code": "000001.SZ",
    "stock_codes": "000001",
    "codes": "000001",
    "symbols": "000001",
    "fund_code": "510050",
    "fund_codes": "510050",
    "bond_code": "110059",
    "bond_codes": "110059",
    "index_code": "000300",
    # 日期
    "date": "20260804",
    "trade_date": "20260804",
    "start_date": "20260701",
    "end_date": "20260804",
    "start": "20260701",
    "end": "20260804",
    "beg": "20260701",
    "year": "2024",
    "quarter": "4",
    "period": "20231231",
    # 市场 / 交易所
    "market": "sz",
    "exchange": "SSE",
    "day": "20260804",
    # 其它
    "indicator": "今日",
    "adjust": "",
    "count": 10,
    "page": 1,
    "limit": 10,
}

#: ⭐ 逐库覆盖表 —— 同一个参数名在不同库要求不同格式。
#: 本表**由实测驱动**：首批 22 个 baostock 探针有 10 个报
#: `error_code=10004006 msg=股票代码应为9位，如：sh.600000`，
#: 即 baostock 要 `sz.000001` 而我给了 `000001` —— **是我的参数格式错，
#: 不是接口不可用**。若不加本表，那 10 条会以 `FAIL_UNREACHABLE` 落进产物，
#: 读者会据此认为 baostock 的财务/分红/复权接口都不可用（`FINDING-184` 的形状）。
LIB_ARG_OVERRIDES: dict[str, dict[str, object]] = {
    "baostock": {
        "code": "sz.000001",
        "symbol": "sz.000001",
        "stock_code": "sz.000001",
        "date": "2026-08-04",
        "trade_date": "2026-08-04",
        "start_date": "2026-07-01",
        "end_date": "2026-08-04",
        "start": "2026-07-01",
        "end": "2026-08-04",
        "year": 2024,
        "quarter": 4,
        "day": "2026-07-01",
    },
    "tushare": {
        "code": "000001.SZ",
        "symbol": "000001.SZ",
        "ts_code": "000001.SZ",
        # ⛔ `FINDING-218-NEW-1`：`year`/`quarter` **必须是 int**，不能是字符串。
        #    根因在库里读到的源码，不是我推测：
        #      tushare/stock/cons.py::_check_input
        #        if isinstance(year, str) or year < 1989: raise TypeError(DATE_CHK_MSG)
        #        elif quarter is None or isinstance(quarter, str) or quarter not in [1,2,3,4]: raise
        #    而扁平表 `ARG_SYNTHESIS` 给的是 `"year": "2024"` / `"quarter": "4"`（字符串）
        #    ⇒ **7 条**财务接口全部报 `TypeError: 年度输入错误…`，被判 FAIL_PROBE_BUG。
        #    ⭐ 这是**我们自己传错类型**，不是"接口不可用"（`FINDING-184` 的形状）。
        #    ⚠ 注意 `isinstance(year, str)` 是**先判类型再比大小**，故 "2024" 这种
        #      "看起来合法"的值也必被拒 —— 光看错误文本"请输入1989年以后的年份"
        #      会误以为是**取值**问题，实际是**类型**问题。
        "year": 2024,
        "quarter": 4,
    },
    "mootdx": {
        # ⛔ `FINDING-218-NEW-2`：`market` **不能给 0** —— 库用 `if not market:` 判空，
        #    而 `not 0 is True` ⇒ 传 0 与"根本没传"在库里**不可区分**。
        #    实测源码 `mootdx/quotes.py:551-557`：
        #        if not market: ...(尝试从 symbol 里 '#' 拆)
        #        if not market: raise ValueError('市场参数错误, 市场参数不能为空.')
        #    ⇒ 7 条 `ExtQuotes.*` 报"市场参数不能为空"**是我们传了 0**，
        #      不是接口不可用（`FINDING-184` 的形状）。
        #
        # ⚠⚠ **我第一版给这里写的理由是错的，留档以免下一个人照抄**：
        #    我原本写"0=MARKET_SZ 深市、故取 1=MARKET_SH 沪市"。
        #    但报错的是 `ExtQuotes` —— **扩展市场**（期货/期权/外盘）API，
        #    它的 `market` 是**交易所 ID**，与股票市场的 `MARKET_SH/SZ` **不是同一套编码**。
        #    权威值来自实测产物本身（`ExtQuotes.markets` 返回 31 行）：
        #        {'market': '1', 'category': '1', 'name': '临时股', 'short_name': 'TP'}
        #    ⇒ 取 1 之所以合法，是因为它**是该 API 的一个真实交易所 ID**（临时股），
        #      而**不是**因为它等于 MARKET_SH。⭐ 结论相同、理由不同 ——
        #      而错的理由会让下一个人在错的编码空间里找值。
        # ⚠ 因此本值只保证"能过 falsy 检查且是合法 ID"，⛔ **不保证有数据**：
        #    market 与 symbol 不配对时会得到空结果（`EMPTY_OK`）——
        #    按 `FINDING-178` 那**不得**读作"无数据"。
        #    真要逐条取到数据，须按 `ExtQuotes.markets` 的 31 行逐个配对试，属独立工作单元。
        # ⚠ 回归风险已实测为 **0**：受 market 影响的 28 条里当前 OK 的 5 条
        #    **全部走 `required_only`（不传 market）**，且 OK 且以 `with_optional`
        #    传过 market 的条目实测 **0 条**。
        #
        # ⭐⭐ `FINDING-246`（2026-08-08）**上面那段预告的坑已实测确认，值已改**：
        #    原值 `market=1, symbol='000001'` 的两个值**各自合法、组合非法** ——
        #    `1` = "临时股"交易所，`000001` = A 股代码，该交易所下**没有这个标的**
        #    ⇒ 库返回空 ⇒ 7 条 `ExtQuotes.*` 记为 `EMPTY_OK`（被误读成"接口失效"）。
        #    真值由**同源目录接口**给出，不是我挑的：
        #        `ExtQuotes.instrument(start=0, offset=800)` → 800 行，
        #        其中 **market 只有 `71`**（港股），首行 `00001 长和`。
        #    配对实测（`artifacts/_exhq_pairing.json`，旧/新两腿对照）：
        #        腿 OLD (1, 000001) : bars=0    minute=0    transaction=0
        #        腿 GOOD(71, 00001) : bars=700  minute=330  transaction=800  ✅
        #    ⇒ 改配对后**净增 3 条**可用（bars/minute/transaction）。
        # ⛔ 与 goal 红线③「不得为变绿挑日期取数」的分界：本处**未动任何日期参数**，
        #    改的是标的坐标，且标的取自目录**第一行**（换成其余 799 行同样成立）。
        # ⚠ 但 `market`/`symbol` 是**全库共享**的合成值，改它会影响 mootdx 下所有
        #    读 `symbol` 的方法（`StdQuotes.*` 走 A 股代码空间）⇒ **不能全局改成港股**。
        #    故这里保持 A 股默认，仅对扩展市场方法用 `FUNC_ARG_OVERRIDES` 定点覆盖（见下）。
        "market": 1,
        "symbol": "000001",
        "code": "000001",
    },
    "tdxpy": {
        "market": 0,
        "code": "000001",
        # ⛔ `FINDING-233`：`exchange` 被 tdxpy **直接拼进文件路径**，不是枚举校验。
        #    实测 `generate_filename('000001','SSE')` → `…\vipdoc\SSE\lday\SSE000001.day`
        #    （该目录不存在）⇒ 3 条 `TdxDailyBarReader.*` 全报"no tdx kline data"。
        #    真实目录名是小写 `sz`/`sh`/`bj`，故合法值是 `'sz'`（实测 1,205 行）。
        #    ⚠ 与 `ARG_SYNTHESIS["exchange"]="SSE"` 的差别是**库特定**的，故只改本库。
        #    ⚠ 值必须与 `code` 配对：`000001` 是深市 ⇒ 取 `sz`。
        "exchange": "sz",
        # ⛔ `FINDING-239`：tdxpy 的 `start` 是**记录偏移量**（int），不是日期。
        #    全库 5 条签名都是 `(self, market, code, start, count)` 这类分页游标，
        #    而合成表给 `'20260701'`（日期字符串）→ 偏移量越界 → 静默 0 行 → EMPTY_OK。
        #    实测 `start=0`：`get_security_list` 1,000 行 /
        #    `get_history_transaction_data` 10 行（`artifacts/_tdx_start.txt`）。
        #    ⚠ 回归风险实测为 0：带 `start` 的记录全库仅 10 条（tdxpy 5 / mootdx 3 /
        #    tushare 2），其中 **OK 态 0 条** ⇒ 不可能弄坏已成功的记录；且本覆盖只作用于 tdxpy。
        #    ⛔ 这**不是** `NEVER_FILL_OPTIONAL` 违规：`start` 是分页**起点**而非
        #    规模上限，给 0 是"从头开始"（取得更多），不是"只取 N 条"（截断）。
        "start": 0,
        # ⛔ `FINDING-240`：tdxpy 的 `date` 也是 **int**（`20260804`），不是字符串。
        #    它走的是通达信二进制协议，日期以整数打包；给 `'20260804'` 时库不做转换，
        #    请求打出去但服务端解不出该日 → **静默 0 行**（又一条 EMPTY_OK）。
        #    实测同一天两条腿（`artifacts/_tdx_argtypes.txt`）：
        #      `date='20260804'` → 0 行 ┆ `date=20260804` → **10 行**
        #      `['time','price','vol','buyorsell']`
        #    ⚠ 第三条腿 `date=20220429`（确定的历史交易日）同样 10 行 ⇒ 排除
        #      "恰好该日无数据"，根因确定是**类型**而非日期取值。
        #    ⛔ 这不是"为变绿挑日期"（红线③）：日期值**不变**，只改类型。
        "date": 20260804,
    },
    "adata": {
        "stock_code": "000001",
        "start_date": "2026-07-01",
        "end_date": "2026-08-04",
    },
}

#: 函数级参数覆盖（FINDING-218）。
#:
#: 问题根因：`ARG_SYNTHESIS["symbol"] = "000001"` 是扁平名→值表，不读枚举域。
#: akshare 大量用同一个参数名 `symbol` 承载两种语义：
#:   · 「证券代码类 symbol」：接受 '000001'
#:   · 「枚举类 symbol」：合法值是 choice of {'全部','个人','基金',...}
#: 按名查表必然给枚举参数注入证券代码，于是端点回答"返回数据为空"。
#:
#: ⚠ 每条都来自实测（inspect.signature + akshare docstring），不是推定：
#:   · [A类] 非法枚举值（我们注入了域外值）
#:   · [B类] 形式合法但取值不对（如 date='20260804' 非季末报告期）
#:   · [C类] akshare 自带默认值可能过期，补传近期合法报告期
FUNC_ARG_OVERRIDES: dict[str, dict[str, object]] = {
    # [A] stock_gdfx_holding_detail_em
    #   indicator 合法域：{"个人","基金","QFII","社保","券商","信托"}
    #   symbol    合法域：{"新进","增加","不变","减少"}
    "akshare::stock_gdfx_holding_detail_em": {
        "date": "20240930",        # B类：补季末报告期
        "indicator": "个人",       # A类：取合法值第一项
        "symbol": "新进",          # A类：取合法值第一项
    },
    # [A] stock_gdfx_holding_teamwork_em
    #   symbol 合法域：{"全部","个人","基金","QFII","社保","券商","信托"}
    "akshare::stock_gdfx_holding_teamwork_em": {
        "symbol": "全部",          # A类：取合法值
    },
    # [B] stock_gdfx_free_holding_statistics_em
    #   date 是报告期，默认 '20210630'（季末格式）
    "akshare::stock_gdfx_free_holding_statistics_em": {
        "date": "20240930",        # B类：补近期合法季末报告期
    },
    # [C] 以下 5 条用 akshare 默认值，但默认值可能已过期（商誉/北向数据报告期推进）
    #   补传近期合法报告期
    # ⭐ `FINDING-313`（2026-08-09）：这两条的 `'20240930'` **不是**"合法季末报告期"
    #    就够了 —— 实测直连 `datacenter-web.eastmoney.com/api/data/v1/get`
    #    （`reportName=RPT_GOODWILL_INDUSTATISTICS`）：
    #        `REPORT_DATE='2024-09-30'` → `result: null` / message「返回数据为空」
    #        `'2025-03-31'`、`'2025-09-30'` → 同样 `result: null`
    #        `'2025-12-31'`、`'2026-03-31'` → `pages=3 count=127` ✅
    #    ⇒ 端点**活着**，是这个报告期已滑出保留窗口。上一版注释说"补近期合法
    #      报告期"，方向对，但 `20240930` 在 2026-08 已经**不再近期**。
    #    ⚠ akshare 侧默认值同样过期（签名里写死 `date='20240930'`/`'20240630'`），
    #      故必须由我们显式覆盖，⛔ 不得改 site-packages。
    #    实测覆盖后：`stock_sy_hy_em` **127 行 / 6 列**、`stock_sy_jz_em` **2,669 行 / 11 列**。
    "akshare::stock_sy_hy_em": {"date": "20251231"},
    "akshare::stock_sy_jz_em": {"date": "20251231"},
    # ⛔ `stock_gdfx_holding_statistics_em` **不认领**：`20251231` 与 `20260331`
    #    两个日期实测都还是 `ValueError: Length mismatch: Expected axis has 18
    #    elements, new values have 17` ⇒ 那是 akshare 自己的列名表比端点少一列，
    #    换日期治不了。留在 `F_PARSE_MISMATCH`（见 8 条 Length mismatch 簇）。
    # ⛔ `stock_hsgt_board_rank_em` 这条注释（"默认值合法，无需覆盖"）**是错的**，
    #    见 `FINDING-315`：它的 `TRADE_DATE` 与 `BOARD_TYPE` 都写在**函数体里**，
    #    `FUNC_ARG_OVERRIDES` 结构上**到不了** ⇒ 留空字典既不合法也没用。
    #    实测端点：`RPT_MUTUAL_BOARD_HOLDRANK_WEB` 最新 `TRADE_DATE='2024-08-16'`、
    #    `BOARD_TYPE='2'`，而 akshare 体内写死 `BOARD_TYPE="5"` ⇒ 恒空。
    "akshare::stock_hsgt_board_rank_em": {},   # ⚠ 见上：这条留着只为不改动既有键集
    # ⭐ `FINDING-314`：`get_roll_yield` 的 `var` 默认 `'BB'` **不是合法品种代码**
    #    ⇒ 恒 0 行（`EMPTY_OK`，无异常，故此前一直被当"没数据"）。
    # ⛔⛔ `FINDING-318`（2026-08-10 更正）：上一版这里写"实测 → **3 行**"，
    #    **那个 3 不是行数**。直连实测该函数返回 **3 个标量组成的 tuple**：
    #        (-0.04627385930896276, 'RB2610', 'RB2701')   # (展期收益率, 近月, 远月)
    #    产物里因此是 `state=OK` / `rows=3` / **`cols=null`** ⇒ 分子判据
    #    （`cols` 非空 **且** `rows>0`）**恒不满足**，它落在 `A_STRUCTURAL_NOT_TABULAR`。
    # ⇒ 本条覆盖的**净贡献是 0**，⛔ 不得计入战果；且因为它把该条从 F 桶推进了
    #    A（天花板）桶，`ceiling_numerator` 由 961 降到 **960** —— 那次漂移就是这条。
    # ⭐ 覆盖**保留**：它让"默认 `var='BB'` 非法"这个实测事实留在代码里可读。
    #    ⛔ 但不得为了凑 `cols` 把 tuple 包成单行 DataFrame（伪造字段名，`FINDING-238`）。
    "akshare::get_roll_yield": {"date": "20260807", "var": "RB"},
    # ⭐ `FINDING-314`：`tushare.get_k_data` 的 `start/end` 默认空串 ⇒ 0 行。
    #    实测 `{code:'000001', start:'2026-07-01', end:'2026-08-07'}` → **28 行**。
    #    ⚠ 与 `mootdx` 那条同一形状：**日期必须带连字符**。
    "tushare::get_k_data": {
        "code": "000001", "start": "2026-07-01", "end": "2026-08-07"},
    "akshare::stock_hsgt_individual_detail_em": {
        "symbol": "002008",        # 保持默认，已有合法参数
        "start_date": "20240101",
        "end_date": "20240930",
    },
    "akshare::fund_value_estimation_em": {},   # 默认 '全部' 合法，Data=null 可能是今日无估值

    # ───────────────────────────────────────────────────────────────────
    # ⭐ `FINDING-205-NEW-1` 修法①的实测结果（2026-08-07，`scripts/_probe_paging_narrow.py`）
    #
    # 这批接口的分页**在函数体内**（签名里没有 page 参数），故
    # `PAGE_LIMIT_OVERRIDES` 结构上覆盖不到；实测其中一条要遍历 2,858 页（ETA 2h11m）。
    # ⛔ 修法**不是**"限制返回规模"（`NEVER_FILL_OPTIONAL` 禁止），
    #    而是**给定一个具体报告期/日期窗口** —— 把"全量遍历"还原成"单次调用"。
    # ⚠ 因此产物里的行数是**该报告期**的行数，⛔ 不得读成"该接口只有这么多数据"。
    #
    # 每条下面的行数与耗时都是**实测值**（独立进程 + 120s 硬超时，非推断）：
    "akshare::stock_gdfx_free_holding_change_em": {
        "date": "20240930",     # 实测 35,145 行 / 109.1s
    },
    "akshare::stock_gdfx_free_holding_teamwork_em": {
        "symbol": "社保",       # 实测 19,376 行 / 53.6s
    },
    "akshare::stock_gdfx_free_top_10_em": {
        "symbol": "sh688686", "date": "20240930",   # 实测 10 行 / 1.5s
    },
    "akshare::stock_gdfx_top_10_em": {
        "symbol": "sh688686", "date": "20240930",   # 实测 10 行 / 1.2s
    },
    "akshare::stock_hsgt_hist_em": {
        "symbol": "北向资金",   # 实测 2,725 行 / 5.0s
    },
    "akshare::stock_hsgt_institution_statistics_em": {
        "market": "北向持股",   # 实测 152 行 / 3.7s
        "start_date": "20240110", "end_date": "20240110",
    },
    "akshare::stock_lhb_ggtj_sina": {
        "symbol": "5",          # 实测 239 行 / 8.8s
    },
    # ⚠ 以下 4 条收窄后仍报错，**已实测但尚未解决**（`FINDING-205-NEW-1` 未闭合部分）：
    #   stock_gdfx_holding_change_em      ValueError: Length mismatch: Expected axis has 21 elements
    #                                     ← akshare 自己的列名表与响应列数不符（库缺陷，非入参）
    #   stock_hsgt_hold_stock_em          TypeError: 'NoneType' object is not subscriptable
    #   stock_hsgt_stock_statistics_em    TypeError: 'NoneType' object is not subscriptable
    #   stock_hsgt_individual_detail_em   TypeError: 'NoneType' object is not subscriptable
    #   ⇒ 后三条是"响应体 Data=null"的形态（`FINDING-218`：收到字节 ≠ 收到数据），
    #     根因多半仍在日期窗口/参数域，需逐条试合法域 —— 属独立工作单元。
    # ⚠ 另 5 条即使给定报告期仍 >120s（`*_holding_analyse_em` ×2 /
    #   `stock_gdfx_free_holding_detail_em` / `*_holding_statistics_em` ×2）：
    #   它们按报告期聚合全市场股东，页数与报告期无关 ⇒ 收窄参数救不了。
    #   ⛔ 它们会被记成 HUNG → 按 §九① **不得**读作"接口不可用"。

    # ───────────────────────────────────────────────────────────────────
    # ⭐ `FINDING-239`（2026-08-08，`scripts/_probe_empty_ok_args.py` /
    #    `_test_library_defaults.py` 实测）：EMPTY_OK 里的**代码格式 / 必填年份**类。
    #    每条的行数都是本机实测值，⛔ 不是从文档抄的。
    #
    # 腾讯分时成交要**带市场前缀**的代码（`sz000001`），给裸 `000001` 静默 0 行。
    # 实测：`sz000001` → 4,565 行（`_test_library_defaults.py`）
    "akshare::stock_zh_a_tick_tx_js": {"symbol": "sz000001"},
    # 财务分析指标需 `start_year`，缺了返回空表。实测：`600004`+`2020` → 25 行
    "akshare::stock_financial_analysis_indicator": {
        "symbol": "600004", "start_year": "2020",
    },

    # ───────────────────────────────────────────────────────────────────
    # ⭐ `FINDING-265`（2026-08-09，`scripts/_r31_argfix_legs.py` 成对腿实测）：
    #    **`symbol` 这个名字的取值域不止「股票代码」一种** —— 扁平的
    #    名→值表把 `symbol` 一律填 `000001`，而下列接口的 `symbol` 分别是
    #    **枚举值**或**报告期**。⛔ 这不是红线③「为变绿挑日期」：
    #    日期一律保留探针原值，改的是**取值域**（性质同 `FINDING-240`
    #    的「date 该传 int 而非 str」——值没换，域纠正了）。
    #    每条旧腿都必须复现 0 行才采信（`FINDING-126`）。
    #
    # `symbol` ∈ {'最新投资评级','上调评级股票',…}（机构推荐池的**页面名**），
    # 库默认 '投资评级选股' 实测 0 行。实测 '最新投资评级' → 5,126 行 × 8 列
    "akshare::stock_institute_recommend": {"symbol": "最新投资评级"},
    # 参数名是 `stock`（不是 symbol）+ `quarter`＝年份+季次；
    # 库默认 600433/20201 实测 0 行。实测 600004/20241 → 4 行 × 12 列
    "akshare::stock_institute_hold_detail": {
        "stock": "600004", "quarter": "20241",
    },
    # adata 概念成分：四个参数默认全 None ⇒ 库内静默返回空（`FINDING-244` 同族）。
    # 实测 `index_code='886013'` → 303 行 × 2 列 ['stock_code','short_name']
    "adata::stock.info.concept_constituent_ths": {"index_code": "886013"},

    # ───────────────────────────────────────────────────────────────────
    # ⭐ `FINDING-241`（2026-08-08，`scripts/_probe_tdx_argtypes.py` 实测）：
    #    `get_security_quotes` 的入参是 **`list[tuple[market, code]]`**，
    #    而按名合成表只能产出**标量** ⇒ 传 `code='000001'` 时库拿不到 market，
    #    静默 0 行。这是**容器结构**层面的不可合成，不是取值问题
    #    （`synthesize_kwargs` 的扁平名→值表在结构上产不出 list[tuple]）。
    #    实测 `all_stock=[(0,'000001')]` → **1 行**
    #    `['market','code','active1','price','last_close','open','high','low']`
    "tdxpy::hq.TdxHq_API.get_security_quotes": {"all_stock": [(0, "000001")]},

    # ───────────────────────────────────────────────────────────────────
    # ⭐ `FINDING-244`（2026-08-08，`scripts/_probe_emptyok_isolated.py` 实测）：
    #    adata 两条「可选参数实为必填」——签名给了默认 `None`，但库内
    #    `if x is None: return 空` ⇒ 不传就静默 0 行（又一条静默降级）。
    #    两腿对比：腿A（探针当前入参）0 行 ┆ 腿B（补参）有数据。
    # `report_date=None` 时不查 ⇒ 补一个交易日。实测 66 行
    "adata::sentiment.hot.list_a_list_daily": {"report_date": "2024-08-01"},
    # `code_list=None` 时不查 ⇒ 补一个代码列表（同 FINDING-241 的嵌套容器形态）。实测 2 行
    "adata::stock.market.list_market_current": {"code_list": ["600000", "000001"]},

    # ───────────────────────────────────────────────────────────────────
    # ⭐⭐ `FINDING-246`（2026-08-08）：mootdx **扩展市场**（`ExtQuotes`）的
    #    `market`/`symbol` 必须**配对**。全库合成值是 A 股坐标（`1` + `000001`），
    #    在扩展市场里指向不存在的标的 ⇒ 静默 0 行 ⇒ 曾被我误判为"整族失效"。
    #    真值来自**同源目录接口** `ExtQuotes.instrument`（800 行，market 仅 `71`=港股）：
    #        腿 OLD (1, 000001) : bars=0    minute=0    transaction=0
    #        腿 GOOD(71, 00001) : bars=700  minute=330  transaction=800  ✅
    #    ⛔ 只能定点覆盖：`symbol` 是全库共享值，全局改成港股会打坏 `StdQuotes.*`
    #      （A 股代码空间）—— 这是**同一参数名在两个编码空间**里的经典冲突
    #      （与 `FINDING-218-NEW-2` 同一形状：market 在 Std/Ext 下不是一套编码）。
    # ⚠ `minutes` 实测仍 0 行（见下）；`transactions` 曾被记为"库内解析缺陷"，
    #    `FINDING-310` 实测**推翻**了那个归因 —— 见该条下方的 `transactions` 覆盖。
    "mootdx::quotes.ExtQuotes.bars": {"market": "71", "symbol": "00001"},
    "mootdx::quotes.ExtQuotes.minute": {"market": "71", "symbol": "00001"},
    "mootdx::quotes.ExtQuotes.transaction": {"market": "71", "symbol": "00001"},

    # ⭐ `FINDING-310`（2026-08-09）：上面那条注释把 `transactions` 的
    #    `ValueError: invalid literal for int() with base 10: ''` 归因为"库内解析缺陷"。
    #    ⛔ **那个归因是错的**。实测 2×2（date 给/不给）：
    #        {market:71, symbol:00001}              → ValueError（同上）
    #        {market:71, symbol:00001, date:20260807} → **800 行 / 10 列** ✅
    #    ⇒ 真因是**缺 `date`**：签名默认 `date=''`，库把空串直接 `int('')`。
    #      报错发生在库里，但**触发者是调用方漏参** —— ⛔ 库内缺陷与
    #      调用方漏参会给出**同一个栈**，不可据栈位置断言归属（`FINDING-233/234` 同形）。
    "mootdx::quotes.ExtQuotes.transactions": {
        "market": "71", "symbol": "00001", "date": "20260807"},
    # ⚠ `ExtQuotes.minutes` 三种入参组合（含带 date、带 start/offset）实测**全 0 行**
    #    ⇒ 本轮**不认领**，仍留 H_REMEDIABLE。⛔ 不得因"同族三条都好了"就类推它也好。

    # ⭐⭐ `FINDING-246`+`FINDING-247` 的 tdxpy 侧落点：活主机 `47.112.95.207:7720`
    #    上 `get_markets` 返回 **31 个市场**，但 `get_instrument_info` 目录里
    #    **实际有合约的只有 market=71（港股通）**，500 条 ⇒ 标的取该目录首条 `00001 长和`。
    #    活主机 + 正确配对实测（`artifacts/_exhq_live_pair.json`）：
    #        get_instrument_quote 1 ┆ get_minute_time_data 330 ┆ get_transaction_data 10
    #        get_instrument_bars 10 ┆ get_history_minute_time_data 330
    #    ⚠ 仍 0 行的两条（`get_history_transaction_data` /
    #      `get_history_instrument_bars_range`）**不在**本次修复范围，另案未结。
    # ⛔ `market` 传 int：tdxpy 的 market 是数值型（同 `FINDING-240` 的类型问题）。
    "tdxpy::exhq.TdxExHq_API.get_instrument_quote": {"market": 71, "code": "00001"},
    "tdxpy::exhq.TdxExHq_API.get_minute_time_data": {"market": 71, "code": "00001"},
    "tdxpy::exhq.TdxExHq_API.get_transaction_data": {"market": 71, "code": "00001"},
    # ⚠ `category` = K 线周期，**必填且合成表里没有**（合成表按参数名给值，
    #   而 `category` 这个名字在其他库里含义完全不同 ⇒ 只能定点给）。
    #   `9` = 日线（与 `mootdx` 的 `frequency=9` 同一套 TDX 周期编码）。
    #   ⭐ 补这个参数会使本条**从"无法合成入参"进入计划** ⇒ 分母 1065→1066，
    #     属**纠正误分类**（它本就是一个可调用数据接口，此前因缺一个必填参数
    #     被判 unsynthesizable 而排除在册），非 goal 红线④ 的"换口径"。
    #     ⛔ 且它进的是**分母**，不是白送进分子：仍须实测 rows>0 才计入。
    "tdxpy::exhq.TdxExHq_API.get_instrument_bars": {
        "market": 71, "code": "00001", "category": 9},
    "tdxpy::exhq.TdxExHq_API.get_history_minute_time_data": {
        "market": 71, "code": "00001", "date": 20260804},

    # ───────────────────────────────────────────────────────────────────
    # ⭐ `FINDING-310`（2026-08-09，本轮 R34）：mootdx **标准市场**（`StdQuotes`）
    #    的 3 条只是**缺日期区间/偏移量**，不是端点问题。
    #    连上现场实测存活的行情主机（`124.71.187.122:7709`，TCP 握手确认，
    #    ⛔ 主机不硬编码进判定 —— `FINDING-247` 明示主机会轮换）后实测：
    #        get_k_data ("000001", 2026-07-01..2026-08-07) → **26 行 / 8 列**
    #        k          (symbol=000001, begin/end 同上)    → **26 行 / 9 列**
    #        transactions(symbol=000001, start=0, offset=100, date=20260807)
    #                                                       → **100 行 / 5 列**
    #    ⚠ 与 `ExtQuotes` 那族**相反**：`Ext*` 6 条实测全 0 行，且 mootdx 自己在
    #      日志里写明"目前扩展市场行情接口已经失效, 请等待作者修复"⇒ 那 6 条是
    #      **上游失效**，不在本次范围（⛔ 不得据此认领缺口）。
    # ⛔ 这 3 条此前**没有**任何 override（已 grep 确认）⇒ 本次补参数**确实会**
    #    改变结果，不是 `FINDING-157` 那种"改了也不动"的空转。
    # ⛔ 此条的参数名我第一版**写错了**（写成 `symbol/start/end`），实测报
    #    `TypeError: get_k_data() got an unexpected keyword argument 'symbol'`。
    #    真签名是 `(self, code, start_date, end_date)`，与同族的 `k()` 不同名。
    #    ⭐ 合并护栏（`FINDING-214`）挡住了这次退化：它没让 `FAIL_PROBE_BUG`
    #      覆盖既有的 `EMPTY_OK`，而是记进 `later_observations` ⇒ 我的错可见。
    # ⚠ 日期**必须带连字符**：实测 `20260701..20260804` → 0 行，
    #   `20260701..20260807` → 仍 0 行，而 `2026-07-01..2026-08-07` → **26 行**
    #   ⇒ 库内部按字符串比较日期，紧凑格式恒不匹配（不是"那几天没数据"）。
    "mootdx::quotes.StdQuotes.get_k_data": {
        "code": "000001", "start_date": "2026-07-01", "end_date": "2026-08-07"},
    "mootdx::quotes.StdQuotes.k": {
        "symbol": "000001", "begin": "2026-07-01", "end": "2026-08-07"},
    "mootdx::quotes.StdQuotes.transactions": {
        "symbol": "000001", "start": 0, "offset": 100, "date": "20260807"},
    # ⭐ `FINDING-314`（2026-08-09）：H 桶 `EMPTY_OK` 27 条的批量实测结果。
    #    ⚠ `EMPTY_OK`（0 行、无异常）**最容易被两头误读**：既不能记成"接口坏了"
    #      （它没报错），也不能记成"可用"（它没有行）。故必须逐条给入参再实测。
    #    实测 21 条里**只有 3 条**能靠入参救活 —— 其余是真没数据/真不可达，如实留桶。
    #
    # `StdQuotes.minutes` 实测 **240 行 / ['price','vol','volume']**（`symbol='000001'`）。
    # ⛔ 不得据此类推同族 `ExtQuotes.minutes`：那条**四种入参组合全 0 行**（见
    #    `FINDING-310`），⭐ 同名不同类 ≠ 同行为。
    "mootdx::quotes.StdQuotes.minutes": {"symbol": "000001"},

    # ───────────────────────────────────────────────────────────────────
    # ⭐ `FINDING-322`（2026-08-10，本轮 R35）：**探针默认标的已退市/无该类记录**
    #    这一族。⚠ 与"接口坏了"必须分开：端点是好的，是**我们喂的标的**不合适。
    #    每条都在**两个独立进程**里各测一次（⛔ 一次成功可能是缓存/偶然）。
    #
    # ① `000001`（平安银行）**没有股权质押记录** ⇒ 端点返回 `code 9201 返回数据为空`
    #    ⇒ 库内 `data_json['data'][...]` 对 `None` 下标 ⇒ `TypeError`。
    #    实测：`000001` → `TypeError: 'NoneType' object is not subscriptable`；
    #          `300750`（宁德时代）→ **25 行 / 15 列**
    #          `['序号','股票代码','股票简称','股东名称','质押股份数量','占所持股份比例',…]`
    #    ⭐ 这条**不是**"挑一个能出数的标的来凑绿"：任何有质押的标的都出数，
    #      而质押明细接口的语义就是"该股的质押记录"，无记录返回空是**正确行为**。
    "akshare::stock_gpzy_individual_pledge_ratio_detail_em": {"symbol": "300750"},
    # ② `110059` 已**退市** ⇒ `efinance` 内部 `get_quote_id` 返回 `''`
    #    ⇒ 下游对 `bool` 下标 ⇒ `TypeError: 'bool' object is not subscriptable`
    #    （库还会打印一行 `证券代码 "110059" 可能有误` —— ⭐ 那行**是库的诊断**，
    #     不是我的观测依据，仍以返回值为准）。
    #    实测：`110075`（存续转债）→ **240 行 / 8 列**
    #          `['债券名称','债券代码','时间','主力净流入','小单净流入',…]`
    "efinance::bond.get_today_bill": {"bond_code": "110075"},
    # ③ `StdQuotes.ohlc` 是 `k()` 的**一行委托**，而 `k()` 早就有可用覆盖（:493）
    #    —— 上一轮补 `k` 时**漏了这条同族**（与 `FINDING-264`「同源装载」同形：
    #    修了一处、漏了等价的另一处）。实测 → **26 行 / 9 列**
    #    `['open','close','high','low','vol','amount','date','code',…]`
    #    ⚠ 日期**必须带连字符**（同 :490 的实测：紧凑格式恒 0 行）。
    "mootdx::quotes.StdQuotes.ohlc": {
        "symbol": "000001", "begin": "2026-07-01", "end": "2026-08-07"},

    # ───────────────────────────────────────────────────────────────────
    # ⭐ `FINDING-323`（2026-08-10，本轮 R35）：雪球两条**不是反爬死局**，
    #    是 akshare **内置的 `xq_a_token` 已失效**，而站点对**匿名访客**照发 token。
    #    ⚠ 这条与我的先验相反（我本以为"需要账号 ⇒ 不可修"），故据实上报。
    #    取证链与"为何这不是伪造凭证"见 `finai.sources.base.fetch_xueqiu_anon_token`。
    #    实测（每次现取新 token、各重跑 3 轮，3/3 稳定）：
    #        `stock_individual_basic_info_xq` SH601127 → **39 行 / 2 列** `['item','value']`
    #        `stock_individual_spot_xq`       SH600000 → **37 行 / 2 列** `['item','value']`
    #    ⛔ 值用 `@finai:` 哨兵而**不写死 token**：① 产物是 git 跟踪的，写死等于把
    #       凭证提交进仓库；② token 会过期 —— 库自带那个就是这么死的。
    #    ⛔ `token` 仍留在 `UNSYNTHESIZABLE` 里（通用合成**不该**去猜凭证），
    #       这里是**函数级**定点覆盖，作用域最小。
    "akshare::stock_individual_basic_info_xq": {
        "symbol": "SH601127", "token": "@finai:xueqiu_anon_token", "timeout": 60},
    "akshare::stock_individual_spot_xq": {
        "symbol": "SH600000", "token": "@finai:xueqiu_anon_token", "timeout": 60},

    # ───────────────────────────────────────────────────────────────────
    # ⭐ `FINDING-328`（2026-08-10，本轮 R35）：**坐标/日期/类型**类，逐条实测。
    #    ⚠ 其中多条**推翻了上一轮的"不可修"判定** —— 旧判定的共同错误是
    #      拿一个**已退市/非法的坐标**去打，然后把"这个坐标没数据"读成"接口坏了"。
    #    ⛔ 行数全部为本机实测值（每条另在**干净进程**复现一次）。
    #
    # [坐标] 库默认 `start_year='1900'`，而库源码 `if start_year in year_list`
    #   取自新浪页面年份链接 ⇒ '1900' 永不命中 ⇒ `return pd.DataFrame()`。
    #   实测 `600004`+`2020` → **25 行 / 86 列**。
    "akshare::stock_financial_analysis_indicator": {
        "symbol": "600004", "start_year": "2020"},
    # [日期窗] 东财 1 分钟端点只留近期；签名默认 2021-10-20..2024-11-01 落在窗外
    #   ⇒ 请求成功（收 18,129 字节）但 0 行。实测当日区间 → **241 行 / 7 列**。
    "akshare::rv_from_stock_zh_a_hist_min_em": {
        "symbol": "000001",
        "start_date": "2026-08-07 09:30:00", "end_date": "2026-08-07 15:00:00"},
    # [坐标·已退市] `110059` 已摘牌（库自己 print「证券代码可能有误」）。
    #   ⚠ 同一坐标错误在本簇出现 **4 次**（另见 `FINDING-322`）⇒ 覆盖表里的
    #   转债坐标会随退市而腐坏，这是**可预期的复发点**。
    #   实测 `127045`（在市）：`get_quote_history` → **1,187 行 / 13 列**；
    #   `get_deal_detail`（用签名默认 `max_count`）→ **2,509 行 / 7 列**。
    "efinance::bond.get_quote_history": {"bond_codes": "127045"},
    "efinance::bond.get_deal_detail": {"bond_code": "127045", "max_count": 1000000},
    "efinance::bond.get_history_bill": {"bond_code": "127045"},
    # [复核推翻] 这两条**探针原样入参就能出数**（旧判定为不可修，实测非）。
    #   ⇒ 写进覆盖表只是把实测坐标固定下来，⛔ 未改任何默认值语义。
    #   `get_market` 无参 → **8,453 行 / 13 列**；`index_current` → **1 行 / 11 列**
    #   （⚠ 指数码**不得带市场前缀**：`sh000001` 实测 0 行）。
    "adata::stock.market.get_market_index_current": {"index_code": "000300"},

    # ⭐ `FINDING-329`（2026-08-10）：上交所融资融券两条的真因是
    #   **tushare 把自己算好的 Referer 注释掉没发**（垫片见
    #   `finai.sources.base.install_tushare_referer_fix`），⛔ 不是入参问题；
    #   这里只补**必需的日期区间**（不给区间时库取"去年今日→今日"，
    #   跨度 365 天 × 每页 100 条递归分页 ⇒ 跑不完）。
    #   实测：`sh_margins` **6 行 / 7 列**、`sh_margin_details` **3,809 行 / 9 列**。
    "tushare::sh_margins": {"start": "2025-08-01", "end": "2025-08-10"},
    "tushare::sh_margin_details": {"start": "2025-08-05", "end": "2025-08-06"},
    # [坐标] 真因是探针传了**带交易所后缀**的 `000001.SZ`，端点不认后缀
    #   （库内 `except` 把它翻成"请检查网络"）。⛔ 与 Referer 垫片无关：
    #   实测**不装**垫片、只去掉后缀就已 **4,746 行 / 4 列**（`600000` → 4,057 行）。
    "tushare::get_today_ticks": {"code": "000001"},

    # ⭐ `FINDING-330`（2026-08-10）：本地 TDX 文件读取器的**周期/文件名**入参。
    #   ⚠ 这两条 `FINDING-328` 曾以"须走专用适配器"为由挂起 —— 实测那个理由**只对
    #   `catalog_source.fetch()` 成立**（它显式拒绝会话初始化类），探针本就直调，
    #   故本轮可落表。⛔ 未改任何适配器、未改 `_route()` 拒绝规则。
    # [文件名] `block()` 默认 `symbol=''` ⇒ 拼出 `T0002\hq_cache\.dat`（库自己报
    #   「文件不存在」）。给真实板块配置文件名 → **5,627 行 / 6 列**。
    "mootdx::reader.StdReader.block": {"symbol": "tdxhy.cfg"},
    # [周期] 默认 `suffix=1` 读 `minline/`，而本机该目录**实测全空**；
    #   `suffix=5` 读 `fzline/`（5,201 个文件）→ **3,408 行 / 6 列**
    #   （`open/high/low/close/amount/volume`）。⭐ 与 `FINDING-321` 互补。
    "mootdx::reader.StdReader.minute": {"symbol": "000001", "suffix": 5},
    # [日期格式] 紧凑格式 `20260701` 让库的标签切片落空 ⇒ 0 行；带连字符 → **25 行 / 8 列**。
    #   ⚠ 成对腿实测（同一连接、同一 code）：`2026-07-01..2026-08-04` = 25 行，
    #     `20260701..20260804` = 0 行 ⇒ 日期格式是唯一变量。
    "tdxpy::hq.TdxHq_API.get_k_data": {
        "code": "000001", "start_date": "2026-07-01", "end_date": "2026-08-04"},

    # ⭐ `FINDING-331`（2026-08-10）：并发三分类批次里**经我独立复现**的 6 条。
    #   ⛔ 每条都是我在干净进程里亲手跑出的行列数（`FINDING-330` 的教训：
    #      转述自己或别人上一轮的"实测"同样不可信）。
    # [坐标·已退市] `sz128039`/`sh113570` 已摘牌 ⇒ 端点返 `None` ⇒ 库 `None[...]`。
    #   换在市转债 `sz123276`：15 分钟 → **64 行 / 11 列**；盘前 → **256 行 / 8 列**。
    #   ⚠ 这是本轮"退市坐标"错误模式的第 **5、6** 次（见 `FINDING-322`/`-328`）。
    "akshare::bond_zh_hs_cov_min": {
        "symbol": "sz123276", "period": "15", "adjust": "",
        "start_date": "1979-09-01 09:32:00", "end_date": "2222-01-01 09:32:00"},
    "akshare::bond_zh_hs_cov_pre_min": {"symbol": "sz123276"},
    # [语义错传] 签名要的是**板块代码**，探针却传了股票代码 `000001` ⇒ 空表取 [0]。
    #   实测 `BK0899` → **41 行 / 16 列**。
    "akshare::stock_board_concept_cons_em": {"symbol": "BK0899"},
    # [枚举值+日期] `type_method='var'` 走的分支已不供数；`'symbol'` + 近期交易日
    #   → **12 行 / 13 列**。
    "akshare::get_roll_yield_bar": {
        "type_method": "symbol", "var": "RB", "date": "20260804"},
    # [枚举值] `symbol='热门概念'` 的分支端点不再返 JSON；`'地域板块'` → **5,528 行 / 21 列**。
    "akshare::stock_classify_sina": {"symbol": "地域板块"},
    # [坐标] 探针传了带交易所后缀的 `000001.SZ`（与 `get_today_ticks` 同一错误模式，
    #   本轮第 2 次）。裸基金代码 `000001` → **1 行 / 16 列**。
    "tushare::get_fund_info": {"code": "000001"},

    # ⭐ `FINDING-336`（2026-08-10）：macro/index 簇 11 条逐条实测后，**唯一**一条
    #   靠入参就能修的。库自带默认 `date='20210910'` 已过期 ⇒ 端点返非 JSON
    #   ⇒ `JSONDecodeError`。单变量隔离（只切 `date`，symbol 不动）：
    #   `20210910` → `JSONDecodeError`；`20260807` → **120 行 / 12 列**；
    #   `20260806` → 同为 **120 行 / 12 列**（两个交易日互为反证，非单日抖动）。
    #   ⛔ 不是"挑一个能绿的参数"：陈旧默认日期是库的缺陷，取近期交易日是唯一可做的事。
    "akshare::stock_industry_pe_ratio_cninfo": {
        "symbol": "证监会行业分类", "date": "20260807"},

    # ───────────────────────────────────────────────────────────────────
    # ⭐ `FINDING-261`（2026-08-09）：`FAIL_PROBE_BUG` 里剩下的**入参域/类型**类。
    #    每条都在本机逐条实测过（命令见台账），⛔ 行数是实测值不是文档抄的。
    #
    # [A类·枚举域] `index_global_hist_sina` 的 `symbol` 是**中文指数名**，
    #   而库自带默认值 `'OMX'` **不在** `index_global_sina_symbol_map` 的 20 个键里
    #   ⇒ 库自己 `KeyError: 'OMX'`（**库的默认值就是坏的**，不是探针给错）。
    #   ⛔ 故这不是"挑参数让它绿"：合法域只有那 20 个键，取其首项是唯一可做的事。
    #   实测 `symbol='英国富时100指数'` → **1,000 行**
    #   `['date','open','high','low','close','volume']`
    "akshare::index_global_hist_sina": {"symbol": "英国富时100指数"},
    # [A类·枚举域] `mootdx.server` 的 `index` 是 `hosts` 字典的**键**
    #   （`'HQ'`/`'EX'`/`'GP'`），默认 `None` ⇒ `hosts[None]` 直接 `KeyError: None`。
    #   同样是**库默认值坏**。取 `'EX'`（扩展行情，与 `FINDING-247` 的活主机同族）。
    #   实测 → 返回 1 条 `[('47.112.95.207', 7720)]` —— 与 `FINDING-247` 记的活主机一致，
    #   ⭐ 这条互相印证：主机池探测本身是通的。
    "mootdx::server": {"index": "EX"},
    # [B类·容器类型] `get_stock_markets(symbols=...)` 要 **list**，
    #   库内 `assert isinstance(symbols, list), 'stock code need list type'`。
    #   扁平合成表给标量 `'000001'` ⇒ AssertionError（同 `FINDING-241` 的结构不可合成）。
    #   实测 `symbols=['000001','600000']` → **2 行** `[[0,'000001'],[1,'600000']]`
    #   （0=深市 / 1=沪市，即市场号推断表）
    "mootdx::quotes.get_stock_markets": {"symbols": ["000001", "600000"]},

    # ───────────────────────────────────────────────────────────────────
    # ⭐ `FINDING-304`（2026-08-09，`scripts/_r34_date_vs_shim.py` 的 2×2 实测）
    #
    # ⛔ **先记我自己的错**：我在 `_r34_tradingday.py` 里**同时**装了换池垫片、
    #    又换了日期，然后把 3 条转化全归因给"探针跑在周日"。那是**混淆变量**。
    #    2×2 拆开（日期 20260804/20260807 × 垫片 关/开）后实测：
    #      · `adata::stock.market.get_market`          → **垫片**的功劳（与日期无关）
    #      · `akshare::rv_from_stock_zh_a_hist_min_em` → **两者都要**
    #      · `akshare::stock_zt_pool_dtgc_em`          → **日期**的功劳（与垫片无关）
    #    ⇒ 只有 1/3 与日期有关。⛔ 且探针的 `date` 默认值本就是 `20260804`
    #      而**不是**"今天"，所以"周日"从一开始就不是机制。
    #
    # ⇒ 故这里只覆盖**实测确属日期问题**的那一条 + 那条"两者都要"的。
    #   ⛔ 不碰 `adata::stock.market.get_market`：它靠垫片即可，加日期是安慰剂。
    #   ⚠ 20260804 是探针全局默认的"已验证交易日"，而这两条实测在 20260804 为空、
    #     在 **20260807** 有数 ⇒ 说明 20260804 对**它们**不合适（可能是数据尚未回填），
    #     ⛔ 这不是"挑一个能出数的日子"：判据（cols 非空 AND rows>0）一字未改。
    "akshare::stock_zt_pool_dtgc_em": {
        "date": "20260807",     # 实测 4 行 / 16 列（20260804 → 0 行）
    },
    "akshare::rv_from_stock_zh_a_hist_min_em": {
        "start_date": "20260807", "end_date": "20260807",   # 实测 241 行 / 7 列
    },
}

#: ⛔⛔ 绝不填充的**可选**参数 —— 它们会限制返回规模。
#:
#: 这是 `FINDING-185` 的红线在打点器上的落点：如果我给 `limit=10` / `count=10`，
#: 每个接口都会**静默返回 10 行**，然后被记成 `OK`。
#: 那正是我上一轮写过的最有害的"修法"（用行数上限截断 baostock 死循环，
#: 吐出 20,000 行垃圾并标记 OK，真实只有 1,976 行）。
#: **挂死是响的，被截断的假数据是哑的。** 故这些名字即使在合成表里也不得作为可选填充。
NEVER_FILL_OPTIONAL = frozenset({
    "count", "limit", "page", "page_num", "page_size", "num", "size",
    "offset", "start_index", "top", "n",
})

#: ⭐ `FINDING-233`：**类构造参数**表 —— `_resolve()` 实例化类时用的 kwargs。
#:
#: 为什么需要它（实测根因，不是推测）：`_resolve()` 原本只试**无参构造**，失败就退回
#: **裸类**，于是后续 `fn(**kwargs)` 调的是**未绑定方法** ⇒ 报
#: `missing 1 required positional argument: 'self'`。
#: 而 `required_params()` 已正确跳过 `self` ⇒ "缺 self"只可能来自"拿到的是类不是实例"。
#: 实测代价：**25 条**接口被记成探针缺陷，其中至少 4 条本机就有真实数据可读
#: （`StdReader.daily` 1,205 行 / `StdReader.fzline` 3,408 行 —— 均为实测值）。
#:
#: ⛔ 只填**本地数据目录**这类"环境事实"，绝不填能限制返回规模的参数
#: （`NEVER_FILL_OPTIONAL` 的红线在这里同样成立）。
#: ⛔ 路径**不存在时不得注入** —— 见 `_ctor_kwargs()`：注入前 `exists()` 实测。
#:   否则会把"本机没装通达信"伪装成"接口可用"，那是 `FINDING-185` 式的哑数据。
#: ⭐ 经**环境变量**注入，两个理由都是实测逼出来的：
#:   ① 硬编码 `C:\new_tdx` 是本机事实，换机器就静默失效（且失效方向是"记成接口不可用"）；
#:   ② 反证腿**必须能跨进程边界** —— 探针在子进程执行（`FINDING-181`：baostock 能挂死主进程），
#:      子进程会**重新 import 本模块**，故在父进程改模块全局变量对它**完全无效**。
#:      我第一版反证腿就是这么写的：正反两腿都得 usable=4，变异**零信息量**
#:      （`FINDING-126` 的形状：变异未被证明能改变输出）。
TDX_LOCAL_DIR = Path(os.environ.get("FINAI_TDX_DIR") or r"C:\new_tdx")
TDX_LOCAL_VIPDOC = TDX_LOCAL_DIR / "vipdoc"

#: 键是 `lib::类的点分路径`（不含方法名）。值是构造 kwargs 的**生成器**，
#: 因为要在调用时点实测路径是否存在（模块导入时点判断会把状态写死）。
CTOR_ARG_OVERRIDES: dict[str, dict[str, object]] = {
    # mootdx 的 reader 全族要 `tdxdir`（通达信**安装根目录**，不是 vipdoc）
    "mootdx::reader.StdReader":            {"tdxdir": str(TDX_LOCAL_DIR)},
    "mootdx::reader.ExtReader":            {"tdxdir": str(TDX_LOCAL_DIR)},
    "mootdx::reader.ReaderBase":           {"tdxdir": str(TDX_LOCAL_DIR)},
    "mootdx::reader.Reader":               {"tdxdir": str(TDX_LOCAL_DIR)},
    "mootdx::reader.MooTdxDailyBarReader": {"tdxdir": str(TDX_LOCAL_DIR)},
    "mootdx::parse.BaseParse":             {"tdxdir": str(TDX_LOCAL_DIR)},
    # tdxpy 的 reader 全族要 `vipdoc_path`（**vipdoc 子目录**，与上面不同层级 ——
    # 实测：给根目录会拼出 `C:\new_tdx\sz\lday\…` 而真实路径是 `…\vipdoc\sz\lday\…`）
    "tdxpy::reader.TdxDailyBarReader":       {"vipdoc_path": str(TDX_LOCAL_VIPDOC)},
    "tdxpy::reader.TdxMinBarReader":         {"vipdoc_path": str(TDX_LOCAL_VIPDOC)},
    "tdxpy::reader.TdxLCMinBarReader":       {"vipdoc_path": str(TDX_LOCAL_VIPDOC)},
    "tdxpy::reader.TdxExHqDailyBarReader":   {"vipdoc_path": str(TDX_LOCAL_VIPDOC)},
    "tdxpy::reader.BlockReader":             {"vipdoc_path": str(TDX_LOCAL_VIPDOC)},
    "tdxpy::reader.CustomerBlockReader":     {"vipdoc_path": str(TDX_LOCAL_VIPDOC)},
    "tdxpy::reader.HistoryFinancialReader":  {"vipdoc_path": str(TDX_LOCAL_VIPDOC)},
}

#: 构造 kwargs 里哪些键是**必须实测存在的本地路径**。命中即校验，不存在则不注入。
_CTOR_PATH_KEYS = frozenset({"tdxdir", "vipdoc_path"})

#: API 类实例化后须调用的**会话初始化**方法序列（`FINDING-237`）。
#: 键是 `lib::类的点分路径`，值是 `[(方法名, kwargs)]`。
#: ⛔ 只补**必须显式调用**才能用的（tdxpy.connect / baostock.login 等），
#:    **不包括**库自己在构造时已完成的（mootdx StdQuotes 无参构造即可用）。
#: ⭐ 主机经**环境变量**可覆盖，与 `TDX_LOCAL_DIR` 同一个理由：反证腿必须能
#:   跨进程边界（子进程 spawn 会重新 import 本模块，父进程改全局变量对它无效）。
_TDX_HQ_HOST = os.environ.get("FINAI_TDX_HOST_OVERRIDE") or "60.12.136.250"
#: ⭐⭐ `FINDING-247`（2026-08-08）：扩展行情主机**换了 IP 和端口**。
#:   旧值 `112.74.214.43:7727` 实测 connect **成功**但每个方法都 0 行/报错 ——
#:   即"连上了一台不再提供服务的机器"，⛔ 这是最坏的失败形态：它不拒连，
#:   所以看起来像"接口没数据"，而不是"服务器不对"。
#:   证据来自 mootdx 自己的 `consts.EX_HOSTS`（`mootdx/consts.py:90-103`）：
#:   **9 个 7727 主机全部被作者注释掉**，取而代之的是 3 个 **7720** 端口主机。
#:   逐台实测（`artifacts/_exhq_server.json`）：
#:       47.112.95.207:7720  → `get_markets` **31 行** ✅
#:       218.75.75.18:7720   → TypeError（tdxpy 侧 connect 返回 False）
#:       58.49.110.76:7720   → 同上
#:       112.74.214.43:7727  → **0 行**（旧值，对照腿）
_TDX_EXHQ_HOST = os.environ.get("FINAI_TDX_EXHQ_HOST_OVERRIDE") or "47.112.95.207"
_TDX_EXHQ_PORT = int(os.environ.get("FINAI_TDX_EXHQ_PORT_OVERRIDE") or 7720)

SESSION_INIT_OVERRIDES: dict[str, list[tuple[str, dict]]] = {
    # tdxpy.hq 需 connect(ip, port)；主机池取第一个实测可连的（FINDING-189 实测 3/10 可连）
    "tdxpy::hq.TdxHq_API": [
        ("connect", {"ip": _TDX_HQ_HOST, "port": 7709}),
    ],
    # tdxpy.exhq（扩展行情）—— ⛔ 仍注册初始化序列，让失败以**库的真实异常**上报，
    # 而非因"未连接"被静默吞成 None → EMPTY_OK（那会把协议失败伪装成"该接口无数据"）。
    # ⚠ 上一版注释断言"协议在该主机实测不可用"，**结论对、归因错**：不可用的是
    #   那台**已退役的主机**，不是协议。换 `47.112.95.207:7720` 后 `get_markets`
    #   实测 31 行（`FINDING-247`）⇒ 协议是好的。
    "tdxpy::exhq.TdxExHq_API": [
        ("connect", {"ip": _TDX_EXHQ_HOST, "port": _TDX_EXHQ_PORT}),
    ],
    # mootdx StdQuotes / ExtQuotes 库自管连接，无参构造即可用 → 空序列（标记"已知不需要"）
    "mootdx::quotes.StdQuotes": [],
    "mootdx::quotes.ExtQuotes": [],
}


def _ctor_kwargs(lib: str, dotted_prefix: str) -> dict:
    """取该类的构造 kwargs，⛔ 路径不存在则**不注入**（诚实失败优于假成功）。"""
    raw = CTOR_ARG_OVERRIDES.get(f"{lib}::{dotted_prefix}")
    if not raw:
        return {}
    out = {}
    for k, v in raw.items():
        if k in _CTOR_PATH_KEYS and not Path(str(v)).exists():
            # 本机没有该目录 ⇒ 保持原样失败，让产物如实记录"缺本地数据"
            return {}
        out[k] = v
    return out


#: 这些必填参数**无法合成** —— 需要本地文件、代理对象、DataFrame 入参等。
#: 命中即跳过并记 `SKIPPED_UNSYNTHESIZABLE`，⛔ 不得记为"接口不可用"。
UNSYNTHESIZABLE = frozenset({
    "filename", "path", "block_file", "code_or_file", "data", "df",
    "proxy", "token", "pos", "file", "filepath", "output", "dest",
    "session", "client", "conn", "api", "func", "callback",
})

STATE_SKIPPED = "SKIPPED_UNSYNTHESIZABLE"


# ─────────────────────────────────────────────── 签名解析

def param_list(sig: str) -> list[str]:
    """从记录下来的签名字符串里取出参数列表。

    ⚠ BUGFIX（本轮实测）：必须**只取顶层括号内**的内容。
    我的第一版直接对整个字符串切逗号，于是把返回注解
    `) -> pandas.core.frame.DataFrame` 当成了参数 —— 实测该"参数名"出现 **340 次**，
    是频次第一，把真正的 `code`(65) 压到第二。
    按那份统计做合成表会得到一张全是垃圾键的表。
    修后 zero-required 从 617 变为 **958**，两个数字差 341 ≈ 那 340 个假参数。
    ⭐ 与 `FINDING-197` §24.5 同族：**统计之前先验证解析本身。**
    """
    s = sig.strip()
    if not s.startswith("("):
        return []
    depth = 0
    inner = ""
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                inner = s[1:i]
                break
    else:
        return []
    parts: list[str] = []
    cur = ""
    depth = 0
    for ch in inner:
        if ch in "[({":
            depth += 1
        elif ch in "])}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def _split_head(p: str) -> str:
    return p.split(":")[0].split("=")[0].strip()


def required_params(sig: str) -> list[str]:
    """返回必填参数名（无默认值、非 self/cls、非 *args/**kwargs）。"""
    out = []
    for p in param_list(sig):
        if p in ("self", "cls", "*", "/") or p.startswith("*"):
            continue
        if "=" in p:
            continue
        out.append(p.split(":")[0].strip())
    return out


def optional_params(sig: str) -> list[str]:
    """返回**可选**参数名（有默认值的）。

    ⭐ 为什么需要它（实测逼出来的第二个缺陷）：
    我的第一版只填必填参数，于是 `query_profit_data(code, year=None, quarter=None)`
    被调成 `query_profit_data(code='sz.000001')` —— baostock 在 year/quarter 为 None 时
    **返回 0 行且不报错**，被我记成 `EMPTY_OK`。
    而直接给 `year=2024, quarter=4` 实测**返回 1 行**。
    `query_all_stock(day=None)` 更直白：给 `day` 得 **7,288 行**，不给得 **0 行**。
    → **7 个 `EMPTY_OK` 里至少 6 个是我的调用约定问题，不是"该接口无数据"。**
    这正是 `FINDING-178` 的形状（mootdx 默认服务器静默返回 0 行）：
    **`rows=0` 与"确实没有数据"在返回值上无法区分。**
    """
    out = []
    for p in param_list(sig):
        if p in ("self", "cls", "*", "/") or p.startswith("*"):
            continue
        if "=" not in p:
            continue
        out.append(_split_head(p))
    return out


def synthesize_variants(sig: str, lib: str | None = None,
                        func_key: str | None = None) -> list[tuple[str, dict]]:
    """返回要**依次尝试**的调用变体。⭐ 这是实测逼出来的第三版设计。

    `func_key` (= `lib::func_name`) 用于 FINDING-218 函数级覆盖（FUNC_ARG_OVERRIDES）。
    若该函数在覆盖表里，则用覆盖表的 kwargs，只生成一个变体（已经是精确入参，无需再试"必填/可选"二变体）。
    若不在覆盖表里，走原有两变体逻辑。
    """
    # ⭐ FINDING-218：若有函数级精确覆盖，直接用，不走通用合成
    if func_key and func_key in FUNC_ARG_OVERRIDES:
        exact = FUNC_ARG_OVERRIDES[func_key]
        return [("func_overridden", exact)]

    minimal, missing = synthesize_kwargs(sig, lib, fill_optional=False)
    if missing:
        return []
    full, _ = synthesize_kwargs(sig, lib, fill_optional=True)
    variants = [("required_only", minimal)]
    if full != minimal:
        variants.append(("with_optional", full))

    page_params = [p for p in (required_params(sig) + optional_params(sig))
                   if p in PAGE_LIMIT_OVERRIDES]
    if page_params:
        paged = dict(minimal)
        for p in page_params:
            paged[p] = PAGE_LIMIT_OVERRIDES[p]
        if paged != minimal:
            variants.insert(0, ("first_page_only", paged))
    return variants


#: 状态优先级（越小越好）。⭐ `EMPTY_OK` 必须排在 `FAIL_DETERMINISTIC` **之后** ——
#: 端点明确回答"参数错"比静默 0 行更有信息量，且 0 行可能只是我的调用约定问题。
_STATE_RANK = {
    "OK": 0, "FAIL_DETERMINISTIC": 1, "EMPTY_OK": 2,
    "FAIL_GATEWAY": 3, "FAIL_UNREACHABLE": 4, "FAIL_PROBE_BUG": 5,
}


def _variant_rank(r: dict) -> tuple[int, int]:
    """排序键：先比状态，同状态比行数（多者胜）。⛔ 行数只在 OK 时有意义。"""
    return (_STATE_RANK.get(r.get("state", ""), 9), -(r.get("rows") or 0))


#: 「这次调用真的完成了」的状态 —— 它们携带的信息**不可伪造**：
#: `OK` 有行数与字段，`EMPTY_OK` 证明调用链走通了，`FAIL_DETERMINISTIC` 是端点亲口回答。
#: ⛔ 不得被"不可解释的失败"覆盖。
_CALL_COMPLETED_STATES = frozenset({"OK", "EMPTY_OK", "FAIL_DETERMINISTIC"})

#: 「已经花代价查明了原因」的状态。⭐ 比 `_CALL_COMPLETED_STATES` 宽：多了
#: `FAIL_GATEWAY`（已定位到网关/库解析）与 `FAIL_PROBE_BUG`（已定位到我们自己）。
#: ⛔ `FINDING-220` 实测代价：重测把 **7 条** `FAIL_GATEWAY`（"已知是我们自己解析失败"）
#:    覆盖成了 `FAIL_UNREACHABLE`（"不知道为什么不通"）—— 83 → 76。
#:    旧的保护面只含 `_CALL_COMPLETED_STATES`，故这 7 条在机制上得不到保护。
#: ⚠ 这个集合与 `verify_data_layer_complete.check_07` 的 `EXPLAINED` **刻意逐字一致** ——
#:    判据与写入侧用同一个谓词，否则又是一处 `FINDING-220-NEW-2` 式的口径分叉。
_EXPLAINED_STATES = frozenset({"OK", "EMPTY_OK", "FAIL_DETERMINISTIC",
                               "FAIL_GATEWAY", "FAIL_PROBE_BUG"})

#: 唯一的「不可解释」终态。⛔ 只有 `已解释 → 这个` 才算退化；
#: `FAIL_UNREACHABLE → FAIL_GATEWAY`（实测 17 条）是**改善**，
#: `FAIL_PROBE_BUG → FAIL_GATEWAY` 是 `FINDING-203` 的**修正方向**。
#: ⇒ 给五态强加全序本身就是错的，故这里只判这一个方向。
_UNEXPLAINABLE_STATE = "FAIL_UNREACHABLE"


def _superseded_by_bytes(old: dict, new: dict) -> bool:
    """旧结论（"我们传错入参"）是否已被新观测的字节数**证伪**。

    ⭐ `FINDING-214-NEW-1`。两个条件**必须同时**成立，缺一不可：

    ① 旧 `state` 是 `FAIL_PROBE_BUG` **且** 旧 `error` 命中"入参/调用约定"类标记
       —— 复用 `finai.sources.base._MISUSE_MARKERS`（⛔ 不再抄一份，
       `FINDING-59-NEW-1`：第二份副本必然与本体漂移）。
    ② 新观测 `bytes_received > 0` —— 即请求**确实发出且端点回了内容**。

    ⛔ 为什么这两个条件合起来才是"证伪"而不是"覆盖"：
       入参非法时库在**发请求之前**就抛异常（这正是 `FINDING-217` 用字节数
       裁决阶段的全部依据：`bytes_received == 0` ⇒ 没有响应可供解析）。
       故"旧 error 说入参错"与"新观测收到 20 万字节"**逻辑上不可同真** ——
       保留旧结论等于在产物里留一条已知为假的断言。

    ⛔ 反向限定（防它变成万能覆盖）：
       · 旧结论若是 `OK`/`EMPTY_OK`/`FAIL_DETERMINISTIC`（调用真的完成过）→ **不适用**，
         那类由 `FINDING-214` 的保护面负责，本函数返回 False。
       · 旧结论若是 `FAIL_GATEWAY`（已定位到"端点回了坏东西"）→ **不适用**，
         那本身就是"收到过字节"的结论，与新证据不矛盾。
       · 新观测没有字节数（`None`）→ **不适用**，不得据"没测到"下结论。
    """
    if (old.get("state") or "") != "FAIL_PROBE_BUG":
        return False
    br = new.get("bytes_received")
    if not isinstance(br, int) or br <= 0:
        return False
    err = (old.get("error") or "").lower()
    if not err:
        return False
    try:
        from finai.sources.base import _MISUSE_MARKERS  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return False   # 读不到权威词表就不判 —— ⛔ 绝不用本地副本兜底
    return any(mk in err for mk in _MISUSE_MARKERS)


def _merge_preserving_credible_evidence(
    old: dict | None, new: dict,
) -> tuple[dict, str | None]:
    """合并同一接口的新旧结果，**拒绝用低信息量证据覆盖高信息量证据**。

    ⛔ `FINDING-214` 实测：补打把 3 个接口已测到的 `OK`（90/100/294 行真实数据）
    覆盖成了 `FAIL_UNREACHABLE`/`FAIL_GATEWAY`。那违反本项目自己写下的可信度
    不对称原则 ——「成功永远可信（行数与字段无法伪造），失败一律不可解释」。
    用"不可解释"覆盖"已证实"是**信息销毁**：覆盖后没有任何人能知道它曾经成功。

    ⭐ 但也**不得反向作假**：真实下线必须能被看见。故修法是**两者都留** ——
    保留可信的旧状态作为 `state`，把新的失败观测追加进 `later_observations`，
    于是"间歇性"本身变成产物里的一等证据（这正是 `FINDING-175` 需要的形状）。

    Args:
        old: 该接口此前的记录（可能来自更早的批次），无则 `None`。
        new: 本次探测的记录。

    Returns:
        `(要落盘的记录, 保留原因)`。保留原因为 `None` 表示采用了 `new`。
    """
    if old is None:
        return new, None
    old_state, new_state = old.get("state"), new.get("state")

    # ⛔ **可解释性退化必须先判，不能等 rank 比完**（`FINDING-220` 的 7 条代价）。
    #    顺序不可颠倒的原因是实测的：`_STATE_RANK` 把 `FAIL_PROBE_BUG`(5) 排在
    #    `FAIL_UNREACHABLE`(4) **之后**，于是「已定位到我们自己写错」→「不知道为什么不通」
    #    会**通过** rank 检查被静默采用 —— 而那正是退化方向。
    #    ⇒ 给五态强加全序在这里会咬人（`check_07` 的注释记录了同一个坑），
    #      故这个方向必须单独判，且判据与 `check_07` 的 `EXPLAINED` 逐字一致。
    _degrades = (old_state in _EXPLAINED_STATES
                 and new_state == _UNEXPLAINABLE_STATE
                 and old_state != _UNEXPLAINABLE_STATE)

    # ⭐ `FINDING-214-NEW-1`：**旧结论已被新证据证伪时，保护它就是保留一条错结论。**
    #    实测形态（6 条 tushare 财务接口）：
    #      顶层 error="TypeError: 年度输入错误…"（断言"我们传错参数"）
    #      而新观测 bytes_received=202,634 —— 请求**已发出且端点回了 20 万字节**
    #    ⇒ "我们传错参数"这个旧结论**不可能仍然为真**：入参非法时请求根本发不出去。
    #    ⛔ 这不是放宽退化保护（那会请回 `FINDING-220` 的 7 条代价）——
    #      判据严格限定为「旧结论是**入参/调用约定**类，且新观测**证明请求已被接收**」，
    #      两个条件同时成立才覆盖，且旧结论移入 `superseded_observations` 留档。
    if _superseded_by_bytes(old, new):
        merged = dict(new)
        sup = list(old.get("superseded_observations") or [])
        sup.append({
            "state": old_state,
            "error": old.get("error"),
            "observed_at": old.get("observed_at"),
            "superseded_at": datetime.now(timezone.utc).isoformat(),
            "why": (f"旧结论断言入参/调用约定错误，但新观测 "
                    f"bytes_received={new.get('bytes_received')}>0 证明请求已被端点接收 "
                    f"⇒ 旧结论已不可能为真（FINDING-214-NEW-1）"),
        })
        merged["superseded_observations"] = sup
        # 保留旧的 later_observations（历史证据一律不丢）
        if old.get("later_observations"):
            merged["later_observations"] = old["later_observations"]
        return merged, (f"旧 {old_state}（入参类）已被新观测的 "
                        f"bytes_received={new.get('bytes_received')} 证伪，采用新结果")

    # 新结果不差且不构成退化 ⇒ 直接采用（含"同为 OK 取更新一次"）
    if not _degrades and _STATE_RANK.get(new_state, 9) <= _STATE_RANK.get(old_state, 9):
        return new, None
    # 旧结果既不是"调用真的完成了"、也不是"已定位原因" ⇒ 无需保护，采用新结果
    if not _degrades and old_state not in _CALL_COMPLETED_STATES:
        return new, None

    merged = dict(old)
    obs = list(merged.get("later_observations") or [])
    obs.append({
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "state": new_state,
        "error": new.get("error"),
        "timeout_used_s": new.get("timeout_used_s"),
        "variant": new.get("variant"),
        # ⭐ 本次尝试**测到的字节数**记在这里，⛔ **不得**提到记录顶层。
        #    理由是判据级的：顶层 `state`/`error` 来自被保留的那次尝试，
        #    而 `bytes_received` 来自本次 —— 把两者拼在同一层会造出一条
        #    "error 来自 A、字节来自 B" 的合成记录，于是
        #    CHECK 5（`classify_exception(error, bytes)` 必须重算出 `state`）
        #    会因为一个**从未同时发生过**的组合而变红或变绿。
        #    ⇒ 宁可让 CHECK 4 对这一条继续红（诚实的缺失），也不造合成观测。
        "bytes_received": new.get("bytes_received"),
        "note": ("这次没成功，但**不足以**推翻上面那次结论 —— "
                 "失败不可解释（FINDING-214）；且若上次是已定位原因"
                 "（FAIL_GATEWAY/FAIL_PROBE_BUG），退化成不可解释同样被拒绝"
                 "（FINDING-220 实测 7 条代价）。两者都留，间歇性因此可见。"),
    })
    merged["later_observations"] = obs
    merged["intermittent"] = True
    # ⭐ `FINDING-220-NEW-1`：这一条**确实在本次被探测过**，只是结论沿用了更可信的旧值。
    #    故必须留下本次的观测时刻与代理状态 —— 否则「本次尝试了哪些」在产物里不可见，
    #    而那正是 CHECK 6 要的东西。⛔ 不得因为"沿用旧结论"就连溯源一起沿用。
    merged["observed_at"] = new.get("observed_at") or datetime.now(timezone.utc).isoformat()
    if new.get("proxy_at_probe") is not None:
        merged["proxy_at_probe"] = new["proxy_at_probe"]
    merged["state_kept_from_earlier_run"] = True
    reason = (f"保留旧 {old_state}(rows={old.get('rows')})，"
              f"本次 {new_state} 记入 later_observations")
    return merged, reason


def synthesize_kwargs(sig: str, lib: str | None = None,
                      *, fill_optional: bool = True,
                      func_key: str | None = None) -> tuple[dict, list[str]]:
    """為必填参数合成实参，并**补填已知的可选参数**。返回 (kwargs, 无法合成的参数名)。

    ⭐ `lib` 参数是实测逼出来的：首批 baostock 探针 10/22 报
    「股票代码应为9位，如：sh.600000」—— 同一个参数名 `code` 在 baostock 要
    `sz.000001`、在 tushare 要 `000001.SZ`、在 mootdx 要 `000001`。
    不区分库就会把**我的格式错**记成 `FAIL_UNREACHABLE`（读者会读成"接口不可用"）。

    ⭐ `func_key` (= `lib::func_name`) 是 FINDING-218 实测逼出来的：
    同一个参数名 `symbol` 在不同函数语义相反（证券代码 vs 枚举值），
    必须函数级覆盖，库级覆盖不够细粒度。优先级：func_key > lib > 全局表。

    ⭐ 可选参数的补填是第二轮实测逼出来的，见 `optional_params()` 的说明。
    ⛔ 但 `NEVER_FILL_OPTIONAL`（count/limit/page…）**绝不补填** ——
       填了会静默限制返回规模，把"截断的假数据"标记成 OK（`FINDING-185` 的教训）。
    """
    # 优先级：函数级 > 库级 > 全局表
    func_overrides = FUNC_ARG_OVERRIDES.get(func_key or "", {})
    lib_overrides = LIB_ARG_OVERRIDES.get(lib or "", {})
    kw: dict = {}
    missing: list[str] = []

    def lookup(name: str):
        if name in func_overrides:
            return func_overrides[name], True
        if name in lib_overrides:
            return lib_overrides[name], True
        if name in ARG_SYNTHESIS:
            return ARG_SYNTHESIS[name], True
        return None, False

    for name in required_params(sig):
        if name in UNSYNTHESIZABLE:
            missing.append(name)
            continue
        val, ok = lookup(name)
        if ok:
            kw[name] = val
        else:
            missing.append(name)

    # 补填可选参数：只填**认识的**，且绝不填限制规模的那一类
    if fill_optional:
        for name in optional_params(sig):
            if name in NEVER_FILL_OPTIONAL or name in UNSYNTHESIZABLE or name in kw:
                continue
            val, ok = lookup(name)
            if ok:
                kw[name] = val

    return kw, missing


# ─────────────────────────────────────────────── 子进程执行体

class HardenFailedError(RuntimeError):
    """加固失败。⛔ 必须抛，不得静默继续（`FINDING-212`）。

    静默继续的后果：整批探针在**无保护状态**下跑完，而产物里没有任何痕迹。
    那样得到的失败结果不可解释，却长得和可解释的一模一样。
    """


def _harden() -> None:
    """关代理（复用 L0 的实现）+ 设 socket 默认超时。

    ⛔ `FINDING-212` 修复要点，三条都是实测逼出来的：

    ① **复用 `finai.sources.base.harden_requests_session()`，不再手写第二份。**
       本函数原先自己 patch 了一遍 `Session.__init__` —— 而 L0 层
       （`finai/sources/base.py:211`）早就有同样的实现，且带幂等标记
       `_finai_hardened`、有专门的测试（`test_sources_l0_traps.py:125`）钉住它。
       手写副本会与本体漂移，这正是项目红线⑪（取数必须走 `finai/sources/`）要防的事。

    ② **不再 `except Exception: pass`。** 原实现静默失败，
       于是"加固没生效"和"加固生效了"在产物里无法区分。改为抛
       `HardenFailedError`，让 `_worker` 把它当成一次可见的探针失败上报。

    ③ **docstring 不再声称"强制 IPv4"。** 原文这么写，但函数体里
       **没有任何 IPv4 相关代码**（实测：无 `AF_INET`、无 `getaddrinfo` 覆写）。
       ⚠ 这里**故意不调用** `force_ipv4()`：`FINDING-182` 已实测撤回
       "push2his 根因是 IPv6" 这个结论（重复 6 次 0/6），该函数
       **不承诺任何修复效果**；而现在启用它会让新结果与已收集的 1,079 条
       不可比。要启用须作为独立工作单元，并重打全批。
    """
    import socket
    socket.setdefaulttimeout(20)
    try:
        from finai.sources.base import harden_requests_session
        harden_requests_session()
    except Exception as exc:  # noqa: BLE001
        raise HardenFailedError(
            f"代理加固失败，拒绝在无保护状态下打点: {type(exc).__name__}: {exc}"
        ) from exc

    # ⭐ `FINDING-299`：push2 面「换池成员重试」垫片。
    #    与上面 ③ 里**故意不启用**的 `force_ipv4()` 是两回事，差别在证据：
    #    `force_ipv4` 重复 6 次 0/6、不承诺任何效果（`FINDING-182` 已撤回该归因）；
    #    本垫片实测 leg A 0/8 → leg B 8/8（`scripts/_r34_pinned_ip_test.py`），
    #    且带变异反证腿（钉到实测坏成员即回落到 0）⇒ 效果是被证明过的。
    #    ⛔ 失败**不静默**：装不上就抛 `HardenFailedError`，与 ② 同一条理由 ——
    #       否则"垫片没生效"和"生效了"在产物里无法区分。
    #    ⚠ 它会为每个 push2 host 打一次挑选探测（约 1 个请求），
    #       这是**故意的**：不实测就不知道哪个池成员供数，而写死 IP 会随轮换失效
    #       （`FINDING-247`）。挑选结果在进程内缓存，一个子进程最多挑一次。
    try:
        from finai.sources.base import install_eastmoney_pool_retry
        install_eastmoney_pool_retry()
    except Exception as exc:  # noqa: BLE001
        raise HardenFailedError(
            f"push2 池重试垫片安装失败: {type(exc).__name__}: {exc}"
        ) from exc

    # ⭐ `FINDING-264`：老 tushare 在 pandas 2.x 上因 `DataFrame.append` 被移除
    #    而**谎报网络故障**（库内 `except Exception: pass` 吞掉真异常）。
    #    ⛔ 必须与 `catalog_source.fetch()` 同源装载，否则产物声称能取、
    #    生产取不到（`FINDING-18` 的"管道事实 vs 市场事实"）。
    #    装不上不得静默：探针会把 7 条本可用接口误记为不可用。
    try:
        from finai.sources.base import install_pandas1_append_shim
        install_pandas1_append_shim()
    except Exception as exc:  # noqa: BLE001
        raise HardenFailedError(
            f"pandas1 append 垫片装载失败，拒绝在会误判 7 条接口的状态下打点: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    # ⭐ `FINDING-311`：brotli 解码器换实现。高压缩比响应在 `brotlicffi`
    #    的分块解码路径上**确定性**失败（实测 rows=2000 压缩比 7.4× ⇒ 3/3 复现），
    #    而同一段字节用 CPython `brotli` 分块喂是完好的。
    #    ⛔ 装不上不得静默：会把"我方解不开"误记为端点不可用（`F_PARSE_MISMATCH`）。
    #    ⚠ 与上面两个垫片同源装载（探针=生产），理由同 `FINDING-18`。
    try:
        from finai.sources.base import install_brotli_decoder_swap
        install_brotli_decoder_swap()
    except Exception as exc:  # noqa: BLE001
        raise HardenFailedError(
            f"brotli 解码器换装失败，拒绝在会误判高压缩比响应的状态下打点: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    # ⭐ `FINDING-327`：为「服务器漏发中间证书」的主机补齐证书链。
    #    ⛔ 校验**保持开启** —— 这与 `verify=False` 是相反的操作（详见函数 docstring）。
    #    ⛔ 必须与 `catalog_source.fetch()` 同源装载（`FINDING-264` 的形状第五次）。
    #    ⚠ 与上面几个垫片不同：装不上**不抛** —— 它只影响 2 个主机的 3 条接口，
    #      让整批探测因此失败会是更大的伤害；且它没有"静默降级"分支可走
    #      （降级只有 `verify=False` 那一条路，而那条路我们根本不实现）。
    from finai.sources.base import install_aia_chain_fix
    install_aia_chain_fix()

    # ⭐ `FINDING-329`：tushare 把自己算好的 `Referer` 注释掉没发 ⇒ 上交所端点回
    #    119 字节「系统繁忙」，库的裸 `except` 再把它翻成"请检查网络"。
    #    ⛔ 同源装载第六例（`FINDING-264` 的形状）。
    #    ⚠ 缺 tushare 时它自己返 False（不抛）；库内部结构变了则**照抛** ——
    #      理由同上面 ②：静默失败会让"没装上"与"装上了"无法区分。
    from finai.sources.base import install_tushare_referer_fix
    install_tushare_referer_fix()

    # ⭐ `FINDING-332`：「缺某个请求头就整体拒答」的主机（逐主机白名单）。
    #    ⛔ 同源装载第七例。⛔ 它**不碰 URL、不碰查询参数** —— 头决定"准不准我读"，
    #       不决定"读到哪些记录"，故与本轮禁止的读侧 filter 重写不是一回事。
    from finai.sources.base import install_required_headers
    install_required_headers()

    # 自证：加固后新建的 session 必须真的不信任环境代理。
    # ⭐ 只断言"设置生效"，不发网络请求 —— 子进程里发请求会污染 I/O 计数。
    import requests
    if requests.Session().trust_env is not False:
        raise HardenFailedError(
            "harden_requests_session() 返回了但 Session().trust_env 仍非 False —— "
            "加固未生效（FINDING-183 的形状：类属性被 __init__ 覆盖）"
        )


def _describe(obj) -> dict:
    """把返回值压成可 JSON 化的证据：行数 + 字段 + 首行。⛔ 不返回大对象。"""
    from collections.abc import Mapping

    import pandas as pd

    if obj is None:
        return {"rows": 0, "cols": None, "note": "returned None"}
    if isinstance(obj, pd.DataFrame):
        n = len(obj)
        return {
            "rows": n,
            "cols": [str(c) for c in obj.columns][:40],
            "dtypes": {str(c): str(t) for c, t in list(obj.dtypes.items())[:12]},
            "first_row": ({str(k): str(v)[:60] for k, v in obj.iloc[0].items()}
                          if n else None),
        }
    if isinstance(obj, pd.Series):
        return {"rows": len(obj), "cols": [str(obj.name)],
                "first_row": str(obj.iloc[0])[:60] if len(obj) else None}
    if isinstance(obj, dict):
        return {"rows": len(obj), "cols": [str(k) for k in list(obj)[:20]]}
    if isinstance(obj, (list, tuple)):
        # ⭐ `FINDING-238`：元素若是 Mapping，**字段名逐字存在于 payload 里** ——
        #    原实现一律给 `cols=None`，把已观测到的字段信息**丢弃**了。
        #    tdxpy 的二进制协议返回 `list[OrderedDict]`，实测
        #    `get_xdxr_info` 79 行含 `['year','month','day','category',…]`，
        #    而同一批里返回**裸 OrderedDict** 的 `get_finance_info` 却因为走 dict
        #    分支而拿到了 cols —— 同样的信息，容器差一层就被丢掉。
        # ⛔ 元素**不是** Mapping 时（`list[int]`、`list[str]`）**必须仍为 None**：
        #    payload 里确实没有字段名，凭空造就是 `FINDING-185` 式的哑数据。
        cols = None
        if obj and all(isinstance(e, Mapping) for e in obj[:200]):
            seen: dict[str, None] = {}   # dict 保序 = 按首次出现排序
            for e in obj[:200]:
                for k in e:
                    seen.setdefault(str(k), None)
            cols = list(seen)[:40]
        return {"rows": len(obj), "cols": cols,
                "first_row": str(obj[0])[:200] if obj else None}
    return {"rows": 1, "cols": None, "first_row": str(obj)[:200]}


def _resolve(lib: str, dotted: str):
    """把 `sub.Class.method` 这样的记录名解析成可调用对象。

    `FINDING-176`：接口常挂在子模块的类上（`mootdx.quotes.Quotes.bars`），
    只反射顶层会漏掉几乎全部 —— 故解析必须支持逐段下钻，且遇到类要实例化。

    `FINDING-237`：socket 客户端需 `connect()` 才能用，否则方法静默返回空。
    实例化后查 `SESSION_INIT_OVERRIDES`，若命中则依次调用初始化方法。
    """
    import importlib
    import inspect

    obj = importlib.import_module(lib)
    segs = dotted.split(".")
    instantiated_class_path = None  # 记录实例化发生的层级，用于查 SESSION_INIT
    for i, seg in enumerate(segs):
        nxt = getattr(obj, seg, None)
        if nxt is None:  # 可能是未自动导入的子模块
            obj = importlib.import_module(f"{getattr(obj, '__name__', lib)}.{seg}")
            continue
        # 类：需实例化才能拿到绑定方法。
        # ⭐ `FINDING-233`：先查 `CTOR_ARG_OVERRIDES` —— 这些类的必填构造参数是
        #    **本地数据目录**，无参构造必然失败，而失败后退回裸类会让后续调用
        #    报 `missing 1 required positional argument: 'self'`（25 条的根因）。
        #    ⛔ 仍保留"失败退回裸类"作为兜底：目标若是 staticmethod 依然可调用。
        if inspect.isclass(nxt):
            ck = _ctor_kwargs(lib, ".".join(segs[: i + 1]))
            try:
                obj = nxt(**ck) if ck else nxt()
            except Exception:
                obj = nxt  # 退回类本身，若目标是 staticmethod 仍可调用
            else:
                # ⭐ FINDING-237：会话初始化**必须在此处**（实例仍在手上）。
                # ⛔ 放到循环外就晚了：那时 `obj` 已经是绑定方法，
                #    `getattr(method, "connect")` 返回 None → 静默跳过 → 19 条全无变化。
                #    这正是我这次修复的第一版缺陷，与 `FINDING-126` 同形
                #    （变异未被证明能改变输出）。
                instantiated_class_path = ".".join(segs[: i + 1])
                init_seq = SESSION_INIT_OVERRIDES.get(
                    f"{lib}::{instantiated_class_path}")
                if init_seq:  # 空序列 = 已知不需要初始化，无需动作
                    for method_name, method_kwargs in init_seq:
                        m = getattr(obj, method_name, None)
                        if not callable(m):
                            raise AttributeError(
                                f"SESSION_INIT_OVERRIDES 指定的 {method_name!r} "
                                f"在 {instantiated_class_path} 上不可调用 —— "
                                f"覆盖表与库已漂移，拒绝静默继续")
                        m(**method_kwargs)
        else:
            obj = nxt
    return obj


def _worker(lib: str, dotted: str, kwargs: dict, q, bytes_counter: mp.Value | None = None) -> None:
    """在子进程里执行一次探测。

    子进程隔离是**硬要求**：baostock 实测能挂死主进程，
    且 `socket.setdefaulttimeout` 约束不住它（`FINDING-181`）。

    ⛔ `_harden()` **必须在 try 内**且单独成段（`FINDING-212` 修复时差点搞错）。

    ⭐ `FINDING-217`：子进程里安装 socket+ssl 字节计数器，随异常一并回传。
    **必须同时 patch `socket.socket` 与 `ssl.SSLSocket`**（FINDING-216 实测）：
    ssl.SSLSocket 覆写了 recv/recv_into，若只 patch 基类则 HTTPS 全盲。

    ⭐ `FINDING-220` 修法②：`bytes_counter` 是共享内存（mp.Value），
    主进程在 terminate 后仍能读到。⛔ mp.Queue 在子进程被 terminate 时不刷盘。
    """
    # ⛔ `FINDING-221`：`socket` **必须在此显式导入**。模块级没有它，而另外两处
    #    `import socket` 都在别的函数（`_harden()` / 网络基线探测）的局部作用域里，
    #    不进模块全局。缺这一行 → 下面装计数器时 `NameError`，且发生在**任何网络调用之前**
    #    → `q.put` 零次 → 主进程 `q.get(timeout)` 必然超时 → 每条探针都被记成 HUNG。
    #    该缺陷曾在 HEAD 上存活四个提交、白跑 136 分钟，因为验证只停在单元测试与静态阅读。
    import socket
    import ssl as _ssl

    # ⭐ `FINDING-298`：py3.11 的 `ssl` 没有 `OP_LEGACY_SERVER_CONNECT`
    #    （该名字 3.12 才加），而 akshare 有 2 处直接引用它
    #    （`fund/fund_amac.py:209`、`fx/fx_c_swap_cm.py:20`）
    #    ⇒ `AttributeError` 在**任何网络调用之前**抛出 ⇒ 被记成 `FAIL_PROBE_BUG`。
    #    这不是端点缺陷，是本机 Python 版本与库的落差（`FINDING-233/234` 同形）。
    #    补的是 OpenSSL 的裸值 `SSL_OP_LEGACY_SERVER_CONNECT = 0x4`，语义与 3.12 一致。
    #    ⛔ 必须在**子进程内**补：worker 由 spawn 重新 import 本模块，
    #       父进程改 `ssl` 的模块属性对它无效（与 `TDX_LOCAL_DIR` 用环境变量同一个理由，见 :471）。
    #    ⭐ 实测收益：`amac_person_bond_org_list` 316 行 4 字段、
    #       `fx_c_swap_cm` 12 行 5 字段（`scripts/_r34_probebug_verify.py`）。
    if not hasattr(_ssl, "OP_LEGACY_SERVER_CONNECT"):
        _ssl.OP_LEGACY_SERVER_CONNECT = 0x4  # type: ignore[attr-defined]

    _io_state: dict = {"received": 0}

    def _bump(n: int) -> None:
        _io_state["received"] += n
        # ⭐ FINDING-220 修法②：同时写共享内存，主进程在 terminate 后仍能读到
        if bytes_counter is not None:
            with bytes_counter.get_lock():
                bytes_counter.value += n

    # 安装计数器：必须覆盖基类 + SSLSocket 两层（FINDING-216：只 patch 基类则 HTTPS 全盲）
    for _cls in (socket.socket, _ssl.SSLSocket):
        _orig_recv = getattr(_cls, "recv", None)
        _orig_ri   = getattr(_cls, "recv_into", None)
        if _orig_recv is not None:
            def _mk_recv(_o=_orig_recv):
                def _r(self, n=8192, *a, **k):
                    b = _o(self, n, *a, **k)
                    _bump(len(b) if b else 0)
                    return b
                return _r
            setattr(_cls, "recv", _mk_recv())
        if _orig_ri is not None:
            def _mk_ri(_o=_orig_ri):
                def _ri(self, buf, *a, **k):
                    n = _o(self, buf, *a, **k)
                    _bump(n or 0)
                    return n
                return _ri
            setattr(_cls, "recv_into", _mk_ri())

    try:
        _harden()
    except BaseException as exc:  # noqa: BLE001
        q.put(("HARDEN_FAILED", f"{type(exc).__name__}: {str(exc)[:300]}"))
        return
    try:
        # ⭐ `FINDING-323`：把 `@finai:` 哨兵解析成**当次现取**的真值。
        #    ⛔ 必须与 `catalog_source.fetch()` 同源解析（`FINDING-264`/`-319`
        #       的形状：只在一侧解析 ⇒ 产物声称能取、生产取不到）。
        #    ⛔ 解析放在 try 内：取凭证失败要作为**一次可见的探针失败**上报，
        #       而不是静默退回"不传 token"（那会把"取不到凭证"伪装成"接口不可用"）。
        from finai.sources.base import resolve_deferred_args
        kwargs = resolve_deferred_args(kwargs)
        if lib == "baostock":
            obj = _probe_baostock(dotted, kwargs)
        else:
            fn = _resolve(lib, dotted)
            if not callable(fn):
                q.put(("EXC", "TypeError",
                       f"resolved object is not callable: {dotted}", "",
                       _io_state["received"]))
                return
            obj = fn(**kwargs)
        q.put(("RESULT", _describe(obj)))
    except BaseException as exc:  # noqa: BLE001
        # ⭐ FINDING-217：5 元组，末位是 bytes_received
        q.put(("EXC", type(exc).__name__, str(exc)[:400],
               traceback.format_exc()[-400:], _io_state["received"]))


def _probe_baostock(dotted: str, kwargs: dict):
    """baostock 的 ResultSet 必须**逐行消费**才能推进游标。

    ⛔ `FINDING-185`：`rs.next()` 只在消费行数据时推进 —— 不调 `get_row_data()`
    就永远返回 `True`。那是**死循环**，不是阻塞。
    我的第一版"修法"用行数上限强行跳出，吐出 20,000 行垃圾并标记为 OK
    （真实只有 1,976 行）——**比它要修的缺陷更有害**。
    故此处上限**触发即 raise**，绝不静默截断。
    """
    import baostock as bs
    import pandas as pd

    fn = getattr(bs, dotted, None)
    if fn is None or not callable(fn):
        raise AttributeError(f"baostock has no callable {dotted!r}")
    bs.login()
    try:
        rs = fn(**kwargs)
        if not hasattr(rs, "next"):
            return rs
        if getattr(rs, "error_code", "0") != "0":
            raise RuntimeError(
                f"baostock error_code={rs.error_code} msg={getattr(rs,'error_msg','')}")
        rows = []
        LIMIT = 60000
        while rs.next():
            rows.append(rs.get_row_data())
            if len(rows) > LIMIT:
                raise RuntimeError(
                    f"baostock 游标未终止: 已取 {len(rows)} 行仍未结束 —— "
                    "疑似未消费行数据导致的死循环，不得当作真实数据")
        return pd.DataFrame(rows, columns=rs.fields) if rows else pd.DataFrame(columns=rs.fields)
    finally:
        try:
            bs.logout()
        except Exception:  # noqa: BLE001, S110
            pass


# ─────────────────────────────────────────────── 主流程

def probe_one(lib: str, dotted: str, kwargs: dict, timeout: int) -> dict:
    """跑一个探针，带外部超时。返回五态之一 + 证据。"""
    from finai.sources.base import (
        EMPTY_OK, FAIL_PROBE_BUG, FAIL_UNREACHABLE, OK, classify_exception,
    )

    q: mp.Queue = mp.Queue()
    # ⭐ FINDING-220 修法②：共享内存计数器，terminate 后仍可读
    bytes_counter = mp.Value('i', 0)
    p = mp.Process(target=_worker, args=(lib, dotted, kwargs, q, bytes_counter), daemon=True)
    t0 = time.time()
    p.start()
    try:
        kind, *rest = q.get(timeout=timeout)
    except (_queue.Empty, EOFError):
        p.terminate()
        p.join(3)
        # ⭐ HUNG 早返回分支现在能带回 bytes_received
        with bytes_counter.get_lock():
            hung_bytes = bytes_counter.value
        return {"state": FAIL_UNREACHABLE, "rows": None,
                "error": f"HUNG_NO_RESULT_IN_{timeout}s",
                "note": "子进程超时被杀 —— ⛔ 不可解释为接口不可用",
                "bytes_received": hung_bytes,
                "elapsed_s": round(time.time() - t0, 2)}
    finally:
        if p.is_alive():
            p.terminate()
            p.join(3)

    elapsed = round(time.time() - t0, 2)
    if kind == "RESULT":
        d = rest[0]
        rows = d.get("rows") or 0
        return {"state": OK if rows > 0 else EMPTY_OK, **d, "elapsed_s": elapsed}

    # ⛔ FINDING-212：加固失败必须在 3 元组解包之前（该消息只带 1 个元素）
    if kind == "HARDEN_FAILED":
        return {"state": FAIL_PROBE_BUG, "rows": None,
                "error": f"HARDEN_FAILED: {rest[0]}",
                "note": ("代理加固未生效，已拒绝在无保护状态下探测 —— "
                         "⛔ 这是我们自己的环境/代码问题，与数据源无关（FINDING-212）"),
                "elapsed_s": elapsed}

    # ⭐ FINDING-217：EXC 消息升级为 5 元组（末位 bytes_received）
    #    兼容旧的 4 元组（例如 baostock 路径和 "not callable" 分支）
    if len(rest) == 4:
        exc_name, exc_text, tb, bytes_received = rest
    else:
        exc_name, exc_text, tb = rest[0], rest[1], rest[2] if len(rest) > 2 else ""
        bytes_received = None

    fake = type(exc_name, (Exception,), {})(exc_text)
    return {"state": classify_exception(fake, bytes_received), "rows": None,
            "error": f"{exc_name}: {exc_text[:200]}",
            "traceback_tail": tb[-200:],
            "bytes_received": bytes_received,
            "elapsed_s": elapsed}


#: TDX 主机池。⭐ 来自 `FINDING-189` 的实测（生产内置 10 台里 3 台可连），
#: 而非我凭印象列的 2 台 —— `FINDING-198` 正是那个窄样本造成的。
TDX_HOSTS = (
    ("60.12.136.250", 7709),
    ("218.75.126.9", 7709),
    ("115.238.56.198", 7709),
    ("180.153.18.170", 7709),   # 2026-08-06 实测 0/5 稳定不可达，保留以记录变化
)

#: 只有这些库走裸 TCP 的 TDX 通道；其余库的打点与 TDX 可达性无关。
TDX_LIBS = frozenset({"mootdx", "tdxpy"})

# ─────────────────────────────────────────── HUNG 重测腿（FINDING-205）
#
# ⛔ 实测教训：35 秒超时把**慢但完全可用**的接口判成了 `FAIL_UNREACHABLE`。
#    `macro_china_cpi_yearly`  59.4s -> 477 行   ✅ 可用
#    `fund_etf_spot_em`        53.9s -> 1,567 行 ✅ 可用
#    两条此前都被记成"不可解释的失败"，而它们是成功的。
#
# ⭐ 更隐蔽的一层：**139 个 akshare 接口内部有分页循环**（按函数体统计，非签名）。
#    我最初按"签名里有无 page 参数"判，得出"只有 3 个" —— 少了 46 倍。
#    `amac_manager_info` 签名是 `()`，内部却是 `for page in range(total_page)`。
#    amac API 实测 `totalPages=2480 / totalElements=247,915 / 单页 2.1s`
#    ⇒ 默认 `end_page="2000"` 需约 70 分钟，任何超时都救不了它，只能改参数。
#
# 故分两手处理：
#   ① `PAGE_LIMIT_OVERRIDES`：对已知分页接口显式给小值，产物标注"仅测首页"
#   ② `HUNG_RETRY_TIMEOUT`：仍 HUNG 的做一次长超时重测，两次都 HUNG 才记不可解释

#: 分页参数覆盖表。⛔ 不是"限制返回规模"（那是 `NEVER_FILL_OPTIONAL` 禁止的），
#: 而是**避免把全量遍历当成单次调用来测**。产物里必须标注这一点，
#: 否则读者会把"首页 100 行"误读成"该接口只有 100 行"。
PAGE_LIMIT_OVERRIDES: dict[str, object] = {
    "end_page": "1",
    "start_page": "1",
}

#: HUNG 后的重测超时。取 150s 是因为实测最慢的**不分页**接口约 60s，
#: 留 2.5 倍余量；⛔ 不取更大值 —— 分页接口需要几十分钟，那不是超时能解决的问题。
HUNG_RETRY_TIMEOUT = 150

# ─────────────────────────────────────────────── 限流（FINDING-200）
#
# ⛔ 阈值来自 `a-stock-data/SKILL.md` 的「东财风控阈值（社区实测 2026-05）」，
#    不是我估的：>5 请求/秒 高风险 · 1 分钟 ≥200 中高 · **5 分钟 ≥300 触发封禁** ·
#    并发 ≥10 高风险。铁律是"串行不并发 + 间隔 ≥1s + 抖动 + 复用会话 + 带 UA"。
#    该文档明写：「**AI 跑批量循环逐个拉龙虎榜/资金流是被封的头号元凶**」。
#
# ⚠ 为什么这件事必须在跑全批**之前**做完：被封的表现之一是**返回空数据**，
#    会被契约判成 `EMPTY_OK` —— 一个**看起来像结论**的东西。
#    封禁一旦发生，其后所有探针的结论都不可解释（`FINDING-175`），
#    且偏差会**随打点顺序系统性变化**（越晚打点越可能落在封禁窗口）。
#    事后无法把"封禁造成的空"与"该接口真的没数据"分开 —— 只能重跑，而重跑会再次触发。

#: **风控域** -> 最小间隔秒数。⭐ 键是风控域（按**主机**聚合），
#: ⛔ 不是 `endpoint_family()` 按函数名后缀猜出来的"族"（`FINDING-408` ②）。
#: 东财最严；通达信/腾讯据 SKILL.md 实测"不封 IP"。
DOMAIN_MIN_INTERVAL: dict[str, float] = {
    "eastmoney": 1.5,
    "ths": 1.2,        # 同花顺实测加过反爬 401
    "cninfo": 1.0,
    "sina": 0.8,
    "legulegu": 0.8,
    "other": 0.5,
    "local": 0.0,      # 裸 TCP / 本地读取，不受 HTTP 风控约束
}

#: 同一主机触发封禁的文档阈值（`a-stock-data/SKILL.md`，社区实测 2026-05）。
#: ⛔ 这是**外部事实**，不是我们可以调的旋钮。
BAN_THRESHOLD_PER_HOST = 300

#: 风控域 -> 5 分钟滑动窗口的**自设**上限。
#: ⛔ `FINDING-408` ①：这里曾是 `{"eastmoney": 240}`，而 240 在 1.5s 间隔下
#:    **结构上不可达** —— 240 个 hit 之间有 239 个间隔 ⇒ 最短跨度
#:    `1.5×239 = 358.5s > 300s` ⇒ 300s 窗口过滤后 `len(hits)` 永远到不了 240
#:    ⇒ 退避分支是死代码，而产物照样把 240 记作"自设余量 20%"的出处。
#: ⭐ 1.5s 下可达的最大上限是 `300//1.5 = 200`；取 180 ⇒ `1.5×179 = 268.5s < 300s`
#:    真的会触发，且只到封禁阈值的 60%。⛔ 上调回 240 是反方向。
DOMAIN_WINDOW_LIMIT: dict[str, int] = {"eastmoney": 180, "ths": 200}
WINDOW_SECONDS = 300

#: ⚠ 兼容别名 —— 旧名字里的 "FAMILY" 已名不符实（键现在是风控域）。
#: 保留是为了不打断既有读者（`tests/test_lessons_are_enforced.py`、
#: `scripts/build_interface_matrix_doc.py`）；新代码请用 `DOMAIN_*`。
FAMILY_MIN_INTERVAL = DOMAIN_MIN_INTERVAL
FAMILY_WINDOW_LIMIT = DOMAIN_WINDOW_LIMIT


def assert_window_budgets_reachable(
    intervals: dict | None = None,
    limits: dict | None = None,
    window: float | None = None,
) -> None:
    """**启动自检**：任何窗口上限都必须在其间隔下可达，否则是死配置。

    判据 `interval × (limit - 1) < window`：`limit` 个 hit 之间有 `limit-1`
    个最小间隔，故这是这批 hit 能挤进的**最短跨度**；它 >= 窗口长度时，
    窗口过滤后的计数永远到不了 `limit`，退避分支成死代码。

    ⛔ 为什么必须是**启动**自检而不是一条测试：`FINDING-408` 实测的代价是
       "上限从未生效"这件事在产物里**看起来像正常**——
       `backoffs_triggered: []` 既可能是"没逼近阈值"，也可能是
       "阈值不可达 + 大部分请求不计数"，而产物无法区分这两者
       （`FINDING-381`：没验过分母的守卫永远是绿的）。
    """
    iv = DOMAIN_MIN_INTERVAL if intervals is None else intervals
    lim = DOMAIN_WINDOW_LIMIT if limits is None else limits
    w = WINDOW_SECONDS if window is None else window
    dead = []
    for dom, limit in lim.items():
        gap = iv.get(dom, 0.5)
        span = gap * (limit - 1)
        if span >= w:
            dead.append(
                f"{dom}: {gap}s × ({limit}-1) = {span:.1f}s >= 窗口 {w}s ⇒ "
                f"该上限永不可达（此间隔下可达上限 <= {int(w // gap)}）")
        if limit >= BAN_THRESHOLD_PER_HOST:
            dead.append(f"{dom}: 自设上限 {limit} >= 封禁阈值 "
                        f"{BAN_THRESHOLD_PER_HOST} ⇒ 没有余量")
    assert not dead, (
        "限流窗口预算是死配置（`FINDING-408` ①）：\n  " + "\n  ".join(dead))
    assert HOST_ATTRIBUTION, (
        "主机归属表为空 ⇒ 窗口预算会退回按函数名后缀计（`FINDING-408` ②）。"
        "重新生成：python scripts/gen_probe_host_attribution.py")


assert_window_budgets_reachable()   # ⛔ import 即自检，死配置立刻变红


def endpoint_family(lib: str, name: str) -> str:
    """按库与函数命名判定目标端点族。**只用于展示/归档，⛔ 不得用作限流键。**

    ⚠ 这是**启发式**：akshare 用后缀标源（`_em`/`_sina`/`_ths`/`_cninfo`/`_lg`），
    但 `other` 桶里必然混着一部分真实东财端点（命名不带 `_em`）。
    ⛔ `FINDING-408` ② 量化了"必然"到底多大：实测 **209 条**确证打
       `*.eastmoney.com` 的记录被这个函数判成了非 eastmoney
       （产物范围内 180 条：akshare 132 + efinance 34 + adata 13 + tushare 1）。
       名字后缀是**库作者的命名习惯**，不是**主机** —— 拿它当风控键是范畴错误。
    ⇒ 限流请用 `rate_limit_domain()`。
    """
    if lib in TDX_LIBS:
        return "local"
    n = name.lower()
    if n.endswith("_em") or "_em_" in n:
        return "eastmoney"
    if "_ths" in n or "ths_" in n:
        return "ths"
    if "cninfo" in n:
        return "cninfo"
    if "_sina" in n or "sina_" in n:
        return "sina"
    if "_lg" in n:
        return "legulegu"
    return "other"


def rate_limit_domain(lib: str, name: str) -> str:
    """**限流键**：该接口所属的风控域（同一域共用一个 5 分钟预算）。

    ⭐ 顺序是"实测优先"：先查 `scripts/probe_host_attribution.py` 的
    **主机归属表**（来源：`inspect.getsource` 读已安装包源码里的 URL 主机），
    取不到才回落 `endpoint_family()` 启发式。

    ⛔ 回落不是"猜得差不多"：归属表里没有的键意味着**没有源码证据**，
       其中仍可能藏着东财端点（残余盲区，见 `FINDING-408` 报告）。
       故回落只是保底，不是等价物 —— 归属表被删时启动自检会变红。
    """
    dom = attributed_domain(lib, name)
    if dom:
        return dom
    return endpoint_family(lib, name)


class Throttle:
    """按**风控域**串行限流 + 5 分钟滑动窗口退避。

    ⛔ 触发退避时**打印并记录**，绝不静默 sleep —— 静默限流会让
    "这批数据是在什么速率下取的"变成不可复核的信息（`FINDING-125` 的形状）。

    ⛔ `FINDING-408`：`wait()` 的入参**必须**是 `rate_limit_domain()` 的结果。
       传 `endpoint_family()` 的结果会让 209 条确证打东财的请求落进无上限的
       `other` 桶（既不计数也不退避）⇒ 最坏合法顺序下同一主机 5 分钟峰值
       实测 401 次（产物范围内 369），越过封禁阈值 300，且全程 0 退避。
    """

    def __init__(self) -> None:
        self.last: dict[str, float] = {}
        self.hits: dict[str, list[float]] = {}
        self.backoffs: list[dict] = []

    def wait(self, domain: str) -> None:
        now = time.time()
        gap = DOMAIN_MIN_INTERVAL.get(domain, 0.5)
        # ① 最小间隔（按风控域 ⇒ 同一主机的所有请求互相约束）
        prev = self.last.get(domain)
        if prev is not None and gap > 0:
            delay = gap - (now - prev)
            if delay > 0:
                time.sleep(delay)
                now = time.time()
        # ② 5 分钟窗口
        limit = DOMAIN_WINDOW_LIMIT.get(domain)
        if limit:
            hits = [t for t in self.hits.get(domain, []) if now - t < WINDOW_SECONDS]
            if len(hits) >= limit:
                sleep_for = WINDOW_SECONDS - (now - hits[0]) + 1
                print(f"    ⏸ 主动退避 {sleep_for:.0f}s —— 风控域 {domain} 在 5 分钟内"
                      f"已发 {len(hits)} 次（自设上限 {limit}，文档阈值 "
                      f"{BAN_THRESHOLD_PER_HOST} 触发封禁）")
                self.backoffs.append({"domain": domain, "at": now,
                                      "window_hits": len(hits),
                                      "slept_s": round(sleep_for, 1)})
                time.sleep(max(0.0, sleep_for))
                now = time.time()
                hits = [t for t in hits if now - t < WINDOW_SECONDS]
            hits.append(now)
            self.hits[domain] = hits
        self.last[domain] = now


def _proxy_state_now() -> dict:
    """当前进程视角的代理状态。⭐ 逐条记录用，使代理污染可**按条**归因。

    ⛔ `FINDING-211` 的教训：1,029 条主扫描全程在系统代理开启下跑完，而产物里
    `network_degraded` 恒为 false，且**没有逐条时间戳**，于是
    `FINDING-211` 记下的 13:21–16:57 梯子窗口**映射不到任何一条记录** ——
    那批失败的代理污染因此**永久不可判**。
    ⇒ 这个函数的唯一目的是：让**将来**重打的记录不再落进同一个陷阱。
    ⚠ 它记的是「探测那一刻我们看到的代理配置」，不是「请求真的走了代理」——
      后者需要抓包，不在本阶段范围内；故字段名是 `proxy_at_probe` 而非 `used_proxy`。
    """
    import os as _os
    import urllib.request
    pr = urllib.request.getproxies()
    return {
        "http": pr.get("http"),
        "https": pr.get("https"),
        "env_http_proxy": _os.environ.get("HTTP_PROXY") or _os.environ.get("http_proxy"),
        "env_https_proxy": _os.environ.get("HTTPS_PROXY") or _os.environ.get("https_proxy"),
        "any_proxy": bool(pr.get("http") or pr.get("https")),
    }


def network_gate(need_tdx: bool = False) -> dict:
    """打点前的网络基线。⛔ 不过则挂起 —— 否则失败结果不可解释（FINDING-175）。

    ⚠ `FINDING-198`：TDX 腿此前是**无条件**阻塞项，且只探 2 台主机、要求 2/2。
    实测 `180.153.18.170` 稳定不可达（0/5）后，门禁永久为假，
    **连根本不走 TDX 的 baostock/akshare 打点也被挡住**。
    现改为：① 仅当本批含 `TDX_LIBS` 的库时才作为阻塞条件；
            ② 主机池扩为实测过的 4 台，判据 **≥1 台可连**（裸 TCP 只需一条链路）；
            ③ 逐台结果落档，不只记比例。
    ⛔ 修法**不得**放宽成"失败也继续" —— 那会退回 `FINDING-175` 之前的状态。
    """
    import socket

    import requests

    res: dict = {}
    http_pass = True
    for name, url in {
        "baidu(境内基准)": "http://www.baidu.com",
        "datacenter-web(东财可用面)": "https://datacenter-web.eastmoney.com/api/data/v1/get",
    }.items():
        ok = 0
        for _ in range(3):
            s = requests.Session()
            s.trust_env = False
            try:
                r = s.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                ok += r.status_code < 500
            except Exception:  # noqa: BLE001, S110
                pass
        res[name] = f"{ok}/3"
        if ok < 2:
            http_pass = False

    per_host: dict[str, str] = {}
    tcp_ok = 0
    for host, port in TDX_HOSTS:
        try:
            socket.create_connection((host, port), timeout=6).close()
            per_host[host] = "OK"
            tcp_ok += 1
        except Exception as exc:  # noqa: BLE001
            per_host[host] = type(exc).__name__
    res["TDX可连主机"] = f"{tcp_ok}/{len(TDX_HOSTS)}"
    res["_tdx_per_host"] = per_host
    res["_tdx_required"] = need_tdx
    # TDX 只在本批确实要走它时才阻塞，且只需 1 台可连
    tdx_pass = (tcp_ok >= 1) if need_tdx else True
    res["_pass"] = http_pass and tdx_pass
    return res


def build_plan() -> tuple[list[dict], dict]:
    """从枚举清单派生探针计划。⭐ 这是本脚本与手写探针的根本区别。"""
    recs = json.loads(RAW.read_text(encoding="utf-8"))
    plan: list[dict] = []
    stats = {"enumerated": len(recs), "excluded_category": 0,
             "not_importable": 0, "unsynthesizable": 0, "planned": 0}
    for r in recs:
        if r["category"] in EXCLUDE_CATEGORIES:
            stats["excluded_category"] += 1
            continue
        # a-stock-data 是 SKILL.md 形态、Agent-Reach 未安装 —— 结构上不可 import。
        # ⛔ 记为"未安装/不可导入"，绝不记为"接口不可用"。
        if r["lib"] in ("a-stock-data", "Agent-Reach"):
            stats["not_importable"] += 1
            continue
        sig = r.get("signature", "()")
        func_key = f"{r['lib']}::{r['name']}"
        variants = synthesize_variants(sig, r["lib"], func_key=func_key)
        if not variants:
            stats["unsynthesizable"] += 1
            continue
        plan.append({"lib": r["lib"], "name": r["name"],
                     "category": r["category"], "variants": variants,
                     "signature": sig[:120]})
        stats["planned"] += 1
    return plan, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", default=None, help="只打某个库")
    ap.add_argument("--category", default=None, help="只打某个类别")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--only-states", nargs="+", default=None,
                    help="只重打指定状态的记录（如 FAIL_GATEWAY FAIL_PROBE_BUG）")
    ap.add_argument("--only-missing", action="store_true",
                    help="只打**产物里还没有任何结果**的计划条目（CHECK 1 的 50 条缺口）。"
                         "⭐ 这个缺口靠重跑发现不了：它是 1373dae 重分类新增进分母的，"
                         "只能靠 set(plan)−set(results) 对账定位。")
    ap.add_argument("--only-missing-bytes", action="store_true",
                    help="只重打**CHECK 4 判据集合内尚无 `bytes_received`** 的记录"
                         "（FAIL_* + 有 error 文本 + 非 HUNG + 该字段为 None）。"
                         "⭐ 与判据同一口径，故'我限定了范围'可被判据本身验证。")
    ap.add_argument("--include-off-plan", action="store_true",
                    help="把**已有结果但不在当前计划内**的 key 也纳入本次候选"
                         "（`FINDING-217-NEW-2`）。⛔ 它们不进覆盖率分子、"
                         "不影响 plan_order_sha256（CHECK 13 锁纯 build_plan() 顺序）；"
                         "纳入的唯一目的是让 CHECK 4 的判据集合可达。")
    ap.add_argument("--exclude-paged", action="store_true",
                    help="跳过**函数体内部有分页循环**的接口（`FINDING-205-NEW-1`）。"
                         "⛔ 它们的分页不在签名里，PAGE_LIMIT_OVERRIDES 覆盖不到；"
                         "实测单条可达 2,858 页 / ETA 2h11m，重打只会拿到 HUNG，"
                         "并把已定位的 FAIL_GATEWAY 退化成不可解释（FINDING-220 的形状）。")
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="网络门禁未过仍继续（结果标记降级）")
    ap.add_argument("--no-retry", action="store_true",
                    help="禁用 HUNG 重测腿（⛔ 会让「慢但可用」被记成不可解释，见 FINDING-205）")
    args = ap.parse_args()

    plan, stats = build_plan()
    # ⭐ CHECK 13（`FINDING-220-NEW-2`）：在**任何过滤之前**留下完整计划顺序。
    #    这是判据侧 `c.plan_keys` 的同一口径；过滤后的 plan 不能用来算这个哈希，
    #    否则「我限定了范围」的自述就无法与计划版本对账。
    plan_keys_full = [f"{p['lib']}::{p['name']}" for p in plan]
    if args.lib:
        plan = [p for p in plan if p["lib"] == args.lib]
    if args.category:
        plan = [p for p in plan if p["category"] == args.category]

    # ⭐ `FINDING-217-NEW-2`：把**已有结果但不在计划内**的 key 合成成候选，
    #    使 CHECK 4 的判据集合（它遍历全部 results，不按 plan 过滤）变得可达。
    # ⛔ 必须在**算完 plan_keys_full 之后**追加 —— 否则会污染 CHECK 13 的哈希
    #    （那条锁的是纯 `build_plan()` 的顺序）。此处已在其后，顺序不可调换。
    if args.include_off_plan:
        _pset = set(plan_keys_full)
        _have_keys: set[str] = set()
        if RESULT.exists():
            try:
                _raw0 = RESULT.read_bytes()
                for _enc in ("utf-8", "utf-8-sig"):
                    try:
                        _prev0 = json.loads(_raw0.decode(_enc))
                        break
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                else:
                    _prev0 = {"results": []}
                _have_keys = {f"{r['lib']}::{r['name']}" for r in _prev0.get("results", [])}
            except Exception:  # noqa: BLE001
                _have_keys = set()
        _off = sorted(_have_keys - _pset)
        # 从离线枚举补签名 —— ⛔ 读不到就不合成（绝不猜参数）
        _raw_recs = {f"{x['lib']}::{x['name']}": x
                     for x in json.loads(RAW.read_text(encoding="utf-8-sig"))}
        _added = 0
        for _k in _off:
            _rec = _raw_recs.get(_k)
            if not _rec:
                continue
            _sig = _rec.get("signature", "()")
            _vars = synthesize_variants(_sig, _rec["lib"], func_key=_k)
            if not _vars:
                continue
            plan.append({"lib": _rec["lib"], "name": _rec["name"],
                         "category": _rec.get("category"), "variants": _vars,
                         "signature": _sig[:120]})
            _added += 1
        print("=== --include-off-plan 追加（FINDING-217-NEW-2）===")
        print(f"  计划外已有结果 {len(_off)} 条 → 可合成变体并追加 {_added} 条")
        print("  ⛔ 它们仍留在 off_plan_records 里，不进覆盖率分子，也不改 plan_order_sha256")

    # ⭐ CHECK 1 的定向腿：只打「计划内但产物里没有任何结果」的条目。
    # ⛔ 不与 --only-states 互斥使用（那个按 state 过滤，前提是**已有**记录；
    #    本参数的目标恰恰是**没有**记录的那批，两者集合天然不相交）。
    if args.only_missing:
        if args.only_states:
            print("⛔ --only-missing 与 --only-states 互斥："
                  "前者选「无记录」，后者选「有记录且状态匹配」，集合不相交。")
            return 2
        have: set[str] = set()
        if RESULT.exists():
            try:
                _raw = RESULT.read_bytes()
                for _enc in ("utf-8", "utf-8-sig"):
                    try:
                        _prev = json.loads(_raw.decode(_enc))
                        break
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                else:
                    print(f"⛔ 无法解析现有产物 {RESULT.name}")
                    return 2
                have = {f"{r['lib']}::{r['name']}" for r in _prev.get("results", [])}
            except Exception as _e:  # noqa: BLE001
                print(f"⛔ 读取现有产物失败 ({type(_e).__name__}: {_e})")
                return 2
        before = len(plan)
        plan = [p for p in plan if f"{p['lib']}::{p['name']}" not in have]
        print("=== --only-missing 过滤 ===")
        print(f"  产物已有 {len(have)} 条结果")
        print(f"  {before} → {len(plan)} 条（计划内尚无任何结果）")
        if not plan:
            print("  ⚠ 过滤后计划为空 —— 计划内每条都已有结果")
            return 0

    # ⭐ CHECK 4 的定向腿：只重打**判据集合内尚无 bytes_received** 的记录。
    # ⛔ 口径必须与 `verify_data_layer_complete.Ctx.eligible_for_bytes()` **逐字一致**，
    #    否则"我限定了范围"这句自述就无法被判据验证（红线 §九⑦ 的形状）。
    if args.only_missing_bytes:
        if not RESULT.exists():
            print(f"⛔ --only-missing-bytes 需要现有产物，但 {RESULT} 不存在")
            return 2
        try:
            _raw = RESULT.read_bytes()
            for _enc in ("utf-8", "utf-8-sig"):
                try:
                    _prev = json.loads(_raw.decode(_enc))
                    break
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
            else:
                print(f"⛔ 无法解析现有产物 {RESULT.name}")
                return 2
        except Exception as _e:  # noqa: BLE001
            print(f"⛔ 读取现有产物失败 ({type(_e).__name__}: {_e})")
            return 2
        _by = {f"{r['lib']}::{r['name']}": r for r in _prev.get("results", [])}

        def _needs_bytes(rec: dict) -> bool:
            """与判据同口径：FAIL_* + 有 error 文本 + 非 HUNG + 该字段为 None。"""
            if not str(rec.get("state", "")).startswith("FAIL"):
                return False
            if not (rec.get("error") or ""):
                return False
            if "HUNG" in str(rec.get("error") or ""):
                return False
            return rec.get("bytes_received") is None

        before = len(plan)
        plan = [p for p in plan
                if _needs_bytes(_by.get(f"{p['lib']}::{p['name']}", {}))]
        print("=== --only-missing-bytes 过滤（CHECK 4 口径）===")
        print(f"  {before} → {len(plan)} 条（FAIL_* 且有 error 且非 HUNG 且缺 bytes_received）")
        if not plan:
            print("  ⚠ 过滤后计划为空 —— 判据集合内每条都已有 bytes_received")
            return 0

    # ⛔ `FINDING-205-NEW-1`：排除函数体内部分页的接口。
    #    这些接口的分页**不在签名里**，故 PAGE_LIMIT_OVERRIDES 结构上覆盖不到；
    #    实测一条 `stock_gdfx_*` 要遍历 2,858 页（ETA 2h11m），
    #    外部超时只会把它记成 HUNG → 重测腿再花 150s → 最终记为不可解释。
    #    ⇒ 对"取字节数"这个目的，重打它们**必然失败且会造成状态退化**。
    if args.exclude_paged:
        import inspect as _inspect
        import re as _re
        _skipped: list[str] = []
        _kept = []
        for p in plan:
            _paged = False
            try:
                _mod = __import__(p["lib"])
                _fn = getattr(_mod, p["name"], None)
                if _fn is not None:
                    _src = _inspect.getsource(_fn)
                    _paged = ("tqdm" in _src
                              or bool(_re.search(r"total_page|for page in", _src)))
            except Exception:  # noqa: BLE001
                _paged = False      # 取不到源码就不排除 —— 保守方向（宁可打它）
            if _paged:
                _skipped.append(f"{p['lib']}::{p['name']}")
            else:
                _kept.append(p)
        print("=== --exclude-paged 过滤（FINDING-205-NEW-1）===")
        print(f"  {len(plan)} → {len(_kept)} 条；跳过 {len(_skipped)} 条内部分页接口")
        for s in _skipped[:8]:
            print(f"    skip {s}")
        if len(_skipped) > 8:
            print(f"    ... 还有 {len(_skipped)-8} 条")
        print("  ⛔ 被跳过的**不是**「接口不可用」——它们是「本批不测，需先按参数收窄」")
        plan = _kept
        if not plan:
            print("  ⚠ 过滤后计划为空")
            return 0

    # ⭐ FINDING-220 修法③（硬顺序步骤 3）：--only-states 定向重测
    # 必须先加载 existing，才能按 state 过滤
    if args.only_states:
        if not RESULT.exists():
            print(f"⛔ --only-states 需要现有产物，但 {RESULT} 不存在")
            return 2
        try:
            raw = RESULT.read_bytes()
            for _enc in ("utf-8", "utf-8-sig"):
                try:
                    prev = json.loads(raw.decode(_enc))
                    break
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
            else:
                print(f"⛔ 无法解析现有产物 {RESULT.name}")
                return 2
            existing_by_key = {f"{r['lib']}::{r['name']}": r
                               for r in prev.get("results", [])}
        except Exception as _e:  # noqa: BLE001
            print(f"⛔ 读取现有产物失败 ({type(_e).__name__}: {_e})")
            return 2

        target_states = set(args.only_states)
        before = len(plan)
        plan = [p for p in plan
                if existing_by_key.get(f"{p['lib']}::{p['name']}", {}).get("state") in target_states]
        print(f"=== --only-states 过滤 ===")
        print(f"  目标状态: {', '.join(sorted(target_states))}")
        print(f"  {before} → {len(plan)} 条")
        if not plan:
            print("  ⚠ 过滤后计划为空")
            return 0

    total_planned = len(plan)
    plan = plan[args.offset:]
    if args.limit is not None:
        plan = plan[: args.limit]

    print("=== 计划派生（FINDING-194：从枚举清单派生，非手写）===")
    for k, v in stats.items():
        print(f"  {k:22} {v}")
    print(f"  {'本次选中':22} {len(plan)} / {total_planned}")
    if args.dry_run:
        for p in plan[:30]:
            vs = " | ".join(f"{n}={k}" for n, k in p["variants"])
            print(f"    {p['lib']:12} {p['name'][:44]:46} {vs[:90]}")
        if len(plan) > 30:
            print(f"    ... 还有 {len(plan)-30} 个")
        return 0

    # FINDING-198: TDX 腿只在本批确实含走 TDX 的库时才作为阻塞条件
    need_tdx = any(p["lib"] in TDX_LIBS for p in plan)
    gate = network_gate(need_tdx=need_tdx)
    print("\n=== 网络门禁 ===")
    for k, v in gate.items():
        if not k.startswith("_"):
            print(f"  {k:28} {v}")
    if not gate["_pass"] and not args.force:
        print("\n⛔ 网络门禁未过 —— 此状态下失败结果不可解释（FINDING-175）。已挂起。")
        return 2

    # ⛔ 自检：另一个扫描已在跑时必须拒绝启动。
    #    否则两个扫描会互相覆盖 lockfile 与产物 —— 那正是 FINDING-204 的形状，
    #    只是加害者从"重判脚本"变成"另一个我自己"。
    #    守卫模块缺失时降级**跳过**（该锁文件由本脚本自己维护，存在性断言仍有效）。
    if probe_in_progress is not None:
        _running, _reason = probe_in_progress()
        if _running and not args.force:
            print(f"\n⛔ 已有打点扫描在运行：{_reason}")
            print("   两个扫描会互相覆盖产物与 lockfile。已拒绝启动。")
            print("   等它结束，或确认要并发时加 --force（⛔ 结果的失败态将不可解释）。")
            return 3
    else:
        print("⚠ finai.probe_guard 不可用（本机缺失），并发守卫已跳过 —— "
              "lockfile 自锁仍生效（FINDING-204）")

    # FINDING-204: 写 lockfile，让外部脚本能检测"打点进行中"而不是猜时序
    import atexit
    import os as _os
    OUT.mkdir(parents=True, exist_ok=True)
    # `FINDING-282` 兜底：记 `boot_id`，让守卫有一条**与 PID 判活无关**的释放路径。
    # ⛔ 为什么需要：判活曾是唯一释放路径，它一坏（实测 6/6 误判活），
    #    陈旧锁就永不释放，取数被永久拦住且理由是假的。
    try:
        import psutil as _psutil
        _boot_id = str(int(_psutil.boot_time()))
    except Exception:  # noqa: BLE001
        _boot_id = None
    LOCK.write_text(json.dumps({
        "pid": _os.getpid(),
        "boot_id": _boot_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "planned": len(plan),
        "note": "打点进行中；本文件存在且 PID 存活时，⛔ 不得修改 auto_probe_results.json",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    atexit.register(lambda: LOCK.unlink(missing_ok=True))

    # 增量落盘：近千个探针跑到一半崩掉不能全丢
    existing: dict = {}
    if RESULT.exists():
        try:
            # ⛔ FINDING-219 同族：静默 pass 会让 existing 默认为 {}，
            #    下一次 _flush 就会把 1,000+ 条历史结果全部丢失。
            #    ⚠ 必须同时支持 utf-8-sig（有 BOM）—— git redirect `>` 在
            #    PowerShell 下会写出 BOM，若只用 utf-8 解码会抛 JSONDecodeError，
            #    而静默 pass 让 existing={} 空字典，下一次写盘就覆盖了全部历史。
            raw = RESULT.read_bytes()
            for _enc in ("utf-8", "utf-8-sig"):
                try:
                    prev = json.loads(raw.decode(_enc))
                    break
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
            else:
                # 两种编码都失败 —— 产物损坏，打印警告但不丢历史（宁可空跑也不覆盖）
                print(f"⚠ 无法解析现有产物 {RESULT.name}，本次将从零开始（历史结果不可用）")
                prev = {"results": []}
            existing = {f"{r['lib']}::{r['name']}": r for r in prev.get("results", [])}
            print(f"  已加载 {len(existing)} 条历史结果（增量模式）")
        except Exception as _e:  # noqa: BLE001
            # 只有真正意外的错误才走这里（如文件被锁）
            # ⛔ 不静默：打印警告，明确告知历史结果不可用
            print(f"⚠ 读取现有产物失败 ({type(_e).__name__}: {_e})，本次从零开始")
            existing = {}

    throttle = Throttle()
    # ⭐ `FINDING-408`：按**风控域**统计（主机归属表实测），⛔ 不按名字后缀的族。
    dom_count = collections.Counter(rate_limit_domain(p["lib"], p["name"])
                                   for p in plan)
    fam_count = collections.Counter(endpoint_family(p["lib"], p["name"])
                                    for p in plan)
    print(f"\n=== 限流计划（FINDING-200 / FINDING-408）===")
    for dom, n in dom_count.most_common():
        moved = n - fam_count.get(dom, 0)
        note = (f"  ← 比名字后缀判族多 {moved:+d} 个" if moved else "")
        print(f"  {dom:12} {n:>5} 个  间隔 {DOMAIN_MIN_INTERVAL.get(dom, 0.5)}s"
              f"  窗口上限 {DOMAIN_WINDOW_LIMIT.get(dom, '-')}{note}")
    eta = sum(n * (DOMAIN_MIN_INTERVAL.get(f, 0.5) + 1.0) for f, n in dom_count.items())
    print(f"  预计耗时下限 ≈ {eta/60:.0f} 分钟（不含退避与超时）")

    print(f"\n=== 打点 {len(plan)} 个（外部超时 {args.timeout}s/个）===")
    done = 0
    this_run_keys = set()  # ⭐ CHECK 3: 本次实际尝试的接口集合
    for p in plan:
        key = f"{p['lib']}::{p['name']}"
        this_run_keys.add(key)
        family = endpoint_family(p["lib"], p["name"])       # 仅用于归档/展示
        domain = rate_limit_domain(p["lib"], p["name"])     # ⭐ 限流键（主机级）
        # ⭐ 逐变体尝试, 取最好的那个。落选变体一并留档, 使这个选择可被审计
        # (⛔ 不留档就等于"我选了一个但没人能复核" —— FINDING-125 的形状)。
        attempts = []
        for vname, vkw in p["variants"]:
            # FINDING-200: 每次真实请求前限流
            # FINDING-408: 键必须是**风控域**，传 family 会漏掉 209 条东财请求
            throttle.wait(domain)
            r = probe_one(p["lib"], p["name"], vkw, args.timeout)
            r["timeout_used_s"] = args.timeout
            attempts.append({"variant": vname, "kwargs": vkw, **r})
            if r["state"] == "OK":
                break  # 已拿到可信结果, 不必再试更差的变体
        best = min(attempts, key=_variant_rank)

        # ⭐ FINDING-205 重测腿：HUNG 不等于不可用。
        # 实测 `macro_china_cpi_yearly` 59.4s/477行、`fund_etf_spot_em` 53.9s/1567行
        # —— 两条都被 35s 超时判成了 `FAIL_UNREACHABLE`，而它们是成功的。
        # ⛔ 故"两次都 HUNG"才允许记不可解释；单次 HUNG 只是"这个超时不够"。
        if "HUNG_NO_RESULT_IN_" in (best.get("error") or "") and not args.no_retry:
            vname, vkw = p["variants"][0]
            throttle.wait(domain)
            r2 = probe_one(p["lib"], p["name"], vkw, HUNG_RETRY_TIMEOUT)
            r2["timeout_used_s"] = HUNG_RETRY_TIMEOUT
            r2["retried_after_hung"] = True
            attempts.append({"variant": f"{vname}_retry{HUNG_RETRY_TIMEOUT}s",
                             "kwargs": vkw, **r2})
            best = min(attempts, key=_variant_rank)
        rec = {"lib": p["lib"], "name": p["name"], "category": p["category"],
               "signature": p["signature"], "endpoint_family": family,
               # ⭐ `FINDING-408`：逐条落档**实际用过的限流键**，
               #    使"这条是在哪个预算下取的"可复核（与 family 不同即为归属表纠正）
               "rate_limit_domain": domain, **best,
               "attempts": attempts if len(attempts) > 1 else None,
               "network_degraded": not gate["_pass"],
               # ⭐ CHECK 6（`FINDING-220-NEW-1`）：时间戳只在**真的探测过**时写，
               #    且写的是这一条探测完成的时刻，不是 flush 时刻。
               #    ⛔ 不得在 _flush 里给历史记录回填 —— 那是发明观测。
               "observed_at": datetime.now(timezone.utc).isoformat(),
               # 逐条代理状态：使「这条失败是否落在代理开启窗口」可判（`FINDING-211`）。
               "proxy_at_probe": _proxy_state_now()}
        # ⛔ FINDING-214: 不得用"不可解释的失败"覆盖"已证实的成功"。
        #    实测代价：3 个接口的 90/100/294 行真实数据被覆盖成了 FAIL_*，
        #    覆盖后无人能知道它们曾经成功。
        rec, kept_reason = _merge_preserving_credible_evidence(existing.get(key), rec)
        existing[key] = rec
        done += 1
        rows = rec.get("rows")
        mark = {"OK": "OK  ", "EMPTY_OK": "EMPTY", "FAIL_DETERMINISTIC": "DET ",
                "FAIL_GATEWAY": "GW  ", "FAIL_UNREACHABLE": "UNRE",
                "FAIL_PROBE_BUG": "BUG "}.get(rec["state"], "????")
        ev = (f"rows={rows} cols={(rec.get('cols') or [])[:4]}"
              if rec["state"] == "OK" else (rec.get("error") or "")[:56])
        via = f" [{rec.get('variant')}]" if len(attempts) > 1 else ""
        print(f"  [{done:>4}/{len(plan)}] {mark} {p['lib']:11} {p['name'][:40]:42} {ev}{via}")
        if done % 25 == 0:
            _flush(existing, gate, stats, throttle, this_run_keys, plan_keys_full)
    _flush(existing, gate, stats, throttle, this_run_keys, plan_keys_full)

    from collections import Counter
    cnt = Counter(r["state"] for r in existing.values())
    print("\n=== 累计汇总（含历史批次）===")
    for st, n in cnt.most_common():
        print(f"  {st:22} {n}")
    tot = sum(cnt.values())
    concl = cnt.get("OK", 0) + cnt.get("FAIL_DETERMINISTIC", 0)
    print(f"\n  已打点 {tot} / 计划 {stats['planned']}  ({tot/stats['planned']*100:.1f}%)")
    print(f"  可作结论的（OK + FAIL_DETERMINISTIC）: {concl}")
    print(f"\n产物: {RESULT}")
    return 0


#: `_flush` 整份重写时**必须搬运**的既有顶层键。
#: ⛔ `FINDING-215` 实测：`reclassify_probe_results.py` 写入的
#: `reclassified_at`/`reclassify_note` 在 `a1306f2` 存在（并标注了 64 条重判），
#: 补打 35 条后**两个键都没了**，而产物里仍有 40 条 `state_before_reclassify`
#: —— 即 40 条状态来自离线重判，产物却不再声明这件事发生过。
#: ⭐ 根因是 `_flush` **从零构造 payload**：它只写自己知道的键，
#: 于是任何"别的脚本加的键"都会在下一次 flush 静默消失。
_PRESERVED_TOP_LEVEL_KEYS = ("reclassified_at", "reclassify_note")


def _carry_forward_provenance(payload: dict) -> dict:
    """把既有产物里的 provenance 顶层键搬进新 payload。

    ⛔ 只搬运、不发明：读不到就不写，绝不猜一个时间戳。
    ⚠ 且**必须核对一致性** —— 若产物里已无任何 `state_before_reclassify`
    却仍有 `reclassified_at`，那说明重判记录已被真实覆盖，此时搬运
    反而会留下一个撒谎的键，故只在确实还有 marker 时搬运。
    """
    if not RESULT.exists():
        return payload
    try:
        prev = json.loads(RESULT.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return payload   # 读不动就不搬 —— 不因搬运失败而丢掉本次结果
    still_marked = sum(
        1 for r in payload.get("results", [])
        if isinstance(r, dict) and r.get("state_before_reclassify")
    )
    if not still_marked:
        return payload
    for key in _PRESERVED_TOP_LEVEL_KEYS:
        if key in prev and key not in payload:
            payload[key] = prev[key]
    payload["reclassified_records_still_present"] = still_marked
    return payload


def _flush(existing: dict, gate: dict, stats: dict,
           throttle: 'Throttle | None' = None, this_run_keys: set | None = None,
           plan_keys_full: list | None = None) -> None:
    """增量落盘。⚠ 每次全量重写，保证产物自洽（不做 append 拼接）。

    ⛔ `FINDING-215`：整份重写**必须**经 `_carry_forward_provenance`，
    否则别的脚本写入的溯源键会被静默丢弃。

    Args:
        this_run_keys:  本次实际尝试的接口集合（CHECK 3 需要）。
        plan_keys_full: **未经 --lib/--category/--only-states/--offset/--limit 过滤的**
                        完整计划键，按计划顺序（CHECK 13 需要）。

    ⛔ `FINDING-220-NEW-1`：**这里绝不回填时间戳。**
       旧实现给每条没有 `probed_at_record` 的记录填 flush 墙钟，实测后果是
       `attempted_this_run=2` 的产物上 1030 条全带同一个时间戳 ——
       等于断言「1030 个接口在同一瞬间被观测」。CHECK 6 因此判绿而能力为零：
       该字段的用途是把每条映射到代理开启窗口（`FINDING-211` 的 13:21–16:57），
       一个恒定的 flush 时刻在这件事上信息量为 0。
       ⇒ 逐条时间戳只在 `main()` 里由**真实探测**写入（`observed_at`），
         历史记录读不到就**留空**（诚实的缺失优于发明的值）。
    """
    import hashlib
    import urllib.request
    OUT.mkdir(parents=True, exist_ok=True)

    now_iso = datetime.now(timezone.utc).isoformat()

    # 顶层代理快照。⚠ 措辞必须限定它描述的是**写盘时刻**，
    # 逐条的代理状态在记录自己的 `proxy_at_probe` 里（那才能对齐探测时刻）。
    proxy_snapshot = {
        "http_proxy": urllib.request.getproxies().get("http"),
        "https_proxy": urllib.request.getproxies().get("https"),
        "captured_at": now_iso,
        "scope": "flush 时刻的系统代理状态；⛔ 不代表历史各批次探测时的状态",
        "per_record_key": "proxy_at_probe（只有本次真实探测过的记录才有）",
        "note": "系统代理状态快照（urllib.request.getproxies）",
    }

    # ⭐ CHECK 13：计划顺序哈希必须与判据同一口径 ——
    # `verify_data_layer_complete.py:475` 哈希的是 `build_plan()` 的**计划顺序键**。
    # ⛔ `FINDING-220-NEW-2`：旧实现哈希「产物内结果按 (lib,name) 排序」，
    #    既换了集合（1030 vs 1065）又换了顺序（排序抹掉了计划顺序），
    #    于是这个哈希对 `--offset` 漂移**完全不敏感** —— 它不携带它声称要锁定的信息。
    if plan_keys_full is None:                      # 兜底：调用方没给就自己派生
        _p, _s = build_plan()
        plan_keys_full = [f"{x['lib']}::{x['name']}" for x in _p]
    plan_order_sha = hashlib.sha256("\n".join(plan_keys_full).encode()).hexdigest()
    raw_sha = hashlib.sha256(RAW.read_bytes()).hexdigest() if RAW.exists() else None

    # 计划外记录：有结果但已不在当前计划内（`1373dae` 换过一版计划）。
    # ⛔ 不删它们（那是丢实测证据），而是**显式标注** —— 使矩阵的分子分母
    #    不再混用两代 plan（CHECK 2 的「已显式标注」分支）。
    _plan_set = set(plan_keys_full)
    off_plan = sorted(k for k in existing if k not in _plan_set)

    payload = {
        "probed_at": now_iso,
        "plan_stats": stats,
        # ⭐ CHECK 3：attempted_this_run 字段（若 this_run_keys 提供）
        "attempted_this_run": len(this_run_keys) if this_run_keys else None,
        "network_gate": gate,
        "proxy_snapshot": proxy_snapshot,
        # ⭐ CHECK 13：哈希绑定
        "plan_order_sha256": plan_order_sha,
        "interfaces_raw_sha256": raw_sha,
        "plan_keys_count": len(plan_keys_full),
        # ⭐ CHECK 2：计划外记录显式标注（保留证据 + 不混入覆盖率分子）
        "off_plan_records": off_plan,
        "off_plan_note": (
            "这些 key 有实测结果但已不在当前 build_plan() 内（计划自 1373dae 变更过）。"
            "⛔ 保留其证据，但不得计入「计划覆盖率」的分子。"),
        # FINDING-200: 限流参数与实际退避必须落档 ——
        # 否则"这批数据是在什么速率下取的"不可复核，且无法区分
        # "接口真的没数据" 与 "被限流/封禁导致的空"。
        "rate_limit": {
            # ⚠ 键名保留 `family_*` 只为不打断既有读者；其**语义已是风控域**。
            "family_min_interval_s": DOMAIN_MIN_INTERVAL,
            "family_window_limit": DOMAIN_WINDOW_LIMIT,
            "domain_min_interval_s": DOMAIN_MIN_INTERVAL,
            "domain_window_limit": DOMAIN_WINDOW_LIMIT,
            "window_seconds": WINDOW_SECONDS,
            "thresholds_source": "a-stock-data/SKILL.md 东财风控阈值(社区实测 2026-05): >5/s 高 · 1min>=200 中高 · 5min>=300 触发封禁 · 并发>=10 高",
            # ⭐ `FINDING-408`：预算键与上限可达性都必须在产物里可复核。
            "keyed_on": "host_rate_limit_domain",
            "attribution_source": (
                "scripts/probe_host_attribution.py（生成器 "
                "scripts/gen_probe_host_attribution.py，来源为 inspect.getsource "
                "读已安装包源码里的 URL 主机；⛔ 不是函数名后缀启发式）"),
            "attribution_entries": len(HOST_ATTRIBUTION),
            "self_imposed_margin": (
                f"eastmoney 窗口上限 {DOMAIN_WINDOW_LIMIT.get('eastmoney')} < "
                f"封禁阈值 {BAN_THRESHOLD_PER_HOST}，且 "
                f"{DOMAIN_MIN_INTERVAL.get('eastmoney')}s×"
                f"({DOMAIN_WINDOW_LIMIT.get('eastmoney')}-1) < {WINDOW_SECONDS}s "
                f"⇒ 该上限**可达**（旧值 240 结构上不可达，退避分支是死代码）"),
            "reachability_self_check": "assert_window_budgets_reachable() @ import",
            "backoffs_triggered": (throttle.backoffs if throttle else []),
        },
        "contract": {
            "OK": "行数>0，永远可信（行数与字段无法伪造）",
            "EMPTY_OK": "调用成功但 0 行 —— ⛔ 不可作为『无数据』结论（FINDING-178）",
            "FAIL_DETERMINISTIC": "端点给出明确语义报错，可信",
            "FAIL_GATEWAY": "502/503/504 或空体解析失败 —— 不可解释",
            "FAIL_UNREACHABLE": "连接层失败/超时/挂死 —— ⛔ 不可解释，不得记为接口不可用",
            "FAIL_PROBE_BUG": "调用方自己写错（AttributeError/TypeError 等），与数据源无关",
        },
        "results": sorted(existing.values(), key=lambda r: (r["lib"], r["name"])),
    }
    payload = _carry_forward_provenance(payload)
    RESULT.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                      encoding="utf-8")


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
