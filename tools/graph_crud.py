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
  list [--prefix P] [--bare] [--id-only]  # ⚠️默认输出带前导空格，管道取 id 用 --bare --id-only
  get <id>                       # 节点属性 + 出/入边（含 kind、label、weight）
  search <kw>                    # 全文搜 content/label
  dump [--prefix P] [--full]     # 全量节点+边导出（便于体检/备份比对）
                                 # ⚠️默认 content 只取前 60 字；批量分析必须加 --full

  upsert <id> --label L [--content C] [--type T] [--domain D] [--weight W]
  status <id> <STATUS>           # 设状态：live / inactive（退役，可逆）

  edge add <from> <to> --kind K [--weight W] [--domain D] [--replace]
                                # 默认幂等：已存在则跳过并报告现有 kind；
                                # --replace：把现有唯一边的 kind 改为 K（仍保单边不变）
  edge set-kind <from> <to> --kind K [--weight W] [--domain D]
  edge rm <from> <to>

  scan-dups                     # 体检：列出任何 (src,dst,kind) 出现 >1 的重边
  check-ids                     # 体检：props['id'] 是否被污染成哈希数字（有污染即 exit 1）
                                # 症状：get 入边为空 / list --prefix 漏检 / dump id 不可用
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
  - status op 的字段名是 **status**（不是 value；CLI 子命令 status <id> <STATUS> 才是位置参数，
    两者不一致极易写混）。现已同时兼容 value，缺字段时给明确报错而非 KeyError。
  - ⚠️ upsert 会把 status 重置为 live：被冻结/退役节点在本批 upsert 全部跑完后，
    最后再跑一次 `status <id> frozen|inactive` 补回（这也是 bulk 里 status op 常放末尾的原因）。
  - ⚠️ **写 bulk 用 Write 一次成型，不要用 Edit 往数组里追加元素**：追加时需匹配上一个元素
    的结尾（多为 `...。"\n  }`），而多个节点的 content 常常以同一句收尾（如口径注），
    匹配不唯一就会失败；换更长上下文又极易粘到错误位置，把 JSON 写坏。
    元素多就分段写成多个 json 分次 bulk，或用脚本 json.load → append → json.dump。
    落库前一律先 `python -c "import json;json.load(open(f))"` 校验。
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
from engine.schema import (  # noqa: E402
    default_node_props,
    dict_from_props,
    is_polluted_id,
    props_to_dict,
    ts_now,
    validate_str_id,
)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根（脚本所在 tools/ 的上两级）


