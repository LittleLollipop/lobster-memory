---
name: lobster-memory
description: 基于知识图谱的 AI 长期记忆引擎（实体-关系-情绪 valence），支持自动抽取、因果边、递归自成长抽取与可观察的遗忘巩固。底层由 axolotl_rs (Rust 图存储) 驱动。
version: 0.2.3
author: Sai
triggers:
  - "长期记忆"
  - "图记忆"
  - "记忆图谱"
allowed-tools:
  - Bash
requires:
  python: ">=3.10"
  platform: "darwin-arm64"   # 仅支持 Apple Silicon (M 系列)
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

### ⑥ 数据读取与一致性校验（查询驱动，必须）
> 图数据库的核心价值=**通过边做定向遍历、字段级提取、只输出差异**；图库不是存储桶，禁「全量 dump 到上下文人工比对」（注意力分散+图库沦为存储，且节点上千后必爆上下文）。

1. **读取 = 定向查询**：从目标节点沿特定类型边取邻居（如 chXXX → unit/chr/jk 出边），字段用正则/属性提取（如只取【时间线】【衔接】【登场】），不拉整段 content、不 dump 全库。
2. **一致性校验 = 写校验器跑规则**，不是人肉读库：把「冲突模式」转成结构化查询（ch→单元映射 vs 总规划、plan 衔接链、角色登场 vs 引用章、时间线衔接词等），校验器只取需要字段、只报差异，几百章/几千节点都秒级。
3. **字段语义全局统一**：同名字段不同时期写法可能语义不同（如 plan「衔接chXXX」早期=预告下章、后期=承接上章）→ 统一语义，让校验器兜底检测。
4. 判定标准：能写成查询的校验，不靠人读；人工只处理查询结果（差异清单），不读原始数据。

### ⑦ 设定下沉纪律（治「上热下冷」，**硬纪律**）
> **病状**：设定层（rule_* / chr_* / concept_* 等真相源节点）改对了，章纲层（chXXX）还是旧口径。
> 典型病例：`chr_shadow` 已写「她不是挡路石头，是宣王的认真开关」，`ch037` 仍写「挡路被碾的石头」；
> `rule_shadow_mechanism` 已禁「影子调兵」，`ch063/ch070` 仍写「兵变」。
> **成因**：改设定时靠记忆去想「哪些章会受影响」——必漏。清单必须由工具算，不能靠想。

1. **每次改动设定层节点，必须先跑 `impact` 列出受影响章号清单，并把清单贴给用户**——不是建议，是纪律。没跑 impact 就报「已修复」，视为未完成。
2. **清单分两组**：A 组=边可达章节（必须逐章核对）；B 组=内容关键词命中但**无边相连**的章节（最易漏，重点看——上面的 `ch063/ch070` 就只在 B 组）。
3. **逐章改完必须跑 `sink-check` 验收**（旧措辞 → 新口径）：`--stale` 传应被替换的旧词，`--ok` 传新口径关键词。**exit 0 才算完成**；非零即仍有章未下沉，继续改。
4. 禁止「改完设定层即报完成」。设定层正确 ≠ 章纲正确，下沉没验收等于没改。
5. 新增红线/改名/改口径时，把新旧措辞一并写进 `rule_*` 节点，方便日后 `sink-check` 直接取词。
6. **`sink-check` 报 FAIL 时，先判断是内容错还是工具误报，再动手**：
   - 章纲里大量【机制·禁写「X」】式声明，旧词常以**否定语境**出现（「她**不是**挡路被碾的石头」、
     「**禁写**「影子调兵／影子下令／影子发动**兵变**」」）。工具已内置否定语境识别，
     会把这类归入「⚠️ 已纠正·否定语境引用」bucket（不计 FAIL，但仍列出供人工确认）。
   - 若判定为工具误报，**修工具，不要绕过它**（加关键词白名单、手动放行、假装没看见都算绕过）。
     理由：工具哭狼一次，下次人就会跳过它——纪律从此作废。误报率是纪律的命门。
   - 修完 `_negated` 必须跑 `selftest`（内置 18 条回归用例，全绿才可继续用它验收）。
7. **采纳考据报告时，报告给了「推荐说／主流说」的，落库不得自行改采异说**；确有理由改采，必须在节点里写明「报告推荐 X，本书采 Y，理由 Z」。
   - **实证（血案）**：考据 B-3 齐量制，报告明写「推荐取学界主流杜预—陈冬生说：1豆=5升、1区=20升、1釜=80升、1钟=800升，进位仍四进」，落库时却看原稿「五进制混合」字样扎眼，改成吴则虞五进制说（25/125/1250），还自造按语「原稿误算」——**原稿 20/80/800 本就是正解**，白改一轮、多花两轮才纠回。
   - **推论**：改考据节点前**必须回读报告原文那一说**，不能凭「看着矛盾就改」。看着矛盾往往是**旧稿把两说并置**没写清，正解是「二选一并写清」，不是换一个。
