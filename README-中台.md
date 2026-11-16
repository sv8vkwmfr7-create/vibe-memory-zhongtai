# VibeMemory 四中台（数据 / 评测 / 测试 / 监控）

> 针对 `Desktop\VibeMemory`（vibe-memory v0.3.0）构建的轻量中台体系：**统一数据底座 + 三大能力中台（评测 / 测试 / 监控）+ 五张可视化大屏**，全部在本文件夹内自包含。
> 构建日期：2026-11-08（同日完成：公开数据集入库、大规模生成评测集、双协议执行器、测试与监控中台整合）｜ 运行环境：`Desktop\VibeMemory\.venv`（Python 3.12）

---

## 30 秒上手

**看结果**：双击打开 [`pages/index.html`](pages/index.html)（四中台门户）→
[数据](pages/data-platform.html) / [评测](pages/eval-platform.html) / [测试](pages/test-platform.html) / [监控](pages/ops-platform.html)。
页面为纯静态、数据内嵌、零外部依赖，离线双击即可打开。

**复跑全流程**：

```bash
cd "C:\Users\ASYS\Desktop\VibeMemory-中台"
PY="C:\Users\ASYS\Desktop\VibeMemory\.venv\Scripts\python.exe"

# ① 测试中台：pytest 全量回归（含覆盖率，约 7 分钟）或 quick 冒烟
cd 测试中台
"$PY" run_tests.py --scope full --timeout 3000
cd ..

# ② 监控中台：确定性负载运行时采集（约 15 秒）
cd 监控中台 && "$PY" collect_runtime_metrics.py && cd ..

# ③ 评测中台：内建+生成评测集全量（pooled 共享记忆库）
cd 评测中台
"$PY" run_eval.py --all
#    公开基准（独立协议，逐文件/逐题隔离，单跑不进 --all）
"$PY" run_eval.py --evalset "benchmarks/locomo_c01.jsonl"          # LoCoMo 第1段（其余 c02..c10 同理）
"$PY" run_eval.py --evalset "benchmarks/longmemeval_s_subset30.jsonl"
"$PY" gate.py
cd ..

# ④ 数据中台：采集 → 加工 → 质量检查
cd 数据中台 && "$PY" collect.py && "$PY" build_assets.py && "$PY" quality.py && cd ..

# ⑤ 重新生成可视化页面
"$PY" scripts/build_pages.py
```

---

## 它解决什么问题

| 痛点 | 中台答案 |
| --- | --- |
| 实验/覆盖率/状态/评测结果散落在仓库各处 | 数据中台：13 类可信来源登记 + SHA-256 指纹 + 变更检测 + 溯源档案 |
| 指标口径不统一，无法回答"现在质量怎么样" | 三层资产 + 质量六性（当前综合分 **0.995**）+ 资产目录（含血缘） |
| 记忆召回质量没有固定评测资产与回归门禁 | **25 个评测集 / 2,568 用例** + 5 项指标 + 双协议执行器 + baseline 门禁 |
| 公开基准接入成本高 | datasets/ 已入库 LoCoMo、LongMemEval-S、PersonaMem（指纹与仓库实验记录交叉验证一致）+ 一键转换器 |
| 结果看不见 | pages/ 三张静态大屏（离线可开） |

---

## 四中台总览（v2 整合）

| 中台 | 回答的问题 | 首轮实测 | 门禁 |
| --- | --- | --- | --- |
| 🗄️ [数据中台](pages/data-platform.html)（底座） | 有哪些可信数据、质量如何 | 15 类来源 15/15 通过 · 质量六性 0.996 · 13 资产 | 质量分 ≥0.85 |
| 🎯 [评测中台](pages/eval-platform.html) | 记忆**检索质量** | 25 套 / 2,568 用例 · LoCoMo R@5 0.83（±2pp）· LME 子集 0.80 | 指标阈值 + 环境可比性 |
| 🧪 [测试中台](pages/test-platform.html) | 代码**回归健康** | pytest 全量 **1,360/1,360 通过**（397s）· 行覆盖 87.9% / 分支 80.1% | 通过率相对门禁（2pp） |
| 📈 [监控中台](pages/ops-platform.html) | **运行时健康** | 1000 atoms 负载：store p95 12.4ms · recall p95 5.4ms · 命中率 91.9% · 零命中率 84.5% | 观测层（对比历史） |

三座能力中台的运行存档全部**回流数据中台**（统一目录、溯源、质量六性），五张大屏统一导航与设计语言。

---

## 评测集体系（25 套 / 2,568 用例）

### 1. 内建冒烟评测集（7 套 / 41 用例，evalsets/）
单跳召回 ×8 ｜ 时序 ×6 ｜ 知识更新 ×5 ｜ 多跳 ×6 ｜ 多跳·显式建边对照 ×6 ｜ 偏好 ×5 ｜ 拒答 ×5