def _find_up(start, target=".memory-graph/memory.axeb"):
    """从 start 逐级向上找 target，找到返回绝对路径，到根为止返回 None。"""
    cur = os.path.abspath(start)
    while True:
        cand = os.path.join(cur, *target.split("/"))
        if os.path.exists(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _resolve_default_db():
    """默认库路径解析顺序：① 脚本所在项目根（软链 tools/graph_crud.py 的常见情形）；
    ② 当前工作目录逐级向上找（**从技能目录直接调真实路径时的救命分支**——
       此时 BASE 指向技能根，那里没有 .memory-graph，旧版只会报「未指定图库路径」）；
    ③ 环境变量 LOBSTER_DB；都没有则返回 None（由调用方要求显式 --db，禁止凭空新建）。"""
    local = os.path.join(BASE, ".memory-graph", "memory.axeb")
    if os.path.exists(local):
        return local
    cwd_db = _find_up(os.getcwd())
    if cwd_db:
        return cwd_db
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


def _s(v):
    """剥离 id 两端空白：list 输出带前导空格，粘贴进 bulk JSON 后
    会静默建出带空格的孤儿节点（查得到 id 却 get 不到内容）。"""
    return v.strip() if isinstance(v, str) else v


def do_bulk(mg, path):
    with open(path, "r", encoding="utf-8") as f:
        ops = json.load(f)
    if not isinstance(ops, list):
        return ["bulk 文件顶层必须是 JSON 数组"]
    log = []
    for i, op in enumerate(ops):
        try:
            kind = op.get("op")
            # 端点字段名兼容：edge_* 支持 from/to 与 src/dst 两种写法。
            # 旧版只认 from/to，写 src/dst 时日志行抛 TypeError（None+str），
            # 报错信息完全看不出是字段名问题——属「工具难用人就绕过去」的典型
            # （与 status/value 同款，见下）。
            frm = _s(op.get("from") or op.get("src"))
            to = _s(op.get("to") or op.get("dst"))
            if kind == "upsert":
                r = do_upsert(mg, _s(op["id"]), op["label"], op.get("content", ""),
                              op.get("type", "concept"), op.get("domain", "knowledge"),
                              float(op.get("weight", 1.0)))
            elif kind == "edge_add":
                r = do_edge_add(mg, frm, to, op["kind"],
                                float(op.get("weight", 1.0)), op.get("domain", "knowledge"),
                                op.get("replace", False))
            elif kind == "edge_set_kind":
                r = do_edge_set_kind(mg, frm, to, op["kind"],
                                     float(op["weight"]) if "weight" in op else None,
                                     op.get("domain"))
            elif kind == "edge_rm":
                r = do_edge_rm(mg, frm, to)
            elif kind == "status":
                # 兼容两种写法：{"status": "frozen"} 与 {"value": "frozen"}。
                # CLI 子命令用位置参数（status <id> <value>），写 bulk 时极易顺手写 value，
                # 旧版只认 status 且报错仅一句 KeyError，属「工具难用人就绕过去」的典型。
                sv = op.get("status") or op.get("value")
                if not sv:
                    r = ("status op 缺字段：需 op['status'] 或 op['value']"
                         "（取值 live / inactive / frozen）")
                else:
                    r = do_status(mg, _s(op["id"]), sv)
            else:
                r = f"未知 op: {kind}"
        except Exception as ex:
            r = f"异常: {ex}"
        who = op.get("id") or (f"{frm}->{to}" if frm and to else "<缺 id / from-src / to-dst>")
        log.append(f"[{i}] {kind} {who} => {r}")
    return log


# ── 设定下沉校验（治「上热下冷」：设定层改了，章纲没跟着改） ──
import re as _re

DEFAULT_CHAPTER_RE = r"^ch\d{3,}$"
DEFAULT_MAX_COVER = 0.6  # impact B 组：关键词命中章数占比超过此值即视为无区分度，剔除


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


def _chapter_text(nodes, c):
    return (nodes[c].get("content") or "") + (nodes[c].get("label") or "")


def do_impact(mg, node_id, kws=None, depth=2, chapter_re=DEFAULT_CHAPTER_RE, hub_degree=15,
              max_cover=DEFAULT_MAX_COVER):
    """列出「改动此设定节点」受影响的章号清单。
    A 组 = 边可达（depth 跳内，双向）；B 组 = 内容关键词命中（最易漏，重点看）。

    max_cover：B 组关键词的**区分度闸门**——命中章数占比超过该阈值的词视为噪声剔除。
    默认 0.6。由来：不做这道闸，自动抽词常抽到「出场」「钟」这类词，
    命中 96/96 章，B 组等于全量章号，比不给还糟（人无法逐章核，只能放弃这个工具）。
    """
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
    # ★覆盖度闸门：先算出每个词的命中率，超过阈值的一律剔除并**明示**——
    # 不静默丢弃，否则用户以为「关键词全命中＝影响面真这么大」。
    n_ch = max(1, len(chapters))
    cover = {t: sum(1 for c in chapters if t in _chapter_text(nodes, c)) / n_ch
             for t in terms}
    noise = sorted(t for t, r in cover.items() if r > max_cover)
    terms = terms - set(noise)

    hit = {}
    for c in chapters:
        ms = [t for t in terms if t in _chapter_text(nodes, c)]
        if ms:
            hit[c] = ms

    out = []
    out.append(f"# impact: {node_id} （{nodes[node_id].get('label','')}）")
    out.append(f"关键词: {sorted(terms) if terms else '(无自动抽取，请用 --kw 指定)'}")
    if noise:
        out.append(
            f"⚠️ 已剔除高频无区分度词（命中 >{max_cover:.0%} 章）：{'、'.join(noise)}"
            f" — 全命中＝没筛选；确需保留用 --max-cover 1.0")
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
            "别写", "禁用", "删掉", "改掉", "旧稿", "原写", "原稿", "已纠正", "纠正", "勿",
            # ⛔ 及下列为 2026-09-08 补。⛔ 是本项目**禁令的专用标记**（红线一律写作「⛔X」），
            # 它其实是最强的否定信号，却一直不在表里——后果是 sink-check 把每一条
            # 红线声明**本身**判成「旧词残留」：写得越守纪律，FAIL 越多。
            # 教训与 _titled_ref 收录书名/题眼完全同构——**判据若不认人真正使用的写法，
            # 人就只能手动跳过，纪律随即作废**。
            "⛔", "不得", "不许", "不该", "不可", "忌写")


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
        if any(cue in clause for cue in NEG_CUES):
            start = i + len(term)
            continue
        # ⛔ 是**行首禁令标记**（写法如「- ⛔红线⑥：…」「⛔称呼：…」），
        # 作用域是整行/整个条目，不是一个谓词——用小句边界会把它切掉
        # （「⛔称呼：…；「钟无艳」仅民间戏语层」里的 ⛔ 落在分号之前）。
        # 因此 ⛔ 单独用**句级**边界（。！？\n，不含分号逗号）。
        head = text[:i]
        scut = -1
        for sep in "。！？\n":
            k = head.rfind(sep)
            if k > scut:
                scut = k
        if "⛔" in head[scut + 1:]:
            start = i + len(term)
            continue
        return False
    return n > 0


