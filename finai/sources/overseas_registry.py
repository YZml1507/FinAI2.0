#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""非 A 股市场接口**登记表**（港股 / 美股 / 期货 / 期权 / 加密外汇）—— 保留待扩展。

⛔⛔ **本模块不取数，只登记。** 用户 2026-08-08 明确：
  「目前我们项目搭建主要针对于 A 股，其他股市会等后续进行拓展，
    所以现在接口先留着并说明。」
  ⇒ 这里存的是**存在性清单**，不是可用性结论。启用前必须先实测。

⭐ **为什么这些接口不在 729 / 1,066 里**（这是最容易误解的一点）：
  它们被 `scripts/auto_probe_interfaces.py::EXCLUDE_CATEGORIES` **有意排除**，
  原话是「B5（港美股/期货/期权/加密）按主方案**仅登记存在性**」。
  ⇒ 不是漏了、不是失败、也不是不可用 —— 是**按设计没测**。
  ⛔ 故对它们既不能说"可用"，也不能说"不可用"。**没有测过就是没有结论。**

分母关系（写清楚免得下次又要对账）：
    枚举总数 1,612
      − 被排除类别 441（本模块登记的就是其中 294 条市场类 + 147 条非市场类）
      − 不可 import / 入参无法合成 …
      = 在册计划 **1,066**（其中实测可用 729）

⚠ 已知的**唯一**例外：TDX 扩展行情（`finai/sources/tdx_ext_source.py`）**已实测**，
  它不经 akshare，直连 TDX 协议 —— 港股 5min 实测回溯至 **2015-11-30**（`FINDING-251`）。
  故"港股行情"这一项**已经有可用通路**，与本表登记的 akshare 港股接口是两条独立路线。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_RAW = _ROOT / "artifacts/interface_matrix/interfaces_raw.json"

#: 被 `EXCLUDE_CATEGORIES` 排除的**市场类**类别（非市场类的"管道/非数据接口"等不在此）。
MARKET_CATEGORIES = ("港股", "美股", "期货", "期权", "加密/外汇")

#: 各市场的现状说明。⛔ `probed` 一律 False —— 除 TDX 扩展行情那条独立路线。
MARKET_STATUS: dict[str, dict] = {
    "港股": {
        "count": 53,
        "probed": False,
        "alt_path": "finai.sources.tdx_ext_source",
        "alt_measured": "港股 5min/15min/日线，实测回溯至 2015-11-30（FINDING-251）；"
                        "香港主板 2,703 只 + 港股通 961 只（FINDING-250）",
        "note": "⭐ 港股是**已有可用通路**的市场（走 TDX 扩展行情，非 akshare）。"
                "本表这 53 条 akshare 接口未实测，属备用路线。",
    },
    "美股": {
        "count": 67,
        "probed": False,
        "alt_path": "finai.sources.tdx_ext_source（未证实）",
        "alt_measured": "⚠ TDX `get_markets` 报了 market=74「美国股票」与 40「中国概念股」，"
                        "但它们**未出现**在已翻的 60,000 条目录样本里（总量 82,879）"
                        "⇒ ⛔ 未证实也未否证。",
        "note": "本表 67 条含 akshare 的美股行情/宏观接口，未实测。"
                "⚠ 其中相当一部分是**美国宏观**（macro_usa_*）而非美股个股行情。",
    },
    "韩股": {
        "count": 0,
        "probed": False,
        "alt_path": None,
        "alt_measured": "⛔ **实测枚举清单里没有任何韩股行情接口**。"
                        "按 'kr/korea/kospi' 搜到的 7 条全是 tdxpy 的 BlockReader"
                        "（板块文件读取，命中的是 'blockreader' 里的字母，与韩股无关）。",
        "note": "⇒ 韩股扩展**需要引入新数据源**，现有 7 个库都不覆盖。"
                "这是路线图上唯一没有现成通路的市场。",
    },
    "期货": {
        "count": 104,
        "probed": False,
        "alt_path": "finai.sources.tdx_ext_source",
        "alt_measured": "上海期货 352 / 郑州商品 292 / 大连商品 278 / 中金所 65 只合约，"
                        "5min 可取（⚠ 深度受合约生命周期限制，IC2608 仅 1,632 根）",
        "note": "TDX 扩展行情已实测可取；本表 104 条 akshare 接口未实测。",
    },
    "期权": {
        "count": 33,
        "probed": False,
        "alt_path": "finai.sources.tdx_ext_source",
        "alt_measured": "个股期权 752 / 中金所期权 726 / 深圳期权 494 只合约",
        "note": "⭐ 另有 18 个**期权隐含波动率**接口（QVIX）**在 729 之内、已实测可用**，"
                "各 2,785 行日频 —— 那是现成的市场恐慌指标，不必等扩展阶段。",
    },
    "加密/外汇": {
        "count": 37,
        "probed": False,
        "alt_path": None,
        "alt_measured": None,
        "note": "⛔ 与本项目路线图无关（A股→美/韩），登记备查即可。",
    },
}


@lru_cache(maxsize=1)
def _raw() -> list[dict]:
    return json.loads(_RAW.read_text(encoding="utf-8"))


def list_interfaces(category: str) -> list[dict]:
    """列出某市场类别下的接口清单。⛔ 只有名字与签名，**没有**可用性结论。"""
    if category not in MARKET_CATEGORIES:
        raise KeyError(f"{category!r} 不是被排除的市场类别；"
                       f"可选：{MARKET_CATEGORIES}。"
                       f"（A 股接口请用 finai.sources.catalog_source）")
    return [{"lib": r["lib"], "name": r["name"], "signature": r.get("signature"),
             "probed": False, "note": "⛔ 按设计未实测（EXCLUDE_CATEGORIES）"}
            for r in _raw() if r.get("category") == category]


def summary() -> dict:
    """各市场：登记条数 / 是否已有可用通路。"""
    return {k: {"registered": v["count"], "probed": v["probed"],
                "usable_path": v["alt_path"]} for k, v in MARKET_STATUS.items()}
