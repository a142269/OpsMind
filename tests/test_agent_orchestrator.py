import asyncio

from agents.agent_orchestrator import (
    AgentProfile,
    AgentResponse,
    AgentType,
    AgentOrchestrator,
    AccessAgent,
    EscalationAgent,
    ServiceDeskAgent,
    Request,
    ResponseComposer,
    RoutingDecision,
    IncidentAgent,
    build_shared_rag_tools,
)
from core.intent_recognizer import IntentCategory, UrgencyLevel


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

        class Messages:
            async def create(inner, **kwargs):
                self.calls.append(kwargs)
                if self.error:
                    raise self.error
                return self.response

        self.messages = Messages()


def make_request(**kwargs):
    values = {
        "message": "ERP 页面报 500，同时我的账号无法登录",
        "user_id": "u1",
        "conv_id": "c1",
        "intent": IntentCategory.APPLICATION_ERROR,
        "intent_group": "incident",
        "urgency": UrgencyLevel.HIGH,
        "intent_confidence": 0.92,
        "entities": {"error_code": ["500"], "account": ["zhangsan"], "service": ["ERP"]},
    }
    values.update(kwargs)
    return Request(**values)


def test_agent_profiles_have_distinct_contracts_and_generation_config():
    assert isinstance(ServiceDeskAgent.profile, AgentProfile)
    assert ServiceDeskAgent.profile.role != IncidentAgent.profile.role
    assert IncidentAgent.profile.workflow != AccessAgent.profile.workflow
    assert IncidentAgent.profile.temperature < ServiceDeskAgent.profile.temperature
    assert "search_knowledge_base" in ServiceDeskAgent.profile.tool_scope
    assert "lookup_error_code" in IncidentAgent.profile.tool_scope
    assert "check_access_fields" in AccessAgent.profile.tool_scope


def test_domain_agents_build_different_role_packets():
    req = make_request()
    general_packet = ServiceDeskAgent(FakeClient(), "test-model")._build_role_packet(req)
    technical_packet = IncidentAgent(FakeClient(), "test-model")._build_role_packet(req)
    access_packet = AccessAgent(FakeClient(), "test-model")._build_role_packet(req)

    assert "triage_targets" in general_packet
    assert "diagnostic_fields" in technical_packet
    assert "verification_fields" in access_packet
    assert general_packet != technical_packet != access_packet


def test_escalation_agent_is_a_real_non_llm_handoff_node():
    client = FakeClient()
    agent = EscalationAgent(client, "test-model")

    result = asyncio.run(agent.handle(make_request(
        intent=IntentCategory.HUMAN_HANDOFF,
        urgency=UrgencyLevel.CRITICAL,
    )))

    assert result.success is True
    assert result.escalate is True
    assert "人工升级" in result.content
    assert "ERP 页面报 500" in result.content
    assert "仍需补充" in result.content
    assert "不代表已创建外部工单" in result.content
    assert "当前已完成（仅系统内部）" in result.content
    assert "请不要发送密码" in result.content
    assert client.calls == []


def test_escalation_security_gives_safe_immediate_steps():
    client = FakeClient()
    agent = EscalationAgent(client, model="test")
    request = make_request(
        message="账号 zhangsan 疑似被盗，机密文件正在被下载",
        intent=IntentCategory.SECURITY_INCIDENT,
        urgency=UrgencyLevel.CRITICAL,
        entities={"account": ["zhangsan"], "service": ["ERP"], "date": ["今天"]},
    )

    result = asyncio.run(agent.handle(request))

    assert "官方自助流程修改密码" in result.content
    assert "保留异常时间" in result.content
    assert "zhangsan" in result.content
    assert "未执行封禁" in result.content
    assert client.calls == []


def test_composer_fallback_preserves_primary_and_supporting_results():
    composer = ResponseComposer(FakeClient(error=RuntimeError("provider down")), "test-model")
    req = make_request()
    responses = [
        AgentResponse(AgentType.INCIDENT, "先收集 request_id 并检查依赖服务。", True),
        AgentResponse(AgentType.ACCESS, "请补充账号标识和登录异常发生时间。", True),
    ]

    content = asyncio.run(composer.compose(req, responses))

    assert content.startswith("先收集 request_id 并检查依赖服务。")
    assert "补充说明" in content
    assert "账号标识" in content


def test_routing_decision_can_target_escalation_pool():
    # Keep this assertion close to the public data contract used by the API.
    decision = RoutingDecision(
        primary_agent=AgentType.ESCALATION,
        reason="critical request",
        confidence=1.0,
    )
    assert decision.agent_types == [AgentType.ESCALATION]
    assert not decision.multi_agent


def test_composite_request_routes_explicit_access_signal_as_supporting_agent():
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator._pool = {
        AgentType.SERVICE_DESK: [object()],
        AgentType.INCIDENT: [object()],
        AgentType.ACCESS: [object()],
    }

    decision = orchestrator._route_decision(make_request())

    assert decision.primary_agent is AgentType.INCIDENT
    assert decision.supporting_agents == [AgentType.ACCESS]
    assert decision.multi_agent is True


