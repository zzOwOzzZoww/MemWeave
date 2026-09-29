# Claude Code 自学习 Demo

这个 Demo 用 Claude Code 原生 Hook 和本地 Runtime 接入 MemWeave，不依赖模型主动调用 MCP：

```text
UserPromptSubmit
  → Runtime Client 调用本地鉴权 API
  → 检索 active 知识
  → 只注入与当前问题相关的内容

Stop
  → Runtime Client 提交本轮 transcript
  → 在结束 Hook 中读取本轮 transcript
  → DeepSeek 提炼 KnowledgeProposal
  → 核对真实工具事件
  → 有成功测试证据才自动晋升 active
  → 无客观证据则保留 candidate 等待人工审核
```

启动脚本会选择随机本地端口，生成临时 Bearer Token，并启动只监听 `127.0.0.1` 的 MemWeave Runtime。Hook 仅配置在 `self-learning-workspace/.claude/settings.json`，不会修改用户全局 Claude Code 设置；MCP 已从该会话中隔离。

## 自动验收

在 PowerShell 中运行：

```powershell
# 从 MemWeave 仓库根目录运行
.\demo\run-self-learning-benchmark.ps1
```

第一轮 Claude Code 会读取 Widget 规范、生成 `alpha.widget.json` 并运行验证器。后台复盘应提炼出 Widget 流程，并通过 transcript 中的成功验证命令将它晋升为 `active`。

第二轮是全新会话，只给出新的 Widget 参数，禁止重新查看规范。`UserPromptSubmit` Hook 应自动注入第一轮学到的流程，Claude Code 生成 `beta.widget.json` 并再次通过验证。

## 自己连续使用

```powershell
.\demo\start-self-learning.ps1
```

在启动的 Claude Code 中先完成一个带测试的真实任务，然后输入 `/new` 开启新会话，再给它一个相似任务。相关 `active` 经验会在提交问题时自动注入。

## 查看候选和指标

直接运行：

```powershell
.\demo\show-self-learning-status.ps1
```

它会显示当前指标和等待人工审核的候选知识。需要审核某一条时，先按脚本里的环境变量进入对应数据库，再使用以下核心命令：

```powershell
python .\scripts\claude_learning_hook.py metrics
python .\scripts\claude_learning_hook.py pending
python .\scripts\claude_learning_hook.py approve <knowledge_id> --reason "我确认这条经验应长期保留"
python .\scripts\claude_learning_hook.py reject <knowledge_id> --reason "这条经验不正确或不具备通用性"
```

指标包括：

- `promotion_rate`：候选中有客观证据并晋升的比例；
- `recall.hit_rate`：用户问题触发相关有效知识的比例；
- `post_recall_success_rate`：召回知识后通过客观验证的比例；
- `average_review_latency_ms`：后台复盘平均耗时，不计入正常回答等待时间；
- `average_injected_chars`：每次召回增加的上下文字符数。

当前 Demo 证明的是“自动采集、自动提炼、证据晋升、跨会话召回和效果反馈”。它还不能证明长期使用一定持续变好，需要在多个任务和多轮时间窗口上继续记录指标。
