# Agent 接入与通用学习 API

Hook 是客户端的事件入口，Runtime 是 MemWeave 的处理接口，两者不是互斥方案。没有原生适配器通常是尚未实现该客户端的事件和数据格式映射，不是缺少厂商授权。

## 统一接入层

Claude、Codex、Gemini 的旧脚本保留为兼容入口，实际都调用 `hooks/shared_hook.py`。Runtime 选择、鉴权、共享项目解析、召回、学习提交、UTF-8 输出和错误处理只维护一套；厂商的事件名、字段和上下文输出格式由 `integration_profiles.py` 描述。知识准入与 LFHV 等领域逻辑在协议无关的 `LearningEngine` 中，返回上下文文本，不生成某个客户端的 Hook 响应。既有 Runtime HTTP 响应格式和旧 Python Adapter 导入保持兼容。

注册与“修复接入”安装的启动器现在也统一调用 `generic_learning_hook`，不再指向各 Agent 的专属运行模块。旧启动器文件名保留兼容，安装器会更新其内容；状态检查同时核对启动器内容、协议配置及自定义方案的托管副本，不能只因文件存在就判定已配置。旧启动器尚未升级时，原生接入会显示待修复。

### 接入方案执行接口

`integration_installation.py` 为后续能力探测层提供三个接口：`prepare_hook_installation` 只读校验方案与目标配置；`install_hook_plan` 根据已确认的协议安装；`inspect_hook_plan` 只读检查安装是否匹配。支持的协议格式是 `command-json`、`gemini-json` 和 `codebuddy-json`，不会根据 Agent 名称猜测未知协议。

```python
from pathlib import Path
from agent_knowledge_bridge.integration_profiles import load_profile
from agent_knowledge_bridge.integration_installation import (
    prepare_hook_installation, install_hook_plan, inspect_hook_plan,
)

# The discovery layer must confirm these paths and this protocol first.
profile = load_profile(prepared_profile_path)
plan = prepare_hook_installation(
    profile, config_path=Path(client_settings_path), protocol="command-json",
)
before = inspect_hook_plan(plan)  # Read-only; no client settings are changed.
installed = install_hook_plan(plan)  # Call only after the user chooses to connect.
```

安装保留其它设置和第三方 Hook，首次修改前备份，重复安装不重写未变化的文件。自定义方案保存为 MemWeave 托管副本，启动器固定使用当前安装的 Python 与 MemWeave 数据目录，从仓库外也能运行；不会在启动时执行探测脚本或模型生成的任意代码。安装时重新读取并校验目标配置，不盲信准备时的旧内容。`configured` 只说明安装匹配，不证明客户端真正触发过事件。

未知 Agent 的后台能力探测与自动方案准备仍未实现。WorkBuddy 的本机 CodeBuddy 执行内核已核对支持命令 Hook，因此现在有已确认的声明式接入方案；其它未知客户端仍不能仅凭登记自动接通。这里的开发接口供已确认方案或后续探测层使用，不要求普通用户自行编写适配代码。

### WorkBuddy

在管理页加入 WorkBuddy，或对旧的 Runtime-only 登记点击“修复接入”，会将 `UserPromptSubmit` 和 `Stop` 写入 `~/.workbuddy/settings.json`。目录优先使用 `WORKBUDDY_CONFIG_DIR`，其次为 `CODEBUDDY_CONFIG_DIR`。已有设置、第三方 Hook 和凭据字段原样保留，修改前备份；不会复制会话或凭据到仓库。

WorkBuddy 直接调用统一执行器，不新增专属运行脚本。`codebuddy_transcript.py` 只解析有界的本地 JSONL，保留当前分支、用户轮次与工具证据；支持原生 `callId`、嵌套命令退出码，排除注入消息和推理内容。Windows 使用隐藏的 PowerShell 命令调用带标准输入输出的 Python，避免 Bash 对 Windows 路径的重解析以及 `pythonw.exe` 无标准流的问题。

安装后重启 WorkBuddy，或新开会话。状态先显示“已配置”；只有记录到与本地项目会话对应的 Hook 执行证据后才显示“已记录执行”。`disableAllHooks` 或 `allowManagedHooksOnly` 限制不会被擅自关闭，项目设置、企业策略仍可能覆盖全局配置。回归验证使用合成会话和本地模拟学习服务，不代表用户真实任务已经成功。

### 配置式接入新客户端

能执行命令 Hook、提供 SDK 回调，或由自己的客户端桥接层发送 JSON 的 Agent，可以使用通用入口，不需要新增专属 MemWeave Python 脚本。下面是一个不在内置 Agent 名单中的示例配置：

