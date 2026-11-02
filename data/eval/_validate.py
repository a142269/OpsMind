#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpsMind 离线评测集自检脚本（仅依赖 Python 标准库，无需联网/无第三方包）。

检查项：
  1. intent_cases.json / dialog_cases.json 可解析、字段合法、ID 唯一；
  2. 意图标签全部来自 core.intent_recognizer.IntentCategory（19 类）；
  3. expected_group 与 intent_recognizer._INTENT_GROUPS 的映射一致；
  4. 每类 >= 5 条、总数 >= 120、难度分布约 4:4:2；
  5. 与 _TEMPLATES（规则路模板）及 evaluation.evaluator.DEFAULT_INTENT_CASES
     做完全一致 / 包含 / 字符串相似度检测，防止数据泄漏；
  6. hard 样本覆盖混合意图、干扰词、错别字、指代、极短句；
  7. dialog_cases.json 的 Agent 路由、CRITICAL 升级、澄清、复合问题覆盖；
  8. 对话轮次与意图用例之间不存在重复文本。

运行：
  <python> OpsMind/data/eval/_validate.py
退出码 0 表示全部 PASS。
"""
from __future__ import annotations

import ast
import difflib
import json
import re
import sys
from collections import Counter
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
ECHOMIND_DIR = EVAL_DIR.parents[1]
INTENT_SRC = ECHOMIND_DIR / "core" / "intent_recognizer.py"
ORCH_SRC = ECHOMIND_DIR / "agents" / "agent_orchestrator.py"
EVALUATOR_SRC = ECHOMIND_DIR / "evaluation" / "evaluator.py"
INTENT_JSON = EVAL_DIR / "intent_cases.json"
DIALOG_JSON = EVAL_DIR / "dialog_cases.json"

VALID_AGENTS = ("service_desk", "incident", "access", "escalation")
VALID_DIFFICULTY = ("easy", "medium", "hard")
DIFFICULTY_TARGET = {"easy": (0.33, 0.47), "medium": (0.33, 0.47), "hard": (0.14, 0.26)}
LEAK_RATIO = 0.80
MIN_TOTAL = 120
MIN_PER_INTENT = 5
MIN_DIALOGS = 12
CASE_KEYS = ("id", "message", "expected_intent", "expected_group", "difficulty", "note")

_PUNCT_RE = re.compile(
    r"[\s，。！？、；：“”‘’（）【】《》…—\-·,.!?;:'\"()\[\]{}~/\\|@#$%^&*+=<>]+"
)


def normalize(text: str) -> str:
    """去掉空白与中英文标点，用于泄漏检测（避免'仅改标点'的复制）。"""
    return _PUNCT_RE.sub("", text).lower()


def parse_enum(path: Path, class_name: str, ) -> dict:
    """解析源码中的 Enum 定义，返回 {成员名: 值}。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            values = {}
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                ):
                    try:
                        value = ast.literal_eval(stmt.value)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(value, str):
                        values[stmt.targets[0].id] = value
            return values
    raise RuntimeError(f"{path} 中找不到 {class_name}")


def _enum_member(node: ast.AST, enum_name: str) -> str:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == enum_name
    ):
        return node.attr
    raise ValueError(f"不是 {enum_name} 的枚举成员: {ast.dump(node)[:80]}")


