---
name: lobster-memory
description: 基于知识图谱的 AI 长期记忆引擎（实体-关系-情绪 valence），支持自动抽取、因果边、递归自成长抽取与可观察的遗忘巩固。底层由 axolotl_rs (Rust 图存储) 驱动。
version: 0.1.0
author: Sai
triggers:
  - "长期记忆"
  - "图记忆"
  - "记忆图谱"
allowed-tools:
  - Bash
requires:
  python: ">=3.10"
  env: []
---

# lobster-memory — Long-term Graph Memory for Lobster Agents

让龙虾类 AI 助手拥有基于知识图谱的长期记忆系统。记忆按「情绪 / 知识 / 任务」分类存储,带多维度情感信号(表扬/批评),并通过「修剪/合并」信号自我巩固。

## Agent 行为协议（龙虾必须遵守）

本技能提供能力,但是否使用由你(龙虾)自主决定。以下是推荐行为,确保记忆系统真正运转:

### ① 会话开始时（第一件事）
1. 初始化会话,记忆文件放在 workspace 下的 `.lobster/memory.axeb`:
   ```python
   from engine.integration import MemorySession
   session = MemorySession(".lobster/memory.axeb", consolidate_every=20)
   ```
2. 把 `session.start()` 返回的上下文字符串注入到你的 system prompt 中(它包含记忆统计,让你知道自己已经有什么记忆、最近在忙哪类事)。
3. 可简要告知用户:「我已加载长期记忆。」

### ② 每轮对话结束后（必须）
1. 用 `session.build_extraction_prompt(user_msg, reply)` 构造抽取 prompt。
2. **调用你自己的 LLM 能力**执行抽取(你就是抽取器,不需要第二个模型),得到 JSON。
3. `session.after_turn(json)` 写入图。
   - 若本轮无有价值内容,抽取返回空 `{"nodes":[],"edges":[]}`,静默跳过即可。
   - 抽取失败不要报错给用户,记一条 warning 后继续。

### ③ 需要回忆时（你主动判断）
- 遇到相关情境,调用 `session.recall(keywords=[...])` 查询相关记忆。
- 想知道「我之前在哪类事上被批评/被表扬过」,用 `session.recall_feedback(valence="negative" | "positive")`。
- 回忆结果仅供你**参考与偏置自己的判断**,不要机械复述给用户。

### ④ 巩固（学习发生在此）
- `session.should_consolidate(round_number)` 返回 True 时,调用 `session.consolidate()`。
- 可选择把巩固摘要(剪掉了什么、合并了什么群落)简要告诉用户。

### ⑤ 会话结束时
- `session.close()` 保存记忆到磁盘。

## Python 环境

`axolotl_rs`(核心图存储引擎)构建在独立 venv 中。运行记忆相关 Python 时**必须使用这个 venv 的 Python**:

```bash
# 方式一:Bash 中先激活
source ~/.workbuddy/venvs/lobster-memory/bin/activate
python your_script.py

# 方式二:直接指定解释器
~/.workbuddy/venvs/lobster-memory/bin/python your_script.py
```

在 Python 代码里也可以通过 `sys.path` 显式加入技能目录:
```python
import sys
sys.path.insert(0, "/path/to/lobster-memory")  # 含 engine/ 的目录
from engine.integration import MemorySession
```

## 快速接入（三行代码）

```python
from engine.integration import MemorySession

# 1. 会话开始：注入记忆上下文到 system prompt
session = MemorySession(".lobster/memory.axeb", consolidate_every=30)
system_prompt += "\n" + session.start()

# 2. 每轮对话后：抽取关键点 → 写入图
for user_msg in conversation:
    reply = agent.respond(user_msg)
    extraction_prompt = session.build_extraction_prompt(user_msg, reply)
    extraction_json = agent.call_llm(extraction_prompt)  # 复用龙虾自身模型
    result = session.after_turn(extraction_json)
    # result = {"nodes_added": 2, "edges_added": 1, "error": None}

    # 3. 定期巩固
    if session.should_consolidate(round_number):
        report = session.consolidate()
```

## 核心能力

| 路径 | 做什么 | 何时触发 |
|---|---|---|
| **写记忆** | 从对话中提取实体/关系/反馈,写图 | 每轮对话后自动 |
| **回忆** | 按需查询相关记忆 | 龙虾自己判断何时查 |
| **巩固** | 5信号评分 → 留/剪/合并,学习发生在此 | 每 K 轮 或 容量超限 |

## API 速查

```python
session = MemorySession(".lobster/memory.axeb", consolidate_every=30)

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
- axolotl_rs (通过 `install.sh` 自动构建到 `~/.workbuddy/venvs/lobster-memory/`)
