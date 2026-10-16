# 拒答与误召回案例归因（2026-10-08）

## Material Passport

- Origin Skill: academic-research-suite / experiment-agent
- Origin Mode: validate
- Verification Status: ANALYZED
- Review Author: Codex 助手；尚未经过独立标注复核。
- Scope: 读取已保存的 LoCoMo、LongMemEval 检索输出；不调用模型、不访问外部服务、不读取本地用户知识库或 Hook 日志。

## 结论摘要

现有报告里的“拒答净空率”不能解释为 MemWeave 的实际拒答成功率或误注入率。LoCoMo 的 0% 来自底层 `KnowledgeStore.search()` 结果非空，脚本没有记录 Adapter 的实际上下文注入；LongMemEval 的 `_abs` 题则普遍标注了可用于解释“为什么信息不足”的相关会话，命中这些会话不等于错误注入。

本轮没有调整检索代码、阈值或历史标签。旧基准已经参与过开发，只能用于归因和回归，不能再作为盲测结果。

## LoCoMo

数据来自本地已保存的 `locomo-retrieval-20260929-v6`：共 1,986 题，其中 1,532 道有证据正例、454 道按当前口径要求空检索。454 道由 Category 5 的 446 道、Category 3 的 7 道和 Category 1 的 1 道组成；报告显示它们全部至少返回一个 session，所以 `abstention_clean_rate` 为 0%。

对 Category 5 的逐题键值做了对照：446 道中有 416 道（93.27%）的 Top-5 包含标注的 adversarial source session，另 30 道没有命中该 session。该结果表示候选检索常找到了与问题有关、但基准要求不能直接作答的对抗性上下文；它本身既不能证明最终答案应答，也不能证明最终发生了注入。

未命中标注 adversarial source session 的 30 个案例 ID：

```text
conv-26:qa-188, conv-30:qa-83, conv-30:qa-92, conv-41:qa-155, conv-41:qa-160,
conv-41:qa-162, conv-41:qa-166, conv-41:qa-182, conv-42:qa-207, conv-42:qa-210,
conv-42:qa-243, conv-43:qa-182, conv-43:qa-191, conv-43:qa-227, conv-44:qa-146,
conv-47:qa-170, conv-48:qa-194, conv-48:qa-207, conv-48:qa-208, conv-48:qa-230,
conv-49:qa-177, conv-49:qa-180, conv-49:qa-181, conv-49:qa-195, conv-50:qa-163,
conv-50:qa-181, conv-50:qa-185, conv-50:qa-186, conv-50:qa-193, conv-50:qa-197
```

另外 8 道非 Category-5 的空检索目标也全部返回了候选：Category 1 为 `conv-26:qa-38`；Category 3 为 `conv-26:qa-31`、`conv-26:qa-47`、`conv-49:qa-32`、`conv-49:qa-39`、`conv-49:qa-47`、`conv-50:qa-40`、`conv-50:qa-43`。同样只能说明候选非空，不能单凭此处判定注入错误。

口径原因已在脚本中确认：`scripts/evaluate_locomo_retrieval.py` 通过 `KnowledgeStore.search()` 直接取候选，并以 `retrieved_keys` 是否为空计算 `abstention_clean_rate`。Core 搜索本身包含答案充分性门，但该路径没有运行 Adapter 的完整 Hook 上下文组装和注入预算，也没有模型答案。因此这个指标应理解为“拒答题 Top-k 为空率”，不能叫作拒答成功率、误注入率或答案准确率。

## LongMemEval

以冻结的完整组第一次重复为准：30 道 `_abs` 问题的上下文全部非空。按题型，knowledge-update 为 6/6、multi-session 为 12/12、single-session-user 为 6/6、temporal-reasoning 为 5/6 至少命中一个标注的相关会话；总体是 29/30。唯一没有会话重合的案例 ID 是 `gpt4_fe651585_abs`。

这 29 个命中不能直接标成误召回。30 道 `_abs` 的标注答案中，20 道明确写“没有提到”，10 道明确写“信息不足”；相应会话可能正是拒答所需的背景证据。LongMemEval 这轮确实通过 Adapter 运行了答案充分性门，但预测文件只保存最终证据块和会话映射，没有保存内容级“是否足以支持拒答”的人工/盲化答案判定，也没有运行答案生成。因此 0% 的 `abstention_empty_rate` 不是拒答门失效证据。

已按问题和参考答案逐题归类，记录在 [longmemeval-abs-review.json](longmemeval-abs-review.json)：不同对象/上下文 12、不同主体/角色 4、缺少计算或比较的一部分 9、缺少数值 2、缺少时间窗口 1、未发生/未记录的事件 2。这个分类反映基准标注的语义，不是穷举整个 haystack 后的答案正确率判定。

