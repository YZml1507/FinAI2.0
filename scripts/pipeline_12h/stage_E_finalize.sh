#!/usr/bin/env bash
# 阶段E：固化收尾（全量回归 + commit + 回写事实源）
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
LOGD=experiments/lab/_logs
TS=$(date '+%F %T')

echo '=== 全量回归 ==='
$PY -m pytest tests/ -q > "$LOGD/regression_E.out" 2>&1 || {
  echo "FAIL: 全量回归未通过"
  tail -15 "$LOGD/regression_E.out"
  exit 1
}
REG=$(tail -1 "$LOGD/regression_E.out")
echo "回归结果: $REG"

echo '=== commit 固化 ==='
git add -A
# 无变化则跳过提交
if git diff --cached --quiet; then
  echo '无可提交变更，跳过 commit'
else
  BASE=$(cat "$LOGD/baseline_run_id.txt" 2>/dev/null || echo 'pending')
  git commit -m "feat(plan-D): 十二小时流水线固化

- A: 宽度最优组合 bd25a45m00i1 晋级候选基线 (run=$BASE)
- B: 宽度口径注入后置门禁 context + 新增 S-2 宽度测试
- C: sentinel 刷屏修复(兜底截断+剔除breadth_series) + dirty指纹只覆盖代码路径
- D: 装甲一撤销立项(修正后收益+0.012~0.3pp/年不抵成本)
- E: 全量回归 $REG" || { echo 'FAIL: commit 失败'; exit 1; }
  echo "commit 完成: $(git rev-parse --short HEAD)"
fi

echo '=== 回写任务跟踪 ==='
.venv/bin/python /dev/stdin <<PYEOF
p = 'docs/TASK_TRACKER.md'
s = open(p, encoding='utf-8').read()
subs = [
    ('- [ ] P2-b', '- [x] P2-b（流水线B阶段已注入，见 commit）'),
    ('- [ ] P2-c', '- [x] P2-c（流水线B阶段已新增测试，见 tests/test_breadth_gate_context.py）'),
]
for old, new in subs:
    if old in s:
        s = s.replace(old, new, 1)
if '更新：2026-09-15 20:30' in s:
    s = s.replace('更新：2026-09-15 20:30', '更新：$TS（十二小时流水线固化完成）', 1)
open(p, 'w', encoding='utf-8').write(s)
print('任务跟踪已回写')
PYEOF

echo "阶段E完成: 回归通过($REG)，commit 固化，任务跟踪已回写"