def _module_assignments(path: Path):
    """遍历源码中的赋值（含带类型注解的 AnnAssign，含类体内的类属性），产出 (target 名, value 节点)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    yield target.id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                yield node.target.id, node.value


def parse_intent_assets() -> tuple:
    """从源码解析意图枚举、_TEMPLATES、_INTENT_GROUPS。"""
    enum_values = parse_enum(INTENT_SRC, "IntentCategory")
    templates: dict[str, list[str]] = {}
    groups: dict[str, str] = {}
    for target, value in _module_assignments(INTENT_SRC):
        if target == "_TEMPLATES" and isinstance(value, ast.Dict):
            for key, item in zip(value.keys, value.values):
                cat = enum_values[_enum_member(key, "IntentCategory")]
                templates[cat] = [ast.literal_eval(entry) for entry in item.elts]
        elif target == "_INTENT_GROUPS" and isinstance(value, ast.Dict):
            for key, item in zip(value.keys, value.values):
                child = _enum_member(key, "IntentCategory")
                groups[child] = enum_values[_enum_member(item, "IntentCategory")]
    return enum_values, templates, groups


def parse_intent_routing() -> dict[str, str]:
    """解析 AgentOrchestrator._INTENT_ROUTING：意图值 -> Agent 类型值。"""
    agent_values = parse_enum(ORCH_SRC, "AgentType")
    intent_values = parse_enum(INTENT_SRC, "IntentCategory")
    for target, value in _module_assignments(ORCH_SRC):
        if target == "_INTENT_ROUTING" and isinstance(value, ast.Dict):
            routing = {}
            for key, item in zip(value.keys, value.values):
                intent = intent_values[_enum_member(key, "IntentCategory")]
                routing[intent] = agent_values[_enum_member(item, "AgentType")]
            return routing
    raise RuntimeError("找不到 _INTENT_ROUTING")


def parse_builtin_cases() -> list[tuple[str, str]]:
    """解析 DEFAULT_INTENT_CASES，作为第二个防泄漏参照源。"""
    cases = []
    for target, value in _module_assignments(EVALUATOR_SRC):
        if target != "DEFAULT_INTENT_CASES" or not isinstance(value, ast.List):
            continue
        for elt in value.elts:
            if (
                isinstance(elt, ast.Call)
                and isinstance(elt.func, ast.Name)
                and elt.func.id == "IntentTestCase"
                and len(elt.args) >= 2
            ):
                msg = ast.literal_eval(elt.args[0])
                intent = ast.literal_eval(elt.args[1])
                if isinstance(msg, str) and isinstance(intent, str):
                    cases.append((msg, intent))
    return cases


class Report:
    def __init__(self) -> None:
        self.checks: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        self.checks.append((bool(ok), name, detail))
        return bool(ok)

    @property
    def failures(self) -> list[tuple[bool, str, str]]:
        return [item for item in self.checks if not item[0]]


def main() -> int:
    report = Report()
    try:
        intent_cases = json.loads(INTENT_JSON.read_text(encoding="utf-8"))
        dialog_cases = json.loads(DIALOG_JSON.read_text(encoding="utf-8"))
    except Exception as ex:  # noqa: BLE001
        print(f"[FAIL] JSON 无法解析: {ex}")
        return 1

    enum_values, templates, groups = parse_intent_assets()
    routing = parse_intent_routing()
    builtin = parse_builtin_cases()
    valid_intents = set(enum_values.values())
    group_of = {
        intent: groups.get(name, enum_values[name]) for name, intent in enum_values.items()
    }

    print("=" * 76)
    print("OpsMind 离线评测集自检")
    print("=" * 76)
    print(f"意图枚举: {len(valid_intents)} 类 | 规则模板: {sum(len(v) for v in templates.values())} 条 "
          f"| 内置用例: {len(builtin)} 条 | 路由映射: {len(routing)} 条")
    print()

    # ── 1. 意图用例结构 ──────────────────────────────────────────────────────
    report.check(isinstance(intent_cases, list), "intent_cases.json 顶层为数组")
    intent_cases = intent_cases if isinstance(intent_cases, list) else []
    report.check(len(intent_cases) >= MIN_TOTAL, f"意图用例总数 >= {MIN_TOTAL}",
                 f"实际 {len(intent_cases)} 条")

    ids: list[str] = []
    bad_items: list[str] = []
    for idx, case in enumerate(intent_cases):
        where = f"第 {idx + 1} 条"
        if not isinstance(case, dict):
            bad_items.append(f"{where} 不是对象")
            continue
        missing = [key for key in CASE_KEYS if key not in case]
        if missing:
            bad_items.append(f"{where} 缺字段 {missing}")
            continue
        ids.append(str(case["id"]))
        if not re.fullmatch(r"I\d{3}", str(case["id"])):
            bad_items.append(f"{where} id 格式错误: {case['id']}")
        if not isinstance(case["message"], str) or not case["message"].strip():
            bad_items.append(f"{where} message 为空")
        if case["expected_intent"] not in valid_intents:
            bad_items.append(f"{where} 非法意图 {case['expected_intent']}")
        elif case["expected_group"] != group_of[case["expected_intent"]]:
            bad_items.append(
                f"{where} 组字段应为 {group_of[case['expected_intent']]}，实际 {case['expected_group']}"
            )
        if case["difficulty"] not in VALID_DIFFICULTY:
            bad_items.append(f"{where} 非法难度 {case['difficulty']}")
        if not isinstance(case["note"], str):
            bad_items.append(f"{where} note 不是字符串")
        elif case["difficulty"] == "hard" and not case["note"].strip():
            bad_items.append(f"{where} hard 用例缺少判定说明")
    report.check(not bad_items, "字段/取值校验", "; ".join(bad_items[:6]))

    report.check(len(ids) == len(set(ids)), "意图用例 ID 唯一",
                 f"重复 ID: {[i for i, n in Counter(ids).items() if n > 1][:5]}")
    report.check(len({normalize(c["message"]) for c in intent_cases if isinstance(c, dict) and isinstance(c.get("message"), str)})
                 == len(intent_cases), "意图用例句子无重复")

    # ── 2. 意图覆盖与难度分布 ────────────────────────────────────────────────
    per_intent = Counter(c["expected_intent"] for c in intent_cases if c.get("expected_intent") in valid_intents)
    uncovered = [intent for intent in sorted(valid_intents) if per_intent[intent] < MIN_PER_INTENT]
    report.check(not uncovered, f"19 类意图每类 >= {MIN_PER_INTENT} 条",
                 f"不足的类: {[(i, per_intent[i]) for i in uncovered]}")
    report.check(set(per_intent) == valid_intents, "19 类意图全覆盖",
                 f"缺失: {sorted(valid_intents - set(per_intent))}")

    diff_counter = Counter(c["difficulty"] for c in intent_cases if c.get("difficulty") in VALID_DIFFICULTY)
    total = len(intent_cases)
    dist_ok = True
    dist_detail = []
    for level in VALID_DIFFICULTY:
        share = diff_counter[level] / total if total else 0.0
        low, high = DIFFICULTY_TARGET[level]
        dist_ok = dist_ok and low <= share <= high
        dist_detail.append(f"{level}={diff_counter[level]}({share:.1%})")
    report.check(dist_ok, "难度分布约 4:4:2（easy/medium/hard 目标区间 33~47% / 33~47% / 14~26%）",
                 ", ".join(dist_detail))

    hard_cases = [c for c in intent_cases if c.get("difficulty") == "hard"]
    hard_notes = " ".join(str(c.get("note", "")) for c in hard_cases)
    for marker, label in (
        ("混合意图", "混合意图"),
        ("干扰词", "干扰词"),
        ("错别字", "错别字"),
        ("指代", "指代"),
    ):
        report.check(marker in hard_notes, f"hard 样本覆盖{label}")
    shortest_hard = min(
        (len(normalize(c["message"])) for c in hard_cases if isinstance(c.get("message"), str)),
        default=99,
    )
    report.check(
        shortest_hard <= 4,
        "hard 样本包含极短句",
        f"hard 最短句子规范化后 {shortest_hard} 字（如'喊个人来'）",
    )

    # ── 3. 防泄漏检测 ────────────────────────────────────────────────────────
    refs: list[tuple[str, str, str]] = []
    for intent, sentences in templates.items():
        for sentence in sentences:
            refs.append((sentence, intent, "_TEMPLATES"))
    for message, intent in builtin:
        refs.append((message, intent, "DEFAULT_INTENT_CASES"))

    leak_violations: list[str] = []
    max_ratio = 0.0
    max_pair = ("", "")
    compared = 0
    for case in intent_cases:
        message = case.get("message", "")
        norm_message = normalize(message)
        for ref_text, ref_intent, source in refs:
            norm_ref = normalize(ref_text)
            if not norm_message or not norm_ref:
                continue
            compared += 1
            if norm_message == norm_ref:
                leak_violations.append(f"{case.get('id')} 与 {source}/{ref_intent} 完全一致: {ref_text}")
                continue
            if len(norm_ref) >= 4 and norm_ref in norm_message:
                leak_violations.append(f"{case.get('id')} 包含 {source} 原句: {ref_text}")
                continue
            ratio = difflib.SequenceMatcher(None, norm_message, norm_ref, autojunk=False).ratio()
            if ratio > max_ratio:
                max_ratio, max_pair = ratio, (message, f"{source}/{ref_intent}: {ref_text}")
            if ratio >= LEAK_RATIO:
                leak_violations.append(
                    f"{case.get('id')} 与 {source} 相似度 {ratio:.3f} >= {LEAK_RATIO}: {ref_text}"
                )
    report.check(not leak_violations, "与 _TEMPLATES / 内置用例无重复或改标点复制",
                 f"{len(leak_violations)} 处: {leak_violations[:5]}")
    report.check(
        True,
        "最高相似度记录",
        f"{max_ratio:.3f}（{max_pair[0]} ↔ {max_pair[1]}），共比对 {compared} 次",
    )

    # ── 4. 对话用例 ──────────────────────────────────────────────────────────
    report.check(isinstance(dialog_cases, list), "dialog_cases.json 顶层为数组")
    dialog_cases = dialog_cases if isinstance(dialog_cases, list) else []
    report.check(len(dialog_cases) >= MIN_DIALOGS, f"对话用例 >= {MIN_DIALOGS} 组",
                 f"实际 {len(dialog_cases)} 组")

    dialog_bad: list[str] = []
    dialog_ids: list[str] = []
    dialog_turns: list[str] = []
    agent_counter: Counter = Counter()
    tag_counter: Counter = Counter()
    for idx, case in enumerate(dialog_cases):
        where = f"第 {idx + 1} 组"
        if not isinstance(case, dict):
            dialog_bad.append(f"{where} 不是对象")
            continue
        turns = case.get("turns")
        if not isinstance(turns, list) or not turns or not all(
            isinstance(turn, str) and turn.strip() for turn in turns
        ):
            dialog_bad.append(f"{where} turns 非空字符串数组")
        else:
            dialog_turns.extend(turns)
        dialog_ids.append(str(case.get("id", "")))
        if not re.fullmatch(r"D\d{2}", str(case.get("id", ""))):
            dialog_bad.append(f"{where} id 格式错误: {case.get('id')}")
        agent = case.get("expected_agent")
        if agent not in VALID_AGENTS:
            dialog_bad.append(f"{where} 非法 Agent: {agent}")
        else:
            agent_counter[agent] += 1
        intent = case.get("expected_intent")
        if intent not in valid_intents:
            dialog_bad.append(f"{where} 非法意图: {intent}")
        tags = case.get("tags")
        if not isinstance(tags, list) or not tags or not all(isinstance(tag, str) for tag in tags):
            dialog_bad.append(f"{where} tags 非空字符串数组")
        else:
            tag_counter.update(tags)
        if isinstance(intent, str) and isinstance(tags, list):
            expected = (
                "escalation"
                if "CRITICAL升级" in tags or intent in ("escalation", "human_handoff")
                else routing.get(intent, "service_desk")
            )
            if agent != expected:
                dialog_bad.append(f"{where} 路由不一致: intent={intent} 应路由 {expected}，实际 {agent}")
        supporting = case.get("expected_supporting_agents", [])
        if supporting and not all(item in VALID_AGENTS for item in supporting):
            dialog_bad.append(f"{where} expected_supporting_agents 含非法 Agent")
        if "复合问题" in (tags or []) and not supporting:
            dialog_bad.append(f"{where} 复合问题缺少 expected_supporting_agents")

    report.check(not dialog_bad, "对话用例字段/路由一致性", "; ".join(dialog_bad[:6]))
    report.check(len(dialog_ids) == len(set(dialog_ids)), "对话用例 ID 唯一")
    report.check(
        all(agent_counter[agent] >= 2 for agent in VALID_AGENTS),
        "4 个 Agent 路由各 >= 2 组",
        ", ".join(f"{agent}={agent_counter[agent]}" for agent in VALID_AGENTS),
    )
    report.check(tag_counter["CRITICAL升级"] >= 1, "含 1 组 CRITICAL 升级", f"实际 {tag_counter['CRITICAL升级']} 组")
    report.check(tag_counter["应澄清"] >= 1, "含应触发澄清的用例", f"实际 {tag_counter['应澄清']} 组")
    report.check(tag_counter["复合问题"] >= 2, "含 >= 2 组复合问题", f"实际 {tag_counter['复合问题']} 组")

    # ── 5. 跨文件重复文本 ────────────────────────────────────────────────────
    intent_texts = Counter(normalize(c["message"]) for c in intent_cases if isinstance(c.get("message"), str))
    dup_across = [turn for turn in dialog_turns if intent_texts[normalize(turn)]]
    report.check(not dup_across, "对话轮次与意图用例无重复句子", f"{dup_across[:3]}")
    report.check(len(dialog_turns) == len({normalize(turn) for turn in dialog_turns}),
                 "对话轮次文本无重复", f"共 {len(dialog_turns)} 轮")

    # ── 6. 统计输出 ──────────────────────────────────────────────────────────
    print("意图分布（19 类）：")
    print("| 意图 | 意图组 | easy | medium | hard | 小计 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for intent in sorted(valid_intents):
        subset = [c for c in intent_cases if c.get("expected_intent") == intent]
        counts = Counter(c.get("difficulty") for c in subset)
        print(
            f"| {intent} | {group_of[intent]} | {counts['easy']} | {counts['medium']} "
            f"| {counts['hard']} | {len(subset)} |"
        )
    print()
    print("难度分布：")
    print("| 难度 | 条数 | 占比 | 目标区间 |")
    print("| --- | --- | --- | --- |")
    for level in VALID_DIFFICULTY:
        share = diff_counter[level] / total if total else 0.0
        low, high = DIFFICULTY_TARGET[level]
        print(f"| {level} | {diff_counter[level]} | {share:.1%} | {low:.0%}~{high:.0%} |")
    print()
    print("对话集覆盖：")
    print("| 维度 | 结果 |")
    print("| --- | --- |")
    print(f"| 对话组数 | {len(dialog_cases)} |")
    for agent in VALID_AGENTS:
        print(f"| {agent} 路由 | {agent_counter[agent]} 组 |")
    print(f"| CRITICAL 升级 | {tag_counter['CRITICAL升级']} 组 |")
    print(f"| 应澄清 | {tag_counter['应澄清']} 组 |")
    print(f"| 复合问题 | {tag_counter['复合问题']} 组 |")
    print(f"| 对话轮次 | {len(dialog_turns)} 轮 |")
    print()

    print("检查明细：")
    for ok, name, detail in report.checks:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}" + (f" —— {detail}" if detail and ok else ""))
        if not ok and detail:
            print(f"          {detail}")
    print()
    failures = report.failures
    print(f"检查合计 {len(report.checks)} 项：PASS {len(report.checks) - len(failures)}，FAIL {len(failures)}")
    if failures:
        print("结论: FAIL")
        return 1
    print("结论: 全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
