"""Consolidation engine — 6-step pipeline (§5.5)."""

import logging
from typing import Any, Dict, List, Optional, Tuple

from .memory_graph import MemoryGraph
from .schema import (
    COMMUNITY_MERGE_THRESHOLD,
    MEMORY_CAPS,
    SCORE_KEEP_HIGH,
    SCORE_KEEP_LOW,
    SCORE_WEIGHTS,
    VALID_DOMAINS,
    ts_days_ago,
    ts_now,
)

logger = logging.getLogger("lobster_memory.consolidator")


# ── Normalization (§5.3) ────────────────────────────────

def _minmax(values: List[float]) -> List[float]:
    """Min-max normalize to [0, 1]. Returns 0.5 for all if no variance."""
    if not values:
        return []
    vmin, vmax = min(values), max(values)
    if vmax == vmin:
        return [0.5] * len(values)
    return [(v - vmin) / (vmax - vmin) for v in values]


def _normalize_signals(
    vertices: List[Dict[str, Any]],
    pagerank_scores: Dict[str, float],
) -> Dict[str, Dict[str, float]]:
    """
    Compute normalized signal vectors for each live vertex.
    Returns {id_str: {"valence": norm, "frequency": norm, ...}}.
    """
    n = len(vertices)
    if n == 0:
        return {}

    # Extract raw signals
    ids = []
    raw_valence = []
    raw_freq = []
    raw_recency = []
    raw_access = []
    raw_centrality = []

    for v in vertices:
        vid = v.get("id", "")
        ids.append(vid)
        raw_valence.append(abs(v.get("valence", 0.0)))  # use absolute for scoring
        raw_freq.append(v.get("weight", 1.0))
        raw_recency.append(ts_days_ago(v.get("last_accessed") or v.get("updated_at")))
        raw_access.append(v.get("access_count", 0))
        raw_centrality.append(pagerank_scores.get(vid, 0.0))

    # Normalize
    n_valence = _minmax(raw_valence)
    n_freq = _minmax(raw_freq)
    n_recency = _minmax([1.0 - min(d / 365.0, 1.0) for d in raw_recency])  # invert: newer=higher
    n_access = _minmax(raw_access)
    n_centrality = _minmax(raw_centrality)

    result = {}
    for i, vid in enumerate(ids):
        result[vid] = {
            "valence": n_valence[i],
            "frequency": n_freq[i],
            "recency": n_recency[i],
            "access": n_access[i],
            "centrality": n_centrality[i],
        }
    return result


def _score_vertex(signals: Dict[str, float], weights: Dict[str, float] = SCORE_WEIGHTS) -> float:
    """Compute weighted score from normalized signals."""
    score = 0.0
    for key, w in weights.items():
        score += w * signals.get(key, 0.0)
    return score


# ── Community detection (simple label propagation, §5.4) ─

def _detect_communities(
    graph: MemoryGraph,
    vertices: List[Dict[str, Any]],
) -> List[List[str]]:
    """
    Simple connected-component detection on the live subgraph.
    Returns list of communities, each community = [id_str, ...].
    """
    if len(vertices) <= 1:
        return []

    # Build adjacency from BFS
    adj: Dict[str, set] = {v["id"]: set() for v in vertices}
    for v in vertices[:50]:  # sample to limit cost
        vid = v["id"]
        try:
            neighbors = graph.walk(vid, 1)
            for nbr in neighbors:
                if nbr in adj:
                    adj[vid].add(nbr)
        except Exception:
            pass

    # Connected components
    visited: set = set()
    communities = []

    for v in vertices:
        vid = v["id"]
        if vid not in visited and vid in adj:
            # BFS
            comp = []
            queue = [vid]
            while queue:
                node = queue.pop(0)
                if node in visited:
                    continue
                visited.add(node)
                comp.append(node)
                for nbr in adj.get(node, set()):
                    if nbr not in visited:
                        queue.append(nbr)
            if len(comp) >= 3:  # min community size
                communities.append(comp)

    return communities


# ── Main consolidation pipeline (§5.5) ──────────────────

