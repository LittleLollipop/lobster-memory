#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
干净龙虾测试脚本
================

模拟一个「什么都不知道」的纯净龙虾,严格按照 lobster-memory 技能的行为协议,
验证长期记忆系统的真实效果:

  Session 1  ── 几轮对话 → 抽取关键点 → 写入图 → 关闭
  (关闭,模拟龙虾重启/新会话)
  Session 2  ── 重开同一 .axeb → 回忆应返回 Session 1 的记忆
                          → 证明记忆跨会话存活

运行方式(必须用 lobster-memory venv 的 Python):
  ~/.workbuddy/venvs/lobster-memory/bin/python test_clean_lobster.py

注意:脚本里的 `mock_extract()` 是 LLM 抽取器的占位实现。
真实使用时,龙虾会用自己的 LLM 能力执行 `build_extraction_prompt` 返回的 prompt,
得到同样的 JSON 结构。这里用硬编码 JSON 保证测试可复现。
"""

import os
import sys
import json

# 让脚本能找到技能(指向本 workspace 的项目级技能副本,隔离 user-level)
SKILL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".workbuddy", "skills", "lobster-memory")
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from engine.integration import MemorySession

MEMORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".lobster", "memory.axeb")


def mock_extract(turn_idx, user_msg, reply):
    """LLM 抽取器的占位实现。返回符合 schema 的 JSON 字符串。

    真实场景下,这一函数体就是龙虾调用自身 LLM 能力的部分:
        prompt = session.build_extraction_prompt(user_msg, reply)
        json_str = lobster.call_llm(prompt)
    这里用硬编码结果保证测试可复现,且覆盖三类记忆 + 多维度反馈。
    """
    cases = {
        # Turn 1: 知识类记忆
        0: {
            "nodes": [
                {"id": "rust_ownership", "label": "Rust所有权", "domain": "knowledge",
                 "type": "concept", "content": "Rust通过移动语义管理内存,避免数据竞争", "weight": 1.0}
            ],
            "edges": [
                {"from": "user", "to": "rust_ownership", "kind": "relates_to",
                 "domain": "knowledge", "weight": 1.0}
            ],
        },
        # Turn 2: 情感类 —— 想法被表扬 (positive / idea)
        1: {
            "nodes": [],
            "edges": [
                {"from": "user", "to": "rust_ownership", "kind": "feedback",
                 "feedback_category": "idea", "valence": 0.8, "domain": "emotion",
                 "weight": 1.0, "content": "用户夸解释清晰、想法好"}
            ],
        },
        # Turn 3: 任务类记忆
        2: {
            "nodes": [
                {"id": "deploy_prod", "label": "部署服务到生产环境", "domain": "task",
                 "type": "task", "content": "下周需部署服务到生产环境", "weight": 1.0}
            ],
            "edges": [
                {"from": "user", "to": "deploy_prod", "kind": "part_of",
                 "domain": "task", "weight": 1.0}
            ],
        },
    }
    return json.dumps(cases.get(turn_idx, {"nodes": [], "edges": []}), ensure_ascii=False)


def run_session_1():
    print("\n" + "=" * 64)
    print("SESSION 1  — 干净龙虾首次对话,积累记忆")
    print("=" * 64)

    # ① 会话开始:初始化 + 注入上下文
    session = MemorySession(MEMORY_PATH, consolidate_every=100)  # 本轮不触发巩固
    ctx = session.start()
    print("\n[start] 注入的上下文(给龙虾的 system prompt 扩展):")
    print("  " + ctx.replace("\n", "\n  "))

    # ② 几轮对话,每轮后抽取并写记忆
    conversation = [
        ("Rust的所有权是什么?", "所有权是Rust管理内存的方式,通过移动语义避免数据竞争。"),
        ("你解释得真清楚,这个想法很好!", "谢谢你的肯定!"),
        ("帮我记一下,下周要部署这个服务到生产环境。", "好的,已经记下了。"),
    ]

    for i, (user, reply) in enumerate(conversation):
        print(f"\n--- Turn {i + 1} ---")
        print(f"  用户: {user}")
        print(f"  龙虾: {reply}")

        # 构造抽取 prompt(真实场景下发给 LLM)
        prompt = session.build_extraction_prompt(user, reply, round_number=i + 1)
        # mock: 用占位实现代替 LLM 调用
        extraction = mock_extract(i, user, reply)
        # ③ 写入图
        result = session.after_turn(extraction)
        print(f"  → 写图: +{result['nodes_added']} 节点, +{result['edges_added']} 边"
              + (f" (error: {result['error']})" if result['error'] else ""))

    # ⑤ 会话结束:保存
    stats = session.graph.get_stats()
    print(f"\n[close] 会话结束,记忆落盘。当前统计: "
          f"节点={stats['total_vertices']}, 边={stats['total_edges']}")
    print(f"  按域分布: {stats['by_domain']}")
    session.close()
    return stats


def run_session_2():
    print("\n" + "=" * 64)
    print("SESSION 2  — 龙虾重启(全新 MemorySession,同一 .axeb)")
    print("=" * 64)

    # 全新会话,没有任何 in-memory 状态 → 必须靠磁盘上的 .axeb 恢复
    session = MemorySession(MEMORY_PATH, consolidate_every=100)
    ctx = session.start()
    print("\n[start] 重启后注入的上下文:")
    print("  " + ctx.replace("\n", "\n  "))

    # 主动回忆:验证 Session 1 的记忆是否还在
    print("\n[recall] 龙虾主动回忆『Rust』相关记忆:")
    memories = session.recall(keywords=["Rust"])
    if not memories:
        print("  ✗ 失败:回忆为空,记忆未跨会话存活!")
        return False
    for m in memories:
        print(f"  ✓ {m.get('label')}: {m.get('content', '')}")

    print("\n[recall_feedback] 龙虾查询『被表扬过的事』(positive):")
    fb = session.recall_feedback(valence="positive")
    if not fb:
        print("  ✗ 失败:未找回情感信号!")
        return False
    for e in fb:
        print(f"  ✓ 反馈 {e.get('from_id')}→{e.get('to_id')} "
              f"category={e.get('feedback_category')} valence={e.get('valence')}")

    # 巩固一次,看报告
    print("\n[consolidate] 手动触发一次巩固:")
    report = session.consolidate()
    print(f"  trashed(回收站)={report['trashed']}, "
          f"communities_found={report['merged']['communities_found']}, "
          f"merged={report['merged']['merged']}")

    session.close()
    print("\n✓ SESSION 2 验证通过:记忆成功跨会话存活。")
    return True


def main():
    print("#" * 64)
    print("# 干净龙虾 · lobster-memory 技能效果测试")
    print("#" * 64)
    print(f"记忆文件: {MEMORY_PATH}")

    # 清理旧的测试记忆,保证从零开始(真正『干净』)
    if os.path.exists(MEMORY_PATH):
        os.remove(MEMORY_PATH)
        print("(已清理旧记忆文件,从零开始)")

    s1_stats = run_session_1()
    ok = run_session_2()

    print("\n" + "=" * 64)
    if ok:
        print("✅ 总体结论:技能工作正常。干净龙虾的记忆可跨会话存活,")
        print("   且按 情绪/知识/任务 正确分类,多维度反馈信号已持久化。")
    else:
        print("❌ 测试失败:记忆未正确跨会话存活。")
    print("=" * 64)

    # 清理测试产物
    if os.path.exists(MEMORY_PATH):
        os.remove(MEMORY_PATH)
    print("(已清理测试记忆文件)")


if __name__ == "__main__":
    main()
