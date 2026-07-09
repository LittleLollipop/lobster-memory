"""Recall path — pure on-demand query with in-memory access logging (§4)."""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .memory_graph import MemoryGraph
from .schema import dict_from_props

logger = logging.getLogger("lobster_memory.recall")

# ── In-memory access log (not written to disk until consolidation) ──

_access_log: List[Tuple[str, float]] = []


def log_access(id_str: str):
    """Record that a vertex/edge was recalled and deemed useful."""
    _access_log.append((id_str, time.time()))


def drain_access_log() -> List[Tuple[str, float]]:
    """Drain and return the current access log (for consolidation)."""
    global _access_log
    log = _access_log.copy()
    _access_log.clear()
    return log


# ── Recall queries ──────────────────────────────────────

def recall(
    graph: MemoryGraph,
    query_keywords: Optional[List[str]] = None,
    domain: Optional[str] = None,
    node_type: Optional[str] = None,
    max_depth: int = 3,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Recall relevant memories from the graph.

    Args:
        graph: The MemoryGraph instance.
        query_keywords: Optional keywords to filter by label/content.
        domain: Optional domain filter (emotion/knowledge/task).
        node_type: Optional node type filter.
        max_depth: BFS/Walk depth.
        limit: Maximum results.

    Returns:
        List of vertex property dicts (status=live only).
    """
    # Collect candidate vertices
    candidates: List[Dict[str, Any]] = []

    # If keywords provided, walk from keyword-matched nodes
    if query_keywords:
        all_labels = graph.get_vertex_labels(limit=500)
        for nid, label in all_labels.items():
            label_lower = label.lower()
            for kw in query_keywords:
                if kw.lower() in label_lower:
                    # Walk from this node
                    visited = graph.walk(nid, max_depth)
                    for vid in visited:
                        v = graph.get_vertex(vid)
                        if v and v.get("status") == "live":
                            # Filter by domain/type if specified
                            if domain and v.get("domain") != domain:
                                continue
                            if node_type and v.get("type") != node_type:
                                continue
                            candidates.append(v)
                    break

    # If no keywords or no matches, get broader list
    if not candidates:
        candidates = graph.list_vertices(
            domain=domain,
            node_type=node_type,
            status="live",
            limit=limit * 2,
        )

    # Deduplicate
    seen = set()
    unique = []
    for v in candidates:
        vid = v.get("id", "")
        if vid not in seen:
            seen.add(vid)
            unique.append(v)
            if len(unique) >= limit:
                break

    # Log access
    for v in unique:
        log_access(v.get("id", ""))

    return unique


def recall_by_feedback(
    graph: MemoryGraph,
    domain: str = "emotion",
    min_valence: Optional[float] = None,
    max_valence: Optional[float] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Recall feedback-related memories (praise/criticism).
    Used when lobster wants to learn from past feedback.
    """
    # Get all live vertices, then filter for those connected to feedback edges
    all_vertices = graph.list_vertices(status="live", domain=domain, limit=500)

    results = []
    for v in all_vertices:
        # Check if this vertex has feedback edges
        nid = v.get("id", "")
        props = v
        valence_val = props.get("valence", 0.0)

        if min_valence is not None and valence_val < min_valence:
            continue
        if max_valence is not None and valence_val > max_valence:
            continue

        results.append(props)

    # Log access
    for r in results[:limit]:
        log_access(r.get("id", ""))

    return results[:limit]


# ── System prompt snippet (§4.1) ────────────────────────

MEMORY_SYSTEM_NOTICE = """
[记忆系统]
你拥有一个长期图记忆系统（lobster-memory）。它记录了：
- 我们讨论过的知识/技术话题
- 你的行为被表扬或批评的经历
- 正在进行的工作任务

这不是自动注入的内容，而是一个你可以**主动查询**的数据库。
当你需要回忆相关事实、上下文、或过去的反馈时，主动使用你的记忆查询能力。
"""


def build_memory_context(stats: Dict[str, Any]) -> str:
    """Build the initial context to inject at session start."""
    lines = [MEMORY_SYSTEM_NOTICE.strip(), ""]
    lines.append(f"[当前记忆概况]")
    lines.append(f"  总节点: {stats['total_vertices']} (活跃)")
    lines.append(f"  总边数: {stats['total_edges']}")
    lines.append(f"  按域: 情绪 {stats['by_domain'].get('emotion',0)} | "
                 f"知识 {stats['by_domain'].get('knowledge',0)} | "
                 f"任务 {stats['by_domain'].get('task',0)}")
    lines.append(f"  容量: {stats['caps']['vertex_pct']}%")
    return "\n".join(lines)
