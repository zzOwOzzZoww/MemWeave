# Claude Code 自学习 Demo 验收记录

> 本文记录 Runtime 引入前的 Hook 直连 Core 验收。当前 v0.3 Runtime 架构结果见 [`v0.3_Runtime架构验收记录_20260918.md`](v0.3_Runtime架构验收记录_20260918.md)。

验收日期：2026-09-18

## 验收目标

验证 MemWeave 能否在不依赖模型主动调用 MCP 的情况下，从一次已验证的 Claude Code 任务中提炼知识，并在全新会话中自动召回该知识完成相似任务。

## 测试设计

第一轮要求 Claude Code 读取 Widget 规范，生成 `alpha.widget.json`，并运行独立验证器。任务结束后，`Stop` Hook 读取 transcript，调用 `deepseek-flash` 生成知识候选；候选只有引用 transcript 中成功的验证命令，才能自动晋升为 `active`。

第一轮结束后，脚本把 `alpha.widget.json` 移出第二轮工作区。第二轮使用新的 Claude 会话 ID，禁止读取 `domain/` 和 `tools/` 内容，只允许执行已知验证器。`UserPromptSubmit` Hook 根据新任务检索并注入第一轮的 `active` 知识。

验收命令：

```powershell
# 从 MemWeave 仓库根目录运行
.\demo\run-self-learning-benchmark.ps1
```

成功条件：两轮均为独立会话；第一轮产生可召回的 `active` 知识；第二轮发生召回且不读取规范；两个产物均通过验证器；召回事件被后续成功验证闭环。

## 实际结果

- 第一轮从空目录创建 `alpha.widget.json`，验证器返回 `PASS`。
- 第二轮创建 `beta.widget.json`，验证器返回 `PASS`。
- 两个学习任务均完成，知识候选均带成功命令证据并晋升。
- 第二轮 transcript 只包含哈希计算、文件写入和验证器执行，没有读取规范或验证器源码。
- 脚本输出 `SELF-LEARNING DEMO PASS`，退出码为 0。
- 第一轮会话 ID：`9d0426e6-3fec-4dd2-b40d-2b5facbac1f3`；第二轮会话 ID：`c81c1c4f-bd06-4029-8db9-7b32f59c2e37`。

本次干净运行产生的指标：

| 指标 | 结果 | 含义 |
|---|---:|---|
| `learning.completed_runs` | 2 | 两轮复盘都完成 |
| `learning.proposals` | 3 | DeepSeek 从两轮 transcript 共提炼 3 条候选 |
| `learning.promotion_rate` | 1.0 | 本轮提取的候选都获得客观证据并晋升 |
| `recall.attempts` | 2 | 两次用户请求都执行了检索 |
| `recall.hits` | 1 | 第一轮知识库为空，第二轮命中 |
| `recall.hit_rate` | 0.5 | 符合“先学习、后召回”的测试设计 |
| `recall.post_recall_success_rate` | 1.0 | 唯一一次有效召回通过后续验证 |
| `recall.average_latency_ms` | 1.97 | 本地知识检索平均耗时 |
| `recall.average_injected_chars` | 500 | 每次检索平均注入字符数，包含未命中的零注入 |
| `learning.average_review_latency_ms` | 4126.65 | DeepSeek 后台复盘平均耗时 |

## 结论和边界

这次验收证明了一个最小的跨会话学习闭环：任务执行产生证据，后台提炼知识，证据门控制晋升，新会话按需召回，任务结果再反向验证知识。它比“把全部历史对话塞进上下文”多了来源、证据、状态和效果反馈。

当前结果只有一组人工构造的任务，不能证明长期使用必然持续变好，也不能证明在复杂业务中优于 Claude Code 自带记忆。`promotion_rate=1.0` 和 `post_recall_success_rate=1.0` 只代表本次样本，不是稳定性能结论。下一阶段应接入 DPI 领域任务，建立应召回、不应召回、错误知识、过期知识和冲突知识样本，比较无 MemWeave 与启用 MemWeave 时的任务成功率、错误召回率、token 增量和延迟。