8. **设定层的指代用词会渗进正文**——分层/改名的口径，要在设定层节点本身就用对，不能只改章纲。
   - 写正文时人会直接抄设定节点的表述；设定层用了禁用词，正文一抄就破功，且章纲 `sink-check` 抓不到（章节点里根本没出现）。
   - **实证**：「钟无艳只属民间戏语层」定下后，4 个高权重规则节点（`rule_e2_timeline` / `rule_sida_decoupled` / `concept_sida` / `concept_lan_vs_qing`）仍用「钟无艳」指代主角。这些是反复被引用的节点，风险比章纲还高。
   - **查法**：定下分层口径后，反查**全部非章节点**里该词的每次出现，按「俗话原文引用／书名／作品名／民间层描述／红线说明本身」归类，剩下的**纯指代**一律改成正文口径。

```bash
# 1) 影响面：我这次改动会波及哪些章
$PY tools/graph_crud.py impact chr_shadow --kw 兵变 --kw 挡路 --kw 石头 --kw 认真开关
# 2) 逐章改（走 bulk），改完验收：旧词是否清干净、新词是否已下沉
$PY tools/graph_crud.py sink-check --stale 兵变 --stale 挡路被碾的石头 --ok 造条件 --ok 认真开关
#    → 结论 PASS (exit 0) 才算收工；FAIL (exit 1) 继续改
#    → 三 bucket：❌仍写旧口径(必须改) / ⚠️已纠正·否定语境引用(人工确认) / ✅已写新口径
# 3) 工具自检（改过 _negated / NEG_CUES 后必跑，不需要图库）
$PY tools/graph_crud.py selftest
```

> **否定语境判定边界 = 小句，不是固定字数**（踩坑修正）：
> 「禁写「影子调兵／影子下令／影子发动**兵变**」」里否定词与旧词相隔十余字，
> 固定 window 永远够不到；而 window 一旦放大到跨句，又会把下一句一次独立的出现误判成否定。
> 取「旧词之前最近句读之后」作边界，两个问题一起解决。

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
├── tools/
│   └── graph_crud.py     ← 图库通用 CLI(唯一写入口,见下)
├── engine/
│   ├── integration.py    ← MemorySession(接入层,从这里开始)
│   ├── base.py           ← LobsterMemory(底层 API)
│   ├── memory_graph.py   ← axolotl 封装(CRUD/PageRank/BFS)
│   ├── extractor.py      ← 抽取 prompt + 校验 + 去重
│   ├── recall.py         ← 回忆接口 + 访问日志
│   ├── consolidator.py   ← 巩固引擎(6步流水线)
│   └── schema.py         ← 常量/枚举/容量参数
```

## 图库 CLI 工具（`tools/graph_crud.py`）

所有图库增删改查只走这**一个入口**，禁止再为某个图库单独写临时 `.py`。

```bash
# 必须在 lobster-memory venv 下运行
PY=~/.workbuddy/venvs/lobster-memory/bin/python

# 默认库 = 调用时所在项目的 .memory-graph/memory.axeb；
# 也可用 --db <path> 显式指定任意库，或设环境变量 LOBSTER_DB。
# 项目里推荐把 tools/graph_crud.py 软链到本文件，避免多份副本漂移。

$PY tools/graph_crud.py scan-dups                 # 重边体检(全图 0 平行边才算健康)
$PY tools/graph_crud.py get <id>                  # 查节点 + 出/入边(kind/weight)
$PY tools/graph_crud.py upsert <id> --label L --content C
$PY tools/graph_crud.py edge add <from> <to> --kind K --weight W
$PY tools/graph_crud.py edge set-kind <from> <to> --kind K
$PY tools/graph_crud.py edge rm <from> <to>
$PY tools/graph_crud.py bulk ops.json             # 批量:JSON 数组,见文件头文档
$PY tools/graph_crud.py impact <id> [--kw K]...   # 设定下沉:受影响章号清单(改设定必跑)
$PY tools/graph_crud.py sink-check --stale 旧词 --ok 新词   # 下沉验收:exit 0 才算完成
$PY tools/graph_crud.py selftest                            # 工具自检:否定语境回归用例(无需图库)
```

**铁律（踩坑固化）**：
- 写操作一律走 `MemoryGraph` 封装层，绝不碰底层 `mg._g.add_edge`（对同 (src,dst) 是「替换+复制」语义，会损坏图）。
- 图是简单有向图：每对 (src→dst) 至多一条边；要改 kind 用 `edge set-kind`，多语义关系写进节点 content/边属性或绕中间节点。
- `add` 默认幂等：已存在同 kind 边则跳过；底层偶发双写由 `_ensure_single` 去重兜底。

## 依赖

- Python 3.10+
- axolotl_rs (通过 `install.sh` 自动构建到 `~/.workbuddy/venvs/lobster-memory/`)
