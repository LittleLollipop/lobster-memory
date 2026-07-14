"""Integration layer — lobster agent's touchpoints with the memory system.

Usage in the lobster agent's conversation loop::

    from lobster_memory.integration import MemorySession

    session = MemorySession("memory.axeb", consolidate_every=30)

    # 1. Session start — inject context into system prompt
    system_prompt_extension = session.start()

    round_count = 0
    for user_msg, assistant_reply in conversation:
        round_count += 1

        # 2. After each turn — build extraction prompt for LLM
        prompt = session.build_extraction_prompt(user_msg, assistant_reply,
                                                  round_count)

        # 3. Lobster sends prompt to its own LLM, gets raw JSON back
        extraction_result = lobster_call_llm(prompt)

        # 4. Process the extraction result
        session.after_turn(extraction_result)

        # 5. Periodically consolidate
        if session.should_consolidate(round_count):
            report = session.consolidate()
            # Optionally tell the user what changed:
            # print(f"[记忆巩固完成: 修剪{report['trashed']}条, 合并{report['merged']['merged']}个群落]")

    session.close()
"""

import logging
from typing import Any, Dict, Optional, Tuple

from .base import LobsterMemory
from .extractor import build_extraction_prompt, process_extraction_result
from .consolidator import check_capacity, consolidate
from .recall import (
    build_memory_context,
    drain_access_log,
    log_access,
    MEMORY_SYSTEM_NOTICE,
    recall,
    recall_by_feedback,
)
from .schema import ts_now

logger = logging.getLogger("lobster_memory.integration")


