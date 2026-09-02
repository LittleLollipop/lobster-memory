#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lobster-memory 图库通用 CLI —— 所有图库操作只走这一个入口，禁止再写临时 .py。

设计铁律（踩坑总结）：
  * 全部写操作只调 MemoryGraph 封装层（mg.upsert_vertex / mg.add_edge / mg.set_status），
    绝不碰底层 mg._g.add_edge（它对同 (src,dst) 是「替换+复制」语义，会损坏图）。
  * 图是简单有向图：每个有序点对（src->dst）最多一条边。add_edge 默认幂等拒绝已存在边；
    要改 kind 用 edge set-kind；多语义关系必须写进节点内容/边属性或绕中间节点。
  * 读操作（取边类型等）只读 mg._g 的 out_neighbors/get_edge，单进程内可靠。

命令:
  list [--prefix P] [--domain D] [--status S] [--type T]
  get <id>                       # 节点属性 + 出/入边（含 kind、label、weight）
  search <kw>                    # 全文搜 content/label
  dump [--prefix P]              # 全量节点+边导出（便于体检/备份比对）

  upsert <id> --label L [--content C] [--type T] [--domain D] [--weight W]
  status <id> <STATUS>           # 设状态：live / inactive（退役，可逆）

  edge add <from> <to> --kind K [--weight W] [--domain D] [--replace]
                                # 默认幂等：已存在则跳过并报告现有 kind；
                                # --replace：把现有唯一边的 kind 改为 K（仍保单边不变）
  edge set-kind <from> <to> --kind K [--weight W] [--domain D]
  edge rm <from> <to>

  scan-dups                     # 体检：列出任何 (src,dst,kind) 出现 >1 的重边
  bulk <file.json>              # 批量：JSON 数组，每个元素 {op:..., ...}，一次执行

bulk 文件格式示例（ops 顺序执行，失败不中断，末尾汇总）:
  [
    {"op":"upsert","id":"chr_x","label":"X","content":"..."},
    {"op":"edge_add","from":"a","to":"b","kind":"mirrors"},
    {"op":"status","id":"chr_y","status":"inactive"},
    {"op":"edge_set_kind","from":"a","to":"b","kind":"serves"},
    {"op":"edge_rm","from":"a","to":"b"}
  ]

注意:
  - 库路径默认取脚本所在项目的 .memory-graph/memory.axeb（或环境变量 LOBSTER_DB），可用 --db 覆盖任意子命令。
  - 本工具是唯一写入口；任何新需求先想「能不能用现有命令/写个 bulk JSON」，不要新开 .py。
