#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e38 分钟线源可得性现场实测探针（2026-09-21 窗口）。

逐源实测三件事，全部如实标注「本次实测」：
  1. 历史深度——月桶二分找最早非空段；
  2. 字段与格数——5m 应为 48 根/日，记录首尾 bar 时间戳约定；
  3. 限速粗测——连续调 N 次计时，外推 487 池 ~5 年采集成本；
另测 baostock 停牌日行为（sh.600027 于 2024-07-19~07-30 停牌 10 个交易日，
日线层该段无行——看分钟线是否同样直接缺行，还是吐停牌日空行）。

tushare 路：仓规要求 PROMAX_TUSHARE_KEY 从 .env/环境变量取（scripts/_secrets.py），
本机两者皆无 ⇒ 不可测，如实登记（tracker 另记该 key 此前实测「token 不对」）。

仅打印 + 落盘 data/minute_probe/source_probe.json（data/ 已 gitignore）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
from scripts._secrets import _read_env_file  # noqa: E402  # 只读探测，不打印值
import os  # noqa: E402

OUT = Path("data/minute_probe")
OUT.mkdir(parents=True, exist_ok=True)
RESULT: dict = {"probe_date": "2026-09-21", "sources": {}}


def rec(src: str, **kw):
    RESULT["sources"].setdefault(src, {}).update(kw)


# ---------------------------------------------------------------- tushare --
def probe_tushare():
    has_env = bool(os.environ.get("PROMAX_TUSHARE_KEY"))
    has_file = bool(_read_env_file("PROMAX_TUSHARE_KEY"))
    print("[tushare] PROMAX_TUSHARE_KEY: env=%s .env=%s" % (has_env, has_file))
    if not (has_env or has_file):
        rec("tushare", status="unavailable",
            note="本机无 key（.env 不入 git，本 VM 未配置）；"
                 "TASK_TRACKER 记该 key 此前实测『token 不对』（商家配额未放开）")
        print("  -> 不可测：无凭证；且历史实测 token 不对")
        return
    import tushare as ts  # 仅当有 key 时才需要
    ts.set_token(os.environ.get("PROMAX_TUSHARE_KEY") or _read_env_file("PROMAX_TUSHARE_KEY"))
    pro = ts.pro_api()
    try:
        t0 = time.time()
        df = pro.stk_mins(ts_code="600000.SH", freq="5min",
                          start_date="2021-01-04 09:30:00", end_date="2021-01-05 15:00:00")
        rec("tushare", status="ok", rows=len(df), latency=round(time.time() - t0, 2))
        print("  stk_mins rows=%d" % len(df))
    except Exception as e:  # noqa: BLE001
        rec("tushare", status="error", error=str(e)[:120])
        print("  stk_mins error: %s" % str(e)[:120])


# --------------------------------------------------------------- akshare --
def probe_akshare():
    import akshare as ak
    try:  # 东财地址池部分损坏（FINDING-299），装换池重试垫片
        from finai.sources.base import install_eastmoney_pool_retry
        install_eastmoney_pool_retry()
    except Exception as e:  # noqa: BLE001
        print("[akshare] eastmoney shim 未装上：%s" % e)
    rep = {}
    # 近期窗：拿真实格数/字段
    for tag, start, end, period in [
        ("recent_5d", "2026-09-10 09:30:00", "2026-09-16 15:00:00", "5"),
        ("old_2015", "2015-01-05 09:30:00", "2015-01-09 15:00:00", "5"),
        ("deep_5m", "2021-01-04 09:30:00", "2026-09-16 15:00:00", "5"),
        ("old_60m", "2015-01-05 09:30:00", "2015-01-09 15:00:00", "60"),
    ]:
        t0 = time.time()
        try:
            df = ak.stock_zh_a_hist_min_em(
                symbol="600000", start_date=start, end_date=end,
                period=period, adjust="")
            lat = round(time.time() - t0, 2)
            if df is None or len(df) == 0:
                rep[tag] = {"rows": 0, "latency_s": lat}
                print("[akshare] %s period=%s rows=0 (%.2fs)" % (tag, period, lat))
                continue
            first_col = df.columns.tolist()
            rep[tag] = {"rows": len(df), "cols": first_col,
                        "first_ts": str(df.iloc[0, 0]), "last_ts": str(df.iloc[-1, 0]),
                        "latency_s": lat}
            print("[akshare] %s period=%s rows=%d first=%s last=%s (%.2fs)"
                  % (tag, period, len(df), rep[tag]["first_ts"], rep[tag]["last_ts"], lat))
            print("         cols=%s" % first_col)
        except Exception as e:  # noqa: BLE001
            rep[tag] = {"error": str(e)[:160], "latency_s": round(time.time() - t0, 2)}
            print("[akshare] %s period=%s ERROR %s" % (tag, period, str(e)[:160]))
    # 限速粗测：连续 3 次近期窗
    lat = []
    for _ in range(3):
        t0 = time.time()
        try:
            ak.stock_zh_a_hist_min_em(symbol="600519", start_date="2026-09-10 09:30:00",
                                      end_date="2026-09-16 15:00:00", period="5", adjust="")
        except Exception:  # noqa: BLE001
            pass
        lat.append(round(time.time() - t0, 2))
    rep["rate_test_latencies"] = lat
    rec("akshare_hist_min_em", **rep)
    print("[akshare] rate test latencies=%s" % lat)


