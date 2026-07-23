"""Consolidation engine — merge-first, type-aware pruning pipeline (§5.5, revised 2026-07-23).

设计原则（用户 2026-07-23 拍板）:
1. **合并优先**: 先合并(归纳/去重), 再遗忘。遗忘是兜底, 不是主动作。
2. **连通分量 = 被动抽象层**: 结构上的连通簇只是"可能可合并归纳"的提示(如同一本小说下
   的人物/服饰节点), 不能单独作为合并依据。
3. **合并需主动语义确认**: 真正合并(破坏性 collapse)必须语义冗余(同域同型 + 内容/标签重叠),
   不能只因"挂在同一 hub 下"。
4. **类型化遗忘**: 不是所有类型都该被时间遗忘。project/knowledge 脉络(概念/人物/事实/任务)
   受保护, 永不因时间被删; 只有 transient 类型(emotion/event)按各自阈值遗忘。

流水线(consolidate):
  Step 1  flush access_log(应用态才做)
  Step 2  取 live 节点 + PageRank
  Step 3  5 信号归一化 + 评分(仅用于排序/选 canonical, 不再直接决定删留)
  Step 4  **合并优先**: 结构连通簇(被动抽象) ∩ 语义冗余 → collapse 去重
  Step 5  **类型化遗忘**: 按 (domain,type) 保留策略决定 trashed, 受 min_retain 保护
  Step 6  (panic) 硬上限仍超 → 按分数强制泄压
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .memory_graph import MemoryGraph
from .schema import (
    MEMORY_CAPS,
    SCORE_WEIGHTS,
    ts_days_ago,
)

logger = logging.getLogger("lobster_memory.consolidator")

# ── Merge thresholds ───────────────────────────────────

# 语义冗余阈值(内容/标签 token Jaccard)。合并(collapse)必须有主动语义确认:
# 同域同型 + (标签近似 或 语义 >= 此)。连通分量只作被动抽象提示, 不驱动合并。
COLLAPSE_THRESHOLD = 0.50

# ── Type-aware retention policy ────────────────────────
#
# 关键: 所有类型都可被遗忘(forgettable=True) —— 但"可被遗忘的阈值"按抽象度区分,
# 直接回应"不是不能遗忘这些类型, 而是它们可接受更长时间的遗忘阈值":
#   越抽象(概念/归纳摘要) → 分数地板极低 + 休眠窗口极长(极难被忘, 但最终仍会)
#   越具体(任务/事实)    → 窗口较短
#   瞬态(事件/情绪)      → 窗口最短、分数地板较高(易在休眠后被清)
# 维度:
#   keep_low        = 归一化分数地板(多低才算"不重要")
#   min_retain_days = 休眠窗口(多久没被访问/更新才允许被时间遗忘)
TYPE_RETENTION: Dict[str, Dict[str, Any]] = {
    # 抽象层: 最耐久
    "concept":           {"forgettable": True, "keep_low": 0.05, "min_retain_days": 365},  # 抽象概念
    "community_summary": {"forgettable": True, "keep_low": 0.05, "min_retain_days": 365},  # 归纳摘要
    # 中等抽象: 长窗口(person 用户指定 180; fact 归入耐久档)
    "person":            {"forgettable": True, "keep_low": 0.05, "min_retain_days": 180},  # 人物/画像
    "fact":              {"forgettable": True, "keep_low": 0.05, "min_retain_days": 180},  # 事实(耐久知识)
    # 具体层: 短窗口(task 用户指定 90)
    "task":              {"forgettable": True, "keep_low": 0.05, "min_retain_days": 90},   # 项目脉络
    # 瞬态层: 极短窗口 + 较高分数地板(易忘)
    "event":             {"forgettable": True, "keep_low": 0.20, "min_retain_days": 7},    # 一次性事件
    "emotion":           {"forgettable": True, "keep_low": 0.30, "min_retain_days": 7},    # 偏好/情绪
}
DEFAULT_TYPE_POLICY = {"forgettable": True, "keep_low": 0.05, "min_retain_days": 180}



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
    n = len(vertices)
    if n == 0:
        return {}

    ids = []
    raw_valence, raw_freq, raw_recency, raw_access, raw_centrality = [], [], [], [], []
    for v in vertices:
        vid = v.get("id", "")
        ids.append(vid)
        raw_valence.append(abs(v.get("valence", 0.0)))
        raw_freq.append(v.get("weight", 1.0))
        raw_recency.append(ts_days_ago(v.get("last_accessed") or v.get("updated_at")))
        raw_access.append(v.get("access_count", 0))
        raw_centrality.append(pagerank_scores.get(vid, 0.0))

    n_valence = _minmax(raw_valence)
    n_freq = _minmax(raw_freq)
    n_recency = _minmax([1.0 - min(d / 365.0, 1.0) for d in raw_recency])
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
    return sum(w * signals.get(key, 0.0) for key, w in weights.items())


# ── Tokenization (lightweight, CJK-aware) ──────────────

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tokens(text: Optional[str]) -> set:
    """CJK char-bigrams + ascii words. Works without jieba."""
    text = (text or "").lower()
    toks: set = set()
    cjk = "".join(_CJK.findall(text))
    for i in range(len(cjk) - 1):
        toks.add(cjk[i:i + 2])
    if cjk:
        toks.add(cjk[0])
    for w in re.findall(r"[a-z0-9_]+", text):
        if len(w) >= 2:
            toks.add(w)
    return toks


def _sem(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    """Semantic overlap (Jaccard) over label+content token sets."""
    ta = _tokens((a.get("label") or "") + " " + (a.get("content") or ""))
    tb = _tokens((b.get("label") or "") + " " + (b.get("content") or ""))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _label_dup(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    la, lb = a.get("label") or "", b.get("label") or ""
    if not la or not lb:
        return False
    return la == lb or la in lb or lb in la


# ── Structural connected components (passive abstraction layer) ─

def _connected_components(
    graph: MemoryGraph,
    vertices: List[Dict[str, Any]],
) -> List[List[str]]:
    """Connected components on the live subgraph. This is a PASSIVE hint of
    'what might belong to the same abstraction' (e.g. one novel), NOT a merge decision."""
    ids = [v["id"] for v in vertices]
    id_set = set(ids)
    adj: Dict[str, set] = {i: set() for i in ids}
    for vid in ids:
        try:
            for nbr in graph.walk(vid, 1):
                if nbr in id_set and nbr != vid:
                    adj[vid].add(nbr)
        except Exception:
            pass
    visited: set = set()
    comps: List[List[str]] = []
    for vid in ids:
        if vid in visited:
            continue
        comp, queue = [], [vid]
        while queue:
            node = queue.pop(0)
            if node in visited:
                continue
            visited.add(node)
            comp.append(node)
            for nbr in adj.get(node, set()):
                if nbr not in visited:
                    queue.append(nbr)
        comps.append(comp)
    return comps


# ── Merge candidate detection (semantic-confirmed) ──────

def _merge_candidates(
    graph: MemoryGraph,
    vertices: List[Dict[str, Any]],
) -> Tuple[List[Tuple[str, str, str, float]], List[List[str]]]:
    """
    Returns (mergeable_pairs, abstraction_clusters).

    mergeable_pairs: (a, b, reason, sem) — semantically-redundant, eligible for COLLAPSE.
    abstraction_clusters: connected components (size>=2) as passive '归纳点' candidates,
                          surfaced for transparency, NOT auto-collapsed.
    """
    # Build full adjacency for component membership
    comps = _connected_components(graph, vertices)
    comp_of: Dict[str, int] = {}
    for idx, comp in enumerate(comps):
        for vid in comp:
            comp_of[vid] = idx

    by_id = {v["id"]: v for v in vertices}
    pairs: List[Tuple[str, str, str, float]] = []

    for i in range(len(vertices)):
        for j in range(i + 1, len(vertices)):
            a, b = vertices[i], vertices[j]
            da, db = a.get("domain"), b.get("domain")
            ta, tb = a.get("type"), b.get("type")
            # engine-internal nodes: never merge
            if da is None or db is None or ta is None or tb is None:
                continue
            if da != db:
                continue  # cross-domain never collapse
            if ta != tb:
                continue  # collapse requires same type (protect entity distinctions)
            sem = _sem(a, b)
            # 主动语义确认: 标签近似 或 语义重叠达标。连通分量仅作提示, 不在此驱动合并。
            if _label_dup(a, b) or sem >= COLLAPSE_THRESHOLD:
                pairs.append((a["id"], b["id"], "semantic", round(sem, 3)))

    return pairs, comps


def _collapse_group(graph: MemoryGraph, group_ids: List[str], score_of: Dict[str, float]) -> int:
    """Collapse all `group_ids` into the highest-scored canonical; trash the rest.
    Re-points direct edges to canonical, preserves provenance in `merged_from`."""
    if len(group_ids) < 2:
        return 0
    canonical_id = max(group_ids, key=lambda x: score_of.get(x, 0.0))
    merged_labels: List[str] = []
    member_list: List[Dict[str, Any]] = []
    group_set = set(group_ids)

    for vid in group_ids:
        if vid == canonical_id:
            continue
        m = graph.get_vertex(vid)
        if not m:
            continue
        merged_labels.append(m.get("label", vid))
        member_list.append({"id": vid, "label": m.get("label", vid)})
        # re-point direct neighbors to canonical
        try:
            for n in graph.walk(vid, 1):
                if n in group_set or n == vid or n == canonical_id:
                    continue
                if graph.get_vertex(n) is None:
                    continue
                graph.add_edge(
                    canonical_id, n,
                    kind="derived",
                    domain=(graph.get_vertex(canonical_id) or {}).get("domain", "knowledge"),
                    weight=0.5,
                )
        except Exception:
            pass
        graph.set_status(vid, "trashed")

    canonical = graph.get_vertex(canonical_id)
    if canonical:
        prev = canonical.get("merged_from")
        if isinstance(prev, str):
            try:
                prev = json.loads(prev)
            except json.JSONDecodeError:
                prev = []
        if not isinstance(prev, list):
            prev = []
        prev.extend(member_list)
        content = canonical.get("content") or ""
        add = "；合并自: " + "、".join(merged_labels)
        canonical["content"] = (content + add)[:2000]
        canonical["merged_from"] = prev
        canonical["merged_count"] = len(prev)
        canonical["weight"] = max(canonical.get("weight", 1.0), 1.0) + len(member_list)
        graph.upsert_vertex(canonical)

    return len(member_list)


# ── Type-aware retention ────────────────────────────────

def _retention_policy(node: Dict[str, Any]) -> Dict[str, Any]:
    domain = node.get("domain")
    ntype = node.get("type")
    # 引擎内部节点(无 domain/type, 如 root)不参与遗忘
    if domain is None or ntype is None:
        return {"forgettable": False, "keep_low": 0.0, "min_retain_days": 9999,
                "reason": "引擎内部节点(受保护)"}
    pol = TYPE_RETENTION.get(ntype, DEFAULT_TYPE_POLICY)
    return {
        "forgettable": pol["forgettable"],
        "keep_low": pol["keep_low"],
        "min_retain_days": pol["min_retain_days"],
        "reason": f"类型[{ntype}] keep_low={pol['keep_low']} min_retain={pol['min_retain_days']}d",
    }


# ── Plan + apply ────────────────────────────────────────

def consolidate(
    graph: MemoryGraph,
    access_log: List[Tuple[str, float]],
    panic_mode: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Full consolidation pipeline (merge-first, type-aware)."""
    report: Dict[str, Any] = {
        "before": {"vertices": graph.vertex_count, "edges": graph.edge_count},
        "trashed": 0,
        "merged": {"communities_found": 0, "merged": 0},
        "protected": 0,
        "abstraction_candidates": 0,
        "cap_level": "hard" if panic_mode else "normal",
        "dry_run": dry_run,
        "top_kept": [],
        "top_trashed": [],
        "merge_groups": [],
        "abstraction_clusters": [],
    }

    # Step 1: flush access_log (only when applying)
    if access_log and not dry_run:
        flushed = graph.flush_access_log(access_log)
        logger.info(f"consolidation: flushed {flushed} access log entries")

    vertices = graph.list_vertices(status="live", limit=100000)
    if not vertices:
        report["after"] = {"vertices": graph.vertex_count, "edges": graph.edge_count}
        return report

    pagerank_scores = graph.pagerank(iterations=30)
    signals = _normalize_signals(vertices, pagerank_scores)
    scored = [(v["id"], _score_vertex(signals.get(v["id"], {})), v) for v in vertices]
    score_of = {vid: sc for vid, sc, _ in scored}

    # Step 4: merge-first
    pairs, comps = _merge_candidates(graph, vertices)
    parent = {v["id"]: v["id"] for v in vertices}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(x, y):
        parent[find(x)] = find(y)
    for a, b, _r, _s in pairs:
        union(a, b)
    raw_groups: Dict[str, List[str]] = {}
    for vid in parent:
        raw_groups.setdefault(find(vid), []).append(vid)
    merge_groups = [g for g in raw_groups.values() if len(g) >= 2]

    if dry_run:
        merged_away = set()
        for g in merge_groups:
            canonical = max(g, key=lambda x: score_of.get(x, 0.0))
            merged_away |= (set(g) - {canonical})
        # type-aware trash preview (no mutation)
        trash_list = []
        protected = 0
        for vid, score, v in scored:
            if vid in merged_away:
                continue
            pol = _retention_policy(v)
            if not pol["forgettable"]:
                protected += 1
                continue
            if score < pol["keep_low"]:
                days = ts_days_ago(v.get("updated_at") or v.get("last_accessed"))
                if days <= pol["min_retain_days"]:
                    protected += 1
                    continue
                trash_list.append(vid)
            else:
                protected += 1
        by_id = {v["id"]: v for v in vertices}
        report["merge_groups"] = [
            {"canonical": max(g, key=lambda x: score_of.get(x, 0.0)),
             "members": [m for m in g if m != max(g, key=lambda x: score_of.get(x, 0.0))]}
            for g in merge_groups
        ]
        report["abstraction_clusters"] = [
            [by_id[m].get("label", m) for m in c] for c in comps if len(c) >= 3
        ]
        report["protected"] = protected
        report["trashed"] = len(trash_list)
        report["merged"]["communities_found"] = len(merge_groups)
        report["merged"]["merged"] = sum(len(g) - 1 for g in merge_groups)
        report["abstraction_candidates"] = len(report["abstraction_clusters"])
        report["after"] = {"vertices": graph.vertex_count, "edges": graph.edge_count}
        report["dry_trash_ids"] = trash_list
        report["dry_merge_members"] = [m for g in merge_groups for m in g
                                       if m != max(g, key=lambda x: score_of.get(x, 0.0))]
        return report

    # Apply merges
    merged_away = set()
    for g in merge_groups:
        n = _collapse_group(graph, g, score_of)
        report["merged"]["merged"] += 1
        merged_away |= (set(g) - {max(g, key=lambda x: score_of.get(x, 0.0))})

    # Step 5: type-aware forgetting
    for vid, score, v in scored:
        if vid in merged_away:
            continue
        pol = _retention_policy(v)
        if not pol["forgettable"]:
            report["protected"] += 1
            continue
        if score < pol["keep_low"]:
            days = ts_days_ago(v.get("updated_at") or v.get("last_accessed"))
            if days <= pol["min_retain_days"]:
                report["protected"] += 1
                continue
            graph.set_status(vid, "trashed")
            report["trashed"] += 1
            report["top_trashed"].append({"id": vid, "score": round(score, 4)})
        else:
            report["protected"] += 1

    # Step 6: panic force-trash if still over hard cap
    if panic_mode:
        remaining = graph.list_vertices(status="live", limit=100000)
        hard_vertex = MEMORY_CAPS["hard_vertex"]
        if len(remaining) > hard_vertex:
            leftover = sorted(
                [(vid, s) for vid, s, _ in scored if vid not in merged_away],
                key=lambda x: x[1],
            )
            excess = len(remaining) - int(hard_vertex * 0.8)
            for vid, _ in leftover[:excess]:
                if graph.get_vertex(vid):
                    graph.set_status(vid, "trashed")
                    report["trashed"] += 1

    report["merged"]["communities_found"] = len(merge_groups)
    report["top_kept"] = [
        {"id": vid, "score": round(s, 4)} for vid, s, _ in scored[:5]
    ]

    graph.save()
    report["after"] = {"vertices": graph.vertex_count, "edges": graph.edge_count}
    return report


# ── Capacity check (§5.6) ────────────────────────────────

def check_capacity(graph: MemoryGraph) -> Optional[str]:
    """Check if graph exceeds capacity thresholds.
    Returns None if OK, or 'soft'/'hard' if a cap was triggered.
    NOTE: capacity is a SAFETY VALVE (panic backstop), not the primary
    consolidation trigger. Primary trigger = periodic schedule + quality (§10.6)."""
    live_vertices = len(graph.list_vertices(status="live", limit=50000))
    edge_count = graph.edge_count

    if live_vertices > MEMORY_CAPS["hard_vertex"] or edge_count > MEMORY_CAPS["hard_edge"]:
        logger.warning(f"HARD cap triggered: v={live_vertices} e={edge_count}")
        return "hard"
    if live_vertices > MEMORY_CAPS["soft_vertex"] or edge_count > MEMORY_CAPS["soft_edge"]:
        logger.info(f"SOFT cap triggered: v={live_vertices} e={edge_count}")
        return "soft"
    return None
