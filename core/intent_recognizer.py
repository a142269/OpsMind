"""
亮点：端到端意图识别

三路融合策略：
  1. LLM 语义理解（权重 70%）—— 主力，理解复杂语义和上下文
  2. Embedding 向量相似度（权重 20%）—— 快速匹配常见表达
  3. 关键词模式匹配（权重 10%）—— 零延迟兜底

三路结果通过加权投票合并，置信度低于阈值时降级为 OTHER。
LLM 和 Embedding 并行调用，不串行等待。

降级路径（B+C 修复，2026-10）：
  - LLM 不可用时不再"早退"直接采信 embedding/pattern 单路结果；
  - 降级时把基础权重 (embedding 0.2 / pattern 0.1) 在**可用源**上重新归一化；
  - 降级时改用独立的 ``degraded_threshold``（默认 0.15，沿用历史标定值），
    使低置信度结果仍能落回 OTHER，让上层澄清/追问逻辑可以触发；
  - 正常路径（LLM 可用）的权重、阈值、仲裁顺序保持改动前逐位一致。
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from core.llm_utils import extract_text_content

logger = logging.getLogger(__name__)


class IntentCategory(Enum):
    QUERY = "query"                      # 通用信息查询
    COMPLAINT = "complaint"              # 服务投诉与不满
    REQUEST = "request"                  # 通用服务请求
    GREETING = "greeting"                # 问候
    ESCALATION = "escalation"            # 升级处理
    INCIDENT = "incident"                # 通用故障
    ACCESS = "access"                    # 通用账号与权限
    FEEDBACK = "feedback"                # 正面反馈
    SERVICE_STATUS = "service_status"    # 服务运行状态
    TICKET_STATUS = "ticket_status"      # 工单进度
    SERVICE_OUTAGE = "service_outage"    # 服务中断
    PERFORMANCE_ISSUE = "performance_issue"  # 性能与可用性下降
    APPLICATION_ERROR = "application_error"  # 应用报错
    ACCOUNT_ACCESS = "account_access"    # 登录、认证与账号访问
    PERMISSION_REQUEST = "permission_request"  # 权限申请与变更
    SECURITY_INCIDENT = "security_incident"    # 安全事件
    CHANGE_REQUEST = "change_request"    # 配置与变更请求
    HUMAN_HANDOFF = "human_handoff"      # 转人工
    OTHER = "other"


class UrgencyLevel(Enum):
    LOW      = 1
    MEDIUM   = 2
    HIGH     = 3
    CRITICAL = 4


@dataclass
class IntentResult:
    intent:     IntentCategory
    confidence: float
    urgency:    UrgencyLevel
    intent_group: str
    entities:   Dict[str, List[str]]   # 从消息中提取的实体
    reasoning:  str
    latency_ms: float
    source_scores: Dict[str, float] = field(default_factory=dict)
    degraded: bool = False                      # LLM 不可用时的降级融合结果
    degraded_sources: List[str] = field(default_factory=list)  # 不可用/被降级的信号源


# 降级融合阈值（B+C 方案）：LLM 不可用、只剩 embedding/pattern 时使用的独立阈值。
# 0.15 是工程兼容默认值，不代表 OpsMind 当前数据集的最优阈值。
# OpsMind 需要使用 `_offline_harness/calibrate_threshold.py` 重新实测标定。
# 该值只适用于「LLM 恒失败」的降级分布，LLM 上线后分数尺度不同，也需重新标定
# （把 ECHOMIND_DEGRADED_THRESHOLD 设为新值即可，无需改代码）。
DEFAULT_DEGRADED_THRESHOLD: float = 0.15


@dataclass
class FusionOutcome:
    """一次三路融合的完整结果（纯数据，便于审计与回放）。

    ``weights_used`` 记录**实际生效**的权重：正常路径为改动前的固定权重，
    降级路径为在可用源上归一化后的权重。``degraded_sources`` 只在降级时非空，
    列出当时不可用的信号源（如 ``["llm"]``）。
    """

    intent: IntentCategory
    confidence: float
    source_scores: Dict[str, float]
    degraded: bool = False
    weights_used: Dict[str, float] = field(default_factory=dict)
    degraded_sources: List[str] = field(default_factory=list)


def _coerce_threshold(value: Any, default: float) -> float:
    """把阈值配置规整到 (0, 1]；非法配置回退默认值，避免启动即崩。"""
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        logger.warning("忽略非法降级阈值配置 %r，回退默认 %.2f", value, default)
        return default
    if not 0.0 < candidate <= 1.0:
        logger.warning("降级阈值 %s 不在 (0, 1] 区间，回退默认 %.2f", candidate, default)
        return default
    return candidate


def _resolve_degraded_threshold(explicit: Optional[float]) -> float:
    """解析降级阈值：显式参数 > ECHOMIND_DEGRADED_THRESHOLD > 0.15 历史标定默认值。"""
    if explicit is not None:
        return _coerce_threshold(explicit, DEFAULT_DEGRADED_THRESHOLD)
    env_value = os.getenv("ECHOMIND_DEGRADED_THRESHOLD", "").strip()
    if env_value:
        return _coerce_threshold(env_value, DEFAULT_DEGRADED_THRESHOLD)
    return DEFAULT_DEGRADED_THRESHOLD


# ── Few-shot 模板（同时用于 LLM 示例和 Embedding 匹配）────────────────────────
_TEMPLATES: Dict[IntentCategory, List[str]] = {
    IntentCategory.QUERY: ["如何提交工单？", "运维值班时间是什么？", "服务目录在哪里？"],
    IntentCategory.COMPLAINT: ["工单等了好几个小时！", "处理效率太差了！", "一直没人跟进！"],
    IntentCategory.REQUEST: ["帮我创建服务请求", "我需要安装办公软件", "请协助申请测试环境"],
    IntentCategory.GREETING: ["你好", "嗨，有人吗", "早上好"],
    IntentCategory.ESCALATION: ["我要升级处理", "找值班经理", "请升级到二线支持"],
    IntentCategory.INCIDENT: ["业务系统不能用了", "服务出现异常", "帮我排查系统故障"],
    IntentCategory.ACCESS: ["账号访问有问题", "请协助处理账号权限", "企业账号怎么管理"],
    IntentCategory.FEEDBACK: ["处理得很及时！", "非常满意", "感谢快速恢复服务"],
    IntentCategory.SERVICE_STATUS: ["ERP 服务现在正常吗？", "官网服务状态怎么样？", "当前是否有服务异常公告？"],
    IntentCategory.TICKET_STATUS: ["INC-2026 处理到哪了？", "我的工单现在是什么状态？", "工单有没有分派工程师？"],
    IntentCategory.SERVICE_OUTAGE: ["ERP 全公司都无法访问", "生产服务中断", "所有用户都连接不上 VPN"],
    IntentCategory.PERFORMANCE_ISSUE: ["系统响应特别慢", "接口延迟很高", "页面加载需要一分钟"],
    IntentCategory.APPLICATION_ERROR: ["应用一直崩溃", "页面报500错误", "系统提示异常错误码"],
    IntentCategory.ACCOUNT_ACCESS: ["无法登录企业账号", "登录一直报401", "MFA 验证码收不到"],
    IntentCategory.PERMISSION_REQUEST: ["申请代码库读取权限", "请开通测试环境访问权限", "需要新增数据库只读权限"],
    IntentCategory.SECURITY_INCIDENT: ["账号疑似被盗", "发现异常登录", "电脑可能中了恶意软件"],
    IntentCategory.CHANGE_REQUEST: ["申请变更生产配置", "请安排今晚发布窗口", "需要修改防火墙规则"],
    IntentCategory.HUMAN_HANDOFF: ["转人工服务台", "我要找人工工程师", "请人工接手"],
}

_SPECIFIC_INTENTS = {
    IntentCategory.SERVICE_STATUS,
    IntentCategory.TICKET_STATUS,
    IntentCategory.SERVICE_OUTAGE,
    IntentCategory.PERFORMANCE_ISSUE,
    IntentCategory.APPLICATION_ERROR,
    IntentCategory.ACCOUNT_ACCESS,
    IntentCategory.PERMISSION_REQUEST,
    IntentCategory.SECURITY_INCIDENT,
    IntentCategory.CHANGE_REQUEST,
    IntentCategory.HUMAN_HANDOFF,
}

_GENERIC_INTENTS = {
    IntentCategory.QUERY,
    IntentCategory.REQUEST,
    IntentCategory.INCIDENT,
    IntentCategory.ACCESS,
    IntentCategory.ESCALATION,
}

_INTENT_GROUPS: Dict[IntentCategory, IntentCategory] = {
    IntentCategory.SERVICE_STATUS: IntentCategory.QUERY,
    IntentCategory.TICKET_STATUS: IntentCategory.QUERY,
    IntentCategory.SERVICE_OUTAGE: IntentCategory.INCIDENT,
    IntentCategory.PERFORMANCE_ISSUE: IntentCategory.INCIDENT,
    IntentCategory.APPLICATION_ERROR: IntentCategory.INCIDENT,
    IntentCategory.ACCOUNT_ACCESS: IntentCategory.ACCESS,
    IntentCategory.PERMISSION_REQUEST: IntentCategory.ACCESS,
    IntentCategory.SECURITY_INCIDENT: IntentCategory.ACCESS,
    IntentCategory.CHANGE_REQUEST: IntentCategory.REQUEST,
    IntentCategory.HUMAN_HANDOFF: IntentCategory.ESCALATION,
}

# 紧急关键词
_URGENCY_KEYWORDS = {
    UrgencyLevel.CRITICAL: ["紧急", "emergency", "urgent", "asap", "立刻"],
    UrgencyLevel.HIGH:     ["今天", "马上", "尽快", "hurry", "now"],
    UrgencyLevel.MEDIUM:   ["这周", "soon", "快点"],
}


def _cosine(a: List[float], b: List[float]) -> float:
    """纯 Python 余弦相似度，不依赖 numpy。"""
    dot = sum(x * y for x, y in zip(a, b))
    na  = sum(x * x for x in a) ** 0.5
    nb  = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class IntentRecognizer:
    """
    端到端意图识别器。

    初始化时不加载任何本地模型，所有 AI 能力通过 Anthropic API 调用。
    模板 Embedding 在首次请求时懒加载并缓存，后续复用。
    """

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "claude-3-5-sonnet-20241022",
        confidence_threshold: float = 0.5,
        degraded_threshold: Optional[float] = None,
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client    = AsyncAnthropic(**kwargs)
        self.model     = model
        self.threshold = confidence_threshold
        # LLM 不可用时使用的独立阈值（B+C 方案）。None 时读环境变量
        # ECHOMIND_DEGRADED_THRESHOLD，再缺省用 0.15（沿用历史离线标定，见 DEFAULT_DEGRADED_THRESHOLD）。
        self.degraded_threshold = _resolve_degraded_threshold(degraded_threshold)
        # 本地字符 n-gram 向量始终可用；如果未来客户端暴露 embeddings 资源，
        # _embed_text 会优先尝试远端向量，否则自动回退本地向量。
        self._embedding_enabled = True

        self._tpl_embeddings: Dict[IntentCategory, List[List[float]]] = {}
        self._cache: Dict[str, IntentResult] = {}
        self.cache_hits   = 0
        self.cache_misses = 0

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    async def recognize(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> IntentResult:
        """
        识别用户意图。

        history 格式：[{"role": "user"/"assistant", "content": "..."}]
        """
        key = self._cache_key(message, history)
        if key in self._cache:
            self.cache_hits += 1
            return self._cache[key]
        self.cache_misses += 1

        t0 = time.monotonic()

        # LLM 和 Embedding 并行（Embedding 不可用时跳过）
        llm_task = asyncio.create_task(self._llm_recognize(message, history))
        emb_task = asyncio.create_task(self._embedding_recognize(message)) if self._embedding_enabled else None
        pat      = self._pattern_recognize(message)

        if emb_task:
            llm, emb = await asyncio.gather(llm_task, emb_task)
        else:
            llm = await llm_task
            emb = {"intent": IntentCategory.OTHER, "confidence": 0.0}

        # 三路融合：统一走 _fuse（降级路径不再早退，见文件头说明）
        outcome = self._fuse(llm, emb, pat)
        entities = self._extract_entities(message)
        urgency  = self._urgency(message, outcome.intent)

        result = IntentResult(
            intent=outcome.intent,
            confidence=outcome.confidence,
            urgency=urgency,
            intent_group=self._intent_group(outcome.intent),
            entities=entities,
            reasoning=llm.get("reasoning", ""),
            latency_ms=(time.monotonic() - t0) * 1000,
            source_scores=outcome.source_scores,
            degraded=outcome.degraded,
            degraded_sources=list(outcome.degraded_sources),
        )

        # LRU 缓存
        if len(self._cache) >= 1000:
            for k in list(self._cache)[:500]:
                del self._cache[k]
        self._cache[key] = result
        return result

    def learn(self, message: str, correct: IntentCategory) -> None:
        """在线学习：将纠正样本加入模板，清除对应 Embedding 缓存。"""
        tpls = _TEMPLATES.setdefault(correct, [])
        if message not in tpls:
            tpls.append(message)
            self._tpl_embeddings.pop(correct, None)  # 下次重新计算
            self._cache.clear()  # 模板更新后旧缓存可能对应过时结果
            logger.info(f"学习新样本 → {correct.value}: {message[:40]}")

    # ── 三路识别策略 ──────────────────────────────────────────────────────────

    async def _llm_recognize(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]],
    ) -> Dict[str, Any]:
        """策略 1：LLM 语义理解（Few-shot + 上下文）。"""
        message = self._clean_text(message)
        # 构建 Few-shot 示例
        examples = "\n".join(
            f'  消息: "{t}" → 意图: {cat.value}'
            for cat, tpls in _TEMPLATES.items()
            for t in tpls[:1]  # 每类取 1 条，控制 prompt 长度
        )
        # 最近 3 轮对话上下文
        ctx = ""
        if history:
            ctx = "\n最近对话:\n" + "\n".join(
                f"  {self._clean_text(m.get('role', 'user'))}: {self._clean_text(m.get('content', ''))}"
                for m in history[-3:]
            )

        prompt = f"""你是企业 IT 服务台意图分析专家。根据示例判断用户意图，返回 JSON。
