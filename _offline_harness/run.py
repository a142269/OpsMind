#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OpsMind offline evaluation harness.

Runs the project's *real* pure-Python logic on a machine with **no network and
no third-party dependencies** by injecting import stubs (``anthropic``, ...)
ahead of the project root on ``sys.path`` and then importing the real source
modules.  The LLM path is deliberately forced to fail (``anthropic`` stub
raises on every ``messages.create``), so every number below comes from the
project's own deterministic / degradation branches.

Usage (from the repository root)::

    python EchoMind/_offline_harness/run.py
    python EchoMind/_offline_harness/run.py --cases EchoMind/data/eval/intent_cases.json
    python EchoMind/_offline_harness/run.py --json-out a.json --md-out a.md

Exit code 0 means the harness completed; it does NOT mean the model is good.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import hashlib
import importlib
import json
import logging
import os
import platform
import re
import statistics
import subprocess
import sys
import time
import types
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# sys.path injection: stubs FIRST, real project root SECOND
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
STUBS_DIR = HERE / "stubs"
FIXTURES_DIR = HERE / "fixtures"
REPORTS_DIR = HERE / "reports"
DEFAULT_CASES_PATH = PROJECT_ROOT / "data" / "eval" / "intent_cases.json"
DEFAULT_JSON_OUT = REPORTS_DIR / "offline_metrics.json"
DEFAULT_MD_OUT = REPORTS_DIR / "offline_metrics.md"

