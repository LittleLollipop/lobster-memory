# lobster-memory 详细设计方案 (v3 — 最终版)

> 基于 `lobster-memory-plan.md` 与 2026-07-08/09 的需求讨论 + 独立评估修补而成。
> 定位:让"龙虾"类助手拥有**长期、可自我改进、可分发安装**的图记忆。
> 技术底座 = **axolotl** (`axolotl_rs` Python 绑定 + `.axeb` 持久化)。

---

## 0. 设计哲学(三条独立河流 + 单一数据源)

### 三条河流

- **写路径**(Write):对话 → 抽取关键点 → 写图。
- **回忆路径**(Recall):龙虾主动推理 → 按需查图 → 读回上下文。**纯净**,不携带任何反馈偏置。
- **巩固路径**(Consolidation):信号 → 修剪/合并图。**学习发生在这里**,而非回忆里。

三条路径互不耦合,唯一下游是同一张图。可维护性、可调试性都最好。

⚠️ 唯一的结构性耦合已显式化解:回忆路径收集 `access_count` 更新到**内存日志**,由巩固路径第一步批量 flush 到 axolotl(见 §2.5 / §5.5)。磁盘写入完全归入巩固路径,三条河流在磁盘侧保持独立。

### 单一数据源

**所有记忆数据(节点、边、属性、社群、软删标记)只存 axolotl 的 `.axeb`**,禁止并行 metadata 侧表(JSON/SQLite)。常驻底座统计 = 对 live 图跑聚合查询实时算出,不单独落文件。理由:多数据源必然产生一致性漂移,长期记忆系统不可承受。

### 学习 = 修剪图

通过 `valence` + 四路辅助信号驱动差异化遗忘与巩固。回忆模块保持纯净 → 你担心的"固定筛查锁死思维成长上限"从根本上被消解。

---

## 1. 系统架构总览

```
┌──────────────────────────────────────────────────────────┐
│  龙虾 Agent (WorkBuddy / 任意支持 skill 的实例)            │
│                                                           │
│   ┌─────────────┐   ┌──────────────┐   ┌───────────────┐  │
│   │ 写路径       │   │ 回忆路径      │   │ 巩固路径       │  │
│   │ extractor  │   │ recall (纯) │   │ consolidator │  │
│   └──────┬──────┘   └──┬─────┬─────┘   └──────┬────────┘  │
│          │ 写          │ 查  │内存日志        │ 修剪/合并  │
└──────────┼─────────────┼─────┼───────────────┼───────────┘
           │             │     │               │
           ▼             ▼     │               ▼
   ┌──────────────────────────────────────────────────────┐
   │  lobster_memory 引擎 (Python, 随 skill 分发)           │
   │   - MemoryGraph 封装层                                 │
   │   - 访问日志 _access_log (内存,不下磁盘)                │
   │   - 统计聚合 (count by domain/status)                  │
   │   - 容量安全阀 (软/硬上限)                              │
   └───────────────────────┬──────────────────────────────┘
                           │ PyO3
                           ▼
   ┌──────────────────────────────────────────────────────┐
   │  axolotl_rs  (Rust + PyO3)                             │
   │   - GraphDB: add_vertex / add_edge / walk / find_paths │
   │   - AXEB 持久化 + WAL 恢复                              │
   │   - 增量 PageRank / BFS (供 centrality 信号)            │
   └───────────────────────┬──────────────────────────────┘
                           │ 磁盘
                           ▼
                   memory.axeb  (单一数据源)
```

**分发形态**:skill 文件夹 + 预编译 wheel(优先)或源码构建(回退)。见 §8。

---

## 2. 数据模型 Schema(全存 axolotl)

> 约定:axolotl 的顶点/边必须能挂一个**属性负载(properties dict / JSON bytes)**。
> 见 §7 前置项 #1。所有字段塞进这个负载,无第二份存储。