TITLE_CUES = ("书名", "题眼", "题名", "戏名", "唱本", "本名",
              # 民间戏语层引用：俗话/唱段是故事内被引用的声音，不是史官主声在用它。
              # ★只收**指称一种被引用文本/声音的名词**，不收地点状语——
              #   「街市上都叫她钟无艳」里的「街市」是地点不是引用标记，收进来会把真残留洗白
              "俗话", "俗语", "谚语", "民谚", "唱段", "唱词", "戏语", "戏文")
GLOSS_CUES = ("指", "一词", "这个称呼", "这个称谓", "称谓", "意为", "说的是", "称呼")
GLOSS_SPAN = 12


def _titled_ref(text, term):
    """term 的每一次出现是否都在书名号《…》内，或紧邻「书名／题眼」等引用提示词。
    与 _negated 同为「放过」判据但语义不同：否定语境＝作者在纠正，引用语境＝作者在指称书名。
    真实坑（2026-09-02）：ch001 写「（书名《有事钟无艳》的民间源头自此埋下）」——
    「钟无艳」在这里是被引用的书名（红线⑥的合法例外①），不是史官层的误用，
    而 sink-check 只认否定语境，把它判成残留 → 人只能手动跳过，纪律随之作废。"""
    start, n = 0, 0
    while True:
        i = text.find(term, start)
        if i < 0:
            break
        n += 1
        pre = text[max(0, i - 16): i]
        post = text[i + len(term): i + len(term) + 16]
        in_brackets = False
        lb = pre.rfind("《")
        if lb >= 0 and "》" not in pre[lb:]:
            rb = post.find("》")
            if rb >= 0 and "《" not in post[:rb]:
                in_brackets = True
        near_cue = any(cue in text[max(0, i - 8):i] for cue in TITLE_CUES)
        # 释义语境：「娘娘」指妻妾身份——作者在**谈论**这个词（多半是红线的裁定说明），不是在用它
        in_quotes = (text[i - 1:i] in ("「", "『", "\"", "'")
                     and post[:1] in ("」", "』", "\"", "'"))
        gloss = in_quotes and any(cue in post[:GLOSS_SPAN] for cue in GLOSS_CUES)
        # 更长引用短语的一部分：如「有事钟无艳」里的「钟无艳」——
        # 作者引用的是那个短语整体（俗语/题眼），不是单独在用这个词。
        # 判据：term 被引号包裹，且引号内的内容比 term 本身长。
        part_of_phrase = False
        lo = max(pre.rfind("「"), pre.rfind("『"),
                 pre.rfind("\""), pre.rfind("'"))
        hi = min([x for x in (post.find("」"), post.find("』"),
                              post.find("\""), post.find("'")) if x >= 0],
                 default=-1)
        if lo >= 0 and hi >= 0:
            inner = text[lo + 1: i + len(term) + hi]
            part_of_phrase = len(inner.strip()) > len(term)
        if not (in_brackets or near_cue or gloss or part_of_phrase):
            return False
        start = i + len(term)
    return n > 0


