#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Alpha 三层信号（修池子 / 排雷 overlay / PEAD 进攻增强）—— 2026-09-17 立项。

三层职责（调研裁决 R5 §3.1 集成方案）：

| 层 | 定位 | 频率 | 容错率 |
|---|---|---|---|
| **质量否决（修池子）** | 准入端硬约束——伪高股息/分红不可持续/低质票**不得入候选** | 日频 flag | 宁缺勿错 |
| **排雷 overlay** | 持仓内一票否决，事件驱动 T+1 清/减仓 | 事件驱动 | 零延迟、宁错杀 |
| **PEAD 进攻增强** | 进攻档专属候选源，20-40 交易日漂移窗口 | 事件驱动 | 可延迟、可漏 |

数据纪律（铁律逐条对应）：
  * **零前视**：财务/预告一律 ``pub_date``（=ann_date）对齐——公告日当天及以后
    才可见；除权事件用真实 ex-date；股息率/市值用 bars 里已有的 PIT 列。
    ⛔ 绝不用 stat_date 当可见日、绝不拿"最新快照"回填历史。
  * **statements 表** ``pub_date`` = income/balancesheet 两表 ann_date 的较迟者
    （保守取晚）；同 end_date 多次披露保留各自 pub_date，同比基期取
    "当前 pub 可见的最新版本"（restate 防前视）。
  * **价格序列 RAW 不复权**：跳空/涨跌停判定用 ``preclose``（交易所除权基准口径，
    与 cleaner.mark_limit_flags 同源）。
  * **sidecar 幂等**：产出 parquet 经 ``_atomic_write_parquet`` 原子落盘，
    同输入重跑字节一致（layer 表按 symbol/date 排序去重）。
  * **行业中性化**：行业标签为 Tushare 当前快照（静态标签，行业极少变更；
    已登记为已知近似——若未来拿到 PIT 行业分类再升级）。

