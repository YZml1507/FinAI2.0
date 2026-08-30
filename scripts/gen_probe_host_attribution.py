import inspect, json
from pathlib import Path

LIBS = ["akshare", "efinance", "baostock", "adata", "mootdx", "tdxpy"]

def host_to_domain(host: str) -> str:
    h = host.lower()
    if "eastmoney" in h or "push2his" in h: return "eastmoney"
    if "10jqka" in h or "hexin" in h: return "ths"
    if "cninfo" in h: return "cninfo"
    if "sina" in h: return "sina"
    if "legulegu" in h: return "legulegu"
    return "other"

table = {}
for lib in LIBS:
    try:
        mod = __import__(lib)
    except Exception:
        continue
    for attr_name in dir(mod):
        obj = getattr(mod, attr_name, None)
        if not callable(obj): continue
        try:
            src = inspect.getsource(obj)
        except Exception:
            continue
        hosts = set()
        for token in src.split():
            t = token.strip("'\"`()[]{},;")
            if t.startswith(("http://", "https://")):
                from urllib.parse import urlparse
                try:
                    h = urlparse(t).hostname
                    if h: hosts.add(h)
                except Exception:
                    pass
        dom = "other"
        for h in hosts:
            d = host_to_domain(h)
            if d != "other":
                dom = d
                break
        key = f"{lib}.{attr_name}"
        table[key] = dom

out = Path(r"D:\Projects\FinAI2.0\scripts\probe_host_attribution.py")
header = "# Auto-generated (2026-08-30): host attribution from installed lib source\n# Regenerate: python scripts/gen_probe_host_attribution.py\nHOST_ATTRIBUTION = "

def attributed_domain_body():
    return '''def attributed_domain(lib: str, name: str) -> str | None:
    """Look up lib.name in HOST_ATTRIBUTION. Returns None if not found."""
    return HOST_ATTRIBUTION.get(f"{lib}.{name}")
'''

body = json.dumps(table, ensure_ascii=False, indent=2)
with open(out, "w", encoding="utf-8") as f:
    f.write(header + body + "\n\n" + attributed_domain_body())
print(f"written {len(table)} entries -> {out}")
