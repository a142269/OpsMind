"""
Judge 输出解析/校验的离线自检脚本 —— 仅依赖标准库，不需要 anthropic / pydantic。

运行（在仓库根目录或任意目录均可）：
    python EchoMind/evaluation/_verify_judge_parse.py

覆盖：纯 JSON、```json 围栏、围栏未闭合、前后带解释文字、嵌套括号、
0-10 分制归一化、包裹格式、垃圾串、缺键、bool/字符串取值、越界、
空输入、JSON 数组等；全部通过时打印「全 PASS」并以退出码 0 结束。
"""
import sys
from pathlib import Path

# 统一输出编码，避免 Windows 控制台/管道下中文变成乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))        # 便于 import judge_parse
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 便于 import evaluation.evaluator

from judge_parse import (  # noqa: E402  （必须先把脚本目录放进 sys.path）
    SCORE_KEYS,
    extract_json_object,
    parse_judge_scores,
    validate_judge_scores,
)

PASSED = 0
FAILED = 0
CASES = 0

FOUR_KEYS = {"relevance", "accuracy", "completeness", "helpfulness"}


def check(case_no, name, condition, detail=""):
    """记录一条断言结果。"""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"[PASS] {case_no}. {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {case_no}. {name}" + (f"  -> {detail}" if detail else ""))


def new_case(name):
    global CASES
    CASES += 1
    return CASES, name


def approx(a, b, tol=1e-4):
    return abs(float(a) - float(b)) <= tol


