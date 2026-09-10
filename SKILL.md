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
9. **改卷／幕的走向时，先「接住」现有章骨架，别往里补一条新线**（用户原话：「不是『补进』现有结构，是『接住』现有结构」）。
   - **做法**：先读该卷全部章节点的现有节拍，问一句「这套骨架能不能承载新方向」。**多数时候能，而且比新加更紧**——因为章骨架当初就是按危机曲线排的，换个危机内容往往严丝合缝。
   - **判据**：若新方向需要挪动章序或新增章，先问能不能用现有章**换个内容**接住；必须动位置时，说明为什么接不住。
   - **实证**：卷一改「撤军危机」，原骨架几乎是为它量身留的——ch018 危机砸下（改＝死撑 vs 撤军的抉择）→ch019-020 她不进宫／解法只在讲席上说（改＝解法就是撤军止损）→ch021 请（改＝请她收拾残局，不是请她打仗）→ch022 齐国欠她（改＝撤军做成，这笔账比打退外敌更硬）→ch025 雪化之后（改＝撤军已定，国库窟窿摆上台面）。**一个章都没挪位置**，只换了内容，且顺手把卷二卷三串成一条线。
10. **采纳用户的点子／钩子时，先做动机自洽检验，不自洽就改一层**；改了必须说明改了什么、为什么、换来什么。
   - 用户的钩子通常**方向对、缺一层动机**。落库前先问：在已有 canon 里，这个动机的解释成本是多少？需要加几条额外假设才说得通？
   - **实证**：用户提议「截话的是影子的爪牙」——但那一年主角还是无名民女，影子要提前两年盯上她，得解释「他怎么知道她会谏」，还需额外假设。改成**「通道自动过滤」**：他不知道有这卷书、不需要知道，他只知道那道过滤存在并维护它。**钩子的全部效果保住了，牵强动机没了**，还顺手咬合了已有 canon（影子＝懒惰的守门人——守门人的工作不是恨谁，是守门），并把它升级成「宣王懒得认／系统懒得听」的结构性呼应。
   - **禁止**默默按字面落库、也**禁止**默默改掉不说明。两种都会让真源与用户认知脱节。
   - **用户复核后回话说「你的改法比我的原议好，我认」时，把理由写进节点**（改了什么／为什么／换来什么），
     别只留结论——下次回看时，节点的动机说明就是判断"还能不能改"的依据。
11. **规则允许的例外，必须写进数据本身，不能靠人每次手动跳过**。
   - `sink-check` 有三种"放过"判据，全部由节点内容自证，不需要命令行参数：
     ① **否定句**（`_negated`）：「她**不是**挡路被碾的石头」「**禁写**兵变」；
     ② **引用语境**（`_titled_ref`）：作者在**指称**这个词，不是在用它——
        旧词在《…》书名号内（ch001「书名《有事钟无艳》的民间源头自此埋下」）、
        紧邻「书名／题眼」提示词，或处在**释义句**里（ch085「「娘娘」指妻妾身份」＝红线的裁定说明）；
        ★2026-09-03 扩：俗话／唱段／戏语同属这一类——它们是**故事内被引用的声音**，
        与书名同级（楔子「市井里在传一句俗话——」有事钟无艳，无事夏迎春「」）。
        ⚠️ cue 表**只收指称一种被引用文本/声音的名词**，不收地点状语——
        把「街市」「市井」收进去，「街市上都叫她钟无艳」这种真残留会被洗白（selftest #04 实测抓到）。
     ③ **节点声明豁免**（`_exempt`）：节点里写 `【下沉豁免·钟无艳：民间戏语层，红线⑥例外】`，
        **只豁免标记里点名的那个词**（「豁免·娘娘」不豁免「钟无艳」，否则一个标记洗白整章）。
   - **实证**：红线⑥「钟无艳只属民间戏语层」定下后，`sink-check --stale 钟无艳` 长期报 4 章 FAIL，
     其中 ch001/ch092 是书名引用、ch095/ch096 是红线明确允许的民间层与后记末行——全是合法例外；
     「娘娘」组同样误报 ch085（那里是裁定说明里的释义句）。四类全是**工具看不懂的合法引用**。
     人每次手动判定放过 = 每次都要人肉看一遍 = 下一步就是"反正都要手动看"，纪律作废。
     修法：加②③两种判据 + 把豁免写进节点，跑出来 0 残留、各章自带理由。**误报率是纪律的命门。**
   - 修完 `_negated` / `_titled_ref` / `_exempt` 任一，必须跑 `selftest`（现 36 条用例，全绿才继续验收）。
     新增引用类 cue 时**必须同步加反向用例**（「没有提示词的同形句」＝真残留，不能放行）——
     cue 收宽一个词，就可能把一整类真漏判成合法。
