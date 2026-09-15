#!/usr/bin/env bash
# 阶段C：清尾降噪（确定性补丁，带语法校验与自动回滚）
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
BK=experiments/lab/_logs/stage_C_backup
mkdir -p "$BK"
cp scripts/lab/sentinel.sh "$BK/sentinel.sh.bak"
cp scripts/run_dividend_backtest.py "$BK/run_dividend_backtest.py.bak"

rollback() {
  cp "$BK/sentinel.sh.bak" scripts/lab/sentinel.sh
  cp "$BK/run_dividend_backtest.py.bak" scripts/run_dividend_backtest.py
  echo 'FAIL: 补丁校验未通过，已自动回滚'
  exit 1
}

$PY /dev/stdin <<'PYEOF' || rollback
p = 'scripts/lab/sentinel.sh'
s = open(p, encoding='utf-8').read()
old1 = '" 2>/dev/null || echo "$line")'
new1 = '" 2>/dev/null || printf \'%s\' "$line" | head -c 200)'
assert s.count(old1) == 1, 'anchor1 missing or not unique'
s = s.replace(old1, new1)
old3 = "  ' '.join(f'{k}={v}' for k,v in ov.items()),"
new3 = "  ' '.join(f'{k}={str(v)[:40]}' for k,v in ov.items() if k != 'breadth_series'),"
assert s.count(old3) == 1, 'anchor3 missing or not unique'
s = s.replace(old3, new3)
open(p, 'w', encoding='utf-8').write(s)
print('C1 sentinel 补丁写入完成')
PYEOF
bash -n scripts/lab/sentinel.sh || rollback
echo 'C1 语法校验通过'

$PY /dev/stdin <<'PYEOF' || rollback
p = 'scripts/run_dividend_backtest.py'
s = open(p, encoding='utf-8').read()
anchor = 'rel = rel.split(" -> ", 1)[1]'
assert s.count(anchor) == 1, 'anchor missing or not unique'
patch = '\n            # dirty 哈希只覆盖代码路径（榜单与文档追加不污染指纹，C2 修复）\n            if not rel.startswith(("strategy/", "scripts/", "backtest/", "tests/")):\n                continue'
s = s.replace(anchor, anchor + patch)
open(p, 'w', encoding='utf-8').write(s)
print('C2 指纹口径补丁写入完成')
PYEOF
$PY -m py_compile scripts/run_dividend_backtest.py || rollback
echo 'C2 语法校验通过'

HASH_BEFORE=$($PY -c "import sys; sys.path.insert(0,'.'); from scripts.run_dividend_backtest import _git_code_hash; print(_git_code_hash())" 2>/dev/null)
echo "当前指纹: $HASH_BEFORE"
touch docs/.fp_probe_tmp.md
HASH_DOC=$($PY -c "import sys; sys.path.insert(0,'.'); from scripts.run_dividend_backtest import _git_code_hash; print(_git_code_hash())" 2>/dev/null)
rm -f docs/.fp_probe_tmp.md
echo "新增文档dirty后指纹: $HASH_DOC"
[ "$HASH_BEFORE" = "$HASH_DOC" ] && echo '指纹验证: 文档 dirty 不污染指纹 ✓' || echo '指纹验证: 存在差异，记录待人工复核'

echo '阶段C完成: 哨兵兜底截断 + 剔除巨型宽度字段 + 指纹 dirty 口径收窄（原文件备份于 _logs/stage_C_backup）'