def main():
    # 0) 模块本身不得引入第三方依赖（否则离线环境跑不起来）
    case_no, name = new_case("依赖检查")
    check(
        case_no, name + "：judge_parse 只依赖标准库",
        "anthropic" not in sys.modules and "pydantic" not in sys.modules,
        f"已加载第三方模块: anthropic={ 'anthropic' in sys.modules }, "
        f"pydantic={ 'pydantic' in sys.modules }",
    )

    # 1) 纯 JSON
    case_no, name = new_case("纯 JSON")
    raw = '{"relevance": 0.9, "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.85}'
    obj = extract_json_object(raw)
    check(case_no, name + "：extract 返回 dict", isinstance(obj, dict), repr(obj))
    ok, scores, err = validate_judge_scores(obj)
    check(
        case_no, name + "：校验通过且四维值正确",
        ok and scores is not None and set(scores) == FOUR_KEYS
        and approx(scores["relevance"], 0.9) and approx(scores["helpfulness"], 0.85),
        f"ok={ok}, scores={scores}, err={err}",
    )

    # 2) ```json markdown 围栏
    case_no, name = new_case("markdown 围栏")
    fenced = "```json\n" + raw + "\n```"
    obj = extract_json_object(fenced)
    check(
        case_no, name + "：剥 ```json 围栏后解析成功",
        isinstance(obj, dict) and obj.get("accuracy") == 0.8,
        repr(obj),
    )
    plain_fenced = "```\n" + raw + "\n```"
    obj = extract_json_object(plain_fenced)
    check(
        case_no, name + "：剥无语言标记的 ``` 围栏后解析成功",
        isinstance(obj, dict) and obj.get("completeness") == 0.7,
        repr(obj),
    )

    # 3) 围栏未闭合（流式输出被截断）
    case_no, name = new_case("围栏未闭合")
    obj = extract_json_object("```json\n" + raw + "\n")
    check(
        case_no, name + "：依靠括号配平兜底解析成功",
        isinstance(obj, dict) and len(obj) == 4,
        repr(obj),
    )

    # 4) 前后带解释文字
    case_no, name = new_case("前后带解释文字")
    explained = "好的，下面是评分结果：\n" + raw + "\n以上就是四个维度的评分，希望有帮助。"
    obj = extract_json_object(explained)
    check(
        case_no, name + "：正文前后的解释文字被跳过",
        isinstance(obj, dict) and obj.get("relevance") == 0.9,
        repr(obj),
    )

    # 5) 嵌套括号 + 字符串里出现花括号和转义引号
    case_no, name = new_case("嵌套括号")
    nested = (
        '评分如下：\n'
        '{"relevance": 0.9, "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.85, '
        '"meta": {"note": "字符串里有 } 和 \\" 引号", "level": 2}}\n'
        '以上。'
    )
    obj = extract_json_object(nested)
    check(
        case_no, name + "：找到外层平衡大括号（字符串内 } 不干扰）",
        isinstance(obj, dict)
        and isinstance(obj.get("meta"), dict)
        and obj["meta"]["note"] == '字符串里有 } 和 " 引号',
        repr(obj),
    )
    # 解释文字里有孤立未配平 `{`，应继续向后寻找真正的 JSON
    stray_brace = '注意 { 这里的左括号不配平\n' + raw
    obj = extract_json_object(stray_brace)
    check(
        case_no, name + "：跳过解释文字里未配平的孤立 {",
        isinstance(obj, dict) and obj.get("accuracy") == 0.8,
        repr(obj),
    )

    # 6) 0-10 分制自动除以 10
    case_no, name = new_case("0-10 分制")
    ten_scale = '{"relevance": 9, "accuracy": 8.5, "completeness": 7, "helpfulness": 6.5}'
    ok, scores, err = parse_judge_scores("评分（10 分制）：" + ten_scale)
    check(
        case_no, name + "：0-10 分制自动归一化到 0-1",
        ok and scores is not None
        and approx(scores["relevance"], 0.9)
        and approx(scores["accuracy"], 0.85)
        and approx(scores["completeness"], 0.7)
        and approx(scores["helpfulness"], 0.65),
        f"ok={ok}, scores={scores}, err={err}",
    )
    ok, scores, err = validate_judge_scores(
        {"relevance": 10, "accuracy": 0, "completeness": 5, "helpfulness": 1}
    )
    check(
        case_no, name + "：边界值 0/10/5/1 归一化为 1.0/0/0.5/0.1",
        ok and scores is not None
        and approx(scores["relevance"], 1.0)
        and approx(scores["accuracy"], 0.0)
        and approx(scores["completeness"], 0.5)
        and approx(scores["helpfulness"], 0.1),
        f"ok={ok}, scores={scores}, err={err}",
    )

    # 7) 包裹一层 {"scores": {...}}
    case_no, name = new_case("scores 包裹格式")
    ok, scores, err = validate_judge_scores({"scores": {
        "relevance": 0.9, "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.85,
    }})
    check(
        case_no, name + "：允许 {\"scores\": {...}} 包裹一层",
        ok and scores is not None and approx(scores["helpfulness"], 0.85),
        f"ok={ok}, scores={scores}, err={err}",
    )

    # 8) 垃圾串：extract 必须返回 None
    case_no, name = new_case("垃圾串")
    for junk in ("抱歉，我无法完成这次评分。", "null", "[]", "[1, 2, 3]", "relevance: 0.9"):
        obj = extract_json_object(junk)
        check(case_no, f"{name}：extract({junk!r}) 返回 None", obj is None, repr(obj))
    ok, scores, err = parse_judge_scores("抱歉，我无法完成这次评分。")
    check(
        case_no, name + "：parse_judge_scores 返回失败且 error 非空",
        (not ok) and scores is None and bool(err),
        f"ok={ok}, scores={scores}, err={err!r}",
    )
    # `{}` 是合法 JSON 对象（extract 会返回 {}），但缺维度必须被 schema 校验拒绝
    check(
        case_no, name + "：{} 能提取为空 dict，但校验必须判为非法",
        extract_json_object("{}") == {} and not parse_judge_scores("{}")[0],
        f"extract={extract_json_object('{}')!r}, parse={parse_judge_scores('{}')!r}",
    )

    # 9) 空输入
    case_no, name = new_case("空输入")
    for empty in (None, "", "   \n\t "):
        obj = extract_json_object(empty)
        check(case_no, f"{name}：extract({empty!r}) 返回 None", obj is None, repr(obj))

    # 10) 缺键
    case_no, name = new_case("缺键")
    missing_key = {"relevance": 0.9, "accuracy": 0.8, "completeness": 0.7}
    ok, scores, err = validate_judge_scores(missing_key)
    check(
        case_no, name + "：缺 helpfulness 判为非法，error 指出缺失维度",
        (not ok) and scores is None and "helpfulness" in err,
        f"ok={ok}, scores={scores}, err={err!r}",
    )
    ok, scores, err = validate_judge_scores({})
    check(
        case_no, name + "：空对象判为非法",
        (not ok) and scores is None and bool(err),
        f"ok={ok}, scores={scores}, err={err!r}",
    )

    # 11) bool 值（True 是 int 子类，必须显式拒绝）
    case_no, name = new_case("bool 值")
    ok, scores, err = validate_judge_scores(
        {"relevance": True, "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.85}
    )
    check(
        case_no, name + "：bool 不接受（error 提到 bool）",
        (not ok) and scores is None and "bool" in err.lower(),
        f"ok={ok}, scores={scores}, err={err!r}",
    )

    # 12) 字符串值 / None 值 / 其他类型
    case_no, name = new_case("非数字值")
    ok, scores, err = validate_judge_scores(
        {"relevance": "0.9", "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.85}
    )
    check(
        case_no, name + "：字符串值不接受",
        (not ok) and scores is None and "字符串" in err,
        f"ok={ok}, scores={scores}, err={err!r}",
    )
    ok, scores, err = validate_judge_scores(
        {"relevance": None, "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.85}
    )
    check(
        case_no, name + "：None 值不接受",
        (not ok) and scores is None and bool(err),
        f"ok={ok}, scores={scores}, err={err!r}",
    )

    # 13) 越界（不做裁剪，避免把坏数据洗成“好分数”）
    case_no, name = new_case("越界")
    for bad in (
        {"relevance": 15, "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.85},
        {"relevance": -0.1, "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.85},
        {"relevance": 11, "accuracy": 20, "completeness": 7, "helpfulness": 8},
    ):
        ok, scores, err = validate_judge_scores(bad)
        check(
            case_no, name + f"：{bad['relevance']} 越界判为非法",
            (not ok) and scores is None and "越界" in err,
            f"ok={ok}, scores={scores}, err={err!r}",
        )
    ok, scores, err = parse_judge_scores(raw)
    check(
        case_no, name + "：validate 只返回四维（不夹带 meta 等额外键）",
        ok and scores is not None and set(scores) == set(SCORE_KEYS),
        f"scores={scores}",
    )

    # 14) 非 dict 输入
    case_no, name = new_case("非 dict 输入")
    for bad_obj in ([1, 2, 3], "0.9", 42, None):
        ok, scores, err = validate_judge_scores(bad_obj)
        check(
            case_no, name + f"：validate({bad_obj!r}) 判为非法",
            (not ok) and scores is None and bool(err),
            f"ok={ok}, scores={scores}, err={err!r}",
        )

    # 15) 真实执行 evaluator（用 stdlib stub 伪造 anthropic/core/agents 依赖）
    try:
        _install_stub_modules()
        _run_integration_checks()
    except Exception as ex:  # noqa: BLE001  自检脚本本身失败要报 FAIL 而不是抛栈
        global FAILED
        FAILED += 1
        print(f"[FAIL] evaluator 集成自检异常: {type(ex).__name__}: {ex}")
        import traceback
        traceback.print_exc()

    # 汇总
    total = PASSED + FAILED
    print()
    print(f"共 {total} 项检查（{CASES} 个用例组）：通过 {PASSED}，失败 {FAILED}")
    if FAILED:
        print("存在 FAIL")
        return 1
    print("全 PASS")
    return 0


def _install_stub_modules():
    """
    安装 anthropic / core / agents 的最小 stub。

    本机没有 anthropic / pydantic，evaluator.py 无法直接 import；但 Judge 的
    “解析→校验→纠偏重试→降级标记”逻辑必须被真实执行才能验证。这里用标准库
    伪造依赖模块，让 evaluator 可以在离线环境跑起来（不联网、不装包）。
    """
    import types
    from types import SimpleNamespace

    anthropic = types.ModuleType("anthropic")

    class _FakeMessages:
        def __init__(self, script):
            self._script = list(script)
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            reply = self._script.pop(0) if self._script else ""
            if isinstance(reply, BaseException):
                raise reply
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])

    class AsyncAnthropic:
        # 每个实例创建时按当前 reply_script 生成回复队列；测试里逐条设置即可
        reply_script = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.messages = _FakeMessages(AsyncAnthropic.reply_script)

    anthropic.AsyncAnthropic = AsyncAnthropic

    core = types.ModuleType("core")
    core.__path__ = []
    llm_utils = types.ModuleType("core.llm_utils")

    def extract_text_content(content):
        parts = []
        for block in content or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts)

    llm_utils.extract_text_content = extract_text_content

    intent_recognizer = types.ModuleType("core.intent_recognizer")

    class IntentCategory:
        pass

    class IntentRecognizer:
        pass

    intent_recognizer.IntentCategory = IntentCategory
    intent_recognizer.IntentRecognizer = IntentRecognizer
    core.llm_utils = llm_utils
    core.intent_recognizer = intent_recognizer

    agents = types.ModuleType("agents")
    agents.__path__ = []
    agent_orchestrator = types.ModuleType("agents.agent_orchestrator")

    class Request:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    agent_orchestrator.Request = Request
    agents.agent_orchestrator = agent_orchestrator

    sys.modules.update({
        "anthropic": anthropic,
        "core": core,
        "core.llm_utils": llm_utils,
        "core.intent_recognizer": intent_recognizer,
        "agents": agents,
        "agents.agent_orchestrator": agent_orchestrator,
    })
    return anthropic


def _run_integration_checks():
    """用 stub 依赖真实执行 evaluator 的 judge 重试、降级统计、per_class 与阈值逻辑。"""
    import asyncio
    from types import SimpleNamespace

    from evaluation import evaluator as ev

    anthropic = sys.modules["anthropic"]
    valid = '{"relevance": 0.9, "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.85}'
    fenced_valid = "```json\n" + valid + "\n```"

    # 1) 第 1 次答非 JSON → 带纠偏消息重试 → 第 2 次成功
    case_no, name = new_case("judge 纠偏重试（真实执行）")
    anthropic.AsyncAnthropic.reply_script = ["抱歉，我无法给出精确评分。", fenced_valid]
    judge = ev.LLMJudge(anthropic.AsyncAnthropic(api_key="fake-key"), "fake-model")
    first = asyncio.run(judge.judge("ERP 无法访问", "已整理故障升级摘要"))
    calls = judge._client.messages.calls
    check(
        case_no, name + "：第 2 次成功，拿回真实分（0.9/0.8/0.7/0.85）",
        (not first.judge_failed) and (not first.judge_degraded) and first.judge_attempts == 2
        and approx(first.overall, 0.8125) and first.error is None,
        f"failed={first.judge_failed}, degraded={first.judge_degraded}, "
        f"attempts={first.judge_attempts}, overall={first.overall}, err={first.error!r}",
    )
    check(
        case_no, name + "：第 2 次请求带 assistant 原输出 + 纠偏消息（3 条 messages）",
        len(calls) == 2 and len(calls[1]["messages"]) == 3
        and calls[1]["messages"][1]["role"] == "assistant"
        and "只输出一个 JSON 对象" in calls[1]["messages"][-1]["content"],
        f"calls={len(calls)}, "
        f"roles={[m.get('role') for m in calls[1]['messages']] if len(calls) > 1 else None}",
    )

    # 2) 两次都失败 → 0.5 兜底 + judge_degraded=True + 报告可统计
    case_no, name = new_case("judge 两次失败（真实执行）")
    anthropic.AsyncAnthropic.reply_script = ["完全不是 JSON", ""]
    judge = ev.LLMJudge(anthropic.AsyncAnthropic(api_key="fake-key"), "fake-model")
    degraded = asyncio.run(judge.judge("q", "a"))
    check(
        case_no, name + "：保留 0.5 兜底但标记 degraded（不再静默）",
        degraded.judge_failed is True and degraded.judge_degraded is True
        and degraded.judge_attempts == 2
        and approx(degraded.relevance, 0.5) and approx(degraded.overall, 0.5)
        and "第 2 次" in (degraded.error or ""),
        f"failed={degraded.judge_failed}, degraded={degraded.judge_degraded}, "
        f"attempts={degraded.judge_attempts}, err={degraded.error!r}",
    )
    check(
        case_no, name + "：恰好调用 2 次（只重试 1 次）",
        len(judge._client.messages.calls) == 2,
        f"calls={len(judge._client.messages.calls)}",
    )

    # 3) 第 1 次调用抛异常（网络/鉴权）→ 重试成功；异常轮不塞空 assistant 消息
    case_no, name = new_case("judge 异常后重试（真实执行）")
    anthropic.AsyncAnthropic.reply_script = [RuntimeError("network down"), valid]
    judge = ev.LLMJudge(anthropic.AsyncAnthropic(api_key="fake-key"), "fake-model")
    recovered = asyncio.run(judge.judge("q", "a"))
    calls = judge._client.messages.calls
    check(
        case_no, name + "：异常后重试成功，attempts=2",
        (not recovered.judge_failed) and (not recovered.judge_degraded)
        and recovered.judge_attempts == 2 and approx(recovered.overall, 0.8125),
        f"failed={recovered.judge_failed}, attempts={recovered.judge_attempts}",
    )
    check(
        case_no, name + "：异常轮 raw 为空，重试只有 2 条 messages（不塞空 assistant）",
        len(calls) == 2 and len(calls[1]["messages"]) == 2,
        f"calls={len(calls)}, msgs={[len(c['messages']) for c in calls]}",
    )

    # 4) 首次即返回 0-10 分制 → 自动归一化，attempts=1
    case_no, name = new_case("judge 0-10 分制（真实执行）")
    anthropic.AsyncAnthropic.reply_script = [
        '以下是评分：{"relevance": 9, "accuracy": 8, "completeness": 7, "helpfulness": 6}'
    ]
    judge = ev.LLMJudge(anthropic.AsyncAnthropic(api_key="fake-key"), "fake-model")
    ten = asyncio.run(judge.judge("q", "a"))
    check(
        case_no, name + "：0-10 分制归一化且不需要重试",
        (not ten.judge_failed) and ten.judge_attempts == 1
        and approx(ten.relevance, 0.9) and approx(ten.helpfulness, 0.6)
        and approx(ten.overall, 0.75),
        f"failed={ten.judge_failed}, attempts={ten.judge_attempts}, overall={ten.overall}",
    )

    # 5) 意图评测 per_class：新增 support，macro_f1 口径不变
    case_no, name = new_case("意图 per_class（真实执行）")

    class _FakeRecognizer:
        def __init__(self, mapping):
            self._mapping = mapping

        async def recognize(self, message):
            return SimpleNamespace(
                intent=SimpleNamespace(value=self._mapping[message]),
                confidence=0.9,
                reasoning="stub",
            )

    ie = ev.IntentEvaluator(_FakeRecognizer({
        "查工单": "ticket_status",
        "申请权限": "permission_request",
        "变更权限": "permission_request",
    }))
    metrics = asyncio.run(ie.evaluate([
        ev.IntentTestCase("查工单", "ticket_status"),
        ev.IntentTestCase("申请权限", "permission_request"),
        ev.IntentTestCase("变更权限", "permission_request"),
    ]))
    pc = metrics["per_class"]
    check(
        case_no, name + "：全对时 support 正确、f1=1.0、macro_f1=1.0",
        set(pc) == {"ticket_status", "permission_request"}
        and pc["ticket_status"]["support"] == 1 and pc["permission_request"]["support"] == 2
        and approx(pc["permission_request"]["f1"], 1.0) and approx(metrics["macro_f1"], 1.0)
        and approx(metrics["accuracy"], 1.0),
        f"per_class={pc}, macro_f1={metrics['macro_f1']}, acc={metrics['accuracy']}",
    )
    ie2 = ev.IntentEvaluator(_FakeRecognizer({"a": "ticket_status", "b": "greeting"}))
    metrics2 = asyncio.run(ie2.evaluate([
        ev.IntentTestCase("a", "ticket_status"),
        ev.IntentTestCase("b", "permission_request"),
    ]))
    pc2 = metrics2["per_class"]
    check(
        case_no, name + "：错判时 support 只按标注计数（总数=用例数）、macro_f1=1/3",
        sum(item["support"] for item in pc2.values()) == 2
        and pc2["permission_request"]["support"] == 1 and pc2["greeting"]["support"] == 0
        and approx(pc2["permission_request"]["precision"], 0.0) and approx(pc2["permission_request"]["recall"], 0.0)
        and approx(metrics2["macro_f1"], 1.0 / 3, tol=1e-3),
        f"per_class={pc2}, macro_f1={metrics2['macro_f1']}",
    )

    # 6) 完整 run()：降级统计进入报告 + per_class 进入 intent 用例 metadata
    case_no, name = new_case("完整 run() 报告（真实执行）")

    class _FakeOrchestrator:
        async def run(self, request):
            return SimpleNamespace(
                response="这是 stub 生成的 IT 服务台回复。",
                agent_type=SimpleNamespace(value="service_desk"),
                intent=SimpleNamespace(value="query"),
            )

    def make_evaluator():
        evaluator = ev.EndToEndEvaluator.__new__(ev.EndToEndEvaluator)
        evaluator._orchestrator = _FakeOrchestrator()
        evaluator._judge = ev.LLMJudge(anthropic.AsyncAnthropic(api_key="fake-key"), "fake-model")
        evaluator._intent_evaluator = ev.IntentEvaluator(_FakeRecognizer({"查工单": "ticket_status"}))
        evaluator._history = []
        evaluator._baseline_path = None
        evaluator._baseline = None
        return evaluator

    intent_cases = [ev.IntentTestCase("查工单", "ticket_status")]
    dialog_cases = [{"question": "INC-2026 还没有分派"}]

    anthropic.AsyncAnthropic.reply_script = ["垃圾输出", "还是垃圾"]
    degraded_report = asyncio.run(make_evaluator().run(
        intent_cases=intent_cases, dialog_cases=dialog_cases,
    ))
    dialog_result = next(r for r in degraded_report.results if r.test_id.startswith("dialog"))
    intent_result = next(r for r in degraded_report.results if r.test_id == "intent_recognition")
    check(
        case_no, name + "：degraded_count=1 / degraded_rate=1.0（分母只算被 judge 的用例）",
        degraded_report.degraded_count == 1
        and approx(degraded_report.degraded_rate, 1.0)
        and degraded_report.total == 2,
        f"degraded_count={degraded_report.degraded_count}, "
        f"rate={degraded_report.degraded_rate}, total={degraded_report.total}",
    )
    anthropic.AsyncAnthropic.reply_script = []
    intent_only = asyncio.run(make_evaluator().run(
        intent_cases=intent_cases, dialog_cases=None,
    ))
    check(
        case_no, name + "：没有 judge 用例时 degraded_rate=0（不会除零/误报）",
        intent_only.degraded_count == 0 and approx(intent_only.degraded_rate, 0.0),
        f"degraded_count={intent_only.degraded_count}, rate={intent_only.degraded_rate}",
    )
    check(
        case_no, name + "：用例级 metadata 带 judge_degraded/judge_attempts/judge_error",
        dialog_result.metadata.get("judge_degraded") is True
        and dialog_result.metadata.get("judge_attempts") == 2
        and bool(dialog_result.metadata.get("judge_error")),
        f"metadata={dialog_result.metadata.get('judge_degraded')}, "
        f"attempts={dialog_result.metadata.get('judge_attempts')}",
    )
    check(
        case_no, name + "：recommendations 明确提示“测量失效而非质量下降”",
        any("测量失效" in rec and "解析失败率" in rec for rec in degraded_report.recommendations),
        f"recommendations={degraded_report.recommendations}",
    )
    check(
        case_no, name + "：intent metadata 透出 per_class（含 support）",
        isinstance(intent_result.metadata.get("per_class"), dict)
        and intent_result.metadata["per_class"].get("ticket_status", {}).get("support") == 1,
        f"per_class={intent_result.metadata.get('per_class')}",
    )
    check(
        case_no, name + "：EvalReport 序列化包含 degraded_count/degraded_rate",
        "degraded_count" in ev.asdict(degraded_report)
        and "degraded_rate" in ev.asdict(degraded_report),
        f"keys={list(ev.asdict(degraded_report))[-3:]}",
    )

    # 7) 正常路径：无降级时 degraded_count=0，阈值判定仍为 0.75
    case_no, name = new_case("完整 run() 正常路径（真实执行）")
    anthropic.AsyncAnthropic.reply_script = [valid]
    healthy_report = asyncio.run(make_evaluator().run(
        intent_cases=intent_cases, dialog_cases=dialog_cases,
    ))
    check(
        case_no, name + "：无降级用例，pass_rate=1.0，不追加测量失效提示",
        healthy_report.degraded_count == 0
        and approx(healthy_report.degraded_rate, 0.0)
        and approx(healthy_report.pass_rate, 1.0)
        and not any("测量失效" in rec for rec in healthy_report.recommendations),
        f"degraded_count={healthy_report.degraded_count}, pass_rate={healthy_report.pass_rate}, "
        f"recs={healthy_report.recommendations}",
    )

    # 8) 阈值/回归口径保持不变：PASS 0.75、相对退化 5%
    case_no, name = new_case("阈值与回归口径")
    check(
        case_no, name + "：PASS_THRESHOLD 仍为 0.75",
        ev.EndToEndEvaluator.PASS_THRESHOLD == 0.75,
        f"PASS_THRESHOLD={ev.EndToEndEvaluator.PASS_THRESHOLD}",
    )
    detector = make_evaluator()
    detector._history = [SimpleNamespace(avg_scores={"relevance": 0.80})]
    check(
        case_no, name + "：0.80→0.75（-6.25%）仍判为回归",
        len(detector._detect_regressions({"relevance": 0.75})) == 1,
        f"regressions={detector._detect_regressions({'relevance': 0.75})}",
    )
    check(
        case_no, name + "：0.80→0.77（-3.75%）不判为回归",
        detector._detect_regressions({"relevance": 0.77}) == [],
        f"regressions={detector._detect_regressions({'relevance': 0.77})}",
    )


if __name__ == "__main__":
    sys.exit(main())
