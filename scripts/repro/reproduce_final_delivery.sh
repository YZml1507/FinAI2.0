#!/usr/bin/env bash
# 最终交付复现（薄封装）：锚点 + e20 两臂复跑并比对冻结期望。
set -euo pipefail
cd "$(dirname "$0")/../.."
exec .venv/bin/python scripts/repro/reproduce_final_delivery.py "$@"