### 2. 生成大样本评测集（7 套 / 970 用例，evalsets/*_large.jsonl）
由 [`generate_evalsets.py`](评测中台/generate_evalsets.py)（v2）批量生成，固定种子可复现：
- **唯一组合设计**：每题 (实体,属性,环境/项目/月份/人/场景) 组合全池唯一，查询文本不撞车（v1 的同查询碰撞已修复：conflict 0.15→**1.00**、preference 0.39→**0.98**、basic 0.74→**0.97**、en 0.65→**1.00**）
- **近邻干扰**：同实体不同属性 / 同属性不同实体干扰会话按概率注入
- **多跳带显式因果边 + causal_bridge**：大池子下考图链路（当前 0.24，暴露大库二跳桥接短板）
- 分布：单跳 ×240 ｜ 时序 ×140 ｜ 更新 ×140 ｜ 多跳 ×180 ｜ 偏好 ×90 ｜ 拒答 ×120 ｜ 英文 ×60

### 3. 公开基准转换评测集（11 套 / 1,557 用例，benchmarks/）
由 [`convert_benchmarks.py`](评测中台/convert_benchmarks.py) 转换，**会话级证据协议**（turn 级证据归并到会话）：

| 基准 | 文件 | 用例 | 协议 | 首轮 Recall@5 |
| --- | --- | --- | --- | --- |
| **LoCoMo**（CC BY-NC 4.0，官方 locomo10.json） | locomo_c01..c10.jsonl | 1,527（对抗题 446、证据缺失 13 未纳入） | 每段对话独立运行；时间锚点为文本日期前缀 | **0.743–0.893**（10 段） |
| **LongMemEval-S** cleaned（MIT，xiaowu0162/longmemeval-cleaned） | longmemeval_s_subset30.jsonl | 30（官方顺序前 30 题可作答题；30 道 _abs 弃答题需答案级协议，未纳入） | **逐题独立记忆库**（isolate） | **0.800** |

### 4. 原始数据集资产（datasets/，共 286 MB）
`locomo/locomo10.json`（2.8MB）、`longmemeval_s/longmemeval_s_cleaned.json`（265MB）、`personamem_v2/`（17MB），
每个数据集带 `card.json` 指纹卡（source_url / sha256 / scale / license / 溯源说明）。
**可信度交叉验证**：LoCoMo 与 LongMemEval 的 SHA-256 与 VibeMemory 仓库 `results/*.json` 实验记录中
的 `source_sha256` **完全一致**（2026-09-26 同源获取）。更多可选数据集见 [docs/数据集资源清单.md](docs/数据集资源清单.md)（26 个基准 × 能力维度矩阵，2026-11-08 检索核实）。

---

## 双记忆协议（run_eval.py）

| 协议 | 触发 | 语义 |
| --- | --- | --- |
| **pooled**（默认） | 普通用例 | 一次调用所有评测集共享一个 `:memory:` 实例（1552 stores / 1508 atoms），互为语义近邻干扰——模拟真实单 Agent 记忆库 |
| **isolated** | 用例带 `"isolate": true` | 逐题建独立实例，只注入该题会话——对齐 LoCoMo/LongMemEval 官方"每题独立记忆"评测方式 |

辅助能力：`--limit N` 抽样冒烟（note 自动标注）；`--k`/`--mode` 可调；`gate.py` 门禁带**环境可比性警告**
（记忆库规模差 3 倍以上或协议不同时提示"指标不可直接对比"）。

## 指标与门禁

Recall@K ｜ MRR ｜ nDCG@K ｜ KeywordHit@K ｜ AbstentionPrecision（拒答代理）。
门禁：绝对阈值（基线 -0.05 容忍带）+ 环境可比性检查（池子规模差 3 倍以上或协议不同时告警"不可直接对比"）；
基线 25 套评测集已固化（`baseline.json`），当前总体门禁 **PASS**——注意：基线固化自最新一轮运行，
固化时刻的 PASS 是定义使然，门禁的约束力在**下一次**运行（指标跌破阈值即 FAIL）时才体现。

## 关键发现（评测平台产出的真实结论）

1. **多跳召回的双短板**：小库（42 atoms）下默认配置 2/6 命中（零词汇重叠 + 中文短句跨会话自动建边为 0），显式建边后 6/6；**大库（1508 atoms）下即使带显式边，二跳桥接 Recall@5 也只有 ~0.24**——图检索收益随库规模稀释，是明确的改进方向（检索预算、边权重、PPR 种子选择）。
2. **同查询碰撞**：pooled 协议下评测集设计必须保证"查询文本全池唯一"，否则指标度量的是碰撞而非检索能力（v1→v2 修复实证）。
3. **LoCoMo 会话级证据召回 0.74–0.89**：纯词法（TF-IDF/precision）在真实长对话上的合理水平，与官方 turn 级协议的结论互补。

## 方法论对齐与诚实声明

- 数据中台对齐信通院《数据中台实践指南》与 OneData（docs/数据中台调研.md，19 条来源）；质量维度口径：完整性/唯一性/一致性/准确性/及时性对应 GB/T 36344-2018 框架（其第六项为"规范性"），"有效性"取自 DAMA 六核心维度。
- 评测中台对齐 OpenCompass 式"评测集→运行→榜单"抽象与 LoCoMo/LongMemEval 能力维度（docs/评测中台调研.md 66 条来源、docs/数据集资源清单.md 26 基准核实）。
- **边界**：内建/生成评测集为规则判分（会话 ID+关键词+陷阱词）；公开基准转换为会话级证据协议，不替代官方 turn 级/答案级（LLM-judge）结论；LongMemEval 仅 30 题子集；PersonaMem 已入库未接转换器（适配逻辑见仓库 experiments/personamem_*）；全部指标基于 TF-IDF/precision 离线可复现环境。
