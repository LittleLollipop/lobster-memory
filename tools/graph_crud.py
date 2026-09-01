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


# ── 只读辅助：全量枚举（pagerank 兜底，list_vertices 会漏只有入边的节点） ──
def all_nodes(g):
    seen = {}
    for k in g.pagerank().keys():
        v = g.get_vertex(k)
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


def main():
    p = argparse.ArgumentParser(description="《有事钟无艳》图库 CLI")
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

    args = p.parse_args()
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

    mg.close()


if __name__ == "__main__":
    main()
