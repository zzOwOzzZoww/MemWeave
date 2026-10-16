# 更新日志 / Changelog

## main — 2026-10-17

这是 `v0.5.0a2` 之后的源码更新，尚未单独建立新 tag 或 Release；安装 GitHub `main` 可获得这些改动。没有更新旧 Release 附件，也没有发布 PyPI。

### 新增

- **分级知识准入**：明确、稳定且证据完整的用户规则可按条件自动晋升；普通知识保留待审；临时状态和不确定推测默认不保存。明确要求长期保留时，也只能作为带限定说明的候选，不能自动当成事实。
- **MCP 知识治理控制面**：AI 可以读取候选与证据、生成治理预览、查看计划并执行已有权限的操作。项目内完全重复的候选可确定性归并；批准、隔离、归档和版本替代仍由用户在管理页确认。
- **治理审计与有条件撤销**：批量操作在事务中复核，数据变化、过期计划和跨项目访问会被拦截；保留变更历史，有后续知识活动时不强行撤销。
- **诊断工具和说明**：补充分级准入、控制面使用、公开数据回放及证据保留审计工具。开发诊断与公开检索成绩分开，不把本地用例通过率当成对外效果指标。

### 修复

- 收紧证据、适用范围和长期授权的对应关系，防止截掉限定词、跨句借用授权，或把模型置信度、无关命令成功当成批准依据。
- 改善混合消息的独立事实保留、复合明确指令的引用覆盖和同轮去重；精确复用候选不依赖标题，也不进行模糊覆盖或跨项目归并。
- 未恢复的客观失败只保留为待审观察，丢弃模型猜测的根因和修复步骤；末次失败、末次状态未知及无关检查成功都不认定为修复完成。
- 严格区分合法零提案与空响应、坏 JSON、截断响应和输出预算耗尽；错误进入既有有界重试流程，不伪装成“没有知识”。后台提取输出预算默认上限为 8192，可通过 `MW_REVIEW_MAX_TOKENS` 在 512–8192 内调整。
- 修复 Claude Code 在 Windows / Git Bash 下可能吞掉 Hook 路径反斜杠的问题。已有用户需执行“修复接入”并检查实际使用的自定义配置；仅升级源码不代表所有旧配置已迁移。
- 安装验收流程补齐 LICENSE 复制与校验；分发包包含治理管理页。

### 验证与边界

- 全量自动化回归：`1260 passed, 25 subtests passed`，包括治理权限、原子性、证据准入、经验恢复、检索回归、Hook 和 Runtime。测试数量有包含关系，不累加为评测样本量。
- 全新虚拟环境、中文/空格路径、wheel 安装、配置、Runtime、管理页及后台学习验收通过。三端已安装 Hook 命令在隔离 Runtime 下验证召回、候选生成、重复事件和零结果；安装与命令验收使用固定模型服务，不调用付费 API。
- 上述验收不等同于三个真实客户端新会话全部通过，也不证明开放领域提取准确率、任务成功率或人工审核量降低了某个比例。真实模型问题复测只是开发诊断，没有将原始响应、私人会话或本地部署产物上传。
- 补充 LongMemEval 已有归档回放的收益与代价说明：活跃集合规模不等于磁盘或索引体积，检索层结果不等于最终问答收益。本轮未重跑或更新公开 benchmark 成绩。

### 升级

先备份本地数据库和配置，再按 [README 升级步骤](README.md#已安装用户升级) 安装 GitHub 最新源码并运行 `memweave ui`。包版本仍为 `0.5.0a2`，升级同版本源码需要 `--force-reinstall`。需要 MCP 治理时，参见 [控制面使用说明](docs/知识治理控制面.md)，安装最新源码的 `mcp` 可选依赖并重新加载客户端工具。

### English summary

Post-`v0.5.0a2` source update on `main`; no new tag, Release assets, or PyPI publication. Adds evidence-scoped tiered admission and an optional MCP governance control plane with guarded previews, deterministic duplicate merging, explicit operator approval, audit history, and conditional undo. Tightens source evidence, scoped consent, failed-experience handling, model-response validation, and exact deduplication. Fixes Claude Code Hook quoting on Windows/Git Bash and adds installation license checks. The full local suite passes **1,260 tests and 25 subtests**; isolated installation and three-agent Hook command checks pass. These are engineering checks, not new public benchmark scores or proof of real-client session success.
