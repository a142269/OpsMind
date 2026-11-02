"""
亮点：端到端 Agent 评测框架

核心问题：如何评测端到端 Agent？

评测维度：
  1. 意图识别准确率 —— 预测意图 vs 标注意图，计算 Accuracy / F1
  2. 响应质量评分 —— 用 LLM 作为评判者（LLM-as-Judge），
     从相关性、准确性、完整性、有用性四个维度打分
  3. 端到端对话评测 —— 模拟完整多轮对话，评估整体体验
  4. 回归测试 —— 与历史基线对比，防止性能退化

LLM-as-Judge 是评测 Agent 质量的关键技术：
  人工标注成本高、主观性强；用 LLM 评判可以规模化、可重复。
"""
import asyncio
import json
import logging
import pathlib
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from core.llm_utils import extract_text_content

from core.intent_recognizer import IntentCategory, IntentRecognizer

from evaluation.judge_parse import extract_json_object, validate_judge_scores

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class IntentTestCase:
    message:          str
    expected_intent:  str
    context:          Optional[Dict[str, Any]] = None


@dataclass
class QualityScores:
    """LLM-as-Judge 评分结果。"""
    relevance:    float   # 相关性：回答是否针对问题
    accuracy:     float   # 准确性：信息是否正确
    completeness: float   # 完整性：是否完整解决问题
    helpfulness:  float   # 有用性：用户是否能据此行动
    judge_failed: bool = False
    error: Optional[str] = None
    # judge_degraded=True 表示四项 0.5 是“兜底默认值”，不是真实评分：
    # 解析/校验失败后重试 1 次仍失败 → 测量失效，报告层需要单独统计，不能当作质量分数解读。
    judge_degraded: bool = False
    judge_attempts: int = 0

    @property
    def overall(self) -> float:
        return statistics.mean([self.relevance, self.accuracy, self.completeness, self.helpfulness])


@dataclass
class EvalResult:
    test_id:    str
    passed:     bool
    scores:     Dict[str, float]
    detail:     str = ""
    metadata:   Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalReport:
    """评测报告。"""
    timestamp:        str
    total:            int
    passed:           int
    pass_rate:        float
    avg_scores:       Dict[str, float]
    regressions:      List[str]          # 相比基线退化的指标
    recommendations:  List[str]
    results:          List[EvalResult]
    # 测量失效统计（新增，只增不改）：judge 重试后仍失败、分数为 0.5 兜底的用例
    degraded_count:   int = 0
    degraded_rate:    float = 0.0


# ── LLM-as-Judge ─────────────────────────────────────────────────────────────