### 2.1 节点 (Vertex) 属性集

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str | 稳定 id(uuid 或 label 归一化哈希) |
| `label` | str | 可读名,如"用户""需求讨论""挫败感" |
| `domain` | enum | `emotion` / `knowledge` / `task` |
| `type` | enum | `person` / `concept` / `task` / `fact` / `event` / `emotion` |
| `content` | str? | 自由文本摘要(可选) |
| `created_at` | ts | 首次写入时间 |
| `updated_at` | ts | 最近一次触碰时间(写或回忆) |
| `status` | enum | `live` / `trashed`(回收站,见 §5.4) |
| `weight` | float | 累计显著性(被强化/被回忆次数累积) |
| `source` | str? | 出自哪段对话/哪一轮(溯源,可选) |
| `access_count` | int | 被主动回忆并判定"有用"的累计次数(巩固信号;写回机制见 §2.5) |
| `last_accessed` | ts? | 最近一次被回忆的时间(写回机制见 §2.5) |

### 2.2 边 (Edge) 属性集

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str | |
| `from` / `to` | id | 两端节点 id |
| `kind` | enum | `relates_to` / `caused` / `part_of` / `feedback` / `derived` |
| `feedback_category` | enum? | `behavior` / `understanding` / `idea` / `action`(仅 feedback 边) |
| `valence` | float | `-1.0 .. +1.0`,情感信号 → 喂给巩固引擎 |
| `weight` | float | 强化强度/次数(频率信号) |
| `domain` | enum | 可与端点不同(跨域边,如工作任务里夹情绪) |
| `created_at` / `updated_at` | ts | |
| `status` | enum | `live` / `trashed` |
| `access_count` | int | 被回忆并判定有用次数(写回机制见 §2.5) |
| `last_accessed` | ts? | (写回机制见 §2.5) |

### 2.3 四种表情 → (category, valence) 映射

| 描述 | `feedback_category` | `valence` |
|---|---|---|
| 行为不对被批评 | `behavior` | 负(~ -0.7) |
| 理解不对被批评 | `understanding` | 负(~ -0.8) |
| 想法很好被表扬 | `idea` | 正(~ +0.8) |
| 做法被赞同 | `action` | 正(~ +0.6) |

数值是初始建议,可随巩固反馈微调。category 与 valence 解耦,干净可扩展。

### 2.4 社群节点 (community summary,合并产物)

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str | |
| `type` | const | `"community_summary"` |
| `domain` | enum / `"mixed"` | |
| `content` | str | **LLM 生成**的群落摘要(从具体到概念的跃迁) |
| `members` | [id] | 群落内原节点 id(原节点转 `trashed`,此节点为活的抽象) |
| `created_at` | ts | |
| `status` | `live` | |

### 2.5 访问日志(内存暂存,非持久化)

回忆路径不直接写 axolotl,而是写入引擎内存日志,巩固时批量 flush:

```python
# recall.py — 仅在内存,不写磁盘
_access_log: list[tuple[str, float]] = []  # [(vertex_or_edge_id, timestamp), ...]

def recall(query, ...):
    results = _do_query(...)
    for item in results_useful:
        _access_log.append((item.id, time.time()))
    return results
```

巩固路径第一步 flush(见 §5.5):累加 `access_count`,刷新 `last_accessed`。**效果**:回忆路径在代码上是纯读 + 内存追加;磁盘写入完全归入巩固路径。三条河流保持独立。代价:会话崩溃丢失当次访问日志(轻于丢记忆数据;可加会话结束 hook 仅 flush 兜底)。

### 2.6 常驻底座(统计层,不单独存)

底座是引擎对 live 图跑聚合查询的结果,每次会话开始注入:

```json
{
  "total_vertices": 1234,
  "total_edges": 2103,
  "by_domain": { "emotion": 310, "knowledge": 720, "task": 204 },
  "by_status": { "live": 1180, "trashed": 54 },
  "active_7d": { "emotion": 12, "knowledge": 88, "task": 30 },
  "caps": { "soft": "11%", "hard": "7%" },
  "generated_at": "2026-07-09T22:50:00+08:00"
}
```

纯统计数字,不漏具体情节记忆。`caps` 字段是容量使用率,触发容量安全阀时龙虾可见(§5.6)。

---

## 3. 写路径 (Write Path)

### 3.1 触发

- **每次对话轮次结束时**,龙虾跑一次抽取——抽取的是**本轮拆解出的关键点**,不是原始对话原文。
- 粒度:以"一轮(用户消息 + 助手回复)"为单位。
- 经济性:仅当本轮确实出现可记的要点/反馈/事实时才写;纯寒暄不写。

