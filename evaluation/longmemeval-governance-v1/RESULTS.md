# LongMemEval 自动准入审计结果

## Material Passport

- Origin Skill: academic-research-suite / experiment-agent
- Origin Mode: run
- Origin Date: 2026-10-16
- Verification Status: COMPLETED；分母与源码哈希已核验，非独立治理标签验证
- Version Label: longmemeval-governance-audit-v1

## 结论先说

现有准入门槛在这轮公开历史压力审计中偏保守：832 条官方用户证据中，217 条保留为 candidate（26.08%），615 条被过滤，没有一条自动转为 active。另外产生了 48 次自动晋升，检查其中的原文发现，普通叙述也可能因词面组合被当成长期授权。

因此，这轮结果不能作为“自动治理效果已达标”的背书。它暴露出两个需要优先核查的边界：**混合消息的事实保留**和**长期指令的授权识别**。前者受本轮整轮候选适配器影响很大，后者也受固定类型分类影响；不能直接推断真实提取器上线后的错误率。

本实验只测试固定原文候选下的 Core 自动准入，不是 LongMemEval 官方问答成绩，也不是实际模型提取、MCP 治理或整个生命周期的效果评测。

## 数据与执行

- 数据来源：[LongMemEval](https://github.com/xiaowu0162/LongMemEval) / [cleaned 数据](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)。使用 cleaned S 全部 500 题，不挑题。
- 固定数据 revision：`98d7416c24c778c2fee6e6f3006e7a073259d48f`。
- 数据 SHA-256：`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`。
- 122,416 次用户消息出现，93,931 种不同原文。重复历史不是独立样本。
- 固定候选构造与评分边界见 [协议](README.md)：整条用户原文作为候选，不看问题、答案或证据标记选句；全部声明 durable，由 Core 实际决定准入。
- 逐题独立临时 SQLite，通过真实 `LearningEngine.learn()` 执行，16 个进程；不人工批准、不伪造工具成功。
- 运行耗时 897.91 秒，500/500 完成，进程退出码 0；无异常重试或跳题。
- Python 3.12.3，Windows 11。调用外部模型 0 次，未读取生产数据库，未改变产品治理规则。
- 该数据此前用于过检索诊断，本轮不是全新盲测。运行前冻结协议、脚本和 Core 源码，结束后哈希一致。

## 全部消息的最终处理状态

| 状态 | 消息出现次数 | 占全部输入 |
| --- | ---: | ---: |
| filtered：未保留 | 89,545 | 73.1481% |
| candidate：保留、等待审核 | 32,633 | 26.6575% |
| active：自动晋升后生效 | 48 | 0.0392% |
| adapter_oversize：整轮超过 8,000 字符，未接入 | 190 | 0.1552% |
| 合计 | 122,416 | 100% |

数据库最终共有 32,257 条 candidate、48 条 active；这是 500 个隔离数据库的记录数相加，不是全数据集跨库去重。candidate 的消息次数多于记录数，是因为相同内容被合并。48 次自动晋升对应 37 种不同原文，不能当成 48 个独立的正确批准样本。

过滤率不是正确过滤率，也不是人工工作量节省率；未标为答题证据的消息，仍可能有长期价值。32,633 条输入留在 candidate，也说明本轮不能证明系统已能让“大多数知识无需人审”。

### 过滤原因

| 原因 | 次数 |
| --- | ---: |
| uncertain_knowledge | 87,539 |
| temporary_knowledge | 1,133 |
| invalid_source_evidence | 836 |
| memory_storage_opt_out | 25 |
| 逐候选原因未记录 | 12 |
| 合计 | 89,545 |

`uncertain_knowledge` 包含疑问、猜测等规则，并不等于这些消息全部是错误知识。`invalid_source_evidence` 也是综合校验拒绝原因，不能全部解释成引用造假。

最后 12 条的逐候选 rejection_reasons 为空；核对原文均命中现有敏感存储请求快路径，该路径在 reviewer 之前返回零候选。这里保留原始计数，不把空原因偷偷填成其他分类。也提示后续统计应区分“提取前抑制”和“候选准入拒绝”。

## 官方证据是否被保留

排除 30 道 abstention 题后，419 道题含官方标注的用户证据，共 832 条消息；另有 51 道正例题没有用户侧证据，不把它们算成用户证据零保留。54 条助手侧证据单列，未作为独立候选测试。

| 指标 | 结果 |
| --- | ---: |
| 用户证据保留，包含待审 candidate | 217 / 832，26.08% |
| 用户证据 active | 0 / 832，0% |
| 用户证据被过滤 | 615 / 832，73.92% |
| 用户证据因整轮过长未接入 | 0 / 832 |
| 至少保留一条用户证据的题目 | 177 / 419，42.24% |
| 用户证据全部保留的题目 | 53 / 419，12.65% |

615 条被过滤证据中，570 条是 `uncertain_knowledge`，45 条是 `temporary_knowledge`。**证据进入 candidate 不代表普通检索已经可用**，因此不能把 26.08% 写成自动治理后的召回率。

| 问题类型 | 官方用户证据保留（含待审） |
| --- | ---: |
| single-session-user | 23 / 64，35.94% |
| single-session-preference | 2 / 44，4.55% |
| single-session-assistant 中含用户证据的部分 | 2 / 5，40.00% |
| multi-session | 102 / 322，31.68% |
| temporal-reasoning | 52 / 253，20.55% |
| knowledge-update | 36 / 144，25.00% |

这是消息证据保留分布，不是六类任务的问答准确率。5 条证据的子组尤其不能据此泛化。

## 看到的两个具体边界

### 1. 同一轮的事实和提问，被一起按疑问处理

- 现象：消息既陈述了用户事实，又追加一个问题；整轮候选进入准入后，事实也一起被过滤。临时词与长期属性混合时也有类似情况。
- 根因：本轮适配器故意不抽取断言，整条消息原样送入；当前疑问、不确定、临时性检查会作用于整份证据。真实 reviewer 通常会选取较短引用，因此这个数字不等于真实提取路径误杀率。
- 改进方向：在实际提取路径验证“按断言划定证据边界”，保留相邻限定词，避免为了通过准入而截掉否定、临时或猜测上下文。不能仅删除问号、放宽规则来追分。

### 2. 有持续性词语，不一定是在授权 Agent 长期执行

- 现象：既有明确要求今后按某种语言回答的真正指令，也有回忆经历、谈未来打算的普通叙述被自动晋升。全部 48 次晋升中已经观察到后一类，但没有独立金标准，不给出“其中多少正确”的伪精度。
- 根因：固定适配器把带 `remember / always` 等词的消息归为 preference；Core 的持久性请求或指令判定也依赖词面条件，部分组合没有严格区分“向 Agent 发指令”和“谈自己的经历/计划”。
- 改进方向：自动晋升要绑定明确的指令主体、动作和持续范围；自述、引文、建议、未来打算不因零散词语共现直接获得授权。真实长期偏好是否允许自动生效，也应有单独且可审计的政策。

以上是代码与样例支持的诊断，不是完整人工标注结论。本轮未修规则或据此重跑取更好分数。

## 开销与验收

- 实际 learn 调用：122,226 次；另 190 条过长输入保留在总分母中。
- 16 进程并发负载下，learn P50 为 79.78 ms，P95 为 296.69 ms。包含编译、SQLite 写入、审计及资源争用，不含外部提取模型，不能与 search-only 延迟或单请求 TTFT 比较。
- 评测脚本测试：10 项通过；包含金标隔离、长输入不截断、实际准入与晋升、重复消息去重、证据分母不静默丢失。
- 治理相关定向测试：104 项通过。
- 全量回归：851 项测试、25 项子测试通过。
- 500 个唯一题目索引齐全，逐题结果重新聚合与报告完全一致，输入与源码哈希不变。

回归通过验证实现边界，不证明公开数据上的效果优秀。没有独立治理标签，`auto_accept_precision` 和 `correct_filter_rate` 均为 null；不声称自动批准准确率、端到端收益、人工工作量节省或统计显著性。

## 复现与材料

```powershell
python scripts/evaluate_longmemeval_governance.py --dataset data/longmemeval/longmemeval_s_cleaned.json --output outputs/longmemeval-governance-20261016 --workers 16
python -m pytest tests/test_longmemeval_governance.py tests/test_tiered_admission.py tests/test_governance_control.py -q
python -m pytest -q
```

输出目录必须全新；复跑时换新路径，不能覆盖此轮输出。公开目录不包含原文会话。逐题状态、manifest 和机器可读报告保留在被 Git 忽略的 `outputs/longmemeval-governance-20261016/`。

- 仓库基准 HEAD：`291edcad79b5ccb2ce274cb980fc7980d5fed4cf`；运行包含此前未提交的治理实现，不能只检出该 HEAD 就声称同一版本。具体文件哈希见本地 manifest。
- 冻结脚本 SHA-256：`422eae75c7ca1d934454729dd531d142455081d13c14256422ed0087ee10faea`。
- 冻结源码映射 SHA-256：`06eb3f38a8699914eac8626157c4624a545678c37ee323564a086fc9f32f0584`，按 JSON 键排序、紧凑分隔序列化后计算。
- `manifest.json` SHA-256：`a60cfbf26e60fb66b2c16780b7f012a11c7f19c39c53ec4a73d7f7dc6a22abcb`。
- `report.json` SHA-256：`883098fa94e9addef5987ca0ece60b8ff35b7915bb773dba39cd513aa1abb944`。
- `cases.jsonl` SHA-256：`75367b8911d3df7c8a09d6d62fe5ab6972a2f421dcf32d3bc872f5f1841ff774`。

## 这轮没测什么

真实提取模型的质量、工具成功证据的覆盖、跨轮隐式反馈、MCP 规划效果、归并/替代的正确性、冲突人工兜底收益、LFHV 及最终回答均不在本轮效果评估范围。LongMemEval 的生活历史记忆需求和 Coding Agent 的稳定规则存储政策也不完全相同；这轮适合暴露准入风险，不足以单独证明完整自动治理能力。
