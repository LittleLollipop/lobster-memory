"""lobster-memory schema: constants, validation, dedup, capacity."""

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("lobster_memory.schema")

# ── Enums ───────────────────────────────────────────────

VALID_DOMAINS = {"emotion", "knowledge", "task"}

VALID_NODE_TYPES = {"person", "concept", "task", "fact", "event", "emotion"}

VALID_EDGE_KINDS = {"relates_to", "caused", "part_of", "feedback", "derived"}

VALID_FEEDBACK_CATEGORIES = {"behavior", "understanding", "idea", "action"}

# ── Capacity safety valves (§5.6) ──────────────────────

MEMORY_CAPS = {
    "soft_vertex": 15000,
    "soft_edge": 40000,
    "hard_vertex": 25000,
    "hard_edge": 60000,
    "panic_threshold_ratio": 0.5,  # 硬上限时 soft-delete 阈值由此替代
}

# ── Scoring weights (initial, tunable) ──────────────────

SCORE_WEIGHTS = {
    "valence": 0.30,
    "frequency": 0.20,
    "recency": 0.20,
    "access": 0.15,
    "centrality": 0.15,
}

SCORE_KEEP_HIGH = 0.6
SCORE_KEEP_LOW = 0.3
COMMUNITY_MERGE_THRESHOLD = 0.5

# ── ID utilities ────────────────────────────────────────

# axolotl uses u64 ids; we map string ids to u64 via hash
ID_SPACE = 2**63  # stay in positive i64 range


