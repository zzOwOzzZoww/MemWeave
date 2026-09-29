# MemWeave 双向知识协作 Demo

这个 Demo 使用一个独立数据库，完整演示：

```text
Claude Code 读取私有策略并发布 candidate
              ↓
Codex 从审核队列读取 candidate，生成交付物并运行本地验证
              ↓
Codex 回写 verified 证据使策略晋升 active，并发布 candidate 回执
              ↓
Claude Code 审核回执并回写 verified，使回执晋升 active
```

Demo 标识为 `MW-DEMO-HANDOFF-20260918`。数据库位于 `demo/data/knowledge.db`，不会读写正式的 `data/knowledge.db`。

## 先看一遍完整效果

在 PowerShell 中执行：

```powershell
cd .\demo
.\run-full-demo.ps1
```

脚本会依次调用真实的 Claude Code（DeepSeek）和 Codex，并在最后输出知识记录、来源 Agent、验证次数和证据链。它不会把 API Key 写入文件，只读取 Windows 用户环境变量 `DEEPSEEK_API_KEY`。

查看结果：

```powershell
python .\scripts\demo_control.py status
python .\scripts\demo_control.py verify
Get-Content -Raw .\codex-workspace\handoff-result.json
Get-Content -Raw .\claude-workspace\acceptance-result.json
```

## 自己分步体验

先重置：

```powershell
python .\scripts\demo_control.py reset
```

然后按顺序分别运行三个入口：

```powershell
.\start-claude-publish.ps1
.\start-codex-handoff.ps1
.\start-claude-accept.ps1
```

每个入口都会打开对应客户端，并附带当前阶段任务。可以观察 Agent 是否主动选择 MCP、它实际拿到了哪些知识、何时提交反馈，以及哪些授权或交互影响体验。完成每一步后输入 `/exit` 返回 PowerShell，再启动下一步。

Codex 启动时只额外授权 `demo/data`，供本地验证器只读检查证据链；策略获取和反馈仍必须经过 MCP。

最终验收：

```powershell
python .\scripts\demo_control.py verify
```

通过标准：

- Claude Code 发布的策略先被记录为 `candidate`；
- Codex 的结果与策略字段完全一致；
- 策略记录包含 Codex 的可追溯 `verified` 证据并晋升为 `active`；
- Codex 发布的回执先被记录为 `candidate`；
- Claude Code 的验收结果引用正确回执；
- 回执记录包含 Claude Code 的可追溯 `verified` 证据并晋升为 `active`。

## 重点体验和记录

实际操作时重点记录以下问题，它们会成为下一版框架需求：

- Agent 是否在需要历史知识时主动检索，还是必须明确提醒；
- 搜索词稍有变化时是否还能找到正确记录；
- 工具授权是否频繁打断任务；
- Agent 是否会把“已读取”误写成“已验证”；
- 验证失败后，错误原因是否足以指导修正；
- 共享知识是否给普通任务带来无关噪音；
- 整个过程相对不使用 MemWeave 增加了多少等待时间。

这个 Demo 当前验证的是“受控任务中的知识交接和证据闭环”。它还没有实现自动会话提取、语义检索、知识归档恢复和冲突合并。
