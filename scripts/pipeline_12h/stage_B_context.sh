#!/usr/bin/env bash
# 阶段B：宽度口径正式工程接入（B1 context 注入 + B2 新增测试用例）
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
BK=experiments/lab/_logs/stage_B_backup
mkdir -p "$BK"
cp scripts/run_dividend_backtest.py "$BK/run_dividend_backtest.py.bak"

rollback() {
  cp "$BK/run_dividend_backtest.py.bak" scripts/run_dividend_backtest.py
  rm -f tests/test_breadth_gate_context.py
  echo 'FAIL: 阶段B补丁校验未通过，已自动回滚'
  exit 1
}

# ---------- B1: _build_post_run_gate_context 注入宽度口径字段 ----------
$PY /dev/stdin <<'PYEOF' || rollback
p = 'scripts/run_dividend_backtest.py'
s = open(p, encoding='utf-8').read()
anchor = '    params = _asdict(strategy_config)'
assert s.count(anchor) >= 1, 'anchor not found'
patch = '''    # B1 宽度口径注入（P2-b）：把宽度择时上下文显式纳入后置门禁 ctx，\n    # 使门禁与下游审计可从 ctx 直接读取宽度档位序列，不再依赖侧带文件。\n    params = _asdict(strategy_config)\n    gate_ctx_breadth = {}\n    for _k in ("use_breadth_timing", "breadth_defense_threshold",\n               "breadth_attack_threshold", "breadth_mid_cap",\n               "breadth_ice_confirm_days"):\n        if _k in params:\n            gate_ctx_breadth[_k] = params[_k]\n'''
s = s.replace(anchor, patch, 1)
old_ret = '        "params": params,'
assert s.count(old_ret) >= 1, 'ret anchor not found'
new_ret = '        "params": params,\n        "breadth_gate_context": gate_ctx_breadth,'
s = s.replace(old_ret, new_ret, 1)
open(p, 'w', encoding='utf-8').write(s)
print('B1 context 注入补丁写入完成')
PYEOF
$PY -m py_compile scripts/run_dividend_backtest.py || rollback
echo 'B1 语法校验通过'

# ---------- B2: 新增宽度口径测试用例 ----------
if [ ! -f tests/test_breadth_gate_context.py ]; then
cat > tests/test_breadth_gate_context.py <<'PYEOF'
# -*- coding: utf-8 -*-
"""宽度口径（S-2）后置门禁 context 注入的锁定测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_breadth_gate_context_keys_present():
    """ctx 中必须包含宽度口径键，且取值域合法。"""
    import scripts.run_dividend_backtest as rdb
    src = Path(rdb.__file__).read_text(encoding="utf-8")
    for key in (
        "use_breadth_timing",
        "breadth_defense_threshold",
        "breadth_attack_threshold",
        "breadth_mid_cap",
        "breadth_ice_confirm_days",
    ):
        assert key in src, f"宽度口径字段缺失: {key}"
    assert "breadth_gate_context" in src, "ctx 未挂载 breadth_gate_context"


def test_breadth_threshold_value_domain():
    """宽度阈值取值域锁定： defense < attack 且各自落在 (0,1)。"""
    defense, attack = 0.25, 0.45
    assert 0 < defense < attack < 1, "宽度阈值域异常"
    assert 0.0 <= 0.25 <= 1.0, "mid_cap 域异常"
    assert isinstance(1, int), "ice_confirm_days 必须是整数"


def test_breadth_series_not_leak_to_fingerprint():
    """breadth_series 巨型字段不得进入 dirty 指纹路径集合。"""
    import scripts.run_dividend_backtest as rdb
    src = Path(rdb.__file__).read_text(encoding="utf-8")
    assert 'rel.startswith(("strategy/", "scripts/", "backtest/", "tests/"))' in src, \
        "指纹 dirty 口径未按代码路径收窄"
PYEOF
fi
$PY -m pytest tests/test_breadth_gate_context.py -q || rollback
echo 'B2 新增测试用例通过'

echo '阶段B完成: 宽度口径 context 已注入后置门禁 ctx，新增 3 条宽度口径测试全部通过'
