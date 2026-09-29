# MemWeave Cross-Agent Reuse Benchmark v1 (candidate)

一个完全合成、可公开分发的跨 Agent 知识复用与治理基准候选集。

## 数据规模与构成

- **500 条评测案例**，由 50 个独立的合成知识主题（kernel）各构造 10 种受控场景。
- 按知识主题分组切分，避免同一主题的十种变体同时出现在不同 split：`calibration` 100 条、`dev` 100 条、`test` 300 条。
- 10 个平衡场景，每类 50 条：跨 Agent 规则召回、跨语言召回、干扰项排序、项目范围隔离、知识更新、同会话噪声过滤、答案证据不足时拒答、近似主题拒答、LFHV 影子恢复候选、隔离/替代知识排除。
- 全部主体、项目、知识记录和对话背景均为人工编写的合成内容；不包含真实用户对话，不从 LoCoMo 等第三方数据集中复制样本。

**这不是 LoCoMo 的等价替代。**LoCoMo 有长篇真实对话和更多问答；本集只有 50 个合成知识主题，重点覆盖 MemWeave 特有的跨 Agent、范围与生命周期判定。500 条是场景行数，不是 500 个独立主题，也不代表 500 个真实用户任务。

## 文件

- `cases.jsonl`：发布的逐例数据。每行一个场景，包含 source/target Agent、当前 project context、可见的合成记忆记录、问题和人工标注的预期行为。
- `build_dataset.py`：确定性重建数据集。
- `validate_dataset.py`：校验数量、split、类别平衡、唯一 ID、知识引用和组间泄漏。
- `score_results.py`：对系统输出的证据 ID 评分，不调用模型。
- `schema.json`：JSONL 行结构说明。
- `DATA-LICENSE.md`：仅适用于本数据集的许可说明，不决定 MemWeave 源代码许可。

## 快速验证和计分

```powershell
python evaluation/memweave-cross-agent-v1/validate_dataset.py
python evaluation/memweave-cross-agent-v1/run_memweave.py `
  --split test `
  --output outputs/mw-cross-agent-test-run
python evaluation/memweave-cross-agent-v1/score_results.py `
  --predictions outputs/mw-cross-agent-test-run/predictions.jsonl `
  --split test `
  --output outputs/mw-cross-agent-test-run/report.json
python evaluation/memweave-cross-agent-v1/compare_naive_fts.py `
  --predictions outputs/mw-cross-agent-test-run/predictions.jsonl `
  --split test `
  --output outputs/mw-cross-agent-test-run/noise-comparison.json
```

预测文件每行格式：

```json
{"case_id":"MWX-021-01","emitted_evidence_ids":["MWX-021-01-memory-1"],"shadow_hit_ids":[]}
```

缺失案例按未命中处理；重复或未知案例 ID 会报错。评分器报告整体和分场景的决策准确率、正向证据召回率、拒答净空率、禁止证据注入率，以及 LFHV 影子候选发现率。评测方应冻结 MemWeave 版本和知识快照，并保存原始预测文件。

如需评估回答是否真的正确，可在预测行加 `"answer_correct": true/false`，该值必须来自确定性测试或不知道实验组别的人工评审；评分器会单独报告答案准确率和标注覆盖率。不能把检索命中当成任务成功，也不能让被测模型自行判定自己答对。预期答案与逐例评分要点见 `expected.answer` 和 `expected.answer_rubric`。

## 正确解释结果

本集是受控诊断基准，适合发现“跨 Agent 没找回”“不该用却注入”“旧值盖过新值”“范围串库”等回归。它不能单独证明生产环境的真实成功率提升、长期自学习收益、用户偏好适配能力或总体知识质量。模板结构较规则，模型或算法可能利用表面词汇；发布时应同时给出逐例错误、分组结果和局限，不能只报一个总分。

