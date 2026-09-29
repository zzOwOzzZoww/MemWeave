# MemWeave 完整 Demo 验收记录

> 历史说明：本文记录的是 v0.1“发布即 active”流程。v0.2 已改为 `candidate → 客观验证 → active`，当前实现和验收结果见 [`v0.2证据生命周期验收记录.md`](v0.2证据生命周期验收记录.md)。

验收日期：2026-09-18

## 验收结论

使用真实 Claude Code（`deepseek-flash`）和 Codex CLI 完成了一次无答案泄漏的双向知识协作：

```text
Claude Code 读取私有策略
  → MCP 发布统一知识
  → Codex 仅通过 MCP 检索
  → Codex 生成交付物并通过本地验证器
  → Codex 回写 verified 并发布执行回执
  → Claude Code 仅通过 MCP 检索回执
  → Claude Code 通过本地验证器并回写 verified
```

最终命令 `demo/run-full-demo.ps1` 从重置独立数据库开始，运行到 `VERIFY FINAL PASS`，进程退出码为 0。

## 最终证据

| 记录 | 知识 ID | 来源 | 跨 Agent 验证 | 最终状态 |
|---|---|---|---|---|
| 权威策略 | `kn_0aac1ee272314bd2` | `claude-code` | `verified by codex` | `active` |
| 执行回执 | `kn_ebeca297fe044292` | `codex` | `verified by claude-code` | `active` |

本地产物：

- `demo/codex-workspace/handoff-result.json`
- `demo/claude-workspace/acceptance-result.json`

两个产物都通过 `demo/scripts/demo_control.py` 的结构、字段、来源 ID和数据库证据检查。Codex 工作区只知道唯一检索词和字段结构，没有策略字段的真实值；真实值必须从 Claude Code 发布的知识正文中读取。

## 客观验证边界

`verify --stage handoff` 检查：

- Claude Code 发布记录存在且为 `active`；
- 发布正文包含可解析的完整策略 JSON；
- 发布正文的字段和值与权威策略一致；
- Codex 输出逐字段匹配策略，保留数字、数组和布尔类型；
- `source_knowledge_id` 指向实际 Claude Code 记录。

`verify --stage acceptance` 检查：

- Codex 回执来源正确；
- 回执包含原策略 ID、`validator=PASS` 和产物名；
- Claude Code 验收物引用实际 Codex 回执 ID。

`verify` 最终检查：

- Claude Code 策略存在 Codex 的 `verified` 证据；
- Codex 回执存在 Claude Code 的 `verified` 证据。

## 体验中发现的问题

| 问题 | 实际影响 | 本版处理 | 后续方向 |
|---|---|---|---|
| Codex MCP 未继承临时数据库环境变量 | Codex 读到正式库，唯一词返回 0 | 启动时通过 Codex MCP 配置显式注入 Demo 数据库 | 为项目提供正式的隔离配置生成器 |
| Codex 任务曾直接包含策略答案 | 即使不检索也可能生成正确结果 | 删除真实值，验证发布正文和交付物的一致性 | 建立更多盲测任务集 |
| 验证器读取工作区外 SQLite 被沙箱拒绝 | Agent 会反复重试并浪费 token | 仅把 `demo/data` 加入本次 Codex 工作区 | 提供独立 verifier tool，避免扩大文件权限 |
| Codex 启动同步无关插件时网络超时 | 首次启动额外等待约 30 秒 | Demo 禁用 plugins、apps、browser 和 skill search | 测量纯客户端启动与 MCP 调用耗时并分别展示 |
| Agent 在工具参数上会试错 | Claude 首次发布曾漏传必填字段，随后自我修正 | 最终结果由 schema 和验证器兜底 | 增加更明确的工具错误码与修复提示 |
| 失败后可能过度自我诊断 | 一次沙箱失败测试消耗大量 token | 修正权限后不再触发该循环 | 增加失败预算和不可恢复错误快速停止机制 |

## 当前判断

这个 Demo 已证明 MemWeave 能完成“跨 Agent 交接知识，并用任务结果反向更新可信度”的最小闭环。它还没有证明自动记忆抽取、长期知识治理或大规模多 Agent 泛化。下一阶段最有价值的工作不是继续增加演示字段，而是建立一组应检索、不应检索、错误知识、过期知识和冲突知识任务，量化调用准确率、结果正确率、额外延迟和 token 开销。