### 3.2 抽取器 (`extractor`) — 三层防御

#### 第 1 层:结构化抽取 prompt

注入到龙虾推理上下文(零额外 LLM 调用,复用龙虾自身模型):

````markdown
[记忆抽取指令 — 每轮对话结束后执行]

你需要在刚结束的这一轮对话中提取"值得记入长期记忆的关键点"。
只提取有实质信息的内容,纯寒暄/确认/打招呼不提取。

输出严格按以下 JSON 格式,不要多余文本:

{
  "nodes": [
    {
      "id": "稳定标识符(英文/拼音,避免空格)",
      "label": "可读名",
      "domain": "emotion|knowledge|task",
      "type": "person|concept|task|fact|event|emotion",
      "content": "摘要(可选)",
      "weight": 1.0
    }
  ],
  "edges": [
    {
      "from": "已存在或本批次的节点id",
      "to": "已存在或本批次的节点id",
      "kind": "relates_to|caused|part_of|feedback|derived",
      "weight": 1.0,
      "feedback_category": "behavior|understanding|idea|action  (仅kind=feedback)",
      "valence": 0.0,
      "domain": "emotion|knowledge|task"
    }
  ]
}

提取规则:
1. 识别本轮出现的实体/事件/判断 → node(domain/type 分类)
2. 识别关系 → edge(kind=非 feedback 类型)
3. 识别"用户批评/表扬了龙虾的某方面" → edge(kind=feedback,填 feedback_category + valence)
   - 批评 = 负值(建议 -0.6~-0.8),表扬 = 正值(建议 +0.6~+0.8)
4. 新旧合并:如果本轮的节点和图中已有节点是同一实体,用已有 id,不要新建
5. 同义词归一化:"张三"和"张总"如果是同一人,用同一个 id
6. domain:情绪的归 emotion,知识/技术的归 knowledge,工作任务的归 task
7. 如果本轮没有值得记的内容,输出 {"nodes": [], "edges": []}

---
[图中已有节点参考 — 帮助判断新旧合并]
{existing_node_labels}
---
````

`{existing_node_labels}` 是抽取前从 axolotl 查出的最近 N 个 live 节点的 `id: label` 列表(N 建议 200)。这是连接抽取器和图的**关键桥梁**,解决"不知道图里已经有什么"的归一并问题。

#### 第 2 层:输出校验

```python
def validate_extraction(raw_json: dict) -> dict:
    """抽取器输出校验,不通过则拒绝整批写入"""
    valid_domains = {"emotion", "knowledge", "task"}
    valid_types = {"person", "concept", "task", "fact", "event", "emotion"}
    valid_kinds = {"relates_to", "caused", "part_of", "feedback", "derived"}
    valid_feedback = {"behavior", "understanding", "idea", "action"}

    for node in raw_json.get("nodes", []):
        assert "id" in node and "label" in node, f"missing id/label: {node}"
        assert node.get("domain") in valid_domains, f"invalid domain: {node}"
        assert node.get("type") in valid_types, f"invalid type: {node}"
        node.setdefault("weight", 1.0)
        node.setdefault("content", None)

    for edge in raw_json.get("edges", []):
        assert "from" in edge and "to" in edge, f"missing from/to: {edge}"
        assert edge.get("kind") in valid_kinds, f"invalid kind: {edge}"
        if edge["kind"] == "feedback":
            assert edge.get("feedback_category") in valid_feedback
            assert -1.0 <= edge.get("valence", 0) <= 1.0
        edge.setdefault("weight", 1.0)
        edge.setdefault("domain", "knowledge")

    return raw_json
```

校验失败 → 抛弃本轮抽取,记录 warning 到引擎日志,**不阻塞龙虾回复**。

#### 第 3 层:去噪后处理

```python
def deduplicate_extraction(extracted, existing_labels: dict[str, str]):
    """标准化归并,避免重复建节点"""
    # 1. 本批内去重:同 label → 同一 id
    # 2. 与已有节点去重:label 模糊匹配(编辑距离<2 或 substring) → 复用已有 id
    # 3. 空批次跳过(无 nodes 且无 edges)
    ...
```

### 3.3 写图封装层 (`MemoryGraph`)

