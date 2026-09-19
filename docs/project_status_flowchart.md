# FinAI2.0 · 项目状态流程图（2026-09-19 14:4x 更新）

> 渲染器：支持 mermaid 的 Markdown 查看器（VS Code / Obsidian / GitHub）
> 交互版见：`docs/project_status_flowchart.html`
> 事实源：`docs/TASK_TRACKER.md`（当前接续，跨窗口唯一事实源）+ `docs/ALPHA3_PLAYBOOK.md`（已裁决结论与纪律）+ `experiments/lab/leaderboard.jsonl`（实验榜单）
> ⚠️ 口径纪律：门禁数量与单测基线一律以单一事实源为准（`scripts/gates/constants.py` + `gate_master_audit.py` 注册表），⛔ 文档不硬编码；历史实测快照行以 `gate-doc-ignore` 标注
> ⚠️ 环境：本机即服务器（4C/7.8G/39G，CPU 是瓶颈）；SSH 单条 <30s、长任务 `setsid nohup`；Python=`.venv/bin/python`

---

## 一、基建总览

```mermaid
flowchart TD
  subgraph REPO["三仓（Private）"]
    F["代码仓 FinAI2.0<br/>HEAD / 领先提交数 以 git 为准（⛔ 不硬编码）<br/>⛔ 领先 origin 数十提交未 push<br/>origin=github.com/YZml1507/FinAI2.0"]
    R["计划仓 research-finai<br/>spec / roadmap / tasks 权威源"]
    H["调研仓 research-finai-latest<br/>Hermes 交付 .cluster/rd_*<br/>R6~R10 报告已回"]
  end
  F -->|镜像同步| R
  H -->|调研报告| F
  ENV["本机 = 服务器（CPU 瓶颈）<br/>.venv/bin/python · .env 双代理凭证<br/>stock_basic_cache.parquet 权威宇宙缓存"]
  DATA["数据底座<br/>红利池 487 日线 → 2026-09-16 · 全 A 宽度 5215<br/>bar 宇宙 5473 = 在市 5220 + 退市 253（2015-2024）<br/>财务 PIT→2026 中报 · forecast/veto/landmine/pead sidecar<br/>GC001 利率 · H20955/H00922 全收益 · namechange PIT<br/>daily_basic_alla 2431 日分片 · dividend_events_alla ✅<br/>fina_alla 补采在跑（只写新目录，不触哈希域）"]
  FP["复现指纹（现行）<br/>universe_hash ac50e9da<br/>data_hash c7630169（E12 isST 重建后）<br/>权威产物 20260907-150402"]
  F --> ENV --> DATA --> FP
  style F fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style R fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style H fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style ENV fill:#f1f5f9,stroke:#94a3b8,color:#0f172a
  style DATA fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style FP fill:#fef3c7,stroke:#f59e0b,color:#0f172a
```

---

## 二、策略谱系 · 基线矩阵

```mermaid
flowchart LR
  MA200["MA200 基线（权威）<br/>3.76% / 21.43%"] --> V1["宽度 v1 默认<br/>−0.87% / 40.46%"]
  V1 --> GRID["24 组参数网格<br/>D/A/m/i → 封顶"]
  GRID --> CHAMP["宽度冠军 bd25a35m00i1<br/>d0.25 / a0.35 / m0 / i1<br/>5.82% / 31.28% = 进攻基线"]
  CHAMP --> E6["+e6 空仓计息<br/>GC001 序列<br/>7.64% / 28.66%"]
  CHAMP --> E7["+e7 降档出清 demote<br/>6.44% / 18.45%"]
  E6 --> E8B["e8b = 冠军 + GC001 + demote<br/>8.58% / 17.40%<br/>实验最优"]
  E7 --> E8B
  E8B --> G2{"晋级六维门禁<br/>G-2 判负：阈值尖峰非平台<br/>⛔ 不晋级 · 保留实验冠军"}
  G2 -.-> E11["e11 linear 单调斜坡<br/>6.32% / 24.40%<br/>判负（退化 2.26pp）"]
  G2 -.-> E13A["e13a 暴露等价臂<br/>7.13% / 21.44%<br/>两因素分解闭合"]
  E11 --> C3["C3 池扩容（在途）<br/>病在池子 → P1 重选"]
  E13A --> C3
  style MA200 fill:#e0e7ff,stroke:#818cf8
  style V1 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style GRID fill:#e0e7ff,stroke:#818cf8
  style CHAMP fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style E6 fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style E7 fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style E8B fill:#fef3c7,stroke:#f59e0b,color:#0f172a
  style G2 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style E11 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style E13A fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style C3 fill:#fef3c7,stroke:#f59e0b,color:#0f172a
```

