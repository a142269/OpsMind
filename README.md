# OpsMind

OpsMind 是面向企业 IT 服务台与运维协同场景的多 Agent 系统。它把服务咨询、故障分诊、账号访问、权限申请、安全事件和人工升级串成一条可观测、可评测、可降级的处理链路，而不是单纯的聊天机器人。

## 核心能力

- 19 类 IT 服务意图识别，以及工单号、服务、账号、日期、错误码实体提取
- ServiceDesk / Incident / Access / Escalation 四类角色协同与结构化路由
- 共享 RAG、角色工具白名单、动态 Skills 注入
- Redis 工作记忆 + ChromaDB 情景记忆与用户画像
- 在线监控、路由降权、工具轨迹和 LLM-as-Judge 端到端评测

系统只通过确定性工具生成诊断计划、权限字段检查和交接摘要；未接入真实工单、监控、IAM 或审批系统时，不会伪造查询、授权或变更结果。

## 快速开始

### 1. 准备环境

- Docker 与 Docker Compose
- Anthropic API Key，或兼容 Anthropic 协议的模型服务

配置示例：

```env
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_MODEL=deepseek-chat
ANTHROPIC_API_KEY=your_key
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

至少替换 `ANTHROPIC_API_KEY`，并在生产环境修改 Redis 默认密码。`ECHOMIND_*` 环境变量与 Compose 服务名暂时保留，用于兼容现有部署，不影响 OpsMind 产品定位。

### 3. 启动服务

```bash
docker compose up -d --build
docker compose ps
```

### 4. 访问入口

- API：`http://localhost:8000`
- Swagger：`http://localhost:8000/docs`
- Health：`http://localhost:8000/health`
- Nginx：`http://localhost`

## 对话主链路

`POST /chat` 的处理流程：

```text
读取三级记忆
  -> 三路融合意图识别与实体提取
  -> 按意图检索运维知识
  -> 主辅 Agent 路由与 Skills 注入
  -> 角色工具调用和回复生成
  -> 写回记忆并记录监控、路由和工具轨迹
```

路由角色：

| Agent | 职责 |
|---|---|
| ServiceDeskAgent | 服务目录、请求澄清、工单分诊与变更信息整理 |
| IncidentAgent | 服务中断、性能下降、应用报错与故障诊断 |
| AccessAgent | 登录认证、权限申请、最小权限检查与安全事件协同 |
| EscalationAgent | 重大事件和明确人工诉求的结构化交接 |

## 主要接口

- 对话：`POST /chat`
- 检索：`POST /search`
- 知识导入：`POST /knowledge/add`、`POST /knowledge/upload`
- 知识统计：`GET /knowledge/stats`
- Skills：`GET /skills`、`POST /skills/reload`
- 监控：`GET /monitor`
- 评测：`POST /eval/run`

## 项目结构

```text
api/main.py                   FastAPI 入口
agents/agent_orchestrator.py  多 Agent 路由与协作
agents/tools.py               服务台、事件、访问权限与升级工具
core/intent_recognizer.py     三路融合意图识别
core/skill_loader.py          动态 Skills 加载
memory/conversation_memory.py Redis + ChromaDB 三级记忆
mcp/tool_manager.py           工具缓存、熔断、查询改写与重排
mcp/knowledge_base.py         ChromaDB 运维知识库
monitor/performance_monitor.py 在线监控
evaluation/evaluator.py       意图与端到端质量评测
skills/                       服务台、事件与访问权限 SOP
data/eval/                    OpsMind 评测数据
data/demo_docs/               演示知识文档
EchoMindFrontend/             Vue 3 前端调试台
logs/                         三轮真实在线评测与优化对比报告
```

## 验证

```bash
py -m pytest tests -q
py -m compileall api agents core evaluation memory mcp monitor
```

在线模型评测需要有效 API Key 和运行中的依赖服务。当前已使用相同模型与数据集完成三轮真实在线评测，最终结果为：综合通过率 **89.66%（26/29）**、意图准确率 **91.73%（122/133）**、对话通过率 **89.29%（25/28）**，Judge 降级率为 0；后端回归测试 **17/17 通过**。三轮原因分析与指标变化见 `logs/optimization_comparison_20261102.md`，各轮 Markdown 报告和最终轮原始 JSON 保留在 `logs/`。

上述结果证明的是当前自建数据集上的回归改进，不代表独立未见数据上的泛化能力或生产 SLA。

## 文档

- [文档中心](wiki/文档中心.md)
- [完整使用指南](wiki/完整使用指南.md)
- [三轮优化对比报告](logs/optimization_comparison_20261102.md)
