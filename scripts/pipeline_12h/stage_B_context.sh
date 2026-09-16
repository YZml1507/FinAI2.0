#!/usr/bin/env bash
# 阶段B：宽度口径正式工程接入（幂等校验版）
# 历史事故：本阶段原实现用 `count(anchor) >= 1` 判定后无条件追加补丁，
# 流水线每轮重跑都再插一遍，导致 run_dividend_backtest.py 被同一补丁块
# 重复追加约 18 次（2026-09-16 已手工清理并把 S-2 宽度口径真正接线）。
# 现改为：**只验证、不追加**——接线缺失时响亮失败，不静默再写。
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
BK=experiments/lab/_logs/stage_B_backup
mkdir -p "$BK"

# ---------- B1: 校验宽度口径注入与 S-2 接线真实存在 ----------
$PY /dev/stdin <<'PYEOF' || { echo 'FAIL: B1 接线校验未通过'; exit 1; }
p = 'scripts/run_dividend_backtest.py'
s = open(p, encoding='utf-8').read()

checks = {
    'record 携带 breadth_gate_context（恰好 1 次）':
        s.count('"breadth_gate_context":') == 1,
    'ctx 顶层上报 use_breadth_timing':
        'ctx["use_breadth_timing"] = True' in s,
    'ctx 顶层上报 breadth_series':
        'ctx["breadth_series"] = bser' in s,
    '宽度冰点宽限日已计算':
        'ctx["breadth_timing_grace_dates"]' in s,
    '无重复补丁块残留（gate_ctx_breadth 仅 1 处赋值）':
        s.count('gate_ctx_breadth = {') == 1,
}
failed = [k for k, ok in checks.items() if not ok]
if failed:
    print('B1 校验失败项:', '; '.join(failed))
    raise SystemExit(1)

r = open('scripts/gates/runner.py', encoding='utf-8').read()
runner_checks = {
    'runner 识别 use_breadth_timing': 'ctx.get("use_breadth_timing")' in r,
    'runner 传入 breadth_series': 's2_ctx["breadth_series"]' in r,
    'runner 传入宽度宽限日': 's2_ctx["timing_grace_dates"] = ctx["breadth_timing_grace_dates"]' in r,
    'MA200 口径也传宽限日': 's2_ctx["timing_grace_dates"] = ctx["timing_grace_dates"]' in r,
}
failed = [k for k, ok in runner_checks.items() if not ok]
if failed:
    print('runner 校验失败项:', '; '.join(failed))
    raise SystemExit(1)
print('B1 接线校验通过（产出侧 ctx + runner 双侧接好）')
PYEOF
$PY -m py_compile scripts/run_dividend_backtest.py scripts/gates/runner.py || exit 1
echo 'B1 语法校验通过'

# ---------- B2: 宽度口径测试用例（不存在则创建，存在则直接跑） ----------
if [ ! -f tests/test_breadth_gate_context.py ]; then
  echo 'FAIL: tests/test_breadth_gate_context.py 缺失（应已入库）'
  exit 1
fi
$PY -m pytest tests/test_breadth_gate_context.py -q || exit 1
echo 'B2 宽度口径测试用例通过'

echo '阶段B完成: 宽度口径 ctx 接线幂等校验通过'