### 基线矩阵（数值为实验快照；对照基准恒为进攻基线）

| 构型 | CAGR | MDD | 年化换手 | 往返 | 定位 / 裁决 |
|---|---|---|---|---|---|
| MA200 权威基线 | 3.76% | 21.43% | — | — | 产物 20260915-235155（旧指纹历史快照） |
| 宽度冠军（进攻基线） | 5.82% | 31.28% | 4.16 | 146 | 对照基准 `bd25a35m00i1` |
| e8b（实验最优） | 8.58% | 17.40% | 4.61 | 156 | G-2 判负不晋级，保留实验冠军 |
| e11 linear | 6.32% | 24.40% | 6.45 | 312 | 判负：退化 2.26pp＞1.5pp |
| e13a 暴露等价臂 | 7.13% | 21.44% | 5.71 | 308 | 归因臂：暴露 −1.45pp + 形态 −0.81pp 闭合 |
| isst 整批重跑 ×5 | 逐值同左 | 逐值同左 | 逐值同左 | 逐值同左 | 新指纹 `c7630169` 下逐值同旧＝纯语义修正 |

---

## 三、判负史与归因

```mermaid
flowchart TD
  subgraph HIST["判负台账（已裁决 · 不许重试）"]
    direction LR
    R1["宽度参数网格加密<br/>24 组封顶 · 边际耗尽"]
    R2["PEAD 路线（终局）<br/>event −0.36% / rebal-fixed 0.03%"]
    R3["排雷卖出版 L2/L3/L5<br/>E2b 3.66%"]
    R4["回场触发器<br/>e5 3.87% / 34.95% · 已 revert"]
    R5["e9 指数/ETF 层<br/>R8 核验负结论×2 → 暂停"]
    R6["P2 滞回带下压出清线<br/>6.46% / 27.44% · 判负回退"]
    R7["e11 linear 连续映射<br/>G-2a/b/c FAIL + 退化 2.26pp"]
    R8["趋势指标 / cyq_perf / 龙虎榜<br/>负 / 禁用"]
  end
  style R1 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style R2 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style R3 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style R4 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style R5 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style R6 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style R7 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style R8 fill:#fee2e2,stroke:#dc2626,color:#0f172a
```

### 机制归因链（两病分开治）

```mermaid
flowchart TD
  Q1["G-2 判负的病在「接口」<br/>仓位 = 宽度阶跃函数 ⇒ 参数曲面台阶+棱边"]
  Q1 --> Q2["C1 单调斜坡已试（e11）<br/>暴露非等价 +6.76pp × 收益 W 形冲突 ⇒ 判负"]
  Q2 --> Q3["e13a 恒等式闭合<br/>暴露代价 −1.45pp(64%) + 形态代价 −0.81pp(36%)<br/>单调斜坡形态本身有害"]
  Q3 --> Q4["方向 = 暴露中性的非单调权重<br/>⛔ 须预登记 + PBO/WFA（两桶 ~170 日有拟合风险）"]
  P1["收益缺口的病在「池子」<br/>质量叠加才是收益源<br/>现池与全 A 高股息池重叠仅 9.7%<br/>连续 3 年 ≥3% 仅 3-28 只（样本饥饿）"]
  P1 --> P2["C3 池扩容 + P1 规格<br/>两线不互相指望（池修收益 · 接口修 G-2）"]
  C2N["C2 分母漂移假设 ⇒ 证伪<br/>r=0.9905 · 宽度序列无需重算"]
  style Q1 fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style Q2 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style Q3 fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style Q4 fill:#fef3c7,stroke:#f59e0b,color:#0f172a
  style P1 fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style P2 fill:#fef3c7,stroke:#f59e0b,color:#0f172a
  style C2N fill:#f1f5f9,stroke:#94a3b8,color:#0f172a
```

