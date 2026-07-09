"""Extractor — prompt template, parsing, and dedup pipeline (§3.2)."""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .schema import (
    ValidationError,
    deduplicate_extraction,
    validate_extraction,
)

logger = logging.getLogger("lobster_memory.extractor")


# ── Extraction prompt (§3.2 layer 1) ────────────────────

EXTRACTION_SYSTEM_PROMPT = """[记忆抽取指令 — 每轮对话结束后执行]

你需要在刚结束的这一轮对话中提取"值得记入长期记忆的关键点"。
只提取有实质信息的内容，纯寒暄/确认/打招呼/简单回应不提取。

输出严格按以下 JSON 格式，不要多余文本，不要 markdown 代码块标记：

{
  "nodes": [
    {
      "id": "稳定标识符(英文或拼音，避免空格和特殊字符)",
      "label": "可读中文名",
      "domain": "emotion|knowledge|task",
      "type": "person|concept|task|fact|event|emotion",
      "content": "简短摘要(可选)",
      "weight": 1.0
    }
  ],
  "edges": [
    {
      "from": "源节点id",
      "to": "目标节点id",
      "kind": "relates_to|caused|part_of|feedback|derived",
      "weight": 1.0,
      "feedback_category": "behavior|understanding|idea|action",
      "valence": 0.0,
      "domain": "emotion|knowledge|task"
    }
  ]
}

提取规则（按优先级）：
1. 识别本轮出现的实体/事件/判断 → node；domain 和 type 都要填
   - 情绪相关（挫败、开心、焦虑、鼓励等）→ domain=emotion
   - 知识/技术话题 → domain=knowledge
   - 工作任务/项目/流程 → domain=task
2. 识别关系 → edge；kind 选最准确的：
   - relates_to: 一般关联
   - caused: 因果关系
   - part_of: 部分-整体
   - derived: 派生/引申
3. 识别"用户对龙虾的批评或表扬" → edge(kind=feedback)
   - 必须填 feedback_category 和 valence
   - 批评=负值(建议 -0.6 到 -0.8)，表扬=正值(建议 +0.6 到 +0.8)
   - behavior: 行为层面的反馈
   - understanding: 理解/认知层面的反馈
   - idea: 想法/创意层面的反馈
   - action: 做法/执行层面的反馈
4. 新旧合并：看下面「已有节点」列表，如果本轮实体已存在，用已有 id
5. 同义词归一化：不同称呼但同一实体用同一个 id
6. 如果本轮没有值得记的内容，输出 {"nodes": [], "edges": []}

---
[图中已有节点 — 用于判断新旧合并]
{existing_nodes}
---
"""


def build_extraction_prompt(
    turn_text: str,
    existing_labels: Dict[str, str],
) -> str:
    """
    Build the full extraction prompt for one conversation turn.

    Args:
        turn_text: The current turn's conversation text.
        existing_labels: {id_str: label} from current live nodes.
    """
    node_list = "\n".join(
        f"  - {nid}: {label}" for nid, label in list(existing_labels.items())[:200]
    )
    if not node_list:
        node_list = "  (暂无已有节点)"
    return EXTRACTION_SYSTEM_PROMPT.replace("{existing_nodes}", f"\n{node_list}\n")


# ── Parsing (§3.2 layer 2) ──────────────────────────────

def parse_extraction(raw_text: str) -> Tuple[dict, Optional[str]]:
    """
    Parse LLM extraction output into a validated dict.
    Returns (extracted_dict, error_message|None).
    """
    # Strip markdown code fences if present
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    # Find JSON object
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        text = text[start:end]
    except ValueError:
        return {"nodes": [], "edges": []}, "no JSON object found in extraction output"

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"extraction JSON parse error: {e}")
        return {"nodes": [], "edges": []}, f"JSON parse error: {e}"

    try:
        validated = validate_extraction(raw)
    except ValidationError as e:
        logger.warning(f"extraction validation failed: {e}")
        return {"nodes": [], "edges": []}, f"validation error: {e}"

    # Empty extraction is valid (nothing to remember)
    if not validated["nodes"] and not validated["edges"]:
        return validated, None

    return validated, None


def run_extraction(
    turn_text: str,
    existing_labels: Dict[str, str],
) -> Tuple[dict, Optional[str]]:
    """
    Full extraction pipeline:
    1. Build prompt
    2. (The LLM call is done externally by the lobster agent)
    3. This function returns the prompt that should be sent to the LLM,
       and expects the caller to pass the LLM's response to parse_extraction.

    This separation exists because the lobster agent manages its own
    LLM invocation; we only provide the prompt and parse the result.
    """
    prompt = build_extraction_prompt(turn_text, existing_labels)
    return prompt  # type: ignore  # caller sends this to LLM, then calls parse_extraction


def process_extraction_result(
    raw_response: str,
    existing_labels: Dict[str, str],
) -> Tuple[dict, Optional[str]]:
    """
    Full post-LLM pipeline: parse → validate → dedup.
    Returns (final_extracted, error|None).
    """
    parsed, err = parse_extraction(raw_response)
    if err:
        return parsed, err
    if not parsed["nodes"] and not parsed["edges"]:
        return parsed, None

    # Layer 3: deduplicate
    deduped = deduplicate_extraction(parsed, existing_labels)
    return deduped, None
