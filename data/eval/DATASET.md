# OpsMind 离线评测数据集（intent / dialog）

本目录存放 OpsMind 企业 IT 服务台的**可复现意图与对话评测集**，用于评估意图识别、多 Agent 路由与回复语义。场景覆盖服务状态、工单、故障诊断、账号访问、权限申请、安全事件、变更发布与回滚。

| 文件 | 内容 | 规模 |
| --- | --- | --- |
| `intent_cases.json` | 单句意图用例 | 133 条，覆盖 19 类意图 |
| `dialog_cases.json` | 短对话用例 | 14 组，28 轮 |
| `_validate.py` | 离线自检脚本 | 以脚本当次输出为准 |
| `DATASET.md` | 本文件：标注规范 + 统计 + 局限 | — |

数据集**不含任何真实用户数据或真实企业接入信息**，全部为按 OpsMind 演示制度/SOP 人工构造的语料。示例中的系统、流程、角色、工单和变更均不代表真实配置；OpsMind 评测基线必须通过真实在线评测生成，不能随文档改写手工更新。

---

## 1. 字段定义

### 1.1 `intent_cases.json`

```json
{
  "id": "I097",
  "message": "登录报错还导致发布没完成，赶紧安排回滚",
  "expected_intent": "change_request",
  "expected_group": "request",
  "difficulty": "hard",
  "note": "混合意图（登录故障加发布失败），按明确回滚诉求归 change_request"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | `I` + 三位序号，全局唯一 |
| `message` | string | 用户原话（单条消息），保留口语、错别字、标点缺失 |
| `expected_intent` | string | 19 类之一，定义见第 2 节 |
| `expected_group` | string | 意图组，按第 2 节映射填写 |
| `difficulty` | string | `easy` / `medium` / `hard`，定义见第 3 节 |
| `note` | string | 判定依据；`hard` 用例必填，用于复核 |

**单标签原则**：每条 `message` 只有一个 `expected_intent`。真实用户一句话常含多个诉求，本集用固定优先级归一（见 2.4），`note` 记录次要诉求与判定理由。

### 1.2 `dialog_cases.json`

```json
{
  "id": "D11",
  "turns": ["业务系统一直报错，而且账号还突然登不上", "你们到底管不管"],
  "expected_agent": "incident",
  "expected_intent": "application_error",
  "expected_supporting_agents": ["access"],
  "tags": ["复合问题", "INCIDENT路由"],
  "note": "应用故障加账号访问双诉求；主 incident、辅 access"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | `D` + 两位序号 |
| `turns` | string[] | 用户侧两轮消息，按时间顺序 |
| `expected_agent` | string | 主 Agent：`service_desk` / `incident` / `access` / `escalation` |
| `expected_intent` | string | **对话结束时应识别出的意图**（以最后一轮诉求为准，除非前轮诉求未被回应） |
| `expected_supporting_agents` | string[] | 仅复合问题用例出现，表示应协作的辅助 Agent |
| `tags` | string[] | `SERVICE_DESK路由` / `INCIDENT路由` / `ACCESS路由` / `ESCALATION路由` / `CRITICAL升级` / `应澄清` / `复合问题` / `指代` |
| `note` | string | 路由、标注及合理的回复验收依据 |

本数据集采用以下路由契约：

1. `tags` 含 `CRITICAL升级`（消息含“紧急/urgent/asap”等）→ `escalation`；
2. 否则 `expected_intent` 为 `escalation` / `human_handoff` → `escalation`；
3. `incident` / `service_outage` / `performance_issue` / `application_error` → `incident`；
4. `access` / `account_access` / `permission_request` / `security_incident` → `access`；
5. 其余意图（含 `query` / `request` / `service_status` / `ticket_status` / `change_request` / `other` 等）→ `service_desk`。

### 1.3 对话回复的可验证口径

对话用例只要求从**实际回复文本和路由结果**能够合理验收，不要求调用并不存在的真实企业工具：

| 场景 | 合理回复应体现 | 不应出现 |
| --- | --- | --- |
| 服务状态 / 工单进度 | 解释演示状态、说明所需标识或查询路径、坦承没有实时结果 | 编造实时状态、受理人、处理进度或恢复时间 |
| 故障 / 性能 / 应用错误 | 收集时间、错误码、耗时、影响范围，给出安全排查或升级建议 | 声称已重启、已恢复、已定位根因，或要求执行危险操作 |
| 账号访问 / 权限申请 | 说明演示 SOP、核验步骤、审批材料与最小权限原则 | 声称已重置、已解锁、已授权或审批必然通过 |
| 安全事件 | 建议停止可疑操作、保留证据、通过可信渠道升级，避免索取敏感信息 | 声称已封禁、已取证、已清除威胁，或引导点击可疑链接 |
| 变更发布 / 回滚 | 核对影响范围、验证方法、窗口和回滚预案 | 声称已审批、已发布、已回滚或已验证生产结果 |
| 语义不清 | 提出简短、具体的澄清问题 | 强行虚构系统、工单号或用户诉求 |

---

## 2. 意图定义与标注边界

19 类意图覆盖通用服务台语义与 IT 专业服务语义。`expected_group` 用于高层聚合，不等于 Agent 类型。

### 2.1 定义表

| 意图 | 意图组 | 判据 | 易混边界 |
| --- | --- | --- | --- |
| `query` | query | 服务时间、渠道、目录、指南等泛信息咨询 | 特定服务/工单状态分别归 `service_status` / `ticket_status` |
| `complaint` | complaint | 表达不满、失望或等待情绪，不要求操作或升级 | 要管理层介入→ `escalation`；要真人→ `human_handoff` |
| `request` | request | 修改工单资料、改派、催办、调整支持预约等一般服务操作 | 权限→ `permission_request`；发布/配置/回滚→ `change_request` |
| `greeting` | greeting | 开场问候、试探在线，无业务诉求 | 问候后立即提出业务问题→按业务诉求 |
| `escalation` | escalation | 要求负责人、管理层、申诉或审计介入 | 只要求换真人坐席→ `human_handoff`；只有情绪→ `complaint` |
| `incident` | incident | 未进一步分型的单用户或泛功能故障 | 大范围不可用、缓慢、错误码分别使用细粒度故障意图 |
| `access` | access | 账号资料、绑定方式、开通/归档等身份生命周期管理 | 登录失败→ `account_access`；资源角色→ `permission_request` |
| `feedback` | feedback | 对服务台、工程师或指引的正面评价 | 先负后正但结论肯定→仍归 `feedback` |
| `service_status` | query | 服务可用状态、状态页字段、维护计划查询 | 明确遭遇大范围不可用→ `service_outage` |
| `ticket_status` | query | 工单受理、分派、审批、处理进度查询 | 要求改派/催办→ `request`；系统状态→ `service_status` |
| `service_outage` | incident | 多用户、多地点、部门级或全站服务不可用 | 仅响应缓慢→ `performance_issue`；单用户泛故障→ `incident` |
| `performance_issue` | incident | 延迟、卡顿、耗时过长、高峰期性能下降 | 完全不可用→ `service_outage`；明确异常/错误码→ `application_error` |
| `application_error` | incident | 白屏、初始化失败、组件错误、4xx/5xx、异常堆栈 | 账号认证失败→ `account_access`；无错误表征的泛故障→ `incident` |
| `account_access` | access | 登录循环、凭证错误、验证码、设备不受信、认证超时 | 换绑资料→ `access`；疑似盗用/攻击→ `security_incident` |
| `permission_request` | access | 查看/编辑/导出/管理员等资源权限申请与审批 | 账号本身开通/归档→ `access`；登录问题→ `account_access` |
| `security_incident` | access | 账号盗用、钓鱼、恶意软件、异常授权、敏感数据异常访问 | 普通凭证错误且无攻击迹象→ `account_access` |
| `change_request` | request | 配置变更、版本发布、发布窗口、停止变更、回滚及变更单处理 | 客户端自身报错→ `application_error`；一般工单操作→ `request` |
| `human_handoff` | escalation | 明确要求真人工程师、人工坐席或电话回访 | 要求管理层/追责→ `escalation` |
| `other` | other | 与 IT 服务台无关的闲聊，或语义模糊无法归类 | 有业务实体且可判断→不得标 `other` |

### 2.2 细粒度优先

能落到具体意图就不得标泛类：

- 服务状态、工单状态、服务中断、性能、应用错误、账号访问、权限、安全、变更、转人工 → 对应细粒度意图；
- 泛类 `query` / `incident` / `access` / `request` 只承接没有更具体归属的消息。

### 2.3 否定与干扰规则（否定优先）

出现“不是 A，是 B”、“先别 A，帮我 B”时，**以被肯定的诉求 B 为准**，A 只视为干扰词：

- `I084` “我不是说功能坏了，我是嫌它响应太慢” → `performance_issue`；
- `I091` “我不是要申请变更，我是客户端一直报错” → `application_error`；
- `I021` “先别转硬件组，帮我改派到办公软件支持组” → `request`；
- `I133` “先别急着说工单，我就是想随便问问” → `other`（用户撤回业务诉求）。

### 2.4 混合意图优先级

一句话含多个诉求时，按风险、影响范围和用户明确动作归一：

1. **安全事件与重大服务中断**优先于一般故障；
2. **明确故障表征**优先于泛状态查询；
3. **用户明确要求的业务动作**（如回滚、权限申请）优先于附带现象；
4. 同一优先级内取用户最后强调或需要更强业务动作的诉求。

示例：`I097` 同时出现登录报错与发布失败，但用户明确要求回滚，因此标 `change_request`。对话集允许“主 Agent + 辅助 Agent”保留复合性，例如 `D11` 主 `incident`、辅 `access`，与单句单标签原则不冲突。

### 2.5 易混对边界速查

| A | B | 分界 |
| --- | --- | --- |
| `service_status` | `service_outage` | 问状态字段/公告→A；明确遭遇多用户或全站不可用→B |
| `incident` | `service_outage` / `performance_issue` / `application_error` | 无更具体范围、耗时或错误表征→A，否则用细粒度意图 |
| `ticket_status` | `request` | 只问工单进度→A；要求催办、改派或修改→B |
| `access` | `account_access` | 身份资料/生命周期→A；认证与登录失败→B |
| `account_access` | `security_incident` | 普通登录失败→A；盗用、钓鱼、异常授权→B |
| `access` | `permission_request` | 账号本身→A；具体资源角色或范围→B |
| `change_request` | `application_error` | 主诉求是发布/配置/回滚→A；主诉求是应用异常→B |
| `complaint` | `escalation` / `human_handoff` | 只有情绪→A；要管理层→`escalation`；要真人坐席→`human_handoff` |

---

## 3. 难度定义

难度描述的是**语义标注与理解难度**，不是规则关键词命中难度。

| 难度 | 判定标准 | 占比目标 |
| --- | --- | --- |
| `easy` | 单句单意图；显式业务名词；无否定、指代、干扰、错别字；标注唯一 | ≈40% |
| `medium` | 需要轻微推断：口语化、并列问句、流程咨询、隐含目的、情绪表达 | ≈40% |
| `hard` | 至少含一项：混合意图、否定干扰、指代、错别字、极短句、情绪与诉求不一致、需上文才能判断 | ≈20% |

`hard` 用例的 `note` 必须说明难在哪、为什么标这个意图。

---

## 4. 统计

### 4.1 意图分布

| 意图 | 意图组 | easy | medium | hard | 小计 |
| --- | --- | --- | --- | --- | --- |
| access | access | 3 | 3 | 1 | 7 |
| account_access | access | 3 | 3 | 1 | 7 |
| application_error | incident | 3 | 3 | 1 | 7 |
| change_request | request | 2 | 3 | 2 | 7 |
| complaint | complaint | 2 | 3 | 2 | 7 |
| escalation | escalation | 2 | 3 | 2 | 7 |
| feedback | feedback | 3 | 3 | 1 | 7 |
| greeting | greeting | 3 | 3 | 1 | 7 |
| human_handoff | escalation | 3 | 3 | 1 | 7 |
| incident | incident | 3 | 3 | 1 | 7 |
| other | other | 2 | 3 | 2 | 7 |
| performance_issue | incident | 2 | 3 | 2 | 7 |
| permission_request | access | 3 | 3 | 1 | 7 |
| query | query | 3 | 3 | 1 | 7 |
| request | request | 2 | 3 | 2 | 7 |
| security_incident | access | 3 | 3 | 1 | 7 |
| service_outage | incident | 3 | 3 | 1 | 7 |
| service_status | query | 3 | 3 | 1 | 7 |
| ticket_status | query | 3 | 3 | 1 | 7 |

- 总数 **133**，19 类**全覆盖**，每类 **7** 条。
- 意图组合计：incident 28、access 28、query 21、request 14、escalation 14；complaint / greeting / feedback / other 各 7。

### 4.2 难度分布

| 难度 | 条数 | 占比 | 目标区间 |
| --- | --- | --- | --- |
| easy | 51 | 38.3% | 33%~47% |
| medium | 57 | 42.9% | 33%~47% |
| hard | 25 | 18.8% | 14%~26% |

### 4.3 对话集覆盖

| 维度 | 结果 |
| --- | --- |
| 对话组数 | 14 |
| `service_desk` 路由 | 5 组（D01/D02/D09/D10/D14） |
| `incident` 路由 | 3 组（D03/D04/D11） |
| `access` 路由 | 3 组（D05/D06/D12） |
| `escalation` 路由 | 3 组（D07/D08/D13） |
| CRITICAL 升级 | 1 组（D08） |
| 应触发澄清 | 2 组（D09/D10） |
| 复合问题（主 + 辅 Agent） | 2 组（D11 故障+账号、D12 权限+故障） |
| 对话轮次 | 28 轮 |

### 4.4 hard 样本构成

| 现象 | 代表用例 |
| --- | --- |
| 混合意图 | `I097` 登录报错加发布失败，明确回滚 → `change_request` |
| 否定/干扰词 | `I084` 不是功能坏、`I091` 不是申请变更、`I021` 先别转硬件组、`I133` 先别说工单 |
| 指代 | `I007` 那份说明、`I020` 那个联系人、`I070` 那个工单、`I083` 上次那个卡顿、`I098` 那个脚本 |
| 错别字 | `I119` 权现（权限）；medium 层另有 `I109` 登陆/验正码、`I117` 申情 |
| 极短句 | `I126` 喊个人来（4 字，hard）、`I022` 嗨喽（2 字，easy） |
| 情绪与诉求不一致 | `I063` 本来挺生气的，后来处理得还行 → `feedback` |
| 需上文才能判断 | `I028` 先问个好，一会儿聊正事 → `greeting` |

---

## 5. 构造原则（防数据泄漏）

1. **不抄规则模板**：所有句子重新组织主语、句式与用词，避免规则模板原句及只改标点的变体。
2. **不抄内置用例**：内置评测用例仅作覆盖参考，同样纳入泄漏对照。
3. **真人语体**：保留省略主语、语气词、口语表达、错别字和中英混用，避免关键词罗列。
4. **句式与长度多样**：陈述句、反问句、并列问句、一句话多诉求，长度从极短句到 40+ 字。
5. **每类场景分散**：同一意图覆盖不同系统、流程与表达方式，避免同义复述。
6. **干扰显式化**：需要否定/干扰的样本，在 `note` 写明干扰词及标注原因。
7. **可复核**：标注争议以第 2 节规则裁决；规则未覆盖的新情况先补规则再补样本。

### 泄漏自检口径

对 `intent_cases.json` 的消息与规则模板、内置用例逐条比对（去空白/中英文标点后）：完全一致、参照句被包含或高相似度复制均应判为违规。服务台领域词汇（工单、权限、登录、回滚）自然共用不属于文本泄漏。

---

## 6. 已知局限

1. **非真实工单**：语料由标注者按演示场景构造，不是线上采样；真实分布、行业黑话和方言可能不同。
2. **强制单标签**：真实用户常一句话多诉求，本集按 2.4 归一；多意图信息只保留在 `note` 与对话集的辅助 Agent 字段中。
3. **边界灰区**：`incident` 与性能/应用错误、`access` 与账号/权限、`request` 与变更等仍有灰区，复核应优先按第 2 节规则。
4. **难度主观**：`easy/medium/hard` 由标注者判定，未做双人标注一致性统计。
5. **覆盖缺口**：仅文本单轮意图 + 14 组短对话；未覆盖长会话、语音转写、多模态附件和复杂事件时间线。
6. **无真实工具结果**：回复验收只能评估流程、信息边界与安全建议，不能证明真实监控、工单、身份、安全或发布系统可用。
7. **路由口径需同步**：意图枚举、分组或 Agent 路由契约变更后，必须同步复核两个 JSON 与本文件。

---

## 7. 自检方式

运行现有离线校验：

```bash
python data/eval/_validate.py
```

校验目标包括：JSON 可解析、字段与枚举合法、ID 与文本唯一、133 条意图覆盖、难度分布、hard 样本现象、14 组对话路由、CRITICAL/澄清/复合问题覆盖以及跨文件重复文本。完整结果以当次命令输出为准。

---

## 8. 维护方式

- 新增意图用例：追加 `intent_cases.json`，`id` 继续递增，`expected_group` 必须与第 2 节一致，并同步更新第 4 节统计。
- 新增对话用例：`turns` 保持非空字符串数组，`expected_agent` 只能使用四种 OpsMind Agent；复合问题必须带 `expected_supporting_agents`。
- 修改意图枚举、分组或路由后，必须重跑自检并同步更新定义、统计与对话验收口径。
- 演示知识应保持“无真实企业接入”的边界，不得添加伪造的实时状态、真实客户名称、认证声明或生产执行结果。
- OpsMind 评测基线只能通过真实在线评测生成，不随语料或文档改写手工更新。