模块分两段：**builder 纯函数**（DataFrame 进、DataFrame 出，离线可测）+
``SignalLayers`` 查询对象（bisect 查询，零状态——状态游标在策略侧）。
"""
from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field
from datetime import date as _date, timedelta as _td
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import pandas as pd

from data.cleaner import board_limit_pct  # 板块涨跌幅阈值唯一登记点（⛔ 不另写）

logger = logging.getLogger(__name__)

__all__ = [
    "LandmineEvent", "PeadEvent", "SignalLayers",
    "build_quality_veto_frame", "build_landmine_frame", "build_pead_frame",
    "load_signal_layers",
]


# ==============================================================================
# ① 修池子：准入端质量否决（日频 flag）
# ==============================================================================

#: 否决原因代码（审计/统计用；reasons 列是它们的 ";" 拼接）
VETO_NO_DIVIDEND_STREAK = "Q1"      # 连续分红年数不足
VETO_ROE_TTM = "Q2"                 # ROE-TTM 不达标或不可得
VETO_PAYOUT_OCF = "Q3"              # 分红/经营现金流超限（含 ocfps≤0 仍分红）
VETO_PSEUDO_YIELD = "Q4"            # 伪高股息：股息率升幅主要由下跌贡献


def _pit_sorted(pit: pd.DataFrame) -> pd.DataFrame:
    """PIT 表按 pub_date 升序、pub 缺失剔除（零前视：只认 pub_date）。"""
    if pit is None or pit.empty:
        return pd.DataFrame()
    df = pit.copy()
    df["pub_date"] = pd.to_datetime(df["pub_date"], errors="coerce").dt.date
    df["stat_date"] = pd.to_datetime(df["stat_date"], errors="coerce").dt.date
    df = df[df["pub_date"].notna() & df["stat_date"].notna()]
    return df.sort_values(["pub_date", "stat_date"]).reset_index(drop=True)


def _roe_ttm_series(pit: pd.DataFrame) -> list[float]:
    """每行 PIT 记录的 ROE-TTM（float）。

    pit ``roe`` 是**累计 YTD**（报告期年初至 stat_date）。TTM 口径：
      * 年报行（stat MM-DD == 12-31）：TTM = 本年 roe；
      * 非年报行：TTM = roe_ytd + 上年年报 roe − 上年同期 roe_ytd；
        两个分量任一缺失 ⇒ NaN（不可得，否决端按不达标处理——宁缺勿错）。

    基期取 stat_date 匹配的历史行（与 pub 无关——这是对**报表内容**的口径换算，
    不是信息可见性；可见性仍由外层 pub_date ≤ d 的 asof 控制）。
    """
    by_stat: dict[_date, float] = {}
    roes = pd.to_numeric(pit["roe"], errors="coerce")
    stats = pit["stat_date"].tolist()
    for s, r in zip(stats, roes.tolist()):
        if s is not None and pd.notna(r):
            by_stat[s] = float(r)          # 同 stat 重复行取后出现者（已排序）
    out: list[float] = []
    for s, r in zip(stats, roes.tolist()):
        if s is None or pd.isna(r):
            out.append(float("nan"))
            continue
        if (s.month, s.day) == (12, 31):
            out.append(float(r))
            continue
        prev_annual = _date(s.year - 1, 12, 31)
        try:
            prev_same = _date(s.year - 1, s.month, s.day)
        except ValueError:
            prev_same = _date(s.year - 1, s.month, 28)
        ra, rp = by_stat.get(prev_annual), by_stat.get(prev_same)
        if ra is None or rp is None:
            out.append(float("nan"))
        else:
            out.append(float(r) + ra - rp)
    return out


def build_quality_veto_frame(
    bars: pd.DataFrame,
    exdiv: pd.DataFrame | None,
    pit: pd.DataFrame | None,
    *,
    roe_ttm_min: float = 10.0,
    payout_ocf_max: float = 0.80,
    div_min_years: int = 3,
    div_lookback_days: int = 1500,
    ocf_window_days: int = 395,
    pseudo_lookback_bars: int = 250,
    pseudo_price_share: float = 0.50,
    fin_sector: bool = False,
    payout_years: int = 3,
) -> pd.DataFrame:
    """单票日频质量否决帧 → ``[date, veto, reasons]``。

    四条准入否决（任一触发即 veto=True；reasons 记录全部命中项）：

    * **Q1 连续分红**：回看 ``div_lookback_days``（默认 1500≈4.1 年）内
      现金分红（cash_dividend>0）的除权日覆盖的**日历年数 ≥ div_min_years**。
      （"连续 3 年分红"的近似——A 股年度分红多在次年 5-9 月除权，按
      除权日年计等价于"近 4 个分红季至少 3 年有分红"。）
    * **Q2 ROE-TTM**：最新可见 PIT 行（pub≤d）的 ROE-TTM < ``roe_ttm_min``
      或不可得 ⇒ 否决。
    * **Q3 分红≤经营现金流 80%**：近 ``payout_years`` 个年报 ocfps 合计
      vs 对应窗口每股现金分红合计（每股对每股、同期间对齐）——
      比例 > ``payout_ocf_max`` ⇒ 否决；ocfps 合计缺失/≤0 且仍有分红
      ⇒ 否决（现金流不支撑分红）。**金融类报表（``fin_sector=True``，
      comp_type≠1）跳过本规则**——银行经营现金流含存贷款流量，
      ocfps 可正可负无分红含义（实测浦发 2024 年报 -11.37）。
      3 年窗口平滑单年 OCF 周期噪声（公用事业/周期股单年为负常见）。
      ``ocf_window_days`` 仍用于 dps 年化窗口边界的年份推算。
    * **Q4 伪高股息**：``pseudo_lookback_bars``（默认 250 交易日）前股息率
      dy1/收盘 p1 与当前 dy0/p0——若 dy0>dy1 且 p0<p1，且
      ``dy1×(p1/p0−1)/(dy0−dy1)``（股息不变假设下由价格下跌解释的升幅占比）
      > ``pseudo_price_share`` ⇒ 否决。dy1 缺失/为 0 ⇒ 无法归因 ⇒ 不否决
      （股息率下限筛选本身已拦住 zero-yield）。

    输入帧可空：pit 空 ⇒ Q2 恒否决（无质量证据不放行，fail-closed）；
    exdiv 空 ⇒ Q1 恒否决。bars 必须含 ``date, close, dividend_yield``。
    """
    if bars is None or bars.empty:
        return pd.DataFrame(columns=["date", "veto", "reasons"])
    df = bars.copy()
    df["_d"] = pd.to_datetime(df["date"]).dt.date
    df = df.sort_values("_d").reset_index(drop=True)
    days = df["_d"].tolist()
    closes = pd.to_numeric(df["close"], errors="coerce").tolist()
    dys = pd.to_numeric(df.get("dividend_yield"), errors="coerce").tolist()

    # --- exdiv 事件（除权日 + 每股现金） ---
    ev: list[tuple[_date, float]] = []
    if exdiv is not None and not exdiv.empty:
        e = exdiv.copy()
        e["_d"] = pd.to_datetime(e["date"], errors="coerce").dt.date
        e["_c"] = pd.to_numeric(e.get("cash_dividend"), errors="coerce")
        for d0, c0 in zip(e["_d"].tolist(), e["_c"].tolist()):
            if d0 is not None and pd.notna(c0) and c0 > 0:
                ev.append((d0, float(c0)))
        ev.sort()
    ev_dates = [d0 for d0, _ in ev]
    ev_years = [d0.year for d0, _ in ev]
    ev_cash = [c0 for _, c0 in ev]

    # --- PIT 行 → (pub, roe_ttm) 与 (pub, annual ocfps) ---
    pit_s = _pit_sorted(pit) if pit is not None else pd.DataFrame()
    pit_pubs: list[_date] = []
    pit_roe_ttm: list[float] = []
    ann_pubs: list[_date] = []               # 年报行的 pub
    ann_ocfps: list[float] = []
    if not pit_s.empty:
        pit_s["_roe_ttm"] = _roe_ttm_series(pit_s)
        pit_pubs = pit_s["pub_date"].tolist()
        pit_roe_ttm = pit_s["_roe_ttm"].tolist()
        ann = pit_s[pit_s["stat_date"].map(
            lambda s: (s.month, s.day) == (12, 31))]
        ann_pubs = ann["pub_date"].tolist()
        ann_ocfps = pd.to_numeric(
            ann.get("cash_flow_per_share"), errors="coerce").tolist()

    out_veto: list[bool] = []
    out_reasons: list[str] = []
    lb = pseudo_lookback_bars
    for i, d in enumerate(days):
        reasons: list[str] = []

        # Q1 连续分红：回看窗口内分红日历年数
        lo = d - _td(days=div_lookback_days)
        j0 = bisect.bisect_left(ev_dates, lo)
        j1 = bisect.bisect_right(ev_dates, d)
        years = set(ev_years[j0:j1])
        if len(years) < div_min_years:
            reasons.append(f"{VETO_NO_DIVIDEND_STREAK}:div_years={len(years)}")

        # Q2 ROE-TTM：最新可见 PIT 行
        k = bisect.bisect_right(pit_pubs, d) - 1
        if k < 0:
            reasons.append(f"{VETO_ROE_TTM}:no_pit")
        else:
            rt = pit_roe_ttm[k]
            if pd.isna(rt):
                reasons.append(f"{VETO_ROE_TTM}:nan")
            elif rt < roe_ttm_min:
                reasons.append(f"{VETO_ROE_TTM}:{rt:.1f}")

        # Q3 分红/经营现金流：近 payout_years 年年报 ocfps 合计 vs
        # 同期分红合计（金融类跳过——ocf 口径无分红含义）
        if not fin_sector:
            lo2 = d - _td(days=ocf_window_days * payout_years)
            m0 = bisect.bisect_right(ev_dates, lo2)
            dps_win = sum(ev_cash[m0:j1])
            ka_end = bisect.bisect_right(ann_pubs, d)
            ocfps_win = [v for v in ann_ocfps[max(0, ka_end - payout_years):ka_end]
                         if pd.notna(v)]
            ocf_sum = sum(ocfps_win) if ocfps_win else float("nan")
            if dps_win > 0:
                if pd.isna(ocf_sum) or ocf_sum <= 0:
                    reasons.append(f"{VETO_PAYOUT_OCF}:ocf_sum={ocf_sum}")
                elif dps_win / ocf_sum > payout_ocf_max:
                    reasons.append(
                        f"{VETO_PAYOUT_OCF}:{dps_win / ocf_sum:.2f}")

        # Q4 伪高股息
        if i >= lb:
            dy0, dy1 = dys[i], dys[i - lb]
            p0, p1 = closes[i], closes[i - lb]
            if (pd.notna(dy0) and pd.notna(dy1) and dy0 > dy1 > 0
                    and pd.notna(p0) and pd.notna(p1) and p0 > 0
                    and p1 > 0 and p0 < p1):
                price_contrib = dy1 * (p1 / p0 - 1.0)
                share = price_contrib / (dy0 - dy1)
                if share > pseudo_price_share:
                    reasons.append(f"{VETO_PSEUDO_YIELD}:share={share:.2f}")

        out_veto.append(bool(reasons))
        out_reasons.append(";".join(reasons))

    return pd.DataFrame({"date": days, "veto": out_veto,
                         "reasons": out_reasons})


# ==============================================================================
# ② 排雷 overlay：事件流（持仓内一票否决 / 减仓）
# ==============================================================================

@dataclass(frozen=True)
class LandmineEvent:
    """排雷事件（策略侧消费）。

    ``action``：``exit_full``（T+1 开盘清仓）/ ``exit_half``（减半仓）/
    ``block_only``（不动仓只禁买）。``cooldown_until``：禁买窗口截止日
    （自然日口径，与是否曾持仓无关——事件窗口语义，防买回空转）。
    """
    symbol: str
    pub_date: _date
    rule: str
    action: str                       # 'exit_full' | 'exit_half' | 'block_only'
    cooldown_bars: int
    cooldown_until: _date | None = None
    detail: str = ""

    @property
    def event_id(self) -> tuple:
        return (self.symbol, self.pub_date, self.rule)


#: 业绩预告类型 → 动作（天风排雷口径：预亏/下修是硬卖出信号；
#: 略减/不确定弱信号降级为 block_only——只禁买不动仓，防弱信号驱动过度换手）
_FORECAST_FULL = frozenset({"预减", "首亏", "续亏"})
_FORECAST_HALF = frozenset({"略减", "不确定"})

#: 交易日冷却 → 自然日近似换算（120 交易日 ≈ 174 自然日）
_BARS_TO_DAYS = 1.45

#: 冷却期默认（交易日）：全清 120 / 减半 60——全清级事件（预亏/下修）
#: 意味基本面恶化，给一个完整财报季的观察期。
COOLDOWN_FULL_BARS = 120
COOLDOWN_HALF_BARS = 60


def build_landmine_frame(
    symbol: str,
    pit: pd.DataFrame | None,
    forecast: pd.DataFrame | None,
    statements: pd.DataFrame | None,
    *,
    ar_ratio_jump: float = 0.15,
    mc_ta_min: float = 0.15,
    ibd_ta_min: float = 0.15,
    int_yield_max: float = 0.02,
    strong_ratio: float = 0.25,
    cooldown_full: int = COOLDOWN_FULL_BARS,
    cooldown_half: int = COOLDOWN_HALF_BARS,
    cooldown_block: int = COOLDOWN_HALF_BARS,
) -> pd.DataFrame:
    """单票排雷事件帧 → ``[pub_date, rule, action, cooldown_bars, detail]``。

    规则（对应 R5 §4.1 的落地子集——只保留本机数据可证的 5 条）：

    * **L1a 预亏/预减**：forecast ``type`` ∈ {预减,首亏,续亏} ⇒ exit_full；
      预告 ``net_profit_max < 0``（即便类型标签缺失）⇒ exit_full。
    * **L1b 略减/不确定**：type ∈ {略减,不确定} ⇒ exit_half。
    * **L1c 预告下修**：同 end_date 多次预告按 pub 排序，后次
      ``net_profit_min`` 低于前次 ⇒ exit_full（"本次上限<上次下限"的
      宽松版：min 序列下降即下修）。
    * **L2 归母/扣非背离**：``net_profit_yoy>30`` 且
      ``deducted_net_profit_yoy<-30`` ⇒ exit_half（非经常性损益粉饰，
      本机实测 279 行）。
    * **L3 年报现金流断裂**：年报行（stat 12-31）``ocfps≤0`` ⇒ exit_half。
    * **L4 应收偏离**：一般工商业（comp_type=='1'）**年报口径**（stat
      12-31）应收（含票据）/营收占比同比跳升 > ``ar_ratio_jump``
      （(AR/Rev)_t > (AR/Rev)_{t-1}×(1+jump)，R5 R6 原文口径）⇒ exit_half。
      同比基期取**当前 pub 可见的最新版本**（restate 防前视）。
      季报/中报不评（应收随季度回款节奏噪声大，实测年触发 400-600 次
      主要是中报噪声）。
    * **action 语义**：``exit_full`` T+1 清仓 / ``exit_half`` 一次性减半 /
      ``block_only`` 不动仓、仅在冷却窗口内禁止买入/建仓。
    * **L5 存贷双高**：一般工商业；货币资金/总资产 ≥ ``mc_ta_min`` 且
      有息负债（短借+长借+应付债券+一年内到期非流动负债+可转债）/总资产
      ≥ ``ibd_ta_min``，且 ``int_income/money_cap < int_yield_max``
      ⇒ block_only；两比率同 ≥ ``strong_ratio`` 时豁免利息检验直接触发
      （利息收入缺失/口径漂移时仍兜底）。仅年报+中报（6-30/12-31）评估——
      季报货币资金季节噪声大。
    """
    rows: list[dict] = []

    # ---- L1：业绩预告 ----
    if forecast is not None and not forecast.empty:
        fc = forecast.copy()
        fc["pub_date"] = pd.to_datetime(fc["pub_date"], errors="coerce").dt.date
        fc["end_date"] = pd.to_datetime(fc["end_date"], errors="coerce").dt.date
        fc = fc[fc["pub_date"].notna() & fc["end_date"].notna()]
        fc = fc.sort_values(["end_date", "pub_date"]).reset_index(drop=True)
        fc["np_min"] = pd.to_numeric(fc.get("net_profit_min"), errors="coerce")
        fc["np_max"] = pd.to_numeric(fc.get("net_profit_max"), errors="coerce")
        prev_min: dict[Any, float] = {}
        for _, r in fc.iterrows():
            typ = str(r.get("type") or "").strip()
            pub, end = r["pub_date"], r["end_date"]
            fired = False
            if typ in _FORECAST_FULL:
                rows.append(dict(pub_date=pub, rule="L1a", action="exit_full",
                                 detail=f"预告{typ}"))
                fired = True
            elif pd.notna(r["np_max"]) and r["np_max"] < 0:
                rows.append(dict(pub_date=pub, rule="L1a", action="exit_full",
                                 detail=f"预亏np_max={r['np_max']:.0f}"))
                fired = True
            if not fired and typ in _FORECAST_HALF:
                rows.append(dict(pub_date=pub, rule="L1b", action="block_only",
                                 detail=f"预告{typ}"))
            # L1c 下修：同 end_date 序列，min 较前次下降
            pm = prev_min.get(end)
            if (pm is not None and pd.notna(r["np_min"])
                    and r["np_min"] < pm):
                rows.append(dict(pub_date=pub, rule="L1c", action="exit_full",
                                 detail=f"下修 {pm:.0f}→{r['np_min']:.0f}"))
            if pd.notna(r["np_min"]):
                prev_min[end] = float(r["np_min"])

    # ---- L2/L3：PIT 指标行 ----
    pit_s = _pit_sorted(pit) if pit is not None else pd.DataFrame()
    if not pit_s.empty:
        yoy = pd.to_numeric(pit_s.get("net_profit_yoy"), errors="coerce")
        dyoy = pd.to_numeric(
            pit_s.get("deducted_net_profit_yoy"), errors="coerce")
        ocf = pd.to_numeric(
            pit_s.get("cash_flow_per_share"), errors="coerce")
        for pub, stat, a, b, c in zip(
                pit_s["pub_date"], pit_s["stat_date"],
                yoy.tolist(), dyoy.tolist(), ocf.tolist()):
            if pd.notna(a) and pd.notna(b) and a > 30.0 and b < -30.0:
                rows.append(dict(
                    pub_date=pub, rule="L2", action="block_only",
                    detail=f"归母{a:.0f}%/扣非{b:.0f}%背离"))
            if (stat.month, stat.day) == (12, 31) and pd.notna(c) and c <= 0:
                rows.append(dict(pub_date=pub, rule="L3", action="exit_half",
                                 detail=f"年报ocfps={c:.2f}"))

    # ---- L4/L5：合并报表 ----
    if statements is not None and not statements.empty:
        st = statements.copy()
        st["pub_date"] = pd.to_datetime(st["pub_date"], errors="coerce").dt.date
        st["end_date"] = pd.to_datetime(st["end_date"], errors="coerce").dt.date
        st = st[st["pub_date"].notna() & st["end_date"].notna()]
        st = st.sort_values(["end_date", "pub_date"]).reset_index(drop=True)
        num = ["revenue", "total_revenue", "accounts_receiv",
               "accounts_receiv_bill", "money_cap", "trad_asset", "st_borr",
               "lt_borr", "bond_payable", "non_cur_liab_due_1y", "cb_borr",
               "total_assets", "int_income"]
        for c in num:
            st[c] = pd.to_numeric(st.get(c), errors="coerce")
        # 每行可见的最新基期值：prior end_date 行中 pub_date ≤ 当前 pub 的最后一条
        by_end: dict[_date, list[int]] = {}
        for idx, r in st.iterrows():
            by_end.setdefault(r["end_date"], []).append(idx)
        for idx, r in st.iterrows():
            comp = str(r.get("comp_type") or "").removesuffix(".0")
            if comp != "1":
                continue                     # 金融/保险报表结构不适用 L4/L5
            pub, end = r["pub_date"], r["end_date"]
            prev_year = _date(end.year - 1, end.month, end.day) \
                if not (end.month == 2 and end.day == 29) else _date(
                    end.year - 1, 2, 28)
            base = None
            for j in by_end.get(prev_year, []):
                if st.at[j, "pub_date"] <= pub:
                    base = st.loc[j]
            if base is not None and (end.month, end.day) == (12, 31):
                # L4 应收偏离（年报口径）：应收(含票据)/营收占比同比跳升
                ar0 = (base["accounts_receiv"] or 0) + (
                    base["accounts_receiv_bill"] or 0)
                ar1 = (r["accounts_receiv"] or 0) + (
                    r["accounts_receiv_bill"] or 0)
                rev0 = base["total_revenue"] if pd.notna(
                    base["total_revenue"]) else base["revenue"]
                rev1 = r["total_revenue"] if pd.notna(
                    r["total_revenue"]) else r["revenue"]
                if (pd.notna(ar0) and pd.notna(ar1) and ar0 > 0
                        and pd.notna(rev0) and pd.notna(rev1) and rev0 > 0):
                    jump = (ar1 / rev1) / (ar0 / rev0) - 1.0
                    if jump > ar_ratio_jump:
                        rows.append(dict(
                            pub_date=pub, rule="L4", action="block_only",
                            detail=f"应收占比跳升{jump:.0%}"))
            # L5 存贷双高（仅年报/中报评估）
            if (end.month, end.day) in ((12, 31), (6, 30)):
                mc, ta = r["money_cap"], r["total_assets"]
                ibd = sum(v for v in (
                    r["st_borr"], r["lt_borr"], r["bond_payable"],
                    r["non_cur_liab_due_1y"], r["cb_borr"])
                    if pd.notna(v))
                if pd.notna(mc) and pd.notna(ta) and ta > 0:
                    mc_r, ibd_r = mc / ta, ibd / ta
                    if mc_r >= mc_ta_min and ibd_r >= ibd_ta_min:
                        ii = r["int_income"]
                        int_yield = (ii / mc) if (
                            pd.notna(ii) and mc > 0) else float("nan")
                        strong = (mc_r >= strong_ratio
                                  and ibd_r >= strong_ratio)
                        if strong or (pd.notna(int_yield)
                                      and int_yield < int_yield_max):
                            rows.append(dict(
                                pub_date=pub, rule="L5", action="block_only",
                                detail=f"存贷双高 mc={mc_r:.0%} ibd={ibd_r:.0%} "
                                       f"int_yield={int_yield:.1%}"))

    if not rows:
        return pd.DataFrame(columns=["pub_date", "rule", "action",
                                     "cooldown_bars", "cooldown_until",
                                     "detail"])
    out = pd.DataFrame(rows)
    out["cooldown_bars"] = out["action"].map(
        {"exit_full": cooldown_full, "exit_half": cooldown_half,
         "block_only": cooldown_block})
    # 冷却窗口（自然日口径，策略侧用 day<=cooldown_until 判定——与持仓状态
    # 无关，消灭「卖出→游标停→买回→旧事件重触发」空转）
    out["cooldown_until"] = [
        p + _td(days=int(round(b * _BARS_TO_DAYS)))
        for p, b in zip(out["pub_date"].tolist(),
                        out["cooldown_bars"].tolist())]
    out["symbol"] = symbol
    out = (out.sort_values("pub_date")
              .drop_duplicates(subset=["pub_date", "rule"], keep="first")
              .reset_index(drop=True))
    return out[["symbol", "pub_date", "rule", "action", "cooldown_bars",
                "detail"]]



# ==============================================================================
# ③ PEAD 进攻增强：扣非 SUE 截面分位 + DEMAX 条件化
# ==============================================================================

@dataclass(frozen=True)
class PeadEvent:
    """PEAD 事件（策略侧消费）。

    ``entry_date``：首个可行动交易日（≥pub_date；公告当日晚披露 ⇒ T+1 开盘
    成交的最早信号日）。``expire_date``：信号失效日（entry+hold_max 交易日）。
    ``pct_rank``：行业中性化后 SUE 在 pub 月 cohort 内的截面分位 [0,1]。
    """
    symbol: str
    pub_date: _date
    stat_date: _date
    sue_raw: float
    sue_adj: float
    pct_rank: float
    entry_date: _date
    expire_date: _date

    @property
    def event_id(self) -> tuple:
        return (self.symbol, self.pub_date)


def build_pead_frame(
    pool_pit: Mapping[str, pd.DataFrame],
    pool_bars: Mapping[str, pd.DataFrame],
    calendar: Sequence[_date],
    *,
    industry: Mapping[str, str] | None = None,
    sue_hist_min: int = 4,
    sue_hist_max: int = 12,
    pct_min: float = 0.80,
    pre_gap_days: int = 20,
    pre_gap_max: float = 0.07,
    limit_buffer_pct: float = 0.5,
    hold_max_days: int = 40,
    winsor_pct: float = 0.01,
) -> pd.DataFrame:
    """全池 PEAD 事件帧。

    列：``symbol, pub_date, stat_date, sue_raw, sue_adj, pct_rank, entry_date,
    expire_date, eligible, excl_reason``。

    信号定义（R5 §4.2 S4 复合口径）：
      * ``sue_raw`` = 本期扣非同比 − 此前 ``sue_hist_max`` 期（≥``sue_hist_min``
        期才计算）扣非同比均值（季节性随机游走漂移项——扣非同比本身已剔除
        季节性，trailing mean 即漂移项估计）；
      * cohort = 同一 pub **月**的全部池内事件；sue_raw 先按 cohort 1%/99%
        winsorize，再减**同行业** cohort 均值（行业中性化；无行业标签则
        退化为全 cohort 去均值——中性情境下两者等价于排序不变）；
      * ``pct_rank`` = sue_adj 在 cohort 内分位；≥``pct_min``(0.80) 才合格；
      * **DEMAX 条件化**：排除公告前 ``pre_gap_days`` 个交易日内出现过
        开盘跳空 ≥``pre_gap_max``（默认 7%）者——预期已被抢跑透支；
      * **触板排除**：entry 日与次日 ``|pctChg|`` ≥ 板块阈值−``limit_buffer_pct``
        ⇒ 不合格（一字/触板买不进或已透支）；
      * ``entry_date`` = 首个 ≥pub_date 的交易日；``expire_date`` =
        entry 起第 ``hold_max_days`` 个交易日（自然到期日，策略可再收紧）。
    """
    cal = sorted(calendar)
    cal_idx = {d: i for i, d in enumerate(cal)}

    events: list[dict] = []
    for sym, pit in pool_pit.items():
        pit_s = _pit_sorted(pit)
        if pit_s.empty:
            continue
        dyoy = pd.to_numeric(
            pit_s.get("deducted_net_profit_yoy"), errors="coerce").tolist()
        pubs = pit_s["pub_date"].tolist()
        stats = pit_s["stat_date"].tolist()

        bars = pool_bars.get(sym)
        bd: dict[_date, tuple[float, float, float]] = {}
        if bars is not None and not bars.empty:
            b = bars.copy()
            b["_d"] = pd.to_datetime(b["date"]).dt.date
            for d0, o0, p0, c0, chg in zip(
                    b["_d"].tolist(),
                    pd.to_numeric(b["open"], errors="coerce").tolist(),
                    pd.to_numeric(b["preclose"], errors="coerce").tolist(),
                    pd.to_numeric(b["close"], errors="coerce").tolist(),
                    pd.to_numeric(b.get("pctChg"), errors="coerce").tolist()):
                bd[d0] = (o0, p0, chg)
        limit_pct = None
        try:
            limit_pct = board_limit_pct(sym)
        except Exception:                    # noqa: BLE001 未登记前缀→不判触板
            pass

        hist: list[float] = []
        for pub, stat, v in zip(pubs, stats, dyoy):
            if pd.isna(v):
                hist.append(float("nan"))     # 占位保持期序，但不进均值
                continue
            clean_hist = [h for h in hist if pd.notna(h)]
            sue = (float(v) - sum(clean_hist[-sue_hist_max:]
                   ) / len(clean_hist[-sue_hist_max:])
                   ) if len(clean_hist) >= sue_hist_min else float("nan")
            hist.append(float(v))
            if pd.isna(sue):
                continue
            # entry = 首个 ≥pub 的交易日
            ei = bisect.bisect_left(cal, pub)
            if ei >= len(cal):
                continue
            entry = cal[ei]
            excl = ""
            # DEMAX：公告前 pre_gap_days 个交易日内的最大开盘跳空
            if pre_gap_days > 0:
                lo_i = max(0, ei - pre_gap_days)
                max_gap = 0.0
                for d0 in cal[lo_i:ei]:
                    o0, p0, _ = bd.get(d0, (float("nan"),) * 3)
                    if pd.notna(o0) and pd.notna(p0) and p0 > 0:
                        max_gap = max(max_gap, o0 / p0 - 1.0)
                if max_gap >= pre_gap_max:
                    excl = f"pre_gap={max_gap:.2%}"
            # 触板排除：entry 与次日 |pctChg| ≥ 板块阈值−buffer
            if not excl and limit_pct is not None:
                thr = limit_pct - limit_buffer_pct
                for d0 in cal[ei:ei + 2]:
                    chg = bd.get(d0, (None, None, None))[2]
                    if pd.notna(chg) and abs(chg) >= thr:
                        excl = f"limit_touch@{d0}"
                        break
            xi = min(ei + hold_max_days, len(cal) - 1)
            events.append({
                "symbol": sym, "pub_date": pub, "stat_date": stat,
                "sue_raw": sue, "entry_date": entry,
                "expire_date": cal[xi], "excl_reason": excl,
                "_month": pub.strftime("%Y-%m"),
                "_ind": (industry or {}).get(sym, "?"),
            })

    ev = pd.DataFrame(events)
    if ev.empty:
        return pd.DataFrame(columns=[
            "symbol", "pub_date", "stat_date", "sue_raw", "sue_adj",
            "pct_rank", "entry_date", "expire_date", "eligible",
            "excl_reason"])

    # cohort 内 winsorize + 行业**中位数**去均值 + 分位
    # （均值会被 yoy 重尾值污染——实测均值口径下 sue_raw=-68 的深度负意外
    #   被行业离群值拉到分位 0.957 变“合格”；中位数 + sue_adj>0 符号闸
    #   双重防中性化翻转。行业组 <3 只退回全 cohort 中位数。）
    sue_adj = pd.Series(float("nan"), index=ev.index)
    pct_rank = pd.Series(float("nan"), index=ev.index)
    for month, grp in ev.groupby("_month"):
        s = grp["sue_raw"].astype(float)
        lo_q, hi_q = s.quantile(winsor_pct), s.quantile(1.0 - winsor_pct)
        w = s.clip(lo_q, hi_q)
        ind_med = w.groupby(grp["_ind"]).transform("median")
        ind_cnt = w.groupby(grp["_ind"]).transform("count")
        cohort_med = w.median()
        center = ind_med.where(ind_cnt >= 3, cohort_med)
        adj = w - center
        sue_adj.loc[grp.index] = adj
        pct_rank.loc[grp.index] = adj.rank(pct=True)
    ev["sue_adj"] = sue_adj
    ev["pct_rank"] = pct_rank
    ev["eligible"] = ((ev["excl_reason"] == "") & (ev["pct_rank"] >= pct_min)
                      & (ev["sue_adj"] > 0))
    ev.loc[ev["pct_rank"].isna(), "eligible"] = False
    return (ev.drop(columns=["_month", "_ind"])
              .sort_values(["entry_date", "symbol"])
              .reset_index(drop=True))


# ==============================================================================
# SignalLayers 查询对象（无状态；策略侧持有 acted/cursor 状态）
# ==============================================================================

@dataclass
class SignalLayers:
    """三层信号的只读查询门面对象。

    * ``veto``:    {symbol: (dates_tuple, veto_tuple, reasons_tuple)} 按日 asof；
    * ``landmine``: {symbol: tuple[LandmineEvent, ...]} 按 pub_date 升序——
      策略侧用游标推进（pub_date ≤ day 即"新可见"）；
    * ``pead``:    {day_iso: [PeadEvent]} entry_date≤day≤expire_date 预展开。
    """
    veto: Mapping[str, tuple] = field(default_factory=dict)
    landmine: Mapping[str, tuple] = field(default_factory=dict)
    pead_by_day: Mapping[str, list] = field(default_factory=dict)
    manifest: Mapping[str, Any] = field(default_factory=dict)

    # ---- 质量否决 ----
    def veto_reason(self, symbol: str, day: _date) -> str | None:
        """``(symbol, day)`` 是否被准入否决；返回原因串或 None。"""
        entry = self.veto.get(symbol)
        if entry is None:
            return "no_veto_data"            # 无数据 = 无质量证据（fail-closed）
        dates, flags, reasons = entry
        i = bisect.bisect_right(dates, day) - 1
        if i < 0:
            return "before_veto_history"     # 首个评估日前无证据 → 否决
        return reasons[i] if flags[i] else None

    # ---- 排雷 ----
    def landmine_list(self, symbol: str) -> tuple:
        """该票全部排雷事件（pub_date 升序元组，供策略游标推进）。"""
        return self.landmine.get(symbol, ())

    def landmine_block(self, symbol: str, day: _date) -> str | None:
        """``day`` 是否处于某排雷事件的禁买冷却窗内；返回规则名或 None。

        事件窗口语义：``pub_date ≤ day ≤ cooldown_until``——不管当时是否
        持仓，窗口内一律禁买（排雷冷却的本意就是“这只票近期基本面有疑”）。
        """
        for ev in self.landmine.get(symbol, ()):
            if ev.pub_date > day:
                break                          # 升序，后面都不可见
            until = ev.cooldown_until or ev.pub_date
            if ev.pub_date <= day <= until:
                return ev.rule
        return None

    # ---- PEAD ----
    def pead_active(self, day: _date) -> list:
        """当日活跃（entry≤day≤expire）且 eligible 的 PEAD 事件。"""
        return self.pead_by_day.get(day.isoformat(), ())

    # ---- 出处 ----
    @property
    def manifest_hash(self) -> str:
        import hashlib
        h = hashlib.sha256()
        for k in sorted(self.manifest):
            h.update(f"{k}={self.manifest[k]}".encode())
        return h.hexdigest()[:16]


def _load_dir_frames(d: Path) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    if not d.exists():
        return out
    for p in sorted(d.glob("*.parquet")):
        try:
            out[p.stem] = pd.read_parquet(p)
        except Exception as exc:             # noqa: BLE001
            logger.warning("signal layer %s 读取失败跳过: %s", p, exc)
    return out


def load_signal_layers(
    data_root: Path,
    calendar: Sequence[_date],
    *,
    require: Iterable[str] = ("veto", "landmine", "pead"),
    suffix: str = "",
) -> SignalLayers:
    """从 sidecar 目录装载三层表 → ``SignalLayers``。

    ``require`` 列出的层若目录缺失/为空 ⇒ ``FileNotFoundError``
    （fail-closed：启用层但无数据不得静默降级为"无约束"）。
    """
    data_root = Path(data_root)
    veto_dir = data_root / f"quality_veto{suffix}"
    lm_dir = data_root / f"landmine_events{suffix}"
    pead_dir = data_root / f"pead_signals{suffix}"
    need = set(require)
    missing = [
        name for name, d in (("veto", veto_dir), ("landmine", lm_dir),
                             ("pead", pead_dir))
        if name in need and not (d.exists() and any(d.glob("*.parquet")))]
    if missing:
        raise FileNotFoundError(
            f"信号层 sidecar 缺失: {missing}（{data_root}）——"
            f"⛔ Fail-Closed：请先运行 scripts/build_signal_layers.py")

    veto: dict[str, tuple] = {}
    for sym, df in _load_dir_frames(veto_dir).items():
        d = pd.to_datetime(df["date"]).dt.date.tolist()
        order = sorted(range(len(d)), key=lambda i: d[i])
        dates = tuple(d[i] for i in order)
        flags = tuple(bool(df["veto"].iloc[i]) for i in order)
        reasons = tuple(str(df["reasons"].iloc[i]) for i in order)
        veto[sym] = (dates, flags, reasons)

    landmine: dict[str, tuple] = {}
    for sym, df in _load_dir_frames(lm_dir).items():
        pubs = pd.to_datetime(df["pub_date"]).dt.date.tolist()
        untils = (pd.to_datetime(df["cooldown_until"]).dt.date.tolist()
                  if "cooldown_until" in df.columns else [None] * len(df))
        evs = []
        for pub, rule, act, cd, det, un in zip(
                pubs, df["rule"].tolist(), df["action"].tolist(),
                df["cooldown_bars"].tolist(), df["detail"].tolist(), untils):
            evs.append(LandmineEvent(
                symbol=sym, pub_date=pub, rule=str(rule), action=str(act),
                cooldown_bars=int(cd),
                cooldown_until=None if pd.isna(un) else un,
                detail=str(det or "")))
        evs.sort(key=lambda e: e.pub_date)
        landmine[sym] = tuple(evs)

    pead_by_day: dict[str, list] = {}
    cal_set = set(calendar)
    cal = sorted(cal_set)
    ci = {d: i for i, d in enumerate(cal)}
    for sym, df in _load_dir_frames(pead_dir).items():
        if "eligible" in df.columns:
            df = df[df["eligible"] == True]    # noqa: E712 只装载合格事件
        for _, r in df.iterrows():
            entry = r["entry_date"]
            expire = r["expire_date"]
            entry = entry.date() if hasattr(entry, "date") else entry
            expire = expire.date() if hasattr(expire, "date") else expire
            ev = PeadEvent(
                symbol=sym,
                pub_date=r["pub_date"].date() if hasattr(
                    r["pub_date"], "date") else r["pub_date"],
                stat_date=r["stat_date"].date() if hasattr(
                    r["stat_date"], "date") else r["stat_date"],
                sue_raw=float(r["sue_raw"]),
                sue_adj=float(r["sue_adj"]) if pd.notna(r["sue_adj"])
                else float("nan"),
                pct_rank=float(r["pct_rank"]) if pd.notna(r["pct_rank"])
                else float("nan"),
                entry_date=entry, expire_date=expire)
            i0, i1 = ci.get(entry), ci.get(expire)
            if i0 is None or i1 is None:
                continue
            for d in cal[i0:i1 + 1]:
                pead_by_day.setdefault(d.isoformat(), []).append(ev)

    manifest = {
        "veto_dir": str(veto_dir), "landmine_dir": str(lm_dir),
        "pead_dir": str(pead_dir),
        "veto_symbols": len(veto), "landmine_symbols": len(landmine),
        "pead_days": len(pead_by_day),
        "pead_events": sum(len(v) for v in pead_by_day.values()),
    }
    return SignalLayers(veto=veto, landmine=landmine,
                        pead_by_day=pead_by_day, manifest=manifest)