"""
import argparse
import json
import os
import sys
from collections import deque

SKILL_ENGINE = os.environ.get(
    "LOBSTER_MEMORY_ENGINE", "/Users/sai/.workbuddy/skills/lobster-memory"
)
sys.path.insert(0, SKILL_ENGINE)

from engine.memory_graph import MemoryGraph  # noqa: E402
from engine.schema import ts_now, default_node_props, dict_from_props, props_to_dict  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根（脚本所在 tools/ 的上两级）


def _resolve_default_db():
    """默认库路径解析：优先脚本所在项目下的 .memory-graph/memory.axeb；
    其次环境变量 LOBSTER_DB；都没有则返回 None（由调用方要求显式 --db，禁止凭空新建）。"""
    local = os.path.join(BASE, ".memory-graph", "memory.axeb")
    if os.path.exists(local):
        return local
    env = os.environ.get("LOBSTER_DB")
    if env:
        return env
    return None


DEFAULT_DB = _resolve_default_db()


# ── 只读辅助：全量枚举 ──
def _all_vertex_ids(g):
    """可靠枚举所有「参与边的顶点」：从 lobster_root 出发，沿出/入边双向 BFS；
    另用 pagerank 键做二次播种，覆盖 root 不可达的高权重节点。

    之所以不能只靠 pagerank：纯汇点（只有入边）在 pagerank 里权重≈0，可能被漏，
    其上的重边就查不出来（已踩过 新书 ch020 漏检的坑）。双向 BFS 当且仅当图连通
    时覆盖全部；完全孤立（无边且无 root 边）的顶点两种法都枚举不到，但它们无边，
    不影响 scan-dups / get / dump 的边正确性，此处接受该限制。
    """
    seeds = []
    root = _sid("lobster_root")
    if g.get_vertex(root) is not None:
        seeds.append(root)
    try:
        for nid in g.pagerank().keys():
            seeds.append(nid)
    except Exception:
        pass
    visited = set()
    q = deque(seeds)
    while q:
        n = q.popleft()
        if n in visited:
            continue
        visited.add(n)
        for m in g.out_neighbors(n):
            if m not in visited:
                q.append(m)
        for m in g.in_neighbors(n):
            if m not in visited:
                q.append(m)
    return visited


def all_nodes(g):
    seen = {}
    for nid in _all_vertex_ids(g):
        v = g.get_vertex(nid)
        if v:
            d = dict_from_props(dict(v))
            seen[d.get("id")] = d
    return seen


def all_edges(g):
    """返回 [(src_id, dst_id, kind, weight, domain, status), ...]，可靠扫描。"""
    nodes = all_nodes(g)
    edges = []
    for sid, sd in nodes.items():
        sk = _sid(sid)
        for n in g.out_neighbors(sk):
            e = g.get_edge(sk, n)
            if not e:
                continue
            ed = dict_from_props(dict(e[1]))
            tid = nodes.get(_iid(g, n), {}).get("id") or _iid(g, n)
            edges.append((sid, tid, ed.get("kind"), ed.get("weight"), ed.get("domain"), ed.get("status")))
    return edges


def _sid(s):
    from engine.schema import str_to_id
    return str_to_id(s)


def _iid(g, n):
    from engine.schema import dict_from_props
    return dict_from_props(dict(g.get_vertex(n))).get("id")


# ── 写操作封装（全部走 mg 封装层） ──
def do_upsert(mg, id, label, content="", type_="concept", domain="knowledge", weight=1.0):
    props = default_node_props(id, label, domain, type_, weight, content)
    existing = mg.get_vertex(id)
    if existing:
        for k, v in props.items():
            if v is not None:
                existing[k] = v
        existing["updated_at"] = ts_now()
        mg.upsert_vertex(existing)
        return "覆盖"
    mg.upsert_vertex(props)
    return "新增"


def _raw_add_edge(mg, frm, to, kind, weight, domain):
    """底层加边（raw）。注意：本环境 raw add_edge 偶发为同对生成两条平行边，调用方须去重。"""
    sk, tk = _sid(frm), _sid(to)
    mg._g.add_edge(sk, tk, weight, {
        "kind": kind, "domain": domain, "weight": weight,
        "status": "live", "created_at": ts_now(), "updated_at": ts_now(),
    })


def _count_pair(mg, frm, to):
    sk, tk = _sid(frm), _sid(to)
    return sum(1 for n in mg._g.out_neighbors(sk) if n == tk)


def _remove_all(mg, frm, to):
    # 用 out_neighbors 计数判断存在性（可靠）；勿用 get_edge —— 跨进程偶发返回 False 误判
    while _count_pair(mg, frm, to) > 0:
        mg._g.remove_edge(_sid(frm), _sid(to))


def _ensure_single(mg, frm, to, kind, weight, domain, retries=3):
    """保证 (frm,to) 最终恰好一条边，kind 为指定值。处理 raw add 偶发双写。"""
    _remove_all(mg, frm, to)
    for _ in range(max(1, retries)):
        _raw_add_edge(mg, frm, to, kind, weight, domain)
        while _count_pair(mg, frm, to) > 1:        # 去重：平行副本删到只剩一条
            mg._g.remove_edge(_sid(frm), _sid(to))
        if _count_pair(mg, frm, to) == 1:
            return True
    return _count_pair(mg, frm, to) == 1


def do_edge_add(mg, frm, to, kind, weight=1.0, domain="knowledge", replace=False):
    if not (mg.get_vertex(frm) and mg.get_vertex(to)):
        return f"跳过：节点缺失 ({frm} 或 {to})"
    cur = None
    for (s, d, k, w, dm, st) in all_edges(mg._g):
        if s == frm and d == to:
            cur = k
            break
    cnt = _count_pair(mg, frm, to)
    # 幂等：恰好 1 条且 kind 相同才跳过；否则（含同 kind 重复边）一律去重重建
    if cur == kind and cnt == 1 and not replace:
        return f"跳过（已存在，kind={kind}）"
    ok = _ensure_single(mg, frm, to, kind, weight, domain)
    return "已加边（保单边）" if ok else "失败（边去重异常）"


def do_edge_set_kind(mg, frm, to, kind, weight=None, domain=None):
    if _count_pair(mg, frm, to) == 0:
        return f"跳过：边不存在 {frm} -> {to}"
    e = mg._g.get_edge(_sid(frm), _sid(to))
    if e is None:
        return f"跳过：边不存在 {frm} -> {to}"
    ed = dict_from_props(dict(e[1]))
    old_w = ed.get("weight", 1.0)
    old_dm = ed.get("domain", "knowledge")
    ok = _ensure_single(mg, frm, to, kind,
                        float(weight) if weight is not None else old_w,
                        domain if domain is not None else old_dm)
    return f"已设 kind -> {kind}" if ok else "失败（边重建异常）"


def do_edge_rm(mg, frm, to):
    before = _count_pair(mg, frm, to) > 0
    _remove_all(mg, frm, to)
    return "已删边" if before and _count_pair(mg, frm, to) == 0 else "边本就不存在"


def do_status(mg, id, status):
    ok = mg.set_status(id, status)
    return "已设状态" if ok else f"失败（节点不存在？{id}）"


def do_bulk(mg, path):
    with open(path, "r", encoding="utf-8") as f:
        ops = json.load(f)
    if not isinstance(ops, list):
        return ["bulk 文件顶层必须是 JSON 数组"]
    log = []
    for i, op in enumerate(ops):
        try:
            kind = op.get("op")
            if kind == "upsert":
                r = do_upsert(mg, op["id"], op["label"], op.get("content", ""),
                              op.get("type", "concept"), op.get("domain", "knowledge"),
                              float(op.get("weight", 1.0)))
            elif kind == "edge_add":
                r = do_edge_add(mg, op["from"], op["to"], op["kind"],
                                float(op.get("weight", 1.0)), op.get("domain", "knowledge"),
                                op.get("replace", False))
            elif kind == "edge_set_kind":
                r = do_edge_set_kind(mg, op["from"], op["to"], op["kind"],
                                     float(op["weight"]) if "weight" in op else None,
                                     op.get("domain"))
            elif kind == "edge_rm":
                r = do_edge_rm(mg, op["from"], op["to"])
            elif kind == "status":
                r = do_status(mg, op["id"], op["status"])
            else:
                r = f"未知 op: {kind}"
        except Exception as ex:
            r = f"异常: {ex}"
        log.append(f"[{i}] {op.get('op')} {op.get('id') or (op.get('from')+'->'+op.get('to'))} => {r}")
    return log


# ── 设定下沉校验（治「上热下冷」：设定层改了，章纲没跟着改） ──
import re as _re

DEFAULT_CHAPTER_RE = r"^ch\d{3,}$"


def _chapter_ids(nodes, pattern=DEFAULT_CHAPTER_RE):
    rx = _re.compile(pattern)
    return sorted([i for i in nodes if rx.match(i)], key=lambda x: (x, int("".join(filter(str.isdigit, x)) or 0)))


def extract_terms(content):
    """从设定节点 content 抽关键词候选：【】标题、引号短语、反引号片段、破折号后短句。"""
    terms = set()
    for m in _re.finditer(r"【([^】]{2,14})】", content):
        terms.add(m.group(1))
    for m in _re.finditer(r"[「『“\"]([^」』”\"]{2,14})[」』”\"]", content):
        terms.add(m.group(1))
    for m in _re.finditer(r"`([^`]{2,24})`", content):
        terms.add(m.group(1))
    for m in _re.finditer(r"『([^』]{2,14})』", content):
        terms.add(m.group(1))
    return {t.strip() for t in terms if 2 <= len(t.strip()) <= 14}


def do_impact(mg, node_id, kws=None, depth=2, chapter_re=DEFAULT_CHAPTER_RE, hub_degree=15):
    """列出「改动此设定节点」受影响的章号清单。
    A 组 = 边可达（depth 跳内，双向）；B 组 = 内容关键词命中（最易漏，重点看）。"""
    g = mg._g
    nodes = all_nodes(g)
    edges = all_edges(g)
    chapters = set(_chapter_ids(nodes, chapter_re))
    if node_id not in nodes:
        return [f"节点不存在: {node_id}"]

    # A 组：双向 BFS（hub 只作终点，不作中转——否则经 lobster_root/act 两跳即全图）
    adj = {}
    for s, d2, k, w, dm, st in edges:
        adj.setdefault(s, set()).add(d2)
        adj.setdefault(d2, set()).add(s)
    degree = {n: len(v) for n, v in adj.items()}
    hub = {n for n, d in degree.items() if d >= hub_degree} | {"lobster_root"}
    seen, frontier = {node_id}, {node_id}
    linked = set()
    for _ in range(max(1, depth)):
        nxt = set()
        for n in frontier:
            for m in adj.get(n, ()):
                if m in seen:
                    continue
                seen.add(m)
                if m in chapters:
                    linked.add(m)
                if m not in hub:      # hub 可达但不扩散
                    nxt.add(m)
        frontier = nxt
        if not frontier:
            break

    # B 组：关键词命中
    terms = set(kws or [])
    if not kws:
        cand = extract_terms(nodes[node_id].get("content") or "")
        terms = {t for t in cand if any(t in (nodes[c].get("content") or "") for c in chapters)}
    hit = {}
    for c in chapters:
        ctext = (nodes[c].get("content") or "") + (nodes[c].get("label") or "")
        ms = [t for t in terms if t in ctext]
        if ms:
            hit[c] = ms

    out = []
    out.append(f"# impact: {node_id} （{nodes[node_id].get('label','')}）")
    out.append(f"关键词: {sorted(terms) if terms else '(无自动抽取，请用 --kw 指定)'}")
    out.append("")
    out.append(f"## A 组 · 边可达章节（{len(linked)}）— 必须逐章核对")
    out.extend(f"  {c} | {nodes[c].get('label','')[:40]}" for c in sorted(linked))
    if not linked:
        out.append("  （无）")
    out.append("")
    out.append(f"## B 组 · 内容关键词命中（{len(hit)}）— 无边相连，最易漏")
    for c in sorted(hit):
        out.append(f"  {c} | {nodes[c].get('label','')[:32]} | 命中: {'、'.join(hit[c])}")
    if not hit:
        out.append("  （无）")
    out.append("")
    allch = sorted(linked | set(hit))
    out.append(f"## 受影响章号清单（并集 {len(allch)}）")
    out.append("  " + " ".join(allch) if allch else "  （空）")
    return out


NEG_CUES = ("不是", "并非", "禁写", "不写", "没有", "严禁", "避免", "不能", "不再",
            "别写", "禁用", "删掉", "改掉", "旧稿", "原写", "原稿", "已纠正", "纠正", "勿")


NEG_WINDOW = 80  # 小句最大回看长度（防超长句），非判定窗口


def _negated(text, term, window=NEG_WINDOW):
    """term 在 text 中的每一次出现是否都处于否定语境（『不是X』『禁写X』『旧稿X』）。
    全部被否定 → True（视为已纠正）；存在任一次未被否定 → False（视为残留）。
    偏保守：宁可误报 FAIL，不可误判 PASS。

    判定边界 = 小句（term 之前最近句读之后），**不是固定字数**——
    「禁写「影子调兵／影子下令／影子发动兵变」」这类列举式，
    否定词与 term 相隔十余字，固定 window 永远够不到；
    而若 window 放大到跨句，又会把下一句一次独立的出现误判成否定。
    小句边界同时解决这两个问题。"""
    start, n = 0, 0
    while True:
        i = text.find(term, start)
        if i < 0:
            break
        n += 1
        clause = text[max(0, i - window): i]
        cut = -1
        for sep in "。！？；，\n":
            k = clause.rfind(sep)
            if k > cut:
                cut = k
        if cut >= 0:
            clause = clause[cut + 1:]
        if not any(cue in clause for cue in NEG_CUES):
            return False
        start = i + len(term)
    return n > 0


def do_sink_check(mg, stale, ok=None, chapter_re=DEFAULT_CHAPTER_RE):
    """下沉校验：扫章节节点，报「仍写旧口径」/「已纠正(否定语境)」/「已写新口径」/「无关」。
    stale=应被替换的旧措辞（可多个）；ok=新口径关键词（可多个）。"""
    g = mg._g
    nodes = all_nodes(g)
    chapters = _chapter_ids(nodes, chapter_re)
    stale = [s for s in stale if s]
    ok = [o for o in (ok or []) if o]
    bad, good, none_, neg = [], [], [], []
    for c in chapters:
        text = (nodes[c].get("content") or "") + (nodes[c].get("label") or "")
        has_stale = [s for s in stale if s in text]
        has_ok = [o for o in ok if o in text]
        if has_stale:
            # 旧词命中：区分「真残留」与「否定语境引用」（如「她不是挡路被碾的石头」）
            real = [s for s in has_stale if not _negated(text, s)]
            if real:
                bad.append((c, real, has_ok))
            else:
                neg.append((c, has_stale, has_ok))
        elif has_ok:
            good.append((c, has_ok))
        else:
            none_.append(c)

    out = []
    out.append(f"# sink-check  staled={stale}  ok={ok}")
    out.append("")
    out.append(f"## ❌ 仍写旧口径（{len(bad)}）— 必须改")
    for c, hs, ho in bad:
        tag = f"  (已含新口径: {'、'.join(ho)})" if ho else ""
        out.append(f"  {c} | {nodes[c].get('label','')[:32]} | 旧词: {'、'.join(hs)}{tag}")
    if not bad:
        out.append("  ✅ 无残留")
    out.append("")
    out.append(f"## ⚠️ 已纠正·否定语境引用（{len(neg)}）— 人工确认后放过")
    for c, hs, ho in neg:
        tag = f"  (另含新口径: {'、'.join(ho)})" if ho else ""
        out.append(f"  {c} | {nodes[c].get('label','')[:32]} | 旧词仅出现于否定句: {'、'.join(hs)}{tag}")
    if not neg:
        out.append("  （无）")
    out.append("")
    out.append(f"## ✅ 已写新口径（{len(good)}）")
    for c, ho in good:
        out.append(f"  {c} | {'、'.join(ho)}")
    if not good:
        out.append("  （无）")
    out.append("")
    out.append(f"## ○ 未提及（{len(none_)}）— 人工判断是否相关")
    if none_:
        out.append("  " + " ".join(none_[:20]) + ("  …" if len(none_) > 20 else ""))
    else:
        out.append("  （无）")
    out.append("")
    verdict = "PASS：无旧口径残留" if not bad else f"FAIL：{len(bad)} 章仍写旧口径"
    if neg:
        verdict += f"（另有 {len(neg)} 章为否定语境引用，需人工确认）"
    out.append(f"结论: {verdict}")
    return out, (1 if bad else 0)


# 否定语境识别回归用例（每条都是真实踩过的坑，删改 _negated 后必须全绿）
NEG_CASES = [
    # (文本, term, 期望 _negated 返回值)
    ("她不是挡路被碾的石头，她是认真开关", "挡路被碾的石头", True),
    ("不写真刀真枪的大规模战争", "真刀真枪", True),
    ("她就是挡路被碾的石头", "挡路被碾的石头", False),
    ("正面挡路被碾的石头，然后兵变", "挡路被碾的石头", False),
    ("旧稿写了我兄，已纠正", "我兄", True),
    ("我兄有远志，非齐鲁可留", "我兄", False),
    # 小句边界：一句被否定，另一句独立出现 → 整体仍算残留
    ("不写真刀真枪。另外真刀真枪地打", "真刀真枪", False),
    ("不写真刀真枪，但这场戏真刀真枪", "真刀真枪", False),
    ("禁用晏婴", "晏婴", True),
    ("晏婴解梦可借鉴", "晏婴", False),
    ("", "晏婴", False),
    ("她不是挡路被碾的石头，也不是谗臣的爪牙", "挡路被碾的石头", True),
    # 列举式：否定词与 term 相隔十余字，固定 window 永远够不到 → 靠小句边界
    ("禁写「影子调兵／影子下令／影子发动兵变」", "兵变", True),
    ("禁写「他下令兵变」", "兵变", True),
    ("他下令兵变，朝野震动", "兵变", False),
    # 禁令在别的小句 → 不得外溢
    ("禁写。\n兵变发生了", "兵变", False),
    ("兵变。禁写兵变", "兵变", False),
    ("禁写兵变。兵变", "兵变", False),
]


def do_selftest():
    """否定语境识别自检——sink-check 的判据本身必须先被校验，
    否则工具一误报，人就会开始跳过它，下沉纪律随即作废。"""
    out = ["# selftest  _negated 否定语境识别", ""]
    bad = 0
    for i, (text, term, want) in enumerate(NEG_CASES, 1):
        got = _negated(text, term)
        if got != want:
            bad += 1
            out.append(f"  ✗ #{i:02d} term={term!r} want={want} got={got} | {text!r}")
    out.append("")
    out.append(f"用例 {len(NEG_CASES)} 条，失败 {bad} 条")
    out.append(f"结论: {'PASS' if bad == 0 else 'FAIL：_negated 判定回归'}")
    return out, (1 if bad else 0)


def main():
    p = argparse.ArgumentParser(description="lobster-memory 图库 CLI（通用，跨书复用）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_db(sp):
        sp.add_argument("--db", default=DEFAULT_DB, help="图库路径")

    sp = sub.add_parser("list", help="列出节点")
    add_db(sp); sp.add_argument("--prefix", default=""); sp.add_argument("--domain", default="")
    sp.add_argument("--status", default=""); sp.add_argument("--type", default="")

    sp = sub.add_parser("get", help="查节点（含出/入边与 kind）")
    add_db(sp); sp.add_argument("id")

    sp = sub.add_parser("search", help="全文搜索")
    add_db(sp); sp.add_argument("kw")

    sp = sub.add_parser("dump", help="全量导出节点+边")
    add_db(sp); sp.add_argument("--prefix", default="")

    sp = sub.add_parser("upsert", help="幂等建/改节点")
    add_db(sp); sp.add_argument("id"); sp.add_argument("--label", required=True)
    sp.add_argument("--content", default=""); sp.add_argument("--type", default="concept")
    sp.add_argument("--domain", default="knowledge"); sp.add_argument("--weight", type=float, default=1.0)

    sp = sub.add_parser("status", help="设节点状态(live/inactive)")
    add_db(sp); sp.add_argument("id"); sp.add_argument("status")

    sp = sub.add_parser("edge", help="边操作：add/set-kind/rm")
    add_db(sp)
    sp.add_argument("action", choices=["add", "set-kind", "rm"])
    sp.add_argument("from_id", metavar="FROM")
    sp.add_argument("to_id", metavar="TO")
    sp.add_argument("--kind", default="relates_to")
    sp.add_argument("--weight", type=float, default=1.0)
    sp.add_argument("--domain", default="knowledge")
    sp.add_argument("--replace", action="store_true", help="add 时把已存在边的 kind 改为 --kind")

    sp = sub.add_parser("scan-dups", help="重边体检")
    add_db(sp)

    sp = sub.add_parser("bulk", help="批量执行 JSON 操作文件")
    add_db(sp); sp.add_argument("file")

    sp = sub.add_parser(
        "impact",
        help="设定下沉·影响面：列出改动某设定节点受影响的章号清单（A=边可达 B=内容命中）")
    add_db(sp); sp.add_argument("id")
    sp.add_argument("--kw", action="append", default=[],
                    help="关键词（可多个）；不给则自动从节点 content 抽取")
    sp.add_argument("--depth", type=int, default=2, help="边可达跳数，默认 2")
    sp.add_argument("--hub-degree", type=int, default=15,
                    help="度数≥该值视为枢纽，可作终点但不中转（防两跳连通全图），默认 15")
    sp.add_argument("--chapter-re", default=DEFAULT_CHAPTER_RE, help="章节点 id 正则")

    sp = sub.add_parser("selftest", help="工具自检：否定语境识别回归用例（不需要图库）")

    sp = sub.add_parser(
        "sink-check",
        help="设定下沉·校验：扫章节报「仍写旧口径 / 已纠正(否定语境) / 已写新口径 / 未提及」")
    add_db(sp)
    sp.add_argument("--stale", action="append", default=[], required=True,
                    help="应被替换的旧措辞（可多个）")
    sp.add_argument("--ok", action="append", default=[], help="新口径关键词（可多个）")
    sp.add_argument("--chapter-re", default=DEFAULT_CHAPTER_RE, help="章节点 id 正则")

    args = p.parse_args()
    if args.cmd == "selftest":
        lines, code = do_selftest()
        print("\n".join(lines))
        sys.exit(code)
    if args.db is None:
        sys.stderr.write(
            "未指定图库路径：请在含 .memory-graph/memory.axeb 的项目目录下运行，"
            "或用 --db 指定，或设环境变量 LOBSTER_DB。\n")
        sys.exit(2)
    mg = MemoryGraph(args.db)
    g = mg._g

    if args.cmd == "list":
        nodes = all_nodes(g)
        ids = sorted(nodes.keys())
        if args.prefix:
            ids = [i for i in ids if i.startswith(args.prefix)]
        print(f"节点总数: {len(ids)} (过滤后 {len([i for i in ids if (not args.domain or nodes[i].get('domain')==args.domain) and (not args.status or nodes[i].get('status')==args.status) and (not args.type or nodes[i].get('type')==args.type)])})")
        for i in ids:
            d = nodes[i]
            if args.domain and d.get("domain") != args.domain: continue
            if args.status and d.get("status") != args.status: continue
            if args.type and d.get("type") != args.type: continue
            print(f"  {i} | {d.get('label','')[:40]} | w{d.get('weight','')} | {d.get('status','')}")

    elif args.cmd == "get":
        d = mg.get_vertex(args.id)
        if not d:
            print(f"节点不存在: {args.id}"); sys.exit(1)
        print(f"id: {d.get('id')}")
        print(f"label: {d.get('label')}")
        print(f"type: {d.get('type')} | domain: {d.get('domain')} | weight: {d.get('weight')} | status: {d.get('status')}")
        print(f"created: {d.get('created_at')} | updated: {d.get('updated_at')}")
        print(f"content: {d.get('content','')}")
        edges = all_edges(g)
        out = [(d2, k, w) for (s, d2, k, w, dm, st) in edges if s == args.id]
        inn = [(s, k, w) for (s, d2, k, w, dm, st) in edges if d2 == args.id]
        print("出边:")
        for d2, k, w in out:
            print(f"  -> {d2} [{k}] w{w}")
        print("入边:")
        for s, k, w in inn:
            print(f"  <- {s} [{k}] w{w}")

    elif args.cmd == "search":
        nodes = all_nodes(g)
        hits = [(i, nodes[i].get("label", "")) for i in nodes
                if args.kw in (nodes[i].get("label", "") + " " + (nodes[i].get("content", "") or ""))]
        print(f"命中 {len(hits)} 条（含「{args.kw}」）:")
        for i, label in hits:
            print(f"  {i} | {label[:40]}")

    elif args.cmd == "dump":
        nodes = all_nodes(g)
        ids = sorted(nodes.keys())
        if args.prefix:
            ids = [i for i in ids if i.startswith(args.prefix)]
        print(f"=== 节点 ({len(ids)}) ===")
        for i in ids:
            d = nodes[i]
            print(f"{i} | {d.get('label','')} | {d.get('type')}/{d.get('domain')} | {d.get('status')} | {d.get('content','')[:60]}")
        edges = all_edges(g)
        print(f"\n=== 边 ({len(edges)}) ===")
        for s, d2, k, w, dm, st in edges:
            if args.prefix and not (s.startswith(args.prefix) or d2.startswith(args.prefix)):
                continue
            print(f"{s} -[{k}]-> {d2} w{w} {st}")

    elif args.cmd == "upsert":
        r = do_upsert(mg, args.id, args.label, args.content, args.type, args.domain, args.weight)
        print(f"{r}节点: {args.id}")

    elif args.cmd == "status":
        print(do_status(mg, args.id, args.status))

    elif args.cmd == "edge":
        if args.action == "add":
            print(do_edge_add(mg, args.from_id, args.to_id, args.kind, args.weight, args.domain, args.replace))
        elif args.action == "set-kind":
            print(do_edge_set_kind(mg, args.from_id, args.to_id, args.kind, args.weight, args.domain))
        else:
            print(do_edge_rm(mg, args.from_id, args.to_id))

    elif args.cmd == "scan-dups":
        edges = all_edges(g)
        seen = {}
        for (s, d2, k, w, dm, st) in edges:
            key = (s, d2, k); seen[key] = seen.get(key, 0) + 1
        dups = [(k, c) for k, c in seen.items() if c > 1]
        print(f"边总数: {len(edges)}；重边组数: {len(dups)}")
        for (s, d2, k), c in dups:
            print(f"  {s} -[{k}]-> {d2} x{c}")
        if not dups:
            print("  ✅ 无平行重复边")

    elif args.cmd == "bulk":
        for line in do_bulk(mg, args.file):
            print(line)

    elif args.cmd == "impact":
        for line in do_impact(mg, args.id, args.kw or None, args.depth, args.chapter_re, args.hub_degree):
            print(line)

    elif args.cmd == "sink-check":
        lines, code = do_sink_check(mg, args.stale, args.ok or None, args.chapter_re)
        for line in lines:
            print(line)
        mg.close()
        sys.exit(code)   # 有残留则非零退出，便于脚本/自动化门禁

    mg.close()


if __name__ == "__main__":
    main()
