# 龙虾长期记忆系统 — 基于 Axolotl GraphDB 的技术方案

## 1. 规模估算 & 性能验证

| 维度 | 个人长期记忆 | Axolotl 实测承载 | 结论 |
|------|:---------:|:-------------:|:----:|
| 实体数 | 5K–20K | 10K (测试), 100K+ (设计) | ✅ 充足 |
| 关系数 | 10K–50K | 50K (测试), ~2M (设计) | ✅ 充足 |
| 单次查询 (walk 3-hop) | < 10ms | 0.2ms | ✅ 15x 余量 |
| BFS 全图遍历 | < 100ms | 40ms | ✅ 够用 |
| 增量更新 | 每次对话 +5-20 条边 | 增量 BFS 1μs/edge | ✅ 即时 |
| PageRank 重算 | 后台异步 | 1.2s @10K | ✅ 可接受 |

**结论：当前能力完全覆盖，无需优化。**

## 2. 架构设计

```
对话历史 (markdown/JSON)
        │
        ▼
┌───────────────────┐
│  记忆提取器 (LLM)  │  ← 从对话中提取 (实体, 关系, 属性)
└───────┬───────────┘
        │
        ▼
┌───────────────────┐
│  Axolotl GraphDB  │  ← 知识图谱存储 (本方案核心)
│  ├─ 实体/关系 CRUD │
│  ├─ PageRank 评分  │
│  ├─ 图遍历/子图     │
│  └─ 持久化 (.axeb) │
└───────┬───────────┘
        │
        ▼
┌───────────────────┐
│  记忆检索引擎       │  ← 查询 + 排序
│  ├─ 图查询 (walk)  │
│  ├─ PageRank 排序  │
│  ├─ 时间衰减       │
│  └─ 向量搜索(可选)  │
└───────┬───────────┘
        │
        ▼
┌───────────────────┐
│  上下文注入器       │  ← 拼接到 LLM prompt
└───────────────────┘
```

## 3. 知识图谱 Schema

### 3.1 节点类型

| 类型 | 属性 | 示例 |
|------|------|------|
| `person` | name, role, last_seen | "张三", "同事/Android开发" |
| `topic` | name, category, depth | "Rust所有权", "编程语言/中级" |
| `project` | name, status, deadline | "龙虾记忆系统", "进行中" |
| `fact` | content, source, confidence | "用户喜欢简短的回复", 0.9 |
| `conversation` | date, summary | "2026-07-07 讨论图数据库" |
| `file` | path, type, summary | "~/docs/plan.md" |

### 3.2 边类型

| 关系 | 权重 | 含义 |
|------|:---:|------|
| `KNOWS` | 0.8 | 认识某人 |
| `DISCUSSED` | 1.0 | 讨论过某话题 (权重=讨论次数) |
| `WORKED_ON` | 1.0 | 参与过某项目 |
| `RELATED_TO` | 0.5 | 话题关联 |
| `MENTIONED_IN` | 1.0 | 出现在某对话中 |
| `REFERENCED` | 0.7 | 用户引用过 |
| `PREFERS` | 0.9 | 用户偏好（高权重不衰减） |

### 3.3 时间衰减权重

```
edge_weight = base_weight × e^(-λ × days_since_last_access)

λ 参数:
  KNOWS:         0.001  (人际关系几乎不衰减)
  DISCUSSED:     0.01   (讨论话题缓慢衰减)
  RELATED_TO:    0.02   (关联关系中等衰减)
  MENTIONED_IN:  0.05   (提及关系快速衰减)
  PREFERS:       0      (偏好不衰减)
```

## 4. 核心 API (Rust → Python)