---

## 四、数据去偏（去幸存者偏差 + 语义修正）

```mermaid
flowchart TD
  SB["R9-A 三臂影子检验（等权年度口径）<br/>全 A +3.47%/年 · 限存活 +5.92%/年 · 限现池 +1.65%/年"]
  SB --> S1["幸存者偏差 −2.45pp/年<br/>现池构造成本 −4.27pp/年<br/>现池退市票 0 只（市场 337）"]
  S1 --> F1["① 退市票 bar 补采 ✅<br/>目标 255 → 有效 253（2 只源空）<br/>delisted_bars 隔离目录"]
  S1 --> F2["② isST 重建 E12 ✅<br/>namechange PIT 名称史 → ±5% 档回写<br/>breadth 805 + dividend 130 + 主板自纠 155 文件<br/>探针 A1-A5 全 PASS · 幂等 0 变更"]
  F2 --> F2B["整批同 SHA 重跑 5 组 ✅<br/>逐值同旧 ⇒ 纯语义修正<br/>data_hash e150ee29 → c7630169"]
  S1 --> F3["③ 极端高息尾部覆盖<br/>随 C3 池重选一并处理"]
  AQ["口径审计 ✅<br/>513100 伪分拆坏值隔离（无消费方）<br/>510880 除息口径正确<br/>映射更正：512890→H20269 · 515100→H20955"]
  TRAP["股息陷阱尾部实锤<br/>dv≥3 前瞻 1 年：ST −51.31% · 退市 −35.07% · 正常 +4.35%<br/>⇒ 高 dv 门槛须与 veto 家族成对使用"]
  style SB fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style S1 fill:#fee2e2,stroke:#dc2626,color:#0f172a
  style F1 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style F2 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style F2B fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style F3 fill:#fef3c7,stroke:#f59e0b,color:#0f172a
  style AQ fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style TRAP fill:#fee2e2,stroke:#dc2626,color:#0f172a
```

> 影子口径边界（引用须带）：等权/年度/前 8 只、未扣成本、退市票按最后可用价平仓、
> dv 为年末快照非日频 PIT——不代表可交易收益。

---

## 五、C3 池扩容（主线在途）

```mermaid
flowchart LR
  WHY["动因：收益缺口病在池子<br/>现池非高股息逻辑构建<br/>重叠仅 9.7%"] --> SPEC["P1 七规则（阈值待本仓复核冻结）<br/>dv≥3 · ROE>0 · 净利同比>0<br/>资产负债率<80% · 支付率∈(0,1)<br/>近一年跌>50% 剔除 · 行业≤20%"]
  SPEC --> SNAP["逐年首交易日快照<br/>覆盖 2015 · 杜绝年末回选前视"]
  SNAP --> RDY{"数据就绪度"}
  RDY --> DA["daily_basic_alla<br/>2431 日分片 ✅"]
  RDY --> DB["dividend_events_alla<br/>5471/5473 ✅（12 源空）"]
  RDY --> DC["fina_alla<br/>补采在跑"]
  RDY --> DD["delisted_bars 253 ✅<br/>namechange PIT ✅"]
  DA --> BL["c3_pool_rebuild.py<br/>逐规则 attrition manifest"]
  DB --> BL
  DC --> BL
  DD --> BL
  BL --> FR["attrition 复核 → 冻结 P1 阈值"]
  FR --> UNI["年度池 universe_provider + 数据面"]
  UNI --> RUN["跑 C3：e8b 同参（d0.25/a0.35/m0/i1+demote+GC001）<br/>变量只有「池」 · 对照锚 = isst-e8b"]
  RUN --> JDG{"判据（预登记固定·不许放宽）<br/>Δ≥+1.5pp 且 MDD 不劣化>3pp → 进门禁<br/>∈±1.5pp → 登记弃 · ≤−1.5pp → 判负<br/>守门：换手≤8 · 池年均≥30"}
  style WHY fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style SPEC fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style SNAP fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style RDY fill:#fef3c7,stroke:#f59e0b,color:#0f172a
  style DA fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style DB fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style DC fill:#fef3c7,stroke:#f59e0b,color:#0f172a
  style DD fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style BL fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style FR fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style UNI fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style RUN fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style JDG fill:#fef3c7,stroke:#f59e0b,color:#0f172a
```