```json
{
  "agent_id": "my-agent",
  "recall_event": "Question",
  "learn_event": "Finished",
  "event_field": "event.name",
  "fields": {
    "session_id": "metadata.session",
    "turn_id": "metadata.turn",
    "cwd": "metadata.cwd",
    "prompt": "request.message",
    "turn": "result.turn"
  },
  "recall_output": {"context": "{{context}}"}
}
```

先在本地 Runtime 中登记并启用同名 Agent，再连接事件入口。未知客户端使用已有 Runtime 鉴权调用 `POST /v1/agents/register`，请求体如下；仅创建 JSON 配置不会绕过登记或停用检查：

```json
{
  "agent_id": "my-agent",
  "display_name": "My Agent",
  "adapter_type": "custom",
  "capabilities": ["recall", "learn", "shared-knowledge"]
}
```

客户端在提问前和完成后执行同一个命令，把事件 JSON 写入 stdin，并消费 stdout 中的 JSON：

```shell
python -m agent_knowledge_bridge.hooks.generic_learning_hook --profile /absolute/path/my-agent.json
```

提问前的输入示例：

```json
{
  "event": {"name": "Question"},
  "metadata": {"session": "session-1", "turn": "turn-1"},
  "request": {"message": "部署 widget 时应该执行什么验证？"}
}
```

完成事件把 `event.name` 换成 `Finished`，并提供 `result.turn`，结构与下一节的统一 `turn` 相同。这条内联学习路径需要在线 Runtime，并同步等待提炼完成；客户端应在后台任务或完成回调中调用，不能阻塞下一次提问前的召回。离线时输出空 JSON，不把聊天原文写入磁盘等待重试。

如果客户端已经有受支持格式的本地会话文件，可在配置中添加 `transcript_format`（`claude`、`codex` 或 `gemini`）以及 `fields.transcript_path` 字段映射，完成事件只提交文件路径，走持久化后台队列。不要同时提供路径和内联 `turn`。自动格式选择只适用于内置 Agent，不会猜测未知客户端的会话格式。

需要依据会话文件绑定召回轮次时，可设置 `bind_transcript_boundary: true`；未知 Agent 必须同时指定受支持的 `transcript_format`。配置的会话格式和绑定设置在在线 Runtime 与本地回退中都生效，不会因为客户端名称相同就改用另一种解析器。默认关闭自定义配置的会话边界绑定，显式 `turn_id` 仍可用于去重与归因。

配置支持简单的点分对象路径，不支持任意代码、表达式或动态加载插件。上下文模板只能使用完整值 `{{context}}` 和 `{{event}}`；零召回始终输出 `{}`。JSON 配置限制为 64 KiB，事件输入限制为 2 MB。未知事件、停用 Agent、无效配置及 Runtime 错误均不会阻断客户端。标准化后的 session、turn、项目及工具证据仍经过原有边界校验。

内置接入也可直接使用通用入口，例如：

```shell
python -m agent_knowledge_bridge.hooks.generic_learning_hook --agent gemini-cli
```

### 能统一与不能统一的部分

事件连接、传输和治理可以统一；不同客户端的私有会话格式仍保留薄解析器。Codex 桌面会话路径补全也保留在 Codex 的格式适配模块中。没有事件回调或上下文注入能力的客户端，不能仅靠登记实现自动学习与注入。自定义配置不会代替厂商提供这些能力；只有明确选定目标配置及已确认协议的安装操作才会修改客户端设置。

## Gemini CLI

当前适配使用官方 `BeforeAgent` 触发召回、`AfterAgent` 提交后台学习，支持 Gemini 的 JSON 和 JSONL 会话记录。在“Agent 维护”中加入 Gemini CLI；以前只登记为 Runtime API 的用户点击“修复接入”，然后重启 Gemini CLI 和 MemWeave Runtime。

配置写入 `~/.gemini/settings.json`。设置了 `GEMINI_CLI_HOME` 时，使用 `<GEMINI_CLI_HOME>/.gemini/settings.json`，不会覆盖已有模型、登录或第三方 Hook 配置，修改前保留一次备份。本机接口核对版本为 Gemini CLI 0.62.0；代码测试不等于真实模型会话验收。

Hook 必须在客户端启用。`hooksConfig.enabled: false` 或单独禁用了 `memweave-beforeagent` / `memweave-afteragent` 时，MemWeave 显示接入待修复，不擅自打开所有第三方 Hook。项目级配置或企业策略也可能覆盖用户设置，可以用 Gemini 的 `/hooks panel` 检查最终生效状态。

召回只向当前轮注入匹配的已准入知识，零结果输出空 JSON。学习只提交本地会话引用和轮次校验信息，队列不复制原始聊天。JSONL 固定读取结束位置；会被整体改写的 JSON 使用消息数量和本轮哈希检查，来源变化会拒绝学习，而不是错误归因到下一轮。

