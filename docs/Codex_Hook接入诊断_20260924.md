# Codex Hook 接入诊断记录（2026-09-24）

## 现象

页面显示 Codex 已加入 MemWeave，但正常桌面对话没有新增 `knowledge.codex-hook-runs.jsonl` 记录，Codex 自产知识数停留在 3。截图中的外部知识库凭据提示不是 MemWeave 的报错，也不是 MemWeave 的依赖。

## 根因

Codex 0.155 的 Hook 有两层开关：

1. `~/.codex/hooks.json` 中存在 `UserPromptSubmit` / `Stop` 命令；
2. `~/.codex/config.toml` 的 `[features]` 中必须有 `hooks = true`。

之前只检查了第一层，因此“配置文件已写入”被错误地展示成“已接入”。第二层关闭时，Codex app-server 会发现配置，但不会在真实 turn 中执行 Hook。

## 修复

- 已在用户级 `config.toml` 打开 `hooks = true`；
- MemWeave 的 Codex Hook 安装器现在会同时修复这个功能开关；
- 管理页面新增三种状态：
  - `Hook 功能未启用`
  - `Hook 未实际执行`
  - `全局 Hook 已配置`
- 页面只有在审计文件中观察到与真实 Codex rollout 对应的执行记录后，才将执行状态视为有效；手工调用 Hook 不再冒充桌面接入。

## 验证方法

重启 Codex Desktop 后，在普通对话中发送一条与已有 Claude 知识相关的问题。随后检查：

```text
<MEMWEAVE_HOME>\data\knowledge.codex-hook-runs.jsonl
```

应出现当前 rollout 对应的 `UserPromptSubmit` 和 `Stop` 完成记录，且 `UserPromptSubmit` 的输出包含 `<memweave_context>`。管理页面应显示 `Hook 已实际执行`，Codex 的“可召回”数量应包含 `source_agent=claude-code` 的 active 知识。

## 边界

MemWeave 只使用本地 SQLite、原生 Hook 和 Runtime API。其他知识库集成及其凭据不是本项目的依赖，也不能作为本项目的修复方案。

## 追加排查：知识可检索但 Codex 收不到（2026-09-24）

数据库和 Runtime 的检索结果正常，问题出在 Windows Hook 进程的标准输出编码。Codex 需要读取 UTF-8 JSON，但 Hook 继承了 GBK 控制台编码；召回上下文包含 `⇾`、`➜` 等字符时，`print(json.dumps(...))` 会失败，日志表现为：

```text
'gbk' codec can't encode character ...
```

这会造成“知识已 Active、页面显示可召回，但 Codex 对话没有拿到上下文”的假象。现已在 `scripts/codex_learning_hook.py` 启动时固定 stdout/stderr 为 UTF-8，并验证真实 Hook 输出可被 UTF-8 JSON 解析，且包含 `<memweave_context>`。同时已重新启动当前 Runtime，避免 Hook 连接到已退出的旧 Runtime 状态文件。

验证结果：本地 Hook 和 Runtime Hook 均返回非空上下文，当前回归测试为 `98 passed, 2 subtests passed`。真实桌面会话仍需在重启 Codex Desktop 后观察审计文件中的新 `UserPromptSubmit` / `Stop` 记录。