只有消息中存在明确证据时才选择细粒度意图；无法确定子类时返回对应的通用意图，不要为细化而猜测。
分类边界：
- 一般功能不可用、无响应或附件/音视频异常，归 incident；明确大面积中断才用 service_outage，明确缓慢/超时才用 performance_issue，明确报错/崩溃/组件加载失败才用 application_error。
- 账号资料、绑定信息、员工目录和账号生命周期管理归 access；登录认证故障用 account_access，资源授权用 permission_request，可疑盗用或攻击用 security_incident。
- 工单字段维护、催办、暂缓处理等流程动作归 request；生产配置、发布窗口、网络或防火墙策略调整才用 change_request。单纯催办不等于 escalation。
- 若主要诉求属于变更发布或权限流程，即使伴随页面异常，也优先按该业务流程分类。
例如明确服务中断返回 service_outage，明确权限申请返回 permission_request，明确登录故障返回 account_access。

        {ctx}
        用户消息: "{message}"

返回格式（仅 JSON，不要其他文字）:
{{"intent": "<意图值>", "confidence": <0-1>, "reasoning": "<一句话说明>"}}

可选意图: {", ".join(c.value for c in IntentCategory)}"""
        prompt = self._clean_text(prompt)

        try:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = extract_text_content(resp.content)
            s, e = raw.find("{"), raw.rfind("}") + 1
            data = json.loads(raw[s:e])
            try:
                data["intent"] = IntentCategory(data["intent"])
            except ValueError:
                data["intent"] = IntentCategory.OTHER
            return data
        except Exception as ex:
            logger.warning(f"LLM 识别失败: {ex}")
            return {"intent": IntentCategory.OTHER, "confidence": 0.0, "reasoning": "LLM 失败", "failed": True}

    async def _embedding_recognize(self, message: str) -> Dict[str, Any]:
        """策略 2：Embedding 向量相似度匹配。"""
        try:
            await self._load_template_embeddings()
            msg_vec = await self._embed_text(message)

            best_cat, best_score = IntentCategory.OTHER, 0.0
            for cat, vecs in self._tpl_embeddings.items():
                score = max(_cosine(msg_vec, v) for v in vecs)
                if score > best_score:
                    best_score, best_cat = score, cat

            return {"intent": best_cat, "confidence": best_score}
        except Exception as ex:
            logger.warning(f"Embedding 识别失败: {ex}")
            return {"intent": IntentCategory.OTHER, "confidence": 0.0}

    def _pattern_recognize(self, message: str) -> Dict[str, Any]:
        """策略 3：关键词模式匹配（同步，零延迟兜底）。"""
        msg = message.lower()
        specific_patterns = {
            IntentCategory.HUMAN_HANDOFF: ["转人工", "人工服务台", "人工工程师", "找人工"],
            IntentCategory.SECURITY_INCIDENT: ["账号被盗", "账户被盗", "异常登录", "恶意软件", "钓鱼邮件", "数据泄露"],
            IntentCategory.PERMISSION_REQUEST: ["申请权限", "开通权限", "权限申请", "只读权限", "访问权限", "授权"],
            IntentCategory.ACCOUNT_ACCESS: ["无法登录", "登录失败", "重置密码", "忘记密码", "401", "mfa", "验证码"],
            IntentCategory.SERVICE_OUTAGE: ["服务中断", "全部无法访问", "所有用户", "大面积故障", "无法连接", "outage"],
            IntentCategory.PERFORMANCE_ISSUE: ["响应慢", "特别慢", "延迟高", "超时", "卡顿", "performance"],
            IntentCategory.APPLICATION_ERROR: ["崩溃", "闪退", "500", "报错", "错误码", "crash"],
            IntentCategory.CHANGE_REQUEST: ["变更申请", "配置变更", "发布窗口", "防火墙规则", "change request"],
            IntentCategory.TICKET_STATUS: ["工单状态", "工单进度", "处理到哪", "分派工程师", "inc-", "req-"],
            IntentCategory.SERVICE_STATUS: ["服务状态", "运行状态", "是否正常", "状态页", "service status"],
        }
        generic_patterns = {
            IntentCategory.ESCALATION: ["升级处理", "值班经理", "二线支持", "supervisor"],
            IntentCategory.COMPLAINT: ["太差", "糟糕", "horrible", "等了很久", "没人跟进"],
            IntentCategory.QUERY: ["?", "？", "怎么", "什么", "如何", "status"],
            IntentCategory.REQUEST: ["帮我", "需要", "申请", "please", "help"],
            IntentCategory.GREETING: ["你好", "嗨", "hello", "hi"],
            IntentCategory.INCIDENT: ["故障", "异常", "不能用", "error", "incident"],
            IntentCategory.ACCESS: ["账号", "账户", "密码", "权限", "account"],
            IntentCategory.FEEDBACK: ["满意", "感谢", "很棒", "好评"],
        }

        best_cat, best_score = self._best_pattern_match(msg, specific_patterns)
        if best_cat != IntentCategory.OTHER:
            return {"intent": best_cat, "confidence": best_score}

        best_cat, best_score = self._best_pattern_match(msg, generic_patterns)
        return {"intent": best_cat, "confidence": best_score}

    # ── 投票合并（B+C：降级不再早退 + 权重归一化 + 独立阈值）──────────────────

    def _vote(self, llm: Dict, emb: Dict, pat: Dict) -> tuple[IntentCategory, float, Dict[str, float]]:
        """兼容入口：返回 (最终意图, 融合置信度, 各路来源得分)。

        签名保持不变（既有测试与离线 harness 依赖三元组），融合逻辑全部在
        :meth:`_fuse`；需要 degraded / weights_used 的调用方直接用 ``_fuse``。
        """
        outcome = self._fuse(llm, emb, pat)
        return outcome.intent, outcome.confidence, outcome.source_scores

    def _fuse(self, llm: Dict, emb: Dict, pat: Dict) -> FusionOutcome:
        """三路融合（纯函数，不读写请求级状态）。

        正常路径（LLM 可用）：完全沿用改动前的权重与阈值，保证输出逐位一致——
            llm 0.7 / embedding 0.2 / pattern 0.1（``_embedding_enabled=False`` 时 0.85 / 0.15）
            阈值 ``self.threshold``（默认 0.5）。

        降级路径（LLM 不可用）：不再早退采信单路结果，而是
            1. 只在**可用源**上重新归一化基础权重（embedding 0.2 / pattern 0.1）；
            2. 使用独立阈值 ``self.degraded_threshold``（默认 0.15，沿用历史标定值）；
            3. 融合分仍低于阈值时返回 OTHER，让上层澄清逻辑可以触发。
        """
        llm_conf = float(llm.get("confidence", 0.0) or 0.0)
        emb_conf = float(emb.get("confidence", 0.0) or 0.0)
        pat_conf = float(pat.get("confidence", 0.0) or 0.0)

        llm_ok = not bool(llm.get("failed"))
        emb_ok = emb.get("intent") != IntentCategory.OTHER and emb_conf > 0
        pat_ok = pat.get("intent") != IntentCategory.OTHER and pat_conf > 0

        source_scores: Dict[str, float] = {
            "llm": llm_conf,
            "embedding": emb_conf,
            "pattern": pat_conf,
        }

        # plan 的元素：(某一路的输出 dict, 该路置信度, 生效权重)
        plan: List[tuple] = []
        degraded_sources: List[str] = []

        if llm_ok:
            degraded = False
            threshold = float(getattr(self, "threshold", 0.5))
            if getattr(self, "_embedding_enabled", True):
                weights_used: Dict[str, float] = {"llm": 0.7, "embedding": 0.2, "pattern": 0.1}
                plan = [(llm, llm_conf, 0.7), (emb, emb_conf, 0.2), (pat, pat_conf, 0.1)]
            else:
                weights_used = {"llm": 0.85, "pattern": 0.15}
                plan = [(llm, llm_conf, 0.85), (pat, pat_conf, 0.15)]
        else:
            degraded = True
            threshold = float(getattr(self, "degraded_threshold", DEFAULT_DEGRADED_THRESHOLD))
            available: Dict[str, float] = {}
            if emb_ok:
                available["embedding"] = 0.2
            if pat_ok:
                available["pattern"] = 0.1
            total = sum(available.values())
            if total <= 0:
                # 所有源都不可用：显式降级为 OTHER，交给上层澄清/追问
                source_scores["degraded"] = 1.0
                source_scores["confidence_threshold"] = round(threshold, 6)
                return FusionOutcome(
                    intent=IntentCategory.OTHER,
                    confidence=0.0,
                    source_scores=source_scores,
                    degraded=True,
                    weights_used={},
                    degraded_sources=["llm", "embedding", "pattern"],
                )
            weights_used = {name: weight / total for name, weight in available.items()}
            if "embedding" in weights_used:
                plan.append((emb, emb_conf, weights_used["embedding"]))
            if "pattern" in weights_used:
                plan.append((pat, pat_conf, weights_used["pattern"]))
            degraded_sources = ["llm"]
            if not emb_ok:
                degraded_sources.append("embedding")
            if not pat_ok:
                degraded_sources.append("pattern")

        scores: Dict[IntentCategory, float] = {}
        group_scores: Dict[IntentCategory, float] = {}
        for result, conf, w in plan:
            cat  = result.get("intent", IntentCategory.OTHER)
            weighted_score = w * conf
            scores[cat] = scores.get(cat, 0.0) + weighted_score
            group = _INTENT_GROUPS.get(cat, cat)
            group_scores[group] = group_scores.get(group, 0.0) + weighted_score

        best_group = max(group_scores, key=group_scores.get)
        best_score = group_scores[best_group]
        best = max(
            (cat for cat in scores if _INTENT_GROUPS.get(cat, cat) == best_group),
            key=scores.get,
        )

        pat_intent = pat.get("intent", IntentCategory.OTHER)
        source_scores["degraded"] = 1.0 if degraded else 0.0
        source_scores["confidence_threshold"] = round(threshold, 6)
        if (best in _GENERIC_INTENTS and pat_intent in _SPECIFIC_INTENTS
                and _INTENT_GROUPS.get(pat_intent, pat_intent) == best_group
                and pat_conf >= 0.5 and best_score < 0.8):
            source_scores["refined_by_pattern"] = pat_conf
            return FusionOutcome(
                intent=pat_intent,
                confidence=max(best_score, pat_conf),
                source_scores=source_scores,
                degraded=degraded,
                weights_used=weights_used,
                degraded_sources=degraded_sources,
            )
        if best_score < threshold:
            return FusionOutcome(
                intent=IntentCategory.OTHER,
                confidence=best_score,
                source_scores=source_scores,
                degraded=degraded,
                weights_used=weights_used,
                degraded_sources=degraded_sources,
            )
        return FusionOutcome(
            intent=best,
            confidence=best_score,
            source_scores=source_scores,
            degraded=degraded,
            weights_used=weights_used,
            degraded_sources=degraded_sources,
        )

    # ── 实体提取 ──────────────────────────────────────────────────────────────

    def _extract_entities(self, message: str) -> Dict[str, List[str]]:
        """用规则提取高价值实体，避免每次识别都额外调用 LLM。"""
        message = self._clean_text(message)
        return {
            "ticket_id": self._unique(
                re.findall(r"\b((?:INC|REQ|CHG|PRB)-[A-Za-z0-9-]{2,28})\b", message, re.I)
                + re.findall(r"(?:工单号?|ticket(?:_id)?)\s*[:：#]*\s*([A-Za-z0-9_-]{4,32})", message, re.I)
            ),
            "service": self._unique(
                re.findall(r"([A-Za-z][A-Za-z0-9_.-]{1,39})\s*(?:服务|系统|应用|平台)", message, re.I)
            ),
            "account": self._unique(
                re.findall(r"(?:账号|账户|用户|account)\s*[:：]?\s*([A-Za-z0-9_.@-]{2,64})", message, re.I)
            ),
            "date": self._unique(re.findall(r"(今天|明天|昨天|本周|这周|下周|\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)", message)),
            "error_code": self._unique(
                re.findall(r"(?:error(?:_code)?|错误码|状态码|http)\s*[:：#]?\s*([45]\d{2})\b", message, re.I)
                + re.findall(r"\b([45]\d{2})\b", message)
            ),
        }

    # ── 辅助 ──────────────────────────────────────────────────────────────────

    async def _load_template_embeddings(self) -> None:
        """懒加载所有模板的 Embedding（只在首次调用时执行）。"""
        missing = [cat for cat in _TEMPLATES if cat not in self._tpl_embeddings]
        if not missing:
            return

        all_texts = [t for cat in missing for t in _TEMPLATES[cat]]
        vecs = [await self._embed_text(text) for text in all_texts]
        idx = 0
        for cat in missing:
            n = len(_TEMPLATES[cat])
            self._tpl_embeddings[cat] = vecs[idx: idx + n]
            idx += n

    async def _embed_text(self, text: str) -> List[float]:
        """
        生成文本向量。

        如果未来接入的官方/兼容客户端提供 embeddings.create，会优先使用远端向量；
        当前 Anthropic SDK 没有该资源时，退化为字符 n-gram 哈希向量。这样不会因为
        Embedding 服务缺失导致三路融合中断。
        """
        embeddings = getattr(self.client, "embeddings", None)
        if embeddings is not None:
            try:
                resp = await embeddings.create(model="voyage-3-lite", input=[text])
                return list(resp.data[0].embedding)
            except Exception as ex:
                logger.warning(f"远端 Embedding 失败，使用本地向量兜底: {ex}")

        return self._local_embedding(text)

    @staticmethod
    def _local_embedding(text: str, dims: int = 256) -> List[float]:
        """稳定的字符 n-gram 哈希向量，用于无远端 Embedding 时的语义近似匹配。"""
        normalized = text.lower().strip()
        vec = [0.0] * dims
        tokens = set()
        for n in (1, 2, 3):
            if len(normalized) >= n:
                tokens.update(normalized[i:i + n] for i in range(len(normalized) - n + 1))
        if not tokens:
            tokens.add(normalized)

        for token in tokens:
            digest = hashlib.md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % dims
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        return vec

    def _urgency(self, message: str, intent: IntentCategory) -> UrgencyLevel:
        msg = message.lower()
        for level, kws in _URGENCY_KEYWORDS.items():
            if any(kw in msg for kw in kws):
                return level
        if intent in (IntentCategory.SERVICE_OUTAGE, IntentCategory.SECURITY_INCIDENT):
            return UrgencyLevel.CRITICAL
        if intent in (
            IntentCategory.ESCALATION,
            IntentCategory.HUMAN_HANDOFF,
            IntentCategory.INCIDENT,
            IntentCategory.APPLICATION_ERROR,
        ):
            return UrgencyLevel.HIGH
        if intent in (IntentCategory.COMPLAINT, IntentCategory.PERFORMANCE_ISSUE):
            return UrgencyLevel.MEDIUM
        return UrgencyLevel.LOW

    def _cache_key(self, message: str, history: Optional[List[Dict[str, str]]] = None) -> str:
        payload = {"message": self._clean_text(message)[:200]}
        if history:
            payload["history"] = [
                {
                    "role": self._clean_text(item.get("role", ""))[:20],
                    "content": self._clean_text(item.get("content", ""))[:160],
                }
                for item in history[-3:]
            ]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _unique(values: List[str]) -> List[str]:
        return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))

    @staticmethod
    def _best_pattern_match(
        message: str,
        patterns: Dict[IntentCategory, List[str]],
    ) -> tuple[IntentCategory, float]:
        best_cat, best_score = IntentCategory.OTHER, 0.0
        for cat, kws in patterns.items():
            hits = sum(1 for kw in kws if kw in message)
            if not hits:
                continue
            # 单个明确业务关键词就给可用置信度；多个关键词命中时提高置信度。
            score = min(1.0, 0.5 + 0.25 * (hits - 1))
            if score > best_score:
                best_score, best_cat = score, cat
        return best_cat, best_score

    @staticmethod
    def _intent_group(intent: IntentCategory) -> str:
        return _INTENT_GROUPS.get(intent, intent).value

    @staticmethod
    def _clean_text(value: Any) -> str:
        """移除 Unicode 代理字符，避免 HTTP 客户端编码 prompt 时崩溃。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @property
    def cache_stats(self) -> Dict[str, Any]:
        total = self.cache_hits + self.cache_misses
        return {
            "size": len(self._cache),
            "hits": self.cache_hits,
            "misses": self.cache_misses,
            "hit_rate": self.cache_hits / total if total else 0.0,
        }