# -------------------------------------------------------------- baostock --
def _bs_drain(rs):
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return rows


def probe_baostock():
    import baostock as bs
    lg = bs.login()
    print("[baostock] login: %s %s" % (lg.error_code, lg.error_msg))
    rep = {"login": lg.error_code}
    FIELDS = "date,time,code,open,high,low,close,volume,amount,adjustflag"
    # 1) 深度月桶
    months = ["2015-01", "2017-06", "2019-06", "2019-12", "2020-03", "2020-06",
              "2020-09", "2020-12", "2021-01", "2021-06", "2024-01"]
    depth = {}
    for m in months:
        rs = bs.query_history_k_data_plus(
            "sh.600000", FIELDS, start_date=m + "-01", end_date=m + "-28",
            frequency="5", adjustflag="3")
        rows = _bs_drain(rs)
        first_time = rows[0][1] if rows else None
        n_days = len({r[0] for r in rows})
        depth[m] = {"rows": len(rows), "days": n_days,
                    "bars_per_day": round(len(rows) / n_days, 1) if n_days else 0,
                    "first_time": first_time}
        print("[baostock] 5m %s rows=%d days=%d bars/day=%.1f first=%s"
              % (m, len(rows), n_days, len(rows) / n_days if n_days else 0, first_time))
    rep["depth_months"] = depth
    # 在首个非空月内二分首交易日
    first_month = next((m for m in months if depth[m]["rows"] > 0), None)
    rep["first_nonempty_month"] = first_month
    if first_month:
        rs = bs.query_history_k_data_plus(
            "sh.600000", FIELDS, start_date=first_month + "-01",
            end_date=first_month + "-31", frequency="5", adjustflag="3")
        rows = _bs_drain(rs)
        if rows:
            rep["earliest_bar"] = rows[0]
            print("[baostock] earliest bar: %s %s" % (rows[0][0], rows[0][1]))
    # 2) 字段格数：一个完整交易日
    rs = bs.query_history_k_data_plus(
        "sh.600000", FIELDS, start_date="2024-01-05", end_date="2024-01-05",
        frequency="5", adjustflag="3")
    one = _bs_drain(rs)
    rep["fields"] = rs.fields
    rep["bars_full_day"] = len(one)
    rep["first_bar_time"] = one[0][1] if one else None
    rep["last_bar_time"] = one[-1][1] if one else None
    rep["sample_bar"] = one[0] if one else None
    print("[baostock] 2024-01-05 bars=%d first=%s last=%s fields=%s"
          % (len(one), rep["first_bar_time"], rep["last_bar_time"], rs.fields))
    # 3) 停牌日行为：sh.600027 2024-07-19~07-30 停牌（日线无行）
    rs = bs.query_history_k_data_plus(
        "sh.600027", FIELDS, start_date="2024-07-15", end_date="2024-08-05",
        frequency="5", adjustflag="3")
    sus = _bs_drain(rs)
    days = sorted({r[0] for r in sus})
    rep["suspension_window_dates"] = days
    rep["suspension_has_empty_rows"] = any(d in days for d in
                                         ["2024-07-19", "2024-07-22", "2024-07-23",
                                          "2024-07-24", "2024-07-25", "2024-07-26",
                                          "2024-07-29", "2024-07-30"])
    print("[baostock] 600027 停牌窗分钟线出现日期=%s" % days)
    # 4) 限速/吞吐：单股全史一次拉 vs 逐月拉
    t0 = time.time()
    rs = bs.query_history_k_data_plus(
        "sh.600000", FIELDS, start_date="2021-01-01", end_date="2026-09-16",
        frequency="5", adjustflag="3")
    full = _bs_drain(rs)
    rep["full_range_call"] = {"rows": len(full), "seconds": round(time.time() - t0, 2),
                              "days": len({r[0] for r in full})}
    print("[baostock] 单股 2021-01~2026-09 一次拉：rows=%d days=%d (%.2fs)"
          % (len(full), rep["full_range_call"]["days"], time.time() - t0))
    lat2 = []
    for sym in ["sh.600519", "sz.000001", "sz.300750"]:
        t0 = time.time()
        rs = bs.query_history_k_data_plus(
            sym, FIELDS, start_date="2024-01-01", end_date="2024-03-31",
            frequency="5", adjustflag="3")
        n = len(_bs_drain(rs))
        lat2.append({"symbol": sym, "rows": n, "seconds": round(time.time() - t0, 2)})
    rep["rate_test"] = lat2
    print("[baostock] rate test=%s" % lat2)
    bs.logout()
    rec("baostock_5m", **rep)


if __name__ == "__main__":
    probe_tushare()
    probe_akshare()
    probe_baostock()
    out = OUT / "source_probe.json"
    out.write_text(json.dumps(RESULT, ensure_ascii=False, indent=1, default=str))
    print("\n落盘 %s" % out)