12. **往图库里写编号清单（章号表／章型表／章节映射），落库后必须机器校验完备性**；
   **写作规格（字数／章型／节奏／密度类）不下沉到章节点**，用「规格节点 + 归类表节点」单表承载。
   - **清单必校验**：手抄几十上百个编号，肉眼核对必错——漏一章或重一章，要等到写到那一章才发现，
     那时已在写正文。校验三件事：①解析出的编号**无重复** ②与图库真实章集合**不漏不多** ③各分组
     标题里声明的计数**与实解析计数相符**、编号**不越出该组区间**。
   - **实证**：96 章章型归类表（大戏47／情绪21／信息28）落库后跑校验脚本，四项全过才敢用；
     若靠眼看，卷三漏掉一章要等写到 ch063 才发现字数没处查。
   - **规格不入章节点的理由**：字数属写作规格不是剧情。标进 96 个章节点 = 同一事实 96 处维护，
     改一次要同步 96 处，必然漂移，且与章节点原有的细纲混在一起后 upsert 覆盖风险翻倍。
   - **做法**：章节点只存剧情；规格与归类另立节点（如 `craft_chapter_length` 定规则 +
     `craft_chapter_typemap` 存全量归类），节点里写明「**唯一归类口径＝该表，改章型改这里**」。

```bash
# 1) 影响面：我这次改动会波及哪些章
$PY tools/graph_crud.py impact chr_shadow --kw 兵变 --kw 挡路 --kw 石头 --kw 认真开关
# 2) 逐章改（走 bulk），改完验收：旧词是否清干净、新词是否已下沉
$PY tools/graph_crud.py sink-check --stale 兵变 --stale 挡路被碾的石头 --ok 造条件 --ok 认真开关
#    → 结论 PASS (exit 0) 才算收工；FAIL (exit 1) 继续改
#    → 三 bucket：❌仍写旧口径(必须改) / ⚠️引用语境(否定句·书名题眼·声明豁免，人工确认) / ✅已写新口径
# 3) 工具自检（改过 _negated / _titled_ref / _exempt 后必跑，不需要图库）
$PY tools/graph_crud.py selftest   # 46 条：否定语境 25 + 书名/释义引用 17 + 声明豁免 4
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

# 默认库解析顺序：① 脚本所在项目根 ② **cwd 逐级向上找** .memory-graph/memory.axeb
#   ③ 环境变量 LOBSTER_DB；也可用 --db <path> 显式指定任意库。
#   ⚠️ ② 是救命分支：直接调技能目录里的真实路径时，BASE 指向技能根（那里没有 .memory-graph），
#      旧版只会报「未指定图库路径」。所以**在目标项目目录下调用即可**，不必先 export LOBSTER_DB。
# ⚠️ 项目里的 tools/graph_crud.py 软链没有执行权限：`./tools/graph_crud.py` 会 permission denied，
#    必须写成 `$PY tools/graph_crud.py`（用解释器跑，软链路径也能被正确解析到项目根）。
# 项目里推荐把 tools/graph_crud.py 软链到本文件，避免多份副本漂移。

$PY tools/graph_crud.py list [--prefix P] [--bare --id-only]   # --bare 管道取 id(默认输出带前导空格)
$PY tools/graph_crud.py dump [--prefix P] [--full]  # ⚠️批量分析必须 --full，否则 content 只 60 字
$PY tools/graph_crud.py scan-dups                 # 重边体检(全图 0 平行边才算健康)
$PY tools/graph_crud.py check-ids                 # id 形态体检(props['id'] 是哈希数字即 exit 1)
$PY tools/graph_crud.py get <id>                  # 查节点 + 出/入边(kind/weight)
$PY tools/graph_crud.py upsert <id> --label L --content C
$PY tools/graph_crud.py edge add <from> <to> --kind K --weight W
$PY tools/graph_crud.py edge set-kind <from> <to> --kind K
$PY tools/graph_crud.py edge rm <from> <to>
$PY tools/graph_crud.py bulk ops.json             # 批量:JSON 数组,见文件头文档
$PY tools/graph_crud.py impact <id> [--kw K]... [--max-cover 0.6]  # 设定下沉:受影响章号清单(改设定必跑)
$PY tools/graph_crud.py sink-check --stale 旧词 --ok 新词   # 下沉验收:exit 0 才算完成
$PY tools/graph_crud.py selftest                            # 工具自检:否定语境回归用例(无需图库)
```