```python
import axolotl_rs

class MemoryGraph:
    def __init__(self, data_path="memory.axeb"):
        self.g = axolotl_rs.AxolotlGraph.open(data_path)

    # ── 写入 ──

    def remember_entity(self, eid, etype, **props):
        """添加/更新实体"""
        self.g.add_vertex(eid, {"type": etype, **props})

    def remember_relation(self, from_e, to_e, rel_type, weight=1.0):
        """添加/更新关系（自动增量权重）"""
        existing = self.g.get_edge(from_e, to_e)
        if existing:
            new_weight = existing[0] + weight
        self.g.add_edge(from_e, to_e, new_weight, {"type": rel_type})

    def forget_entity(self, eid):
        """删除实体"""
        self.g.delete_vertex(eid)

    # ── 检索 ──

    def recall(self, topic_id, depth=2, n=10):
        """回忆：从话题出发，BFS 提取相关记忆"""
        visited = self.g.walk(topic_id, depth)
        # 按 PageRank 排序返回 top-n
        ...

    def find_connections(self, a, b, max_hops=3):
        """发现 A 和 B 之间的连接路径"""
        return self.g.find_paths(max_hops, max_results=5)

    def recent_topics(self, days=7):
        """最近讨论的话题"""
        ...

    # ── 维护 ──

    def consolidate(self):
        """记忆巩固：重新计算 PageRank，清理低权重边"""
        ...

    def apply_decay(self):
        """应用时间衰减到所有边"""
        ...

    def save(self):
        self.g.save()
```

## 5. 上下文注入流程

```
用户说："上次张三提到的那个 Rust 库叫什么来着？"

Step 1: 关键词提取 → person=张三, topic=Rust
Step 2: 图查询:
         find_connections(张三, Rust) → 
          张三 --DISCUSSED(3次)--> Rust所有权
          张三 --MENTIONED_IN--> "2026-07-03对话"
          "2026-07-03对话" --MENTIONED_IN--> "Tokio异步运行时"
Step 3: 上下文注入 LLM prompt:
         [记忆] 张三在 7月3日 讨论过 Rust 所有权 (3次)
         [记忆] 张三在 7月3日 提到过 Tokio 异步运行时
Step 4: LLM 回答 → "张三提到的是 Tokio 异步运行时库"
```

## 6. 对话 → 知识图谱 的自动提取 (LLM Pipeline)

```
每次对话结束后:

1. 摘要提取 (LLM)
   Input: 对话全文
   Output: {entities: [{name, type, properties}],
            relations: [{from, to, type, weight}]}

2. 图写入 (10-50ms per conversation)
   for entity in entities:
       memory.remember_entity(hash(entity.name), entity.type, ...)
   for rel in relations:
       memory.remember_relation(hash(rel.from), hash(rel.to), rel.type)

3. 后台任务 (每分钟/每10条新边)
   - apply_decay()       # 时间衰减
   - consolidate()       # PageRank 重算
   - save()              # 持久化
```

## 7. 项目结构建议

```
lobster-memory/
├── Cargo.toml              # Rust 依赖 (axolotl-rs)
├── pyproject.toml           # Python 项目配置
├── src/
│   ├── memory_graph.py      # MemoryGraph 封装 (本文第4节)
│   ├── extractor.py         # LLM 实体关系提取器 (本文第6节)
│   ├── retriever.py         # 记忆检索 + 排序
│   ├── decay.py             # 时间衰减策略
│   ├── injector.py          # Prompt 上下文注入
│   └── cli.py               # 命令行工具
├── tests/
├── data/
│   └── memory.axeb          # 持久化文件
└── README.md
```

## 8. 实现路线 (3天)

| 天 | 任务 | 产出 |
|:---:|------|------|
| **Day 1** | MemoryGraph 封装 + CRUD 测试 | `pip install lobster-memory` 可用 |
| **Day 2** | LLM 提取器 + 上下文注入 | 端到端：对话→图→检索→注入 |
| **Day 3** | 时间衰减 + 记忆巩固 + 文档 | 完整可运行原型 |

## 9. 注意事项

- **不做向量搜索**：知识图谱检索本身足够。向量搜索是额外维度，可后续叠加。
- **LLM 提取用轻量模型**：本地 7B 模型或 API 小模型 (gpt-4o-mini) 足够。
- **持久化依靠 Axolotl**：`.axeb` 文件 + WAL 恢复，无需额外存储层。
- **多用户隔离**：每个用户一个 `.axeb` 文件，或一个图内用 `user_id` 过滤。