def str_to_id(s: str) -> int:
    """Convert a string id to a u64-compatible integer via SHA-256 truncation."""
    h = hashlib.sha256(s.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") % ID_SPACE


def ts_now() -> str:
    """ISO 8601 timestamp in UTC."""
    return datetime.now(timezone.utc).isoformat()


def ts_days_ago(ts: Optional[str]) -> float:
    """Days since the given ISO timestamp. Returns large number if missing."""
    if not ts:
        return 365.0
    try:
        dt = datetime.fromisoformat(ts)
        now = datetime.now(timezone.utc)
        return (now - dt).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 365.0


# ── Default node/edge props ─────────────────────────────

def default_node_props(
    id_str: str,
    label: str,
    domain: str,
    node_type: str,
    weight: float = 1.0,
    content: Optional[str] = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": id_str,
        "label": label,
        "domain": domain,
        "type": node_type,
        "content": content,
        "created_at": ts_now(),
        "updated_at": ts_now(),
        "status": "live",
        "weight": weight,
        "source": source,
        "access_count": 0,
        "last_accessed": None,
    }


def default_edge_props(
    id_str: str,
    from_id: str,
    to_id: str,
    kind: str,
    weight: float = 1.0,
    domain: str = "knowledge",
    feedback_category: Optional[str] = None,
    valence: float = 0.0,
) -> Dict[str, Any]:
    return {
        "id": id_str,
        "from": from_id,
        "to": to_id,
        "kind": kind,
        "feedback_category": feedback_category,
        "valence": valence,
        "weight": weight,
        "domain": domain,
        "created_at": ts_now(),
        "updated_at": ts_now(),
        "status": "live",
        "access_count": 0,
        "last_accessed": None,
    }


# ── Validation (§3.2 layer 2) ───────────────────────────

class ValidationError(Exception):
    """Raised when extraction output fails schema validation."""
    pass


def validate_extraction(raw: dict) -> dict:
    """Validate extracted nodes/edges. Raises ValidationError on failure."""
    if not isinstance(raw, dict):
        raise ValidationError("extraction output must be a dict")

    nodes = raw.get("nodes", [])
    edges = raw.get("edges", [])

    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValidationError("nodes/edges must be lists")

    for node in nodes:
        if not isinstance(node, dict):
            raise ValidationError(f"node must be dict: {node}")
        if "id" not in node or "label" not in node:
            raise ValidationError(f"node missing id/label: {node}")
        domain = node.get("domain")
        if domain not in VALID_DOMAINS:
            raise ValidationError(f"invalid domain '{domain}' in node {node.get('id')}")
        ntype = node.get("type")
        if ntype not in VALID_NODE_TYPES:
            raise ValidationError(f"invalid type '{ntype}' in node {node.get('id')}")
        node.setdefault("weight", 1.0)
        node.setdefault("content", None)

    for edge in edges:
        if not isinstance(edge, dict):
            raise ValidationError(f"edge must be dict: {edge}")
        if "from" not in edge or "to" not in edge:
            raise ValidationError(f"edge missing from/to: {edge}")
        kind = edge.get("kind")
        if kind not in VALID_EDGE_KINDS:
            raise ValidationError(f"invalid kind '{kind}' in edge {edge.get('from')}->{edge.get('to')}")
        if kind == "feedback":
            fc = edge.get("feedback_category")
            if fc not in VALID_FEEDBACK_CATEGORIES:
                raise ValidationError(f"invalid feedback_category '{fc}' in edge")
            v = edge.get("valence", 0)
            if not isinstance(v, (int, float)) or v < -1.0 or v > 1.0:
                raise ValidationError(f"invalid valence '{v}' in feedback edge")
        edge.setdefault("weight", 1.0)
        edge.setdefault("domain", "knowledge")
        edge.setdefault("feedback_category", None)
        edge.setdefault("valence", 0.0)

    return raw


# ── Deduplication (§3.2 layer 3) ────────────────────────

def deduplicate_extraction(
    extracted: dict,
    existing_labels: Dict[str, str],
) -> dict:
    """
    Normalize extracted nodes/edges against existing graph state.
    - existing_labels: {id_str: label, ...} from current live nodes.
    """
    nodes = extracted.get("nodes", [])
    edges = extracted.get("edges", [])

    # Build reverse index: label → id (lowercase for fuzzy matching)
    label_to_id: Dict[str, str] = {}
    for nid, lbl in existing_labels.items():
        label_to_id[lbl.lower()] = nid

    # ── Dedup nodes ──
    seen_in_batch: Dict[str, str] = {}  # lower_label → id
    deduped_nodes = []
    for node in nodes:
        nid = node["id"]
        label_lower = node["label"].lower().strip()

        # 1. Match against known labels (exact lowercase)
        if label_lower in label_to_id:
            node["id"] = label_to_id[label_lower]
            node["_merged"] = True
        # 2. Match against same-batch labels
        elif label_lower in seen_in_batch:
            node["id"] = seen_in_batch[label_lower]
            node["_merged"] = True
        else:
            seen_in_batch[label_lower] = nid

        deduped_nodes.append(node)

    # ── Update edge references ──
    # Valid ids = existing graph node ids + this-batch node ids.
    # An edge is kept if EITHER endpoint is an existing or newly-created node.
    # (Feedback/relation edges often connect to pre-existing nodes.)
    valid_node_ids = (
        set(existing_labels.keys())        # ids already in the graph
        | set(seen_in_batch.values())      # ids created within this batch
        | {n["id"] for n in deduped_nodes} # ids after remapping
    )

    deduped_edges = []
    for edge in edges:
        # Ensure from/to reference valid ids
        if edge["from"] in valid_node_ids or edge["to"] in valid_node_ids:
            deduped_edges.append(edge)

    return {"nodes": deduped_nodes, "edges": deduped_edges}


# ── JSON serialization helpers ──────────────────────────

def props_to_dict(props: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure all property values are JSON-serializable (axolotl supports int/float/str/bool/null)."""
    result = {}
    for k, v in props.items():
        if v is None:
            result[k] = None
        elif isinstance(v, (int, float, str, bool)):
            result[k] = v
        elif isinstance(v, (list, dict)):
            result[k] = json.dumps(v, ensure_ascii=False)  # serialize nested as JSON string
        else:
            result[k] = str(v)
    return result


def dict_from_props(props: Dict[str, Any]) -> Dict[str, Any]:
    """Reverse: deserialize JSON string values back to Python objects."""
    result = {}
    for k, v in props.items():
        if isinstance(v, str) and (v.startswith("[") or v.startswith("{")):
            try:
                result[k] = json.loads(v)
            except json.JSONDecodeError:
                result[k] = v
        else:
            result[k] = v
    return result
