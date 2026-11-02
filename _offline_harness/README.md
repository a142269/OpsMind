# OpsMind 离线评测 harness（`_offline_harness/`）

**目标**：在一台**断网、无第三方依赖**的机器上，让 OpsMind 里**不依赖 LLM 的真实核心逻辑真正跑起来**，产出可复现、可对质的指标。

**关键约束**：离线验证时 **不 pip install、不联网、不用 Docker**，只使用 Python 标准库和仓库内依赖桩。

## 核心方法：依赖桩 + `sys.path` 前置注入

```python
sys.path.insert(0, "_offline_harness/stubs")  # anthropic / httpx / prometheus_client / chromadb / redis
sys.path.insert(0, ".")                      # 项目根
from core.intent_recognizer import IntentRecognizer   # 真实源码
```

- `stubs/anthropic.py`：`AsyncAnthropic` + `messages.create`，默认 **mode="fail"** —— 每次 `messages.create` 都抛
  `RuntimeError("offline: LLM disabled ...")`，模拟“无 key / 断网”。项目自己的 `_llm_recognize` 捕获异常 →
  `failed=True` → 走项目自带的降级分支。**这正是要测的行为**。
- 桩带计数器（`anthropic.snapshot()`），报告里的“LLM 调用 N 次 / 失败 M 次”来自计数器，不是估算。
- 其余桩（`httpx` / `prometheus_client` / `chromadb` / `redis`）只在 import 需要时兜底，全部不联网。

## 运行

```powershell
# 默认：自动读 data/eval/intent_cases.json（存在时）+ evaluator 内置 11 条用例
python _offline_harness/run.py

# 显式指定数据集，并自定义报告路径
python _offline_harness/run.py --cases data/eval/intent_cases.json \
    --json-out _offline_harness/reports/offline_metrics.json \
    --md-out  _offline_harness/reports/offline_metrics.md
```

输出：

- `reports/offline_metrics.json` —— 结构化全量指标（含逐用例明细、错分样例、治理检查证据）。
- `reports/offline_metrics.md` —— 人读报告（含“这套指标不能说明什么”一节）。
- 退出码 0 = harness 跑完，**不代表模型好**。

## 指标覆盖

| 层 | 指标 | 被测真实入口 |
|---|---|---|
| 意图 | a) pattern 单路 / b) 本地向量单路 / c) 融合（离线真实降级分支）/ c-ablation 0.85-0.15 / per-class / 混淆样例 | `IntentRecognizer._pattern_recognize / _embedding_recognize / recognize / _vote` |
| 意图 | e) 实体抽取（ticket_id / service / account / date / error_code） | `_extract_entities` |
| 意图 | f) urgency | `_urgency` |
| 路由 | g) primary Agent 准确率（端到端 / gold 意图 / 纯路由三种口径） | `AgentOrchestrator._route_decision` |
| 路由 | h) multi_agent 触发率与命中；i) 澄清触发 | `_route_decision / _needs_clarification` |
| 治理 | j) 三态熔断（假时钟 CLOSED→OPEN→HALF_OPEN→CLOSED）/ k) TTL 缓存 / l) fallback / m) Skills 匹配与 5000 字符预算 | `CircuitBreaker / MCPToolManager.call / SkillManager` |

每条指标都打印/记录 **分子 / 分母 / n**，不硬编码任何结果数字。

## 文件

```
_offline_harness/
├─ run.py                     # 一键入口
├─ stubs/                     # 依赖桩（只提供 import 契约，不实现第三方 SDK）
│  ├─ anthropic.py
│  ├─ httpx.py
│  ├─ prometheus_client.py
│  ├─ chromadb.py
│  └─ redis/ (__init__.py, asyncio.py)
├─ fixtures/
│  ├─ entity_cases.json       # harness 自建标注的实体夹具（非项目数据集）
│  └─ skills_budget_long/     # 5000 字符预算截断夹具（运行时生成）
└─ reports/                   # offline_metrics.json / offline_metrics.md
```

## 补丁清单（全部运行时、可逆，且不改项目源码）

1. `sys.path` 前置注入桩（import 解析层，不写文件）。
2. `mcp.tool_manager.time` → `SimpleNamespace(monotonic=FakeClock)`，`try/finally` 恢复：用假时钟驱动 60s TTL / 熔断恢复，**不真 sleep**。
3. `IntentRecognizer._embedding_enabled = False`（仅 c-ablation 诊断，`try/finally` 恢复）。

`core/`、`agents/`、`mcp/`、`evaluation/` 等**源码一行未改**。

## 指标边界（必读）

LLM 路被刻意置为不可用：**LLM 语义识别、Query Rewrite、Rerank、LLM-as-Judge、多 Agent Composer 都没有被测量**；
RAG / 向量库 / Redis / 知识库检索、端到端延迟、Token 成本、并发与限流均未覆盖；
数据集为人工构造（规模有限，内置 11 条还与 `_TEMPLATES` 有重叠），不能当线上泛化准确率。
详见报告第 10 节「这套指标不能说明什么」。
