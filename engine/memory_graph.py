"""MemoryGraph — thin wrapper over axolotl_rs.AxolotlGraph."""

import fcntl
import logging
from typing import Any, Dict, List, Optional, Tuple

import axolotl_rs  # type: ignore

from .schema import (
    SCORE_WEIGHTS,
    VALID_DOMAINS,
    default_edge_props,
    default_node_props,
    dict_from_props,
    props_to_dict,
    str_to_id,
    ts_now,
)

logger = logging.getLogger("lobster_memory.memory_graph")


class MemoryGraph:
    """Manages the lobster's long-term graph memory via axolotl."""

    def __init__(self, data_path: str = "memory.axeb"):
        self._path = data_path
        # 文件锁：runner 每次调用都是独立进程，若同时跑 recall + remember
        # 并发 open/save 同一 memory.axeb，写者的 save() 会稳定抛
        # RuntimeError: Io("No such file or directory")（axolotl 非并发安全）。
        # 用 fcntl 排他锁把同文件的并发访问串行化，避免撞崩。
        self._lock_path = data_path + ".lock"
        self._lock_fd = open(self._lock_path, "w")
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX)  # 阻塞直到独占
        self._g = axolotl_rs.AxolotlGraph.open(data_path)
        self._ensure_root()

    def _ensure_root(self):
        """Ensure a root anchor vertex exists for graph traversal."""
        root_nid = str_to_id("lobster_root")
        if self._g.get_vertex(root_nid) is None:
            self._g.add_vertex(root_nid, {
                "id": "lobster_root",
                "label": "龙虾记忆根节点",
                "domain": "knowledge",
                "type": "concept",
                "status": "live",
                "weight": 0.0,
                "access_count": 0,
                "last_accessed": None,
                "created_at": ts_now(),
                "updated_at": ts_now(),
            })

    # ── Properties ──────────────────────────────────────

    @property
    def vertex_count(self) -> int:
        return self._g.vertex_count()

    @property
    def edge_count(self) -> int:
        return self._g.edge_count()

    # ── Write: Facts ────────────────────────────────────

    def remember_fact(
        self,
        id_str: str,
        label: str,
        domain: str,
        node_type: str,
        weight: float = 1.0,
        content: Optional[str] = None,
        source: Optional[str] = None,
        connect_to_root: bool = True,
    ) -> int:
        """Add or update a fact node. Returns the numeric id."""
        nid = str_to_id(id_str)
        props = default_node_props(id_str, label, domain, node_type, weight, content, source)

        is_new = self._g.get_vertex(nid) is None
        existing = None if is_new else self._g.get_vertex(nid)

        if existing:
            existing_dict = dict_from_props(dict(existing))
            existing_dict["weight"] = existing_dict.get("weight", 1.0) + weight
            existing_dict["updated_at"] = ts_now()
            if content:
                existing_dict["content"] = content
            props = existing_dict

        self._g.add_vertex(nid, props_to_dict(props))

        # Auto-connect new vertices to root for walk-based enumeration
        if is_new and connect_to_root and id_str != "lobster_root":
            root_nid = str_to_id("lobster_root")
            if self._g.get_edge(root_nid, nid) is None:
                self._g.add_edge(root_nid, nid, 0.01, {"kind": "has_member", "status": "live"})

        return nid

    # ── Write: Relations ────────────────────────────────

    def remember_relation(
        self,
        id_str: str,
        from_id: str,
        to_id: str,
        kind: str,
        weight: float = 1.0,
        domain: str = "knowledge",
    ) -> Tuple[int, int]:
        """Add or update a relation edge. Returns (from_nid, to_nid)."""
        from_nid = str_to_id(from_id)
        to_nid = str_to_id(to_id)

        # Ensure both vertices exist (create placeholders if needed)
        if self._g.get_vertex(from_nid) is None:
            self._g.add_vertex(from_nid, {"id": from_id, "label": from_id, "status": "live"})
        if self._g.get_vertex(to_nid) is None:
            self._g.add_vertex(to_nid, {"id": to_id, "label": to_id, "status": "live"})

        props = default_edge_props(id_str, from_id, to_id, kind, weight, domain)

        existing = self._g.get_edge(from_nid, to_nid)
        if existing:
            _w, existing_props = existing
            existing_dict = dict_from_props(dict(existing_props))
            existing_dict["weight"] = existing_dict.get("weight", 1.0) + weight
            existing_dict["updated_at"] = ts_now()
            props = existing_dict

        self._g.add_edge(from_nid, to_nid, weight, props_to_dict(props))
        return from_nid, to_nid

    # ── Write: Feedback ─────────────────────────────────

    def remember_feedback(
        self,
        id_str: str,
        from_id: str,
        to_id: str,
        category: str,
        valence: float,
        weight: float = 1.0,
        domain: str = "emotion",
        content: Optional[str] = None,
    ) -> Tuple[int, int]:
        """Record a piece of feedback (praise/criticism)."""
        from_nid = str_to_id(from_id)
        to_nid = str_to_id(to_id)

        if self._g.get_vertex(from_nid) is None:
            self._g.add_vertex(from_nid, {"id": from_id, "label": from_id, "status": "live"})
        if self._g.get_vertex(to_nid) is None:
            self._g.add_vertex(to_nid, {"id": to_id, "label": to_id, "status": "live"})

        props = default_edge_props(
            id_str, from_id, to_id, "feedback",
            weight=weight, domain=domain,
            feedback_category=category, valence=valence,
        )

        existing = self._g.get_edge(from_nid, to_nid)
        if existing:
            _w, existing_props = existing
            existing_dict = dict_from_props(dict(existing_props))
            existing_dict["weight"] = existing_dict.get("weight", 1.0) + weight
            existing_dict["valence"] = (existing_dict.get("valence", 0.0) + valence) / 2.0
            existing_dict["updated_at"] = ts_now()
            props = existing_dict

        self._g.add_edge(from_nid, to_nid, weight, props_to_dict(props))

        # ── Also create an emotion feedback-event NODE so it is recallable ──
        # axolotl has no edge-enumeration API, so feedback is stored both as
        # an edge (graph structure) AND as an emotion-domain node (recallable
        # via recall_by_feedback, which filters emotion vertices by valence).
        fb_node_id = f"fb_event_{from_id}_{to_id}"
        nid = self.remember_fact(
            id_str=fb_node_id,
            label=f"{category}反馈",
            domain="emotion",
            node_type="emotion",
            weight=weight,
            content=content,
        )
        raw = self._g.get_vertex(nid)
        if raw is not None:
            np = dict_from_props(dict(raw))
            np["valence"] = valence
            np["feedback_category"] = category
            np["from_id"] = from_id
            np["to_id"] = to_id
            np["from"] = from_id
            np["to"] = to_id
            self._g.add_vertex(nid, props_to_dict(np))

        return from_nid, to_nid

    # ── Write: Community summary ────────────────────────

    def remember_community(
        self,
        id_str: str,
        content: str,
        members: List[str],
        domain: str = "mixed",
    ) -> int:
        """Create a community summary super-node."""
        nid = str_to_id(id_str)
        props = {
            "id": id_str,
            "label": content[:80],
            "domain": domain,
            "type": "community_summary",
            "content": content,
            "members": members,  # serialized as JSON string by props_to_dict
            "created_at": ts_now(),
            "updated_at": ts_now(),
            "status": "live",
            "weight": len(members),
            "access_count": 0,
            "last_accessed": None,
            "source": None,
        }
        self._g.add_vertex(nid, props_to_dict(props))
        return nid

    # ── Write: vertex upsert & edge ─────────────────────

    def upsert_vertex(self, props: Dict[str, Any]) -> int:
        """Upsert a vertex from a full props dict (preserves caller-set fields)."""
        nid = str_to_id(props["id"])
        props = dict(props)
        props["updated_at"] = ts_now()
        self._g.add_vertex(nid, props_to_dict(props))
        return nid

    def add_edge(
        self,
        from_id: str,
        to_id: str,
        kind: str,
        weight: float = 1.0,
        domain: str = "knowledge",
        valence: float = 0.0,
    ) -> Optional[Tuple[int, int]]:
        """Add an edge if both vertices exist and the edge is new. Returns (from,to) or None."""
        from_nid = str_to_id(from_id)
        to_nid = str_to_id(to_id)
        if self._g.get_vertex(from_nid) is None or self._g.get_vertex(to_nid) is None:
            return None
        if self._g.get_edge(from_nid, to_nid) is not None:
            return None
        self._g.add_edge(from_nid, to_nid, weight, {
            "kind": kind,
            "domain": domain,
            "valence": valence,
            "status": "live",
            "weight": weight,
            "created_at": ts_now(),
            "updated_at": ts_now(),
        })
        return (from_nid, to_nid)

    # ── Read: Vertex ────────────────────────────────────

    def get_vertex(self, id_str: str) -> Optional[Dict[str, Any]]:
        """Get vertex properties by string id."""
        nid = str_to_id(id_str)
        raw = self._g.get_vertex(nid)
        if raw is None:
            return None
        return dict_from_props(dict(raw))

    def list_vertices(
        self,
        domain: Optional[str] = None,
        node_type: Optional[str] = None,
        status: str = "live",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Enumerate live vertices, optionally filtered."""
        results = []
        # axolotl doesn't have a list-all API, so we use walk from known seeds
        # or iterate through a known index. For now, we walk from a set of
        # known root vertices.
        # In practice, lobster-memory maintains a root node "lobster_root"
        root_nid = str_to_id("lobster_root")
        visited = self._g.walk(root_nid, 10)
        for vid in visited:
            if len(results) >= limit:
                break
            raw = self._g.get_vertex(vid)
            if raw is None:
                continue
            props = dict_from_props(dict(raw))
            if props.get("status") != status:
                continue
            if domain and props.get("domain") != domain:
                continue
            if node_type and props.get("type") != node_type:
                continue
            results.append(props)
        return results

    def get_vertex_labels(self, limit: int = 200) -> Dict[str, str]:
        """Return {id_str: label} for the most recent live vertices."""
        labels = {}
        vertices = self.list_vertices(status="live", limit=limit)
        for v in vertices:
            labels[v.get("id", "")] = v.get("label", "")
        return labels

    # ── Update: access counters ─────────────────────────

    def flush_access_log(self, access_log: List[Tuple[str, float]]) -> int:
        """
        Batch-write access counters from the in-memory access log.
        Called by consolidation step 1.
        Returns number of flushed entries.
        """
        count = 0
        now = ts_now()
        seen: set = set()

        for id_str, _timestamp in access_log:
            if id_str in seen:
                continue
            seen.add(id_str)
            nid = str_to_id(id_str)
            raw = self._g.get_vertex(nid)
            if raw is not None:
                props = dict_from_props(dict(raw))
                props["access_count"] = props.get("access_count", 0) + 1
                props["last_accessed"] = now
                self._g.add_vertex(nid, props_to_dict(props))
                count += 1
        return count

    # ── Update: status ──────────────────────────────────

    def set_status(self, id_str: str, status: str) -> bool:
        """Set vertex status (e.g. 'live' → 'trashed')."""
        nid = str_to_id(id_str)
        raw = self._g.get_vertex(nid)
        if raw is None:
            return False
        props = dict_from_props(dict(raw))
        props["status"] = status
        props["updated_at"] = ts_now()
        self._g.add_vertex(nid, props_to_dict(props))
        return True

    # ── Statistics (§2.6) ───────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """Compute the statistical base for session start injection."""
        vertices = self.list_vertices(status="live", limit=100000)
        trashed = self.list_vertices(status="trashed", limit=100000)

        by_domain = {d: 0 for d in VALID_DOMAINS}
        active_7d = {d: 0 for d in VALID_DOMAINS}
        now = ts_now()

        for v in vertices:
            d = v.get("domain", "knowledge")
            by_domain[d] = by_domain.get(d, 0) + 1
            # Rough active-7d check
            updated = v.get("updated_at")
            if updated:
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromisoformat(updated)
                    delta = datetime.now(timezone.utc) - dt
                    if delta.days <= 7:
                        active_7d[d] = active_7d.get(d, 0) + 1
                except (ValueError, TypeError):
                    pass

        # Capacity status
        cap_used_vertex = len(vertices) / 15000 * 100 if vertices else 0
        cap_used_edge = self.edge_count / 40000 * 100 if self.edge_count else 0

        return {
            "total_vertices": len(vertices),
            "total_edges": self.edge_count,
            "by_domain": {d: by_domain.get(d, 0) for d in VALID_DOMAINS},
            "by_status": {"live": len(vertices), "trashed": len(trashed)},
            "active_7d": {d: active_7d.get(d, 0) for d in VALID_DOMAINS},
            "caps": {"vertex_pct": round(cap_used_vertex, 1), "edge_pct": round(cap_used_edge, 1)},
            "generated_at": now,
        }

    # ── Graph algorithms ────────────────────────────────

    def pagerank(self, iterations: int = 50) -> Dict[str, float]:
        """Run PageRank and return {id_str: score}."""
        raw = self._g.pagerank(iterations, 0.85)
        result = {}
        # raw is dict[int→float], we need to map back
        for nid, score in raw.items():
            # attempt reverse lookup via vertex
            v = self._g.get_vertex(nid)
            if v is not None:
                props = dict_from_props(dict(v))
                result[props.get("id", str(nid))] = round(score, 6)
        return result

    def bfs(self, id_str: str) -> Dict[str, int]:
        """BFS from a vertex. Returns {id_str: distance}."""
        nid = str_to_id(id_str)
        raw = self._g.bfs(nid)
        result = {}
        for vid, dist in raw.items():
            v = self._g.get_vertex(vid)
            if v is not None:
                props = dict_from_props(dict(v))
                result[props.get("id", str(vid))] = dist
        return result

    def walk(self, id_str: str, max_depth: int) -> List[str]:
        """Walk from a vertex. Returns list of id_strs."""
        nid = str_to_id(id_str)
        visited = self._g.walk(nid, max_depth)
        result = []
        for vid in visited:
            v = self._g.get_vertex(vid)
            if v is not None:
                props = dict_from_props(dict(v))
                result.append(props.get("id", str(vid)))
        return result

    # ── Persistence ─────────────────────────────────────

    def save(self) -> str:
        return self._g.save()

    def close(self):
        try:
            self._g.close()
        finally:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            self._lock_fd.close()

    def __repr__(self) -> str:
        return f"<MemoryGraph v={self.vertex_count} e={self.edge_count} path={self._path}>"