def test_agent_tool_scopes_are_real_and_isolated():
    service_desk_tools = set(ServiceDeskAgent(FakeClient(), "test-model").get_tools())
    incident_tools = set(IncidentAgent(FakeClient(), "test-model").get_tools())
    access_tools = set(AccessAgent(FakeClient(), "test-model").get_tools())
    escalation_tools = set(EscalationAgent(FakeClient(), "test-model").get_tools())

    assert service_desk_tools == {"inspect_request_context", "suggest_required_fields"}
    assert incident_tools == {"lookup_error_code", "build_diagnostic_plan"}
    assert access_tools == {"check_access_fields", "build_access_request_summary"}
    assert escalation_tools == {"create_handoff_summary"}
    assert not service_desk_tools & incident_tools
    assert not incident_tools & access_tools


def test_shared_rag_tool_is_available_to_all_agents():
    class RagManager:
        async def search_with_rewrite(self, tool_name, query, top_k=5):
            return type(
                "Result",
                (),
                {"success": True, "data": [{"title": "ERP 故障排查", "content": "记录 request_id 并检查依赖服务"}], "reranked": True},
            )()

    shared = build_shared_rag_tools(RagManager())

    service_desk = ServiceDeskAgent(FakeClient(), "test-model")
    incident = IncidentAgent(FakeClient(), "test-model")
    access = AccessAgent(FakeClient(), "test-model")
    escalation = EscalationAgent(FakeClient(), "test-model")

    for agent in (service_desk, incident, access, escalation):
        agent.set_shared_tools(shared)
        tools = agent.get_tools()
        assert "search_knowledge_base" in tools


def test_tool_input_validation_rejects_unknown_fields():
    agent = IncidentAgent(FakeClient(), "test-model")
    spec = agent.get_tools()["lookup_error_code"]

    try:
        agent._validate_tool_input(spec, {"error_code": "401", "secret": "nope"})
    except ValueError as exc:
        assert "不允许的工具参数" in str(exc)
    else:
        raise AssertionError("unknown tool fields should be rejected")


def test_tool_use_round_trip_executes_only_whitelisted_tool():
    class ToolUseBlock:
        type = "tool_use"
        id = "toolu_1"
        name = "lookup_error_code"
        input = {"error_code": "401"}

    class TextBlock:
        type = "text"
        text = "已根据 401 错误码给出排查建议。"

    class ToolClient:
        def __init__(self):
            self.calls = []
            self.responses = [
                type("Response", (), {"content": [ToolUseBlock()]})(),
                type("Response", (), {"content": [TextBlock()]})(),
            ]

        class Messages:
            def __init__(self, owner):
                self.owner = owner

            async def create(self, **kwargs):
                self.owner.calls.append(kwargs)
                return self.owner.responses.pop(0)

        @property
        def messages(self):
            return self.Messages(self)

    client = ToolClient()
    agent = IncidentAgent(client, "test-model")
    response = asyncio.run(agent.handle(make_request()))

    assert response.success is True
    assert response.tools_used == ["lookup_error_code"]
    assert len(response.tool_traces) == 1
    assert response.tool_traces[0]["tool_name"] == "lookup_error_code"
    assert response.tool_traces[0]["input"] == {"error_code": "401"}
    assert response.tool_traces[0]["success"] is True
    assert len(client.calls) == 2
    assert {tool["name"] for tool in client.calls[0]["tools"]} == {
        "lookup_error_code",
        "build_diagnostic_plan",
    }
    assert "tool_result" in str(client.calls[1]["messages"])


def test_concurrent_requests_keep_tool_traces_isolated():
    class ToolUseBlock:
        type = "tool_use"

        def __init__(self, code):
            self.id = f"toolu_{code}"
            self.name = "lookup_error_code"
            self.input = {"error_code": code}

    class TextBlock:
        type = "text"

        def __init__(self, text):
            self.text = text

    class ConcurrentClient:
        class Messages:
            async def create(inner, **kwargs):
                messages = kwargs["messages"]
                last_content = messages[-1]["content"]
                if isinstance(last_content, list):
                    code = "500" if "500" in str(last_content) else "401"
                    await asyncio.sleep(0)
                    return type("Response", (), {"content": [TextBlock(f"{code} 排查完成")]})()
                code = "500" if "500" in str(last_content) else "401"
                await asyncio.sleep(0.01 if code == "401" else 0)
                return type("Response", (), {"content": [ToolUseBlock(code)]})()

        def __init__(self):
            self.messages = self.Messages()

    async def run_both():
        agent = IncidentAgent(ConcurrentClient(), "test-model")
        return await asyncio.gather(
            agent.handle(make_request(message="登录报 401", entities={"error_code": ["401"]})),
            agent.handle(make_request(message="页面报 500", entities={"error_code": ["500"]})),
        )

    first, second = asyncio.run(run_both())
    assert first.tool_traces[0]["input"] == {"error_code": "401"}
    assert second.tool_traces[0]["input"] == {"error_code": "500"}


def test_monitor_penalty_falls_back_to_service_desk_agent():
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    service_desk = ServiceDeskAgent(FakeClient(), "test-model")
    incident = IncidentAgent(FakeClient(), "test-model")
    access = AccessAgent(FakeClient(), "test-model")
    incident.stats.monitor_penalty = 0.5
    orchestrator._pool = {
        AgentType.SERVICE_DESK: [service_desk],
        AgentType.INCIDENT: [incident],
        AgentType.ACCESS: [access],
    }

    decision = orchestrator._route_decision(make_request(
        message="ERP 页面报 500",
        entities={"error_code": ["500"]},
    ))

    assert decision.primary_agent is AgentType.SERVICE_DESK
    assert decision.supporting_agents == []
    assert "monitor_fallback=[incident]" in decision.reason