> 预登记：`docs/C3_POOL_RESELECT_PREREG.md`（判据已预固定）；宇宙 = bar 5473（在市 5220 + 退市 253），PIT 成员资格按在市日判定；行业标签为当前快照（无 PIT 源，已登记简化）。

---

## 六、门禁体系（从「空转装饰」到「真拦」，Alpha 时代沿用）

```mermaid
flowchart TD
  G["门禁体系<br/>STATIC 12 + RUN_EVIDENCE 17<br/>数量以 gate_master_audit 注册表为单一事实源（⛔ 不硬编码）"]
  G --> L1["回测/测量期<br/>report-only 不阻断"]
  G --> L2["推送期 pre-push<br/>静态可判门禁阻断<br/>G-MDD-1 降为 WARN（展示不阻断）"]
  G --> L3["准入期 --acceptance ⛔ BLOCKER<br/>验签 + schema + status + 指标健全 + 阈值"]
  G --> L4["定时审计 --scheduled<br/>cron 每日 02:17 UTC"]
  L3 --> K1["G-DOC-1 文档↔产物一致<br/>truth = 20260907-150402<br/>行内豁免须非空理由+自证历史快照"]
  L3 --> K2["G-REF-1 引用路径存在<br/>白名单仅运行期生成物"]
  L3 --> K3["G-REPRO-1 复现门禁<br/>数据语义变更 ⇒ 整批同 SHA 重跑<br/>+ 基线变更登记（isST 案例已执行）"]
  style G fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style L1 fill:#f1f5f9,stroke:#94a3b8,color:#0f172a
  style L2 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style L3 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style L4 fill:#dcfce7,stroke:#16a34a,color:#0f172a
  style K1 fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style K2 fill:#dbeafe,stroke:#2563eb,color:#0f172a
  style K3 fill:#dbeafe,stroke:#2563eb,color:#0f172a
```

### 晋级六维门禁 · e8b 判例（判据升级 C7 在案）

| 维 | 内容 | e8b 结果 |
|---|---|---|
| G-1 留出验证 | holdout 2022-2024 | ✅ 8.35% / 12.89%（零退化） |
| G-2 阈值稳健 | ±10% 扰动平台区 | ❌ 尖峰非平台（10 组 7 塌）⇒ **判负** |
| G-3 成本/本金 | 费率×2 / 10 万本金 | ✅ 7.79% / 8.46% |
| G-5 行为探针 | 执行一致性 | ✅ e7-demote-audit 18 跨界日 0 违规 |
| G-6 复现 | 指纹 + 重锚 | ✅ b-repro 逐值一致 |
| **裁决** | — | ⛔ 不晋级，保留实验冠军身份；瓶颈在阈值稳健性非实现 |

> C7 判据分层（预登记值）：G-2a 平台宽度≥50% · G-2b 退化单调性 · G-2c Lipschitz L×10%≤6.3pp · G-2d PBO / G-2e DSR · G-2g 换手≤8；信号层用 PBO/DSR、构型层用平台宽度，⛔ 两层不许互相替换。

---

## 七、测试基线演进

```mermaid
flowchart LR
  T0["治理时代（历史快照）<br/>19 → 162 → … → 629 passed"] --> T1["Alpha 时代<br/>1067 → 1073 → 1075 → 1078 → 1079"]
  T1 --> T2["C1 连续权重 +13<br/>当前基线以单一事实源为准<br/>constants.TEST_BASELINE_PASSED<br/>⛔ 文档不硬编码"]
  style T0 fill:#e0e7ff,stroke:#818cf8
  style T1 fill:#e0e7ff,stroke:#818cf8
  style T2 fill:#dcfce7,stroke:#16a34a,color:#0f172a
```