代码层面可定位的限制是：`answerability_decision()` 对没有单主体元数据的候选返回 `answer_slots_unresolved`，而 `apply_answerability_gate()` 保留兼容输出。两份公开基准的整段会话导入都没有填充单主体 `subject_terms`；已保存的 LoCoMo 诊断也确实执行过 `answerability` 阶段，原因记录为 `supported_or_unresolved`。因此不能说它绕过了全部门禁，准确说法是“门禁无法解析充分的事实槽位时，仍可能放行主题相关记录”。

12 例机制诊断中的 `ABD-unrelated-control` 则有明确的手写标签：问 SQLite 备份策略却输出 WAL 并发说明，属于缺少所问关系，而不是不同版本或生命周期问题。它是真实保留的夹具误注入，但只有 3 个负例，不能外推线上比例。

另有 470 道正例中 22 道最终注入结果未包含任何标注答案会话：temporal-reasoning 9、single-session-user 5、single-session-preference 4、multi-session 3、single-session-assistant 1、knowledge-update 0。逐题预测保存了最终结果 trace，但未保存完整的仲裁前候选及逐项淘汰链路，故目前不能把这些失败可靠归因到 Direct 搜索、答案充分性门、仲裁或预算截断中的某一个阶段。

22 个正例漏召回 ID（按题型）：

- temporal-reasoning：`gpt4_7f6b06db`、`gpt4_e061b84f`、`gpt4_1e4a8aec`、`gpt4_59149c78`、`gpt4_4929293b`、`9a707b82`、`eac54add`、`gpt4_8279ba03`、`gpt4_fe651585`
- single-session-user：`e47becba`、`5d3d2817`、`ad7109d1`、`e01b8e2f`、`4100d0a0`
- single-session-preference：`75832dbd`、`09d032c9`、`d24813b1`、`d6233ab6`
- multi-session：`gpt4_f2262a51`、`c18a7dc8`、`8e91e7d9`
- single-session-assistant：`71a3fd6b`

## 留出评测准备

新协议见 [PROTOCOL.md](../memweave-cross-agent-v2-holdout/PROTOCOL.md)。已生成 20 个全新合成主题、120 个场景，按主题冻结 dev 30 / test 90，并为每条事实标注主体、关系、对象、有效时间、项目范围、预期证据和禁止证据。未复制 LoCoMo、LongMemEval 或 v1 的 50 个主题内容；精确文本无复用已校验，语义主题隔离是设计判断而非数学保证。

已补独立执行器和评分契约测试，观察真实 Adapter 的候选、Core 返回证据与实际注入；不修改生产逻辑。dev 预检因新评测器把嵌套阶段耗时纳入语义重复比较而中断，工具已修正，失败产物保留。遵守实验技能“不自动重试崩溃实验”的约束，本轮没有重跑，也没有运行 test；目前没有可报告的新成绩。

## 验证边界

本轮复算的是已有逐题预测，不是重新验证历史实验的可复现性。没有显著性检验、置信区间或因果收益声明。按实验技能完成 11/11 方法论检查：分组/总体反转无比较对象；不从会话级命中推断答案；承认合成与开发筛选偏差；无协变量调整；明确正负例分母；不把失败重测描述成泛化提升；保留所有失败；不筛选有利题型；披露协议变更与中断；不声称任务收益的因果关系；不作反向因果推断。新留出集的同主题场景不是独立样本。

## 工程校验

数据确定性重建与 SHA-256 校验通过，30 道 `_abs` 归因 ID 与原数据完全对齐。新增评测工具测试 19 passed；包含检索仲裁与消融脚本的定向测试 108 passed；全量测试 686 passed、9 subtests passed。这些是工程测试结果，不是留出集成绩。

所有新增内容限于本轮评测目录及 `tests/test_holdout_evaluation.py`；没有改动生产源码、现有基准、README，未提交或推送 GitHub。

## 建议

1. 将 LoCoMo 指标改称候选层指标；如需评价拒答，需单独评估最终答案或 Adapter 实际注入，并分别记录无关候选、相关但证据不足、充分支持三类结果。
2. 对 LongMemEval `_abs` 报告上下文非空、标注相关会话覆盖和内容级支持性，不能把“有上下文”本身当成错误。
3. 将上述正例漏召回、`_abs` 无会话重合案例和 LoCoMo 候选异常保留为回归/人工复核清单；它们已见于开发报告，不应放回新留出集。