def consolidate(
    graph: MemoryGraph,
    access_log: List[Tuple[str, float]],
    panic_mode: bool = False,
) -> Dict[str, Any]:
    """
    Full consolidation pipeline.
    1. flush access_log → axolotl
    2. score live vertices (normalized signals)
    3. apply three-tier actions (keep/consolidate/trash)
    4. detect communities
    5. merge eligible communities
    6. generate report

    Args:
        graph: MemoryGraph instance.
        access_log: Drained access log from recall module.
        panic_mode: If True, use harder thresholds (hard cap triggered).

    Returns:
        Consolidation report dict.
    """
    report = {
        "before": {"vertices": graph.vertex_count, "edges": graph.edge_count},
        "trashed": 0,
        "merged": {"communities_found": 0, "merged": 0},
        "cap_level": "hard" if panic_mode else "normal",
        "top_kept": [],
        "top_trashed": [],
    }

    # Step 1: flush access_log
    if access_log:
        flushed = graph.flush_access_log(access_log)
        logger.info(f"consolidation: flushed {flushed} access log entries")

    # Step 2: get live vertices + PageRank
    vertices = graph.list_vertices(status="live", limit=100000)
    if not vertices:
        logger.info("consolidation: no live vertices to score")
        report["after"] = {"vertices": graph.vertex_count, "edges": graph.edge_count}
        return report

    pagerank_scores = graph.pagerank(iterations=30)

    # Step 3: normalize and score
    signals = _normalize_signals(vertices, pagerank_scores)

    # Determine thresholds
    if panic_mode:
        keep_high = 0.7
        keep_low = MEMORY_CAPS["panic_threshold_ratio"]  # 0.5
    else:
        keep_high = SCORE_KEEP_HIGH
        keep_low = SCORE_KEEP_LOW

    scored = []
    for v in vertices:
        vid = v["id"]
        sig = signals.get(vid, {})
        score = _score_vertex(sig)
        scored.append((vid, score, v))

    scored.sort(key=lambda x: x[1], reverse=True)

    # Apply actions
    for vid, score, _v in scored:
        if score >= keep_high:
            pass  # kept, potential community candidate
        elif score >= keep_low:
            pass  # kept, no action
        else:
            graph.set_status(vid, "trashed")
            report["trashed"] += 1

    # Top/bottom for report
    report["top_kept"] = [
        {"id": vid, "score": round(s, 4)}
        for vid, s, _ in scored[:5] if s >= keep_low
    ]
    report["top_trashed"] = [
        {"id": vid, "score": round(s, 4)}
        for vid, s, _ in scored[-5:] if s < keep_low
    ]

    # Step 4: detect communities (among kept vertices)
    kept_vertices = [v for vid, s, v in scored if s >= keep_high]
    communities = _detect_communities(graph, kept_vertices)
    report["merged"]["communities_found"] = len(communities)

    # Step 5: merge eligible communities
    for comp in communities:
        # Compute community density score
        comp_scores = []
        for vid in comp:
            sig = signals.get(vid, {})
            comp_scores.append(_score_vertex(sig))

        avg_score = sum(comp_scores) / len(comp_scores) if comp_scores else 0.0
        density = min(len(comp) / 10.0, 1.0)
        community_score = density * avg_score

        if community_score >= COMMUNITY_MERGE_THRESHOLD:
            # Merge: create summary node, trash members
            member_labels = []
            for vid in comp:
                v = graph.get_vertex(vid)
                if v:
                    member_labels.append(v.get("label", vid))

            summary_id = f"community_{comp[0][:8]}"
            summary_content = f"群落摘要({len(comp)}个节点): {'; '.join(member_labels[:5])}..."

            graph.remember_community(summary_id, summary_content, comp)
            for vid in comp:
                graph.set_status(vid, "trashed")

            report["merged"]["merged"] += 1
            logger.info(f"merged community: {summary_id} ({len(comp)} members)")

    # Step 5.5 (panic): force-trash if still over hard cap
    if panic_mode:
        remaining = graph.list_vertices(status="live", limit=100000)
        hard_vertex = MEMORY_CAPS["hard_vertex"]
        if len(remaining) > hard_vertex:
            # Force-trash lowest-scored vertices until under hard cap
            leftover = sorted(
                [(vid, s) for vid, s, _ in scored if s < keep_low and graph.get_vertex(vid)],
                key=lambda x: x[1],
            )
            excess = len(remaining) - int(hard_vertex * 0.8)
            for vid, _ in leftover[:excess]:
                graph.set_status(vid, "trashed")
                report["trashed"] += 1
            logger.warning(f"panic mode: force-trashed {min(excess, len(leftover))} vertices")

    # Save
    graph.save()

    report["after"] = {"vertices": graph.vertex_count, "edges": graph.edge_count}
    return report


# ── Capacity check (§5.6) ────────────────────────────────

def check_capacity(graph: MemoryGraph) -> Optional[str]:
    """
    Check if graph exceeds capacity thresholds.
    Returns None if OK, or "soft"/"hard" if a cap was triggered.
    """
    live_vertices = len(graph.list_vertices(status="live", limit=50000))
    edge_count = graph.edge_count

    if live_vertices > MEMORY_CAPS["hard_vertex"] or edge_count > MEMORY_CAPS["hard_edge"]:
        logger.warning(f"HARD cap triggered: v={live_vertices} e={edge_count}")
        return "hard"
    if live_vertices > MEMORY_CAPS["soft_vertex"] or edge_count > MEMORY_CAPS["soft_edge"]:
        logger.info(f"SOFT cap triggered: v={live_vertices} e={edge_count}")
        return "soft"
    return None
