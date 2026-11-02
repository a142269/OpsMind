"""
LLM-as-Judge 输出的解析与校验 —— 纯标准库模块（零第三方依赖）。

为什么单独抽成文件：
  `evaluator.py` 依赖 `anthropic` / `core.*`，在没有安装这些依赖的环境里无法 import；
  而 Judge 输出解析/校验本身只涉及字符串、JSON 和数值处理。
  抽成独立模块后，`_verify_judge_parse.py` 可以直接 import 并真实跑回归，不需要起服务。

两类失败要分开处理，避免把无效 Judge 输出误计为中等质量回复：
  1. 解析失败：模型返回了空串 / markdown 围栏 / 前后带解释文字；
  2. 校验失败：解析出的 JSON 缺维度、值是 bool / 字符串、或超出 0~1 量纲。
  两者都不应该被当成"模型恰好答了中等水平（0.5）"。
"""
import json
import math
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

# Judge 必须给出的四个维度（与 LLMJudge.JUDGE_PROMPT 保持一致）
SCORE_KEYS: Tuple[str, ...] = (
    "relevance",
    "accuracy",
    "completeness",
    "helpfulness",
)

# 部分模型会把四个维度再包一层，例如 {"scores": {...}}；校验时允许剥这一层
_SCORE_WRAPPERS: Tuple[str, ...] = ("scores", "score", "evaluation", "result", "data")

# ```json ... ``` / ``` ... ``` 围栏；未闭合的围栏由“括号配平”兜底
_FENCED_RE = re.compile(r"```[ \t]*(?:json|JSON|Json)?[ \t]*\r?\n?(.+?)(?:```|\Z)", re.DOTALL)


def _iter_balanced_objects(text: str) -> Iterator[str]:
    """
    按出现顺序产出 text 中所有“括号配平的 {...}”子串。

    可正确跳过字符串内部的 `{` / `}` 以及转义引号：
      {"note": "这里有个 } 和 \" 引号"}
    对于前面出现孤立 `{` 的场景（例如解释文字里的花括号），会继续向后寻找下一个候选。
    """
    if not text:
        return
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            cur = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif cur == "\\":
                    escaped = True
                elif cur == '"':
                    in_string = False
                continue
            if cur == '"':
                in_string = True
            elif cur == "{":
                depth += 1
            elif cur == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
                    break


def _try_loads(candidate: str) -> Optional[Dict[str, Any]]:
    """尝试把一个候选子串解析成 dict；失败或结果不是对象时返回 None。"""
    if not isinstance(candidate, str):
        return None
    candidate = candidate.strip()
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _iter_candidates(text: str) -> Iterator[str]:
    """按“直接解析 → 剥围栏 → 括号配平”的优先级产出候选子串。"""
    yield text
    for block in _FENCED_RE.findall(text):
        yield block
        yield from _iter_balanced_objects(block)
    yield from _iter_balanced_objects(text)


def extract_json_object(text: Any) -> Optional[Dict[str, Any]]:
    """
    从模型输出里尽力提取第一个 JSON 对象。

    尝试顺序：
      1. 直接 json.loads 整段文本（容忍首尾空白）
      2. 剥离 ```json / ``` 围栏后再解析（围栏未闭合也能匹配）
      3. 在文本中做括号配平，取第一个平衡的 {...} 再解析
      4. 全部失败返回 None

    只返回 dict；模型如果输出 JSON 数组/标量，视为提取失败。
    """
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", errors="ignore")
    if not isinstance(text, str) or not text.strip():
        return None
    for candidate in _iter_candidates(text):
        data = _try_loads(candidate)
        if data is not None:
            return data
    return None


def _candidate_score_dicts(obj: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """产出 obj 自身，以及 obj 里可能包裹四维评分的嵌套 dict。"""
    yield obj
    for wrapper in _SCORE_WRAPPERS:
        inner = obj.get(wrapper)
        if isinstance(inner, dict):
            yield inner


def validate_judge_scores(
    obj: Any,
) -> Tuple[bool, Optional[Dict[str, float]], str]:
    """
    校验并归一化 Judge 评分。

    返回 ``(ok, normalized, error)``：
      - ok=True 时 normalized 是四维齐全、取值已落到 0~1 的 dict，error 为空串；
      - ok=False 时 normalized 为 None，error 说明首个失败原因。

    规则：
      - 必须是 JSON 对象（dict），允许被 {"scores": {...}} 之类包一层；
      - relevance / accuracy / completeness / helpfulness 四个键必须齐全；
      - 每个值必须是数字：拒绝 bool（`True` 是 int 子类，必须显式排除）、
        拒绝字符串、拒绝 NaN / inf；
      - 统一量纲：四项都在 0~1 按原值；都在 0~10 自动除以 10；
        负值或超过 10 视为非法（不做裁剪，避免把坏数据洗成“好分数”）。
    """
    if not isinstance(obj, dict):
        return False, None, f"judge 输出不是 JSON 对象（实际类型: {type(obj).__name__}）"

    data: Optional[Dict[str, Any]] = None
    for candidate in _candidate_score_dicts(obj):
        if any(key in candidate for key in SCORE_KEYS):
            data = candidate
            break
    if data is None:
        return False, None, f"judge 输出缺少维度: {'、'.join(SCORE_KEYS)}（未找到任何评分对象）"

    missing = [key for key in SCORE_KEYS if key not in data]
    if missing:
        return False, None, f"judge 输出缺少维度: {'、'.join(missing)}"

    raw: Dict[str, float] = {}
    for key in SCORE_KEYS:
        value = data[key]
        if isinstance(value, bool):
            return False, None, f"维度 {key} 不是数字（bool 不接受: {value!r}）"
        if isinstance(value, str):
            return False, None, f"维度 {key} 不是数字（字符串不接受: {value!r}）"
        if not isinstance(value, (int, float)):
            return False, None, f"维度 {key} 不是数字（实际类型: {type(value).__name__}）"
        number = float(value)
        if not math.isfinite(number):
            return False, None, f"维度 {key} 不是有限数值: {number!r}"
        raw[key] = number

    if all(0.0 <= value <= 1.0 for value in raw.values()):
        scale = 1.0
    elif all(0.0 <= value <= 10.0 for value in raw.values()):
        scale = 10.0
    else:
        over = "、".join(f"{key}={raw[key]:g}" for key in SCORE_KEYS)
        return False, None, f"维度取值越界（要求 0~1 或 0~10）: {over}"

    normalized = {
        key: round(raw[key] / scale, 4)
        for key in SCORE_KEYS
    }
    return True, normalized, ""


def parse_judge_scores(text: Any) -> Tuple[bool, Optional[Dict[str, float]], str]:
    """``extract_json_object`` + ``validate_judge_scores`` 的组合入口。"""
    obj = extract_json_object(text)
    if obj is None:
        return False, None, "无法从 judge 输出中提取 JSON 对象（空输出 / 无 JSON / 括号不配平）"
    return validate_judge_scores(obj)
