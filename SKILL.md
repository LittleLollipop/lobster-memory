# lobster-memory — Long-term Graph Memory for Lobster Agents

让龙虾类 AI 助手拥有基于知识图谱的长期记忆系统。

## 快速接入（龙虾 Agent 三行代码）

```python
from lobster_memory import MemorySession

# 1. 会话开始：注入记忆上下文到 system prompt
session = MemorySession("memory.axeb", consolidate_every=30)
system_prompt += "\n" + session.start()

# 2. 每轮对话后：抽取关键点 → 写入图
for user_msg in conversation:
    reply = agent.respond(user_msg)

    # 构造抽取 prompt,由龙虾自己的 LLM 执行抽取
    extraction_prompt = session.build_extraction_prompt(user_msg, reply)
    extraction_json = agent.call_llm(extraction_prompt)  # 复用龙虾自身模型

    # 处理抽取结果
    result = session.after_turn(extraction_json)
    # result = {"nodes_added": 2, "edges_added": 1, "error": None}

    # 3. 定期巩固
    if session.should_consolidate(round_number):
        report = session.consolidate()
        # 可选:把巩固摘要告诉用户
```

## 核心能力

| 路径 | 做什么 | 何时触发 |
|---|---|---|
| **写记忆** | 从对话中提取实体/关系/反馈,写图 | 每轮对话后自动 |
| **回忆** | 按需查询相关记忆 | 龙虾自己判断何时查 |
| **巩固** | 5信号评分 → 留/剪/合并,学习发生在此 | 每 K 轮 或 容量超限 |

## API 速查

```python
session = MemorySession("memory.axeb", consolidate_every=30)

# 生命周期
ctx = session.start()                       # → system prompt 扩展字符串
prompt = session.build_extraction_prompt(   # → 抽取 prompt(发给 LLM)
    user_msg="用户说了什么",
    assistant_reply="你回复了什么",
)
result = session.after_turn(llm_output)     # → {"nodes_added": N, "edges_added": M}

# 记忆查询
memories = session.recall(keywords=["Rust"])
fb = session.recall_feedback(valence="negative")  # 查历史批评

# 巩固
if session.should_consolidate(round_number):
    report = session.consolidate()
    # report: {"before": {...}, "after": {...}, "trashed": N, "merged": {...}}

session.close()  # 保存 + 关闭
```

## 文件结构

```
lobster-memory/
├── SKILL.md              ← 你正在看的
├── install.sh            ← 一键安装(wheel 优先)
├── engine/
│   ├── integration.py    ← MemorySession(接入层,从这里开始)
│   ├── base.py           ← LobsterMemory(底层 API)
│   ├── memory_graph.py   ← axolotl 封装(CRUD/PageRank/BFS)
│   ├── extractor.py      ← 抽取 prompt + 校验 + 去重
│   ├── recall.py         ← 回忆接口 + 访问日志
│   ├── consolidator.py   ← 巩固引擎(6步流水线)
│   └── schema.py         ← 常量/枚举/容量参数
```

## 依赖

- Python 3.10+
- axolotl_rs (通过 `install.sh` 自动构建)
