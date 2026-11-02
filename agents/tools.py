"""Agent 工具定义与实现。

所有 Agent 工具集中在这里，编排器只负责：
  1. 根据 Agent 类型暴露工具白名单
  2. 执行 LLM 返回的 tool_use
  3. 将工具结果回传给 LLM

工具本身保持确定性、可测试，并明确区分：
  - 当前请求分析
  - 事件排障建议
  - 访问权限字段核验与申请摘要
  - 人工升级摘要
  - 共享知识库 RAG

工单查询、权限授予、配置变更等需要真实业务系统授权的动作不在这里伪造。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from agents.agent_orchestrator import Request


AgentToolHandler = Callable[["Request", Dict[str, Any]], Union[Any, Awaitable[Any]]]


@dataclass(frozen=True)
class AgentToolSpec:
    """Agent 可见工具的定义和执行函数。"""

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: AgentToolHandler


def make_tool(
    name: str,
    description: str,
    properties: Dict[str, Any],
    handler: AgentToolHandler,
    required: Optional[List[str]] = None,
) -> AgentToolSpec:
    """创建带 JSON Schema 的 Agent 工具。"""
    return AgentToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
        handler=handler,
    )


def inspect_request_context(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """服务台工具：返回脱敏后的当前请求快照。"""
    return {
        "intent": req.intent.value if req.intent else None,
        "intent_group": req.intent_group,
        "urgency": req.urgency.name if req.urgency else None,
        "intent_confidence": round(req.intent_confidence, 4),
        "entities": req.entities or {},
        "context_available": bool(req.context),
        "requested_focus": str(args.get("focus", "service_desk"))[:40],
    }


def suggest_required_fields(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """服务台工具：按业务类型计算下一轮只需询问的字段。"""
    intent = req.intent.value if req.intent else "other"
    fields: List[str] = []
    if intent == "ticket_status":
        fields = ["工单号"]
    elif intent in {"service_status", "service_outage", "performance_issue", "application_error"}:
        fields = ["受影响服务", "开始时间与影响范围"]
    elif intent in {"access", "account_access", "security_incident"}:
        fields = ["账号标识", "目标服务", "异常发生时间"]
    elif intent == "permission_request":
        fields = ["账号标识", "目标资源", "权限级别", "业务理由"]
    elif intent == "change_request":
        fields = ["目标服务", "变更内容", "期望窗口", "回滚方案"]
    elif intent in {"complaint", "request"}:
        fields = ["事件时间", "期望处理方式"]
    elif intent == "other":
        fields = ["希望解决的具体 IT 服务问题"]
    return {
        "intent": intent,
        "required_fields": fields,
        "known_entities": req.entities or {},
    }


def lookup_error_code(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """技术工具：解释常见错误码的排查方向，不声称读取了服务端日志。"""
    code = str(args.get("error_code", "")).upper().strip()
    mapping = {
        "401": ("认证失败", ["确认 Token/API Key 是否过期", "确认请求时间戳和签名", "确认账号登录状态"]),
        "403": ("权限不足", ["确认账号角色和资源授权", "确认资源权限和 IP 白名单"]),
        "404": ("资源或路径不存在", ["确认接口路径和环境", "确认资源标识是否正确"]),
        "500": ("服务端处理异常", ["记录 request_id 和发生时间", "检查依赖服务、参数格式和服务端日志"]),
    }
    meaning, steps = mapping.get(
        code,
        ("暂未识别的错误码", ["补充完整错误信息、发生时间和运行环境"]),
    )
    return {
        "error_code": code,
        "meaning": meaning,
        "next_steps": steps,
        "server_log_checked": False,
    }


def build_diagnostic_plan(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """技术工具：生成低风险排障顺序。"""
    environment = str(args.get("environment", "unknown"))[:80]
    reproduced = bool(args.get("reproduced", False))
    steps = [
        "复现并记录完整错误信息",
        "确认网络、DNS、代理和证书",
        "确认版本、配置和权限",
    ]
    if reproduced:
        steps.append("用最小请求复现并记录 request_id")
    return {
        "environment": environment,
        "reproduced": reproduced,
        "diagnostic_steps": steps,
    }


def check_access_fields(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """访问权限工具：检查申请或排障所需字段，不连接 IAM 系统。"""
    fields = {
        "account": bool(req.entities.get("account") or args.get("account")),
        "target_resource": bool(req.entities.get("service") or args.get("target_resource")),
        "access_level": bool(args.get("access_level")),
        "business_reason": bool(args.get("business_reason")),
    }
    return {
        "fields": fields,
        "missing_fields": [name for name, present in fields.items() if not present],
        "can_grant_access": False,
        "reason": "当前工具只做字段检查，不连接 IAM、审批或账号系统",
    }


def build_access_request_summary(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """访问权限工具：生成最小权限申请摘要，不提交真实审批。"""
    return {
        "request_id": req.request_id,
        "account": str(args.get("account") or (req.entities.get("account") or ["未提供"])[0])[:64],
        "target_resource": str(args.get("target_resource") or (req.entities.get("service") or ["未提供"])[0])[:120],
        "access_level": str(args.get("access_level", "未提供"))[:40],
        "business_reason": str(args.get("business_reason", "未提供"))[:240],
        "duration": str(args.get("duration", "按组织默认策略"))[:60],
        "submitted": False,
        "note": "仅为申请摘要，需由真实审批流程和授权人员处理",
    }


def create_handoff_summary(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """升级工具：生成可交给值班工程师的结构化摘要。"""
    return {
        "request_id": req.request_id,
        "reason": str(args.get("reason", "需要值班工程师继续核验"))[:120],
        "intent": req.intent.value if req.intent else "unknown",
        "urgency": req.urgency.name if req.urgency else "UNKNOWN",
        "entities": req.entities or {},
        "sensitive_data_required": False,
    }


def build_shared_rag_tools(tool_manager: Any) -> Dict[str, AgentToolSpec]:
    """构建所有 Agent 可共享的 RAG 工具。"""

    async def search_knowledge_base(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or req.message or "").strip()
        top_k = int(args.get("top_k", 5) or 5)
        if not query:
            return {"success": False, "error": "query 不能为空", "results": []}
        if tool_manager is None:
            return {"success": False, "error": "RAG 工具未初始化", "results": []}

        result = await tool_manager.search_with_rewrite(
            "knowledge_search",
            query,
            top_k=top_k,
        )
        if not getattr(result, "success", False):
            return {
                "success": False,
                "query": query,
                "error": getattr(result, "error", "知识库检索失败"),
                "results": [],
                "reranked": False,
            }

        return {
            "success": True,
            "query": query,
            "top_k": top_k,
            "results": result.data,
            "reranked": bool(getattr(result, "reranked", False)),
        }

    return {
        "search_knowledge_base": make_tool(
            "search_knowledge_base",
            "检索知识库并返回最相关的文档片段；可用于服务台、事件、访问权限和升级场景。",
            {
                "query": {"type": "string", "description": "用户问题或检索关键词"},
                "top_k": {"type": "integer", "description": "返回结果条数"},
            },
            search_knowledge_base,
            required=["query"],
        )
    }


def service_desk_tools() -> Dict[str, AgentToolSpec]:
    return {
        "inspect_request_context": make_tool(
            "inspect_request_context",
            "查看当前请求的意图、紧急度、实体和上下文可用性；不查询外部业务系统。",
            {"focus": {"type": "string", "description": "希望关注的业务方向"}},
            inspect_request_context,
        ),
        "suggest_required_fields": make_tool(
            "suggest_required_fields",
            "根据当前意图建议下一轮只需向用户补充的字段。",
            {},
            suggest_required_fields,
        ),
    }


def incident_tools() -> Dict[str, AgentToolSpec]:
    return {
        "lookup_error_code": make_tool(
            "lookup_error_code",
            "解释常见 HTTP 错误码的可能含义和低风险排查方向；不会读取服务端日志。",
            {"error_code": {"type": "string", "description": "例如 401、403、500"}},
            lookup_error_code,
            required=["error_code"],
        ),
        "build_diagnostic_plan": make_tool(
            "build_diagnostic_plan",
            "根据运行环境和是否可复现生成排障顺序，不执行修改配置等操作。",
            {
                "environment": {"type": "string", "description": "App、浏览器、服务端或 Docker 等"},
                "reproduced": {"type": "boolean", "description": "问题是否可以稳定复现"},
            },
            build_diagnostic_plan,
            required=["environment", "reproduced"],
        ),
    }


def access_tools() -> Dict[str, AgentToolSpec]:
    return {
        "check_access_fields": make_tool(
            "check_access_fields",
            "检查账号访问或权限申请字段是否齐全；不连接 IAM 或审批系统。",
            {
                "account": {"type": "string", "description": "账号标识，不要填写密码或验证码"},
                "target_resource": {"type": "string", "description": "目标系统、服务或资源"},
                "access_level": {"type": "string", "description": "例如只读、写入或指定角色"},
                "business_reason": {"type": "string", "description": "最小权限申请的业务理由"},
            },
            check_access_fields,
        ),
        "build_access_request_summary": make_tool(
            "build_access_request_summary",
            "生成最小权限申请摘要供用户确认和人工审批；不会提交或授予权限。",
            {
                "account": {"type": "string", "description": "账号标识"},
                "target_resource": {"type": "string", "description": "目标系统、服务或资源"},
                "access_level": {"type": "string", "description": "权限级别或角色"},
                "business_reason": {"type": "string", "description": "业务理由"},
                "duration": {"type": "string", "description": "期望权限有效期"},
            },
            build_access_request_summary,
            required=["target_resource", "access_level", "business_reason"],
        ),
    }


def escalation_tools() -> Dict[str, AgentToolSpec]:
    return {
        "create_handoff_summary": make_tool(
            "create_handoff_summary",
            "生成交给值班工程师的结构化交接摘要，不会创建真实工单。",
            {"reason": {"type": "string", "description": "需要升级的原因"}},
            create_handoff_summary,
        ),
    }
