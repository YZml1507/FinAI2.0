#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""备用日线源探查：解析 FetchResult 真实结构，实测 tdx / citydata 可达性。仅打印。"""
import sys
sys.path.insert(0, ".")

# ---- tdx ----
try:
    from finai.sources import tdx_daily_bar_adapter as tdx
    fr = tdx.fetch("daily_bar", symbol="sh.600000",
                   start_date="20150101", end_date="20241231")
    attrs = {a: getattr(fr, a, None) for a in dir(fr) if not a.startswith("_")}
    print("[tdx] FetchResult attrs:")
    for k, v in attrs.items():
        if callable(v):
            continue
        s = repr(v)
        print("   %s = %s" % (k, s[:90]))
except Exception as e:
    import traceback; traceback.print_exc()

print("-" * 50)

# ---- citydata ----
try:
    from finai.sources import citydata_source as cd
    fr2 = cd.fetch("daily", symbol="sh.600000",
                   start_date="20150101", end_date="20241231")
    attrs2 = {a: getattr(fr2, a, None) for a in dir(fr2) if not a.startswith("_")}
    print("[citydata] FetchResult attrs:")
    for k, v in attrs2.items():
        if callable(v):
            continue
        s = repr(v)
        print("   %s = %s" % (k, s[:90]))
except Exception as e:
    import traceback; traceback.print_exc()
