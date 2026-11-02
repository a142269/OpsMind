#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
intent_degraded 透传链路自检（offline，stdlib only，无网络/无第三方依赖）。

验证目标（task-6）：
  1. Request / OrchestratorResult 各新增 intent_degraded 且旧构造方式不破；
  2. `_route_decision` 在 intent_degraded=True 时于 reason 末尾追加 "; intent_degraded"，
     未降级时 reason 完全不变，且路由结果（primary/confidence）不受影响；
  3. `run()` / `run_parallel()` / 澄清分支把 req.intent_degraded 透传到 OrchestratorResult；
  4. `api/main.py` 的 ChatResponse 字段与 /chat 两处传参接线正确（静态 AST 校验，
     因为 api/main.py 依赖 fastapi/pydantic，离线无法 import）；
  5. lead 在 /eval/run 里加的 degraded_count / degraded_rate 透传仍在。

运行（仓库根目录）：
    python EchoMind/_offline_harness/_verify_degraded_plumbing.py
退出码 0 = 全部 PASS。
"""
from __future__ import annotations

import ast
import asyncio
import dataclasses
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
STUBS_DIR = HERE / "stubs"

# 与 _offline_harness/run.py 相同的前置注入：stubs 优先，其次项目根
sys.path.insert(0, str(STUBS_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

# 统一输出编码，避免 Windows 管道下中文乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from agents.agent_orchestrator import (  # noqa: E402
    AgentOrchestrator,
    AgentType,
    OrchestratorResult,
    Request,
    RoutingDecision,
)
from core.intent_recognizer import IntentCategory, UrgencyLevel  # noqa: E402

PASSED = 0
FAILED = 0
CASES = 0
MARKER = "; intent_degraded"


def check(case_no: int, name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"[PASS] {case_no}. {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {case_no}. {name}" + (f"  -> {detail}" if detail else ""))


def new_case(name: str):
    global CASES
    CASES += 1
    return CASES, name


def make_request(**overrides) -> Request:
    kwargs = dict(
        message="INC-2026 处理到哪了",
        user_id="offline_user",
        conv_id="offline_conv",
        intent=IntentCategory.TICKET_STATUS,
        intent_group="query",
        urgency=UrgencyLevel.LOW,
        intent_confidence=0.9,
    )
    kwargs.update(overrides)
    return Request(**kwargs)


def find_calls(tree: ast.AST, func_name: str):
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == func_name
    ]


def keyword_value(call: ast.Call, name: str):
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def is_attr(node, base: str, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == base
    )


def check_api_wiring() -> None:
    """静态校验 api/main.py 接线（无法 import fastapi/pydantic，只能 AST）。"""
    case_no, name = new_case("api/main.py 接线（AST）")
    main_path = PROJECT_ROOT / "api" / "main.py"
    source = main_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # ChatResponse 必须有 intent_degraded: bool = False
    chat_response_fields = {}
    has_default = False
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ChatResponse":
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    chat_response_fields[item.target.id] = ast.unparse(item.annotation)
                    if item.target.id == "intent_degraded" and item.value is not None:
                        has_default = ast.unparse(item.value) == "False"
    check(
        case_no, name + "：ChatResponse 有 intent_degraded: bool = False",
        chat_response_fields.get("intent_degraded") == "bool" and has_default,
        f"fields={chat_response_fields}",
    )

    # /chat 构造 OrcReq 时传 intent_degraded=intent_result.degraded
    orc_calls = find_calls(tree, "OrcReq")
    orc_ok = any(
        is_attr(keyword_value(call, "intent_degraded"), "intent_result", "degraded")
        for call in orc_calls
    )
    check(
        case_no, name + "：OrcReq 传 intent_degraded=intent_result.degraded",
        orc_ok,
        f"OrcReq calls={len(orc_calls)}",
    )

    # /chat 返回 ChatResponse 时传 intent_degraded=result.intent_degraded
    chat_calls = find_calls(tree, "ChatResponse")
    chat_ok = any(
        is_attr(keyword_value(call, "intent_degraded"), "result", "intent_degraded")
        for call in chat_calls
    )
    check(
        case_no, name + "：ChatResponse 传 intent_degraded=result.intent_degraded",
        chat_ok,
        f"ChatResponse calls={len(chat_calls)}",
    )

    # lead 的 eval 降级统计透传必须仍在
    check(
        case_no, name + "：lead 的 degraded_count/degraded_rate 4 行保留",
        "report.degraded_count" in source and "report.degraded_rate" in source
        and '"degraded_count"' in source and '"degraded_rate"' in source,
        "run_eval 里的降级统计透传疑似被覆盖",
    )


def main() -> int:
    orchestrator = AgentOrchestrator(api_key="offline-no-key", model="offline-stub-model")

    # 1) 旧构造方式不破：不传 intent_degraded 时默认 False
    case_no, name = new_case("向后兼容默认值")
    plain_request = Request(message="你好", user_id="u", conv_id="c")
    check(
        case_no, name + "：Request 不传 intent_degraded 默认 False",
        plain_request.intent_degraded is False,
        f"got={plain_request.intent_degraded!r}",
    )
    plain_result = OrchestratorResult(
        request_id="r1",
        response="ok",
        agent_type=AgentType.SERVICE_DESK,
        intent=None,
    )
    check(
        case_no, name + "：OrchestratorResult 旧构造默认 False",
        plain_result.intent_degraded is False,
        f"got={plain_result.intent_degraded!r}",
    )

    # 2) _route_decision：降级追加标记、不降级完全不变、路由结果不变
    case_no, name = new_case("_route_decision 标记")
    base_request = make_request()
    degraded_request = dataclasses.replace(base_request, intent_degraded=True)
    base_decision = orchestrator._route_decision(base_request)
    degraded_decision = orchestrator._route_decision(degraded_request)
    check(
        case_no, name + "：降级 reason 以 '; intent_degraded' 结尾（只出现 1 次）",
        degraded_decision.reason.endswith(MARKER)
        and degraded_decision.reason.count("intent_degraded") == 1,
        f"reason={degraded_decision.reason!r}",
    )
    check(
        case_no, name + "：未降级 reason 不含标记且保持原样",
        "intent_degraded" not in base_decision.reason,
        f"reason={base_decision.reason!r}",
    )
    check(
        case_no, name + "：标记只追加在末尾，原 reason 内容/顺序不变",
        degraded_decision.reason == base_decision.reason + MARKER,
        f"base={base_decision.reason!r}\\ndegraded={degraded_decision.reason!r}",
    )
    check(
        case_no, name + "：路由算法不受影响（primary/confidence/supporting 一致）",
        degraded_decision.primary_agent == base_decision.primary_agent
        and degraded_decision.confidence == base_decision.confidence
        and degraded_decision.supporting_agents == base_decision.supporting_agents,
        f"base=({base_decision.primary_agent}, {base_decision.confidence}), "
        f"degraded=({degraded_decision.primary_agent}, {degraded_decision.confidence})",
    )

    # 2b) 其余路由分支（CRITICAL / ESCALATION）也要带标记
    case_no, name = new_case("_route_decision 其他分支")
    critical = orchestrator._route_decision(
        make_request(urgency=UrgencyLevel.CRITICAL, intent_degraded=True)
    )
    escalation = orchestrator._route_decision(
        make_request(intent=IntentCategory.ESCALATION, intent_degraded=True)
    )
    check(
        case_no, name + "：CRITICAL 分支 reason 带标记",
        critical.reason.endswith(MARKER) and critical.primary_agent == AgentType.ESCALATION,
        f"reason={critical.reason!r}",
    )
    check(
        case_no, name + "：ESCALATION 分支 reason 带标记",
        escalation.reason.endswith(MARKER) and escalation.primary_agent == AgentType.ESCALATION,
        f"reason={escalation.reason!r}",
    )

    # 3) run()：intent_degraded 透传到 OrchestratorResult（intent 已给出，跳过识别）
    case_no, name = new_case("run() 透传")
    degraded_result = asyncio.run(orchestrator.run(degraded_request))
    check(
        case_no, name + "：result.intent_degraded=True 且 reason 末尾带标记",
        degraded_result.intent_degraded is True
        and degraded_result.routing_reason.endswith(MARKER)
        and degraded_result.routing_reason.count("intent_degraded") == 1,
        f"flag={degraded_result.intent_degraded!r}, reason={degraded_result.routing_reason!r}",
    )
    check(
        case_no, name + "：run() 仍有正常响应（透传未破坏执行链路）",
        bool(degraded_result.response),
        f"response={degraded_result.response[:60]!r}",
    )
    base_result = asyncio.run(orchestrator.run(base_request))
    check(
        case_no, name + "：未降级时 result.intent_degraded=False 且 reason 无标记",
        base_result.intent_degraded is False
        and "intent_degraded" not in base_result.routing_reason,
        f"flag={base_result.intent_degraded!r}, reason={base_result.routing_reason!r}",
    )

    # 3b) run() 澄清分支（不经过 _route_decision）也要带标记
    case_no, name = new_case("run() 澄清分支")
    clarify_request = make_request(
        intent=IntentCategory.OTHER,
        intent_confidence=0.1,
        intent_degraded=True,
    )
    clarify_result = asyncio.run(orchestrator.run(clarify_request))
    check(
        case_no, name + "：澄清分支 reason 带标记且 flag 透传",
        clarify_result.intent_degraded is True
        and clarify_result.routing_reason.endswith(MARKER)
        and "澄清" in clarify_result.routing_reason,
        f"flag={clarify_result.intent_degraded!r}, reason={clarify_result.routing_reason!r}",
    )

    # 4) run_parallel()：透传 req.intent_degraded
    case_no, name = new_case("run_parallel() 透传")
    parallel_decision = RoutingDecision(
        primary_agent=AgentType.SERVICE_DESK,
        supporting_agents=[AgentType.INCIDENT],
        reason="self-check parallel decision",
        confidence=0.5,
    )
    parallel_degraded = asyncio.run(
        orchestrator.run_parallel(degraded_request, parallel_decision)
    )
    parallel_base = asyncio.run(
        orchestrator.run_parallel(base_request, parallel_decision)
    )
    check(
        case_no, name + "：降级请求的并行结果为 True",
        parallel_degraded.intent_degraded is True,
        f"flag={parallel_degraded.intent_degraded!r}",
    )
    check(
        case_no, name + "：未降级请求的并行结果为 False",
        parallel_base.intent_degraded is False,
        f"flag={parallel_base.intent_degraded!r}",
    )

    # 5) api/main.py 接线静态校验
    check_api_wiring()

    total = PASSED + FAILED
    print()
    print(f"共 {total} 项检查（{CASES} 个用例组）：通过 {PASSED}，失败 {FAILED}")
    if FAILED:
        print("存在 FAIL")
        return 1
    print("全 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
