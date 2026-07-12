# ClawHub 上架说明（Marketplace Listing）

> 用途：准备在 ClawHub（https://clawhub.ai，OpenClaw/WorkBuddy 生态官方技能市场）发布
> `lobster-memory` 与 `wb-lobster-memory`。
> 分析时间：2026-07-11。发布前请按 ClawHub 实际字段规范核对 SKILL.md 的 frontmatter。

## 两个技能的关系

- **`lobster-memory`** —— 核心引擎：Rust 图存储 `axolotl_rs` + Python 引擎层（抽取 / 回忆 / 巩固）。
- **`wb-lobster-memory`** —— WorkBuddy 桥接层，把引擎接入对话流。**依赖** `lobster-memory`。

## 上架单元建议

只把 **`wb-lobster-memory`** 作为用户入口上架（它自带 `dependencies` 说明）。`lobster-memory` 作为依赖引擎，
用户在安装 wb 层时按提示安装。两个仓库都补齐了 ClawHub 审核字段（`version` / `triggers` / `allowed-tools` / `requires`）。

## 上架字段（建议值）

### wb-lobster-memory（用户入口）
- `name`: wb-lobster-memory
- `version`: 0.1.0
- `author`: Sai
- `description`: WorkBuddy 接入 lobster-memory 长期图记忆的桥接技能……（见 SKILL.md）
- `categories`: **Agents**（首选）；若允许多选，加 **Knowledge**
- `topics`: memory, knowledge-graph, long-term-memory, ai-agent, workbuddy, agent-memory, llm-memory
- `tags`: memory, knowledge-graph, long-term-memory, agent, workbuddy
- `triggers`: 记住这个 / 用图记忆 / 回忆一下 / 巩固记忆 / 长期记忆
- `allowed-tools`: [Bash]
- `requires.env`: LOBSTER_MEMORY_ENGINE, LOBSTER_MEMORY_PYTHON

### lobster-memory（依赖引擎）
- `name`: lobster-memory
- `version`: 0.1.0
- `author`: Sai
- `categories`: **Development**（首选）；若允许多选，加 **Knowledge**
- `topics`: memory, knowledge-graph, graph-database, rust, pyo3, llm-memory, long-term-memory, ai-agent
- `tags`: memory, knowledge-graph, rust, graph-database, long-term-memory
- `allowed-tools`: [Bash]
- `requires`: python >= 3.10

## 用户侧安装步骤

1. 安装核心引擎（含 `axolotl_rs` 图存储）：
   ```bash
   git clone https://github.com/LittleLollipop/lobster-memory.git
   cd lobster-memory && bash install.sh
   ```
2. 安装桥接层（ClawHub 一行命令，或 git clone 后按 SKILL.md 配置）。
3. 设置环境变量指向引擎：
   ```bash
   export LOBSTER_MEMORY_ENGINE=/path/to/lobster-memory
   export LOBSTER_MEMORY_PYTHON=/path/to/lobster-memory/venv/bin/python
   ```

## 已知限制（诚实披露，避免差评）

- **平台限制**：当前仅支持 **Apple Silicon (macOS aarch64 / M 系列芯片)**。预编译 wheel 为 `aarch64-apple-darwin`，Intel Mac / Linux / Windows 暂未提供 wheel 且未经测试。
- **版本定位**：`0.x` 早期原型，单人维护。设计理念领先（递归自成长抽取 / 情绪 valence / 可观察遗忘 / 因果边），工程成熟度尚在早期。
- **`axolotl_rs` 安装**：当前需 Rust 工具链源码构建，或从 PyPI / 仓库内 `wheels/` 安装预编译包。`install.sh` 已改为 wheel 优先、源码兜底，并在缺 Rust 时给出明确报错。
- **未跑公开基准**：尚未提交 LongMemEval / DMR 等基准分数，"水平"目前为主观评估。
- **无时态版本化**：事实变更目前为覆盖式，计划补 `valid_from/invalid_at` 窗口（与 Zep 拉平）。

> 诚实定位文案（可用于 ClawHub 简介）：*想秀的是脑子，不是 star 数。* 与 `0.x` 版本号自洽，设预期、防过度包装反噬。