薄封装,把 schema 字段映射进 axolotl 的 `add_vertex(label, props)` / `add_edge(from, to, props)`。提供语义化入口:`remember_fact` / `remember_relation` / `remember_feedback`。

**幂等/去重**(规避未修的 dedupe bug):
- 写前先 `get_edge`/`get_vertex` 查是否已存在(按 id/归一化 label);
- 已存在 → 更新属性(累加 `weight`、刷新 `updated_at`),**不新增重复拓扑**;
- 与软删机制兼容:无论 status 是 live 还是 trashed,始终更新已有实体。

---

## 4. 回忆路径 (Recall Path) — 纯净、按需

### 4.1 功能告示(注入龙虾 system prompt)

不塞具体记忆,只告知存在一个可主动查询的数据库:

```
[记忆系统]
你拥有一个长期图记忆系统(lobster-memory)。它记录了:
- 我们讨论过的知识/技术话题
- 你的行为被表扬或批评的经历
- 正在进行的工作任务

这不是自动注入的内容,而是一个你可以**主动查询**的数据库。
当你需要回忆相关事实、上下文、或过去的反馈时,使用 /lobster recall <查询>。
```

### 4.2 回忆接口

- **接口**:`recall(query_keywords, filters={domain, type, status:live})` → 返回子图/路径。
- **访问记录**:返回结果中被判"有用"的实体/边 id 记入 `_access_log`(§2.5),不在本次磁盘写。
- **硬性规则**:
  - 查询层**一律过滤 `status=trashed`**;
  - 不把任何筛选算法的品味固化进注入内容;
  - 返回结果如何被龙虾使用,完全由龙虾自主决定。

### 4.3 追溯钩子(可选)

巩固路径跑完后,输出最近社群摘要标题作为 hints。**不强制注入**,龙虾可忽略:

```
[最近记忆变化] 上次巩固新增 1 个社群摘要:"用户对 Rust 异步编程的理解进展"
```

---

## 5. 巩固路径 (Consolidation Path) — 学习发生处

### 5.1 触发机制

| 触发器 | 条件 |
|---|---|
| **周期性** | 每 K 轮对话自动触发(K 可配,建议 20–50) |
| **手动** | 用户执行 `/lobster consolidate` |
| **软上限** | 顶点 > 15K 或 边 > 40K → 跳过 K 轮等待,立即触发 |
| **硬上限** | 顶点 > 25K 或 边 > 60K → 立即触发 + 激进模式(见 §5.6) |

### 5.2 信号集(喂给评分)

| 信号 | 来源 | 对"留/剪"的影响 |
|---|---|---|
| `valence` | feedback 边 | 正→留,负→倾向剪 |
| `frequency` | 边/节点 `weight`(被强化次数) | 高→留(已成定见) |
| `recency` | `updated_at` / `last_accessed` | 旧且弱→剪 |
| `access` | `access_count`(被回忆判有用次数) | 高→留(实用记忆) |
| `centrality` | **axolotl 增量 PageRank**(现成复用) | 高→枢纽,留 |

### 5.3 标准化方法

所有信号过 **min-max 归一化**到 0~1,范围取当前 live 图的实际分布:

```python
def normalize(values: list[float]) -> list[float]:
    vmin, vmax = min(values), max(values)
    if vmax == vmin:
        return [0.5] * len(values)
    return [(v - vmin) / (vmax - vmin) for v in values]
```

| 信号 | raw 范围 | 归一化 |
|---|---|---|
| valence | -1..+1 | `(v+1)/2` (负=0,正=1) |
| frequency (weight) | 1..N | min-max |
| recency | 距今天数 | `1 - days/max_days` (越近越高) |
| access_count | 0..N | min-max |
| centrality | 0..~1 | 本身接近 0~1,可直接用 |

### 5.4 评分 → 三档动作(个体评分)

```
score = w1·valence_norm + w2·freq_norm + w3·recency_norm
      + w4·access_norm + w5·centrality_norm
```

| 档位 | 条件 | 动作 |
|---|---|---|
| 巩固 | `score > 0.6` | 保留;激活的节点参与后续社群检测 |
| 保留 | `0.3 ≤ score ≤ 0.6` | 保留 |
| 软删除 | `score < 0.3` | 置 `status=trashed`(回收站,不删拓扑,可恢复) |

权重为初始建议,需实测调参。

