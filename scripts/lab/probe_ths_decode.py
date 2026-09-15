#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""同花顺差分编码解码验证 + 网易CSV备用通道实测。

Fail-Closed 纪律：解码正确性必须用本地权威红利池数据核验
（sh.600000：2020-06-15 收盘=10.35，2023-03-10 收盘=7.15），
核验不过则同花顺链路整体弃用，⛔ 不带病上线。
"""
from __future__ import annotations

import json
import sys

import requests

UA = {"Referer": "http://stockpage.10jqka.com.cn/", "User-Agent": "Mozilla/5.0"}


def ths_fetch(symbol: str) -> dict:
    num = symbol.split(".")[1]
    url = f"http://d.10jqka.com.cn/v6/line/hs_{num}/01/all.js"
    r = requests.get(url, headers=UA, timeout=15)
    r.raise_for_status()
    r.encoding = "gbk"
    t = r.text
    return json.loads(t[t.index("(") + 1: t.rindex(")")])


def ths_decode(d: dict) -> list[tuple]:
    """差分解码：每组4值 (d_open, d_high, d_low, d_close)，对前收/首日发行价累加。"""
    factor = d["priceFactor"]
    nums = [int(x) for x in d["price"].split(",") if x != ""]
    vols = d["volumn"].split(",")
    dates_raw = d["dates"].split(",")
    full_dates: list[str] = []
    idx = 0
    for year, cnt in d["sortYear"]:
        for dd in dates_raw[idx: idx + cnt]:
            dd = dd.zfill(4)
            full_dates.append(f"{year}-{dd[:2]}-{dd[2:]}")
        idx += cnt
    issue_base = float(d.get("issuePrice") or 0)
    rows = []
    prev_close = None
    for k in range(len(full_dates)):
        a, b, c, e = nums[k * 4: k * 4 + 4]
        if prev_close is None:
            open_i = int(round(issue_base * factor)) + a
        else:
            open_i = prev_close + a
        high = open_i + b
        low = open_i + c
        close = open_i + e
        try:
            vol = float(vols[k]) if k < len(vols) and vols[k] not in ("", "-") else 0.0
        except ValueError:
            vol = 0.0
        rows.append((full_dates[k], open_i / factor, high / factor,
                     low / factor, close / factor, vol))
        prev_close = close
    return rows


def main() -> int:
    print("=" * 60)
    print("[1] 网易 chddata CSV 备用通道实测")
    print("=" * 60)
    try:
        url = ("http://quotes.money.163.com/service/chddata.html"
               "?code=0600000&start=20150101&end=20241231"
               "&fields=TCLOSE;HIGH;LOW;TOPEN;VOTURNOVER;VATURNOVER")
        r = requests.get(url, timeout=20)
        r.encoding = "gbk"
        lines = [ln for ln in r.text.strip().splitlines() if ln]
        print(f"  HTTP {r.status_code}, 行数 {len(lines)}")
        if len(lines) > 2:
            print(f"  表头: {lines[0][:80]}")
            print(f"  首行: {lines[1][:80]}")
            print(f"  尾行: {lines[-1][:80]}")
    except Exception as e:
        print(f"  网易 FAIL: {repr(e)[:120]}")

    print("=" * 60)
    print("[2] 同花顺差分解码验证（对照本地权威价）")
    print("=" * 60)
    d = ths_fetch("sh.600000")
    print(f"  total={d.get('total')} issuePrice={d.get('issuePrice')!r} "
          f"factor={d.get('priceFactor')} sortYear首末={d['sortYear'][0]}..{d['sortYear'][-1]}")
    rows = ths_decode(d)
    print(f"  解码行数: {len(rows)}")
    print(f"  首日: {rows[0]}")
    print(f"  末日: {rows[-1]}")
    checks = {"2020-06-15": 10.35, "2023-03-10": 7.15}
    ok = True
    rmap = {r[0]: r for r in rows}
    for day, expect in checks.items():
        got = rmap.get(day)
        if got is None:
            print(f"  {day}: FAIL 日期缺失")
            ok = False
            continue
        match = abs(got[4] - expect) < 1e-6
        ok = ok and match
        print(f"  {day}: 解码收盘={got[4]:.2f} 权威={expect} {'OK' if match else 'MISMATCH'}")
    in_range = [r for r in rows if "2015-01-01" <= r[0] <= "2024-12-31"]
    print(f"  2015-2024 区间行数: {len(in_range)}（预期约2426）")
    print(f"  区间首日: {in_range[0] if in_range else 'NONE'}")
    print(f"\n解码核验总评: {'PASS 可上线' if ok else 'FAIL 禁用此源'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