Gemini 工具状态 `success` 只表示工具调用完成，不足以证明 shell 测试通过。必须有明确退出码等证据；失败、取消和运行中分别保留失败或未知状态。新知识仍遵守 candidate 准入和证据晋升规则。

## 其它 Agent 的统一输入

客户端适配器先把自己的单轮格式转换为 `user_text`、`assistant_text` 和 `tools`，调用 `POST /v1/learning/turn`。不要求伪装成 Claude transcript，也不依赖模型主动调用 MCP。

```python
from agent_knowledge_bridge.runtime_client import MemWeaveRuntimeClient

client = MemWeaveRuntimeClient(
    base_url="http://127.0.0.1:8765",
    token=runtime_token,  # 由本机配置获取，不写进源码。
    agent_id="custom-agent",
    project_key="my-project",
)
result = client.learn(
    session_id="session-1",
    turn_id="turn-1",
    turn={
        "user_text": "部署 widget 前使用项目的 verifier。",
        "assistant_text": "已执行验证。",
        "tools": [{
            "id": "tool-1",
            "name": "Shell",
            "input": {"command": "python verify_widget.py"},
            "output": "PASS",
            "exit_code": 0,
        }],
    },
)
```

只有文本 `PASS`、没有退出码时，不能据此认定测试成功。不要传模型猜测的成功标志。工具 ID 在同轮内必须唯一，不能同时提交 `turn` 和 `transcript_path`。文本、工具数量和工具输入长度有上限；输入在提炼前脱敏，原始用户和助手文本不写入学习队列。

统一内联输入是同步接口，需要允许后台提炼耗时；客户端应在完成事件中调用，不能放在用户提问前的召回热路径。`POST /v1/learning/queue` 只接受可读的本地 `transcript_path`，不接受内联聊天，以免持久化私密原文。已支持的会话格式可显式设置 `transcript_format="claude" | "codex" | "gemini"`；未知 Agent 的 `auto` 格式会返回 422，不再默认当成 Claude 解析。

召回仍使用 `client.recall(...)`，客户端负责把返回的 `hookSpecificOutput.additionalContext` 放入当前轮上下文。共享项目、来源 Agent/会话、人工审核、停用和零召回规则与原生适配器一致。

## English Summary

All maintained native entry points now delegate to one shared executor. Client event names, fields, configuration locations, and context output are declarative profiles; memory policy runs in the protocol-neutral `LearningEngine`. Legacy imports, installed entry points, and the Runtime HTTP envelope remain compatible.

Native registration and repair now install launchers that invoke `generic_learning_hook` directly. `prepare_hook_installation` is read-only, `install_hook_plan` applies a confirmed protocol, and `inspect_hook_plan` verifies the actual launcher and configuration. Installation preserves third-party hooks, backs up existing settings, and does not rewrite unchanged files. Custom profiles are stored as managed snapshots; installed launchers work outside the repository. Configuration readiness does not prove a live client event. Automatic capability discovery for unknown clients is not implemented. WorkBuddy now has a confirmed `codebuddy-json` profile: registration or repair installs prompt/stop hooks in its settings and uses a thin parser for native JSONL. Tests use synthetic sessions; actual client execution is reported separately from configuration readiness.

Clients outside the built-in registry can use `python -m agent_knowledge_bridge.hooks.generic_learning_hook --profile /absolute/path/profile.json`. Profiles map JSON object paths and output placeholders; they do not execute arbitrary code or load plugins. Supported transcript files use the persistent reference-only queue. Inline turns require a live Runtime and are never spooled to disk. Private transcript formats still need thin parsers, and the client must provide an event callback and context-injection capability.

Register and enable the matching Agent ID first through the authenticated `/v1/agents/register` API with `adapter_type: "custom"`. A profile does not bypass registration or disabled-Agent checks.

Explicit `transcript_format` and `bind_transcript_boundary` settings apply to both online recall and local fallback. Unknown clients need a supported explicit format to enable transcript boundary binding; otherwise they can use explicit turn IDs without parsing a transcript.

Hooks are client events; the Runtime is the local processing boundary. Gemini CLI now maps `BeforeAgent` to recall and `AfterAgent` to queued learning. Enable or repair it in Agent Maintenance, then restart both the client and the Runtime. User configuration is preserved and backed up. Disabled hooks remain disabled. The interface was checked against Gemini CLI 0.62.0; automated adapter tests are not a live-model acceptance test.

Other clients can submit normalized `turn` data to `/v1/learning/turn`, or choose an explicit supported `transcript_format`. The synchronous inline API does not spool private conversation text. The asynchronous queue accepts local transcript references only. Command completion is not proof of test success; candidate admission, objective evidence, project isolation, and zero-result behavior remain unchanged.

Gemini references: [Hook specification](https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md), [configuration and controls](https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/index.md), [native recording types](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/services/chatRecordingTypes.ts).