**⚠️ 批量取数三戒（2026-09-08：三条都造成过「静默失真」——不报错，只是结论错）**：
1. **`dump` 默认只输出 content 前 60 字**，批量分析必须加 `--full`。
   事故：《有事钟无艳》审计第一轮基于截断文本，得出「伏笔 0 条、红线零违规」的假结论，
   真实数字是 234 条。⚠️也不要凭 dump 的短输出下结论。
2. **`list` 默认输出带两个前导空格**，把首字段直接喂给 `get` 会**静默取到空串**。
   管道取 id 用 `--bare --id-only`。（CLI 入口现已统一 strip 所有 id 类参数兜底，但仍别依赖。）
3. **`impact` 的 B 组关键词有覆盖度闸门**（`--max-cover`，默认 0.6）：
   命中章数占比超阈值的词被剔除并**明示列出**。由来：自动抽词常抽到「出场」「钟离春」这类词，
   命中 96/96 章，B 组等于全量章号——比不给还糟（人无法逐核，只能放弃这个工具）。
   确需保留用 `--max-cover 1.0`。

**⚠️ 机检判据必须认人真正使用的写法（2026-09-08）**：
`sink-check` 曾把**每一条红线声明本身**判成「旧词残留」——4 章 FAIL 全是误报，
根因是 `NEG_CUES` 里没有 `⛔`：本项目的禁令一律写作「⛔X」，⛔ 是最强的否定信号，机器却不认。
**后果比误报严重：写得越守纪律 FAIL 越多，人只能手动跳过，纪律随即作废。**
已修：①`⛔` 等入 `NEG_CUES`；②⛔ 是**行首标记**（作用域=整行），用句级边界（。！？\n）判定，
其余否定词是小句谓词仍用小句边界（。！？；，\n）——两者边界不同，混用会互相破坏；
③`_titled_ref` 增加「更长引用短语的一部分」（「有事钟无艳」里的「钟无艳」是引用俗语整体，不是在单独用词）。
★**新增放行判据必须同时补 selftest 反向用例**（「⛔ 在别的小句 → 不得外溢」这类），
否则放行迟早会扩张成「整章被洗白」——这与 `_titled_ref` 收录「书名/题眼」是同一条道理。

**铁律（踩坑固化）**：
- 写操作一律走 `MemoryGraph` 封装层，绝不碰底层 `mg._g.add_edge`（对同 (src,dst) 是「替换+复制」语义，会损坏图）。
- 图是简单有向图：每对 (src→dst) 至多一条边；要改 kind 用 `edge set-kind`，多语义关系写进节点 content/边属性或绕中间节点。
- `add` 默认幂等：已存在同 kind 边则跳过；底层偶发双写由 `_ensure_single` 去重兜底。
- **⚠️ bulk 的 op 名只有三个：`upsert` / `status` / `edge_add`**（`edge set-kind`/`edge rm` 是 CLI 子命令，不是 bulk op）。
  写 `{"op":"edge", "kind_name":...}` **不会报错**，而是逐条打印 `未知 op: edge` 后 exit 0 ——
  看起来像"跑完了"，实际边一条没落（实测 batch15：5 个 upsert 成功、7 条边全静默丢失，
  事后 `get` 才发现没边）。正确写法：`{"op":"edge_add","from":A,"to":B,"kind":"governs","weight":1.5}`
  （字段名 `from`/`to`/`kind`/`weight`；`edge_add` 内部走 `_ensure_single`，保单边）。
  ★**bulk 跑完必须回看每一行的返回值**：只有 `=> 新增 / 覆盖 / 已加边（保单边）` 才算落上，
  出现 `未知 op` 立刻改 op 名重跑（只重跑失败的边即可，upsert 幂等不用回滚）。