> 守卫资产：E-1 五必挂真实探针 · `gate-doc-ignore` 按文件分组语义守卫（`tests/test_gate_consistency.py` allowed_counts）· `gate-doc-void` 清单 ≤8 · 排除目录 `docs/audit/` 整片免检可见计数 · dated 日志行单独计数 · C1 权重 13 测 · E12 isST 探针 A1-A5。

---

## 八、当前队列（按序）

| 序 | 事项 | 状态 |
|---|---|---|
| 1 | `fina_alla` 财务 PIT 补采（setsid 后台）→ 完成即跑 `scripts/lab/c3_pool_rebuild.py` → attrition 复核 → 冻结 P1 → 年度池 universe_provider + 数据面 → 跑 C3 | 🔄 在跑 |
| 2 | flowchart `.md` + `.html` 重改（本节即产物） | 🔄 本窗口 |
| 3 | R9/R10 已交付 → 按 PLAYBOOK 口径审计（时序/无前视/成本/多重检验）→ 裁决可预登记方向 → 更新 `docs/EXPANSION_MEMO.md` | ⏳ 待办 |
| 4 | 候选方向：暴露中性非单调权重（须预登记 + PBO/WFA）/ C4 SMA5 联用 / C8 集成 / C7 G-2 判据完整版 | ⏳ 待裁决 |

---

## 九、⚠️ 已知缺口（如实登记，未解决）

| # | 缺口 | 性质 | 处置 |
|---|---|---|---|
| 1 | `G-MDD-1` FAIL（权威产物 43.08%＞35%） | **真实策略缺陷** | 策略层改进后自然转绿 |
| 2 | 退市票 bar 已采 253 只，`feed`/`alive_universe` 未消费新宇宙 | 数据面待接 | C3 数据面一并接入 |
| 3 | 行业标签无 PIT 源（当前快照） | 已登记简化 | C3 预登记 §二 |
| 4 | e8b 阈值面无平台区（±10% 结构性敏感） | 阈值类过拟合特征 | 非单调权重方向（预登记+PBO/WFA） |
| 5 | 飞书告警为 stub（`ops/feishu_alert.py` 未实现） | 如实披露 | 待用户拍板 |
| 6 | 领先 origin 数十提交未 push | 单点风险 | 待用户拍板 |
| 7 | R9/R10 为外部研究证据，尚非本仓终局裁决 | 待审计 | 队列第 3 项 |

---

## 十、历史归档 · 治理时代压缩（仅追溯，不作决策依据）

> **治理时代（2026-08 ~ 09-10）压缩归档**：Phase 0–3.5 清零（数据层 → 事件引擎 → 策略层 → 红利切换）、T312 数据硬伤根治与防伪审计、M1–M4′ 治理重做（门禁空转揭穿 → 真拦）、H5/H5b/H5c 死带病因（路径 A 影子复算）、Phase 4 暂停、MA200 → 宽度择时换轨。
> **追溯指针**：`CLAUDE.md` 工程纪事 · `docs/HANDOFF_20260915.md`（⛔ 已过期，仅追溯）· `docs/audit/*` · TASK_TRACKER 历史节。
> **权威产物锚**：20260907-150402 — CAGR −3.20% / MDD 43.08% / 年化换手 201.14% / 胜率 28.21%（唯一事实源 `experiments/runs/*.json`；仓内任何 md 的结论性数字一律不可引用）。
> ⛔ 本节及全文的历史数字均为快照、不作决策依据；当前事实以 TASK_TRACKER「当前接续」为准。

---

## 更新历史

| 日期 | 内容 |
|---|---|
| 2026-09-10 | 治理时代版本（全仓审计推翻重建：门禁空转揭穿 + M1–M4′ 收口） |
| 2026-09-19 | Alpha 时代重构：谱系基线矩阵 + 判负史归因 + 数据去偏 + C3 在途；治理时代压缩归档至 §十 |