sys.path.insert(0, str(STUBS_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

logging.disable(logging.ERROR)  # project warnings are counted explicitly instead


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Import real project modules (with stubbed third-party dependencies)
# ---------------------------------------------------------------------------
import anthropic  # noqa: E402  (stub module, verified below)
import mcp.tool_manager as tool_manager_module  # noqa: E402
from agents.agent_orchestrator import (  # noqa: E402
    AgentOrchestrator,
    AgentType,
    Request,
)
from core.intent_recognizer import (  # noqa: E402
    IntentCategory,
    IntentRecognizer,
    UrgencyLevel,
    _GENERIC_INTENTS,
    _INTENT_GROUPS,
    _SPECIFIC_INTENTS,
    _TEMPLATES,
)
from core.skill_loader import SkillManager  # noqa: E402
from mcp.tool_manager import (  # noqa: E402
    CircuitBreaker,
    CircuitState,
    MCPToolManager,
    Tool,
)

REAL_IMPORT_MODULES = [
    "core.intent_recognizer",
    "core.llm_utils",
    "core.skill_loader",
    "agents.tools",
    "agents.agent_orchestrator",
    "mcp.tool_manager",
    "evaluation.evaluator",
]

VALID_INTENTS = {category.value for category in IntentCategory}

# ---------------------------------------------------------------------------
# Harness-declared expectation tables (documented in the report)
# ---------------------------------------------------------------------------
# Intent -> expected primary Agent. Declared from OpsMind business semantics;
# it only generates comparable expectation labels and is not imported into routing.
EXPECTED_AGENT_BY_INTENT: Dict[str, str] = {
    "incident": "incident",
    "service_outage": "incident",
    "performance_issue": "incident",
    "application_error": "incident",
    "access": "access",
    "account_access": "access",
    "permission_request": "access",
    "security_incident": "access",
    "escalation": "escalation",
    "human_handoff": "escalation",
    # remaining intents fall back to service_desk
}
DEFAULT_AGENT = "service_desk"

# Harness lexicon for deciding whether a message covers both incident and access
# domains (used only to score multi-agent routing).
INCIDENT_DOMAIN_TERMS = [
    "故障", "中断", "崩溃", "报错", "错误码", "响应慢", "延迟", "超时",
    "404", "500", "502", "接口", "页面打不开", "服务不可用", "vpn 连不上",
]
ACCESS_DOMAIN_TERMS = [
    "无法登录", "登录失败", "账号", "账户", "密码", "验证码", "mfa", "401",
    "403", "权限", "授权", "异常登录", "钓鱼", "token",
]


def expected_agent(intent_value: str) -> str:
    return EXPECTED_AGENT_BY_INTENT.get(intent_value, DEFAULT_AGENT)


def expected_group(intent_value: str) -> str:
    """Intent group via the project's real _INTENT_GROUPS mapping."""
    try:
        category = IntentCategory(intent_value)
    except ValueError:
        return "unknown"
    return _INTENT_GROUPS.get(category, category).value


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def rel(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def md_cell(value: Any) -> str:
    text = str(value)
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def fmt(value: Optional[float], digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def ratio(numerator: int, denominator: int) -> Optional[float]:
    return numerator / denominator if denominator else None


def normalize_text(text: str) -> str:
    return re.sub(r"[\s?？!！。.,，、:：;；\"'“”‘’()（）\[\]【】]+", "", (text or "").lower())


class FakeClock:
    """Injectable monotonic clock; the harness never sleeps in real time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


@contextlib.contextmanager
def patched_module_time(module: Any, clock: FakeClock):
    """Replace ``module.time`` with a fake clock, restoring it afterwards."""
    original = module.time
    module.time = types.SimpleNamespace(monotonic=clock.monotonic)
    try:
        yield clock
    finally:
        module.time = original


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------
def classification_metrics(
    y_true: Sequence[str],
    y_pred: Sequence[str],
    positive_labels: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Accuracy / macro-F1 / per-class P-R-F1 / confusion counts.

    Macro-F1 averages over ``y_true ∪ y_pred`` (sklearn-default behaviour);
    labels that were never predicted still contribute F1=0, which prevents a
    model that simply never emits a class from looking good.
    """
    if len(y_true) != len(y_pred):
        raise ValueError("y_true / y_pred length mismatch")
    n = len(y_true)
    labels = sorted(set(y_true) | set(y_pred)) if positive_labels is None else list(positive_labels)
    correct = sum(1 for truth, pred in zip(y_true, y_pred) if truth == pred)

    per_class: Dict[str, Dict[str, Any]] = {}
    for label in labels:
        tp = sum(1 for truth, pred in zip(y_true, y_pred) if truth == label and pred == label)
        fp = sum(1 for truth, pred in zip(y_true, y_pred) if truth != label and pred == label)
        fn = sum(1 for truth, pred in zip(y_true, y_pred) if truth == label and pred != label)
        support = sum(1 for truth in y_true if truth == label)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        per_class[label] = {
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
            "support": support,
            "tp": tp,
            "fp": fp,
            "fn": fn,
        }

    f1_values = [entry["f1"] for entry in per_class.values()]
    macro_f1 = statistics.mean(f1_values) if f1_values else 0.0
    supported = [entry["f1"] for entry in per_class.values() if entry["support"] > 0]
    macro_f1_supported = statistics.mean(supported) if supported else 0.0

    confusion: Dict[str, Dict[str, int]] = {}
    for truth, pred in zip(y_true, y_pred):
        confusion.setdefault(truth, {})
        confusion[truth][pred] = confusion[truth].get(pred, 0) + 1

    return {
        "n": n,
        "correct": correct,
        "accuracy": round(correct / n, 6) if n else 0.0,
        "macro_f1": round(macro_f1, 6),
        "macro_f1_supported": round(macro_f1_supported, 6),
        "labels": labels,
        "per_class": per_class,
        "confusion": confusion,
    }


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------
@dataclass
class Case:
    id: str
    message: str
    expected_intent: str
    expected_group: Optional[str] = None
    difficulty: Optional[str] = None
    note: Optional[str] = None
    expected_entities: Dict[str, List[str]] = field(default_factory=dict)
    expected_urgency: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Dataset:
    name: str
    path: Optional[Path]
    source: str
    cases: List[Case]
    sha256: Optional[str] = None
    load_warnings: List[str] = field(default_factory=list)


def load_case_file(path: Path, name: Optional[str] = None) -> Dataset:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("cases") or payload.get("data") or payload.get("items") or []
    if not isinstance(payload, list):
        raise ValueError(f"{path}: expected a JSON array of intent cases")

    cases: List[Case] = []
    warnings: List[str] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            warnings.append(f"case[{index}] 不是对象，已跳过")
            continue
        message = str(item.get("message") or item.get("query") or item.get("text") or "").strip()
        intent = str(
            item.get("expected_intent") or item.get("expected") or item.get("label") or ""
        ).strip()
        if not message:
            warnings.append(f"case[{index}] 缺少 message，已跳过")
            continue
        if intent not in VALID_INTENTS:
            warnings.append(f"case[{index}] expected_intent={intent!r} 非法，已跳过")
            continue
        entities = item.get("expected_entities") or item.get("entities") or {}
        if not isinstance(entities, dict):
            entities = {}
        cases.append(
            Case(
                id=str(item.get("id") or f"case{index + 1:03d}"),
                message=message,
                expected_intent=intent,
                expected_group=item.get("expected_group"),
                difficulty=item.get("difficulty"),
                note=item.get("note"),
                expected_entities={str(k): list(v or []) for k, v in entities.items()},
                expected_urgency=item.get("expected_urgency"),
                raw=item,
            )
        )
    if not cases:
        raise ValueError(f"{path}: 没有可用的意图用例")

    dataset_name = name or f"{path.stem}_{len(cases)}"
    return Dataset(
        name=dataset_name,
        path=path,
        source=f"外部数据集 JSON ({rel(path)})",
        cases=cases,
        sha256=sha256_file(path),
        load_warnings=warnings,
    )


def load_builtin_cases() -> Dataset:
    """Built-in 11 cases, imported from the real evaluation/evaluator.py."""
    evaluator_path = PROJECT_ROOT / "evaluation" / "evaluator.py"
    warnings: List[str] = []
    source = "evaluation/evaluator.py::DEFAULT_INTENT_CASES (imported)"
    raw_cases: List[Tuple[str, str]] = []
    try:
        evaluator = importlib.import_module("evaluation.evaluator")
        for case in getattr(evaluator, "DEFAULT_INTENT_CASES", []):
            raw_cases.append((str(case.message), str(case.expected_intent)))
    except Exception as ex:  # pragma: no cover - only if the file is mid-edit
        warnings.append(f"import evaluation.evaluator 失败，改用源码解析兜底: {type(ex).__name__}: {ex}")
        source = "evaluation/evaluator.py::DEFAULT_INTENT_CASES (source-parsed fallback)"
        text = evaluator_path.read_text(encoding="utf-8")
        match = re.search(r"DEFAULT_INTENT_CASES[^=]*=\s*\[(.*?)\n\]", text, re.S)
        block = match.group(1) if match else ""
        raw_cases = re.findall(r'IntentTestCase\(\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\)', block)
    cases = [
        Case(id=f"B{index + 1:02d}", message=message, expected_intent=intent)
        for index, (message, intent) in enumerate(raw_cases)
        if message and intent in VALID_INTENTS
    ]
    if not cases:
        raise RuntimeError("无法加载 DEFAULT_INTENT_CASES（import 与源码解析都失败）")
    return Dataset(
        name=f"builtin_{len(cases)}",
        path=evaluator_path,
        source=source,
        cases=cases,
        sha256=sha256_file(evaluator_path),
        load_warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
def module_provenance() -> Dict[str, Any]:
    modules: Dict[str, Any] = OrderedDict()
    for name in REAL_IMPORT_MODULES:
        entry: Dict[str, Any] = {"name": name}
        try:
            module = importlib.import_module(name)
            path = Path(getattr(module, "__file__", "") or "").resolve()
            is_real = str(path).startswith(str(STUBS_DIR)) is False and str(path).startswith(
                str(PROJECT_ROOT)
            )
            entry.update(
                {
                    "imported": True,
                    "file": rel(path),
                    "is_project_source": bool(is_real),
                    "sha256": sha256_file(path) if path.is_file() else None,
                }
            )
        except Exception as ex:
            entry.update({"imported": False, "error": f"{type(ex).__name__}: {ex}"})
        modules[name] = entry

    stub_path = Path(anthropic.__file__).resolve()
    stub_active = str(stub_path).startswith(str(STUBS_DIR))
    return {
        "modules": modules,
        "stub": {
            "module": "anthropic",
            "file": str(stub_path),
            "active": stub_active,
            "sdk_marker": bool(getattr(anthropic, "STUB_SDK", False)),
        },
        "stub_files": sorted(rel(path) for path in STUBS_DIR.rglob("*.py")),
    }


def git_provenance() -> Dict[str, Any]:
    def run(*args: str) -> Optional[str]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(PROJECT_ROOT), *args],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if completed.returncode != 0:
                return None
            return completed.stdout.strip()
        except Exception:
            return None

    status = run("status", "--porcelain")
    return {
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "head": run("rev-parse", "HEAD"),
        "dirty_files": status.splitlines()[:80] if status else [],
    }


# ---------------------------------------------------------------------------
# Intent analysis
# ---------------------------------------------------------------------------
@dataclass
class CaseObservation:
    case: Case
    pattern: Dict[str, Any]
    embedding: Dict[str, Any]
    fused_intent: str
    fused_confidence: float
    fused_group: str
    fused_urgency: "UrgencyLevel"
    fused_entities: Dict[str, List[str]]
    source_scores: Dict[str, float]
    latency_ms: float


def leakage_stats(dataset: Dataset) -> Dict[str, Any]:
    template_sentences = [sentence for sentences in _TEMPLATES.values() for sentence in sentences]
    template_set = set(template_sentences)
    template_normalized = {normalize_text(sentence) for sentence in template_sentences}
    exact_hits: List[str] = []
    normalized_hits: List[str] = []
    for case in dataset.cases:
        if case.message in template_set:
            exact_hits.append(case.message)
        if normalize_text(case.message) in template_normalized:
            normalized_hits.append(case.message)
    return {
        "template_categories": len(_TEMPLATES),
        "template_sentences": len(template_sentences),
        "n": len(dataset.cases),
        "exact_overlap": len(exact_hits),
        "exact_overlap_ratio": ratio(len(exact_hits), len(dataset.cases)),
        "normalized_overlap": len(normalized_hits),
        "normalized_overlap_ratio": ratio(len(normalized_hits), len(dataset.cases)),
        "examples": normalized_hits[:8],
    }


async def observe_dataset(dataset: Dataset, recognizer: IntentRecognizer) -> List[CaseObservation]:
    observations: List[CaseObservation] = []
    for case in dataset.cases:
        pattern = recognizer._pattern_recognize(case.message)
        embedding = await recognizer._embedding_recognize(case.message)
        result = await recognizer.recognize(case.message)
        observations.append(
            CaseObservation(
                case=case,
                pattern=pattern,
                embedding=embedding,
                fused_intent=result.intent.value,
                fused_confidence=float(result.confidence),
                fused_group=result.intent_group,
                fused_urgency=result.urgency,
                fused_entities=dict(result.entities),
                source_scores=dict(result.source_scores),
                latency_ms=float(result.latency_ms),
            )
        )
    return observations


def weighted_branch_ablation(
    recognizer: IntentRecognizer, observations: List[CaseObservation]
) -> Dict[str, Any]:
    """Exercise the real 0.85/0.15 ``_vote`` branch with an absent LLM signal.

    The measured code only reaches ``weights = [(llm, 0.85), (pat, 0.15)]`` when
    ``_embedding_enabled`` is False AND the LLM result is not flagged
    ``failed``.  Offline the LLM *always* fails, so the real pipeline takes the
    ``failed`` early-return instead.  This ablation feeds the honest "LLM
    signal = 0.0, no failed flag" input into the real ``_vote`` to measure what
    that branch would do without an LLM.
    """
    saved = recognizer._embedding_enabled
    recognizer._embedding_enabled = False
    try:
        predictions: List[str] = []
        confidences: List[float] = []
        for observation in observations:
            llm_abstain = {
                "intent": IntentCategory.OTHER,
                "confidence": 0.0,
                "reasoning": "LLM unavailable offline; signal=0.0",
            }
            intent, confidence, _scores = recognizer._vote(
                llm_abstain, observation.embedding, observation.pattern
            )
            predictions.append(intent.value)
            confidences.append(round(float(confidence), 6))
    finally:
        recognizer._embedding_enabled = saved
    y_true = [observation.case.expected_intent for observation in observations]
    metrics = classification_metrics(y_true, predictions)
    metrics["branch"] = "weights=[(llm,0.85),(pattern,0.15)]; llm_intent=other, llm_conf=0.0, failed flag absent"
    metrics["note"] = "ablation only: real offline pipeline takes the llm.failed early-return instead"
    metrics["predicted_other"] = sum(1 for prediction in predictions if prediction == IntentCategory.OTHER.value)
    metrics["always_predicts_other"] = metrics["predicted_other"] == len(predictions)
    metrics["confidence_stats"] = {
        "min": min(confidences) if confidences else None,
        "max": max(confidences) if confidences else None,
        "mean": round(statistics.mean(confidences), 6) if confidences else None,
    }
    return metrics


def lane_agreement(observations: List[CaseObservation]) -> Dict[str, Any]:
    n = len(observations)
    stats = {
        "n": n,
        "pattern_equals_embedding": sum(
            1 for obs in observations if obs.pattern.get("intent") == obs.embedding.get("intent")
        ),
        "fused_equals_embedding": sum(
            1 for obs in observations if obs.fused_intent == getattr(obs.embedding.get("intent"), "value", None)
        ),
        "fused_equals_pattern": sum(
            1 for obs in observations if obs.fused_intent == getattr(obs.pattern.get("intent"), "value", None)
        ),
        "fused_is_other": sum(1 for obs in observations if obs.fused_intent == IntentCategory.OTHER.value),
        "llm_confidence_all_zero": sum(
            1 for obs in observations if float(obs.source_scores.get("llm", 0.0)) == 0.0
        ),
    }
    stats["fused_equals_embedding_ratio"] = ratio(stats["fused_equals_embedding"], n)
    stats["fused_equals_pattern_ratio"] = ratio(stats["fused_equals_pattern"], n)
    pattern_correct = sum(
        1
        for obs in observations
        if getattr(obs.pattern.get("intent"), "value", None) == obs.case.expected_intent
    )
    embedding_correct = sum(
        1
        for obs in observations
        if getattr(obs.embedding.get("intent"), "value", None) == obs.case.expected_intent
    )
    pattern_correct_fused_wrong = sum(
        1
        for obs in observations
        if getattr(obs.pattern.get("intent"), "value", None) == obs.case.expected_intent
        and obs.fused_intent != obs.case.expected_intent
    )
    union_correct = sum(
        1
        for obs in observations
        if obs.fused_intent == obs.case.expected_intent
        or getattr(obs.pattern.get("intent"), "value", None) == obs.case.expected_intent
    )
    stats.update(
        {
            "pattern_correct": pattern_correct,
            "embedding_correct": embedding_correct,
            "pattern_correct_but_fused_wrong": pattern_correct_fused_wrong,
            "pattern_correct_but_fused_wrong_ratio": ratio(pattern_correct_fused_wrong, n),
            "union_pattern_or_fused_correct": union_correct,
            "union_pattern_or_fused_accuracy": ratio(union_correct, n),
            "union_note": (
                "诊断上界（不是被测实现的输出）：若低置信度时回退 pattern 路，正确数最多为两者并集；"
                "该数字只说明被降级分支丢掉了多少可用信号。"
            ),
        }
    )
    return stats


def degradation_diagnostics(
    observations: List[CaseObservation], recognizer: IntentRecognizer
) -> Dict[str, Any]:
    """Quantify how the LLM-failure branch actually behaves.

    In ``_vote`` the ``llm.get("failed")`` branch returns the embedding result
    *without* comparing it to ``confidence_threshold``.  When the embedding lane
    always produces some positive similarity, that means the threshold and the
    OTHER fallback never trigger -- which is a measurable property, not a claim.
    """
    threshold = float(recognizer.threshold)
    confidences = [observation.fused_confidence for observation in observations]
    below = [value for value in confidences if value < threshold]
    bypassed = [
        observation
        for observation in observations
        if observation.fused_intent != IntentCategory.OTHER.value
        and observation.fused_confidence < threshold
    ]
    predicted_other = [
        observation for observation in observations if observation.fused_intent == IntentCategory.OTHER.value
    ]
    embedding_non_other = sum(
        1
        for observation in observations
        if getattr(observation.embedding.get("intent"), "value", None) != IntentCategory.OTHER.value
    )
    pattern_non_other = sum(
        1
        for observation in observations
        if getattr(observation.pattern.get("intent"), "value", None) != IntentCategory.OTHER.value
    )
    return {
        "n": len(observations),
        "confidence_threshold": threshold,
        "fused_confidence": {
            "min": round(min(confidences), 6) if confidences else None,
            "max": round(max(confidences), 6) if confidences else None,
            "mean": round(statistics.mean(confidences), 6) if confidences else None,
            "median": round(statistics.median(confidences), 6) if confidences else None,
        },
        "fused_below_threshold": len(below),
        "fused_below_threshold_ratio": ratio(len(below), len(observations)),
        "threshold_bypassed_low_confidence_non_other": len(bypassed),
        "predicted_other": len(predicted_other),
        "embedding_non_other": embedding_non_other,
        "pattern_non_other": pattern_non_other,
        "finding": (
            "LLM 失败的早退分支直接返回 embedding 结果，不比较 confidence_threshold；"
            "当 embedding 总有正相似度时，OTHER/澄清/低置信度降级都不会发生。"
        ),
    }


def misclassified_details(observations: List[CaseObservation]) -> List[Dict[str, Any]]:
    details: List[Dict[str, Any]] = []
    for observation in observations:
        if observation.fused_intent == observation.case.expected_intent:
            continue
        details.append(
            {
                "id": observation.case.id,
                "message": observation.case.message,
                "expected": observation.case.expected_intent,
                "predicted": observation.fused_intent,
                "confidence": round(observation.fused_confidence, 6),
                "urgency": observation.fused_urgency.name,
                "difficulty": observation.case.difficulty,
                "note": observation.case.note,
                "lanes": {
                    "llm": {
                        "intent": "other",
                        "confidence": round(float(observation.source_scores.get("llm", 0.0)), 6),
                        "reasoning": "LLM unavailable offline (stub raises)",
                    },
                    "embedding": {
                        "intent": getattr(observation.embedding.get("intent"), "value", None),
                        "confidence": round(
                            float(observation.embedding.get("confidence", 0.0) or 0.0), 6
                        ),
                    },
                    "pattern": {
                        "intent": getattr(observation.pattern.get("intent"), "value", None),
                        "confidence": round(
                            float(observation.pattern.get("confidence", 0.0) or 0.0), 6
                        ),
                    },
                },
                "source_scores": {
                    key: round(float(value), 6) for key, value in observation.source_scores.items()
                },
            }
        )
    return details


def urgency_analysis(observations: List[CaseObservation]) -> Dict[str, Any]:
    distribution = Counter(observation.fused_urgency.name for observation in observations)
    keyword_hits = 0
    for observation in observations:
        lowered = observation.case.message.lower()
        if any(
            keyword in lowered
            for keywords in (
                ["紧急", "emergency", "urgent", "asap", "立刻"],
                ["今天", "马上", "尽快", "hurry", "now"],
                ["这周", "soon", "快点"],
            )
            for keyword in keywords
        ):
            keyword_hits += 1
    labeled = [
        observation
        for observation in observations
        if observation.case.expected_urgency in {level.name for level in UrgencyLevel}
    ]
    accuracy = None
    correct = 0
    if labeled:
        correct = sum(
            1
            for observation in labeled
            if observation.fused_urgency.name == observation.case.expected_urgency
        )
        accuracy = round(correct / len(labeled), 6)
    return {
        "labeled": bool(labeled),
        "n": len(observations),
        "n_labeled": len(labeled),
        "correct": correct,
        "accuracy": accuracy,
        "distribution": {
            level.name: {
                "count": int(distribution.get(level.name, 0)),
                "ratio": ratio(int(distribution.get(level.name, 0)), len(observations)),
            }
            for level in UrgencyLevel
        },
        "explicit_urgency_keyword_hits": keyword_hits,
        "explicit_urgency_keyword_ratio": ratio(keyword_hits, len(observations)),
    }


# ---------------------------------------------------------------------------
# Routing analysis
# ---------------------------------------------------------------------------
def analyze_routing(
    dataset: Dataset,
    observations: List[CaseObservation],
    orchestrator: AgentOrchestrator,
) -> Dict[str, Any]:
    cases = [observation.case for observation in observations]
    end_to_end_expected: List[str] = []
    end_to_end_actual: List[str] = []
    oracle_expected: List[str] = []
    oracle_actual: List[str] = []
    pure_expected: List[str] = []
    pure_actual: List[str] = []

    multi_expected: List[bool] = []
    multi_actual: List[bool] = []
    clarification_actual: List[bool] = []
    clarification_expected: List[bool] = []
    misrouted: List[Dict[str, Any]] = []
    multi_details: List[Dict[str, Any]] = []
    clarification_details: List[Dict[str, Any]] = []
    agent_confusion: List[Dict[str, Any]] = []

    for observation in observations:
        case = observation.case
        expected_agent_value = expected_agent(case.expected_intent)

        predicted_request = Request(
            message=case.message,
            user_id="offline_user",
            conv_id="offline_conv",
            intent=IntentCategory(observation.fused_intent),
            intent_group=observation.fused_group,
            urgency=observation.fused_urgency,
            intent_confidence=observation.fused_confidence,
            entities=observation.fused_entities,
        )
        gold_request = dataclasses.replace(
            predicted_request, intent=IntentCategory(case.expected_intent)
        )
        pure_request = dataclasses.replace(
            gold_request, urgency=UrgencyLevel.LOW
        )

        decision_pred = orchestrator._route_decision(predicted_request)
        decision_gold = orchestrator._route_decision(gold_request)
        decision_pure = orchestrator._route_decision(pure_request)

        end_to_end_actual.append(decision_pred.primary_agent.value)
        end_to_end_expected.append(expected_agent_value)
        oracle_actual.append(decision_gold.primary_agent.value)
        oracle_expected.append(expected_agent_value)
        pure_actual.append(decision_pure.primary_agent.value)
        pure_expected.append(expected_agent_value)

        lowered = case.message.lower()
        has_incident = any(term in lowered for term in INCIDENT_DOMAIN_TERMS)
        has_access = any(term in lowered for term in ACCESS_DOMAIN_TERMS)
        expected_multi = has_incident and has_access
        multi_expected.append(expected_multi)
        multi_actual.append(bool(decision_gold.multi_agent))

        clar = bool(orchestrator._needs_clarification(predicted_request))
        clarification_actual.append(clar)
        clarification_expected.append(case.expected_intent == IntentCategory.OTHER.value)
        if clar:
            clarification_details.append(
                {
                    "id": case.id,
                    "message": case.message,
                    "predicted": observation.fused_intent,
                    "confidence": round(observation.fused_confidence, 6),
                    "expected": case.expected_intent,
                }
            )
        if decision_gold.multi_agent:
            multi_details.append(
                {
                    "id": case.id,
                    "message": case.message,
                    "expected_intent": case.expected_intent,
                    "primary": decision_gold.primary_agent.value,
                    "supporting": [agent.value for agent in decision_gold.supporting_agents],
                    "expected_multi_by_lexicon": expected_multi,
                }
            )
        if decision_pred.primary_agent.value != expected_agent_value:
            misrouted.append(
                {
                    "id": case.id,
                    "message": case.message,
                    "expected_intent": case.expected_intent,
                    "predicted_intent": observation.fused_intent,
                    "expected_agent": expected_agent_value,
                    "actual_agent": decision_pred.primary_agent.value,
                    "supporting": [agent.value for agent in decision_pred.supporting_agents],
                    "urgency": observation.fused_urgency.name,
                    "scores_reason": decision_pred.reason,
                }
            )
        agent_confusion.append(
            {
                "expected": expected_agent_value,
                "actual": decision_pred.primary_agent.value,
            }
        )

    n = len(observations)
    expected_multi_count = sum(1 for flag in multi_expected if flag)
    actual_multi_count = sum(1 for flag in multi_actual if flag)
    hits = sum(1 for expected, actual in zip(multi_expected, multi_actual) if expected and actual)
    combinations = Counter(
        f"{detail['primary']}+{'+'.join(detail['supporting']) or 'none'}" for detail in multi_details
    )
    multi = {
        "n": n,
        "expected_multi_agent": expected_multi_count,
        "actual_multi_agent": actual_multi_count,
        "trigger_rate": ratio(actual_multi_count, n),
        "hit": hits,
        "recall": ratio(hits, expected_multi_count),
        "precision": ratio(hits, actual_multi_count),
        "false_trigger": sum(
            1 for expected, actual in zip(multi_expected, multi_actual) if actual and not expected
        ),
        "missed": sum(
            1 for expected, actual in zip(multi_expected, multi_actual) if expected and not actual
        ),
        "combinations": dict(sorted(combinations.items())),
        "details": multi_details[:40],
        "lexicon": {
            "incident": INCIDENT_DOMAIN_TERMS,
            "access": ACCESS_DOMAIN_TERMS,
            "definition": "expected_multi_agent := 消息同时包含事件域词与访问权限域词（harness 词典）",
            "caveat": "该词典只用于离线路由评估，不进入生产路由代码。",
        },
    }

    expected_other_count = sum(1 for flag in clarification_expected if flag)
    predicted_other_count = sum(
        1 for observation in observations if observation.fused_intent == IntentCategory.OTHER.value
    )
    predicted_other_low_conf = sum(
        1
        for observation in observations
        if observation.fused_intent == IntentCategory.OTHER.value
        and observation.fused_confidence < 0.5
    )
    clarification = {
        "n": n,
        "triggered": sum(1 for flag in clarification_actual if flag),
        "trigger_rate": ratio(sum(1 for flag in clarification_actual if flag), n),
        "expected_other": expected_other_count,
        "predicted_other": predicted_other_count,
        "predicted_other_low_confidence": predicted_other_low_conf,
        "hit_expected_other": sum(
            1 for expected, actual in zip(clarification_expected, clarification_actual) if expected and actual
        ),
        "recall_on_other": ratio(
            sum(1 for expected, actual in zip(clarification_expected, clarification_actual) if expected and actual),
            expected_other_count,
        ),
        "false_clarification": sum(
            1 for expected, actual in zip(clarification_expected, clarification_actual) if actual and not expected
        ),
        "details": clarification_details[:60],
    }

    end_to_end = classification_metrics(end_to_end_expected, end_to_end_actual)
    oracle = classification_metrics(oracle_expected, oracle_actual)
    pure = classification_metrics(pure_expected, pure_actual)

    return {
        "expected_agent_mapping": {
            "reason": (
                "按 OpsMind 业务语义声明的 intent→期望主 Agent 映射；故障归 incident，"
                "登录、权限与安全归 access，其余服务台请求默认归 service_desk。"
                "该表只用于生成期望标签，不进入被测代码路径。"
            ),
            "mapping": dict(EXPECTED_AGENT_BY_INTENT),
            "default": DEFAULT_AGENT,
        },
        "end_to_end": end_to_end,
        "oracle_gold_intent": oracle,
        "pure_routing_gold_intent_low_urgency": pure,
        "multi_agent": multi,
        "clarification": clarification,
        "misrouted": misrouted[:60],
        "agent_confusion": {
            "n": n,
            "matrix": dict(Counter(f"{row['expected']}->{row['actual']}" for row in agent_confusion)),
        },
    }


# ---------------------------------------------------------------------------
# Entity analysis
# ---------------------------------------------------------------------------
ENTITY_FIELDS = ["ticket_id", "service", "account", "date", "error_code"]


def entity_analysis(recognizer: IntentRecognizer) -> Dict[str, Any]:
    fixture_path = FIXTURES_DIR / "entity_cases.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    cases = payload.get("cases") or []
    probe = recognizer._extract_entities("工单号：INC-2026，ERP 系统，账号 zhangsan，今天报错 500")
    implemented = {name: name in probe for name in ENTITY_FIELDS}

    per_field: Dict[str, Any] = {}
    for name in ENTITY_FIELDS:
        tp = fp = fn = tn = 0
        correct_cases = 0
        rows: List[Dict[str, Any]] = []
        for case in cases:
            expected = [str(item) for item in (case.get("expected") or {}).get(name, [])]
            predicted = [str(item) for item in (recognizer._extract_entities(case["message"]).get(name) or [])]
            expected_norm = {normalize_text(item) for item in expected}
            predicted_norm = {normalize_text(item) for item in predicted}
            match = expected_norm == predicted_norm
            if match:
                correct_cases += 1
                if expected_norm:
                    tp += 1
                else:
                    tn += 1
            else:
                tp += len(expected_norm & predicted_norm)
                fp += len(predicted_norm - expected_norm)
                fn += len(expected_norm - predicted_norm)
            rows.append(
                {
                    "id": case.get("id"),
                    "message": case["message"],
                    "expected": expected,
                    "predicted": predicted,
                    "match": match,
                }
            )
        n = len(cases)
        positive = tp + fn
        per_field[name] = {
            "implemented": implemented[name],
            "n": n,
            "correct_cases": correct_cases,
            "case_accuracy": round(correct_cases / n, 6) if n else 0.0,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "positive": positive,
            "recall": round(tp / positive, 6) if positive else None,
            "precision": round(tp / (tp + fp), 6) if (tp + fp) else None,
            "cases": rows,
        }

    implemented_fields = [name for name in ENTITY_FIELDS if implemented[name]]
    overall_n = len(cases) * len(implemented_fields)
    overall_correct = sum(per_field[name]["correct_cases"] for name in implemented_fields)
    return {
        "fixture": rel(fixture_path),
        "fixture_kind": "harness-authored labelled fixture (NOT a project dataset)",
        "n": len(cases),
        "fields": ENTITY_FIELDS,
        "implemented_fields": implemented_fields,
        "missing_fields": [name for name in ENTITY_FIELDS if not implemented[name]],
        "extractor_output_keys": sorted(probe.keys()),
        "per_field": per_field,
        "overall": {
            "n": overall_n,
            "correct": overall_correct,
            "accuracy": round(overall_correct / overall_n, 6) if overall_n else None,
            "note": "只统计真实实现返回的字段；缺失字段单独列出，不计入该总体准确率",
        },
    }


# ---------------------------------------------------------------------------
# End-to-end offline smoke (real orchestrator.run, LLM disabled)
# ---------------------------------------------------------------------------
async def end_to_end_smoke(
    orchestrator: AgentOrchestrator,
    observations: List[CaseObservation],
    max_agents: int = 4,
) -> Dict[str, Any]:
    chosen: List[CaseObservation] = []
    seen = set()
    for observation in observations:
        agent = expected_agent(observation.case.expected_intent)
        if agent in seen:
            continue
        seen.add(agent)
        chosen.append(observation)
        if len(chosen) >= max_agents:
            break

    results: List[Dict[str, Any]] = []
    for observation in chosen:
        case = observation.case
        request = Request(
            message=case.message,
            user_id="offline_user",
            conv_id="offline_conv",
            intent=IntentCategory(observation.fused_intent),
            intent_group=observation.fused_group,
            urgency=observation.fused_urgency,
            intent_confidence=observation.fused_confidence,
            entities=observation.fused_entities,
        )
        started = time.perf_counter()
        error = None
        try:
            result = await orchestrator.run(request)
            payload = {
                "id": case.id,
                "message": case.message,
                "expected_intent": case.expected_intent,
                "expected_agent": expected_agent(case.expected_intent),
                "resolved_intent": result.intent.value if result.intent else None,
                "primary_agent": result.primary_agent.value if result.primary_agent else None,
                "agent_type": result.agent_type.value,
                "escalated": bool(result.escalated),
                "response_chars": len(result.response or ""),
                "response_head": (result.response or "")[:80],
                "routing_reason": result.routing_reason,
                "ok": bool(result.response),
            }
        except Exception as ex:  # the point of the smoke test is that this does not happen
            error = f"{type(ex).__name__}: {ex}"
            payload = {
                "id": case.id,
                "message": case.message,
                "ok": False,
                "error": error,
            }
        payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        results.append(payload)

    return {
        "n": len(results),
        "ok": sum(1 for item in results if item.get("ok")),
        "exceptions": sum(1 for item in results if item.get("error")),
        "llm_path_raised": anthropic.snapshot()["errors"],
        "results": results,
    }


# ---------------------------------------------------------------------------
# Governance checks: circuit breaker / cache / fallback / skills
# ---------------------------------------------------------------------------
def governance_circuit_breaker() -> Dict[str, Any]:
    clock = FakeClock(1000.0)
    checks: List[Dict[str, Any]] = []
    transitions: List[str] = []

    def note(label: str) -> None:
        transitions.append(f"{label}:{cb.state.value}")

    with patched_module_time(tool_manager_module, clock):
        cb = CircuitBreaker(failure_threshold=5, recovery_s=60.0)
        note("init")
        for _ in range(5):
            cb.record_failure()
        note("after_5_failures")
        allowed_open = cb.allow()
        note("allow_at_t+0")
        clock.advance(59.0)
        allowed_59 = cb.allow()
        note("allow_at_t+59")
        clock.advance(1.0)
        allowed_60 = cb.allow()
        note("allow_at_t+60")
        state_half_open = cb.state.value
        cb.record_success()
        note("after_probe_success")
        allowed_closed = cb.allow()
        note("allow_after_success")

        # observation (not a requirement): what happens if the probe fails
        cb_probe = CircuitBreaker(failure_threshold=5, recovery_s=60.0)
        for _ in range(5):
            cb_probe.record_failure()
        clock.advance(61.0)
        cb_probe.allow()  # OPEN -> HALF_OPEN
        state_before_probe_fail = cb_probe.state.value
        cb_probe.record_failure()
        state_after_probe_fail = cb_probe.state.value
        fail_count_after = cb_probe.fail_count
        allow_after_probe_fail = cb_probe.allow()
        clock.advance(61.0)
        cb_probe.allow()

    checks.append(
        {
            "name": "连续 5 次失败后进入 OPEN",
            "passed": transitions[1].endswith("open"),
            "detail": f"state={transitions[1]}",
        }
    )
    checks.append(
        {
            "name": "OPEN 状态拒绝请求（t+0 与 t+59）",
            "passed": allowed_open is False and allowed_59 is False,
            "detail": f"allow@t+0={allowed_open}, allow@t+59={allowed_59}",
        }
    )
    checks.append(
        {
            "name": "60s 后进入 HALF_OPEN 并放行探测",
            "passed": allowed_60 is True and state_half_open == CircuitState.HALF_OPEN.value,
            "detail": f"allow@t+60={allowed_60}, state={state_half_open}",
        }
    )
    checks.append(
        {
            "name": "探测成功后恢复 CLOSED 且继续放行",
            "passed": transitions[-2].endswith("closed") and allowed_closed is True,
            "detail": f"{transitions[-2]}, allow={allowed_closed}",
        }
    )
    state_sequence = [item.split(":", 1)[1] for item in transitions]
    expected_sequence = ["closed", "open", "open", "open", "half_open", "closed", "closed"]
    checks.append(
        {
            "name": "状态转移序列 CLOSED→OPEN→HALF_OPEN→CLOSED",
            "passed": state_sequence == expected_sequence,
            "detail": " -> ".join(state_sequence),
        }
    )

    return {
        "n_checks": len(checks),
        "passed": sum(1 for check in checks if check["passed"]),
        "checks": checks,
        "transitions": transitions,
        "state_sequence": state_sequence,
        "config": {"failure_threshold": 5, "recovery_s": 60.0, "clock": "FakeClock(monotonic, 不 sleep)"},
        "observation_half_open_probe_failure": {
            "before": state_before_probe_fail,
            "after": state_after_probe_fail,
            "fail_count": fail_count_after,
            "allow_after_failed_probe": allow_after_probe_fail,
            "note": (
                "观察项（非任务 PASS/FAIL 要求）：进入 HALF_OPEN 时 fail_count 没有清零，"
                "所以探测失败会让 fail_count 累加到阈值以上并立即重新 OPEN；"
                "同时 HALF_OPEN 的 allow() 对每个调用方都放行，没有「只放一个探测请求」的闸门。"
            ),
        },
    }


async def governance_ttl_cache() -> Dict[str, Any]:
    clock = FakeClock(2000.0)
    calls = {"count": 0}

    async def handler(params: Dict[str, Any], context: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        calls["count"] += 1
        return [{"doc": f"doc-{index}", "text": f"缓存测试文档 {index}"} for index in range(6)]

    manager = MCPToolManager(api_key="offline")
    manager.register(
        Tool(
            name="kb_search",
            description="offline cache fixture",
            handler=handler,
            schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            cache_ttl=60.0,
            timeout_s=5.0,
            supports_rerank=True,
        )
    )
    with patched_module_time(tool_manager_module, clock):
        first = await manager.call("kb_search", {"query": "VPN 故障排查"})
        calls_after_first = calls["count"]
        second = await manager.call("kb_search", {"query": "VPN 故障排查"})
        calls_after_second = calls["count"]
        reranked = await manager.call("kb_search", {"query": "VPN 故障排查"}, rerank_top_k=2)
        calls_after_rerank = calls["count"]
        clock.advance(61.0)
        expired = await manager.call("kb_search", {"query": "VPN 故障排查"})
        calls_after_expiry = calls["count"]
        uncached = await manager.call("kb_search", {"query": "VPN 故障排查"}, use_cache=False)
        calls_after_no_cache = calls["count"]

    checks = [
        {
            "name": "同 key 二次调用命中缓存且不增加 handler 调用",
            "passed": (second.cached is True) and calls_after_second == calls_after_first,
            "detail": (
                f"cached={second.cached}, handler_calls {calls_after_first}->{calls_after_second}, "
                f"n=2 calls"
            ),
        },
        {
            "name": "rerank_top_k 不同则缓存 miss（handler 再被调用）",
            "passed": (reranked.cached is False) and calls_after_rerank == calls_after_second + 1,
            "detail": (
                f"cached={reranked.cached}, reranked={reranked.reranked}, "
                f"handler_calls {calls_after_second}->{calls_after_rerank}"
            ),
        },
        {
            "name": "TTL 过期后缓存 miss（假时钟前进 61s）",
            "passed": (expired.cached is False) and calls_after_expiry == calls_after_rerank + 1,
            "detail": f"cached={expired.cached}, handler_calls {calls_after_rerank}->{calls_after_expiry}",
        },
        {
            "name": "use_cache=False 强制穿透缓存",
            "passed": (uncached.cached is False) and calls_after_no_cache == calls_after_expiry + 1,
            "detail": f"cached={uncached.cached}, handler_calls {calls_after_expiry}->{calls_after_no_cache}",
        },
    ]
    return {
        "n_checks": len(checks),
        "passed": sum(1 for check in checks if check["passed"]),
        "checks": checks,
        "config": {"cache_ttl_s": 60.0, "supports_rerank": True, "clock": "FakeClock(monotonic, 不 sleep)"},
        "handler_calls_total": calls["count"],
    }


async def governance_fallback_and_integrated_breaker() -> Dict[str, Any]:
    clock = FakeClock(3000.0)
    flags = {"fail": True}
    calls = {"count": 0}

    async def handler(params: Dict[str, Any], context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        calls["count"] += 1
        if flags["fail"]:
            raise RuntimeError("backend unavailable (offline fixture)")
        return {"items": ["ok"], "calls": calls["count"]}

    def fallback(params: Dict[str, Any], context: Optional[Dict[str, Any]], error: str) -> Dict[str, Any]:
        return {
            "degraded": True,
            "reason": error,
            "items": [],
            "source": "tool.fallback",
        }

    manager = MCPToolManager(api_key="offline")
    manager.register(
        Tool(
            name="unstable_tool",
            description="offline fallback fixture",
            handler=handler,
            schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            cache_ttl=0.0,
            timeout_s=5.0,
            fallback=fallback,
        )
    )
    tool = manager._tools["unstable_tool"]
    states: List[str] = ["closed"]
    results: List[Any] = []
    with patched_module_time(tool_manager_module, clock):
        for _ in range(5):
            results.append(await manager.call("unstable_tool", {"query": "x"}))
        states.append(tool.breaker.state.value)
        after_five = tool.breaker.state.value
        sixth = await manager.call("unstable_tool", {"query": "x"})
        states.append(tool.breaker.state.value)
        after_sixth = tool.breaker.state.value
        open_rejection = sixth
        clock.advance(61.0)
        probe_allowed = tool.breaker.allow()  # OPEN -> HALF_OPEN
        state_after_probe = tool.breaker.state.value
        states.append(state_after_probe)
        flags["fail"] = False
        seventh = await manager.call("unstable_tool", {"query": "x"})
        states.append(tool.breaker.state.value)
        state_final = tool.breaker.state.value

    fallback_data = sixth.data if isinstance(sixth.data, dict) else {}
    checks = [
        {
            "name": "连续 5 次失败后熔断打开",
            "passed": after_five == CircuitState.OPEN.value,
            "detail": f"state after 5 failed calls = {after_five}",
        },
        {
            "name": "熔断打开时返回结构化降级结果而非抛异常",
            "passed": isinstance(sixth.data, dict) and bool(fallback_data.get("degraded")),
            "detail": f"success={sixth.success}, data={fallback_data}",
        },
        {
            "name": "熔断错误信息可识别",
            "passed": "熔断" in str(sixth.error or ""),
            "detail": f"error={sixth.error!r}",
        },
        {
            "name": "60s 后 HALF_OPEN 探测并成功恢复 CLOSED",
            "passed": (
                probe_allowed is True
                and state_after_probe == CircuitState.HALF_OPEN.value
                and state_final == CircuitState.CLOSED.value
                and seventh.cached is False
            ),
            "detail": (
                f"probe_allowed={probe_allowed}, state_after_probe={state_after_probe}, "
                f"final={state_final}, probe_data={seventh.data}"
            ),
        },
    ]
    return {
        "n_checks": len(checks),
        "passed": sum(1 for check in checks if check["passed"]),
        "checks": checks,
        "state_sequence": states,
        "open_rejection_error": open_rejection.error,
        "open_rejection_data": fallback_data,
        "probe_recovery_data": seventh.data,
        "config": {"failure_threshold": 5, "recovery_s": 60.0, "clock": "FakeClock(monotonic, 不 sleep)"},
    }


def governance_skills() -> Dict[str, Any]:
    skills_dir = PROJECT_ROOT / "skills"
    manager = SkillManager(str(skills_dir))
    loaded = manager.load()
    errors = manager.errors

    long_dir = FIXTURES_DIR / "skills_budget_long"
    long_dir.mkdir(parents=True, exist_ok=True)
    long_body = "预算截断测试文本。" * 900
    for index in ("a", "b"):
        skill_file = long_dir / f"long_{index}" / "SKILL.md"
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(
            "---\n"
            f"name: 超长预算测试{index}\n"
            "description: 5000 字符预算截断夹具\n"
            "keywords: 超长预算测试\n"
            "enabled: true\n"
            "---\n\n"
            f"# 超长预算测试{index}\n\n"
            f"{long_body}\n",
            encoding="utf-8",
        )

    checks: List[Dict[str, Any]] = []
    per_domain: Dict[str, Any] = {}
    for domain in ("access_support", "technical_support", "general_customer_service"):
        skill = next(
            (item for item in loaded if Path(item.path).parent.name == domain),
            None,
        )
        if skill is None:
            per_domain[domain] = {"loaded": False}
            checks.append({"name": f"{domain} Skill 加载", "passed": False, "detail": "not loaded"})
            continue
        agent_type = skill.agents[0] if skill.agents else None
        message = skill.keywords[0] if skill.keywords else "你好"
        prompt = manager.prompt_for(message, agent_type)
        matched = skill.name in prompt
        per_domain[domain] = {
            "loaded": True,
            "name": skill.name,
            "agents": skill.agents,
            "keywords_first": skill.keywords[0] if skill.keywords else None,
            "probe_message": message,
            "agent_type": agent_type,
            "prompt_chars": len(prompt),
            "matched": matched,
        }
        checks.append(
            {
                "name": f"{domain} Skill 按关键词+Agent 命中",
                "passed": matched,
                "detail": f"name={skill.name!r}, agent={agent_type}, prompt_chars={len(prompt)}",
            }
        )

    access_skill = next(
        (item for item in loaded if Path(item.path).parent.name == "access_support"), None
    )
    isolation_prompt = manager.prompt_for("申请权限", "incident") if access_skill else ""
    checks.append(
        {
            "name": "Skill 跨 Agent 隔离（access 文案不注入 incident）",
            "passed": access_skill is not None and access_skill.name not in isolation_prompt,
            "detail": f"prompt_chars={len(isolation_prompt)}",
        }
    )
    unmatched = manager.prompt_for("今天天气不错", "service_desk")
    checks.append(
        {
            "name": "无关键词命中时不注入 Skill",
            "passed": unmatched == "",
            "detail": f"prompt_chars={len(unmatched)}",
        }
    )

    long_manager = SkillManager(str(long_dir), max_prompt_chars=5000)
    long_skills = long_manager.load()
    long_prompt = long_manager.prompt_for("超长预算测试")
    wide_manager = SkillManager(str(long_dir), max_prompt_chars=10000)
    wide_manager.load()
    wide_prompt = wide_manager.prompt_for("超长预算测试")
    budget_ok = (
        len(long_prompt) <= 5000 + 200
        and long_prompt.endswith("...")
        and len(wide_prompt) > len(long_prompt) + 1000
    )
    checks.append(
        {
            "name": "5000 字符总预算截断生效",
            "passed": budget_ok,
            "detail": (
                f"budget=5000 -> prompt_chars={len(long_prompt)}; "
                f"budget=10000 -> prompt_chars={len(wide_prompt)}; "
                f"fixture_skills={len(long_skills)}"
            ),
        }
    )

    return {
        "n_checks": len(checks),
        "passed": sum(1 for check in checks if check["passed"]),
        "checks": checks,
        "skills_dir": rel(skills_dir),
        "loaded_skills": len(loaded),
        "load_errors": errors,
        "per_domain": per_domain,
        "default_max_prompt_chars": manager.max_prompt_chars,
        "budget_fixture": {
            "dir": rel(long_dir),
            "skills": len(long_skills),
            "budget_5000_chars": len(long_prompt),
            "budget_10000_chars": len(wide_prompt),
            "per_skill_cap": 3200,
        },
    }


async def governance_llm_stub_control() -> Dict[str, Any]:
    """Prove the LLM stub switch is real and reversible, then restore 'fail'.

    This check only validates the *stub contract*; the ``static`` response is
    never used to compute a metric.
    """
    client = anthropic.AsyncAnthropic(api_key="offline")
    checks: List[Dict[str, Any]] = []
    before = anthropic.snapshot()
    anthropic.set_mode("static")
    try:
        response = await client.messages.create(model="stub", messages=[{"role": "user", "content": "x"}])
        static_ok = bool(getattr(response, "content", None))
    finally:
        anthropic.set_mode("fail")
    during = anthropic.snapshot()
    checks.append(
        {
            "name": "桩切换 static 可返回对象",
            "passed": static_ok,
            "detail": f"mode=static, calls {before['calls']}->{during['calls']}",
        }
    )
    raised = False
    try:
        await client.messages.create(model="stub", messages=[{"role": "user", "content": "x"}])
    except RuntimeError as ex:
        raised = "offline" in str(ex)
    after = anthropic.snapshot()
    checks.append(
        {
            "name": "切换回 fail 后调用必然抛离线错误",
            "passed": raised,
            "detail": f"mode=fail, calls={after['calls']}, errors={after['errors']}",
        }
    )
    return {
        "n_checks": len(checks),
        "passed": sum(1 for check in checks if check["passed"]),
        "checks": checks,
        "counter_after": after,
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------
def metric_row_table(metrics: Dict[str, Any]) -> List[List[str]]:
    return [
        [
            str(metrics["correct"]),
            str(metrics["n"]),
            fmt(metrics["accuracy"]),
            fmt(metrics["macro_f1"]),
            fmt(metrics["macro_f1_supported"]),
        ]
    ]


def md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(md_cell(cell) for cell in row) + " |")
    return "\n".join(lines)


def per_class_table(metrics: Dict[str, Any]) -> str:
    rows = []
    for label, entry in metrics["per_class"].items():
        if entry["support"] == 0 and entry["tp"] == 0 and entry["fp"] == 0:
            continue
        rows.append(
            [
                label,
                entry["support"],
                fmt(entry["precision"]),
                fmt(entry["recall"]),
                fmt(entry["f1"]),
            ]
        )
    return md_table(["意图", "support (n)", "P", "R", "F1"], rows)


def build_markdown(report: Dict[str, Any]) -> str:
    meta = report["meta"]
    datasets = report["datasets"]
    lines: List[str] = []
    add = lines.append

    add("# OpsMind 离线评测报告（LLM 不可用 · 断网 · 零第三方依赖）")
    add("")
    add(f"- 生成时间：{meta['generated_at']}")
    add(f"- Python：{meta['python']['version']}（{meta['python']['executable']}）")
    add(f"- 平台：{meta['platform']}")
    add(f"- 项目根：`{meta['project_root']}` / 分支：`{meta.get('git', {}).get('branch')}` / HEAD：`{meta.get('git', {}).get('head')}`")
    add(f"- LLM 依赖桩：`anthropic` 由 `{meta['provenance']['stub']['file']}` 替换（active={meta['provenance']['stub']['active']}），所有 `messages.create` 抛 `RuntimeError`")
    add(f"- LLM 调用计数：{meta['llm_stub']['calls']} 次调用 / {meta['llm_stub']['errors']} 次失败"
        f"（桩计数器，非估算；其中 {meta['llm_stub'].get('non_error_calls')} 次为桩自检 static 调用，其余全部离线失败）")
    add("- **本报告所有指标均由真实项目源码计算，没有任何硬编码结果数字。**")
    add("")

    add("## 0. 关键数字摘要")
    add("")
    summary_rows = []
    for dataset in datasets:
        intent = dataset["intent"]
        routing = dataset["routing"]
        ablation = intent["fused_weighted_0_85_0_15_ablation"]
        summary_rows.append(
            [
                dataset["name"],
                dataset["n"],
                f"{intent['pattern']['correct']}/{intent['pattern']['n']} ({fmt(intent['pattern']['accuracy'])})",
                f"{intent['embedding']['correct']}/{intent['embedding']['n']} ({fmt(intent['embedding']['accuracy'])})",
                f"{intent['fused']['correct']}/{intent['fused']['n']} ({fmt(intent['fused']['accuracy'])})",
                f"{ablation['correct']}/{ablation['n']} ({fmt(ablation['accuracy'])})",
                f"{routing['end_to_end']['correct']}/{routing['end_to_end']['n']} ({fmt(routing['end_to_end']['accuracy'])})",
                f"{routing['oracle_gold_intent']['correct']}/{routing['oracle_gold_intent']['n']} ({fmt(routing['oracle_gold_intent']['accuracy'])})",
            ]
        )
    add(
        md_table(
            [
                "数据集",
                "n",
                "a) pattern acc",
                "b) 向量单路 acc",
                "c) 融合 acc",
                "c-ablation 0.85/0.15 acc",
                "g) 路由 e2e acc",
                "g-oracle gold 意图 acc",
            ],
            summary_rows,
        )
    )
    add("")
    add(
        "> 一句话结论：`" + meta["headline"] + "`"
    )
    add("")

    add("## 1. 真实性证明：哪些真代码被跑到了")
    add("")
    add("注入方式是 `sys.path.insert(0, stubs)` 后直接 `import` 项目真实模块，没有复制/重写算法。")
    add("")
    module_rows = []
    for name, entry in meta["provenance"]["modules"].items():
        module_rows.append(
            [
                name,
                entry.get("file"),
                "是" if entry.get("is_project_source") else "否",
                (entry.get("sha256") or "")[:16],
                "OK" if entry.get("imported") else f"FAIL: {entry.get('error')}",
            ]
        )
    add(md_table(["模块", "文件", "项目源码", "sha256[:16]", "导入"], module_rows))
    add("")
    add(f"依赖桩文件：{'、'.join('`' + item + '`' for item in meta['provenance']['stub_files'])}")
    add("")

    add("## 2. 数据集")
    add("")
    selection = meta.get("dataset_selection", {})
    add(f"- 数据集选择：{selection.get('note', 'n/a')}")
    add("")
    dataset_rows = []
    for dataset in datasets:
        dataset_rows.append(
            [
                dataset["name"],
                dataset["path"] or "n/a",
                dataset["n"],
                dataset["source"],
                (dataset.get("sha256") or "")[:16],
            ]
        )
    add(md_table(["数据集", "路径", "n", "来源", "sha256[:16]"], dataset_rows))
    add("")
    for dataset in datasets:
        add(f"### 数据集 `{dataset['name']}`")
        add("")
        add(f"- 类别分布（n={dataset['n']}）：" + "、".join(
            f"`{label}`={count}" for label, count in dataset["class_distribution"].items()
        ))
        difficulty = dataset.get("difficulty_distribution") or {}
        if difficulty:
            add(f"- 难度分布（n={dataset['n']}）：" + "、".join(f"`{label}`={count}" for label, count in difficulty.items()))
        group_info = dataset.get("group_label_consistency") or {}
        if group_info.get("n"):
            add(
                f"- 数据集 `expected_group` 与项目 `_INTENT_GROUPS` 一致："
                f"{group_info['correct']}/{group_info['n']}（{fmt(group_info['accuracy'])}）"
            )
        leakage = dataset["leakage"]
        add(
            f"- 与 `_TEMPLATES` 泄漏检查：模板句 {leakage['template_sentences']} 条"
            f"（{leakage['template_categories']} 个意图类有模板，OTHER 无模板）；"
            f"精确重复 {leakage['exact_overlap']}/{leakage['n']}（{fmt(leakage['exact_overlap_ratio'])}），"
            f"归一化后重复 {leakage['normalized_overlap']}/{leakage['n']}"
            f"（{fmt(leakage['normalized_overlap_ratio'])}）。"
            + ("**存在泄漏，指标会偏乐观。**" if leakage["normalized_overlap"] else "未发现与模板句重复。")
        )
        if leakage["examples"]:
            add(f"  重复样例：{'；'.join('`' + md_cell(item) + '`' for item in leakage['examples'][:5])}")
        if dataset.get("load_warnings"):
            add(f"- 加载告警：{'；'.join(dataset['load_warnings'])}")
        add("")

    add("## 3. 意图层指标")
    add("")
    add("三路说明：LLM 路被桩强制置为失败（`failed=True`），因此 `_vote` 走的是项目自带的"
        "「LLM 失败 → embedding（>0）→ pattern」降级分支；c) 是该真实分支的输出。"
        "`c-ablation` 才是任务里提到的 0.85/0.15 权重分支，只作为诊断，不代表线上融合结果。")
    add("")
    for dataset in datasets:
        name = dataset["name"]
        intent = dataset["intent"]
        add(f"### `{name}`")
        add("")
        rows = []
        for tag, label in (
            ("a", "pattern 单路"),
            ("b", "本地向量单路"),
            ("c", "融合（离线真实分支）"),
            ("c-ablation", "0.85/0.15 分支（诊断）"),
        ):
            metrics = intent[{"a": "pattern", "b": "embedding", "c": "fused", "c-ablation": "fused_weighted_0_85_0_15_ablation"}[tag]]
            rows.append(
                [
                    tag,
                    label,
                    f"{metrics['correct']}/{metrics['n']}",
                    fmt(metrics["accuracy"]),
                    fmt(metrics["macro_f1"]),
                    fmt(metrics["macro_f1_supported"]),
                ]
            )
        add(md_table(["编号", "指标", "分子/分母", "accuracy", "macro-F1", "macro-F1(仅 support>0)"], rows))
        add("")
        ablation = intent["fused_weighted_0_85_0_15_ablation"]
        if ablation.get("always_predicts_other"):
            add(
                f"> c-ablation 说明（n={ablation['n']}）：该分支对全部用例都输出 OTHER"
                f"（{ablation['predicted_other']}/{ablation['n']}），"
                f"因此它的正确数恰好等于 OTHER 类支持数 {ablation['correct']}；"
                "原因是 0.85×0 + 0.15×pattern ≤ 0.15 永远达不到 threshold=0.5。"
            )
            add("")
        add(f"**c) 融合 per-class（含 support，n={intent['fused']['n']}）**")
        add("")
        add(per_class_table(intent["fused"]))
        add("")
        agreement = intent["lane_agreement"]
        add(
            f"路间一致性（n={agreement['n']}）：pattern==embedding {agreement['pattern_equals_embedding']}/{agreement['n']}；"
            f"融合==embedding {agreement['fused_equals_embedding']}/{agreement['n']}；"
            f"融合==pattern {agreement['fused_equals_pattern']}/{agreement['n']}；"
            f"融合=OTHER {agreement['fused_is_other']}/{agreement['n']}；"
            f"LLM 置信度全为 0：{agreement['llm_confidence_all_zero']}/{agreement['n']}。"
            f"pattern 单路本身答对 {agreement['pattern_correct']}/{agreement['n']}，"
            f"但融合仍错 {agreement['pattern_correct_but_fused_wrong']}/{agreement['n']}"
            f"（{fmt(agreement['pattern_correct_but_fused_wrong_ratio'])}）——即这些分数被降级分支直接丢掉了。"
            f"两路并集正确数（诊断上界）{agreement['union_pattern_or_fused_correct']}/{agreement['n']}"
            f"（{fmt(agreement['union_pattern_or_fused_accuracy'])}）。"
        )
        add("")
        degradation = intent.get("degradation_diagnostics") or {}
        if degradation:
            conf = degradation["fused_confidence"]
            add(
                f"**降级分支诊断（n={degradation['n']}）**：`confidence_threshold={degradation['confidence_threshold']}`；"
                f"融合置信度 min/median/max = {fmt(conf['min'])}/{fmt(conf['median'])}/{fmt(conf['max'])}；"
                f"低于阈值 {degradation['fused_below_threshold']}/{degradation['n']}"
                f"（{fmt(degradation['fused_below_threshold_ratio'])}）；"
                f"其中仍输出非-OTHER {degradation['threshold_bypassed_low_confidence_non_other']} 条；"
                f"预测 OTHER {degradation['predicted_other']}/{degradation['n']}；"
                f"embedding 非-OTHER {degradation['embedding_non_other']}/{degradation['n']}，"
                f"pattern 非-OTHER {degradation['pattern_non_other']}/{degradation['n']}。"
            )
            add("")
            add(f"> {degradation['finding']}")
            add("")
        add("**d) 错分样例（原句 + expected + predicted + 三路分数）**")
        add("")
        mis = intent["misclassified"]
        if mis:
            mis_rows = []
            for item in mis[:40]:
                lanes = item["lanes"]
                mis_rows.append(
                    [
                        item["id"],
                        item["message"],
                        item["expected"],
                        item["predicted"],
                        fmt(item["confidence"]),
                        f"llm={lanes['llm']['confidence']:.2f}",
                        f"emb={lanes['embedding']['intent']}/{lanes['embedding']['confidence']:.2f}",
                        f"pat={lanes['pattern']['intent']}/{lanes['pattern']['confidence']:.2f}",
                    ]
                )
            add(md_table(["id", "原句", "expected", "predicted", "conf", "llm", "emb", "pat"], mis_rows))
            add("")
            add(f"共错分 {len(mis)}/{dataset['n']} 条；MD 最多展示 40 条，JSON 中为全量。")
        else:
            add("无错分样例。")
        add("")

    add("## 4. 实体抽取（e）")
    add("")
    entities = report["entities"]
    add(
        f"标注集：`{entities['fixture']}`（{entities['fixture_kind']}，n={entities['n']}）。"
        f"真实 `_extract_entities` 返回字段：{ '、'.join(entities['extractor_output_keys']) }；"
        f"任务要求但实现缺失的字段：{ '、'.join(entities['missing_fields']) or '无' }。"
    )
    add("")
    entity_rows = []
    for field_name, data in entities["per_field"].items():
        entity_rows.append(
            [
                field_name,
                "有" if data["implemented"] else "**无实现**",
                f"{data['correct_cases']}/{data['n']}",
                fmt(data["case_accuracy"]),
                f"{data['tp']}/{data['tp'] + data['fn']}" if (data["tp"] + data["fn"]) else "n/a",
                fmt(data["recall"]),
                fmt(data["precision"]),
            ]
        )
    add(md_table(["字段", "实现", "整句正确/全部用例", "整句 accuracy", "命中/应命中", "recall", "precision"], entity_rows))
    add("")
    add(
        f"已实现字段汇总：{entities['overall']['correct']}/{entities['overall']['n']} = {fmt(entities['overall']['accuracy'])}"
        f"（{entities['overall']['note']}）"
    )
    add("")

    add("## 5. urgency（f）")
    add("")
    for dataset in datasets:
        urgency = dataset["urgency"]
        dist = "、".join(
            f"{level}={entry['count']}/{urgency['n']}" for level, entry in urgency["distribution"].items()
        )
        add(f"- `{dataset['name']}`：{dist}")
        add(
            f"  - 标注情况：n_labeled={urgency['n_labeled']}，accuracy="
            f"{fmt(urgency['accuracy']) if urgency['labeled'] else 'n/a（数据集无 urgency 标注，只出分布）'}；"
            f"显式紧急关键词命中 {urgency['explicit_urgency_keyword_hits']}/{urgency['n']}"
            f"（{fmt(urgency['explicit_urgency_keyword_ratio'])}）"
        )
    add("")

    add("## 6. 路由层（g/h/i）")
    add("")
    add("期望 Agent 映射表（harness 声明，只用于生成期望标签，不进入被测代码）：")
    add("")
    mapping_rows = [[intent, agent] for intent, agent in report["datasets"][0]["routing"]["expected_agent_mapping"]["mapping"].items()]
    mapping_rows.append(["（其余意图）", report["datasets"][0]["routing"]["expected_agent_mapping"]["default"]])
    add(md_table(["expected_intent", "expected_primary_agent"], mapping_rows))
    add("")
    for dataset in datasets:
        routing = dataset["routing"]
        add(f"### `{dataset['name']}`")
        add("")
        route_rows = []
        for tag, label, metrics in (
            ("g", "端到端（预测意图+预测紧急度）", routing["end_to_end"]),
            ("g-oracle", "路由层隔离（gold 意图+预测紧急度）", routing["oracle_gold_intent"]),
            ("g-pure", "纯路由（gold 意图+LOW 紧急度）", routing["pure_routing_gold_intent_low_urgency"]),
        ):
            route_rows.append(
                [
                    tag,
                    label,
                    f"{metrics['correct']}/{metrics['n']}",
                    fmt(metrics["accuracy"]),
                    fmt(metrics["macro_f1"]),
                ]
            )
        add(md_table(["编号", "口径", "分子/分母", "accuracy", "macro-F1"], route_rows))
        add("")
        add("**primary Agent per-class**")
        add("")
        add(per_class_table(routing["end_to_end"]))
        add("")
        multi = routing["multi_agent"]
        add(
            f"**h) 复合问题**：multi_agent 实际触发 {multi['actual_multi_agent']}/{multi['n']}"
            f"（trigger_rate={fmt(multi['trigger_rate'])}）；按 harness 词典应有 {multi['expected_multi_agent']}/{multi['n']}；"
            f"命中 {multi['hit']}/{multi['expected_multi_agent']}（recall={fmt(multi['recall'])}，"
            f"precision={fmt(multi['precision'])}），误触发 {multi['false_trigger']}，漏触发 {multi['missed']}。"
        )
        if multi.get("combinations"):
            add(f"实际触发组合（主+辅）：{multi['combinations']}")
        if multi.get("lexicon", {}).get("caveat"):
            add(f"> 口径说明：{multi['lexicon']['caveat']}")
        if multi["details"]:
            add("")
            add(
                md_table(
                    ["id", "原句", "expected_intent", "primary", "supporting", "词典判定应复合"],
                    [
                        [
                            item["id"],
                            item["message"],
                            item["expected_intent"],
                            item["primary"],
                            ",".join(item["supporting"]) or "-",
                            "是" if item["expected_multi_by_lexicon"] else "否",
                        ]
                        for item in multi["details"][:15]
                    ],
                )
            )
        clarification = routing["clarification"]
        add("")
        add(
            f"**i) 澄清触发**：{clarification['triggered']}/{clarification['n']}"
            f"（trigger_rate={fmt(clarification['trigger_rate'])}）；expected=other 的用例 "
            f"{clarification['expected_other']}/{clarification['n']}，命中 {clarification['hit_expected_other']}"
            f"，other 召回 {fmt(clarification['recall_on_other'])}，误澄清 {clarification['false_clarification']}；"
            f"预测为 OTHER {clarification.get('predicted_other')}/{clarification['n']}"
            f"（其中低置信度 {clarification.get('predicted_other_low_confidence')}）。"
        )
        if clarification["details"]:
            add("")
            add(
                md_table(
                    ["id", "原句", "predicted", "confidence", "expected"],
                    [
                        [item["id"], item["message"], item["predicted"], fmt(item["confidence"]), item["expected"]]
                        for item in clarification["details"][:15]
                    ],
                )
            )
        if routing["misrouted"]:
            add("")
            add(f"**g) 路由错误明细（共 {len(routing['misrouted'])} 条，MD 最多展示 20 条）**")
            add("")
            add(
                md_table(
                    ["id", "原句", "expected_intent", "predicted_intent", "expected_agent", "actual_agent", "urgency"],
                    [
                        [
                            item["id"],
                            item["message"],
                            item["expected_intent"],
                            item["predicted_intent"],
                            item["expected_agent"],
                            item["actual_agent"],
                            item["urgency"],
                        ]
                        for item in routing["misrouted"][:20]
                    ],
                )
            )
        add("")

    add("## 7. 工具与 Skills 治理（j/k/l/m，PASS/FAIL）")
    add("")
    governance = report["governance"]
    checks: List[Dict[str, Any]] = []
    for section, label in (
        ("circuit_breaker", "j) 三态熔断"),
        ("ttl_cache", "k) TTL 缓存"),
        ("fallback", "l) fallback + 集成熔断"),
        ("skills", "m) Skills 匹配/预算"),
        ("llm_stub_control", "桩自检"),
    ):
        for check in governance[section]["checks"]:
            checks.append([label, check["name"], "PASS" if check["passed"] else "FAIL", check["detail"]])
    add(md_table(["组", "检查项", "结果", "证据（n / 计数）"], checks))
    add("")
    add("**j) 状态转移证据（假时钟，不 sleep）**")
    add("")
    add("```")
    for item in governance["circuit_breaker"]["transitions"]:
        add(item)
    add(f"最终序列：{' -> '.join(governance['circuit_breaker']['state_sequence'])}")
    add("```")
    add("")
    add("**l) 集成熔断状态序列**")
    add("")
    add("```")
    add(" -> ".join(governance["fallback"]["state_sequence"]))
    add(f"OPEN 时降级返回：{governance['fallback']['open_rejection_data']}")
    add(f"探测恢复后返回：{governance['fallback']['probe_recovery_data']}")
    add("```")
    add("")
    observation = governance["circuit_breaker"]["observation_half_open_probe_failure"]
    add(
        f"> 额外观察（不计 PASS/FAIL）：HALF_OPEN 探测失败后 `fail_count` 累加到 {observation['fail_count']}"
        f"（进入 HALF_OPEN 时未清零），状态变为 `{observation['after']}`，`allow()`={observation['allow_after_failed_probe']}；"
        "且 HALF_OPEN 的 `allow()` 对每个调用方都放行，没有“只放一个探测请求”的闸门。"
    )
    add("")
    add(
        f"治理汇总：{sum(governance[s]['passed'] for s in ('circuit_breaker','ttl_cache','fallback','skills','llm_stub_control'))}"
        f"/{sum(governance[s]['n_checks'] for s in ('circuit_breaker','ttl_cache','fallback','skills','llm_stub_control'))} 项 PASS。"
    )
    add("")
    m = governance["skills"]
    add(
        f"Skills：目录 `{m['skills_dir']}` 加载 {m['loaded_skills']} 个（errors={len(m['load_errors'])}），"
        f"默认预算 {m['default_max_prompt_chars']} 字符；真实三域 prompt 长度 "
        + "、".join(
            f"{domain}={data.get('prompt_chars')}" for domain, data in m["per_domain"].items() if data.get("loaded")
        )
        + f"；预算夹具 n={m['budget_fixture']['skills']} 个 Skill，5000 预算输出 {m['budget_fixture']['budget_5000_chars']} 字符、"
        f"10000 预算输出 {m['budget_fixture']['budget_10000_chars']} 字符。"
    )
    add("")

    add("## 8. 端到端离线冒烟（`AgentOrchestrator.run`，LLM 全失败）")
    add("")
    smoke = report["e2e_smoke"]
    add(f"用例 n={smoke['n']}，无异常 {smoke['ok']}/{smoke['n']}，抛异常 {smoke['exceptions']}；"
        f"全程 LLM 桩累计调用 {meta['llm_stub']['calls']} 次、失败 {meta['llm_stub']['errors']} 次"
        f"（含 {meta['llm_stub'].get('non_error_calls')} 次桩自检调用）。")
    add("")
    add(
        md_table(
            ["id", "原句", "expected_agent", "expected_intent", "resolved_intent", "实际 primary", "escalated", "response_chars", "routing_reason"],
            [
                [
                    item.get("id"),
                    item.get("message"),
                    item.get("expected_agent"),
                    item.get("expected_intent"),
                    item.get("resolved_intent"),
                    item.get("primary_agent") or item.get("error"),
                    item.get("escalated"),
                    item.get("response_chars"),
                    item.get("routing_reason"),
                ]
                for item in smoke["results"]
            ],
        )
    )
    add("")

    add("## 9. 补丁清单与「未改源码」声明")
    add("")
    add("本 harness **只新建** `EchoMind/_offline_harness/` 下的文件，没有修改项目任何源码（`core/`、`agents/`、`mcp/`、`evaluation/` 等）。")
    add("")
    add("运行时补丁/注入仅以下 3 处，全部可逆，且都在报告里列明：")
    add("")
    patch_rows = [
        ["依赖桩注入", "`sys.path.insert(0, EchoMind/_offline_harness/stubs)` + `import anthropic`", "使真实模块可 import；`AsyncAnthropic.messages.create` 恒抛 `RuntimeError`，模拟无 key/断网", "否（仅进程内 import 解析）"],
        ["假时钟", "`mcp.tool_manager.time = SimpleNamespace(monotonic=FakeClock)`（`try/finally` 恢复）", "在 j/k/l 中驱动 60s TTL / 熔断恢复，不真实 sleep", "否（运行时对象替换，结束恢复）"],
        ["0.85/0.15 诊断开关", "`IntentRecognizer._embedding_enabled = False`（`try/finally` 恢复）", "仅用于 c-ablation 诊断，不进入 a/b/c 与路由指标", "否（运行时属性替换，结束恢复）"],
    ]
    add(md_table(["补丁", "位置", "目的", "是否改源码"], patch_rows))
    add("")
    add("被调用但没有 patch 的真实方法（均为源码原样）：")
    add("")
    for item in report["patches"]["invoked_real_methods"]:
        add(f"- `{item}`")
    add("")

    add("## 10. 这套指标不能说明什么（必须读）")
    add("")
    for item in report["caveats"]:
        add(f"- {item}")
    add("")

    add("## 11. 复现")
    add("")
    add("```powershell")
    for command in report["reproduction"]["commands"]:
        add(command)
    add("```")
    add("")
    add(f"退出码：0（本次运行耗时 {report['meta']['duration_s']} 秒）。")
    add("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpsMind offline evaluation harness")
    parser.add_argument(
        "--cases",
        action="append",
        default=None,
        metavar="PATH",
        help="外部意图数据集 JSON（可重复）。默认自动读取 EchoMind/data/eval/intent_cases.json",
    )
    parser.add_argument("--json-out", default=str(DEFAULT_JSON_OUT), help="JSON 报告输出路径")
    parser.add_argument("--md-out", default=str(DEFAULT_MD_OUT), help="Markdown 报告输出路径")
    parser.add_argument("--no-builtin", action="store_true", help="不附带 evaluator 内置 11 条用例")
    parser.add_argument("--skip-e2e", action="store_true", help="跳过端到端冒烟")
    return parser.parse_args(argv)


async def run_async(args: argparse.Namespace) -> Dict[str, Any]:
    started = time.perf_counter()
    say_lines: List[str] = []
    console: List[str] = []

    def say(message: str = "") -> None:
        console.append(message)
        print(message, flush=True)

    # --- provenance / environment -----------------------------------------
    provenance = module_provenance()
    if not provenance["stub"]["active"]:
        raise RuntimeError(f"anthropic 桩未生效，实际加载的是 {provenance['stub']['file']}")
    stub_info = provenance["stub"]
    say("=" * 78)
    say("OpsMind 离线评测 harness（无 LLM / 无网络 / 无第三方依赖）")
    say("=" * 78)
    say(f"Python      : {sys.version.split()[0]} ({sys.executable})")
    say(f"项目根      : {PROJECT_ROOT}")
    say(f"anthropic桩 : {stub_info['file']} (active={stub_info['active']})")
    say(f"运行时间    : {now_iso()}")
    say("")

    anthropic.reset_counters()
    recognizer = IntentRecognizer(api_key="offline-no-key", model="offline-stub-model")
    orchestrator = AgentOrchestrator(api_key="offline-no-key", model="offline-stub-model")

    # --- datasets ----------------------------------------------------------
    explicit_paths = [Path(item).expanduser() for item in (args.cases or [])]
    auto_path = DEFAULT_CASES_PATH if DEFAULT_CASES_PATH.exists() else None
    external_paths: List[Path] = []
    for path in explicit_paths or ([auto_path] if auto_path else []):
        if path and path.exists() and path not in external_paths:
            external_paths.append(path)

    dataset_selection = {
        "explicit_cases": [rel(path) for path in explicit_paths],
        "auto_detected_intent_cases": bool(auto_path),
        "default_cases_path": rel(DEFAULT_CASES_PATH),
        "default_cases_path_exists": DEFAULT_CASES_PATH.exists(),
        "builtin_included": not args.no_builtin,
        "note": (
            "优先使用外部 intent_cases.json；本次运行该文件存在，因此主数据集为外部数据，"
            "同时保留 evaluator 内置 11 条作为对照（报告给两套结果）。"
            if auto_path
            else "本次运行未发现 data/eval/intent_cases.json，退回 evaluator 内置 11 条用例，报告中已注明。"
        ),
    }

    datasets: List[Dataset] = []
    for index, path in enumerate(external_paths):
        dataset = load_case_file(path, name=f"{path.stem}" + (f"_{index + 1}" if len(external_paths) > 1 else ""))
        datasets.append(dataset)
    if not args.no_builtin:
        datasets.append(load_builtin_cases())
    if not datasets:
        raise RuntimeError("没有可用数据集")
    if auto_path is None and not explicit_paths:
        say("注意：data/eval/intent_cases.json 尚不存在，本次只用 evaluator 内置 11 条用例（会在报告中注明）。")
        say("")

    say("数据源：")
    for dataset in datasets:
        say(f"  - {dataset.name}: n={len(dataset.cases)} [{dataset.source}]")
    say("")

    # --- per-dataset analysis ---------------------------------------------
    dataset_reports: List[Dict[str, Any]] = []
    for dataset in datasets:
        say(f"---- 数据集 {dataset.name} (n={len(dataset.cases)}) ----")
        observations = await observe_dataset(dataset, recognizer)
        y_true = [observation.case.expected_intent for observation in observations]
        pattern_pred = [getattr(observation.pattern.get("intent"), "value", "other") for observation in observations]
        embedding_pred = [
            getattr(observation.embedding.get("intent"), "value", "other") for observation in observations
        ]
        fused_pred = [observation.fused_intent for observation in observations]

        pattern_metrics = classification_metrics(y_true, pattern_pred)
        embedding_metrics = classification_metrics(y_true, embedding_pred)
        fused_metrics = classification_metrics(y_true, fused_pred)
        ablation_metrics = weighted_branch_ablation(recognizer, observations)

        say(
            f"[a] pattern 单路      : {pattern_metrics['correct']}/{pattern_metrics['n']} (n={pattern_metrics['n']}) "
            f"accuracy={pattern_metrics['accuracy']:.4f} macro-F1={pattern_metrics['macro_f1']:.4f}"
        )
        say(
            f"[b] 本地向量单路      : {embedding_metrics['correct']}/{embedding_metrics['n']} (n={embedding_metrics['n']}) "
            f"accuracy={embedding_metrics['accuracy']:.4f} macro-F1={embedding_metrics['macro_f1']:.4f}"
        )
        say(
            f"[c] 融合(真实降级)    : {fused_metrics['correct']}/{fused_metrics['n']} (n={fused_metrics['n']}) "
            f"accuracy={fused_metrics['accuracy']:.4f} macro-F1={fused_metrics['macro_f1']:.4f}"
        )
        say(
            f"[c-ablation] 0.85/0.15: {ablation_metrics['correct']}/{ablation_metrics['n']} "
            f"(n={ablation_metrics['n']}) accuracy={ablation_metrics['accuracy']:.4f} "
            f"macro-F1={ablation_metrics['macro_f1']:.4f} （LLM 信号=0 的诊断分支）"
        )

        routing = analyze_routing(dataset, observations, orchestrator)
        say(
            f"[g] 路由 primary(e2e) : {routing['end_to_end']['correct']}/{routing['end_to_end']['n']} "
            f"(n={routing['end_to_end']['n']}) accuracy={routing['end_to_end']['accuracy']:.4f} macro-F1={routing['end_to_end']['macro_f1']:.4f}"
        )
        say(
            f"[g-oracle] gold 意图  : {routing['oracle_gold_intent']['correct']}/{routing['oracle_gold_intent']['n']} "
            f"accuracy={routing['oracle_gold_intent']['accuracy']:.4f}"
        )
        say(
            f"[g-pure] gold+LOW紧急度: {routing['pure_routing_gold_intent_low_urgency']['correct']}/"
            f"{routing['pure_routing_gold_intent_low_urgency']['n']} "
            f"accuracy={routing['pure_routing_gold_intent_low_urgency']['accuracy']:.4f}"
        )
        multi = routing["multi_agent"]
        say(
            f"[h] multi_agent 触发率: {multi['actual_multi_agent']}/{multi['n']} "
            f"(n={multi['n']}, expected={multi['expected_multi_agent']}, hit={multi['hit']}/{multi['expected_multi_agent']})"
        )
        clarification = routing["clarification"]
        say(
            f"[i] 澄清触发          : {clarification['triggered']}/{clarification['n']} (n={clarification['n']}, "
            f"expected_other={clarification['expected_other']}, hit={clarification['hit_expected_other']})"
        )

        class_distribution = Counter(y_true)
        group_consistency = [
            observation
            for observation in observations
            if observation.case.expected_group
        ]
        group_consistency_correct = sum(
            1
            for observation in group_consistency
            if str(observation.case.expected_group) == expected_group(observation.case.expected_intent)
        )
        dataset_report = {
            "name": dataset.name,
            "path": rel(dataset.path),
            "source": dataset.source,
            "n": len(dataset.cases),
            "sha256": dataset.sha256,
            "load_warnings": dataset.load_warnings,
            "class_distribution": dict(sorted(class_distribution.items())),
            "group_distribution": dict(
                sorted(Counter(expected_group(intent_value) for intent_value in y_true).items())
            ),
            "group_label_consistency": {
                "n": len(group_consistency),
                "correct": group_consistency_correct,
                "accuracy": ratio(group_consistency_correct, len(group_consistency)),
                "note": "数据集自带 expected_group 与项目 _INTENT_GROUPS 的一致率（只对带标签用例）",
            },
            "difficulty_distribution": dict(
                sorted(
                    Counter(
                        observation.case.difficulty or "unlabeled" for observation in observations
                    ).items()
                )
            ),
            "leakage": leakage_stats(dataset),
            "intent": {
                "pattern": pattern_metrics,
                "embedding": embedding_metrics,
                "fused": fused_metrics,
                "fused_weighted_0_85_0_15_ablation": ablation_metrics,
                "lane_agreement": lane_agreement(observations),
                "degradation_diagnostics": degradation_diagnostics(observations, recognizer),
                "misclassified": misclassified_details(observations),
            },
            "urgency": urgency_analysis(observations),
            "routing": routing,
        }
        dataset_reports.append(dataset_report)
        degradation = dataset_report["intent"]["degradation_diagnostics"]
        say(
            f"[c-diag] 融合置信度<阈值: {degradation['fused_below_threshold']}/{degradation['n']} "
            f"(n={degradation['n']}, threshold={degradation['confidence_threshold']})，"
            f"低置信度仍输出非-OTHER: {degradation['threshold_bypassed_low_confidence_non_other']}，"
            f"预测 OTHER: {degradation['predicted_other']}"
        )
        agreement = dataset_report["intent"]["lane_agreement"]
        say(
            f"[c-diag] pattern 答对但融合错: {agreement['pattern_correct_but_fused_wrong']}/{agreement['n']} "
            f"(n={agreement['n']})，两路并集上界: {agreement['union_pattern_or_fused_correct']}/{agreement['n']} "
            f"({fmt(agreement['union_pattern_or_fused_accuracy'])})"
        )
        group_info = dataset_report["group_label_consistency"]
        if group_info["n"]:
            say(
                f"[组标签] expected_group 与 _INTENT_GROUPS 一致: {group_info['correct']}/{group_info['n']} "
                f"(n={group_info['n']})"
            )
        say("")

    # --- entity / urgency / governance ------------------------------------
    say("---- 实体抽取（e，harness 自建标注夹具）----")
    entities = entity_analysis(recognizer)
    for field_name, data in entities["per_field"].items():
        emphasis = " [该字段在真实实现中不存在]" if not data["implemented"] else ""
        say(
            f"[e] {field_name:<11}: 整句正确 {data['correct_cases']}/{data['n']} (n={data['n']}) "
            f"accuracy={data['case_accuracy']:.4f} recall={fmt(data['recall'])}{emphasis}"
        )
    say(f"[e] 已实现字段总体  : {entities['overall']['correct']}/{entities['overall']['n']} "
        f"(n={entities['overall']['n']}) accuracy={fmt(entities['overall']['accuracy'])}")
    say("")

    for dataset_report in dataset_reports:
        urgency = dataset_report["urgency"]
        detail = "、".join(
            f"{level}={entry['count']}/{urgency['n']}" for level, entry in urgency["distribution"].items()
        )
        say(
            f"[f] {dataset_report['name']} urgency 分布: {detail} "
            f"(n={urgency['n']}, labeled={urgency['n_labeled']}, accuracy={fmt(urgency['accuracy'])})"
        )
    say("")

    say("---- 工具与 Skills 治理（j/k/l/m）----")
    governance = {
        "circuit_breaker": governance_circuit_breaker(),
        "ttl_cache": await governance_ttl_cache(),
        "fallback": await governance_fallback_and_integrated_breaker(),
        "skills": governance_skills(),
        "llm_stub_control": await governance_llm_stub_control(),
    }
    for section in ("circuit_breaker", "ttl_cache", "fallback", "skills", "llm_stub_control"):
        for check in governance[section]["checks"]:
            say(f"[{section}] {'PASS' if check['passed'] else 'FAIL'} {check['name']} :: {check['detail']}")
    say("")
    say(f"[j] 熔断状态序列: {' -> '.join(governance['circuit_breaker']['state_sequence'])}")
    say(f"[l] 集成熔断序列: {' -> '.join(governance['fallback']['state_sequence'])}")
    say("")

    # --- end-to-end smoke --------------------------------------------------
    if args.skip_e2e:
        smoke: Dict[str, Any] = {"n": 0, "ok": 0, "exceptions": 0, "results": [], "skipped": True}
    else:
        say("---- 端到端离线冒烟（orchestrator.run，LLM 全失败）----")
        smoke = await end_to_end_smoke(orchestrator, await observe_dataset(datasets[0], recognizer))
        for item in smoke["results"]:
            say(
                f"[e2e] {item['id']}: primary={item.get('primary_agent')} "
                f"escalated={item.get('escalated')} response_chars={item.get('response_chars')} "
                f"ok={item.get('ok')}"
            )
        say("")

    # --- summarise ---------------------------------------------------------
    llm_snapshot = anthropic.snapshot()
    llm_snapshot["non_error_calls"] = llm_snapshot["calls"] - llm_snapshot["errors"]
    llm_snapshot["self_test_static_calls"] = 1
    llm_snapshot["note"] = (
        "non_error_calls 即桩自检时的那 1 次 static 调用；其余全部是离线失败调用。"
    )
    duration = round(time.perf_counter() - started, 2)
    primary_ds = dataset_reports[0]
    primary_intent = primary_ds["intent"]
    primary_route = primary_ds["routing"]
    headline = (
        f"主数据集 n={primary_ds['n']}：pattern {primary_intent['pattern']['correct']}/{primary_intent['pattern']['n']}、"
        f"向量 {primary_intent['embedding']['correct']}/{primary_intent['embedding']['n']}、"
        f"融合 {primary_intent['fused']['correct']}/{primary_intent['fused']['n']}，"
        f"融合结果等于向量结果 {primary_intent['lane_agreement']['fused_equals_embedding']}/{primary_intent['lane_agreement']['n']} 次；"
        f"路由端到端 {primary_route['end_to_end']['correct']}/{primary_route['end_to_end']['n']}，"
        f"gold 意图输入下 {primary_route['oracle_gold_intent']['correct']}/{primary_route['oracle_gold_intent']['n']}，"
        f"澄清触发 {primary_route['clarification']['triggered']}/{primary_route['clarification']['n']}。"
    )
    framework_checks = sum(
        governance[section]["passed"]
        for section in ("circuit_breaker", "ttl_cache", "fallback", "skills", "llm_stub_control")
    )
    framework_total = sum(
        governance[section]["n_checks"]
        for section in ("circuit_breaker", "ttl_cache", "fallback", "skills", "llm_stub_control")
    )

    caveats = [
        "LLM 路被刻意置为不可用：`anthropic` 桩每次 `messages.create` 都抛 `RuntimeError`，所以所有 LLM 语义理解、Query Rewriting、Rerank、LLM-as-Judge、多 Agent 汇总（composer）都没有被测量。",
        "RAG / 向量库 / Redis 记忆 / 知识库检索完全没有覆盖（chromadb、redis 只在需要 import 时才被桩住，本报告没有任何检索类指标）。",
        "端到端延迟、Token 成本、吞吐、并发、限流、超时没有被测量；仓库里 60s 级超时/熔断恢复均用假时钟验证，不代表真实负载表现。",
        "a/b/c 只测「意图分类」一个环节，不代表回复质量、事实正确性、是否完成任务；没有 LLM-as-Judge，也就没有相关性/完整性/有用性分数。",
        "数据集规模有限：内置 11 条用例与 `_TEMPLATES` 存在明显泄漏（见第 2 节），只能当冒烟/回归，不能当泛化准确率；外部数据集（若存在）仍是人工构造，非线上分布。",
        "e 的实体标注与 h 的「应否复合问题」词典由 harness 自建（fixture 与规则均在源码/报告中可查），n 很小，只能作为字段抽取与路由回归检查。",
        "g 的期望 Agent 映射由 harness 按业务语义声明，与项目 `_INTENT_ROUTING` 同构；它验证「实现是否符合设计」，不能证明「设计本身是否合理」。",
        "c-ablation 的 0.85/0.15 分支在 LLM 信号为 0 时必然产出 OTHER（0.15×pattern 达不到 0.5 阈值），这是分支设计的诊断结论，不是线上融合指标。",
        "单机单次运行、固定随机性（无随机源）可复现，但换机器/换 Python 小版本时 hash 向量与浮点结果可能有细微差异。",
    ]

    report: Dict[str, Any] = {
        "meta": {
            "generated_at": now_iso(),
            "duration_s": duration,
            "headline": headline,
            "project_root": str(PROJECT_ROOT),
            "harness_dir": rel(HERE),
            "python": {
                "version": sys.version.split()[0],
                "executable": sys.executable,
                "implementation": platform.python_implementation(),
            },
            "platform": platform.platform(),
            "offline": True,
            "network_used": False,
            "llm_stub": llm_snapshot,
            "provenance": provenance,
            "git": git_provenance(),
            "dataset_selection": dataset_selection,
            "intent_categories": len(IntentCategory),
            "template_sentences": sum(len(items) for items in _TEMPLATES.values()),
            "specific_intents": sorted(intent.value for intent in _SPECIFIC_INTENTS),
            "generic_intents": sorted(intent.value for intent in _GENERIC_INTENTS),
            "governance_summary": {"passed": framework_checks, "total": framework_total},
            "commands": [
                " ".join([sys.executable, str(HERE / "run.py")] + sys.argv[1:]),
            ],
            "console_log_lines": len(console),
        },
        "datasets": dataset_reports,
        "entities": entities,
        "governance": governance,
        "e2e_smoke": smoke,
        "patches": {
            "project_source_modified": False,
            "write_scope": ["EchoMind/_offline_harness/"],
            "patches": [
                {"name": "dependency stubs", "where": "sys.path.insert(0, EchoMind/_offline_harness/stubs)", "reversible": "import 解析层", "source_modified": False},
                {"name": "fake clock", "where": "mcp.tool_manager.time (try/finally restored)", "reversible": True, "source_modified": False},
                {"name": "embedding switch", "where": "IntentRecognizer._embedding_enabled=False (try/finally restored, c-ablation only)", "reversible": True, "source_modified": False},
            ],
            "invoked_real_methods": [
                "core.intent_recognizer.IntentRecognizer.recognize",
                "core.intent_recognizer.IntentRecognizer._llm_recognize (异常失败分支)",
                "core.intent_recognizer.IntentRecognizer._embedding_recognize",
                "core.intent_recognizer.IntentRecognizer._pattern_recognize",
                "core.intent_recognizer.IntentRecognizer._vote",
                "core.intent_recognizer.IntentRecognizer._extract_entities",
                "core.intent_recognizer.IntentRecognizer._urgency",
                "core.intent_recognizer.IntentRecognizer._local_embedding",
                "agents.agent_orchestrator.AgentOrchestrator._domain_scores",
                "agents.agent_orchestrator.AgentOrchestrator._route_decision",
                "agents.agent_orchestrator.AgentOrchestrator._collaboration_targets",
                "agents.agent_orchestrator.AgentOrchestrator._needs_clarification",
                "agents.agent_orchestrator.AgentOrchestrator.run",
                "mcp.tool_manager.CircuitBreaker.allow/record_failure/record_success",
                "mcp.tool_manager.MCPToolManager.call (cache / breaker / validate / fallback)",
                "core.skill_loader.SkillManager.load/prompt_for",
                "evaluation.evaluator.DEFAULT_INTENT_CASES (数据源)",
            ],
        },
        "caveats": caveats,
        "reproduction": {
            "python": sys.executable,
            "commands": [
                f'& "{sys.executable}" _offline_harness/run.py',
                f'& "{sys.executable}" _offline_harness/run.py --cases data/eval/intent_cases.json',
            ],
            "json_out": str(Path(args.json_out).resolve()),
            "md_out": str(Path(args.md_out).resolve()),
        },
        "console": console,
    }
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_console()
    args = parse_args(argv)
    json_out = Path(args.json_out).expanduser().resolve()
    md_out = Path(args.md_out).expanduser().resolve()
    json_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.parent.mkdir(parents=True, exist_ok=True)

    try:
        report = asyncio.run(run_async(args))
    except Exception as ex:
        import traceback

        traceback.print_exc()
        print(f"\n[harness] FAILED: {type(ex).__name__}: {ex}", file=sys.stderr)
        return 1

    markdown = build_markdown(report)
    json_out.write_text(json.dumps(jsonable(report), ensure_ascii=False, indent=2), encoding="utf-8")
    md_out.write_text(markdown, encoding="utf-8")

    print("=" * 78)
    print("报告已生成：")
    print(f"  JSON: {json_out}")
    print(f"  MD  : {md_out}")
    print("=" * 78)
    print("解读提示：本 harness 的 LLM 路被强制不可用；README 与报告第 10 节列出了指标边界。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