### 5.5 完整 Consolidation 流程

```
consolidation():
  1. [flush]   将 _access_log 批量写入 axolotl
               (累加 access_count, 刷新 last_accessed)
  2. [score]   对每个 live 节点/边计算归一化评分 → 三档动作
  3. [prune]   低分 → status=trashed
  4. [detect]  对 live 子图跑 community detection (连通分量/label propagation)
  5. [merge]   对每个检测到的群落:
               community_score = density × avg(member_score)
               if community_score > COMMUNITY_MERGE_THRESHOLD:
                  LLM 生成摘要节点,原成员 → trashed
  6. [report]  输出 consolidation_report (§11.2)
```

社群合并决策与个体评分**完全分离**(步骤 2/3 vs 步骤 4/5),高评分节点不"绑架"合并。

`COMMUNITY_MERGE_THRESHOLD` 初始建议 0.5,可调。

### 5.6 容量安全阀(两级)

```python
# schema.py
MEMORY_CAPS = {
    "soft_vertex": 15000,  "soft_edge":  40000,
    "hard_vertex": 25000,  "hard_edge":  60000,
    "panic_threshold_ratio": 0.5,  # 硬上限时 soft-delete 阈值提升到此
}
```

| 级别 | 触发条件 | 效果 |
|---|---|---|
| 软上限 | > 15K 顶点 或 > 40K 边 | 跳过 K 轮周期,立即 consolidate |
| 硬上限 | > 25K 顶点 或 > 60K 边 | 立即 consolidate + `panic_threshold`(阈值从 0.3 提到 0.5) + 全量社群合并 + 若仍超限按 score 升序强制 trash 至软上限以下 |

---

## 6. LLM 依赖

- **不单独配 LLM**。抽取、回忆决策、社群摘要,全部复用**龙虾环境自身模型**。
- 好处:零配置、零 key → "所有龙虾开箱即用"。
- 后续若有人想要更强抽取,留 env 覆盖入口即可。

---

## 7. axolotl 前置项(需支持/扩展,用户已认领)

| # | 前置项 | 必要性 | 说明 |
|---|---|---|---|
| 1 | **顶点/边支持任意属性负载** | 必须 | `add_vertex` / `add_edge` 接受 properties dict → 映射到 §2 全部字段。若当前不支持,需扩展 axolotl。 |
| 2 | **WAL + AXEB 持久化** | 已有 | 确认 `from_file_or_new` / `save` 正确重放含属性的图。 |
| 3 | **查询层过滤 `status`** | 引擎侧做 | `walk/bfs` 遍历邻接 block、不懂 `status`;过滤在 Python 层(`recall`/`consolidation` 拿到结果后再滤 `trashed`)。个人规模开销可忽略。 |
| 4 | (暂不动) dedupe bug | 搁置 | §3.3 幂等写规避;软删除机制顺带绕开(不调 `delete_edge`)。 |

---

## 8. 技能封装与分发 (Skill Packaging)

### 8.1 目录结构

```
lobster-memory/
├── SKILL.md                 # 触发词、能力说明、调用约定
├── engine/
│   ├── __init__.py
│   ├── memory_graph.py      # MemoryGraph 封装层
│   ├── extractor.py         # 抽取器(§3.2 prompt + 校验 + 去重)
│   ├── recall.py            # 回忆查询 + _access_log
│   ├── consolidator.py      # 巩固引擎(§5.5 完整流程)
│   ├── base.py              # 常驻底座统计聚合
│   └── schema.py            # §2 字段常量 + 校验 + MEMORY_CAPS
├── install.sh               # wheel 优先 / maturin 回退(§8.3)
└── requirements.txt
```

### 8.2 SKILL.md 要点

- 描述:为龙虾提供长期图记忆(写/回忆/巩固)。
- 触发:对话中自然调用;或显式 `/lobster remember|recall|consolidate`。
- 约束:所有数据经 `MemoryGraph` 落 axolotl `.axeb`;回忆不固定注入;巩固按需/定时。

### 8.3 安装脚本(预编译 wheel 优先 + 源码构建回退)

Phase 0 验证 axolotl 属性支持后,**并行构建 wheel**(maturin + GitHub Actions,一次配置自动发布):

