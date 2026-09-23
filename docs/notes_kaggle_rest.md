# Kaggle API 通道备忘（2026-09-23 实证）

- 凭证：org secret `KAGGLE_KEY`（legacy 32-hex key）+ username `mengxinyz`
- `kaggle` CLI 1.7.4.5 的 kernels 子命令走 api.kaggle.com 新 RPC 端点
  → 对 legacy Basic 凭证一律 401/403，**不可用**。
- datasets 子命令（list/download）仍走 www.kaggle.com 老 REST → 可用。
- kernels 全链路绕行 = 直接调老 REST + Basic auth：
  - push: `POST https://www.kaggle.com/api/v1/kernels/push`
    JSON body: slug(=owner/slug), newTitle, text(=脚本全文), language,
    kernelType, isPrivate, enableGpu, enableInternet,
    datasetDataSources[], competitionDataSources[], kernelDataSources[],
    categoryIds[]
  - status: `GET /api/v1/kernels/status?userName=X&kernelSlug=Y`
    → {"status":"queued|running|complete|error","failureMessage":...}
  - output: `GET /api/v1/kernels/output?userName=X&kernelSlug=Y`
  - datasets create/download 同理走 /api/v1/datasets/*
- phone verification 未实测确认，但 legacy REST 全通说明该账号
  已有 kernels 权限（此前 401 全是 RPC 端点不认 legacy 凭证所致，
  与账号验证等级无关）。
