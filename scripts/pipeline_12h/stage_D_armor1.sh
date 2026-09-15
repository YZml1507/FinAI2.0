#!/usr/bin/env bash
# 阶段D：装甲一（除权前15天禁建仓过滤）去留评估（确定性裁决）
set -u
cd "$(dirname "$0")/../.."
DOC=docs/ARMOR1_EXDIV_FILTER_DESIGN.md
TS=$(date '+%F %T')

[ -f "$DOC" ] || { echo 'FAIL: 装甲一设计文档不存在'; exit 1; }

cat >> "$DOC" <<EOF

---

## 去留评估结论（$TS · 阶段D 流水线裁决）

**结论：撤销立项（不实施）。**

理由：
1. Hermes 自我修正后的真实收益区间为 +0.012pp ~ +0.3pp/年（原 +3pp 系误估），
   收益上限不足以覆盖『候选过滤 + 15 天前瞻窗口』的实施、回归与长期维护成本。
2. 修正后立项动机为『S-4 事前化的确定性收益』，本质是『少亏钱』而非『多赚钱』，
   与方案 D 主线宽度择时（MDD 21.43% / CAGR +3.76%）相比边际价值过低。
3. 若未来现金分红占比或红利税政策发生实质变化，可凭本文档与 probe_exdiv_window.py 重新评估。

处置：P7 从主线任务清单关闭，设计文档与调研工具保留备查。
EOF
echo '装甲一撤销结论已写入设计文档'

.venv/bin/python /dev/stdin <<'PYEOF'
p = 'docs/TASK_TRACKER.md'
s = open(p, encoding='utf-8').read()
old = '- [ ] P7 装甲一'
new = '- [x] P7 装甲一（已撤销立项：修正后收益 +0.012pp~+0.3pp/年 不抵实施成本，详见设计文档尾部评估）'
if old in s:
    s = s.replace(old, new, 1)
    open(p, 'w', encoding='utf-8').write(s)
    print('任务跟踪 P7 已关闭')
else:
    print('P7 行格式不匹配，跳过自动勾选')
PYEOF

echo '阶段D完成: 装甲一撤销立项，结论已落文档并同步任务跟踪'