- **bulk 字段名有两个别名坑，均已兼容，但仍优先用第一种**：`status` op 认 `status`/`value`；
  `edge_*` op 认 `from`/`to` 与 `src`/`dst`。写错时旧版只抛裸 `TypeError: None + str`，
  看不出是字段名问题——已修为可读报错（`<缺 id / from-src / to-dst>`）。
- **⚠️ upsert 是整段替换，写 bulk 前必须用 `get` 取回原文再追加**。
  凭印象重编 content 会静默删掉已有细纲（实测：ch032 有截话伏笔/H3宣王在场/淳于髡状态三段，
  ch095 有【下沉豁免】标记——整段替换一次全没）。正确姿势：原文完整保留 + 末尾追加新段，
  JSON 写完先量字数，应**大于**原文。
  ★**更彻底的做法：别手抄原文**——用脚本调 `graph_crud.py get <id>` 取回 content，
  正则 `content: (.*?)\n出边:` 解析，拼上追加段再 `json.dump` 生成 bulk 文件。
  手工 `get` 之后肉眼复述一遍仍然是抄，抄就一定有漏；脚本取的是字节原样。
  生成后校验：`orig[:60] in new_content`（原文确实还在开头）。
- **⚠️ 节点 id 只能是「非空字符串」，绝不能是纯数字串**（2026-09-10 事故）：
  把 `str_to_id()` 的十进制输出当 id 回写，会让节点**永久失去可读 id**（哈希不可逆），
  并连带三症状：`get` 的**入边清单为空**、`list --prefix` **漏检**、`dump` 显示的 id 不可用。
  实测《有事钟无艳》339 节点中 42 个中招（13 章 + 9 条角色声谱 + 11 条伏笔 + 其余）。
  ★**写入侧已硬拦**：`upsert_vertex` 调 `validate_str_id`，传哈希 id 直接 raise（bulk 记为该行异常）。
  ★**读取侧体检**：`$PY tools/graph_crud.py check-ids`（有污染即 exit 1，可接门禁）。
  ★**恢复法**：哈希不可逆，只能按 label 反查历史脚本/JSON 里的明文 id，
  用 `str_to_id(候选) == 该节点键` 校验后回填 `raw["id"]`——这是数学验证，不是猜。
  ★**污染会掩盖重边**：`scan-dups` 输出若带哈希端点，说明该端 id 已坏、重边可能早已存在
  （实测：修复前显示 `lobster_root -> 596088711727576705 x2`，修复后现形为 `-> ch092 x2`）。
- **⚠️ 用 MemoryGraph 裸写脚本后必须显式 `save()`**（同日事故）：
  `engine` 层的 `add_vertex`/`add_edge` 只改内存，**进程退出即丢失，且全无报错**——
  第一次修复脚本 42 条"全部回填成功"、返回码 0，重开进程一读纹丝未动。
  `graph_crud.py` 各子命令退出前统一 `mg.close()`（内部落盘）所以看不出来；
  自己写脚本时必须 `mg.save()` 再 `mg.close()`。★**写完务必另起进程回读验证**（同进程读到的只是内存）。
- **⚠️ sink-check 默认只扫 `ch\d{3,}`**：楔子/后记/幕/卷这类**非编号篇章不在管辖内**，
  改了它们的措辞不会有任何机器提示。扫它们要显式给 `--chapter-re`：
  `$PY tools/graph_crud.py sink-check --stale 钟无艳 --ok 钟离春 --chapter-re "^(seg_|act)"`
  （实测：楔子细纲落库后默认扫全绿，用 `--chapter-re` 一扫立刻抓到 1 处待判定的引用。）

## 依赖

- Python 3.10+
- axolotl_rs (通过 `install.sh` 自动构建到 `~/.workbuddy/venvs/lobster-memory/`)