```
Phase 0a: axolotl 顶点属性验证 + MemoryGraph 薄封装
Phase 0b (并行): 构建预编译 wheel
  - macOS arm64 (Apple Silicon)  ← 覆盖大多数 Mac
  - Linux x86-64                  ← 覆盖服务器/CI
```

安装脚本两路:

```bash
#!/bin/bash
# install.sh — lobster-memory v1
if pip install lobster_memory-*.whl 2>/dev/null; then
    echo "✓ Installed from prebuilt wheel"
elif command -v cargo &>/dev/null; then
    echo "Building from source..."
    maturin develop --release
    pip install -r requirements.txt
else
    echo "请安装 Rust: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
    exit 1
fi
```

### 8.4 多龙虾可安装

每人一个独立 `.axeb`(默认 `~/.lobster/<agent_id>/memory.axeb`)→ 天然隔离。skill 文件夹 + 安装脚本可直接分享。

---

## 9. 分阶段实现路线图

| Phase | 内容 | 产出 |
|---|---|---|
| **0a** | axolotl 顶点属性验证 + MemoryGraph 薄封装 | 读写带属性的节点/边,`.axeb` 可重开 |
| **0b** | (并行) 预编译 wheel (macOS arm64 + Linux x86-64) | 安装脚本可两路分发 |
| **1** | 写路径:extractor(prompt + 校验 + 去重) + 幂等写 | 每轮拆解关键点写入图 |
| **2** | 回忆路径:recall + _access_log + 查询层 trashed 过滤 + 功能告示 | 龙虾按需查图 |
| **3** | 巩固路径:信号评分 + 归一化 + 软删回收站 + 社群合并 + 容量安全阀 | 图随对话自我修剪/抽象 |
| **4** | 常驻底座 + skill 封装(SKILL.md + install.sh + 统计聚合) | 可分发技能包 |
| **5** | 发布:wheel 上传 PyPI / 分享安装脚本 | 开箱即用 |

---

## 10. 待拍板 / 开放问题

1. **K 默认值**:每多少轮主动巩固?(建议 20–50)
2. **社群检测算法**:v1 连通分量/label propagation 是否认可?
3. **巩固评分权重**:§5.4 初始权重 w1~w5 是否需要预设具体值?
4. **抽取器 few-shot 示例**:是否需要在 prompt 里加入具体对话→JSON 的样本?(建议至少 2 条,一条含 feedback,一条不含)
5. **axolotl `add_vertex` 属性支持的最终形态** — 以你扩展后的实现为准。

---

## 11. 交叉关切(错误处理 / 质量评估 / 容量)

### 11.1 错误处理

| 场景 | 策略 |
|---|---|
| 抽取器返回非法 JSON | 抛弃本轮抽取,不写图。engine log warning。**不阻塞龙虾回复** |
| axolotl write 失败(磁盘满/I/O) | 重试 1 次,仍失败 → 抛弃本轮写入,error log。**不阻塞龙虾回复** |
| recall 返回空结果 | 正常。输出空,龙虾自行决定 |
| consolidation 中途崩溃 | 所有操作为幂等 check-then-write,重跑安全 |
| .axeb 损坏 | WAL 重放恢复;若也损坏 → 从 `memory.axeb.bak` 恢复(每次 save 后 cp) |
| 会话崩溃(访问日志丢失) | 仅丢失当次 access_count 增量。可加会话结束 hook 仅 flush 兜底 |

### 11.2 质量报告

每次 consolidation 后输出,供 human review:

```python
consolidation_report = {
    "before":     {"vertices": N, "edges": M},
    "after":      {"vertices": n, "edges": m},
    "trashed":    count,
    "merged":     {"communities_found": c, "merged": k},
    "cap_level":  "normal" | "soft" | "hard",
    "top_kept":   [{"id": "...", "score": 0.92}, ...],
    "top_trashed":[{"id": "...", "score": 0.12}, ...],
}
```

人肉检查 top 5 kept/trashed 是迭代信号权重的唯一可信反馈通道。

### 11.3 容量约束

见 §5.6 两级安全阀。`MEMORY_CAPS` 参数可在 `schema.py` 调整。底座统计(§2.6)的 `caps` 字段反馈当前使用率。

---

*本方案为 v3 最终版,可直接作为编码起点。请就 §10 各项拍板后进入 Phase 0a。*
