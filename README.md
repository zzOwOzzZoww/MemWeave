# MemWeave（织忆）

**简体中文** | [English](https://github.com/zzOwOzzZoww/MemWeave/blob/main/README.en.md)

MemWeave 是一个给 Coding Agent 用的本地共享记忆层。

它让 Claude Code、Codex 等 Agent 在同一个项目里复用已经确认过的知识，比如技术决策、用户偏好、踩坑经验和项目约定。它不会把所有对话都当成“记忆”，也不会为了看起来聪明而硬塞不相关内容。

> 当前版本：0.5.0a1（Alpha）。核心闭环已经可以运行，适合在测试项目中体验和验证。

## 它解决什么问题

平时在多个 Agent 之间切换，经常会遇到这些情况：

- Codex 刚弄清楚的项目约定，Claude Code 又要重新问一遍。
- 旧方案已经被替换，但 Agent 还在引用过时结论。
- 一段偶然对话被当成长期事实，之后反复干扰任务。
- 为了提高召回率，系统把“有点像”的内容也塞进上下文。

MemWeave 想做的是一个小而完整的闭环：

1. Agent 结束一轮工作后，只提炼可能值得保留的候选知识。
2. 候选经过人工确认或客观证据验证后，才能成为可用知识。
3. 新任务开始时，只召回当前项目和当前问题真正相关的内容。
4. 旧知识可以归档、隔离、替换或删除，并保留来源和证据记录。

## 设计原则

- **本地优先**：知识库和 Runtime 默认都在本机运行。
- **先候选，后生效**：新知识默认是 candidate，不会直接污染长期记忆。
- **零召回很正常**：没有合适内容时就返回空，不强行注入相近话题。
- **Core 不依赖 MCP**：Agent 通过原生 Hook 或 Runtime API 接入；MCP 只是可选入口。
- **保留上下文边界**：项目、来源 Agent、来源会话和证据引用都会记录。
- **不保存敏感信息**：不要持久化密码、Token、原始私人对话或未经验证的猜测。

## 工作方式

~~~text
Claude Code Hook ─┐
Codex Hook ───────┼─ 本地 Runtime ── 检索 → 仲裁 → 上下文注入
CLI / Web ───────┘       │
                        └─ 后台学习 → 候选知识 → 审核/验证 → 生命周期治理
                                      │
                                 SQLite + FTS5

可选 MCP ─────────────────────────────┘
~~~

检索热路径不调用模型，主要使用 SQLite FTS5/BM25，再做有限的中英文术语桥接、同主题扩展和证据门禁。它不是通用语义搜索引擎，目标是让固定的知识治理闭环保持可解释、可审计。

## 快速开始

要求 Python 3.11 或更高版本。

### 直接从 GitHub 安装

~~~shell
python -m pip install "git+https://github.com/zzOwOzzZoww/MemWeave.git"
memweave setup
~~~

### 从源码安装（开发者）

~~~shell
git clone https://github.com/zzOwOzzZoww/MemWeave.git
cd MemWeave
python -m pip install -e .
memweave setup
~~~

setup 会引导你填写模型服务的 Base URL、模型名和 API Key。模型只用于后台知识提炼，日常召回不会读取凭据，也不会调用模型。

Windows 下会创建“MemWeave知识管理”桌面入口。macOS 和 Linux 可以通过 memweave ui 打开管理页，目前桌面入口只在 Windows 做过验收。

## 常用命令

~~~shell
memweave                     # 首次使用进入配置，之后打开管理页
memweave ui                  # 启动 Runtime 并打开管理页
memweave status              # 查看 Agent、知识和学习任务状态
memweave configure           # 修改模型 API 配置，不会清空知识库
memweave doctor              # 检查本地配置和运行状态
memweave doctor --check-api  # 发送一次短请求检查 API，可能产生少量费用
memweave shortcut            # 修复 Windows 桌面入口
~~~

在管理页的“Agent 维护”中选择 Claude Code 或 Codex，才会安装对应的全局 Hook。安装后可能需要重启客户端。

## 知识怎么生效

MemWeave 不会把“模型说过”当成“已经证实”。常见状态包括：

- **candidate**：刚提炼出来，等待确认。
- **active**：通过审核或证据验证，可以参与召回。
- **archived**：暂时不使用，但保留历史记录。
- **quarantined**：存在风险或超过审核期限，默认不参与召回。

明确的偏好或决定发生变化时，管理页会提示新旧版本冲突。确认替换后，新版本生效，旧版本只用于历史追溯。复杂、带条件或含糊的说法仍需要人工判断。

## 数据与隐私

默认数据目录是 ~/.memweave，主要包括配置、SQLite 数据库、运行状态和日志。可以用 MEMWEAVE_HOME 改到其他目录。

后台提炼会把经过脱敏的会话摘要发送给你配置的模型服务商。脱敏无法覆盖所有自定义凭据格式，所以不要把不允许外发的会话接入远程服务商。

API Key 不会写进知识库。Windows 使用当前用户的 DPAPI 加密保存；其他系统写入权限为 0600 的独立配置文件，目前不做额外加密。

## 开发与测试

~~~shell
python -m pip install -e ".[test]"
python -m pytest tests -q
python -m pip wheel . --no-deps --wheel-dir dist
~~~

截至 2026-09-29，全量测试为 **408 passed, 9 subtests passed**，并通过 Windows、Ubuntu 与 Python 3.11、3.12 的 CI 验证。这些结果证明当前固定测试集上的行为，不代表开放领域语义理解、真实 Agent 任务成功率或生产规模性能。

项目目录：

~~~text
src/agent_knowledge_bridge/        Core、Runtime、CLI 和管理页
src/agent_knowledge_bridge/hooks/  Claude Code / Codex 原生 Hook
scripts/                           开发、安装验收和评测脚本
tests/                             回归测试与产品安装测试
docs/                              设计说明和历史验收记录
evaluation/                        合成评测集候选版
~~~

## 当前边界

- 目前是 Alpha，还没有完成所有操作系统和客户端版本的实机验证。
- 术语桥是小型、可审计的规则集，不追求覆盖所有表达方式。
- 合成评测集用于检查闭环行为，不等同于真实对话或真实任务评测。

本项目使用 [Apache License 2.0](https://github.com/zzOwOzzZoww/MemWeave/blob/main/LICENSE)。

更详细的设计与验收记录可以查看 [docs/](https://github.com/zzOwOzzZoww/MemWeave/tree/main/docs) 目录。