EXEMPT_MARK = "下沉豁免"
EXEMPT_SPAN = 60


def _exempt(text, term):
    """节点显式声明的豁免：形如「【下沉豁免·钟无艳：民间戏语层，红线⑥例外】」。
    规则允许的例外必须写进数据本身（带理由），而不是让人每次手动跳过——
    手动跳过的下一步就是"反正每次都要手动看"，下沉纪律随即作废。

    只认**标记里点名的词**：「【下沉豁免·娘娘：…】」不豁免「钟无艳」——
    豁免必须指名道姓，否则一个标记就把整章洗白了。"""
    start = 0
    while True:
        k = text.find(EXEMPT_MARK, start)
        if k < 0:
            return False
        seg = text[k + len(EXEMPT_MARK): k + len(EXEMPT_MARK) + EXEMPT_SPAN]
        m = _re.match(r"\s*[·:：]?\s*([^\n】：；]{1,40})", seg)
        if m and term in m.group(1):
            return True
        start = k + len(EXEMPT_MARK)
    return False


def do_sink_check(mg, stale, ok=None, chapter_re=DEFAULT_CHAPTER_RE):
    """下沉校验：扫章节节点，报「仍写旧口径」/「引用语境(否定句·书名题眼)」/「已写新口径」/「无关」。
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
            # 旧词命中：区分「真残留」与「引用语境」（否定句如「她不是挡路被碾的石头」、
            # 书名题眼如「书名《有事钟无艳》」——后者是红线允许的引用，不是误用）
            real = [s for s in has_stale if not _negated(text, s)]
            cited, exempted = [], []
            for s in real:
                if _titled_ref(text, s):
                    cited.append(s)
                elif _exempt(text, s):
                    exempted.append(s)
            real = [s for s in real if s not in cited and s not in exempted]
            for s in cited:
                neg.append((c, [s], has_ok, "引用·书名/题眼/释义"))
            for s in exempted:
                neg.append((c, [s], has_ok, "节点声明豁免"))
            if real:
                bad.append((c, real, has_ok))
            elif not cited and not exempted:
                neg.append((c, has_stale, has_ok, "否定句"))
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
    out.append(f"## ⚠️ 引用语境（{len(neg)}）— 否定句或书名/题眼引用，人工确认后放过")
    for c, hs, ho, kind in neg:
        tag = f"  (另含新口径: {'、'.join(ho)})" if ho else ""
        out.append(f"  {c} | {nodes[c].get('label','')[:32]} | {kind}: {'、'.join(hs)}{tag}")
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
        n_title = sum(1 for x in neg if x[3] == "引用·书名/题眼/释义")
        n_ex = sum(1 for x in neg if x[3] == "节点声明豁免")
        n_neg = len(neg) - n_title - n_ex
        detail = []
        if n_neg:
            detail.append(f"{n_neg} 章否定句")
        if n_title:
            detail.append(f"{n_title} 章书名/题眼引用")
        if n_ex:
            detail.append(f"{n_ex} 章节点声明豁免")
        verdict += f"（另有 {'、'.join(detail)}，需人工确认）"
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
    # ⛔ 作为禁令标记（2026-09-08）：本项目的红线一律写成「⛔X」，
    # 曾因不在 cue 表里，导致每条红线声明都被判成「旧词残留」——守纪律反被罚。
    ("⛔称呼：史官主声一律「钟离春」；「钟无艳」仅民间戏语层", "钟无艳", True),
    ("⛔**红线⑥：层1 主声⛔「钟无艳」**（一律钟离春／无盐女）", "钟无艳", True),
    ("⛔「钟无艳」不得渗出", "钟无艳", True),
    ("⛔⛔红线⑥：层1 一律「钟离春／无盐女」，⛔「钟无艳」不得渗出", "钟无艳", True),
    # 反向：⛔ 在别的小句 → 不得外溢；没有 ⛔ 就是真残留
    ("⛔红线。\n史官层称她钟无艳", "钟无艳", False),
    ("旁白说她就是钟无艳", "钟无艳", False),
    # 自检问句（「有没有出现X？」）不是残留
    ("有没有出现「钟无艳」（层1）？", "钟无艳", True),
]


# 引用语境（书名/题眼）回归用例：旧词是被指称的书名，不是误用
TITLED_CASES = [
    # (文本, term, 期望 _titled_ref 返回值)
    ("（书名《有事钟无艳》的民间源头自此埋下）", "钟无艳", True),
    ("题眼「有事钟无艳，无事夏迎春」", "钟无艳", True),
    ("她就是钟无艳", "钟无艳", False),
    # 书名号与另一处独立出现并存 → 不得整体放行
    ("书名《有事钟无艳》。街市上都叫她钟无艳", "钟无艳", False),
    ("", "钟无艳", False),
    # 释义语境：作者在谈论这个词（红线裁定说明），不是在用这个词
    ("「娘娘」指妻妾身份，与接下后位不冲突", "娘娘", True),
    ("她被称作「娘娘」，人人称善", "娘娘", False),
    ("「娘娘」是宋元以后的宫廷称呼", "娘娘", True),
    # 民间戏语层引用（2026-09-03）：俗话/唱段是故事内被引用的声音，
    # 与书名同级——红线⑥本来就允许「钟无艳」只属民间戏语层
    ("市井里在传一句俗话——「有事钟无艳，无事夏迎春」", "钟无艳", True),
    ("木鱼书唱段里唱她钟无艳", "钟无艳", True),
    # 反向：地点状语不是引用标记（「街市」「市井」若收进 cue 表，真残留会被洗白）
    ("街市上都叫她钟无艳", "钟无艳", False),
    ("市井传她是钟无艳", "钟无艳", False),
    # 反向：没有引用提示词，就是史官主声在用它 → 真残留
    ("史官主声一律称她钟无艳", "钟无艳", False),
    ("她就是那个钟无艳，无人不晓", "钟无艳", False),
    # 更长引用短语的一部分（2026-09-08）：「有事钟无艳」是被引用的俗语/题眼整体，
    # 作者不是在单独使用「钟无艳」这个词
    ("这正是「有事钟无艳」的原型动作", "钟无艳", True),
    ("「有事钟无艳，无事夏迎春」这句俗话", "钟无艳", True),
    # 反向：引号内就是 term 本身 → 不算短语引用（应靠否定语境或真残留判定）
    ("她被叫作「钟无艳」", "钟无艳", False),
]


# 节点声明豁免的回归用例
EXEMPT_CASES = [
    # (文本, term, 期望 _exempt 返回值)
    ("【下沉豁免·钟无艳：民间戏语层，红线⑥例外】", "钟无艳", True),
    ("【下沉豁免·钟无艳：民间戏语层】她唱的是钟无艳", "钟无艳", True),
    ("【下沉豁免·娘娘：民间戏语层】她还是叫钟无艳", "钟无艳", False),
    ("史官层写她", "钟无艳", False),
]


# ── id 形态体检（治「props['id'] 被写成哈希」，2026-09-10 事故） ──
def do_check_ids(mg):
    """体检：列出 props['id'] 被污染成纯数字（str_to_id 输出）的节点。

    污染后果：get 的**入边清单为空**、list --prefix **漏检**、dump 显示不可用 id；
    原始字符串**不可逆**（哈希），只能按 label 反查历史脚本/JSON 回填。
    返回 (lines, exit_code)：有污染即非零退出，便于门禁。
    """
    g = mg._g
    rows = []
    for nid in _all_vertex_ids(g):
        raw = g.get_vertex(nid)
        if raw is None:
            continue
        p = dict_from_props(dict(raw))
        if is_polluted_id(p.get("id")):
            rows.append((str(p.get("id")), p.get("label", ""), p.get("type", "")))
    out = ["# id 形态体检", ""]
    out.append(f"污染节点: {len(rows)}")
    if not rows:
        out.append("  ✅ 所有节点 props['id'] 均为非纯数字字符串")
    else:
        out.append("  ⚠️ 下列节点 props['id'] 是 str_to_id 的十进制输出（原始字符串不可逆）")
        out.append("     连带症状：get 入边为空 / list --prefix 漏检 / dump 的 id 不可用")
        out.append("")
        for pid, lab, typ in sorted(rows, key=lambda x: x[1]):
            out.append(f"  {pid} | {typ or '-':<10} | {lab}")
        out.append("")
        out.append("  恢复法：按 label 反查历史脚本/JSON 中的明文 id，"
                   "验证 str_to_id(候选) == 该节点键 后回填 raw['id']，并 **save()**。")
    return out, (1 if rows else 0)


def do_selftest():
    """引用语境识别自检——sink-check 的判据本身必须先被校验，
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

    out.append("")
    out.append("# selftest  _titled_ref 引用语境识别（书名/题眼）")
    out.append("")
    tbad = 0
    for i, (text, term, want) in enumerate(TITLED_CASES, 1):
        got = _titled_ref(text, term)
        if got != want:
            tbad += 1
            out.append(f"  ✗ #{i:02d} term={term!r} want={want} got={got} | {text!r}")
    out.append("")
    out.append(f"用例 {len(TITLED_CASES)} 条，失败 {tbad} 条")

    out.append("")
    out.append("# selftest  _exempt 节点声明豁免")
    out.append("")
    ebad = 0
    for i, (text, term, want) in enumerate(EXEMPT_CASES, 1):
        got = _exempt(text, term)
        if got != want:
            ebad += 1
            out.append(f"  ✗ #{i:02d} term={term!r} want={want} got={got} | {text!r}")
    out.append("")
    out.append(f"用例 {len(EXEMPT_CASES)} 条，失败 {ebad} 条")

    out.append("")
    out.append("# selftest  validate_str_id / is_polluted_id（id 污染守卫）")
    out.append("")
    vbad = 0
    vcases = [
        ("ch003", True), ("fx_lingge", True), ("a", True),
        ("ch001_old", True), ("1234567", True),          # 7 位数字不拦（非哈希形态）
        ("8253730911577692809", False), ("12345678", False),
        ("", False), (None, False), (12345, False),
    ]
    for i, (val, want_ok) in enumerate(vcases, 1):
        try:
            validate_str_id(val)
            got = True
        except Exception:
            got = False
        if got != want_ok:
            vbad += 1
            out.append(f"  ✗ #{i:02d} val={val!r} want_ok={want_ok} got={got}")
    out.append("")
    out.append(f"用例 {len(vcases)} 条，失败 {vbad} 条")

    total = bad + tbad + ebad + vbad
    ncases = len(NEG_CASES) + len(TITLED_CASES) + len(EXEMPT_CASES) + len(vcases)
    out.append("")
    out.append(f"合计 {ncases} 条，失败 {total} 条")
    out.append(f"结论: {'PASS' if total == 0 else 'FAIL：判据回归'}")
    return out, (1 if total else 0)