class LLMJudge:
    """
    用 LLM 评判 Agent 响应质量。

    为什么用 LLM 而不是人工？
    - 可规模化：数千条测试用例自动评测
    - 可重复：相同输入得到稳定评分
    - 多维度：同时评估相关性、准确性等多个维度

    注意：LLM Judge 本身也有偏差，建议定期用人工标注校准。
    """

    JUDGE_PROMPT = """你是企业 IT 服务台质量评估专家。请对以下运维协同 Agent 响应进行评分。

用户问题: {question}
Agent 响应: {response}
{context_section}

请从以下四个维度评分（0.0-1.0），返回 JSON：
- relevance: 响应是否直接针对用户问题（0=完全无关，1=完全相关）
- accuracy: 信息是否准确无误（0=明显错误，1=完全正确）
- completeness: 是否完整解决了用户需求（0=完全没解决，1=完全解决）
- helpfulness: 用户能否据此采取行动（0=毫无帮助，1=非常有帮助）

只返回 JSON，例如: {{"relevance": 0.9, "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.85}}"""

    # 解析/校验失败后追加的纠偏消息：只重试 1 次，降低 token 成本同时救回大部分格式问题
    JUDGE_CORRECTION = (
        "你上一次的输出无法被解析为合法评分。请严格只输出一个 JSON 对象："
        "不要任何解释文字，不要 Markdown 代码围栏；"
        "必须且只能包含 relevance、accuracy、completeness、helpfulness 四个键，"
        "值为 0.0 到 1.0 之间的数字。示例: "
        '{"relevance": 0.9, "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.85}'
    )

    def __init__(self, client: AsyncAnthropic, model: str):
        self._client = client
        self._model  = model

    async def judge(
        self,
        question: str,
        response: str,
        context: Optional[str] = None,
    ) -> QualityScores:
        ctx_section = f"背景信息: {context}" if context else ""
        prompt = self.JUDGE_PROMPT.format(
            question=question,
            response=response,
            context_section=ctx_section,
        )
        prompt = self._clean_text(prompt)
        messages: List[Dict[str, str]] = [{"role": "user", "content": prompt}]
        errors: List[str] = []

        # 最多尝试 2 次：第 1 次用原始 prompt；失败后追加纠偏消息重试 1 次。
        for attempt in (1, 2):
            raw = ""
            try:
                resp = await self._client.messages.create(
                    model=self._model, max_tokens=1024, temperature=0.0,
                    messages=messages,
                )
                raw = self._clean_text(extract_text_content(resp.content))
            except Exception as ex:
                errors.append(f"第 {attempt} 次调用失败: {ex}")
            else:
                obj = extract_json_object(raw)
                ok, scores, parse_error = validate_judge_scores(obj)
                if ok and scores is not None:
                    return QualityScores(
                        relevance=scores["relevance"],
                        accuracy=scores["accuracy"],
                        completeness=scores["completeness"],
                        helpfulness=scores["helpfulness"],
                        judge_attempts=attempt,
                    )
                if obj is None:
                    errors.append(
                        f"第 {attempt} 次输出无法提取 JSON（{len(raw)} 字符）: {raw[:80]!r}"
                    )
                else:
                    errors.append(f"第 {attempt} 次输出校验失败: {parse_error}")

            # 准备纠偏重试：保留上一轮输出（非空时）作为 assistant 回合，再追加纠偏指令。
            if attempt == 1:
                retry_messages = list(messages)
                if raw.strip():
                    retry_messages.append({"role": "assistant", "content": raw[:2000]})
                retry_messages.append({"role": "user", "content": self.JUDGE_CORRECTION})
                messages = retry_messages

        # 两次都失败：保留 0.5 兜底值（向后兼容），但显式标记这是测量失效而非真实评分。
        error_text = "; ".join(errors) or "judge 调用失败（原因未知）"
        logger.warning(f"LLM Judge 两次尝试均失败: {error_text}")
        return QualityScores(
            0.5, 0.5, 0.5, 0.5,
            judge_failed=True,
            error=error_text,
            judge_degraded=True,
            judge_attempts=2,
        )

    @staticmethod
    def _clean_text(value: Any) -> str:
        """移除 Unicode 代理字符，避免 LLM 请求编码失败。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")


# ── 意图识别评测 ──────────────────────────────────────────────────────────────

class IntentEvaluator:
    """评测意图识别的准确率和 F1。"""

    def __init__(self, recognizer: IntentRecognizer):
        self._recognizer = recognizer

    async def evaluate(self, cases: List[IntentTestCase]) -> Dict[str, Any]:
        predictions, ground_truth = [], []
        case_details: List[Dict[str, Any]] = []

        for case in cases:
            result = await self._recognizer.recognize(case.message)
            predicted = result.intent.value
            predictions.append(predicted)
            ground_truth.append(case.expected_intent)
            case_details.append({
                "message": case.message,
                "expected": case.expected_intent,
                "predicted": predicted,
                "confidence": result.confidence,
                "reasoning": result.reasoning,
            })

        # 纯 Python 计算指标
        correct = sum(p == g for p, g in zip(predictions, ground_truth))
        accuracy = correct / len(predictions) if predictions else 0.0

        # 每类 F1（新增 support：该类在标注中出现的次数 = TP + FN）
        labels = sorted(set(ground_truth + predictions))
        per_class: Dict[str, Dict[str, Any]] = {}
        f1_values: List[float] = []
        for label in labels:
            tp = sum(p == label and g == label for p, g in zip(predictions, ground_truth))
            fp = sum(p == label and g != label for p, g in zip(predictions, ground_truth))
            fn = sum(p != label and g == label for p, g in zip(predictions, ground_truth))
            prec = tp / (tp + fp) if (tp + fp) else 0.0
            rec  = tp / (tp + fn) if (tp + fn) else 0.0
            f1   = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
            f1_values.append(f1)
            per_class[label] = {
                "precision": round(prec, 4),
                "recall": round(rec, 4),
                "f1": round(f1, 4),
                "support": tp + fn,
            }

        # macro_f1 仍用未取整的原始 F1 求均值，保持与历史报告口径一致
        macro_f1 = statistics.mean(f1_values) if f1_values else 0.0

        return {
            "accuracy":   round(accuracy, 4),
            "macro_f1":   round(macro_f1, 4),
            "per_class":  per_class,
            "total":      len(cases),
            "correct":    correct,
            "cases":      case_details,
        }


# ── 端到端评测器 ──────────────────────────────────────────────────────────────

class EndToEndEvaluator:
    """
    端到端 Agent 评测。

    评测流程：
      1. 运行意图识别评测（准确率/F1）
      2. 运行对话质量评测（LLM-as-Judge）
      3. 与历史基线对比（回归检测）
      4. 生成可操作的优化建议
    """

    # 质量及格线
    PASS_THRESHOLD = 0.75

    def __init__(
        self,
        orchestrator,
        recognizer: IntentRecognizer,
        api_key:  str,
        base_url: Optional[str] = None,
        model:    str = "claude-3-5-sonnet-20241022",
        baseline_path: Optional[str] = None,
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = AsyncAnthropic(**kwargs)

        self._orchestrator     = orchestrator
        self._judge            = LLMJudge(client, model)
        self._intent_evaluator = IntentEvaluator(recognizer)
        self._history:         List[EvalReport] = []
        self._baseline_path = pathlib.Path(baseline_path) if baseline_path else None
        self._baseline: Optional[EvalReport] = self._load_baseline()

    async def run(
        self,
        intent_cases:    Optional[List[IntentTestCase]] = None,
        dialog_cases:    Optional[List[Dict[str, Any]]] = None,
    ) -> EvalReport:
        """
        运行完整评测。

        intent_cases: 意图识别测试用例
        dialog_cases:
          - 单轮: [{"question": "..."}]
          - 多轮: [{"turns": ["第一轮", "第二轮", ...]}]
        """
        results: List[EvalResult] = []
        all_scores: Dict[str, List[float]] = {
            "relevance": [], "accuracy": [], "completeness": [], "helpfulness": []
        }

        # 1. 意图识别评测
        intent_metrics: Dict[str, Any] = {}
        if intent_cases:
            intent_metrics = await self._intent_evaluator.evaluate(intent_cases)
            passed = intent_metrics["accuracy"] >= self.PASS_THRESHOLD
            results.append(EvalResult(
                test_id="intent_recognition",
                passed=passed,
                scores={"accuracy": intent_metrics["accuracy"], "macro_f1": intent_metrics["macro_f1"]},
                detail=f"准确率 {intent_metrics['accuracy']:.1%}，Macro-F1 {intent_metrics['macro_f1']:.3f}",
                metadata={
                    "total": intent_metrics.get("total", 0),
                    "correct": intent_metrics.get("correct", 0),
                    "cases": intent_metrics.get("cases", []),
                    # 对外字段只增不改：per_class 新增在 metadata 里（含 precision/recall/f1/support）
                    "per_class": intent_metrics.get("per_class", {}),
                },
            ))

        # 2. 对话质量评测（调用 orchestrator 产出回复，再用 LLM Judge 评分）
        if dialog_cases:
            for i, case in enumerate(dialog_cases):
                case_results = await self._evaluate_dialog_case(case, i)
                results.extend(case_results)
                for r in case_results:
                    for k in all_scores:
                        if k in r.scores:
                            all_scores[k].append(r.scores[k])

        # 3. 汇总
        avg_scores = {
            k: round(statistics.mean(v), 4) for k, v in all_scores.items() if v
        }
        if intent_metrics:
            avg_scores["intent_accuracy"] = intent_metrics["accuracy"]

        passed_count = sum(1 for r in results if r.passed)
        pass_rate    = passed_count / len(results) if results else 0.0

        # 测量失效统计：judge 两次尝试都失败、分数是 0.5 兜底值的用例数。
        # 分母只算“真的调过 judge 的用例”（意图评测不经过 judge），否则失败率会被稀释。
        # 这个比例高时，pass_rate / avg_scores 反映的是评测链路故障，不是模型质量。
        degraded_count = sum(1 for r in results if r.metadata.get("judge_degraded"))
        judge_scored_count = sum(1 for r in results if "judge_attempts" in r.metadata)
        degraded_rate = (
            round(degraded_count / judge_scored_count, 4) if judge_scored_count else 0.0
        )

        # 4. 回归检测
        regressions = self._detect_regressions(avg_scores)

        # 5. 优化建议
        recommendations = self._recommendations(
            avg_scores,
            intent_metrics,
            degraded_count=degraded_count,
            degraded_rate=degraded_rate,
            total=judge_scored_count,
        )

        report = EvalReport(
            timestamp=datetime.now().isoformat(),
            total=len(results),
            passed=passed_count,
            pass_rate=round(pass_rate, 4),
            avg_scores=avg_scores,
            regressions=regressions,
            recommendations=recommendations,
            results=results,
            degraded_count=degraded_count,
            degraded_rate=degraded_rate,
        )
        self._history.append(report)
        self._save_baseline(report)
        return report

    async def _evaluate_dialog_case(self, case: Dict[str, Any], case_idx: int) -> List[EvalResult]:
        """评测单轮或多轮对话用例。"""
        from agents.agent_orchestrator import Request as OrcReq

        questions = self._dialog_turns(case)
        if not questions:
            return []

        conv_id = str(case.get("conv_id") or f"eval_{case_idx}")
        user_id = str(case.get("user_id") or "eval_user")
        history: List[Dict[str, str]] = []
        results: List[EvalResult] = []

        for turn_idx, question in enumerate(questions):
            context = self._history_context(history)
            orch_req = OrcReq(
                message=question,
                user_id=user_id,
                conv_id=conv_id,
                context=context,
                history=history[-6:] if history else None,
            )
            orch_result = await self._orchestrator.run(orch_req)
            actual_answer = orch_result.response

            scores = await self._judge.judge(question, actual_answer, context=context or None)
            passed = scores.overall >= self.PASS_THRESHOLD

            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": actual_answer})

            test_id = f"dialog_{case_idx}" if len(questions) == 1 else f"dialog_{case_idx}_turn_{turn_idx}"
            results.append(EvalResult(
                test_id=test_id,
                passed=passed,
                scores={
                    "relevance": scores.relevance,
                    "accuracy": scores.accuracy,
                    "completeness": scores.completeness,
                    "helpfulness": scores.helpfulness,
                    "overall": scores.overall,
                },
                detail=f"Q: {question[:30]}... → 综合评分 {scores.overall:.3f}",
                metadata={
                    "question": question,
                    "response": actual_answer,
                    "agent_type": orch_result.agent_type.value,
                    "intent": orch_result.intent.value if orch_result.intent else None,
                    "turn": turn_idx,
                    "conv_id": conv_id,
                    "judge_failed": scores.judge_failed,
                    # judge_degraded=True：分数是 0.5 兜底值，属于测量失效而非真实评分
                    "judge_degraded": scores.judge_degraded,
                    "judge_error": scores.error,
                    "judge_attempts": scores.judge_attempts,
                },
            ))

        return results

    @staticmethod
    def _dialog_turns(case: Dict[str, Any]) -> List[str]:
        turns = case.get("turns")
        if isinstance(turns, list):
            return [str(t) for t in turns if str(t).strip()]
        question = case.get("question")
        return [str(question)] if question else []

    @staticmethod
    def _history_context(history: List[Dict[str, str]]) -> str:
        if not history:
            return ""
        lines = [f"{m['role']}: {m['content']}" for m in history[-8:]]
        return "[评测多轮历史]\n" + "\n".join(lines)

    def _detect_regressions(self, current: Dict[str, float]) -> List[str]:
        """与上一次评测对比，找出退化超过 5% 的指标。"""
        prev_report = self._history[-1] if self._history else self._baseline
        if prev_report is None:
            return []
        prev = prev_report.avg_scores
        regressions = []
        for metric, value in current.items():
            if metric in prev and prev[metric] > 0:
                delta = (value - prev[metric]) / prev[metric]
                if delta < -0.05:
                    regressions.append(
                        f"{metric}: {prev[metric]:.3f} → {value:.3f} (退化 {abs(delta):.1%})"
                    )
        return regressions

    def _recommendations(
        self,
        scores: Dict[str, float],
        intent_metrics: Dict[str, Any],
        degraded_count: int = 0,
        degraded_rate: float = 0.0,
        total: int = 0,
    ) -> List[str]:
        recs = []
        if scores.get("intent_accuracy", 1.0) < 0.90:
            recs.append("意图识别准确率 < 90%：增加 Few-shot 示例，或对低 F1 的意图类别补充训练数据")
        if scores.get("relevance", 1.0) < 0.75:
            recs.append("相关性偏低：检查 Agent system_prompt，确保 Agent 聚焦于用户问题")
        if scores.get("completeness", 1.0) < 0.75:
            recs.append("完整性偏低：Agent 可能过早结束回答，考虑在 prompt 中要求提供完整解决方案")
        if scores.get("helpfulness", 1.0) < 0.75:
            recs.append("有用性偏低：回答可能过于抽象，考虑要求 Agent 提供具体操作步骤")
        if degraded_count > 0:
            # 关键：把“评测链路坏了”和“模型质量差”分开，避免误读 0.5 兜底分
            recs.append(
                f"judge 解析失败率 {degraded_rate:.1%}（{degraded_count}/{total} 条用例）："
                "属于测量失效而非质量下降，当前 0.5 分为解析失败后的兜底默认值，"
                "应先修复 Judge 输出格式/重试链路，再解读 pass_rate 与 avg_scores"
            )
        if not recs:
            recs.append("所有指标均达标，继续保持")
        return recs

    @property
    def history(self) -> List[EvalReport]:
        return self._history

    def _load_baseline(self) -> Optional[EvalReport]:
        if not self._baseline_path or not self._baseline_path.exists():
            return None
        try:
            data = json.loads(self._baseline_path.read_text(encoding="utf-8"))
            return self._report_from_dict(data)
        except Exception as ex:
            logger.warning(f"读取评测基线失败: {ex}")
            return None

    def _save_baseline(self, report: EvalReport) -> None:
        if not self._baseline_path:
            return
        try:
            self._baseline_path.parent.mkdir(parents=True, exist_ok=True)
            self._baseline_path.write_text(
                json.dumps(asdict(report), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._baseline = report
        except Exception as ex:
            logger.warning(f"保存评测基线失败: {ex}")

    @staticmethod
    def _report_from_dict(data: Dict[str, Any]) -> EvalReport:
        return EvalReport(
            timestamp=data.get("timestamp", ""),
            total=int(data.get("total", 0)),
            passed=int(data.get("passed", 0)),
            pass_rate=float(data.get("pass_rate", 0.0)),
            avg_scores=dict(data.get("avg_scores", {})),
            regressions=list(data.get("regressions", [])),
            recommendations=list(data.get("recommendations", [])),
            results=[
                EvalResult(
                    test_id=r.get("test_id", ""),
                    passed=bool(r.get("passed", False)),
                    scores=dict(r.get("scores", {})),
                    detail=r.get("detail", ""),
                    metadata=dict(r.get("metadata", {})),
                )
                for r in data.get("results", [])
            ],
            degraded_count=int(data.get("degraded_count", 0)),
            degraded_rate=float(data.get("degraded_rate", 0.0)),
        )


# ── 内置测试用例（开箱即用）──────────────────────────────────────────────────

DEFAULT_INTENT_CASES: List[IntentTestCase] = [
    IntentTestCase("ERP 服务现在正常吗？",       "service_status"),
    IntentTestCase("INC-2026 处理到哪了？",       "ticket_status"),
    IntentTestCase("工单等了好几个小时！",         "complaint"),
    IntentTestCase("应用一直报500错误",            "application_error"),
    IntentTestCase("生产环境所有用户都无法访问",    "service_outage"),
    IntentTestCase("我要投诉，转人工！",           "human_handoff"),
    IntentTestCase("你好",                         "greeting"),
    IntentTestCase("登录一直报401",                "account_access"),
    IntentTestCase("申请代码库读取权限",            "permission_request"),
    IntentTestCase("账号疑似被盗，有异常登录",      "security_incident"),
    IntentTestCase("申请今晚的生产发布窗口",        "change_request"),
]

DEFAULT_DIALOG_CASES: List[Dict[str, Any]] = [
    {"question": "ERP 全公司都无法访问，已经影响财务结账"},
    {"question": "企业账号登录一直报错 401"},
    {"question": "我需要申请测试环境数据库的只读权限"},
    {"question": "帮我查询 INC-2026 的处理进度"},
    {"turns": ["你好，VPN 连不上", "已经持续半小时，影响整个上海办公室", "请帮我整理升级值班工程师的摘要"]},
]
