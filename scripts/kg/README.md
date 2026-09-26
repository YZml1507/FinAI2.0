# Kaggle GPU sweep pipeline (proven 2026-09-26, ~65x vs local CPU)

e83-style walk-forward arm at V9_PARAMS: 18.4min on Kaggle T4 vs ~19h local @3.7 cores.

## 使用
1. 数据打包进 Kaggle dataset `mengxinyz/finai-e108-xlab`（Xlab4/Xlab_2025/信号 parquets）
2. `kaggle datasets version -p <dir> -m "..."` 上传更新
3. kernel.py 顶部 `ARMS = {arm: (file, col)}` 定义臂集
4. `env -u KAGGLE_USERNAME -u KAGGLE_KEY kaggle kernels push`
5. `kaggle kernels status/output <slug>` 轮询收单；results.jsonl 在 output 包

## 注意
- 挂载路径前缀: `/kaggle/input/datasets/<user>/<ds>/`（2026 新布局）
- 推理有 device 不匹配 warning，无碍（inplace predict 可优化）
- T4 单臂约 18-19min；kernel 12h 上限 → 每 kernel ≤35 臂
- 环境变量 KAGGLE_USERNAME/KEY 若存在会覆盖 kaggle.json → 必须 `env -u` 前缀