class MemorySession:
    """
    Session-level orchestrator for lobster-memory.

    Manages the lifecycle: inject context → extract per-turn → consolidate periodically.
    """

    def __init__(
        self,
        data_path: str = "memory.axeb",
        consolidate_every: int = 30,
        max_node_labels: int = 200,
    ):
        self._lm = LobsterMemory(data_path)
        self._consolidate_every = consolidate_every
        self._max_node_labels = max_node_labels
        self._round_count = 0
        self._last_consolidation_report: Optional[Dict[str, Any]] = None

    # ── Session start ───────────────────────────────────

    def start(self) -> str:
        """
        Call at session start. Returns a string to inject into the
        lobster's system prompt.

        Includes:
        - Functional notice about the memory system
        - Current memory statistics (vertex/edge counts, by domain)
        - Any recent consolidation highlights
        """
        parts = []

        # Statistics
        stats = self._lm._graph.get_stats()
        parts.append(MEMORY_SYSTEM_NOTICE.strip())
        parts.append("")
        parts.append("[当前记忆]")
        parts.append(
            f"  总节点 {stats['total_vertices']} | "
            f"总边 {stats['total_edges']}"
        )
        # Active nodes — the real "should I consolidate?" gauge
        if stats["active_7d"]:
            active = stats["active_7d"]
            parts.append(
                f"  近7天活跃: 情绪 {active.get('emotion', 0)} | "
                f"知识 {active.get('knowledge', 0)} | "
                f"任务 {active.get('task', 0)}"
            )
        parts.append(
            f"  按域: 情绪 {stats['by_domain'].get('emotion', 0)} | "
            f"知识 {stats['by_domain'].get('knowledge', 0)} | "
            f"任务 {stats['by_domain'].get('task', 0)}"
        )
        # Safety valve — not physical capacity
        parts.append(
            f"  软策略线 {stats['total_vertices']}/15000 "
            f"({stats['caps']['vertex_pct']}%)  — 安全阀，非物理容量"
        )

        # Recent consolidation highlights
        if self._last_consolidation_report:
            r = self._last_consolidation_report
            merged = r.get("merged", {})
            if merged.get("merged", 0) > 0:
                parts.append(f"  [上次巩固] 合并了 {merged['merged']} 个记忆群落, "
                             f"修剪 {r.get('trashed', 0)} 条")

        parts.append("")
        parts.append("当你需要回忆时，主动查询你的记忆系统。")

        return "\n".join(parts)

    # ── Per-turn extraction ─────────────────────────────

    def build_extraction_prompt(
        self,
        user_msg: str,
        assistant_reply: str,
        round_number: int = 0,
    ) -> str:
        """
        Build the extraction prompt for the current turn.
        Call this AFTER the assistant has replied to the user.

        Args:
            user_msg: The user's message in this turn.
            assistant_reply: The assistant's reply in this turn.
            round_number: Current conversation round (for logging).

        Returns:
            A string prompt to send to the lobster's LLM for extraction.
        """
        self._round_count = round_number

        # Build turn text
        turn_text = f"用户: {user_msg}\n助手: {assistant_reply}"

        # Get existing node labels for dedup
        existing_labels = self._lm._graph.get_vertex_labels(limit=self._max_node_labels)

        # Build and return the extraction prompt
        return build_extraction_prompt(turn_text, existing_labels)

    def after_turn(self, extraction_response: str) -> Dict[str, Any]:
        """
        Process the LLM's extraction response.
        Call this after the lobster's LLM returns the extraction JSON.

        Args:
            extraction_response: Raw LLM output (should contain JSON).

        Returns:
            Summary dict: {"nodes_added": N, "edges_added": M, "error": Optional[str]}.
        """
        turn_text = f"round_{self._round_count}"
        return self._lm.remember_turn(turn_text, extraction_response)

    # ── On-demand recall ────────────────────────────────

    def recall(
        self,
        keywords: Optional[list] = None,
        domain: Optional[str] = None,
        limit: int = 20,
    ) -> list:
        """
        Recall relevant memories on demand.
        Call this when the lobster decides to actively query its memory.

        Args:
            keywords: Optional keywords to search for.
            domain: Optional domain filter.
            limit: Maximum results.

        Returns:
            List of vertex property dicts.
        """
        return recall(
            self._lm._graph,
            query_keywords=keywords,
            domain=domain,
            limit=limit,
        )

    def recall_feedback(
        self,
        valence: Optional[str] = None,
        limit: int = 20,
    ) -> list:
        """
        Recall past feedback (praise/criticism).
        Useful when lobster wants to learn from past interactions.

        Args:
            valence: "positive" / "negative" / None (all).
            limit: Maximum results.

        Returns:
            List of vertex property dicts.
        """
        min_v, max_v = None, None
        if valence == "positive":
            min_v = 0.3
        elif valence == "negative":
            max_v = -0.3

        return recall_by_feedback(
            self._lm._graph,
            domain="emotion",
            min_valence=min_v,
            max_valence=max_v,
            limit=limit,
        )

    # ── Consolidation scheduling ────────────────────────

    def should_consolidate(self, round_count: Optional[int] = None) -> bool:
        """
        Check if consolidation should run now.
        Checks: capacity thresholds + periodic schedule.

        Args:
            round_count: Current round number (uses internal counter if None).

        Returns:
            True if consolidation is due.
        """
        if round_count is not None:
            self._round_count = round_count

        # Check capacity (trumps schedule)
        cap = check_capacity(self._lm._graph)
        if cap is not None:
            return True

        # Check periodic schedule
        if self._round_count > 0 and self._round_count % self._consolidate_every == 0:
            return True

        return False

    def consolidate(self) -> Dict[str, Any]:
        """
        Run consolidation (6-step pipeline).
        Call this when should_consolidate() returns True.

        Returns:
            Consolidation report dict.
        """
        access_log = drain_access_log()
        cap = check_capacity(self._lm._graph)
        panic = cap == "hard"

        report = consolidate(self._lm._graph, access_log, panic_mode=panic)

        # Save report for next session's highlights
        self._last_consolidation_report = report

        # Log summary
        logger.info(
            f"consolidation done: v={report['before']['vertices']}→{report['after']['vertices']}, "
            f"trashed={report['trashed']}, merged={report['merged']['merged']}, "
            f"level={report['cap_level']}"
        )

        return report

    def consolidate_summary(self) -> Optional[str]:
        """Return a human-readable summary of the last consolidation (for telling the user)."""
        if not self._last_consolidation_report:
            return None

        r = self._last_consolidation_report
        parts = [f"[记忆巩固完成]"]
        parts.append(f"  节点: {r['before']['vertices']} → {r['after']['vertices']}")
        parts.append(f"  边数: {r['before']['edges']} → {r['after']['edges']}")

        if r['trashed'] > 0:
            parts.append(f"  软删除: {r['trashed']} 条低质记忆")

        merged = r.get('merged', {})
        if merged.get('merged', 0) > 0:
            parts.append(f"  合并群落: {merged['merged']} 个")

        if r.get('cap_level') == 'hard':
            parts.append(f"  ⚠️ 容量硬上限触发,已激进修剪")

        return "\n".join(parts)

    # ── Access logging ──────────────────────────────────

    def log_access(self, id_str: str):
        """Record that a vertex was recalled and deemed useful."""
        log_access(id_str)

    # ── Session end ─────────────────────────────────────

    def close(self):
        """Clean up: drain access log, save, close graph."""
        # Final flush of any remaining access log entries
        remaining = drain_access_log()
        if remaining:
            self._lm._graph.flush_access_log(remaining)

        self._lm.save()
        self._lm.close()

    @property
    def graph(self):
        """Direct access to MemoryGraph for advanced queries."""
        return self._lm._graph

    def __repr__(self) -> str:
        return (
            f"<MemorySession v={self._lm._graph.vertex_count} "
            f"e={self._lm._graph.edge_count} "
            f"round={self._round_count}>"
        )
