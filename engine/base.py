"""Base statistics and session context builder (§2.6)."""

from typing import Dict, Any

from .memory_graph import MemoryGraph
from .recall import build_memory_context, drain_access_log
from .consolidator import check_capacity, consolidate


class LobsterMemory:
    """
    Top-level API for lobster-memory skill integration.
    The lobster agent interacts with this class.
    """

    def __init__(self, data_path: str = "memory.axeb"):
        self._graph = MemoryGraph(data_path)

    @property
    def graph(self) -> MemoryGraph:
        return self._graph

    # ── Session start ───────────────────────────────────

    def get_context(self) -> str:
        """Get the memory context to inject at session start."""
        stats = self._graph.get_stats()
        return build_memory_context(stats)

    # ── After each turn ─────────────────────────────────

    def remember_turn(self, turn_text: str, extraction_result: str) -> Dict[str, Any]:
        """
        Process one turn's extraction result.
        Call after the lobster runs extraction LLM.

        Args:
            turn_text: The conversation turn text (for logging).
            extraction_result: The raw LLM extraction output (JSON string).

        Returns:
            Summary dict: {"nodes_added": N, "edges_added": M, "error": Optional[str]}.
        """
        from .extractor import process_extraction_result

        existing_labels = self._graph.get_vertex_labels(limit=200)
        extracted, err = process_extraction_result(extraction_result, existing_labels)

        if err:
            return {"nodes_added": 0, "edges_added": 0, "error": err}

        nodes_added = 0
        edges_added = 0

        for node in extracted.get("nodes", []):
            try:
                self._graph.remember_fact(
                    id_str=node["id"],
                    label=node["label"],
                    domain=node.get("domain", "knowledge"),
                    node_type=node.get("type", "concept"),
                    weight=node.get("weight", 1.0),
                    content=node.get("content"),
                )
                nodes_added += 1
            except Exception as e:
                logger.error(f"remember_fact failed: {e}")

        for edge in extracted.get("edges", []):
            try:
                kind = edge["kind"]
                if kind == "feedback":
                    self._graph.remember_feedback(
                        id_str=f"{edge['from']}_{edge['to']}_fb",
                        from_id=edge["from"],
                        to_id=edge["to"],
                        category=edge.get("feedback_category", "behavior"),
                        valence=edge.get("valence", 0.0),
                        weight=edge.get("weight", 1.0),
                        domain=edge.get("domain", "emotion"),
                        content=edge.get("content"),
                    )
                else:
                    self._graph.remember_relation(
                        id_str=f"{edge['from']}_{edge['to']}_{kind}",
                        from_id=edge["from"],
                        to_id=edge["to"],
                        kind=kind,
                        weight=edge.get("weight", 1.0),
                        domain=edge.get("domain", "knowledge"),
                    )
                edges_added += 1
            except Exception as e:
                logger.error(f"remember_relation/feedback failed: {e}")

        return {"nodes_added": nodes_added, "edges_added": edges_added, "error": None}

    # ── Consolidation trigger ───────────────────────────

    def consolidate_if_needed(self, round_count: int, k: int = 30) -> Dict[str, Any]:
        """
        Run consolidation if conditions are met.
        Returns consolidation report or {"skipped": True}.
        """
        cap = check_capacity(self._graph)
        if cap == "hard":
            access_log = drain_access_log()
            return consolidate(self._graph, access_log, panic_mode=True)

        if cap == "soft" or round_count % k == 0:
            access_log = drain_access_log()
            return consolidate(self._graph, access_log, panic_mode=False)

        return {"skipped": True, "round": round_count}

    # ── Persistence ─────────────────────────────────────

    def save(self):
        self._graph.save()

    def close(self):
        self._graph.close()

    def __repr__(self) -> str:
        return f"<LobsterMemory {self._graph}>"


# Module-level logger
import logging
logger = logging.getLogger("lobster_memory")
