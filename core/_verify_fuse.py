"""离线自检：意图融合降级修复（B+C 方案）。

背景
----
改动前 ``IntentRecognizer._vote`` 在 ``llm.failed`` 时**提前 return**：
只要 embedding 有正分就直接采信它，pattern 被丢掉，``confidence_threshold``
也被绕过（离线 133 条用例里 133 条置信度 < 0.5 却全部输出非 OTHER）。

本次修复：
  * 新增纯函数 ``_fuse``，正常路径权重/阈值/仲裁顺序与改动前逐位一致；
  * 降级路径在**可用源**上重新归一化权重，并使用独立的 ``degraded_threshold``；
  * ``_vote`` 保留三元组签名，内部委托 ``_fuse``；
  * ``IntentResult`` 只增 ``degraded`` / ``degraded_sources`` 两个字段。

运行（本机无外网、不 pip）::

    cd EchoMind
    "C:\\...\\python.exe" core/_verify_fuse.py

脚本只用标准库 + ``_offline_harness/stubs`` 依赖桩，不联网、不写任何项目文件。
退出码 0 = 全部 PASS。
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STUBS = PROJECT_ROOT / "_offline_harness" / "stubs"

# 顺序很重要：依赖桩必须在项目根之前，否则会 import 失败（本机没有 anthropic SDK）
for _path in (str(STUBS), str(PROJECT_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from core.intent_recognizer import (  # noqa: E402  (sys.path 注入后才能 import)
    DEFAULT_DEGRADED_THRESHOLD,
    _GENERIC_INTENTS,
    _INTENT_GROUPS,
    _SPECIFIC_INTENTS,
    FusionOutcome,
    IntentCategory,
    IntentRecognizer,
)

OTHER = IntentCategory.OTHER

RESULTS: List[Tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(passed), detail))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))
    return bool(passed)


def approx(value: float, expected: float, tol: float = 1e-9) -> bool:
    return abs(float(value) - float(expected)) <= tol


def make_recognizer(
    threshold: float = 0.5,
    degraded_threshold: float = DEFAULT_DEGRADED_THRESHOLD,
    embedding_enabled: bool = True,
) -> IntentRecognizer:
    """绕过 __init__ 造一个纯融合用实例（与 tests/test_intent_recognizer.py 同法）。"""
    recognizer = IntentRecognizer.__new__(IntentRecognizer)
    recognizer.threshold = threshold
    recognizer.degraded_threshold = degraded_threshold
    recognizer._embedding_enabled = embedding_enabled
    return recognizer


def case(intent: IntentCategory, confidence: float, failed: bool = False) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"intent": intent, "confidence": confidence}
    if failed:
        payload["failed"] = True
        payload["reasoning"] = "LLM 失败"
    return payload


def legacy_vote(
    recognizer: IntentRecognizer, llm: Dict, emb: Dict, pat: Dict
) -> Tuple[IntentCategory, float, Dict[str, float]]:
    """改动前 ``_vote`` 的逐字复刻（来源：本分支 HEAD 版 core/intent_recognizer.py）。

    仅用于回归对比，证明"LLM 正常时行为逐位一致"。
    """
    source_scores = {
        "llm": float(llm.get("confidence", 0.0) or 0.0),
        "embedding": float(emb.get("confidence", 0.0) or 0.0),
        "pattern": float(pat.get("confidence", 0.0) or 0.0),
    }
    if llm.get("failed"):
        if emb.get("intent") != IntentCategory.OTHER and emb.get("confidence", 0.0) > 0:
            return emb["intent"], source_scores["embedding"], source_scores
        if pat.get("intent") != IntentCategory.OTHER and pat.get("confidence", 0.0) > 0:
            return pat["intent"], source_scores["pattern"], source_scores
        return IntentCategory.OTHER, 0.0, source_scores

    if recognizer._embedding_enabled:
        weights = [(llm, 0.7), (emb, 0.2), (pat, 0.1)]
    else:
        weights = [(llm, 0.85), (pat, 0.15)]
    scores: Dict[IntentCategory, float] = {}
    group_scores: Dict[IntentCategory, float] = {}
    for result, weight in weights:
        category = result.get("intent", IntentCategory.OTHER)
        confidence = result.get("confidence", 0.0)
        weighted = weight * confidence
        scores[category] = scores.get(category, 0.0) + weighted
        group = _INTENT_GROUPS.get(category, category)
        group_scores[group] = group_scores.get(group, 0.0) + weighted

    best_group = max(group_scores, key=group_scores.get)
    best_score = group_scores[best_group]
    best = max((cat for cat in scores if _INTENT_GROUPS.get(cat, cat) == best_group), key=scores.get)

    pattern_intent = pat.get("intent", IntentCategory.OTHER)
    pattern_conf = float(pat.get("confidence", 0.0) or 0.0)
    if (
        best in _GENERIC_INTENTS
        and pattern_intent in _SPECIFIC_INTENTS
        and _INTENT_GROUPS.get(pattern_intent, pattern_intent) == best_group
        and pattern_conf >= 0.5
        and best_score < 0.8
    ):
        source_scores["refined_by_pattern"] = pattern_conf
        return pattern_intent, max(best_score, pattern_conf), source_scores
    if best_score < recognizer.threshold:
        return IntentCategory.OTHER, best_score, source_scores
    return best, best_score, source_scores


# ── 自检项 ────────────────────────────────────────────────────────────────────

def test_threshold_config() -> None:
    print("\n---- 1. degraded_threshold 可配置性 ----")
    env_name = "ECHOMIND_DEGRADED_THRESHOLD"
    saved = os.environ.pop(env_name, None)
    try:
        recognizer = IntentRecognizer(api_key="offline-no-key", model="offline-stub-model")
        check(
            "默认值为标定值 0.15",
            approx(recognizer.degraded_threshold, 0.15) and approx(DEFAULT_DEGRADED_THRESHOLD, 0.15),
            f"degraded_threshold={recognizer.degraded_threshold}",
        )
        check(
            "正常路径阈值仍为 0.5（未被改动）",
            approx(recognizer.threshold, 0.5),
            f"threshold={recognizer.threshold}",
        )

        os.environ[env_name] = "0.42"
        from_env = IntentRecognizer(api_key="offline-no-key", model="offline-stub-model")
        check("环境变量覆盖生效", approx(from_env.degraded_threshold, 0.42), f"={from_env.degraded_threshold}")

        os.environ[env_name] = "not-a-number"
        bad = IntentRecognizer(api_key="offline-no-key", model="offline-stub-model")
        check("非法环境变量回退默认值且不崩", approx(bad.degraded_threshold, 0.15), f"={bad.degraded_threshold}")

        os.environ[env_name] = "3.7"
        out_of_range = IntentRecognizer(api_key="offline-no-key", model="offline-stub-model")
        check(
            "越界环境变量回退默认值",
            approx(out_of_range.degraded_threshold, 0.15),
            f"={out_of_range.degraded_threshold}",
        )

        explicit = IntentRecognizer(
            api_key="offline-no-key", model="offline-stub-model", degraded_threshold=0.6
        )
        check("显式参数优先于环境变量", approx(explicit.degraded_threshold, 0.6), f"={explicit.degraded_threshold}")
    finally:
        os.environ.pop(env_name, None)
        if saved is not None:
            os.environ[env_name] = saved


def test_weight_normalisation() -> None:
    print("\n---- 2. 降级权重归一化（LLM 失败 + emb/pat 都有分）----")
    recognizer = make_recognizer()
    llm = case(OTHER, 0.0, failed=True)
    emb = case(IntentCategory.ACCESS, 0.6)
    pat = case(IntentCategory.PERMISSION_REQUEST, 1.0)

    outcome = recognizer._fuse(llm, emb, pat)
    check("返回 FusionOutcome", isinstance(outcome, FusionOutcome), type(outcome).__name__)
    check("degraded=True", outcome.degraded is True, f"degraded={outcome.degraded}")
    check("degraded_sources=['llm']", outcome.degraded_sources == ["llm"], str(outcome.degraded_sources))
    check(
        "权重归一化 {embedding:2/3, pattern:1/3}",
        approx(outcome.weights_used.get("embedding", 0.0), 2.0 / 3.0)
        and approx(outcome.weights_used.get("pattern", 0.0), 1.0 / 3.0)
        and approx(sum(outcome.weights_used.values()), 1.0),
        str({k: round(v, 6) for k, v in outcome.weights_used.items()}),
    )
    check(
        "source_scores 兼容键齐全 + 新增 degraded/confidence_threshold",
        {"llm", "embedding", "pattern"}.issubset(outcome.source_scores)
        and approx(outcome.source_scores["degraded"], 1.0)
        and approx(outcome.source_scores["confidence_threshold"], DEFAULT_DEGRADED_THRESHOLD),
        f"keys={sorted(outcome.source_scores)}",
    )

    # 归一化后的加权是否真的生效：emb 0.6 → 2/3*0.6 = 0.4（不再是裸 0.6）
    cross_group = recognizer._fuse(
        case(OTHER, 0.0, failed=True),
        case(IntentCategory.ACCESS, 0.6),      # access 组 0.4
        case(IntentCategory.INCIDENT, 1.0),    # incident 组 0.3333，不同组不累加
    )
    check(
        "降级分 = 归一化权重 × 各路置信度（0.667×0.6=0.4）",
        cross_group.intent is IntentCategory.ACCESS and approx(cross_group.confidence, 0.4),
        f"intent={cross_group.intent.value} conf={cross_group.confidence:.6f}",
    )

    # 同组两路累加 + 组内 pattern 细化：best=access(0.4) 宽泛类、pattern=account_access 细类且同组
    same_group = recognizer._fuse(
        case(OTHER, 0.0, failed=True),
        case(IntentCategory.ACCESS, 0.6),      # 0.4
        case(IntentCategory.ACCOUNT_ACCESS, 1.0),      # 0.3333  → 同组合计 0.7333
    )
    check(
        "同组累加 0.7333 后仍保持组内细粒度提升（access→account_access）",
        same_group.intent is IntentCategory.ACCOUNT_ACCESS
        and approx(same_group.confidence, 1.0)
        and approx(same_group.source_scores.get("refined_by_pattern", 0.0), 1.0),
        f"intent={same_group.intent.value} conf={same_group.confidence:.4f}",
    )


def test_pattern_not_dropped() -> None:
    print("\n---- 3. 降级时 pattern 不再被丢弃（旧：emb 一定赢；新：按归一化分数选组 + 阈值）----")
    recognizer = make_recognizer()
    llm = case(OTHER, 0.0, failed=True)
    emb = case(IntentCategory.INCIDENT, 0.4)   # A：事件组 0.2667
    pat = case(IntentCategory.PERMISSION_REQUEST, 1.0)      # B：访问权限组 0.3333 → 组分数上 B 胜

    old_intent, old_conf, _ = legacy_vote(recognizer, llm, emb, pat)
    new_outcome = recognizer._fuse(llm, emb, pat)
    print(
        f"    旧实现: intent={old_intent.value:<14} conf={old_conf:.4f} "
        f"（直接返回 embedding，pattern 被丢）"
    )
    print(
        f"    新实现: intent={new_outcome.intent.value:<14} conf={new_outcome.confidence:.4f} "
        f"degraded={new_outcome.degraded} weights={ {k: round(v, 4) for k, v in new_outcome.weights_used.items()} }"
    )
    check(
        "旧实现 = embedding 单路（复现 bug）",
        old_intent is IntentCategory.INCIDENT and approx(old_conf, 0.4),
        f"{old_intent.value}/{old_conf}",
    )
    # 归一化后 pattern 的访问权限组(0.3333) 高于 emb 的事件组(0.2667)：决策已经不再由 emb 单方面决定；
    # 0.3333 > 标定阈值 0.15 → 接受 pattern 的细粒度意图（这正是修复要达到的效果）。
    check(
        "pattern 的高分组胜出并被接受（不再是 emb 一定赢）",
        new_outcome.intent is IntentCategory.PERMISSION_REQUEST
        and approx(new_outcome.confidence, 1.0 / 3.0)
        and new_outcome.degraded,
        f"intent={new_outcome.intent.value} conf={new_outcome.confidence:.6f}",
    )
    # 反向对照：把阈值抬到 0.5，同一输入必须落 OTHER —— 证明阈值仍然在门控
    strict = make_recognizer(degraded_threshold=0.5)
    strict_outcome = strict._fuse(llm, emb, pat)
    print(
        f"    阈值=0.50 时: intent={strict_outcome.intent.value} conf={strict_outcome.confidence:.4f}"
        f"（阈值抬高后保守落 OTHER，阈值门控有效）"
    )
    check(
        "阈值抬到 0.50 后同一输入落 OTHER（阈值门控有效）",
        strict_outcome.intent is IntentCategory.OTHER and approx(strict_outcome.confidence, 1.0 / 3.0),
        f"{strict_outcome.intent.value}/{strict_outcome.confidence:.4f}",
    )

    # 反向：emb 明确更强时仍应胜出，避免"修复成只信 pattern"
    emb_strong = case(IntentCategory.INCIDENT, 0.9)   # 0.6
    pat_weak = case(IntentCategory.PERMISSION_REQUEST, 0.5)        # 0.1667
    outcome2 = recognizer._fuse(case(OTHER, 0.0, failed=True), emb_strong, pat_weak)
    check(
        "emb 明显更强时仍由 emb 胜出（不是无脑偏向 pattern）",
        outcome2.intent is IntentCategory.INCIDENT,
        f"{outcome2.intent.value}/{outcome2.confidence:.4f}",
    )


def test_low_confidence_falls_back_to_other() -> None:
    print("\n---- 4. 全源低分 → OTHER（阈值真正生效）----")
    recognizer = make_recognizer()
    outcome = recognizer._fuse(
        case(OTHER, 0.0, failed=True),
        case(IntentCategory.INCIDENT, 0.10),   # 0.10 * 0.6667 = 0.0667
        case(IntentCategory.PERMISSION_REQUEST, 0.10),      # 0.10 * 0.3333 = 0.0333
    )
    check(
        "融合分低于标定阈值 0.15 → OTHER",
        outcome.intent is IntentCategory.OTHER and outcome.confidence < DEFAULT_DEGRADED_THRESHOLD,
        f"intent={outcome.intent.value} conf={outcome.confidence:.4f} "
        f"threshold={outcome.source_scores['confidence_threshold']}",
    )
    print(
        f"    旧实现在同一输入下返回: {legacy_vote(recognizer, case(OTHER, 0.0, failed=True), case(IntentCategory.INCIDENT, 0.10), case(IntentCategory.PERMISSION_REQUEST, 0.10))}"
    )

    # 恰好在阈值允许范围内的单路结果应被接受（权重 1.0）
    outcome_edge = recognizer._fuse(
        case(OTHER, 0.0, failed=True),
        case(IntentCategory.OTHER, 0.0),
        case(IntentCategory.PERMISSION_REQUEST, 1.0),      # 仅有 pattern，权重 1.0 → 1.0
    )
    check(
        "仅 pattern 可用时权重=1.0 且被接受",
        outcome_edge.intent is IntentCategory.PERMISSION_REQUEST
        and approx(outcome_edge.weights_used.get("pattern", 0.0), 1.0)
        and outcome_edge.degraded_sources == ["llm", "embedding"],
        f"intent={outcome_edge.intent.value} conf={outcome_edge.confidence:.4f} "
        f"weights={outcome_edge.weights_used} sources={outcome_edge.degraded_sources}",
    )


def test_no_source_available() -> None:
    print("\n---- 5. 所有源都不可用 → OTHER / degraded=True ----")
    recognizer = make_recognizer()
    outcome = recognizer._fuse(case(OTHER, 0.0, failed=True), case(OTHER, 0.0), case(OTHER, 0.0))
    check(
        "intent=OTHER, confidence=0.0",
        outcome.intent is IntentCategory.OTHER and approx(outcome.confidence, 0.0),
        f"{outcome.intent.value}/{outcome.confidence}",
    )
    check("degraded=True 且 sources 列出三路", outcome.degraded and outcome.degraded_sources == ["llm", "embedding", "pattern"], str(outcome.degraded_sources))
    check("weights_used 为空", outcome.weights_used == {}, str(outcome.weights_used))


def test_normal_path_identical() -> None:
    print("\n---- 6. LLM 正常路径回归：新 _vote vs 旧实现（必须逐位一致）----")
    cases: List[Tuple[str, Dict, Dict, Dict, float, bool]] = [
        # (名称, llm, emb, pat, 正常路径阈值, embedding_enabled)
        # 前 3 条来自 tests/test_intent_recognizer.py（期望 0.63 / 0.22 / 0.61）
        (
            "tests/用例1 跨组不细化",
            case(IntentCategory.ACCESS, 0.9),
            case(OTHER, 0.0),
            case(IntentCategory.APPLICATION_ERROR, 0.5),
            0.5,
            True,
        ),
        (
            "tests/用例2 0.3/0.9/0.4 → application_error",
            case(IntentCategory.ACCESS, 0.3),
            case(IntentCategory.APPLICATION_ERROR, 0.9),
            case(IntentCategory.INCIDENT, 0.4),
            0.2,   # 原测试显式把阈值降到 0.2，否则 0.22 会被判 OTHER
            True,
        ),
        (
            "tests/用例3 同组细粒度提升",
            case(IntentCategory.INCIDENT, 0.8),
            case(OTHER, 0.0),
            case(IntentCategory.APPLICATION_ERROR, 0.5),
            0.5,
            True,
        ),
        # 自造 3 条正常用例
        (
            "自造1 三路一致（incident）",
            case(IntentCategory.INCIDENT, 0.94),
            case(IntentCategory.INCIDENT, 0.81),
            case(IntentCategory.APPLICATION_ERROR, 0.75),
            0.5,
            True,
        ),
        (
            "自造2 问候双路命中（greeting）",
            case(IntentCategory.GREETING, 0.99),
            case(IntentCategory.GREETING, 0.88),
            case(OTHER, 0.0),
            0.5,
            True,
        ),
        (
            "自造3 低分落 OTHER（permission_request 0.55 不够）",
            case(IntentCategory.PERMISSION_REQUEST, 0.55),
            case(OTHER, 0.0),
            case(IntentCategory.PERMISSION_REQUEST, 0.5),
            0.5,
            True,
        ),
    ]

    for name, llm, emb, pat, threshold, embedding_enabled in cases:
        recognizer = make_recognizer(threshold=threshold, embedding_enabled=embedding_enabled)
        old = legacy_vote(recognizer, llm, emb, pat)
        new = recognizer._vote(llm, emb, pat)
        new_scores = {k: v for k, v in new[2].items() if k not in ("degraded", "confidence_threshold")}
        same = (
            old[0] is new[0]
            and old[1] == new[1]
            and {k: v for k, v in old[2].items()} == new_scores
        )
        print(
            f"    {name:<28} 旧=({old[0].value:<14} {old[1]:.6f})  新=({new[0].value:<14} {new[1]:.6f})  "
            f"{'一致' if same else '不一致!'}"
        )
        check(f"正常路径逐位一致 [{name}]", same, f"old={old[0].value}/{old[1]} new={new[0].value}/{new[1]}")

    # 与既有测试常量交叉校验（防止 legacy_vote 复刻本身写错）
    recognizer = make_recognizer()
    first = recognizer._vote(
        case(IntentCategory.ACCESS, 0.9), case(OTHER, 0.0), case(IntentCategory.APPLICATION_ERROR, 0.5)
    )
    check("既有测试常量 0.63", first[1] == 0.63, f"conf={first[1]}")
    second = make_recognizer(threshold=0.2)._vote(
        case(IntentCategory.ACCESS, 0.3),
        case(IntentCategory.APPLICATION_ERROR, 0.9),
        case(IntentCategory.INCIDENT, 0.4),
    )
    check("既有测试常量 0.22", abs(second[1] - 0.22) < 1e-12, f"conf={second[1]}")
    third = recognizer._vote(
        case(IntentCategory.INCIDENT, 0.8), case(OTHER, 0.0), case(IntentCategory.APPLICATION_ERROR, 0.5)
    )
    check(
        "既有测试常量 0.61 + refined_by_pattern",
        abs(third[1] - 0.61) < 1e-12 and third[2].get("refined_by_pattern") == 0.5,
        f"conf={third[1]} refined={third[2].get('refined_by_pattern')}",
    )

    # _embedding_enabled=False 分支（离线 harness 的 c-ablation 依赖它）
    recognizer = make_recognizer(embedding_enabled=False)
    old = legacy_vote(
        recognizer,
        case(IntentCategory.ACCESS, 0.6),
        case(IntentCategory.ACCESS, 0.7),
        case(IntentCategory.PERMISSION_REQUEST, 0.75),
    )
    new = recognizer._vote(
        case(IntentCategory.ACCESS, 0.6),
        case(IntentCategory.ACCESS, 0.7),
        case(IntentCategory.PERMISSION_REQUEST, 0.75),
    )
    check(
        "_embedding_enabled=False 时仍走 0.85/0.15 分支（c-ablation 语义保持）",
        old[0] is new[0] and old[1] == new[1],
        f"old={old[0].value}/{old[1]} new={new[0].value}/{new[1]}",
    )


def test_recognize_offline() -> None:
    print("\n---- 7. recognize() 端到端（LLM 桩恒失败）写出 degraded 字段 ----")

    async def run() -> None:
        recognizer = IntentRecognizer(api_key="offline-no-key", model="offline-stub-model")
        message = "登录一直报401，帮我看看"
        result = await recognizer.recognize(message)
        print(
            f"    intent={result.intent.value} conf={result.confidence:.4f} "
            f"degraded={result.degraded} sources={result.degraded_sources} "
            f"threshold={result.source_scores.get('confidence_threshold')}"
        )
        check("recognize 返回 degraded=True", result.degraded is True, str(result.degraded))
        check("degraded_sources 含 'llm'", "llm" in result.degraded_sources, str(result.degraded_sources))
        check(
            "source_scores 带 degraded / confidence_threshold",
            approx(result.source_scores.get("degraded", -1.0), 1.0)
            and approx(result.source_scores.get("confidence_threshold", -1.0), DEFAULT_DEGRADED_THRESHOLD),
            str({k: result.source_scores[k] for k in ("degraded", "confidence_threshold")}),
        )
        check("intent_group 与意图一致", result.intent_group == recognizer._intent_group(result.intent), result.intent_group)

        # 缓存命中返回同一对象（缓存逻辑未改动）
        cached = await recognizer.recognize(message)
        check("缓存命中返回同一结果对象（缓存逻辑未改）", cached is result, f"hits={recognizer.cache_hits}")

        # 冒烟：三条消息均应产出合法枚举，不抛异常
        for text in ("申请权限", "你好", "INC-2026 处理到哪了"):
            item = await recognizer.recognize(text)
            check(
                f"冒烟 [{text}] 返回合法 intent",
                isinstance(item.intent, IntentCategory) and 0.0 <= item.confidence <= 1.0,
                f"{item.intent.value}/{item.confidence:.4f}/degraded={item.degraded}",
            )

    asyncio.run(run())


def main() -> int:
    print("=" * 78)
    print("OpsMind 意图融合降级修复（B+C）离线自检")
    print(f"Python      : {sys.version.split()[0]}")
    print(f"项目根      : {PROJECT_ROOT}")
    print(f"依赖桩      : {STUBS}")
    print("=" * 78)

    try:
        test_threshold_config()
        test_weight_normalisation()
        test_pattern_not_dropped()
        test_low_confidence_falls_back_to_other()
        test_no_source_available()
        test_normal_path_identical()
        test_recognize_offline()
    except Exception:  # noqa: BLE001 - 自检脚本要打印完整栈后失败
        traceback.print_exc()
        print("\n[verify] 自检过程中抛异常 → FAIL", file=sys.stderr)
        return 1

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 78)
    print(f"自检结果: {passed}/{total} PASS" + ("（全部通过）" if passed == total else "（存在 FAIL）"))
    print("=" * 78)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
