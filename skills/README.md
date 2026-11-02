# OpsMind Skills 文档

OpsMind 启动时会从 `ECHOMIND_SKILLS_DIR` 读取 Skills，并在匹配用户请求时注入到对应 Agent 的 system prompt。Skills 用于维护 IT 服务台分诊、事件排障 SOP、访问权限边界和人工升级规则。

当前内置三类 Skills：

```text
skills/general_customer_service/SKILL.md  # IT 服务台：咨询、澄清、分诊和工单交接
skills/technical_support/SKILL.md         # 事件响应：故障诊断、影响评估和恢复验证
skills/access_support/SKILL.md           # 访问协同：登录恢复、权限申请和安全升级
```

目录名暂时保留以兼容现有部署路径；实际路由由 front matter 的 `agents` 字段决定。

## Skill 文件格式

推荐每个 Skill 使用独立目录，并将主文件命名为 `SKILL.md`：

```text
skills/<skill_name>/SKILL.md
```

文件顶部使用简单 front matter：

```markdown
---
name: IT 事件响应与故障诊断规范
description: 适用于 IncidentAgent 的事件定级、故障排查和升级规范
keywords: 故障,中断,报错,超时,500,日志,监控
agents: incident
enabled: true
---
```

字段说明：

- `name`：Skill 展示名称，会出现在注入给模型的 prompt 中。
- `description`：简短说明，方便 `/skills` 接口排查。
- `keywords`：触发关键词，用户消息命中后才注入；多个关键词用英文逗号或中文逗号分隔均可。
- `agents`：适用 Agent，可填 `service_desk`、`incident`、`access`、`escalation`，多个值用逗号分隔。
- `enabled`：是否启用，支持 `true/false`。

## 编写要求

- 重要规则放在文档前半部分，因为过长内容会按 prompt 预算截断。
- 一类 Skill 只描述一类职责，不混合服务台、事件和访问权限规则。
- 必须包含“角色定位”“处理流程”“升级条件”“禁止事项”等稳定章节。
- 对密码、验证码、API Key、Token 等敏感信息必须写明禁止收集或禁止公开。
- 不得虚构工单、监控、日志、IAM 或审批系统的查询与执行结果。
- 对需要人工、值班工程师、安全团队或真实业务系统处理的场景要明确写出升级条件。

## 热加载

修改 Skill 文件后，不需要重启服务，调用：

```bash
curl -X POST http://localhost:8000/skills/reload
```

查看加载结果和解析错误：

```bash
curl http://localhost:8000/skills
```