def main():
    p = argparse.ArgumentParser(description="lobster-memory 图库 CLI（通用，跨书复用）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_db(sp):
        sp.add_argument("--db", default=DEFAULT_DB, help="图库路径")

    sp = sub.add_parser("list", help="列出节点")
    add_db(sp); sp.add_argument("--prefix", default=""); sp.add_argument("--domain", default="")
    sp.add_argument("--status", default=""); sp.add_argument("--type", default="")
    sp.add_argument("--bare", action="store_true",
                    help="输出不带缩进与分隔（每行仅 `id` 或 `id|label`），便于管道取 id")
    sp.add_argument("--id-only", action="store_true", help="只输出 id 一列（配合 --bare 使用）")

    sp = sub.add_parser("get", help="查节点（含出/入边与 kind）")
    add_db(sp); sp.add_argument("id")

    sp = sub.add_parser("search", help="全文搜索")
    add_db(sp); sp.add_argument("kw")

    sp = sub.add_parser("dump", help="全量导出节点+边")
    add_db(sp); sp.add_argument("--prefix", default="")
    sp.add_argument("--full", action="store_true",
                    help="输出完整 content（默认只取前 60 字；⚠️批量分析必须用 --full，否则静默失真）")

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

    sp = sub.add_parser(
        "check-ids", help="体检 props['id'] 是否被污染成哈希数字（有污染即 exit 1）")
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
    sp.add_argument("--max-cover", type=float, default=DEFAULT_MAX_COVER,
                    help=f"B 组关键词命中章数占比上限，超过即视为无区分度并剔除（默认 {DEFAULT_MAX_COVER}；"
                         f"设 1.0 关闭该闸门）")

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

    # ⚠️ id 类参数统一 strip：`list` 默认输出带两个前导空格，把首字段直接喂给
    # get/edge 会「静默取到空串」——不报错，只是查不到（2026-09-08 事故：
    # 批量取全文时数百节点全返回空，整轮审计结论失真）。根治放在入口，
    # 这样无论调用方 strip 与否都不会再踩。
    for _a in ("id", "from_id", "to_id", "prefix"):
        _v = getattr(args, _a, None)
        if isinstance(_v, str):
            setattr(args, _a, _v.strip())
    for _a in ("kw", "stale", "ok"):
        _v = getattr(args, _a, None)
        if isinstance(_v, list):
            setattr(args, _a, [x.strip() for x in _v if isinstance(x, str)])

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
            if args.bare:
                # ⚠️ 默认输出带两个前导空格（人眼可读），但把首字段直接喂给 get
                # 会「静默取到空串」——不报错、只是查不到（2026-09-08 事故）。
                # 管道取 id 一律用 --bare --id-only。
                print(i if args.id_only else f"{i}|{d.get('label','')[:40]}")
            else:
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
            body = d.get('content', '')
            if not args.full:
                body = body[:60]
            print(f"{i} | {d.get('label','')} | {d.get('type')}/{d.get('domain')} | {d.get('status')} | {body}")
        edges = all_edges(g)
        print(f"\n=== 边 ({len(edges)}) ===")
        for s, d2, k, w, dm, st in edges:
            if args.prefix and not (s.startswith(args.prefix) or d2.startswith(args.prefix)):
                continue
            print(f"{s} -[{k}]-> {d2} w{w} {st}")

    elif args.cmd == "upsert":
        try:
            r = do_upsert(mg, args.id, args.label, args.content, args.type, args.domain,
                          args.weight)
            print(f"{r}节点: {args.id}")
        except ValueError as ex:
            # id 形态非法（如把 str_to_id 输出当 id 回写）——拒绝写入并给可读报错，
            # 而不是甩一段 traceback（2026-09-10 事故的守卫，见 check-ids）。
            print(f"❌ {ex}")
            mg.close()
            sys.exit(2)

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

    elif args.cmd == "check-ids":
        lines, code = do_check_ids(mg)
        for line in lines:
            print(line)
        mg.close()
        sys.exit(code)   # 有污染则非零退出，便于脚本/自动化门禁

    elif args.cmd == "bulk":
        for line in do_bulk(mg, args.file):
            print(line)

    elif args.cmd == "impact":
        for line in do_impact(mg, args.id, args.kw or None, args.depth, args.chapter_re,
                              args.hub_degree, args.max_cover):
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