`LFHV` 场景的标准是：归档知识**不应进入正常输出**，但应出现在独立 shadow probe 的候选中；“影子命中”不自动等于恢复，也不等于任务收益。

## 2026-09-29：抗噪声对照结果

300 条冻结 `test` 回归案例上的 Naive FTS Top-3 对照结果已保存到 [noise-comparison-test-20260929.json](noise-comparison-test-20260929.json)。对照组使用同一套 SQLite FTS5/BM25 和查询分词，但关闭项目、生命周期、证据、版本替代、同会话和最低相关性门禁。

| 指标 | Naive FTS Top-3 | MemWeave |
|---|---:|---:|
| 决策准确率 | 54.33% | 100.00% |
| 正向证据召回率 | 70.67% | 100.00% |
| 负例误注入率 | 76.67% | 0.00% |
| 禁止证据注入率 | 55.67% | 0.00% |
| 无意义上下文体积 | 18,103 UTF-8 bytes | 0 bytes |

这组结果是术语归一化、技术标识符门禁和复合问题召回修复后的回归闭环。本轮已使用冻结 `test` 的失败簇定位问题，因此 100% 不能作为独立留出泛化成绩。UTF-8 bytes 不是模型 Token；Naive 延迟只测内存 FTS 查询，而 MemWeave 延迟包含完整本地 Adapter 路径，因此两者不能直接用于速度优劣结论。

## 2026-09-29：两种生命周期评测口径验收结果

上面的标准对应冻结的 `shadow-v1`，Runner 默认按此模式显式关闭自动恢复，保留与 v1 标签的兼容性。MemWeave 新默认行为是“按需复用、同步复活”，请用 `--profile on-demand-v2` 单独验收；它只在内存中调整 LFHV 类预期，原 `cases.jsonl` 和历史基线保持不变。

新模式同时检查本轮输出与实际恢复，并报告 `lfhv_same_turn_recovery_rate`。预测行记录 `profile` 和 `restored_evidence_ids`，评分器拒绝混用口径、未知证据引用和没有本轮输出的恢复归功。缺失预测不算正确拒答。

最终验收结果：

| profile | 决策准确率 | 正向召回 | 拒答净空率 | 禁止证据注入 | LFHV 指标 |
|---|---:|---:|---:|---:|---:|
| `shadow-v1` | 100.00% | 100.00% | 100.00% | 0.00% | 影子候选发现率 100.00% |
| `on-demand-v2` | 100.00% | 100.00% | 100.00% | 0.00% | 同轮恢复率 100.00% |

复现命令：

```powershell
python evaluation/memweave-cross-agent-v1/run_memweave.py --split test --profile shadow-v1 --output outputs/mw-shadow-v1-test
python evaluation/memweave-cross-agent-v1/score_results.py --predictions outputs/mw-shadow-v1-test/predictions.jsonl --split test --profile shadow-v1 --output outputs/mw-shadow-v1-test/report.json
python evaluation/memweave-cross-agent-v1/run_memweave.py --split test --profile on-demand-v2 --output outputs/mw-demand-v2-test
python evaluation/memweave-cross-agent-v1/score_results.py --predictions outputs/mw-demand-v2-test/predictions.jsonl --split test --profile on-demand-v2 --output outputs/mw-demand-v2-test/report.json
```

新版 Runner 每例独立数据库，按目标使用 Codex 或 Claude Adapter，标题只取原始内容，不使用场景标签。数据量与真实任务边界不变：500 条仍来自 50 个合成主题。两种 profile 的总分不可直接互换；[BASELINE.md](BASELINE.md) 继续保留首次运行的历史低分，没有被新结果覆盖。

## 版本和完整性

- Dataset version: `mw-cross-agent-v1.0.0-candidate`
- Build: `python build_dataset.py`
- Validate: `python validate_dataset.py`
- SHA-256: 运行 validator 查看；公开发布时把结果一并打 tag。

当前为发布候选集，需由维护者最终审阅标注和许可后再打正式版本标签。
